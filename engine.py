"""
AI Ops Troubleshooting Assistant — Core Engine

Usage:
  from engine import analyze_incident
  result = analyze_incident("order-service P95 latency from 200ms to 3s")
"""

import json
import os
import re

# ── Input Schema ──────────────────────────────────────────

SCHEMA_EXAMPLE = {
    "title": "服务延迟升高",
    "service": "order-service",
    "severity": "P1",
    "symptom": "P95 latency from 200ms -> 3s",
    "time_range": "last 10 min",
    "metrics": "CPU 90%, memory 70%",
    "logs": "connection timeout errors increasing",
    "trace": "optional - SkyWalking trace ID xxx",
    "recent_changes": "new deployment 30 min ago"
}


# ── System Prompt (the engine's brain) ────────────────────

SYSTEM_PROMPT = """你是一个资深 SRE 工程师，负责生产环境故障分析。

## 你的分析规范

### 分析顺序（严格执行）
1. 是否是资源问题（CPU / 内存 / IO / 磁盘）
2. 是否是依赖问题（数据库 / 消息队列 / 缓存 / 下游服务）
3. 是否是应用代码问题（错误率上升 / Exception / OOM）
4. 是否是流量问题（突增 / 热点 / 限流）
5. 是否是变更引发（部署 / 配置 / 发布）

### 输出格式
必须使用 JSON 格式，禁止输出任何其他文字。

{
  "classification": "资源/应用/依赖/外部/变更",
  "severity_assessment": "true_fault/false_alarm/needs_investigation",
  "root_cause_hypotheses": [
    {
      "rank": 1,
      "cause": "具体根因描述",
      "probability": "high/medium/low",
      "evidence": "为什么是这个原因的证据",
      "verify_steps": ["具体排查命令1", "具体排查命令2"]
    }
  ],
  "immediate_actions": ["可执行的修复操作"],
  "mitigation_suggestions": ["短期缓解方案"],
  "long_term_fix": ["长期改进建议"],
  "commands": {
    "check": ["kubectl get pods -n...", "curl -s http://..."],
    "fix": ["kubectl scale deployment...", "systemctl restart..."]
  }
}

### 禁止行为
- 禁止空泛建议（如"检查日志"必须改成具体命令）
- 禁止输出 markdown 或自然语言段落
- 禁止输出 JSON 以外的内容
- 如果信息不足，在 root_cause_hypotheses 里写 Insufficient data，并列出还需要什么信息
"""


# ── Knowledge Base ────────────────────────────────────────

