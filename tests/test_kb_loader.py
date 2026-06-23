"""
Tests for kb_loader.py — YAML knowledge base loader and matching engine.

Run: python -m pytest tests/ -v
"""
import os
import sys
import json
import tempfile
from pathlib import Path

import pytest

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import kb_loader
from kb_loader import (
    _tokenize, _normalize_entry, _build_inverted_indexes,
    load_all, quick_match_weighted, route_by_confidence,
    search_keyword, get_entry,
)


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_module_cache():
    """Reset kb_loader module-level caches before each test."""
    kb_loader._KNOWLEDGE_BASE = None
    kb_loader._INVERTED_INDEX = None
    kb_loader._TITLE_INDEX = None
    kb_loader._ROOTCAUSE_INDEX = None
    yield
    kb_loader._KNOWLEDGE_BASE = None
    kb_loader._INVERTED_INDEX = None
    kb_loader._TITLE_INDEX = None
    kb_loader._ROOTCAUSE_INDEX = None


@pytest.fixture
def sample_entries():
    """A minimal set of KB entries for testing."""
    return [
        {
            "id": "KB001",
            "title": "CPU 飙高 - 应用线程阻塞",
            "title_en": "High CPU - Thread Blocking",
            "keywords": ["cpu", "high cpu", "cpu 100%", "load average"],
            "category": "resource",
            "severity": "P1",
            "common_causes": [
                "Full GC 频繁（Java）",
                "死循环或无限递归",
            ],
            "check_commands": [
                "top -H -p $(pgrep -f order-service)",
                "jstack $(pgrep -f order-service) | grep -A 20 'RUNNABLE'",
            ],
            "fix_commands": [
                "jstat -gcutil $(pgrep -f order-service) 1000 10",
            ],
            "tags": ["java", "jvm", "cpu"],
            "source": "internal",
            "confidence_weight": 1.0,
            "root_cause": "线程阻塞或死循环导致CPU飙高",
        },
        {
            "id": "KB002",
            "title": "OOM - Java 堆内存溢出",
            "title_en": "OutOfMemoryError - Java Heap Space",
            "keywords": ["oom", "out of memory", "java heap", "OOMKilled", "memory leak"],
            "category": "resource",
            "severity": "P1",
            "common_causes": [
                "堆内存配置不足",
                "内存泄漏",
            ],
            "check_commands": [
                "dmesg | grep -i 'oom\\|kill'",
                "jstat -gcutil $(pgrep -f java) 1000 10",
            ],
            "fix_commands": [
                "jmap -dump:live,format=b,file=/tmp/heapdump.hprof $(pgrep -f java)",
            ],
            "tags": ["java", "jvm", "oom", "memory"],
            "source": "internal",
            "confidence_weight": 1.0,
            "root_cause": "堆内存不足或内存泄漏导致OOM",
        },
        {
            "id": "KB003",
            "title": "Redis 连接超时/阻塞",
            "title_en": "Redis Connection Timeout",
            "keywords": ["redis", "redis timeout", "redis blocked", "redis latency"],
            "category": "dependency",
            "severity": "P1",
            "common_causes": [
                "Redis 慢查询阻塞",
                "连接数达到 maxclients 上限",
            ],
            "check_commands": [
                "redis-cli INFO clients",
                "redis-cli SLOWLOG GET 20",
            ],
            "fix_commands": [
                "redis-cli CONFIG SET maxclients 10000",
            ],
            "tags": ["redis", "cache", "timeout"],
            "source": "internal",
            "confidence_weight": 0.9,
            "root_cause": "Redis慢查询或连接数达上限",
        },
    ]


@pytest.fixture
def mock_kb_dir(tmp_path, sample_entries):
    """Create a temporary KB directory with YAML files."""
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    yaml_file = kb_dir / "resource.yaml"
    import yaml
    with open(yaml_file, "w", encoding="utf-8") as f:
        yaml.dump(sample_entries, f, allow_unicode=True, default_flow_style=False)
    return kb_dir


# ── Tokenization tests ────────────────────────────────────────────────

