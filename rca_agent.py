"""
RCA Agent — AIOps-Wizard
=========================
Takes an alert + Context Package → LLM analysis → structured RCA report.

Pipeline:
  Alert + Context Package
       │
       ▼
  RCA Agent (LLM Analysis)
       │
       ├── Timeline Reconstruction
       ├── Root Cause Identification
       ├── Impact Assessment
       ├── Fix Recommendations
       └── Historical Case Matching (future)
       │
       ▼
  Structured RCA Report
"""

import json
import os
from typing import Any

from context_collector import ContextPackage, summarize_package


# ── System Prompt ────────────────────────────────────────────────────

RCA_SYSTEM_PROMPT = """You are a Senior SRE / Production Engineer with 15+ years of experience at a large internet company. You are doing a post-incident Root Cause Analysis (RCA).

Your task: Given an alert and the collected context (K8s, logs, metrics, traces), produce a professional RCA report.

Output format (strict JSON, no markdown wrapping):

{
  "title": "故障标题",
  "severity": "P0" | "P1" | "P2" | "P3",
  "status": "resolved" | "mitigated" | "ongoing" | "investigating",
  "summary": "一句话总结故障现象和根因",
  "timeline": [
    {
      "time": "14:01",
      "event": "事件描述",
      "source": "Prometheus/K8s/Logs/Otel/Alert"
    }
  ],
  "impact": {
    "services": ["受影响服务列表"],
    "duration_minutes": 30,
    "error_rate_increase": "e.g. +35%",
    "latency_increase": "e.g. P99 from 200ms to 3s",
    "users_affected": "e.g. ~5000 users"
  },
  "root_cause": {
    "summary": "根因一句话总结",
    "detail": "详细的根因分析，包括问题链",
    "confidence": "high" | "medium" | "low",
    "evidence": ["证据1", "证据2"]
  },
  "contributing_factors": [
    {"factor": "因素描述", "type": "config" | "code" | "infra" | "process" | "external"}
  ],
  "fix_actions": [
    {
      "priority": 1,
      "action": "修复动作描述",
      "command": "具体命令（如果有）",
      "owner": "团队名",
      "verified": false
    }
  ],
  "prevention": [
    "长期改进项1",
    "长期改进项2"
  ],
  "lesson_learned": "给团队的经验教训总结",
  "key_metrics": {
    "mttr_minutes": 15,
    "mttr_minutes": 45,
    "error_budget_consumed_pct": 12
  }
}

Rules:
1. Every claim MUST be backed by evidence from the context provided.
2. If you don't have enough data, state "insufficient data" — do NOT fabricate numbers.
3. Timeline events must be in chronological order.
4. Root cause must be specific — not "server failure" but "Prometheus collector certificate expired causing HPA to not receive metrics".
5. Fix commands must be copy-paste ready and production-safe.
"""


def build_rca_prompt(alert: str, context_summary: str, service: str = "") -> str:
    """Build the prompt for the RCA LLM call."""
    return f"""Please perform a Root Cause Analysis for the following production incident.

## Alert
{alert}

## Service
{service or "unknown"}

## Context Package
{context_summary}

## Instructions
1. Analyze the alert in the context of the provided data.
2. Reconstruct the timeline of events.
3. Identify the root cause and contributing factors.
4. Provide specific fix actions with executable commands.
5. Format your response as a valid JSON object matching the schema above.

Focus on making the report actionable for an on-call engineer."""