KNOWLEDGE_BASE = [
    {
        "id": "KB001",
        "title": "CPU 飙高 - 应用线程阻塞",
        "keywords": ["cpu", "high cpu", "cpu 100%", "load average"],
        "category": "resource",
        "common_causes": [
            "Full GC 频繁（Java）",
            "死循环或无限递归",
            "线程池打满",
            "热点流量导致 CPU 密集计算"
        ],
        "check_commands": [
            "top -H -p $(pgrep -f order-service)  # 查看线程级 CPU",
            "jstack $(pgrep -f order-service) | grep -A 20 'RUNNABLE' | head -100",
            "pidstat -t -p $(pgrep -f order-service) 1 5",
            "kubectl top pod -n production | grep high-cpu"
        ],
        "fix_commands": [
            "jstat -gcutil $(pgrep -f order-service) 1000 10  # 检查 GC",
            "curl -s http://localhost:8080/actuator/health | jq ."
        ]
    },
    {
        "id": "KB002",
        "title": "内存溢出 (OOM)",
        "keywords": ["oom", "out of memory", "memory leak", "内存泄漏", "OOMKilled"],
        "category": "resource",
        "common_causes": [
            "堆内存泄漏（Java）",
            "大对象分配过多",
            "缓存未限制大小",
            "连接池泄漏"
        ],
        "check_commands": [
            "kubectl describe pod -n production | grep -A 10 'Last State:'",
            "dmesg | grep -i oom | tail -5",
            "jstat -gcutil $(pgrep -f java) 1000 5",
            "jmap -histo:live $(pgrep -f java) | head -30"
        ],
        "fix_commands": [
            "kubectl set resources deployment order-service --memory=2Gi",
            "java -Xmx2g -Xms1g -jar app.jar"
        ]
    },
    {
        "id": "KB003",
        "title": "数据库连接池耗尽",
        "keywords": ["connection pool", "datasource", "hikari", "db connection", "数据库连接"],
        "category": "dependency",
        "common_causes": [
            "慢查询导致连接不释放",
            "连接池配置过小",
            "死锁导致连接泄漏",
            "数据库端 max_connections 不够"
        ],
        "check_commands": [
            "curl -s http://localhost:8080/actuator/metrics/hikaricp.connections.active",
            "show processlist;  # 在 MySQL 中查看活跃连接",
            "SELECT * FROM information_schema.INNODB_TRX\\G",
            "netstat -anp | grep 3306 | wc -l"
        ],
        "fix_commands": [
            "kubectl exec -it mysql-db -- mysqladmin processlist",
            "KILL <thread_id>;  # 杀死阻塞连接"
        ]
    },
    {
        "id": "KB004",
        "title": "Redis 延迟飙升",
        "keywords": ["redis", "cache", "延迟", "timeout", "缓存"],
        "category": "dependency",
        "common_causes": [
            "Big Key 操作（如 hgetall 一个大 hash）",
            "慢查询（如 keys *）",
            "内存达到 maxmemory 触发淘汰",
            "fork 生成 RDB 导致短暂阻塞"
        ],
        "check_commands": [
            "redis-cli -h redis-host INFO commandstats | head -20",
            "redis-cli -h redis-host SLOWLOG GET 10",
            "redis-cli -h redis-host INFO memory | grep -E 'used_memory_human|maxmemory'",
            "redis-cli -h redis-host --bigkeys"
        ],
        "fix_commands": [
            "redis-cli -h redis-host CONFIG SET timeout 30",
            "redis-cli -h redis-host MEMORY PURGE"
        ]
    },
    {
        "id": "KB005",
        "title": "Pod CrashLoopBackOff",
        "keywords": ["crashloop", "pod crash", "CrashLoopBackOff", "容器重启"],
        "category": "application",
        "common_causes": [
            "启动命令参数错误",
            "配置文件找不到或格式错误",
            "依赖服务未就绪",
            "健康检查失败",
            "OOMKilled"
        ],
        "check_commands": [
            "kubectl describe pod -n production $(kubectl get pods -n production | grep CrashLoop | head -1 | awk '{print $1}')",
            "kubectl logs -n production --tail=50 --previous $(kubectl get pods -n production | grep CrashLoop | head -1 | awk '{print $1}')",
            "kubectl get events -n production --sort-by='.lastTimestamp' | tail -10"
        ],
        "fix_commands": [
            "kubectl logs -n production --previous <pod>  # 看上次启动日志",
            "kubectl rollout undo deployment/order-service  # 回滚"
        ]
    },
    {
        "id": "KB006",
        "title": "磁盘空间满",
        "keywords": ["disk", "磁盘", "空间不足", "no space", "磁盘满"],
        "category": "resource",
        "common_causes": [
            "日志未轮转",
            "容器 overlay 层膨胀",
            "ES/数据库数据文件暴增",
            "Docker 镜像/容器残留"
        ],
        "check_commands": [
            "df -h",
            "du -sh /var/lib/docker/overlay2/* | sort -rh | head -10",
            "du -sh /var/log/* | sort -rh | head -10",
            "find / -xdev -size +1G -exec ls -lh {} \\; 2>/dev/null | head -20"
        ],
        "fix_commands": [
            "docker system prune -af  # 清理 Docker 残留",
            "journalctl --vacuum-size=500M  # 清理 systemd 日志",
            "truncate -s 0 /var/log/containers/*.log"
        ]
    },
    {
        "id": "KB007",
        "title": "网络延迟/丢包",
        "keywords": ["network", "网络", "延迟高", "丢包", "timeout", "connect timeout"],
        "category": "external",
        "common_causes": [
            "跨 AZ/Region 网络带宽打满",
            "DNS 解析慢",
            "TCP 连接队列溢出",
            "iptables/安全组规则误配"
        ],
        "check_commands": [
            "mtr -r -c 10 <target_ip>  # 网络路径追踪",
            "ss -s  # 查看 socket 统计",
            "sar -n DEV 1 5  # 查看网卡流量",
            "ping -c 10 <target>  # 基础延迟测试"
        ],
        "fix_commands": []
    },
    {
        "id": "KB008",
        "title": "消息队列积压",
        "keywords": ["mq", "kafka", "rabbitmq", "消息积压", "消息延迟", "consumer"],
        "category": "dependency",
        "common_causes": [
            "消费者处理能力不足",
            "消费者阻塞或挂掉",
            "分区分配不均",
            "消息体积异常大"
        ],
        "check_commands": [
            "kafka-consumer-groups --bootstrap-server <broker> --group <group> --describe",
            "rabbitmqctl list_queues name messages messages_ready",
            "curl -s http://localhost:8080/actuator/health | jq ."
        ],
        "fix_commands": [
            "kubectl scale deployment consumer-service --replicas=5",
            "kafka-consumer-groups --bootstrap-server <broker> --group <group> --reset-offsets --to-latest --execute"
        ]
    },
    {
        "id": "KB009",
        "title": "慢 SQL/数据库性能",
        "keywords": ["slow query", "慢查询", "slow sql", "数据库慢", "mysql slow"],
        "category": "application",
        "common_causes": [
            "缺少索引",
            "数据量大导致全表扫描",
            "锁等待",
            "数据库 Buffer Pool 太小"
        ],
        "check_commands": [
            "SELECT * FROM mysql.slow_log ORDER BY start_time DESC LIMIT 10;",
            "EXPLAIN ANALYZE <slow_query>;",
            "SHOW ENGINE INNODB STATUS\\G",
            "SELECT * FROM performance_schema.events_statements_summary_by_digest ORDER BY sum_timer_wait DESC LIMIT 10;"
        ],
        "fix_commands": [
            "CREATE INDEX idx_name ON table(column);",
            "ANALYZE TABLE <table_name>;"
        ]
    },
    {
        "id": "KB010",
        "title": "配置错误/发布回滚",
        "keywords": ["config", "配置", "部署", "发布", "deploy", "rollback", "回滚"],
        "category": "change",
        "common_causes": [
            "配置项拼写错误",
            "环境变量未正确注入",
            "新版本引入了 bug",
            "依赖组件版本不兼容"
        ],
        "check_commands": [
            "kubectl describe deployment order-service -n production | grep -A 5 'Args:\\|Command:'",
            "kubectl rollout history deployment/order-service -n production",
            "git log --oneline -10",
            "diff <(kubectl get configmap -n production -o yaml) <(kubectl get configmap -n production --previous -o yaml)"
        ],
        "fix_commands": [
            "kubectl rollout undo deployment/order-service -n production",
            "kubectl set env deployment/order-service KEY=VALUE"
        ]
    }
]


