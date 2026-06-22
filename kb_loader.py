"""
kb_loader.py — YAML Knowledge Base Loader + Weighted Confidence Matching Engine

Loads YAML KB files from kb/*.yaml (excluding template.yaml), builds an inverted
index for fast lookups, and provides weighted confidence scoring for incident
matching with confidence-based routing.

Design:
    - load_all(): reads kb/*.yaml, merges into a list
    - quick_match_weighted(): weighted keyword/title/phrase matching
    - route_by_confidence(): returns 'direct' / 'context' / 'llm_only'
    - search_keyword(): search across title, keywords, root_cause
    - get_entry(): fetch single entry by ID
    - Falls back to engine.KNOWLEDGE_BASE if YAML directory is missing
"""

import glob
import logging
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

# ── Paths ──────────────────────────────────────────────────────────────

KB_DIR = Path(__file__).parent / "kb"
TEMPLATE_FILENAME = "template.yaml"

# ── Logging ────────────────────────────────────────────────────────────

logger = logging.getLogger("kb_loader")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "[%(levelname)s] %(name)s: %(message)s"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ── Module-level cache ─────────────────────────────────────────────────

_KNOWLEDGE_BASE = None     # list[dict] — all entries
_INVERTED_INDEX = None     # dict[str, list[str]] — word -> [kb_id, ...]
_TITLE_INDEX = None        # dict[str, list[str]] — word -> [kb_id, ...]
_ROOTCAUSE_INDEX = None    # dict[str, list[str]] — word -> [kb_id, ...]


# ══════════════════════════════════════════════════════════════════════
#   Internal helpers
# ══════════════════════════════════════════════════════════════════════

def _tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens (non-empty)."""
    if not text:
        return []
    return list(set(re.findall(r"[a-zA-Z0-9\u4e00-\u9fff_\-]+", text.lower())))


def _load_yaml_files() -> list[dict] | None:
    """Read all YAML files in KB_DIR, excluding template.yaml.

    Supports both single-entry YAML files (dict) and multi-entry
    YAML files (list of dicts).
    """
    if not KB_DIR.is_dir():
        logger.warning("KB directory does not exist: %s", KB_DIR)
        return None

    yaml_files = sorted(glob.glob(str(KB_DIR / "*.yaml")))
    # Exclude template.yaml
    yaml_files = [f for f in yaml_files
                  if not f.endswith(TEMPLATE_FILENAME)]

    if not yaml_files:
        logger.warning("No YAML files found in %s (excluding template.yaml)",
                       KB_DIR)
        return None

    # Sort so category files load first and individual kb*.yaml
    # files load last, giving individual files dedup priority
    yaml_files.sort(key=lambda f: (
        1 if Path(f).name.startswith("kb") else 0,
        f
    ))

    entries = []
    for fpath in yaml_files:
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)

            if isinstance(data, list):
                # File contains a list of entries (e.g., category yamls)
                for item in data:
                    if isinstance(item, dict):
                        entries.append(_normalize_entry(item, fpath))
            elif isinstance(data, dict):
                # Single entry file
                entries.append(_normalize_entry(data, fpath))
            else:
                logger.warning("Skipping %s: not a valid YAML mapping or list",
                               fpath)
        except yaml.YAMLError as exc:
            logger.error("YAML parse error in %s: %s", fpath, exc)
        except Exception as exc:
            logger.error("Error reading %s: %s", fpath, exc)

    # Deduplicate by ID (last loaded wins — individual kb*.yaml override
    # category yaml files since they're loaded alphabetically later)
    seen_ids = {}
    deduped = []
    for entry in entries:
        eid = entry["id"]
        if eid in seen_ids:
            # Replace previous entry with same ID
            logger.debug("Duplicate entry %s: replacing with latest", eid)
            for i, existing in enumerate(deduped):
                if existing["id"] == eid:
                    deduped[i] = entry
                    break
        else:
            seen_ids[eid] = True
            deduped.append(entry)

    return deduped if deduped else None


def _normalize_entry(data: dict, fpath: str) -> dict | None:
    """Validate and normalize a single KB entry dict."""
    if not data.get("id"):
        logger.warning("Skipping entry in %s: missing 'id' field", fpath)
        return None
    if not data.get("keywords"):
        logger.warning("Skipping entry %s in %s: missing or empty 'keywords'",
                       data.get("id", "?"), fpath)
        return None
    data.setdefault("confidence_weight", 1.0)
    data.setdefault("root_cause", "")
    data.setdefault("common_causes", [])
    data.setdefault("check_commands", [])
    data.setdefault("fix_commands", [])
    data.setdefault("category", "general")
    if isinstance(data.get("keywords"), str):
        data["keywords"] = [data["keywords"]]
    return data


def _load_from_engine_fallback() -> list[dict]:
    """Fallback: load KNOWLEDGE_BASE from engine.py if available."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from engine import KNOWLEDGE_BASE
        entries = []
        for item in KNOWLEDGE_BASE:
            entry = dict(item)
            entry.setdefault("confidence_weight", 1.0)
            entry.setdefault("root_cause", "")
            entries.append(entry)
        logger.info("Fallback: loaded %d entries from engine.KNOWLEDGE_BASE",
                     len(entries))
        return entries
    except Exception as exc:
        logger.error("Fallback to engine.KNOWLEDGE_BASE failed: %s", exc)
        return []


