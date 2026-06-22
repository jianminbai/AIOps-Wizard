"""
AI Ops Troubleshooting Assistant — FastAPI Backend

Enterprise-grade version with:
  - YAML knowledge base + confidence-based routing
  - API Key authentication
  - Per-IP rate limiting
  - Daily LLM call quota (save token cost)
  - Structured logging
  - LLM timeout & retry
  - File-locked KB writes
  - Multi-provider support (DeepSeek, OpenAI, Claude, OpenRouter)
  - Incident history with feedback loop
  - LLM response caching
  - Semantic matching (synonym expansion + optional embeddings)
  - Prometheus Alertmanager webhook
  - Feishu/DingTalk/WeCom bot integration
  - Multi-turn conversation
  - Markdown report export

Env vars:
  AIOPS_API_KEY          If set, all requests must include X-API-Key header
  AIOPS_RATE_LIMIT       Max requests per minute per IP (default: 5)
  AIOPS_LLM_PER_IP_DAILY Max LLM calls per IP per day (default: 30)
  AIOPS_LLM_GLOBAL_DAILY Max LLM calls globally per day (default: 500)
  AIOPS_DIRECT_THRESHOLD Confidence threshold for direct KB answer (default: 0.7)
  AIOPS_LOG_LEVEL        Logging level: DEBUG, INFO, WARNING, ERROR (default: INFO)
  LLM_TIMEOUT            LLM API timeout in seconds (default: 60)
  LLM_MAX_RETRIES        LLM API max retries (default: 1)
  FEISHU_WEBHOOK_URL     Feishu bot webhook URL
  DINGTALK_WEBHOOK_URL   DingTalk bot webhook URL
  WECOM_WEBHOOK_URL      WeCom bot webhook URL
"""
import os
import sys
import json
import time
import uuid
import logging
import subprocess
from pathlib import Path
from typing import Optional
from datetime import date, datetime, timezone
from collections import defaultdict

# fcntl is Unix-only; import conditionally for file locking
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    fcntl = None  # type: ignore
    _HAS_FCNTL = False

# ── Logging setup ─────────────────────────────────────────────────

LOG_LEVEL = os.environ.get("AIOPS_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("aiops-api")

sys.path.insert(0, str(Path(__file__).parent))
from engine import (
    SYSTEM_PROMPT, build_user_prompt, build_context, parse_llm_response
)
from kb_loader import (
    load_all, quick_match_weighted, route_by_confidence,
    get_entry, search_keyword
)
from semantic_match import (
    expand_query, extract_entities_llm, semantic_match,
    MATCH_ENGINE_AVAILABLE, EMBEDDING_AVAILABLE,
)
from history import (
    record_incident, submit_feedback, search_similar,
    get_stats, get_recent_incidents, get_incident,
)
from cache import get_cached, set_cached, cache_stats, clear_cache
from bot import send_alert, format_analysis_for_chat
from report import generate_markdown_report, generate_summary_report

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel
    import uvicorn
except ImportError:
    print("Installing dependencies...")
    subprocess.run([
        sys.executable, "-m", "pip", "install",
        "fastapi", "uvicorn", "openai", "pydantic", "httpx", "pyyaml"
    ], check=True)
    print("Dependencies installed. Restarting...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

app = FastAPI(title="AI Ops Troubleshooting Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════════════
#  Security: API Key Auth + Rate Limit + Daily Quota
# ══════════════════════════════════════════════════════════════════════

AIOPS_API_KEY = os.environ.get("AIOPS_API_KEY", "").strip()
RATE_LIMIT_PER_MIN = int(os.environ.get("AIOPS_RATE_LIMIT", "5"))
# Daily LLM quota: per-IP limit AND global cap
LLM_PER_IP_DAILY = int(os.environ.get("AIOPS_LLM_PER_IP_DAILY", "30"))
LLM_GLOBAL_DAILY = int(os.environ.get("AIOPS_LLM_GLOBAL_DAILY", "500"))
DIRECT_THRESHOLD = float(os.environ.get("AIOPS_DIRECT_THRESHOLD", "0.7"))

# ── Rate limiter (per-IP, in-memory) ──
_rate_store: dict[str, list[float]] = defaultdict(list)

def _check_rate_limit(ip: str) -> tuple[bool, int]:
    """Returns (allowed, remaining). Minutes sliding window."""
    now = time.time()
    window = 60.0
    _rate_store[ip] = [t for t in _rate_store[ip] if now - t < window]
    count = len(_rate_store[ip])
    if count >= RATE_LIMIT_PER_MIN:
        return False, 0
    _rate_store[ip].append(now)
    return True, RATE_LIMIT_PER_MIN - count - 1

# ── Daily LLM quota (persisted: per-IP + global cap) ──
_QUOTA_FILE = Path(__file__).parent / ".llm_quota.json"

def _get_today() -> str:
    return date.today().isoformat()

def _read_quota() -> dict:
    try:
        data = json.loads(_QUOTA_FILE.read_text())
        if data.get("date") == _get_today():
            return data
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    return {"date": _get_today(), "ips": {}, "global_llm": 0, "global_direct": 0}

def _save_quota(data: dict):
    _QUOTA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def _check_llm_quota(ip: str) -> tuple[bool, int, int]:
    """Returns (allowed, remaining_this_ip, remaining_global).
       Only counts LLM calls (direct KB hits are free).
    """
    data = _read_quota()
    ip_data = data.get("ips", {}).get(ip, {"llm": 0})
    ip_used = ip_data.get("llm", 0)
    global_used = data.get("global_llm", 0)

    if ip_used >= LLM_PER_IP_DAILY:
        return False, 0, LLM_GLOBAL_DAILY - global_used
    if global_used >= LLM_GLOBAL_DAILY:
        return False, LLM_PER_IP_DAILY - ip_used, 0

    return True, LLM_PER_IP_DAILY - ip_used, LLM_GLOBAL_DAILY - global_used

def _record_llm_call(ip: str):
    data = _read_quota()
    ips = data.setdefault("ips", {})
    ip_data = ips.setdefault(ip, {"llm": 0, "direct": 0})
    ip_data["llm"] = ip_data.get("llm", 0) + 1
    data["global_llm"] = data.get("global_llm", 0) + 1
    _save_quota(data)

def _record_direct_call(ip: str):
    data = _read_quota()
    ips = data.setdefault("ips", {})
    ip_data = ips.setdefault(ip, {"llm": 0, "direct": 0})
    ip_data["direct"] = ip_data.get("direct", 0) + 1
    data["global_direct"] = data.get("global_direct", 0) + 1
    _save_quota(data)

# ── Auth middleware ──
async def verify_request(request: Request):
    """Middleware: check API key, rate limit, return 429/401 if violated."""
    # Skip auth for health check, frontend, and CORS preflight
    path = request.url.path
    method = request.method
    if path in ("/health", "/") or path.startswith("/static") or method == "OPTIONS":
        return

    # API Key check (if configured)
    if AIOPS_API_KEY:
        client_key = request.headers.get("X-API-Key", "")
        # Also allow query parameter: ?api_key=xxx
        query_key = request.query_params.get("api_key", "")
        if client_key != AIOPS_API_KEY and query_key != AIOPS_API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized: missing or invalid X-API-Key header")

    # Rate limit check
    client_ip = request.client.host if request.client else "unknown"
    allowed, remaining = _check_rate_limit(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {RATE_LIMIT_PER_MIN} requests/min per IP"
        )

# Register middleware
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    try:
        await verify_request(request)
    except HTTPException as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"success": False, "error": e.detail}
        )
    response = await call_next(request)
    # Add security headers
    response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_PER_MIN)
    if AIOPS_API_KEY:
        response.headers["X-Auth-Required"] = "api-key"
    return response


