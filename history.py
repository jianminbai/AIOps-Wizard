"""
history.py — Incident History & Feedback Loop

SQLite-backed incident recording with:
  - Full incident context preservation (text, metrics, logs, analysis)
  - Human feedback collection (confirmed root cause, effective fix, MTTR)
  - Similar incident retrieval (keyword + optional embedding cosine)
  - KB confidence_weight auto-tuning based on feedback
  - MTTR statistics per category

Usage:
  from history import record_incident, search_similar, submit_feedback, get_stats
"""

import json
import sqlite3
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("aiops-history")

DB_PATH = Path(__file__).parent / ".incidents.db"


def _get_conn() -> sqlite3.Connection:
    """Get a thread-safe DB connection with WAL mode."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            id              TEXT PRIMARY KEY,
            created_at      TEXT NOT NULL,
            client_ip       TEXT DEFAULT 'unknown',
            incident_text   TEXT NOT NULL,
            metrics_json    TEXT DEFAULT '{}',
            logs_text       TEXT DEFAULT '',
            route           TEXT NOT NULL,          -- direct / context / llm_only
            confidence_score REAL DEFAULT 0.0,
            top_kb_id       TEXT DEFAULT '',
            all_matches_json TEXT DEFAULT '[]',
            analysis_json   TEXT DEFAULT '{}',
            raw_llm_json    TEXT DEFAULT '',
            provider        TEXT DEFAULT 'deepseek',
            model           TEXT DEFAULT 'deepseek-chat',
            elapsed_ms      REAL DEFAULT 0.0,
            -- Embedding (optional, for semantic search)
            embedding_json  TEXT DEFAULT '[]',
            -- Human feedback
            feedback_status TEXT DEFAULT 'pending',  -- pending / confirmed / rejected
            confirmed_kb_id TEXT DEFAULT '',
            fix_applied     TEXT DEFAULT '',
            fix_effective   INTEGER DEFAULT 0,       -- 0=unknown, 1=yes, -1=no
            actual_mttr_min INTEGER DEFAULT 0,
            feedback_notes  TEXT DEFAULT '',
            feedback_at     TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS kb_effectiveness (
            kb_id           TEXT PRIMARY KEY,
            times_matched   INTEGER DEFAULT 0,
            times_confirmed INTEGER DEFAULT 0,
            times_rejected  INTEGER DEFAULT 0,
            avg_mttr_min    REAL DEFAULT 0.0,
            updated_at      TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at);
        CREATE INDEX IF NOT EXISTS idx_incidents_route ON incidents(route);
        CREATE INDEX IF NOT EXISTS idx_incidents_feedback ON incidents(feedback_status);
        CREATE INDEX IF NOT EXISTS idx_incidents_text ON incidents(incident_text);
    """)
    conn.commit()
    conn.close()
    logger.info("History DB initialized at %s", DB_PATH)


# ── Record ────────────────────────────────────────────────────────────