def _build_inverted_indexes(entries: list[dict]):
    """Build word-level inverted indexes for fast matching."""
    global _INVERTED_INDEX, _TITLE_INDEX, _ROOTCAUSE_INDEX

    kw_idx = defaultdict(list)
    title_idx = defaultdict(list)
    rc_idx = defaultdict(list)

    for entry in entries:
        kb_id = entry["id"]

        # Index keywords (each keyword token contributes to the inverted index)
        for kw in entry.get("keywords", []):
            for token in _tokenize(kw):
                if kb_id not in kw_idx[token]:
                    kw_idx[token].append(kb_id)

        # Index title
        for token in _tokenize(entry.get("title", "")):
            if kb_id not in title_idx[token]:
                title_idx[token].append(kb_id)

        # Index root_cause
        for token in _tokenize(entry.get("root_cause", "")):
            if kb_id not in rc_idx[token]:
                rc_idx[token].append(kb_id)

    _INVERTED_INDEX = dict(kw_idx)
    _TITLE_INDEX = dict(title_idx)
    _ROOTCAUSE_INDEX = dict(rc_idx)

    logger.debug("Inverted indexes built: %d keyword tokens, %d title tokens, "
                 "%d root_cause tokens",
                 len(_INVERTED_INDEX), len(_TITLE_INDEX), len(_ROOTCAUSE_INDEX))


# ══════════════════════════════════════════════════════════════════════
#   Public API
# ══════════════════════════════════════════════════════════════════════

def load_all(force_reload: bool = False) -> list[dict]:
    """
    Load and return the full knowledge base as a list of dicts.

    Tries YAML files first (KB_DIR / *.yaml, excluding template.yaml).
    Falls back to engine.KNOWLEDGE_BASE if YAML loading fails or directory
    is missing.

    Results are cached in module-level _KNOWLEDGE_BASE.
    """
    global _KNOWLEDGE_BASE

    if _KNOWLEDGE_BASE is not None and not force_reload:
        return _KNOWLEDGE_BASE

    # Strategy 1: load from YAML files
    entries = None
    if yaml is not None:
        entries = _load_yaml_files()
    else:
        logger.warning("PyYAML not installed, cannot load YAML KB files")

    # Strategy 2: fallback to engine.KNOWLEDGE_BASE
    if entries is None:
        entries = _load_from_engine_fallback()

    _KNOWLEDGE_BASE = entries

    # Build inverted indexes if we have entries
    if _KNOWLEDGE_BASE:
        _build_inverted_indexes(_KNOWLEDGE_BASE)
        logger.info("Knowledge base loaded: %d entries", len(_KNOWLEDGE_BASE))
    else:
        logger.warning("Knowledge base is empty")

    return _KNOWLEDGE_BASE