# ── Pydantic Models ──────────────────────────────────

MAX_INCIDENT_LENGTH = 10_000
MAX_LOGS_LENGTH = 50_000
MAX_METRICS_KEYS = 200

VALID_PROVIDERS = {"deepseek", "openai", "openrouter", "claude", "anthropic"}
VALID_CATEGORIES = {"resource", "dependency", "application", "external", "change", "security", "business", "general"}
VALID_SEVERITIES = {"P0", "P1", "P2", "P3", "P4"}

from pydantic import field_validator, Field


class AnalyzeRequest(BaseModel):
    incident: str = Field(
        ...,
        min_length=1,
        max_length=MAX_INCIDENT_LENGTH,
        description="Incident/alert text describing the problem"
    )
    metrics: Optional[dict] = Field(
        None,
        description="Optional metrics data (max 200 keys)"
    )
    logs: Optional[str] = Field(
        None,
        max_length=MAX_LOGS_LENGTH,
        description="Optional log snippets"
    )
    model: str = Field(
        "deepseek-chat",
        min_length=1,
        max_length=100,
        description="LLM model name"
    )
    provider: str = Field(
        "deepseek",
        description="LLM provider (deepseek, openai, openrouter, claude, anthropic)"
    )
    api_key: Optional[str] = Field(
        None,
        max_length=256,
        description="User-provided API key (overrides env var)"
    )
    force_llm: bool = Field(
        False,
        description="Force LLM call even if KB direct match found"
    )

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        if v not in VALID_PROVIDERS:
            raise ValueError(f"Invalid provider '{v}'. Must be one of: {', '.join(sorted(VALID_PROVIDERS))}")
        return v

    @field_validator("metrics")
    @classmethod
    def validate_metrics_size(cls, v: Optional[dict]) -> Optional[dict]:
        if v and len(v) > MAX_METRICS_KEYS:
            raise ValueError(f"Metrics dict exceeds max of {MAX_METRICS_KEYS} keys")
        return v


class AnalyzeResponse(BaseModel):
    success: bool
    confidence_route: str = ""
    confidence_score: float = 0.0
    quota_remaining: int = 0
    kb_direct_answer: Optional[dict] = None
    quick_matches: list = []
    analysis: dict = {}
    raw: str = ""


class KBEntryRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="KB entry title")
    title_en: Optional[str] = Field("", max_length=200, description="English title")
    keywords: list[str] = Field(..., min_length=1, max_length=50, description="Matching keywords (1-50)")
    category: str = Field(..., description="Category (resource, dependency, application, external, change, general)")
    severity: Optional[str] = Field("P2", description="Severity: P0-P4")
    common_causes: list[str] = Field(..., min_length=1, max_length=20, description="Common root causes (1-20)")
    check_commands: list[str] = Field(default_factory=list, max_length=30, description="Commands to run for diagnosis")
    fix_commands: list[str] = Field(default_factory=list, max_length=30, description="Commands to run for remediation")
    tags: Optional[list[str]] = Field(default_factory=list, max_length=20, description="Tags")
    source: Optional[str] = Field("manual", max_length=50, description="Source of the entry")
    confidence_weight: Optional[float] = Field(1.0, ge=0.1, le=2.0, description="Confidence weighting factor (0.1-2.0)")

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category '{v}'. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}")
        return v

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: Optional[str]) -> Optional[str]:
        if v and v not in VALID_SEVERITIES:
            raise ValueError(f"Invalid severity '{v}'. Must be one of: {', '.join(sorted(VALID_SEVERITIES))}")
        return v


# ── LLM Call ──────────────────────────────────────────

# Timeout and retry config
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "1"))

# Provider base URLs
LLM_PROVIDERS = {
    "deepseek":    "https://api.deepseek.com/v1",
    "openai":      "https://api.openai.com/v1",
    "openrouter":  "https://openrouter.ai/api/v1",
    "claude":      "https://api.anthropic.com/v1",
    "anthropic":   "https://api.anthropic.com/v1",
}


def call_llm(messages: list, model: str, provider: str, api_key: str = None) -> str:
    """Call LLM API with timeout and retry.

    Supports: deepseek, openai, openrouter, claude/anthropic.
    Falls back to env vars for API keys.
    """
    import openai

    # Resolve API key
    if api_key:
        key = api_key
    else:
        # Try provider-specific key, then generic OPENAI_API_KEY,
        # then ANTHROPIC_API_KEY for Claude
        key = os.environ.get(
            f"{provider.upper()}_API_KEY",
            os.environ.get("OPENAI_API_KEY",
                os.environ.get("ANTHROPIC_API_KEY", "")
            )
        )

    if not key:
        logger.warning("No API key configured for provider=%s", provider)
        return json.dumps({
            "error": f"No API key configured for provider '{provider}'",
            "raw_response": ""
        })

    base_url = LLM_PROVIDERS.get(provider, LLM_PROVIDERS["deepseek"])
    logger.info("LLM call: provider=%s model=%s base_url=%s", provider, model, base_url)

    last_error = None
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            client = openai.OpenAI(
                api_key=key,
                base_url=base_url,
                timeout=LLM_TIMEOUT,
                max_retries=0,  # We handle retries ourselves
            )
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.1,
                max_tokens=2000,
            )
            content = resp.choices[0].message.content or ""
            logger.info(
                "LLM call succeeded: provider=%s model=%s tokens_used=%s",
                provider, model,
                getattr(resp.usage, 'total_tokens', 'unknown')
            )
            return content
        except Exception as e:
            last_error = e
            error_str = str(e)
            logger.warning(
                "LLM call attempt %d/%d failed: %s",
                attempt + 1, LLM_MAX_RETRIES + 1, error_str[:200]
            )
            if attempt < LLM_MAX_RETRIES:
                # Exponential backoff: 1s, 2s, 4s...
                backoff = 2 ** attempt
                logger.info("Retrying in %ds...", backoff)
                time.sleep(backoff)

    logger.error("LLM call failed after %d attempts: %s", LLM_MAX_RETRIES + 1, str(last_error)[:300])
    return json.dumps({
        "error": f"LLM API error after {LLM_MAX_RETRIES + 1} attempts: {str(last_error)}",
        "raw_response": str(last_error),
    })


def build_kb_context(kb_entry: dict) -> str:
    causes = "\n".join(f"  - {c}" for c in kb_entry.get("common_causes", []))
    checks = "\n".join(f"  $ {c}" for c in kb_entry.get("check_commands", []))
    fixes = "\n".join(f"  $ {f}" for f in kb_entry.get("fix_commands", []))
    return f"""
## Matched KB: {kb_entry.get('title', '')}
### Common Causes
{causes}
### Check Commands
{checks}
### Fix Commands
{fixes}
"""


# ── API Routes ────────────────────────────────────────

@app.get("/health")
def health(request: Request):
    ip = request.client.host if request.client else "unknown"
    kb = load_all()
    quota = _read_quota()
    # Get this IP's usage
    ip_data = quota.get("ips", {}).get(ip, {"llm": 0, "direct": 0})
    return {
        "status": "ok",
        "kb_count": len(kb),
        "kb_source": "yaml",
        "auth_required": bool(AIOPS_API_KEY),
        "rate_limit": RATE_LIMIT_PER_MIN,
        "quota": {
            "my_ip": ip,
            "my_llm_today": ip_data.get("llm", 0),
            "my_llm_limit": LLM_PER_IP_DAILY,
            "my_direct_today": ip_data.get("direct", 0),
            "global_llm_today": quota["global_llm"],
            "global_llm_limit": LLM_GLOBAL_DAILY,
            "remaining": LLM_PER_IP_DAILY - ip_data.get("llm", 0),
        }
    }


