"""
AI Ops Troubleshooting Assistant — FastAPI Backend

Enterprise-grade version with YAML knowledge base, confidence-based routing,
and KB CRUD management.

Start with:
  cd /opt/data/.hermes/scripts/ops-assistant
  pip install fastapi uvicorn openai pyyaml httpx 2>/dev/null
  python api.py

Knowledge base files: kb/*.yaml (edit directly to add enterprise knowledge)
"""
import os
import sys
import json
import subprocess
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from engine import (
    SYSTEM_PROMPT, build_user_prompt, build_context, parse_llm_response
)
from kb_loader import (
    load_all, quick_match_weighted, route_by_confidence,
    get_entry, search_keyword
)

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse
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


# ── Pydantic Models ──────────────────────────────────

class AnalyzeRequest(BaseModel):
    incident: str
    metrics: Optional[dict] = None
    logs: Optional[str] = None
    model: str = "deepseek-chat"
    provider: str = "deepseek"
    api_key: Optional[str] = None
    force_llm: bool = False  # skip direct KB answer, always call LLM


class AnalyzeResponse(BaseModel):
    success: bool
    confidence_route: str = ""         # direct / context / llm_only
    confidence_score: float = 0.0
    kb_direct_answer: Optional[dict] = None  # direct route: KB entry
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
    """Call LLM API. Falls back to hermes config or env vars."""
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
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=2000,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        return json.dumps({"error": str(e), "raw_response": str(e)})


# ── Confidence-Based Analysis ────────────────────────

def build_kb_context(kb_entry: dict) -> str:
    """Format a KB entry as LLM context."""
    causes = "\n".join(f"  - {c}" for c in kb_entry.get("common_causes", []))
    checks = "\n".join(f"  $ {c}" for c in kb_entry.get("check_commands", []))
    fixes = "\n".join(f"  $ {f}" for f in kb_entry.get("fix_commands", []))
    return f"""
## 匹配到的知识库条目: {kb_entry.get('title', '')}
### 常见原因
{causes}
### 排查命令
{checks}
### 修复命令
{fixes}
"""


# ── API Routes ────────────────────────────────────────

@app.get("/health")
def health():
    kb = load_all()
    return {"status": "ok", "kb_count": len(kb), "kb_source": "yaml"}


@app.get("/kb")
def list_kb(category: str = None, search: str = None):
    """List KB entries. Optionally filter by category or search keyword."""
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
    """Get full KB entry by ID."""
    entry = get_entry(kb_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"KB entry {kb_id} not found")
    return entry


@app.post("/kb")
def add_kb_entry(req: KBEntryRequest):
    """Add a new KB entry by appending to the appropriate category YAML file.

    To ensure persistence, this writes to the YAML file on disk.
    Simple approach: append to resource.yaml by default.
    User can then move it to the right category file.
    """
    category_file = Path(__file__).parent / "kb" / f"{req.category}.yaml"
    if not category_file.exists():
        category_file = Path(__file__).parent / "kb" / "resource.yaml"

    import yaml
    with open(category_file) as f:
        entries = yaml.safe_load(f) or []

    # Generate new ID
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
        "id": new_id,
        "title": req.title,
        "title_en": req.title_en,
        "keywords": req.keywords,
        "category": req.category,
        "severity": req.severity,
        "common_causes": req.common_causes,
        "check_commands": req.check_commands,
        "fix_commands": req.fix_commands,
        "tags": req.tags,
        "source": req.source,
        "confidence_weight": req.confidence_weight,
    }

    entries.append(new_entry)
    with open(category_file, "w") as f:
        yaml.dump(entries, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # Reload KB cache
    import kb_loader
    kb_loader._KNOWLEDGE_BASE = None
    kb_loader._INVERTED_INDEX = None

    return {"status": "created", "id": new_id, "entry": new_entry}


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    """Analyze an incident with confidence-based knowledge base routing.

    Routing logic:
      - confidence >= 0.7 → DIRECT: return KB entry directly (no LLM cost)
      - confidence >= 0.4 → CONTEXT: KB context + LLM analysis
      - confidence < 0.4  → LLM_ONLY: pure LLM analysis
    """
    # 1. Weighted matching against YAML knowledge base
    matches = quick_match_weighted(req.incident, min_score=0.0)
    top_match = matches[0] if matches else None
    score = top_match["score"] if top_match else 0.0
    route = route_by_confidence(score) if top_match else "llm_only"

    # If caller forces LLM, override route
    if req.force_llm and route == "direct":
        route = "context"

    # 2. Route handling
    if route == "direct" and top_match and not req.force_llm:
        # DIRECT: Return KB entry directly, no LLM call
        kb_entry = get_entry(top_match["kb_id"])
        return AnalyzeResponse(
            success=True,
            confidence_route="direct",
            confidence_score=score,
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
                        "evidence": "匹配知识库条目: " + kb_entry.get("title", ""),
                        "verify_steps": kb_entry.get("check_commands", []),
                    }
                    for i, cause in enumerate(kb_entry.get("common_causes", []))
                ],
                "immediate_actions": [
                    f"参考 '{kb_entry.get('title', '')}' 排查步骤"
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

    # 3. Build LLM prompt (with or without KB context)
    user_content = build_user_prompt(req.incident)
    context = build_context(req.metrics, req.logs)
    if context:
        user_content += f"\n\n{context}"

    if route == "context" and top_match:
        kb_entry = get_entry(top_match["kb_id"])
        if kb_entry:
            kb_ctx = build_kb_context(kb_entry)
            user_content += f"\n\n{kb_ctx}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content}
    ]

    # 4. Call LLM
    raw_response = call_llm(
        messages,
        model=req.model,
        provider=req.provider,
        api_key=req.api_key
    )

    # 5. Parse response
    parsed = parse_llm_response(raw_response)

    return AnalyzeResponse(
        success=True,
        confidence_route=route,
        confidence_score=score,
        quick_matches=matches[:5],
        analysis=parsed,
        raw=raw_response
    )


# ── Frontend ──────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(Path(__file__).parent.joinpath("docs/index.html").read_text())


# ── Main ───────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8766))
    print(f"🚀 AI Ops Assistant (Enterprise) starting on http://0.0.0.0:{port}")
    print(f"📚 Knowledge Base: {len(load_all())} entries via YAML")
    print(f"🔍 Routing: direct(>=0.7) | context(>=0.4) | llm_only(<0.4)")
    uvicorn.run(app, host="0.0.0.0", port=port)
