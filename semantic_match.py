"""
semantic_match.py — Enhanced Matching Engine

Upgrades over kb_loader's pure keyword matching:
  1. LLM-assisted entity extraction (service name, metric, symptom type)
  2. Synonym expansion for Chinese-English cross-language matching
  3. Optional embedding-based semantic similarity (sentence-transformers)
  4. Confidence scoring that blends keyword + semantic + entity signals

Usage:
  from semantic_match import enhance_query, semantic_match, MATCH_ENGINE_AVAILABLE
"""

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger("semantic-match")

# ── Synonym Dictionary (CN ↔ EN) ─────────────────────────────────────
# Expands queries so "内存爆了" also matches "oom" and "out of memory"

SYNONYM_MAP: dict[str, list[str]] = {
    # Resource
    "cpu": ["cpu", "processor", "核", "中央处理器"],
    "memory": ["memory", "mem", "内存", "ram"],
    "disk": ["disk", "磁盘", "硬盘", "storage", "存储", "volume"],
    "oom": ["oom", "out of memory", "内存溢出", "内存耗尽", "内存爆了", "OOMKilled"],
    "cpu high": ["cpu飙高", "cpu高", "cpu 100", "cpu打满", "cpu spike", "high cpu"],
    "thread": ["线程", "thread", "阻塞", "blocked", "thread pool"],
    "gc": ["gc", "garbage collection", "full gc", "垃圾回收", "frequent gc"],
    "fd": ["fd", "file descriptor", "文件描述符", "too many open files"],
    "tcp": ["tcp", "time_wait", "连接", "connection", "端口耗尽", "port exhaustion"],
    "disk full": ["磁盘满", "disk full", "no space", "空间不足", "inode"],

    # K8s
    "pod": ["pod", "容器", "container", "实例"],
    "crash": ["crash", "崩溃", "crashloop", "重启", "restart", "OOMKilled"],
    "pending": ["pending", "调度失败", "资源不足", "insufficient"],
    "node": ["node", "节点", "notready", "不可用", "kubelet"],
    "hpa": ["hpa", "autoscaler", "扩缩容", "弹性伸缩", "auto scale"],
    "imagepull": ["imagepull", "镜像拉取", "拉取失败", "ImagePullBackOff"],

    # Database
    "db": ["db", "database", "数据库", "mysql", "postgresql"],
    "connection pool": ["连接池", "connection pool", "hikari", "druid", "datasource", "pool exhausted"],
    "slow query": ["慢查询", "慢sql", "slow query", "slow sql", "查询慢"],
    "deadlock": ["死锁", "deadlock", "锁等待", "lock wait"],

    # Cache / Redis
    "redis": ["redis", "缓存", "cache", "内存数据库"],
    "redis timeout": ["redis超时", "redis timeout", "redis慢", "redis blocked"],
    "bigkey": ["bigkey", "大key", "big key", "hgetall"],

    # MQ
    "mq": ["mq", "消息队列", "kafka", "rabbitmq", "rocketmq", "message queue"],
    "consumer lag": ["积压", "lag", "backlog", "消费延迟", "消息堆积"],

    # Network
    "network": ["网络", "network", "延迟", "timeout", "丢包", "packet loss"],
    "dns": ["dns", "域名", "解析", "coredns", "nslookup"],
    "ssl": ["ssl", "tls", "证书", "certificate", "过期", "expired"],

    # ES
    "elasticsearch": ["elasticsearch", "es", "集群", "cluster", "red", "yellow"],

    # APM
    "trace": ["trace", "调用链", "skywalking", "jaeger", "zipkin", "span"],
    "apm": ["apm", "agent", "探针", "pinpoint"],

    # Change
    "deploy": ["部署", "deploy", "发布", "release", "上线", "rollout"],
    "config": ["配置", "config", "configmap", "环境变量", "env"],
    "rollback": ["回滚", "rollback", "undo", "回退"],
    "cicd": ["cicd", "ci/cd", "pipeline", "流水线", "jenkins", "gitlab"],

    # Gateway
    "gateway": ["gateway", "网关", "nginx", "ingress", "502", "504", "bad gateway"],
}


def expand_query(text: str) -> str:
    """Expand query text with synonyms to improve keyword matching coverage.

    Example:
        "订单服务内存爆了" → includes "oom out of memory" in the text so
        the keyword matcher finds KB002 (OOM).
    """
    text_lower = text.lower()
    expansions: list[str] = []

    for canonical, synonyms in SYNONYM_MAP.items():
        # If any synonym is in the text, add the canonical + all synonyms
        if any(s.lower() in text_lower for s in synonyms):
            # Only add expansions that aren't already present
            for s in synonyms:
                if s.lower() not in text_lower:
                    expansions.append(s)

    if not expansions:
        return text

    expanded = text + "\n[expanded: " + ", ".join(dict.fromkeys(expansions)) + "]"
    logger.debug("Query expanded: %d synonyms added", len(expansions))
    return expanded


# ── Entity Extraction (LLM-assisted) ─────────────────────────────────

ENTITY_EXTRACTION_PROMPT = """Extract key entities from this IT incident description. Return ONLY valid JSON.

{
  "services": ["service name or component"],
  "symptoms": ["symptom keywords in English"],
  "metrics_mentioned": ["metric names like cpu, memory, latency"],
  "category_hint": "resource/dependency/application/external/change/unknown",
  "severity_hint": "P0/P1/P2/P3/unknown"
}

Incident: {incident}
"""