@app.get("/kb")
def list_kb(
    category: str = None,
    search: str = None,
    page: int = 1,
    page_size: int = 50,
):
    """List knowledge base entries with optional filtering, search, and pagination.

    Query params:
        category: Filter by category (resource, dependency, application, external, change)
        search:   Full-text search across titles, keywords, and root_cause
        page:     Page number (1-based, default 1)
        page_size: Entries per page (default 50, max 200)
    """
    # Clamp pagination
    page = max(1, page)
    page_size = max(1, min(page_size, 200))

    if search:
        results = search_keyword(search)
    else:
        results = load_all()

    if category:
        results = [e for e in results if e.get("category") == category]

    total = len(results)

    # Paginate
    start = (page - 1) * page_size
    end = start + page_size
    page_results = results[start:end]

    entries = [
        {
            "id": k["id"],
            "title": k.get("title", ""),
            "title_en": k.get("title_en", ""),
            "category": k.get("category", ""),
            "severity": k.get("severity", "P2"),
            "tags": k.get("tags", []),
            "source": k.get("source", ""),
            "keywords_count": len(k.get("keywords", [])),
        }
        for k in page_results
    ]

    return {
        "entries": entries,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }
    }


@app.get("/kb/{kb_id}")
def get_kb_entry(kb_id: str):
    entry = get_entry(kb_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"KB entry {kb_id} not found")
    return entry


