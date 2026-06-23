"""
Context Collector — AIOps-Wizard
==================================
Gathers operational context from multiple data sources (K8s, Elasticsearch,
Prometheus, OpenTelemetry) and packages them into a structured Context Package
for downstream RCA analysis.

Architecture:
  Alert Input
      │
      ▼
  ContextCollector
      ├── K8sPlugin        (kubectl get pods, describe, top, events)
      ├── ElasticPlugin    (recent error logs, top errors, traces)
      ├── PrometheusPlugin (CPU, Memory, Latency, Error Rate)
      └── OtelPlugin       (trace data, spans)
      │
      ▼
  Context Package (dict) → RCA Agent
"""

import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from typing import Any


# ── Helpers ──────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 15) -> str | None:
    """Run a shell command, return stdout or None on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# ── Context Package ──────────────────────────────────────────────────

ContextPackage = dict[str, Any]


def empty_package() -> ContextPackage:
    return {
        "alert": {},
        "kubernetes": {},
        "elasticsearch": {},
        "prometheus": {},
        "otel": {},
        "collected_at": datetime.utcnow().isoformat() + "Z",
    }


# ── Plugins ──────────────────────────────────────────────────────────

class K8sPlugin:
    """Collect Kubernetes context around a namespace / service."""

    def __init__(self, namespace: str = "default", service: str = ""):
        self.namespace = namespace
        self.service = service

    def collect(self) -> dict[str, Any]:
        ctx: dict[str, Any] = {"namespace": self.namespace, "service": self.service}

        # Pod list
        if self.service:
            out = _run(["kubectl", "get", "pods", "-n", self.namespace,
                         "-l", f"app={self.service}", "-o", "wide", "--no-headers"])
        else:
            out = _run(["kubectl", "get", "pods", "-n", self.namespace,
                         "-o", "wide", "--no-headers"])
        if out:
            ctx["pods"] = out.strip()
        else:
            return self._sample()

        # Describe problem pods
        if ctx.get("pods"):
            lines = [l for l in ctx["pods"].split("\n") if l.strip()]
            for line in lines[:5]:  # top 5
                parts = line.split()
                if parts:
                    pod_name = parts[0]
                    desc = _run(["kubectl", "describe", "pod", "-n",
                                  self.namespace, pod_name])
                    if desc:
                        ctx.setdefault("descriptions", {})[pod_name] = desc[:2000]
                    break  # just one pod for brevity

        # Events
        events = _run(["kubectl", "get", "events", "-n", self.namespace,
                        "--sort-by=.lastTimestamp", "--no-headers"])
        if events:
            ctx["events"] = events.strip()

        # Resource usage
        top = _run(["kubectl", "top", "pod", "-n", self.namespace, "--no-headers"])
        if top:
            ctx["resource_usage"] = top.strip()

        ctx["data_source"] = "live" if ctx.get("pods") else "sample"
        return ctx

    def _sample(self) -> dict[str, Any]:
        """Return sample K8s context when cluster is unreachable."""
        return {
            "namespace": self.namespace,
            "service": self.service,
            "data_source": "sample",
            "pods": (
                f"{self.service}-7d8f9c6b4-x2mno   1/1  Running  0  12h\n"
                f"{self.service}-7d8f9c6b4-j4klp   1/1  Running  0  12h\n"
                f"{self.service}-7d8f9c6b4-q3wxe   0/1  Pending  0  5m"
            ),
            "events": (
                "5m         Warning  FailedScaleUp    horizontalpodautoscaler/payment-api  "
                "Failed to scale up: missing metrics\n"
                "12h        Normal   ScaledReplicaSet  deployment/payment-api               "
                "Scaled up to 3 replicas"
            ),
            "resource_usage": (
                f"{self.service}-7d8f9c6b4-x2mno  500m  256Mi\n"
                f"{self.service}-7d8f9c6b4-j4klp  980m  512Mi\n"
                f"{self.service}-7d8f9c6b4-q3wxe  <unknown>  <unknown>"
            ),
        }


class ElasticPlugin:
    """Collect log context from Elasticsearch."""

    def __init__(self, hosts: list[str] | None = None):
        self.hosts = hosts or _env("ES_HOSTS", "http://localhost:9200").split(",")

    def collect(self, service: str = "", minutes: int = 30) -> dict[str, Any]:
        ctx: dict[str, Any] = {"time_range": f"last {minutes} minutes"}

        # Try to query real ES
        es_query = self._build_es_query(service, minutes)
        result = _run([
            "curl", "-s", "--max-time", "10",
            f"{self.hosts[0]}/_search",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(es_query),
        ])
        if result:
            try:
                data = json.loads(result)
                hits = data.get("hits", {}).get("hits", [])
                if hits:
                    ctx["recent_logs"] = [
                        h["_source"].get("message", str(h["_source"]))[:500]
                        for h in hits[:10]
                    ]
                    ctx["total_hits"] = data["hits"]["total"]["value"]
                    ctx["data_source"] = "live"
                    return ctx
            except (json.JSONDecodeError, KeyError):
                pass

        return self._sample(service)

    def _build_es_query(self, service: str, minutes: int) -> dict:
        must = [{"range": {"@timestamp": {"gte": f"now-{minutes}m"}}}]
        if service:
            must.append({"match": {"service.name": service}})
        return {
            "size": 20,
            "query": {"bool": {"must": must}},
            "sort": [{"@timestamp": "desc"}],
        }

    def _sample(self, service: str) -> dict[str, Any]:
        service_prefix = service or "payment-api"
        return {
            "data_source": "sample",
            "time_range": "last 30 minutes",
            "total_hits": 156,
            "recent_logs": [
                f"2026-06-23T14:01:15 ERROR [{service_prefix}] "
                f"o.a.h.c.pool.PoolingHttpClientConnectionManager - "
                f"Connection to https://prometheus.internal:9090 refused",
                f"2026-06-23T14:01:20 ERROR [{service_prefix}] "
                f"c.n.s.s.r.ScrapeManager - Scrape failed: "
                f"connection refused to prometheus.internal:9090",
                f"2026-06-23T14:02:00 WARN  [{service_prefix}] "
                f"o.s.b.a.j.DatabaseDriver - HikariPool-1: Thread starvation detected",
                f"2026-06-23T14:03:30 ERROR [{service_prefix}] "
                f"o.a.c.c.C.[.[.[/] - Servlet.service() for servlet "
                f"[dispatcherServlet] threw exception: java.net.SocketTimeoutException",
                f"2026-06-23T14:05:00 ERROR [{service_prefix}] "
                f"g.z.p.c.Controller - Request failed with status 503: "
                f"Service Unavailable",
            ],
            "error_counts": {
                "connection_refused": 24,
                "socket_timeout": 18,
                "503_service_unavailable": 42,
                "thread_starvation": 6,
            },
        }


class PrometheusPlugin:
    """Collect metrics context from Prometheus."""

    def __init__(self, host: str = ""):
        self.host = host or _env("PROMETHEUS_HOST", "http://localhost:9090")

    def collect(self, service: str = "", minutes: int = 30) -> dict[str, Any]:
        ctx: dict[str, Any] = {"time_range": f"last {minutes} minutes", "service": service}

        # Try real Prometheus queries
        queries = {
            "cpu": f'rate(process_cpu_seconds_total{{job="{service}"}}[5m])' if service
                    else 'rate(node_cpu_seconds_total[5m])',
            "memory": f'process_resident_memory_bytes{{job="{service}"}}' if service
                      else 'node_memory_MemTotal_bytes',
        }
        for key, query in queries.items():
            result = _run([
                "curl", "-s", "--max-time", "5",
                f"{self.host}/api/v1/query",
                "--data-urlencode", f"query={query}",
            ])
            if result:
                try:
                    data = json.loads(result)
                    ctx[key] = data
                except json.JSONDecodeError:
                    pass

        if ctx.get("cpu") or ctx.get("memory"):
            ctx["data_source"] = "live"
        else:
            return self._sample(service)

        return ctx

    def _sample(self, service: str) -> dict[str, Any]:
        return {
            "data_source": "sample",
            "time_range": "last 30 minutes",
            "service": service,
            "cpu": {
                "status": "success",
                "data": {
                    "result": [
                        {"metric": {"pod": f"{service}-7d8f9c6b4-x2mno"},
                         "value": [1719136870, "0.35"]},
                        {"metric": {"pod": f"{service}-7d8f9c6b4-j4klp"},
                         "value": [1719136870, "0.92"]},
                        {"metric": {"pod": f"{service}-7d8f9c6b4-q3wxe"},
                         "value": [1719136870, "0.88"]},
                    ]
                }
            },
            "memory": {
                "status": "success",
                "data": {
                    "result": [
                        {"metric": {"pod": f"{service}-7d8f9c6b4-x2mno"},
                         "value": [1719136870, "268435456"]},
                        {"metric": {"pod": f"{service}-7d8f9c6b4-j4klp"},
                         "value": [1719136870, "536870912"]},
                    ]
                }
            },
        }


class OtelPlugin:
    """Collect OpenTelemetry trace / span context."""

    def __init__(self, endpoint: str = ""):
        self.endpoint = endpoint or _env("OTEL_ENDPOINT", "http://localhost:4318")

    def collect(self, service: str = "", minutes: int = 30) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "time_range": f"last {minutes} minutes",
            "service": service,
        }

        # Try Otel collector health check
        result = _run(["curl", "-s", "--max-time", "3",
                        f"{self.endpoint}/v1/traces"])
        if result is None:
            return self._sample(service)

        ctx["data_source"] = "live"
        return ctx

    def _sample(self, service: str) -> dict[str, Any]:
        return {
            "data_source": "sample",
            "service": service,
            "traces": [
                {
                    "trace_id": "abc123",
                    "root_span": "payment-api.POST /api/order",
                    "duration_ms": 12500,
                    "error": True,
                    "spans": [
                        {"name": "HTTP POST /api/order", "duration_ms": 12500, "status": "error"},
                        {"name": "validate_token", "duration_ms": 15, "status": "ok"},
                        {"name": "check_inventory", "duration_ms": 12000, "status": "error"},
                        {"name": "redis.get:inventory_cache", "duration_ms": 11500, "status": "error"},
                    ]
                },
                {
                    "trace_id": "abc124",
                    "root_span": "payment-api.GET /health",
                    "duration_ms": 3000,
                    "error": True,
                    "spans": [
                        {"name": "GET /health", "duration_ms": 3000, "status": "error"},
                        {"name": "check_prometheus_connectivity", "duration_ms": 2900, "status": "error"},
                    ]
                }
            ]
        }


# ── Main Collector ───────────────────────────────────────────────────

def collect_context(
    alert: str,
    service: str = "",
    namespace: str = "default",
    enable_k8s: bool = True,
    enable_es: bool = True,
    enable_prometheus: bool = True,
    enable_otel: bool = True,
) -> ContextPackage:
    """Collect context from all enabled plugins and return a Context Package.

    Args:
        alert: The raw alert text.
        service: Service name (e.g. 'payment-api').
        namespace: K8s namespace.
        enable_*: Toggle individual plugins.

    Returns:
        ContextPackage dict with all collected data.
    """
    pkg = empty_package()
    pkg["alert"] = {"text": alert, "service": service, "namespace": namespace}
    pkg["collected_at"] = datetime.utcnow().isoformat() + "Z"

    if enable_k8s:
        k8s = K8sPlugin(namespace=namespace, service=service)
        pkg["kubernetes"] = k8s.collect()

    if enable_es:
        es = ElasticPlugin()
        pkg["elasticsearch"] = es.collect(service=service)

    if enable_prometheus:
        prom = PrometheusPlugin()
        pkg["prometheus"] = prom.collect(service=service)

    if enable_otel:
        otel = OtelPlugin()
        pkg["otel"] = otel.collect(service=service)

    return pkg


def summarize_package(pkg: ContextPackage) -> str:
    """Produce a human-readable summary of the context package for LLM prompts."""
    lines = []
    lines.append(f"=== Context Package ===")
    lines.append(f"Alert: {pkg['alert'].get('text', 'N/A')}")
    lines.append(f"Service: {pkg['alert'].get('service', 'N/A')}")
    lines.append(f"Collected: {pkg['collected_at']}")
    lines.append("")

    k = pkg.get("kubernetes", {})
    if k:
        lines.append("--- Kubernetes ---")
        lines.append(f"Source: {k.get('data_source', 'N/A')}")
        if k.get("pods"):
            lines.append("Pods:\n" + k["pods"])
        if k.get("events"):
            lines.append("Events:\n" + k["events"][:1000])
        if k.get("resource_usage"):
            lines.append("Resource Usage:\n" + k["resource_usage"])

    es = pkg.get("elasticsearch", {})
    if es:
        lines.append("")
        lines.append("--- Elasticsearch ---")
        lines.append(f"Source: {es.get('data_source', 'N/A')}")
        lines.append(f"Total hits: {es.get('total_hits', 0)}")
        if es.get("error_counts"):
            lines.append("Error counts: " + json.dumps(es["error_counts"], indent=2))
        if es.get("recent_logs"):
            lines.append("Recent logs:")
            for log in es["recent_logs"][:8]:
                lines.append(f"  {log}")

    prom = pkg.get("prometheus", {})
    if prom:
        lines.append("")
        lines.append("--- Prometheus ---")
        lines.append(f"Source: {prom.get('data_source', 'N/A')}")

    otel = pkg.get("otel", {})
    if otel:
        lines.append("")
        lines.append("--- OpenTelemetry ---")
        lines.append(f"Source: {otel.get('data_source', 'N/A')}")
        if otel.get("traces"):
            for t in otel["traces"][:3]:
                lines.append(f"  Trace {t.get('trace_id')}: {t.get('root_span')} "
                           f"({t.get('duration_ms')}ms) {'❌' if t.get('error') else '✅'}")

    return "\n".join(lines)