def extract_entities_llm(incident_text: str, provider: str = "deepseek", api_key: str = None) -> dict:
    """Use LLM to extract structured entities from free-text incident.

    Falls back gracefully if LLM is unavailable — returns empty dict.
    """
    try:
        import openai
        if api_key:
            key = api_key
        else:
            key = os.environ.get(f"{provider.upper()}_API_KEY",
                                 os.environ.get("OPENAI_API_KEY", ""))
        if not key:
            logger.debug("No API key for entity extraction; skipping")
            return {}

        bases = {
            "deepseek": "https://api.deepseek.com/v1",
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "claude": "https://api.anthropic.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
        }
        base_url = bases.get(provider, bases["deepseek"])
        client = openai.OpenAI(api_key=key, base_url=base_url, timeout=15)

        prompt = ENTITY_EXTRACTION_PROMPT.format(incident=incident_text[:2000])
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=300,
        )
        content = resp.choices[0].message.content or "{}"

        # Parse JSON from response
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {}
    except Exception as e:
        logger.debug("Entity extraction failed (non-critical): %s", e)
        return {}


# ── Embedding (optional, lazy-loaded) ────────────────────────────────

_EMBEDDING_MODEL = None


def _get_embedding_model():
    """Lazy-load sentence-transformers model."""
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
            # Use a small, fast multilingual model
            model_name = os.environ.get("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
            _EMBEDDING_MODEL = SentenceTransformer(model_name)
            logger.info("Embedding model loaded: %s", model_name)
        except ImportError:
            logger.warning("sentence-transformers not installed; embedding disabled")
            return None
        except Exception as e:
            logger.warning("Failed to load embedding model: %s", e)
            return None
    return _EMBEDDING_MODEL


def compute_embedding(text: str) -> Optional[list[float]]:
    """Compute embedding vector for text. Returns None if unavailable."""
    model = _get_embedding_model()
    if model is None:
        return None
    try:
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()
    except Exception as e:
        logger.debug("Embedding failed: %s", e)
        return None


def semantic_match(
    incident_text: str,
    kb_entries: list[dict],
    top_k: int = 10,
) -> list[dict]:
    """Enhanced matching that blends keyword + optional embedding scores.

    Args:
        incident_text: Raw incident description
        kb_entries: List of KB entry dicts (must have 'keywords', 'title', 'root_cause')
        top_k: Number of results to return

    Returns:
        Sorted list of dicts with 'kb_id', 'title', 'score', 'match_signals'
    """
    # 1. Expand query with synonyms
    expanded_text = expand_query(incident_text)

    # 2. Compute embedding (optional, graceful fallback)
    embedding = compute_embedding(incident_text)

    # 3. Score each KB entry
    text_lower = expanded_text.lower()
    results = []

    for entry in kb_entries:
        signals = _compute_signals(entry, text_lower, embedding)
        if signals["keyword_matches"] == 0 and signals["embedding_score"] < 0.3:
            continue

        # Weighted blend: keyword (0.5) + title (0.2) + embedding (0.3)
        kw_score = signals["keyword_score"]
        title_score = signals["title_score"]
        emb_score = signals["embedding_score"] if embedding else 0.0
        cw = entry.get("confidence_weight", 1.0)

        blended = (kw_score * 0.5 + title_score * 0.2 + emb_score * 0.3) * cw
        blended = max(0.0, min(1.0, blended))

        results.append({
            "kb_id": entry["id"],
            "title": entry["title"],
            "category": entry.get("category", "general"),
            "score": round(blended, 4),
            "match_signals": signals,
            "check_commands": entry.get("check_commands", []),
            "common_causes": entry.get("common_causes", []),
            "fix_commands": entry.get("fix_commands", []),
            "root_cause": entry.get("root_cause", ""),
            "confidence_weight": cw,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


def _compute_signals(entry: dict, text_lower: str, embedding: Optional[list[float]]) -> dict:
    """Compute matching signals for a single KB entry."""
    keywords = entry.get("keywords", [])

    def _safe_lower(s) -> str:
        if not isinstance(s, str):
            return str(s).lower()
        return s.lower()

    title = _safe_lower(entry.get("title", ""))
    root_cause = _safe_lower(entry.get("root_cause", ""))

    # Keyword matching
    matched_kws = []
    for kw in keywords:
        if _safe_lower(kw) in text_lower:
            matched_kws.append(kw)

    num_matched = len(matched_kws)
    kw_score = min(1.0, num_matched * 0.15)
    if num_matched >= 3:
        kw_score = min(1.0, kw_score + 0.2)

    # Title matching bonus
    title_score = 0.0
    for kw in matched_kws:
        kw_lower = _safe_lower(kw)
        if kw_lower in title:
            title_score = 0.3
            break
    # Also check root_cause
    for kw in matched_kws:
        if _safe_lower(kw) in root_cause:
            title_score = max(title_score, 0.2)
            break

    # Embedding similarity (computed elsewhere or passed in)
    emb_score = 0.0
    if embedding and entry.get("_embedding"):
        try:
            emb = entry["_embedding"]
            dot = sum(a * b for a, b in zip(embedding, emb))
            norm_a = sum(a * a for a in embedding) ** 0.5
            norm_b = sum(b * b for b in emb) ** 0.5
            if norm_a > 0 and norm_b > 0:
                emb_score = dot / (norm_a * norm_b)
        except (TypeError, ValueError):
            pass

    return {
        "keyword_matches": num_matched,
        "matched_keywords": matched_kws,
        "keyword_score": round(kw_score, 4),
        "title_score": round(title_score, 4),
        "embedding_score": round(emb_score, 4),
    }


def should_use_semantic(kb_size: int) -> bool:
    """Heuristic: semantic matching is most valuable with larger KBs."""
    return kb_size >= 20


# Check availability
MATCH_ENGINE_AVAILABLE = True  # Base synonym expansion always works
EMBEDDING_AVAILABLE = False
try:
    import sentence_transformers
    EMBEDDING_AVAILABLE = True
except ImportError:
    pass