@app.post("/kb")
def add_kb_entry(req: KBEntryRequest):
    """Add a new knowledge base entry to the appropriate YAML file.

    Uses file locking (fcntl on Unix, portalocker-style on Windows) to
    prevent race conditions during concurrent writes.
    """
    category_file = Path(__file__).parent / "kb" / f"{req.category}.yaml"
    if not category_file.exists():
        category_file = Path(__file__).parent / "kb" / "resource.yaml"

    logger.info("Adding KB entry: title=%s category=%s", req.title, req.category)

    import yaml
    lock_file = str(category_file) + ".lock"

    try:
        # Acquire lock (Unix: fcntl.flock; Windows: skip locking gracefully)
        lock_fd = None
        if _HAS_FCNTL:
            lock_fd = open(lock_file, "w")
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            except OSError:
                logger.debug("File locking failed; proceeding without lock")
        else:
            logger.debug("File locking unavailable on this platform; proceeding without lock")

        # Read existing entries
        with open(category_file, "r", encoding="utf-8") as f:
            entries = yaml.safe_load(f)
            if not isinstance(entries, list):
                logger.warning("YAML file %s is not a list; wrapping in list", category_file.name)
                entries = [entries] if entries else []
            entries = [e for e in entries if isinstance(e, dict)]

        # Find max ID number
        max_num = 0
        for e in entries:
            eid = str(e.get("id", "KB000"))
            try:
                num = int(eid.replace("KB", ""))
                max_num = max(max_num, num)
            except ValueError:
                pass

        new_id = f"KB{max_num + 1:03d}"

        # Build new entry
        new_entry = {
            "id": new_id,
            "title": req.title,
            "title_en": req.title_en or "",
            "keywords": req.keywords,
            "category": req.category,
            "severity": req.severity or "P2",
            "common_causes": req.common_causes,
            "check_commands": req.check_commands or [],
            "fix_commands": req.fix_commands or [],
            "tags": req.tags or [],
            "source": req.source or "manual",
            "confidence_weight": req.confidence_weight or 1.0,
        }
        entries.append(new_entry)

        # Write atomically: write to temp file, then rename
        tmp_file = str(category_file) + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            yaml.dump(entries, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        os.replace(tmp_file, str(category_file))  # Atomic on most OS

    except Exception as e:
        logger.error("Failed to add KB entry: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to write KB entry: {str(e)}")
    finally:
        # Release lock and clean up
        if lock_fd:
            try:
                lock_fd.close()
            except Exception:
                pass
        try:
            Path(lock_file).unlink(missing_ok=True)
        except Exception:
            pass

    # Invalidate KB cache so next load picks up the new entry
    import kb_loader
    kb_loader._KNOWLEDGE_BASE = None
    kb_loader._INVERTED_INDEX = None
    kb_loader._TITLE_INDEX = None
    kb_loader._ROOTCAUSE_INDEX = None

    logger.info("KB entry created: id=%s title=%s", new_id, req.title)
    return {"status": "created", "id": new_id, "entry": new_entry}


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest, request: Request = None):
    """Analyze with confidence routing + quota tracking.

    Routing:
      score >= DIRECT_THRESHOLD → DIRECT (no LLM, free)
      score >= 0.4              → CONTEXT (KB + LLM)
      score < 0.4               → LLM_ONLY
    """
    # Get client IP for per-IP quota tracking
    client_ip = request.client.host if (request and request.client) else "unknown"
    start_time = time.time()

    logger.info(
        "Analyze request: ip=%s incident_len=%d force_llm=%s provider=%s model=%s",
        client_ip, len(req.incident), req.force_llm, req.provider, req.model
    )

    # 1. Check cache first (only for non-force_llm requests)
    if not req.force_llm:
        cached = get_cached(req.incident, req.provider, req.model)
        if cached:
            elapsed = (time.time() - start_time) * 1000
            logger.info("Cache HIT: ip=%s elapsed_ms=%.0f", client_ip, elapsed)
            # Record in history even for cached responses
            record_incident(
                incident_text=req.incident,
                route=cached.get("confidence_route", "cached"),
                confidence_score=cached.get("confidence_score", 0),
                matches=cached.get("quick_matches", []),
                analysis=cached.get("analysis", {}),
                raw_llm=cached.get("raw", ""),
                client_ip=client_ip,
                metrics=req.metrics,
                logs=req.logs or "",
                provider=req.provider,
                model=req.model,
                elapsed_ms=elapsed,
            )
            return AnalyzeResponse(**cached)

    # 2. Weighted matching (with semantic enhancement)
    kb_entries = load_all()
    matches_kw = quick_match_weighted(req.incident, min_score=0.0)

    # If semantic matching available and KB is large enough, blend scores
    if MATCH_ENGINE_AVAILABLE and len(kb_entries) >= 15:
        matches_semantic = semantic_match(req.incident, kb_entries, top_k=10)
        # Merge: use semantic scores, keep keyword-matched keywords
        merged = {}
        for m in matches_kw:
            merged[m["kb_id"]] = m
        for m in matches_semantic:
            if m["kb_id"] in merged:
                # Blend: 60% semantic + 40% keyword
                merged[m["kb_id"]]["score"] = round(
                    m["score"] * 0.6 + merged[m["kb_id"]]["score"] * 0.4, 4
                )
            else:
                merged[m["kb_id"]] = {**m, "matched_keywords": m.get("match_signals", {}).get("matched_keywords", [])}
        matches = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
    else:
        matches = matches_kw

    top_match = matches[0] if matches else None
    score = top_match["score"] if top_match else 0.0
    route = route_by_confidence(score) if top_match else "llm_only"
    # Use configured threshold for direct
    if route == "direct" and score < DIRECT_THRESHOLD:
        route = "context"

    if req.force_llm and route == "direct":
        route = "context"

    logger.info(
        "Routing: ip=%s route=%s score=%.4f kb_id=%s",
        client_ip, route, score, top_match.get("kb_id", "none") if top_match else "none"
    )

    quota = _read_quota()
    ip_data = quota.get("ips", {}).get(client_ip, {"llm": 0, "direct": 0})

    # 2. DIRECT route (no LLM call)
    if route == "direct" and top_match and not req.force_llm:
        kb_entry = get_entry(top_match["kb_id"])
        _record_direct_call(client_ip)
        elapsed = (time.time() - start_time) * 1000
        logger.info(
            "DIRECT response: ip=%s kb_id=%s score=%.4f elapsed_ms=%.0f",
            client_ip, top_match["kb_id"], score, elapsed
        )
        analysis = {
            "classification": kb_entry.get("category", ""),
            "severity_assessment": "true_fault",
            "root_cause_hypotheses": [
                {
                    "rank": i + 1,
                    "cause": cause,
                    "probability": "high" if i == 0 else "medium",
                    "evidence": "Matched KB: " + kb_entry.get("title", ""),
                    "verify_steps": kb_entry.get("check_commands", []),
                }
                for i, cause in enumerate(kb_entry.get("common_causes", []))
            ],
            "immediate_actions": [
                f"Refer to '{kb_entry.get('title', '')}' troubleshooting steps"
            ],
            "mitigation_suggestions": kb_entry.get("common_causes", []),
            "commands": {
                "check": kb_entry.get("check_commands", []),
                "fix": kb_entry.get("fix_commands", []),
            },
            "source": "knowledge_base",
        }
        raw_str = json.dumps(kb_entry, ensure_ascii=False, indent=2)

        # Record in history
        record_incident(
            incident_text=req.incident,
            route="direct", confidence_score=score,
            matches=matches[:5], analysis=analysis, raw_llm=raw_str,
            client_ip=client_ip, metrics=req.metrics, logs=req.logs or "",
            provider=req.provider, model=req.model, elapsed_ms=elapsed,
        )
        return AnalyzeResponse(
            success=True,
            confidence_route="direct",
            confidence_score=score,
            quota_remaining=LLM_PER_IP_DAILY - ip_data.get("llm", 0),
            kb_direct_answer=kb_entry,
            quick_matches=matches[:5],
            analysis=analysis,
            raw=raw_str,
        )

    # Check LLM quota before proceeding (per-IP + global)
    llm_allowed, ip_remaining, global_remaining = _check_llm_quota(client_ip)
    if not llm_allowed:
        reason = "你的今日 LLM 额度已用完" if ip_remaining == 0 else "全局 LLM 额度已用完"
        logger.warning(
            "LLM quota exceeded: ip=%s ip_remaining=%d global_remaining=%d",
            client_ip, ip_remaining, global_remaining
        )
        return AnalyzeResponse(
            success=False,
            confidence_route="quota_exceeded",
            confidence_score=score,
            quota_remaining=0,
            quick_matches=matches[:5],
            analysis={"error": f"{reason}（每 IP 每天 {LLM_PER_IP_DAILY} 次，全局每天 {LLM_GLOBAL_DAILY} 次）"},
            raw="",
        )

    # 3. Build prompt
    user_content = build_user_prompt(req.incident)
    context = build_context(req.metrics, req.logs)
    if context:
        user_content += f"\n\n{context}"
    if route == "context" and top_match:
        kb_entry = get_entry(top_match["kb_id"])
        if kb_entry:
            user_content += f"\n\n{build_kb_context(kb_entry)}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content}
    ]

    # 4. Call LLM
    raw_response = call_llm(
        messages, model=req.model, provider=req.provider, api_key=req.api_key
    )
    _record_llm_call(client_ip)

    parsed = parse_llm_response(raw_response)
    elapsed = (time.time() - start_time) * 1000
    parse_ok = "parse_error" not in parsed
    logger.info(
        "LLM response: ip=%s route=%s score=%.4f elapsed_ms=%.0f parse_ok=%s",
        client_ip, route, score, elapsed, parse_ok
    )

    # Build response
    response = AnalyzeResponse(
        success=True,
        confidence_route=route,
        confidence_score=score,
        quota_remaining=ip_remaining - 1,
        quick_matches=matches[:5],
        analysis=parsed,
        raw=raw_response
    )

    # Record in history
    record_incident(
        incident_text=req.incident,
        route=route, confidence_score=score,
        matches=matches[:5], analysis=parsed, raw_llm=raw_response,
        client_ip=client_ip, metrics=req.metrics, logs=req.logs or "",
        provider=req.provider, model=req.model, elapsed_ms=elapsed,
    )

    # Cache successful responses
    if parse_ok:
        set_cached(req.incident, req.provider, req.model, response.model_dump())

    return response


