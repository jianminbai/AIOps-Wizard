"""
Tests for engine.py — Core analysis engine.

Run: python -m pytest tests/ -v
"""
import json
import sys
from pathlib import Path

import pytest

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine import (
    SYSTEM_PROMPT,
    KNOWLEDGE_BASE,
    build_user_prompt,
    build_context,
    quick_match,
    parse_llm_response,
)


# ── System Prompt tests ───────────────────────────────────────────────

class TestSystemPrompt:
    def test_system_prompt_is_string(self):
        assert isinstance(SYSTEM_PROMPT, str)

    def test_contains_analysis_order(self):
        assert "分析顺序" in SYSTEM_PROMPT
        assert "资源问题" in SYSTEM_PROMPT
        assert "依赖问题" in SYSTEM_PROMPT
        assert "应用代码问题" in SYSTEM_PROMPT

    def test_requires_json_output(self):
        assert "JSON 格式" in SYSTEM_PROMPT
        assert "禁止输出任何其他文字" in SYSTEM_PROMPT

    def test_contains_forbidden_behaviors(self):
        assert "禁止" in SYSTEM_PROMPT
        assert "禁止空泛建议" in SYSTEM_PROMPT
        assert "禁止输出 markdown" in SYSTEM_PROMPT


# ── Knowledge Base tests ──────────────────────────────────────────────

class TestKnowledgeBase:
    def test_kb_is_list(self):
        assert isinstance(KNOWLEDGE_BASE, list)

    def test_kb_has_entries(self):
        assert len(KNOWLEDGE_BASE) >= 30

    def test_kb_entries_have_required_fields(self):
        required_fields = ["id", "title", "keywords", "category",
                          "common_causes", "check_commands"]
        for entry in KNOWLEDGE_BASE:
            for field in required_fields:
                assert field in entry, f"Entry {entry.get('id', '?')} missing '{field}'"

    def test_kb_ids_are_unique(self):
        ids = [e["id"] for e in KNOWLEDGE_BASE]
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {ids}"

    def test_kb_categories_valid(self):
        valid_categories = {"resource", "dependency", "application",
                           "external", "change"}
        for entry in KNOWLEDGE_BASE:
            assert entry["category"] in valid_categories, \
                f"Entry {entry['id']} has invalid category: {entry['category']}"

    def test_kb_keywords_are_nonempty(self):
        for entry in KNOWLEDGE_BASE:
            assert len(entry["keywords"]) > 0, \
                f"Entry {entry['id']} has empty keywords"

    def test_kb_ids_format(self):
        import re
        for entry in KNOWLEDGE_BASE:
            assert re.match(r'^KB\d{3}$', entry["id"]), \
                f"Entry {entry['id']} has invalid ID format"


# ── build_user_prompt tests ───────────────────────────────────────────

class TestBuildUserPrompt:
    def test_includes_incident_text(self):
        prompt = build_user_prompt("CPU 飙高到 100%")
        assert "CPU 飙高到 100%" in prompt
        assert "分析以下生产环境故障" in prompt
        assert "结构化 JSON 分析报告" in prompt

    def test_handles_multiline(self):
        prompt = build_user_prompt("Line 1\nLine 2\nLine 3")
        assert "Line 1" in prompt
        assert "Line 2" in prompt

    def test_handles_empty(self):
        prompt = build_user_prompt("")
        assert len(prompt) > 0
        assert "分析以下生产环境故障" in prompt


# ── build_context tests ───────────────────────────────────────────────

class TestBuildContext:
    def test_metrics_only(self):
        context = build_context(metrics={"cpu": 90, "memory": 70})
        assert "指标数据" in context
        assert "cpu" in context
        assert "90" in context

    def test_logs_only(self):
        context = build_context(logs="ERROR: connection timeout")
        assert "日志片段" in context
        assert "connection timeout" in context

    def test_both_metrics_and_logs(self):
        context = build_context(
            metrics={"cpu": 90},
            logs="ERROR: timeout"
        )
        assert "指标数据" in context
        assert "日志片段" in context

    def test_empty_input(self):
        context = build_context()
        assert context == ""

    def test_none_input(self):
        context = build_context(metrics=None, logs=None)
        assert context == ""