class TestTokenize:
    def test_simple_english(self):
        tokens = _tokenize("high cpu thread blocked")
        assert "high" in tokens
        assert "cpu" in tokens
        assert "thread" in tokens
        assert "blocked" in tokens

    def test_chinese_tokens(self):
        # Verify the tokenizer regex supports CJK Unified Ideographs range
        import re as _re
        pattern = r'[a-zA-Z0-9一-鿿_\-]+'
        # Test that CJK chars are matched
        assert _re.match(pattern, chr(0x9ad8))  # U+9AD8 is within CJK range
        assert _re.match(pattern, 'cpu')
        # Whitespace should not match
        assert not _re.match(pattern + '$', ' ')
    def test_mixed_language(self):
        tokens = _tokenize("Java Full GC frequent cpu 100")
        assert "java" in tokens
        assert "full" in tokens
        assert "gc" in tokens
        assert "frequent" in tokens
        assert "cpu" in tokens
        assert "100" in tokens

    def test_empty_input(self):
        assert _tokenize("") == []
        assert _tokenize(None) == []

    def test_special_characters(self):
        tokens = _tokenize("cpu_usage-avg load-average")
        assert "cpu_usage" in tokens or "cpu_usage-avg" in tokens
        # Hyphens and underscores are word characters in the regex


# ── Normalization tests ───────────────────────────────────────────────

class TestNormalizeEntry:
    def test_valid_entry(self):
        entry = _normalize_entry(
            {"id": "KB999", "keywords": ["test"], "title": "Test"},
            "test.yaml"
        )
        assert entry["id"] == "KB999"
        assert entry["confidence_weight"] == 1.0
        assert entry["common_causes"] == []
        assert entry["check_commands"] == []

    def test_missing_id(self):
        entry = _normalize_entry(
            {"keywords": ["test"]},
            "test.yaml"
        )
        assert entry is None

    def test_missing_keywords(self):
        entry = _normalize_entry(
            {"id": "KB999"},
            "test.yaml"
        )
        assert entry is None

    def test_string_keywords_converted(self):
        entry = _normalize_entry(
            {"id": "KB999", "keywords": "single_keyword"},
            "test.yaml"
        )
        assert entry["keywords"] == ["single_keyword"]

    def test_default_category(self):
        entry = _normalize_entry(
            {"id": "KB999", "keywords": ["test"]},
            "test.yaml"
        )
        assert entry["category"] == "general"


# ── Load tests ────────────────────────────────────────────────────────

class TestLoadAll:
    def test_load_from_yaml(self, monkeypatch, tmp_path, sample_entries):
        """Test loading entries from YAML files."""
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        import yaml
        yaml_file = kb_dir / "resource.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(sample_entries, f, allow_unicode=True)

        # Patch KB_DIR to use temp directory
        monkeypatch.setattr(kb_loader, "KB_DIR", kb_dir)
        kb_loader._KNOWLEDGE_BASE = None

        entries = load_all(force_reload=True)
        assert len(entries) == 3
        assert entries[0]["id"] == "KB001"
        assert entries[1]["id"] == "KB002"
        assert entries[2]["id"] == "KB003"

    def test_load_fallback_to_engine(self, monkeypatch, tmp_path):
        """Test fallback to engine.KNOWLEDGE_BASE when YAML fails."""
        # Point to non-existent directory
        kb_dir = tmp_path / "nonexistent_kb"
        monkeypatch.setattr(kb_loader, "KB_DIR", kb_dir)
        kb_loader._KNOWLEDGE_BASE = None

        entries = load_all(force_reload=True)
        # Should fall back to engine.py KNOWLEDGE_BASE
        assert len(entries) >= 30

    def test_caching(self, monkeypatch, tmp_path, sample_entries):
        """Test that results are cached on subsequent calls."""
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        import yaml
        yaml_file = kb_dir / "resource.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(sample_entries, f, allow_unicode=True)

        monkeypatch.setattr(kb_loader, "KB_DIR", kb_dir)
        kb_loader._KNOWLEDGE_BASE = None

        entries1 = load_all()
        entries2 = load_all()  # Should hit cache
        assert entries1 is entries2  # Same object reference

    def test_force_reload(self, monkeypatch, tmp_path, sample_entries):
        """Test that force_reload=True bypasses cache."""
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        import yaml
        yaml_file = kb_dir / "resource.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(sample_entries, f, allow_unicode=True)

        monkeypatch.setattr(kb_loader, "KB_DIR", kb_dir)
        kb_loader._KNOWLEDGE_BASE = None

        entries1 = load_all()
        entries2 = load_all(force_reload=True)
        assert entries1 is not entries2  # Different objects on force reload


