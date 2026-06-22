"""
AI Ops Troubleshooting Assistant — FastAPI Backend

Enterprise-grade version with:
  - YAML knowledge base + confidence-based routing
  - API Key authentication
  - Per-IP rate limiting
  - Daily LLM call quota (save token cost)

Env vars:
  AIOPS_API_KEY          If set, all requests must include X-API-Key header
  AIOPS_RATE_LIMIT       Max requests per minute per IP (default: 20)
  AIOPS_DAILY_LLM_LIMIT  Max LLM calls per day (default: 200)
  AIOPS_DIRECT_THRESHOLD Confidence threshold for direct KB answer (default: 0.7)
"""
import os
import sys
import json
import time
import uuid
import subprocess
from pathlib import Path
from typing import Optional
from datetime import date, datetime
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from engine import (
    SYSTEM_PROMPT, build_user_prompt, build_context, parse_llm_response
)
from kb_loader import (
    load_all, quick_match_weighted, route_by_confidence,
    get_entry, search_keyword
)

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
    # Skip auth for health check and frontend
    path = request.url.path
    if path in ("/health", "/") or path.startswith("/static"):
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

class AnalyzeRequest(BaseModel):
    incident: str
    metrics: Optional[dict] = None
    logs: Optional[str] = None
    model: str = "deepseek-chat"
    provider: str = "deepseek"
    api_key: Optional[str] = None
    force_llm: bool = False


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
    title: str
    title_en: Optional[str] = ""
    keywords: list[str]
    category: str
    severity: Optional[str] = "P2"
    common_causes: list[str]
    check_commands: list[str]
    fix_commands: list[str]
    tags: Optional[list[str]] = []
    source: Optional[str] = "manual"
    confidence_weight: Optional[float] = 1.0


# ── LLM Call ──────────────────────────────────────────

def call_llm(messages: list, model: str, provider: str, api_key: str = None) -> str:
    """Call LLM API. Falls back to env vars."""
    import openai
    if api_key:
        key = api_key
    else:
        key = os.environ.get(
            f"{provider.upper()}_API_KEY",
            os.environ.get("OPENAI_API_KEY", "")
        )
    bases = {
        "deepseek": "https://api.deepseek.com/v1",
        "openai": "https://api.openai.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
    }
    base_url = bases.get(provider, bases["deepseek"])
    client = openai.OpenAI(api_key=key, base_url=base_url)
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages,
            temperature=0.1, max_tokens=2000,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        return json.dumps({"error": str(e), "raw_response": str(e)})


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
def list_kb(category: str = None, search: str = None):
    if search:
        results = search_keyword(search)
    else:
        results = load_all()
    if category:
        results = [e for e in results if e.get("category") == category]
    return {"entries": [
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
        for k in results
    ]}


@app.get("/kb/{kb_id}")
def get_kb_entry(kb_id: str):
    entry = get_entry(kb_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"KB entry {kb_id} not found")
    return entry


@app.post("/kb")
def add_kb_entry(req: KBEntryRequest):
    category_file = Path(__file__).parent / "kb" / f"{req.category}.yaml"
    if not category_file.exists():
        category_file = Path(__file__).parent / "kb" / "resource.yaml"
    import yaml
    with open(category_file) as f:
        entries = yaml.safe_load(f) or []
    max_num = 0
    for e in entries:
        eid = e.get("id", "KB000")
        try:
            num = int(eid.replace("KB", ""))
            max_num = max(max_num, num)
        except ValueError:
            pass
    new_id = f"KB{max_num + 1:03d}"
    new_entry = {
        "id": new_id, "title": req.title, "title_en": req.title_en,
        "keywords": req.keywords, "category": req.category,
        "severity": req.severity, "common_causes": req.common_causes,
        "check_commands": req.check_commands, "fix_commands": req.fix_commands,
        "tags": req.tags, "source": req.source,
        "confidence_weight": req.confidence_weight,
    }
    entries.append(new_entry)
    with open(category_file, "w") as f:
        yaml.dump(entries, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    import kb_loader
    kb_loader._KNOWLEDGE_BASE = None
    kb_loader._INVERTED_INDEX = None
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

    # 1. Weighted matching
    matches = quick_match_weighted(req.incident, min_score=0.0)
    top_match = matches[0] if matches else None
    score = top_match["score"] if top_match else 0.0
    route = route_by_confidence(score) if top_match else "llm_only"
    # Use configured threshold for direct
    if route == "direct" and score < DIRECT_THRESHOLD:
        route = "context"

    if req.force_llm and route == "direct":
        route = "context"

    quota = _read_quota()
    ip_data = quota.get("ips", {}).get(client_ip, {"llm": 0, "direct": 0})

    # 2. DIRECT route (no LLM call)
    if route == "direct" and top_match and not req.force_llm:
        kb_entry = get_entry(top_match["kb_id"])
        _record_direct_call(client_ip)
        return AnalyzeResponse(
            success=True,
            confidence_route="direct",
            confidence_score=score,
            quota_remaining=LLM_PER_IP_DAILY - ip_data.get("llm", 0),
            kb_direct_answer=kb_entry,
            quick_matches=matches[:5],
            analysis={
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
            },
            raw=json.dumps(kb_entry, ensure_ascii=False, indent=2),
        )

    # Check LLM quota before proceeding (per-IP + global)
    llm_allowed, ip_remaining, global_remaining = _check_llm_quota(client_ip)
    if not llm_allowed:
        reason = "你的今日 LLM 额度已用完" if ip_remaining == 0 else "全局 LLM 额度已用完"
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
    return AnalyzeResponse(
        success=True,
        confidence_route=route,
        confidence_score=score,
        quota_remaining=ip_remaining - 1,
        quick_matches=matches[:5],
        analysis=parsed,
        raw=raw_response
    )


# ── Frontend ──────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(Path(__file__).parent.joinpath("index.html").read_text())


# ── Main ───────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8766))
    print(f"🚀 AI Ops Assistant (Enterprise) starting on http://0.0.0.0:{port}")
    print(f"📚 Knowledge Base: {len(load_all())} entries via YAML")
    print(f"🔍 Routing: direct(>={DIRECT_THRESHOLD}) | context(>=0.4) | llm_only(<0.4)")
    if AIOPS_API_KEY:
        print(f"🔑 API Key auth: ENABLED ({AIOPS_API_KEY[:4]}...{AIOPS_API_KEY[-4:]})")
    else:
        print("⚠️  API Key auth: DISABLED (set AIOPS_API_KEY to enable)")
    print(f"⏱️  Rate limit: {RATE_LIMIT_PER_MIN} req/min per IP")
    print(f"💰 Per-IP LLM quota: {LLM_PER_IP_DAILY}/day | Global cap: {LLM_GLOBAL_DAILY}/day")
    uvicorn.run(app, host="0.0.0.0", port=port)