# ── quick_match tests ─────────────────────────────────────────────────

class TestQuickMatch:
    def test_exact_keyword_match(self):
        matches = quick_match("CPU 飙高到 100%，线程阻塞")
        assert len(matches) > 0
        assert matches[0]["kb_id"] == "KB001"

    def test_case_insensitive(self):
        matches = quick_match("OOM ERROR")
        assert len(matches) > 0

    def test_no_match(self):
        matches = quick_match("今天天气很好")
        assert len(matches) == 0

    def test_returns_at_most_five(self):
        matches = quick_match("cpu memory disk network redis database oom")
        assert len(matches) <= 5

    def test_sorted_by_score(self):
        matches = quick_match("oom memory cpu")
        for i in range(len(matches) - 1):
            assert matches[i]["match_score"] >= matches[i + 1]["match_score"]

    def test_returns_required_fields(self):
        matches = quick_match("CPU 飙高")
        assert len(matches) > 0
        m = matches[0]
        assert "kb_id" in m
        assert "title" in m
        assert "category" in m
        assert "match_score" in m
        assert "check_commands" in m


# ── parse_llm_response tests ──────────────────────────────────────────

class TestParseLlmResponse:
    def test_parse_valid_json(self):
        valid_json = '{"classification": "resource", "severity_assessment": "true_fault"}'
        result = parse_llm_response(valid_json)
        assert result["classification"] == "resource"
        assert result["severity_assessment"] == "true_fault"

    def test_parse_json_with_markdown_fence(self):
        response = '''```json
{
  "classification": "application",
  "severity_assessment": "true_fault",
  "root_cause_hypotheses": [
    {
      "rank": 1,
      "cause": "死循环导致CPU飙高",
      "probability": "high",
      "evidence": "线程dump显示大量RUNNABLE线程",
      "verify_steps": ["jstack <pid> | grep RUNNABLE"]
    }
  ],
  "immediate_actions": ["重启服务"],
  "mitigation_suggestions": ["限流"],
  "long_term_fix": ["修复死循环代码"],
  "commands": {
    "check": ["top -H -p <pid>"],
    "fix": ["kill -9 <pid> && systemctl restart service"]
  }
}
```'''
        result = parse_llm_response(response)
        assert result["classification"] == "application"
        assert len(result["root_cause_hypotheses"]) == 1

    def test_parse_json_without_fence(self):
        raw = '{"classification": "external", "severity_assessment": "needs_investigation"}'
        result = parse_llm_response(raw)
        assert result["classification"] == "external"

    def test_parse_invalid_json_returns_error(self):
        result = parse_llm_response("This is not JSON at all")
        assert "raw_response" in result
        assert "parse_error" in result

    def test_parse_empty_response(self):
        result = parse_llm_response("")
        assert "parse_error" in result or "raw_response" in result

    def test_parse_json_with_whitespace(self):
        result = parse_llm_response('  \n  {"classification": "change"}  \n')
        assert result["classification"] == "change"

    def test_parse_full_analysis_structure(self):
        """Test that a complete analysis response can be parsed."""
        full_response = {
            "classification": "resource",
            "severity_assessment": "true_fault",
            "root_cause_hypotheses": [
                {
                    "rank": 1,
                    "cause": "内存泄漏导致OOM",
                    "probability": "high",
                    "evidence": "堆内存持续增长未释放",
                    "verify_steps": ["jmap -histo:live <pid>"]
                }
            ],
            "immediate_actions": ["重启应用"],
            "mitigation_suggestions": ["增加内存限制"],
            "long_term_fix": ["修复内存泄漏代码"],
            "commands": {
                "check": ["jstat -gcutil <pid>"],
                "fix": ["systemctl restart app"]
            }
        }
        result = parse_llm_response(json.dumps(full_response, ensure_ascii=False))
        assert result["classification"] == "resource"
        assert len(result["root_cause_hypotheses"]) == 1
        assert result["root_cause_hypotheses"][0]["rank"] == 1
        assert "check" in result["commands"]
        assert "fix" in result["commands"]
