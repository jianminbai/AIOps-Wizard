"""
cache.py — LLM Response Cache

Caches LLM analysis results to avoid redundant API calls for similar incidents.
Uses a simple JSON-file store with TTL-based expiry.

Strategy:
  - Normalize incident text (lowercase, strip whitespace, truncate)
  - Compute a hash of the normalized text
  - Cache hit: return cached analysis (24h TTL)
  - Cache miss: call LLM, store result

Config:
  CACHE_TTL_HOURS: Time-to-live in hours (default: 24)
  CACHE_MAX_ENTRIES: Max entries before LRU eviction (default: 1000)
"""

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("aiops-cache")

CACHE_FILE = Path(__file__).parent / ".llm_cache.json"
CACHE_TTL_HOURS = int(os.environ.get("CACHE_TTL_HOURS", "24"))
CACHE_MAX_ENTRIES = int(os.environ.get("CACHE_MAX_ENTRIES", "1000"))


def _read_cache() -> dict:
    """Read cache from disk. Returns {key: {result, created_at}}."""
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, FileNotFoundError, ValueError):
        pass
    return {}


def _write_cache(data: dict):
    """Write cache to disk. Evicts oldest entries if over max."""
    if len(data) > CACHE_MAX_ENTRIES:
        # Sort by created_at, keep newest
        sorted_items = sorted(
            data.items(),
            key=lambda kv: kv[1].get("created_at", 0),
            reverse=True,
        )
        data = dict(sorted_items[:CACHE_MAX_ENTRIES])

    CACHE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _normalize(text: str) -> str:
    """Normalize incident text for cache key generation."""
    # Lowercase, collapse whitespace
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    # Truncate to reasonable length
    return text[:500]


def _cache_key(incident: str, provider: str, model: str) -> str:
    """Generate a cache key from incident + provider + model."""
    normalized = _normalize(incident)
    raw = f"{normalized}|{provider}|{model}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get_cached(incident: str, provider: str, model: str) -> Optional[dict]:
    """Try to retrieve a cached analysis result.

    Returns the cached response dict, or None if not found / expired.
    """
    key = _cache_key(incident, provider, model)
    cache = _read_cache()

    entry = cache.get(key)
    if not entry:
        return None

    # Check TTL
    created = entry.get("created_at", 0)
    ttl_seconds = CACHE_TTL_HOURS * 3600
    if time.time() - created > ttl_seconds:
        # Expired — remove it
        cache.pop(key, None)
        _write_cache(cache)
        logger.debug("Cache expired: key=%s age_h=%.1f", key, (time.time() - created) / 3600)
        return None

    logger.info("Cache HIT: key=%s age_h=%.1f", key, (time.time() - created) / 3600)
    return entry.get("result")


def set_cached(incident: str, provider: str, model: str, result: dict):
    """Store an analysis result in cache."""
    key = _cache_key(incident, provider, model)
    cache = _read_cache()

    cache[key] = {
        "result": result,
        "created_at": time.time(),
        "incident_preview": _normalize(incident)[:100],
    }

    _write_cache(cache)
    logger.debug("Cache SET: key=%s", key)


def clear_cache():
    """Clear all cached entries."""
    CACHE_FILE.write_text("{}", encoding="utf-8")
    logger.info("Cache cleared")


def cache_stats() -> dict:
    """Return cache statistics."""
    cache = _read_cache()
    now = time.time()
    active = 0
    expired = 0
    for entry in cache.values():
        if now - entry.get("created_at", 0) < CACHE_TTL_HOURS * 3600:
            active += 1
        else:
            expired += 1
    return {
        "total_entries": len(cache),
        "active_entries": active,
        "expired_entries": expired,
        "ttl_hours": CACHE_TTL_HOURS,
        "max_entries": CACHE_MAX_ENTRIES,
    }
