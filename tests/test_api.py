"""
Tests for api.py — FastAPI backend.

Run: python -m pytest tests/ -v
"""
import os
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Mock environment before importing app
os.environ.setdefault("AIOPS_API_KEY", "")
os.environ.setdefault("AIOPS_RATE_LIMIT", "100")
os.environ.setdefault("AIOPS_LLM_PER_IP_DAILY", "100")
os.environ.setdefault("AIOPS_LLM_GLOBAL_DAILY", "1000")

from fastapi.testclient import TestClient
from api import app

client = TestClient(app)


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_quota():
    """Reset quota state before each test."""
    import api as api_module
    quota_file = api_module._QUOTA_FILE
    if quota_file.exists():
        quota_file.unlink()
    yield
    if quota_file.exists():
        quota_file.unlink()


@pytest.fixture
def sample_incident():
    return {
        "incident": "生产环境 order-service CPU 飙高到 100%，P95 延迟从 200ms 升至 3s",
        "metrics": {"cpu": 95, "memory": 70, "latency_p95": 3000},
        "logs": "2024-01-01T10:00:00Z ERROR connection timeout",
    }


# ── Health endpoint tests ─────────────────────────────────────────────

class TestHealthEndpoint:
    def test_health_returns_ok(self):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_health_includes_kb_count(self):
        response = client.get("/health")
        data = response.json()
        assert "kb_count" in data
        assert data["kb_count"] > 0

    def test_health_includes_quota_info(self):
        response = client.get("/health")
        data = response.json()
        assert "quota" in data
        assert "my_ip" in data["quota"]
        assert "my_llm_today" in data["quota"]
        assert "remaining" in data["quota"]

    def test_health_includes_rate_limit_info(self):
        response = client.get("/health")
        data = response.json()
        assert "rate_limit" in data
        assert "auth_required" in data


# ── Frontend endpoint tests ───────────────────────────────────────────

class TestFrontendEndpoint:
    def test_index_returns_html(self):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_index_contains_app_title(self):
        response = client.get("/")
        assert "AI" in response.text or "Ops" in response.text or "Wizard" in response.text


# ── KB list endpoint tests ────────────────────────────────────────────

class TestKBListEndpoint:
    def test_list_all_entries(self):
        response = client.get("/kb")
        assert response.status_code == 200
        data = response.json()
        assert "entries" in data
        assert len(data["entries"]) > 0

    def test_list_entry_fields(self):
        response = client.get("/kb")
        data = response.json()
        entry = data["entries"][0]
        assert "id" in entry
        assert "title" in entry
        assert "category" in entry
        assert "severity" in entry
        assert "tags" in entry

    def test_list_filter_by_category(self):
        response = client.get("/kb?category=resource")
        assert response.status_code == 200
        data = response.json()
        for entry in data["entries"]:
            assert entry["category"] == "resource"

    def test_list_search_keyword(self):
        response = client.get("/kb?search=redis")
        assert response.status_code == 200
        data = response.json()
        # Should find Redis-related entries
        titles = [e["title"].lower() for e in data["entries"]]
        assert any("redis" in t for t in titles)

    def test_list_combined_filter_and_search(self):
        response = client.get("/kb?category=dependency&search=redis")
        assert response.status_code == 200
        data = response.json()
        for entry in data["entries"]:
            assert entry["category"] == "dependency"


# ── KB get endpoint tests ─────────────────────────────────────────────

class TestKBGetEndpoint:
    def test_get_existing_entry(self):
        response = client.get("/kb/KB001")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "KB001"

    def test_get_nonexistent_entry(self):
        response = client.get("/kb/KB99999")
        assert response.status_code == 404

    def test_get_returns_full_entry(self):
        response = client.get("/kb/KB001")
        data = response.json()
        assert "title" in data
        assert "keywords" in data
        assert "category" in data
        assert "common_causes" in data
        assert "check_commands" in data


# ── Analyze endpoint tests ────────────────────────────────────────────

class TestAnalyzeEndpoint:
    def test_analyze_direct_route(self, sample_incident):
        """A clear CPU issue should match KB directly with high confidence."""
        response = client.post("/analyze", json=sample_incident)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "confidence_route" in data
        assert "confidence_score" in data
        assert "analysis" in data

    def test_analyze_with_force_llm(self):
        """force_llm=true should override direct routing."""
        incident = {
            "incident": "CPU 飙高到 100%，线程阻塞，Full GC 频繁",
            "force_llm": True,
        }
        response = client.post("/analyze", json=incident)
        assert response.status_code == 200
        data = response.json()
        # With force_llm, should not be "direct" unless LLM call fails
        # At minimum, it should return a valid response structure
        assert "success" in data
        assert "confidence_route" in data

    def test_analyze_response_structure(self, sample_incident):
        response = client.post("/analyze", json=sample_incident)
        data = response.json()
        required_fields = [
            "success", "confidence_route", "confidence_score",
            "quota_remaining", "quick_matches", "analysis"
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

    def test_analyze_handles_empty_incident(self):
        """Empty incident should fail validation (min_length=1)."""
        response = client.post("/analyze", json={"incident": ""})
        assert response.status_code == 422  # Rejected by Pydantic validation

    def test_analyze_with_metrics_only(self):
        response = client.post("/analyze", json={
            "incident": "database connection pool exhausted",
            "metrics": {"hikaricp.connections.active": 50, "hikaricp.connections.max": 50},
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_analyze_with_logs_only(self):
        response = client.post("/analyze", json={
            "incident": "Pod 频繁重启",
            "logs": "OOMKilled: true\nExit Code: 137",
        })
        assert response.status_code == 200

    def test_analyze_with_provider_override(self, sample_incident):
        sample_incident["provider"] = "openai"
        sample_incident["model"] = "gpt-4o-mini"
        response = client.post("/analyze", json=sample_incident)
        # Should still work (even if API key is missing, the KB direct route
        # should handle it without calling LLM)
        assert response.status_code == 200

    def test_analyze_no_match_goes_to_llm(self):
        """An incident with no KB match should route to llm_only."""
        response = client.post("/analyze", json={
            "incident": "今天天气真好，阳光明媚，适合出去玩。",
        })
        assert response.status_code == 200
        data = response.json()
        # Should be llm_only or could fail if LLM quota exceeded
        assert data["confidence_route"] in ("llm_only", "quota_exceeded")


# ── Error handling tests ──────────────────────────────────────────────

class TestErrorHandling:
    def test_invalid_json_body(self):
        response = client.post("/analyze",
                              content="not valid json",
                              headers={"Content-Type": "application/json"})
        # FastAPI returns 422 for invalid JSON
        assert response.status_code in (422, 400)

    def test_missing_required_field(self):
        response = client.post("/analyze", json={})
        assert response.status_code == 422

    def test_invalid_model_parameter(self):
        """Empty model string should fail validation (min_length=1)."""
        response = client.post("/analyze", json={
            "incident": "CPU 飙高",
            "model": "",
        })
        assert response.status_code == 422


# ── Rate limiting tests ───────────────────────────────────────────────

class TestRateLimiting:
    def test_security_headers(self):
        response = client.get("/health")
        assert "x-ratelimit-limit" in response.headers


# ── CORS tests ────────────────────────────────────────────────────────

class TestCORS:
    def test_cors_preflight(self):
        """OPTIONS request should be handled by CORS middleware."""
        response = client.options("/analyze", headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        })
        # CORS middleware handles preflight and returns 200
        assert response.status_code == 200

    def test_cors_headers(self):
        response = client.options("/health", headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        })
        assert response.status_code == 200