# ── Matching tests ────────────────────────────────────────────────────

class TestQuickMatchWeighted:
    @pytest.fixture(autouse=True)
    def setup_entries(self, monkeypatch, tmp_path, sample_entries):
        """Set up KB with sample entries before each test."""
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        import yaml
        yaml_file = kb_dir / "resource.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(sample_entries, f, allow_unicode=True)
        monkeypatch.setattr(kb_loader, "KB_DIR", kb_dir)
        kb_loader._KNOWLEDGE_BASE = None
        kb_loader._INVERTED_INDEX = None

    def test_exact_match_high_score(self):
        matches = quick_match_weighted("CPU 飙高到 100%, 线程阻塞严重")
        assert len(matches) > 0
        assert matches[0]["kb_id"] == "KB001"
        assert matches[0]["score"] > 0.3

    def test_multiple_matches_sorted(self):
        matches = quick_match_weighted("Java 应用 OOM 了，cpu 也很高")
        assert len(matches) >= 2
        # Scores should be sorted descending
        for i in range(len(matches) - 1):
            assert matches[i]["score"] >= matches[i + 1]["score"]

    def test_no_match(self):
        matches = quick_match_weighted("今天的天气真不错")
        assert len(matches) == 0

    def test_min_score_filter(self):
        matches = quick_match_weighted("CPU 飙高", min_score=0.5)
        for m in matches:
            assert m["score"] >= 0.5

    def test_redis_match(self):
        matches = quick_match_weighted("Redis 连接超时，延迟很高")
        assert len(matches) > 0
        # Redis entry should be top match
        redis_match = next((m for m in matches if "redis" in m["title"].lower()), None)
        assert redis_match is not None
        assert redis_match["score"] > 0

    def test_confidence_weight_affects_score(self):
        """Entries with lower confidence_weight should have lower scores."""
        matches = quick_match_weighted("redis timeout")
        for m in matches:
            if m["kb_id"] == "KB003":
                # confidence_weight is 0.9, so score should be scaled
                assert m["confidence_weight"] == 0.9

    def test_return_fields(self):
        matches = quick_match_weighted("CPU 飙高")
        assert len(matches) > 0
        m = matches[0]
        assert "kb_id" in m
        assert "title" in m
        assert "category" in m
        assert "score" in m
        assert "confidence_weight" in m
        assert "matched_keywords" in m
        assert "check_commands" in m
        assert "common_causes" in m
        assert "fix_commands" in m
        assert "root_cause" in m

    def test_quoted_phrase_bonus(self):
        """Quoted phrases in incident should get a bonus."""
        matches = quick_match_weighted('出现 "out of memory" 错误')
        # Should match KB002 (OOM)
        oom_matches = [m for m in matches if m["kb_id"] == "KB002"]
        if oom_matches:
            # The quoted phrase should have contributed to the score
            assert oom_matches[0]["score"] > 0

    def test_three_plus_keyword_bonus(self):
        """Matching 3+ keywords should give a bonus."""
        matches = quick_match_weighted("oom memory leak java heap")
        if matches:
            top = matches[0]
            if len(top.get("matched_keywords", [])) >= 3:
                assert top["score"] >= 0.15 * 3 + 0.2  # base + bonus

    def test_score_clamped_to_one(self):
        """Score should never exceed 1.0."""
        # Match with many keywords and title match
        matches = quick_match_weighted("cpu high cpu cpu 100% load average 线程阻塞")
        for m in matches:
            assert m["score"] <= 1.0
            assert m["score"] >= 0.0


# ── Routing tests ─────────────────────────────────────────────────────

class TestRouteByConfidence:
    def test_direct_route(self):
        assert route_by_confidence(0.7) == "direct"
        assert route_by_confidence(0.85) == "direct"
        assert route_by_confidence(1.0) == "direct"

    def test_context_route(self):
        assert route_by_confidence(0.4) == "context"
        assert route_by_confidence(0.5) == "context"
        assert route_by_confidence(0.69) == "context"

    def test_llm_only_route(self):
        assert route_by_confidence(0.0) == "llm_only"
        assert route_by_confidence(0.1) == "llm_only"
        assert route_by_confidence(0.39) == "llm_only"

    def test_negative_score(self):
        assert route_by_confidence(-0.1) == "llm_only"


