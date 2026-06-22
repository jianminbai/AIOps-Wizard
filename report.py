"""
report.py — Analysis Report Export

Generates Markdown reports from incident analysis results.
Supports exporting single incidents or batch summaries.

Usage:
  from report import generate_markdown_report, generate_summary_report
"""

import json
from datetime import datetime, timezone
from typing import Optional


def generate_markdown_report(
    incident_text: str,
    analysis: dict,
    route: str,
    score: float,
    matches: list,
    incident_id: str = "",
    metrics: Optional[dict] = None,
    logs: str = "",
) -> str:
    """Generate a comprehensive Markdown report for a single incident.

    Args:
        incident_text: Original incident description
        analysis: Parsed analysis result from LLM
        route: Confidence routing used (direct/context/llm_only)
        score: Confidence score
        matches: Top KB matches
        incident_id: Optional incident ID (from history)
        metrics: Optional metrics snapshot
        logs: Optional log snippets

    Returns:
        Markdown string suitable for saving or sharing
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []

    # Header
    lines.append(f"# AIOps 故障分析报告")
    lines.append(f"")
    lines.append(f"| 项目 | 详情 |")
    lines.append(f"|------|------|")
    lines.append(f"| **报告时间** | {now} |")
    if incident_id:
        lines.append(f"| **事件 ID** | {incident_id} |")
    lines.append(f"| **匹配路由** | `{route}` |")
    lines.append(f"| **置信度** | {score:.1%} |")
    lines.append(f"")

    # Incident text
    lines.append(f"## 故障描述")
    lines.append(f"")
    lines.append(f"```")
    lines.append(incident_text[:3000])
    lines.append(f"```")
    lines.append(f"")

    # Classification
    classification = analysis.get("classification", "N/A")
    severity = analysis.get("severity_assessment", "N/A")
    lines.append(f"## 分析结论")
    lines.append(f"")
    lines.append(f"- **故障分类**: {classification}")
    lines.append(f"- **严重度评估**: {severity}")
    lines.append(f"")

    # Root cause hypotheses
    hypotheses = analysis.get("root_cause_hypotheses", [])
    if hypotheses:
        lines.append(f"## 根因假设")
        lines.append(f"")
        lines.append(f"| 排名 | 概率 | 根因 | 证据 | 验证步骤 |")
        lines.append(f"|------|------|------|------|----------|")
        for h in hypotheses:
            rank = h.get("rank", "?")
            prob = h.get("probability", "?")
            cause = h.get("cause", "?")
            evidence = h.get("evidence", "")
            verify = "; ".join(h.get("verify_steps", [])[:2])
            lines.append(f"| {rank} | {prob} | {cause} | {evidence} | {verify} |")
        lines.append(f"")

    # Commands
    commands = analysis.get("commands", {})
    if commands.get("check"):
        lines.append(f"## 排查命令")
        lines.append(f"")
        for i, cmd in enumerate(commands["check"][:10], 1):
            lines.append(f"{i}. `{cmd}`")
        lines.append(f"")

    if commands.get("fix"):
        lines.append(f"## 修复命令")
        lines.append(f"")
        for i, cmd in enumerate(commands["fix"][:10], 1):
            lines.append(f"{i}. `{cmd}`")
        lines.append(f"")

    # Actions
    actions = analysis.get("immediate_actions", [])
    if actions:
        lines.append(f"## 立即操作")
        lines.append(f"")
        for a in actions:
            lines.append(f"- {a}")
        lines.append(f"")

    mitigations = analysis.get("mitigation_suggestions", [])
    if mitigations:
        lines.append(f"## 缓解建议")
        lines.append(f"")
        for m in mitigations:
            lines.append(f"- {m}")
        lines.append(f"")

    long_term = analysis.get("long_term_fix", [])
    if long_term:
        lines.append(f"## 长期改进")
        lines.append(f"")
        for lt in long_term:
            lines.append(f"- {lt}")
        lines.append(f"")

    # KB matches
    if matches:
        lines.append(f"## 匹配知识库条目")
        lines.append(f"")
        lines.append(f"| KB ID | 标题 | 分类 | 匹配分 |")
        lines.append(f"|-------|------|------|--------|")
        for m in matches[:5]:
            lines.append(f"| {m.get('kb_id', '?')} | {m.get('title', '?')} | {m.get('category', '?')} | {m.get('score', 0):.3f} |")
        lines.append(f"")

    # Context data
    if metrics:
        lines.append(f"## 指标快照")
        lines.append(f"")
        lines.append(f"```json")
        lines.append(json.dumps(metrics, ensure_ascii=False, indent=2)[:2000])
        lines.append(f"```")
        lines.append(f"")

    if logs:
        lines.append(f"## 日志片段")
        lines.append(f"")
        lines.append(f"```")
        lines.append(logs[:3000])
        lines.append(f"```")
        lines.append(f"")

    # Footer
    lines.append(f"---")
    lines.append(f"*报告由 AIOps-Wizard 自动生成*")

    return "\n".join(lines)


def generate_summary_report(incidents: list[dict], days: int = 7) -> str:
    """Generate a summary report for multiple incidents.

    Args:
        incidents: List of incident dicts from history.get_recent_incidents()
        days: Number of days covered

    Returns:
        Markdown summary report
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []

    lines.append(f"# AIOps 故障总结报告")
    lines.append(f"")
    lines.append(f"**时间范围**: 过去 {days} 天")
    lines.append(f"**生成时间**: {now}")
    lines.append(f"**事件总数**: {len(incidents)}")
    lines.append(f"")

    # Distribution
    routes = {}
    for inc in incidents:
        r = inc.get("route", "unknown")
        routes[r] = routes.get(r, 0) + 1

    lines.append(f"## 路由分布")
    lines.append(f"")
    for route, count in sorted(routes.items()):
        lines.append(f"- {route}: {count} 次 ({count/len(incidents)*100:.1f}%)")
    lines.append(f"")

    # Feedback summary
    with_feedback = [i for i in incidents if i.get("feedback_status") != "pending"]
    confirmed = [i for i in incidents if i.get("feedback_status") == "confirmed"]
    lines.append(f"## 反馈统计")
    lines.append(f"")
    lines.append(f"- 有反馈: {len(with_feedback)} / {len(incidents)} ({len(with_feedback)/max(len(incidents),1)*100:.1f}%)")
    lines.append(f"- 已确认根因: {len(confirmed)}")
    if confirmed:
        avg_mttr = sum(i.get("actual_mttr_min", 0) for i in confirmed) / max(len(confirmed), 1)
        lines.append(f"- 平均 MTTR: {avg_mttr:.0f} 分钟")
    lines.append(f"")

    # Incident list
    lines.append(f"## 事件列表")
    lines.append(f"")
    lines.append(f"| ID | 时间 | 描述 | 路由 | 反馈状态 | MTTR |")
    lines.append(f"|----|------|------|------|----------|------|")
    for inc in incidents[:50]:
        iid = inc.get("id", "?")[-12:]
        ts = inc.get("created_at", "?")[:16]
        desc = inc.get("incident_text", "?")[:60]
        route = inc.get("route", "?")
        fb = inc.get("feedback_status", "pending")
        mttr = f"{inc.get('actual_mttr_min', '')}min" if inc.get("actual_mttr_min") else "-"
        lines.append(f"| {iid} | {ts} | {desc} | {route} | {fb} | {mttr} |")

    lines.append(f"")
    lines.append(f"---")
    lines.append(f"*报告由 AIOps-Wizard 自动生成*")

    return "\n".join(lines)