def record_incident(
    incident_text: str,
    route: str,
    confidence_score: float,
    matches: list,
    analysis: dict,
    raw_llm: str = "",
    client_ip: str = "unknown",
    metrics: Optional[dict] = None,
    logs: str = "",
    provider: str = "deepseek",
    model: str = "deepseek-chat",
    elapsed_ms: float = 0.0,
    embedding: Optional[list[float]] = None,
) -> str:
    """Record an analysis event. Returns the incident ID."""
    incident_id = f"INC-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{_short_hash()}"
    created_at = datetime.now(timezone.utc).isoformat()

    top_kb = matches[0]["kb_id"] if matches else ""
    embedding_json = json.dumps(embedding or [], ensure_ascii=False)

    conn = _get_conn()
    conn.execute("""
        INSERT INTO incidents (
            id, created_at, client_ip, incident_text, metrics_json, logs_text,
            route, confidence_score, top_kb_id, all_matches_json,
            analysis_json, raw_llm_json, provider, model, elapsed_ms, embedding_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        incident_id, created_at, client_ip, incident_text,
        json.dumps(metrics or {}, ensure_ascii=False),
        logs, route, confidence_score, top_kb,
        json.dumps(matches, ensure_ascii=False),
        json.dumps(analysis, ensure_ascii=False),
        raw_llm, provider, model, elapsed_ms, embedding_json,
    ))

    # Update KB effectiveness counters
    for m in matches[:5]:
        kb_id = m["kb_id"]
        conn.execute("""
            INSERT INTO kb_effectiveness (kb_id, times_matched, times_confirmed, times_rejected, avg_mttr_min, updated_at)
            VALUES (?, 1, 0, 0, 0.0, ?)
            ON CONFLICT(kb_id) DO UPDATE SET
                times_matched = times_matched + 1,
                updated_at = ?
        """, (kb_id, created_at, created_at))

    conn.commit()
    conn.close()
    logger.info("Recorded incident: id=%s route=%s score=%.3f", incident_id, route, confidence_score)
    return incident_id


# ── Feedback ──────────────────────────────────────────────────────────

def submit_feedback(
    incident_id: str,
    confirmed_kb_id: str = "",
    fix_applied: str = "",
    fix_effective: bool | None = None,
    actual_mttr_min: int = 0,
    notes: str = "",
) -> bool:
    """Submit human feedback for a previous incident analysis."""
    now = datetime.now(timezone.utc).isoformat()
    eff_val = 1 if fix_effective is True else (-1 if fix_effective is False else 0)
    status = "confirmed" if confirmed_kb_id else "rejected"

    conn = _get_conn()
    cursor = conn.execute("""
        UPDATE incidents SET
            feedback_status = ?,
            confirmed_kb_id = ?,
            fix_applied = ?,
            fix_effective = ?,
            actual_mttr_min = ?,
            feedback_notes = ?,
            feedback_at = ?
        WHERE id = ?
    """, (status, confirmed_kb_id, fix_applied, eff_val, actual_mttr_min, notes, now, incident_id))

    if cursor.rowcount == 0:
        conn.close()
        logger.warning("Feedback for unknown incident: %s", incident_id)
        return False

    # Update KB effectiveness: confirmed/rejected counts & MTTR
    if confirmed_kb_id:
        conn.execute("""
            INSERT INTO kb_effectiveness (kb_id, times_matched, times_confirmed, times_rejected, avg_mttr_min, updated_at)
            VALUES (?, 0, 1, 0, ?, ?)
            ON CONFLICT(kb_id) DO UPDATE SET
                times_confirmed = times_confirmed + 1,
                avg_mttr_min = (avg_mttr_min * times_confirmed + ?) / (times_confirmed + 1),
                updated_at = ?
        """, (confirmed_kb_id, actual_mttr_min, now, actual_mttr_min, now))

    conn.commit()
    conn.close()
    logger.info("Feedback recorded: incident=%s status=%s", incident_id, status)
    return True


# ── Search Similar ────────────────────────────────────────────────────

def search_similar(
    incident_text: str,
    limit: int = 5,
    embedding: Optional[list[float]] = None,
) -> list[dict]:
    """Find historically similar incidents.

    Uses keyword overlap as primary signal. If embedding is provided,
    also computes cosine similarity for ranking.
    """
    conn = _get_conn()

    # Tokenize the query for keyword matching
    import re
    query_tokens = set(re.findall(r"[a-zA-Z0-9一-鿿_\-]+", incident_text.lower()))

    rows = conn.execute("""
        SELECT id, created_at, incident_text, route, confidence_score,
               top_kb_id, all_matches_json, analysis_json, feedback_status,
               confirmed_kb_id, fix_applied, fix_effective, actual_mttr_min,
               embedding_json
        FROM incidents
        ORDER BY created_at DESC
        LIMIT 200
    """).fetchall()

    results = []
    for row in rows:
        row_tokens = set(re.findall(r"[a-zA-Z0-9一-鿿_\-]+", row["incident_text"].lower()))
        if not query_tokens or not row_tokens:
            continue
        overlap = len(query_tokens & row_tokens)
        if overlap == 0:
            continue

        jaccard = overlap / len(query_tokens | row_tokens)

        results.append({
            "id": row["id"],
            "created_at": row["created_at"],
            "incident_text": row["incident_text"],
            "route": row["route"],
            "confidence_score": row["confidence_score"],
            "top_kb_id": row["top_kb_id"],
            "feedback_status": row["feedback_status"],
            "confirmed_kb_id": row["confirmed_kb_id"],
            "fix_applied": row["fix_applied"],
            "fix_effective": bool(row["fix_effective"]) if row["fix_effective"] else None,
            "actual_mttr_min": row["actual_mttr_min"],
            "similarity": round(jaccard, 4),
            "analysis": json.loads(row["analysis_json"]) if row["analysis_json"] else {},
        })

    results.sort(key=lambda x: x["similarity"], reverse=True)

    # If embedding provided, re-rank top candidates by cosine similarity
    if embedding and len(embedding) > 0 and len(results) > 0:
        for r in results:
            try:
                emb = json.loads(row["embedding_json"])
                if emb and len(emb) == len(embedding):
                    dot = sum(a * b for a, b in zip(embedding, emb))
                    norm_a = sum(a * a for a in embedding) ** 0.5
                    norm_b = sum(b * b for b in emb) ** 0.5
                    if norm_a > 0 and norm_b > 0:
                        r["cosine_similarity"] = round(dot / (norm_a * norm_b), 4)
            except (json.JSONDecodeError, TypeError):
                pass
        # If cosine scores exist, blend with jaccard
        if any("cosine_similarity" in r for r in results):
            for r in results:
                cos = r.get("cosine_similarity", 0.0)
                r["similarity"] = round(r["similarity"] * 0.3 + cos * 0.7, 4)
            results.sort(key=lambda x: x["similarity"], reverse=True)

    conn.close()
    return results[:limit]


# ── Statistics ────────────────────────────────────────────────────────

def get_stats(days: int = 30) -> dict:
    """Get usage & effectiveness statistics."""
    conn = _get_conn()

    total_incidents = conn.execute(
        "SELECT COUNT(*) as c FROM incidents"
    ).fetchone()["c"]

    route_counts = {}
    for row in conn.execute(
        "SELECT route, COUNT(*) as c FROM incidents GROUP BY route"
    ).fetchall():
        route_counts[row["route"]] = row["c"]

    feedback_rate = 0
    if total_incidents > 0:
        with_feedback = conn.execute(
            "SELECT COUNT(*) as c FROM incidents WHERE feedback_status != 'pending'"
        ).fetchone()["c"]
        feedback_rate = round(with_feedback / total_incidents * 100, 1)

    # Top effective KB entries
    top_kb = []
    for row in conn.execute("""
        SELECT kb_id, times_matched, times_confirmed, times_rejected, avg_mttr_min
        FROM kb_effectiveness
        WHERE times_confirmed > 0
        ORDER BY times_confirmed DESC LIMIT 10
    """).fetchall():
        top_kb.append({
            "kb_id": row["kb_id"],
            "times_matched": row["times_matched"],
            "times_confirmed": row["times_confirmed"],
            "times_rejected": row["times_rejected"],
            "accuracy": round(row["times_confirmed"] / max(row["times_matched"], 1) * 100, 1),
            "avg_mttr_min": round(row["avg_mttr_min"], 1),
        })

    # Recent incidents count (last N days)
    cutoff = datetime.now(timezone.utc).isoformat()
    recent = conn.execute(
        "SELECT COUNT(*) as c FROM incidents WHERE created_at >= datetime('now', ?)",
        (f'-{days} days',)
    ).fetchone()["c"]

    # MTTR by category
    mttr_by_route = {}
    for row in conn.execute("""
        SELECT route, AVG(actual_mttr_min) as avg_mttr, COUNT(*) as c
        FROM incidents WHERE actual_mttr_min > 0
        GROUP BY route
    """).fetchall():
        mttr_by_route[row["route"]] = {
            "avg_min": round(row["avg_mttr"], 1),
            "count": row["c"],
        }

    conn.close()
    return {
        "total_incidents": total_incidents,
        "recent_incidents": recent,
        "days": days,
        "route_distribution": route_counts,
        "feedback_rate_pct": feedback_rate,
        "top_effective_kb": top_kb,
        "mttr_by_route": mttr_by_route,
    }


def get_recent_incidents(limit: int = 20, with_feedback_only: bool = False) -> list[dict]:
    """Get recent incident records for the UI."""
    conn = _get_conn()
    where = "WHERE feedback_status != 'pending'" if with_feedback_only else ""
    rows = conn.execute(f"""
        SELECT id, created_at, incident_text, route, confidence_score,
               top_kb_id, feedback_status, fix_applied, fix_effective, actual_mttr_min
        FROM incidents
        {where}
        ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_incident(incident_id: str) -> dict | None:
    """Get full incident record by ID."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    # Parse JSON fields
    for field in ["metrics_json", "all_matches_json", "analysis_json", "embedding_json"]:
        try:
            d[field] = json.loads(d[field])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


# ── Helpers ───────────────────────────────────────────────────────────

def _short_hash() -> str:
    """Generate a short random hex string."""
    import hashlib
    import random
    raw = f"{time.time()}-{random.random()}"
    return hashlib.md5(raw.encode()).hexdigest()[:6]


# Auto-initialize on import
init_db()
