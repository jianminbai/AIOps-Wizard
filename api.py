"""
AI Ops Troubleshooting Assistant — FastAPI Backend

Start with:
  cd /opt/data/.hermes/scripts/ops-assistant
  pip install fastapi uvicorn openai 2>/dev/null
  python api.py
"""

import os
import sys
import json
import subprocess
from pathlib import Path
from typing import Optional

# Add current dir to path for engine import
sys.path.insert(0, str(Path(__file__).parent))
from engine import (
    SYSTEM_PROMPT, KNOWLEDGE_BASE, build_user_prompt,
    build_context, quick_match, parse_llm_response
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
        "fastapi", "uvicorn", "openai", "pydantic", "httpx"
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
    model: str = "deepseek-v4-flash"  # default model
    provider: str = "deepseek"
    api_key: Optional[str] = None


class AnalyzeResponse(BaseModel):
    success: bool
    quick_matches: list = []
    analysis: dict = {}
    raw: str = ""


# ── LLM Call ──────────────────────────────────────────

def call_llm(messages: list, model: str, provider: str, api_key: str = None) -> str:
    """Call LLM API. Falls back to hermes config or env vars."""
    import openai
    
    # Determine API key and base URL
    if api_key:
        key = api_key
    else:
        # Try common env vars
        key = os.environ.get(
            f"{provider.upper()}_API_KEY",
            os.environ.get("OPENAI_API_KEY", "")
        )
    
    # Determine base URL
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
            temperature=0.1,  # low temperature for analysis
            max_tokens=2000,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        return json.dumps({"error": str(e), "raw_response": str(e)})


# ── API Routes ────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "kb_count": len(KNOWLEDGE_BASE)}


@app.get("/kb")
def list_kb():
    """List knowledge base entries."""
    return {"entries": [
        {"id": k["id"], "title": k["title"], "category": k["category"]}
        for k in KNOWLEDGE_BASE
    ]}


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    """Analyze an incident and return structured diagnosis."""
    
    # 1. Build prompt
    user_content = build_user_prompt(req.incident)
    context = build_context(req.metrics, req.logs)
    if context:
        user_content += f"\n\n{context}"
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content}
    ]
    
    # 2. Quick match against knowledge base
    matches = quick_match(req.incident)
    
    # 3. Call LLM
    raw_response = call_llm(
        messages, 
        model=req.model, 
        provider=req.provider,
        api_key=req.api_key
    )
    
    # 4. Parse response
    parsed = parse_llm_response(raw_response)
    
    return AnalyzeResponse(
        success=True,
        quick_matches=matches,
        analysis=parsed,
        raw=raw_response
    )


# ── Frontend (single HTML page) ────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(Path(__file__).parent.joinpath("index.html").read_text())


# ── Main ───────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8766))
    print(f"🚀 AI Ops Assistant starting on http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