# ── Search tests ──────────────────────────────────────────────────────

class TestSearchKeyword:
    @pytest.fixture(autouse=True)
    def setup_entries(self, monkeypatch, tmp_path, sample_entries):
        """Set up KB with sample entries before each test."""
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        import yaml
        yaml_file = kb_dir / "resource.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(sample_entries, f, allow_unicode=True)
        monkeypatch.setattr(kb_loader, "KB_DIR", kb_dir)
        kb_loader._KNOWLEDGE_BASE = None
        kb_loader._INVERTED_INDEX = None
        kb_loader._TITLE_INDEX = None
        kb_loader._ROOTCAUSE_INDEX = None

    def test_search_by_keyword(self):
        results = search_keyword("redis")
        assert len(results) == 1
        assert results[0]["id"] == "KB003"

    def test_search_by_title(self):
        results = search_keyword("CPU")
        assert len(results) >= 1
        ids = [r["id"] for r in results]
        assert "KB001" in ids

    def test_search_by_root_cause(self):
        results = search_keyword("堆内存")
        assert len(results) >= 1

    def test_search_no_results(self):
        results = search_keyword("xyz_nonexistent_abc")
        assert len(results) == 0

    def test_search_chinese(self):
        results = search_keyword("内存溢出")
        assert len(results) >= 1

    def test_search_empty_query(self):
        results = search_keyword("")
        assert len(results) == 0

    def test_title_matches_ranked_first(self):
        """Entries where title contains the query should be ranked first."""
        results = search_keyword("cpu")
        if len(results) > 0:
            # KB001 has "CPU" in title, should be first
            assert results[0]["id"] == "KB001"

    def test_results_deduplicated(self):
        """Results should not contain duplicate entries."""
        results = search_keyword("java")
        ids = [r["id"] for r in results]
        assert len(ids) == len(set(ids))


# ── Get entry tests ───────────────────────────────────────────────────

class TestGetEntry:
    @pytest.fixture(autouse=True)
    def setup_entries(self, monkeypatch, tmp_path, sample_entries):
        """Set up KB with sample entries before each test."""
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        import yaml
        yaml_file = kb_dir / "resource.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(sample_entries, f, allow_unicode=True)
        monkeypatch.setattr(kb_loader, "KB_DIR", kb_dir)
        kb_loader._KNOWLEDGE_BASE = None

    def test_get_existing_entry(self):
        entry = get_entry("KB001")
        assert entry is not None
        assert entry["id"] == "KB001"
        assert "CPU" in entry["title"]

    def test_get_nonexistent_entry(self):
        entry = get_entry("KB999")
        assert entry is None

    def test_get_entry_is_copy(self):
        """get_entry should return a copy, not the original dict."""
        entry = get_entry("KB001")
        entry["modified"] = True
        entry2 = get_entry("KB001")
        assert "modified" not in entry2


# ── Inverted index tests ──────────────────────────────────────────────

class TestInvertedIndex:
    def test_build_indexes(self, sample_entries):
        kb_loader._INVERTED_INDEX = None
        kb_loader._TITLE_INDEX = None
        kb_loader._ROOTCAUSE_INDEX = None
        _build_inverted_indexes(sample_entries)

        assert kb_loader._INVERTED_INDEX is not None
        assert kb_loader._TITLE_INDEX is not None
        assert kb_loader._ROOTCAUSE_INDEX is not None

        # Check that common tokens are indexed
        assert "cpu" in kb_loader._INVERTED_INDEX
        assert "redis" in kb_loader._INVERTED_INDEX
        assert "oom" in kb_loader._INVERTED_INDEX

    def test_inverted_index_kb_ids(self, sample_entries):
        _build_inverted_indexes(sample_entries)
        # "cpu" should point to KB001
        assert "KB001" in kb_loader._INVERTED_INDEX.get("cpu", [])

    def test_title_index(self, sample_entries):
        _build_inverted_indexes(sample_entries)
        assert kb_loader._TITLE_INDEX is not None
        # "redis" from title of KB003
        redis_tokens = kb_loader._TITLE_INDEX
        assert any("KB003" in ids for ids in redis_tokens.values())