def quick_match_weighted(
    incident_text: str,
    min_score: float = 0.0
) -> list[dict]:
    """
    Match incident_text against the knowledge base using weighted confidence.

    Scoring algorithm (per KB entry):
        - Each keyword match:                           +0.15
        - Title contains the matched keyword word:      +0.30
        - Exact quoted phrase match:                    +0.10 (extra)
        - Matched keyword count >= 3:                   +0.20 (bonus)
        - Multiply by entry's confidence_weight
        - Final score clamped to [0.0, 1.0]

    Returns list[dict] sorted by score descending, with fields:
        kb_id, title, category, score, confidence_weight,
        matched_keywords, check_commands, common_causes, fix_commands, root_cause
    """
    entries = load_all()
    if not entries:
        return []

    text_lower = incident_text.lower()
    results = []

    # Tokenize incident for phrase detection
    # (Exact quoted phrases: e.g. "slow query" in incident)
    quoted_phrases = re.findall(r'"([^"]+)"', incident_text)
    quoted_phrases_lower = [p.lower() for p in quoted_phrases]

    for entry in entries:
        matched_kws = []
        title_lower = entry.get("title", "").lower()

        for kw in entry.get("keywords", []):
            kw_lower = kw.lower()
            if kw_lower in text_lower:
                matched_kws.append(kw)

        if not matched_kws:
            continue

        # ── Score calculation ──
        score = 0.0
        num_matched = len(matched_kws)

        # +0.15 per keyword match
        score += num_matched * 0.15

        # +0.3 if title contains any matched keyword word
        for kw in matched_kws:
            for word in _tokenize(kw):
                if word in title_lower:
                    score += 0.3
                    break  # one-time bonus per entry, not per keyword

        # +0.1 extra per exact quoted phrase match
        for phrase in quoted_phrases_lower:
            for kw in matched_kws:
                if kw.lower() == phrase or phrase in kw.lower():
                    score += 0.1
                    break

        # +0.2 bonus if 3+ keywords matched
        if num_matched >= 3:
            score += 0.2

        # Multiply by confidence_weight
        cw = float(entry.get("confidence_weight", 1.0))
        score *= cw

        # Clamp to [0.0, 1.0]
        score = max(0.0, min(1.0, score))

        if score < min_score:
            continue

        results.append({
            "kb_id": entry["id"],
            "title": entry["title"],
            "category": entry.get("category", "general"),
            "score": round(score, 4),
            "confidence_weight": cw,
            "matched_keywords": matched_kws,
            "check_commands": entry.get("check_commands", []),
            "common_causes": entry.get("common_causes", []),
            "fix_commands": entry.get("fix_commands", []),
            "root_cause": entry.get("root_cause", ""),
        })

    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def route_by_confidence(score: float) -> str:
    """
    Route a score to one of three confidence levels:

        score >= 0.7  → 'direct'    (return KB entry directly, no LLM)
        score >= 0.4  → 'context'   (KB as context + LLM analysis)
        score <  0.4  → 'llm_only'  (LLM analysis only)
    """
    if score >= 0.7:
        return "direct"
    elif score >= 0.4:
        return "context"
    else:
        return "llm_only"


def _ensure_indexes():
    """Ensure inverted indexes are built."""
    global _INVERTED_INDEX, _TITLE_INDEX, _ROOTCAUSE_INDEX
    if _INVERTED_INDEX is None:
        entries = load_all()
        if entries:
            _build_inverted_indexes(entries)