def build_user_prompt(incident_text: str) -> str:
    """Build the user message from raw incident text."""
    return f"""分析以下生产环境故障：

{incident_text}

请按 System Prompt 的格式输出结构化 JSON 分析报告。"""


def build_context(metrics: dict = None, logs: str = None) -> str:
    """Build additional context block."""
    parts = []
    if metrics:
        parts.append(f"## 指标数据\n{json.dumps(metrics, ensure_ascii=False, indent=2)}")
    if logs:
        parts.append(f"## 日志片段\n{logs}")
    return "\n\n".join(parts)


# ── Keyword-based quick triage (optional, for speed) ─────

def quick_match(incident_text: str) -> list:
    """Try to match incident text against knowledge base keywords."""
    results = []
    text_lower = incident_text.lower()
    for kb in KNOWLEDGE_BASE:
        score = 0
        for kw in kb["keywords"]:
            if kw.lower() in text_lower:
                score += 1
        if score > 0:
            results.append({
                "kb_id": kb["id"],
                "title": kb["title"],
                "category": kb["category"],
                "match_score": score,
                "check_commands": kb["check_commands"]
            })
    results.sort(key=lambda x: x["match_score"], reverse=True)
    return results[:5]


# ── Helper: parse AI response ─────────────────────────────

def parse_llm_response(raw: str) -> dict:
    """Extract JSON from LLM response (handles markdown code fences)."""
    # Try to find JSON between ```json and ```
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw, re.DOTALL)
    if match:
        raw = match.group(1)
    
    # Try to parse as JSON
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: return raw text
        return {"raw_response": raw, "parse_error": "Could not parse JSON"}


__all__ = [
    "SYSTEM_PROMPT", "KNOWLEDGE_BASE", "build_user_prompt",
    "build_context", "quick_match", "parse_llm_response"
]