# ── History & Feedback ─────────────────────────────────

@app.get("/history")
def list_history(page: int = 1, page_size: int = 20, with_feedback_only: bool = False):
    """List recent incident records."""
    incidents = get_recent_incidents(
        limit=min(page_size, 100),
        with_feedback_only=with_feedback_only,
    )
    return {"incidents": incidents, "count": len(incidents)}


@app.get("/history/{incident_id}")
def get_history_entry(incident_id: str):
    """Get full details of a historical incident."""
    entry = get_incident(incident_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
    return entry


@app.get("/history/similar")
def find_similar_history(incident: str, limit: int = 5):
    """Find historically similar incidents by keyword overlap."""
    if not incident or len(incident.strip()) < 3:
        return {"similar": []}
    results = search_similar(incident, limit=min(limit, 20))
    return {"similar": results}


@app.post("/feedback")
def post_feedback(
    incident_id: str,
    confirmed_kb_id: str = "",
    fix_applied: str = "",
    fix_effective: bool | None = None,
    actual_mttr_min: int = 0,
    notes: str = "",
):
    """Submit human feedback for a previous analysis."""
    ok = submit_feedback(
        incident_id=incident_id,
        confirmed_kb_id=confirmed_kb_id,
        fix_applied=fix_applied,
        fix_effective=fix_effective,
        actual_mttr_min=actual_mttr_min,
        notes=notes,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
    return {"status": "feedback_recorded", "incident_id": incident_id}


@app.get("/stats")
def statistics(days: int = 30):
    """Get usage and effectiveness statistics."""
    return get_stats(days=days)


# ── Report Export ───────────────────────────────────────

@app.get("/report/{incident_id}")
def get_report(incident_id: str):
    """Generate a Markdown report for a historical incident."""
    entry = get_incident(incident_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
    matches = entry.get("all_matches_json", [])
    if isinstance(matches, str):
        matches = json.loads(matches)
    analysis = entry.get("analysis_json", {})
    if isinstance(analysis, str):
        analysis = json.loads(analysis)
    metrics = entry.get("metrics_json", {})
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    md = generate_markdown_report(
        incident_text=entry.get("incident_text", ""),
        analysis=analysis,
        route=entry.get("route", "unknown"),
        score=entry.get("confidence_score", 0),
        matches=matches,
        incident_id=incident_id,
        metrics=metrics,
        logs=entry.get("logs_text", ""),
    )
    return {"markdown": md, "incident_id": incident_id}


@app.get("/report/summary")
def get_summary_report(days: int = 7):
    """Generate a summary report for the last N days."""
    incidents = get_recent_incidents(limit=200)
    # Filter by date
    cutoff = datetime.now(timezone.utc).isoformat()
    recent = []
    for inc in incidents:
        ts = inc.get("created_at", "")
        if ts >= cutoff[:10]:  # Simple date comparison
            recent.append(inc)
    if len(recent) < len(incidents):
        recent = incidents  # Fallback
    md = generate_summary_report(recent[:100], days=days)
    return {"markdown": md, "incident_count": len(recent)}


# ── Cache Management ────────────────────────────────────

@app.get("/cache/stats")
def get_cache_stats():
    """Get LLM response cache statistics."""
    return cache_stats()


@app.post("/cache/clear")
def clear_cache_endpoint():
    """Clear the LLM response cache."""
    clear_cache()
    return {"status": "cache_cleared"}


# ── Webhooks: Prometheus Alertmanager ────────────────────

@app.post("/webhook/prometheus")
async def webhook_prometheus(request: Request):
    """Prometheus Alertmanager webhook receiver.

    Expects Alertmanager v4 webhook JSON format.
    Auto-analyzes firing alerts and returns analysis.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    alerts = body.get("alerts", [])
    if not alerts:
        return {"status": "ok", "alerts_processed": 0}

    results = []
    for alert in alerts[:5]:  # Limit to 5 alerts per webhook call
        annotations = alert.get("annotations", {})
        labels = alert.get("labels", {})

        # Build incident text from alert
        alertname = labels.get("alertname", "Unknown Alert")
        summary = annotations.get("summary", "")
        description = annotations.get("description", "")
        incident = f"[Prometheus Alert] {alertname}: {summary} {description}"

        client_ip = request.client.host if request.client else "unknown"

        # Quick-match against KB
        matches = quick_match_weighted(incident, min_score=0.0)
        top = matches[0] if matches else None
        score = top["score"] if top else 0.0
        route = route_by_confidence(score) if top else "llm_only"

        if route == "direct" and top:
            kb_entry = get_entry(top["kb_id"])
            results.append({
                "alert": alertname,
                "route": "direct",
                "kb_id": top["kb_id"],
                "suggestion": kb_entry.get("common_causes", [])[:3] if kb_entry else [],
                "check_commands": kb_entry.get("check_commands", [])[:3] if kb_entry else [],
            })
        else:
            results.append({
                "alert": alertname,
                "route": route,
                "kb_id": top["kb_id"] if top else "",
                "score": score,
                "needs_llm": route != "direct",
            })

        # Optionally send to chat bot
        if route != "direct":
            content = f"**告警**: {alertname}\n**摘要**: {summary}\n**描述**: {description}\n**匹配**: {top['kb_id'] if top else '无'}"
            send_alert(f"Prometheus 告警: {alertname}", content)

    logger.info("Prometheus webhook: processed %d alerts", len(results))
    return {"status": "ok", "alerts_processed": len(results), "results": results}


# ── Webhooks: Feishu / DingTalk / WeCom Bot ──────────────

@app.post("/webhook/feishu")
async def webhook_feishu(request: Request):
    """Feishu (Lark) bot webhook receiver.

    Accepts messages from Feishu custom bot and auto-analyzes.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Extract text from Feishu message
    text = ""
    try:
        # Feishu custom bot format: {"text": "message"}
        text = body.get("text", "")
        if not text:
            # Feishu card callback format
            text = body.get("action", {}).get("value", "")
    except Exception:
        pass

    if not text:
        return {"msg": "No text content found in message"}

    # Quick analyze
    incident = text.strip()
    matches = quick_match_weighted(incident, min_score=0.0)
    top = matches[0] if matches else None

    if top and top["score"] >= DIRECT_THRESHOLD:
        kb_entry = get_entry(top["kb_id"])
        content = f"**路由**: 直接匹配\n**知识库**: {top['kb_id']} - {top['title']}\n\n"
        if kb_entry:
            content += f"**常见原因**:\n" + "\n".join(f"- {c}" for c in kb_entry.get("common_causes", [])[:5])
            content += f"\n\n**排查命令**:\n" + "\n".join(f"`{c}`" for c in kb_entry.get("check_commands", [])[:3])
        send_to_feishu_response = send_alert(f"故障分析: {incident[:50]}", content, channels=["feishu"])
    else:
        content = f"**匹配分**: {top['score']:.0% if top else 0:.0%}\n建议通过 Web 界面进行 LLM 深度分析。"
        send_alert(f"待分析: {incident[:50]}", content, channels=["feishu"])

    return {"status": "ok", "matched_kb": top["kb_id"] if top else "", "score": top["score"] if top else 0}


@app.post("/webhook/dingtalk")
async def webhook_dingtalk(request: Request):
    """DingTalk bot webhook receiver."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    text = body.get("text", {}).get("content", "")
    if not text:
        return {"msg": "No text content"}

    incident = text.strip()
    matches = quick_match_weighted(incident, min_score=0.0)
    top = matches[0] if matches else None

    if top and top["score"] >= DIRECT_THRESHOLD:
        kb_entry = get_entry(top["kb_id"])
        content = f"## 故障分析: {incident[:50]}\n\n**路由**: 直接匹配\n**KB**: {top['kb_id']} - {top['title']}\n\n"
        if kb_entry:
            content += f"### 常见原因\n" + "\n".join(f"- {c}" for c in kb_entry.get("common_causes", [])[:5])
        send_alert(f"故障分析: {incident[:50]}", content, channels=["dingtalk"])
    else:
        send_alert(f"待分析: {incident[:50]}", f"匹配分: {top['score']:.0% if top else 0:.0%}，建议深度分析。", channels=["dingtalk"])

    return {"status": "ok", "matched_kb": top["kb_id"] if top else ""}


# ── Multi-turn Conversation ──────────────────────────────

class ConversationTurn(BaseModel):
    incident: str = Field(..., min_length=1, max_length=MAX_INCIDENT_LENGTH)
    history: list[dict] = Field(default_factory=list, description="Previous conversation turns [{role, content}]")
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    api_key: Optional[str] = None

CONVERSATION_SYSTEM_PROMPT = """你是一个资深 SRE 工程师，正在与值班同事进行交互式排障对话。

每次对话会包含历史消息。请根据最新消息和历史上下文，给出下一步排查建议。

## 输出格式
{
  "response": "你的回复（自然语言）",
  "next_steps": ["具体排查命令或操作"],
  "confidence": "high/medium/low",
  "needs_escalation": true/false,
  "suspected_root_cause": "当前怀疑的根因"
}

## 规则
- 每次只建议 1-3 个下一步操作
- 如果信息不足，主动询问更多上下文
- 如果确认了根因，给出修复步骤
"""


@app.post("/conversation")
def conversation(req: ConversationTurn, request: Request = None):
    """Multi-turn interactive troubleshooting conversation."""
    client_ip = request.client.host if (request and request.client) else "unknown"

    # Enrich: add KB context for the initial turns
    kb_hint = ""
    if len(req.history) <= 1:
        matches = quick_match_weighted(req.incident, min_score=0.3)
        if matches:
            top = matches[0]
            kb_entry = get_entry(top["kb_id"])
            if kb_entry:
                causes = "\n".join(f"  - {c}" for c in kb_entry.get("common_causes", [])[:3])
                kb_hint = f"\n\n[知识库参考: {top['title']}]\n常见原因:\n{causes}"

    messages = [
        {"role": "system", "content": CONVERSATION_SYSTEM_PROMPT},
    ]
    for turn in req.history[-10:]:  # Keep last 10 turns
        messages.append({"role": turn.get("role", "user"), "content": turn.get("content", "")})
    messages.append({"role": "user", "content": req.incident + kb_hint})

    raw = call_llm(messages, model=req.model, provider=req.provider, api_key=req.api_key)
    parsed = parse_llm_response(raw)

    return {
        "success": True,
        "response": parsed.get("response", str(parsed)),
        "next_steps": parsed.get("next_steps", []),
        "confidence": parsed.get("confidence", "medium"),
        "needs_escalation": parsed.get("needs_escalation", False),
        "suspected_root_cause": parsed.get("suspected_root_cause", ""),
        "raw": raw,
    }


# ── Frontend ──────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(
        Path(__file__).parent.joinpath("index.html").read_text(encoding="utf-8")
    )


# ── Main ───────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8766))

    kb_count = len(load_all())

    logger.info("=" * 60)
    logger.info("AI Ops Assistant (Enterprise) starting on http://0.0.0.0:%d", port)
    logger.info("Knowledge Base: %d entries via YAML", kb_count)
    logger.info("Routing: direct(>=%.1f) | context(>=0.4) | llm_only(<0.4)", DIRECT_THRESHOLD)
    logger.info("Rate limit: %d req/min per IP", RATE_LIMIT_PER_MIN)
    logger.info("Per-IP LLM quota: %d/day | Global cap: %d/day", LLM_PER_IP_DAILY, LLM_GLOBAL_DAILY)
    logger.info("LLM providers: %s", ", ".join(sorted(LLM_PROVIDERS.keys())))
    logger.info("LLM timeout: %ds | Max retries: %d", LLM_TIMEOUT, LLM_MAX_RETRIES)
    if AIOPS_API_KEY:
        logger.info("API Key auth: ENABLED")
    else:
        logger.warning("API Key auth: DISABLED (set AIOPS_API_KEY to enable)")
    logger.info("=" * 60)

    # Also print to stdout for Docker logs
    print(f"🚀 AI Ops Assistant (Enterprise) starting on http://0.0.0.0:{port}")
    print(f"📚 Knowledge Base: {kb_count} entries via YAML")
    print(f"🔍 Routing: direct(>={DIRECT_THRESHOLD}) | context(>=0.4) | llm_only(<0.4)")
    if AIOPS_API_KEY:
        print(f"🔑 API Key auth: ENABLED")
    else:
        print("⚠️  API Key auth: DISABLED (set AIOPS_API_KEY to enable)")
    print(f"⏱️  Rate limit: {RATE_LIMIT_PER_MIN} req/min per IP")
    print(f"💰 Per-IP LLM quota: {LLM_PER_IP_DAILY}/day | Global cap: {LLM_GLOBAL_DAILY}/day")
    print(f"🤖 LLM providers: {', '.join(sorted(LLM_PROVIDERS.keys()))}")
    uvicorn.run(app, host="0.0.0.0", port=port)