def search_keyword(q: str) -> list[dict]:
    """
    Search knowledge base for keyword 'q' across titles, keywords, and root_cause.
    Returns matching entries (deduplicated by ID).
    """
    entries = load_all()
    if not entries:
        return []

    # Build id->entry lookup once
    entry_map = {e["id"]: e for e in entries}

    # Ensure indexes are built
    _ensure_indexes()

    kw_index = _INVERTED_INDEX or {}
    title_index = _TITLE_INDEX or {}
    rc_index = _ROOTCAUSE_INDEX or {}

    q_lower = q.lower()
    tokens = _tokenize(q_lower)
    if not tokens:
        return []

    matched_ids = set()

    # Search in keyword index
    for token in tokens:
        if token in kw_index:
            matched_ids.update(kw_index[token])

    # Search in title index
    for token in tokens:
        if token in title_index:
            matched_ids.update(title_index[token])

    # Search in root_cause index
    for token in tokens:
        if token in rc_index:
            matched_ids.update(rc_index[token])

    # Also do direct substring matching on title and root_cause
    # for cases where multi-word queries don't tokenize perfectly
    for entry in entries:
        eid = entry["id"]
        if eid in matched_ids:
            continue
        if q_lower in entry.get("title", "").lower():
            matched_ids.add(eid)
        elif q_lower in entry.get("root_cause", "").lower():
            matched_ids.add(eid)
        else:
            for kw in entry.get("keywords", []):
                if q_lower in kw.lower():
                    matched_ids.add(eid)
                    break

    # Sort by relevance: entries whose title/root_cause contain the query first
    def _sort_key(eid):
        entry = entry_map[eid]
        title_lower = entry.get("title", "").lower()
        rc_lower = entry.get("root_cause", "").lower()
        # Prefer exact title match > keyword match > root_cause match
        if q_lower in title_lower:
            return 0
        for kw in entry.get("keywords", []):
            if q_lower in kw.lower():
                return 1
        if q_lower in rc_lower:
            return 2
        return 3

    matched = [entry_map[eid] for eid in sorted(matched_ids, key=_sort_key)]
    return matched


def get_entry(kb_id: str) -> dict | None:
    """
    Get a single knowledge base entry by its ID (e.g., 'KB001').
    Returns None if not found.
    """
    entries = load_all()
    if not entries:
        return None
    for entry in entries:
        if entry["id"] == kb_id:
            return dict(entry)
    return None


# ── Module initialization ──────────────────────────────────────────────

# Auto-load on import so the module is immediately usable
load_all()


# ══════════════════════════════════════════════════════════════════════
#   Command-line usage
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Knowledge Base Loader")
    parser.add_argument("action", choices=["list", "search", "get", "match"],
                        default="list", nargs="?")
    parser.add_argument("query", nargs="?", default="",
                        help="Search query or KB ID")
    parser.add_argument("--min-score", type=float, default=0.0,
                        help="Minimum confidence score for matching")
    args = parser.parse_args()

    if args.action == "list":
        kb = load_all()
        print(f"Knowledge Base: {len(kb)} entries")
        for entry in kb:
            cw = entry.get("confidence_weight", 1.0)
            print(f"  {entry['id']:>6} | {entry['title']:<40} | cw={cw}")

    elif args.action == "search":
        if not args.query:
            print("Error: search requires a query string")
            sys.exit(1)
        results = search_keyword(args.query)
        print(f"Search results for '{args.query}': {len(results)} match(es)")
        for entry in results:
            print(f"  {entry['id']:>6} | {entry['title']}")

    elif args.action == "get":
        if not args.query:
            print("Error: get requires a KB ID (e.g., KB004)")
            sys.exit(1)
        entry = get_entry(args.query)
        if entry:
            print(json.dumps(entry, ensure_ascii=False, indent=2))
        else:
            print(f"Entry '{args.query}' not found")

    elif args.action == "match":
        if not args.query:
            print("Error: match requires incident text")
            sys.exit(1)
        results = quick_match_weighted(args.query, min_score=args.min_score)
        print(f"Match results for '{args.query}': {len(results)} match(es)")
        for r in results:
            route = route_by_confidence(r["score"])
            print(f"  {r['score']:.4f} [{route:>8}] {r['kb_id']:>6} | {r['title']}")
            print(f"       matched: {r['matched_keywords']}")
