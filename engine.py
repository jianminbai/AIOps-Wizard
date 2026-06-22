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
    },
    {
        "id": "KB011",
        "title": "Elasticsearch 集群状态 RED/yellow",
        "keywords": ["elasticsearch", "es", "集群状态", "cluster health", "red", "yellow", "unassigned shards"],
        "category": "dependency",
        "common_causes": [
            "节点故障导致分片未分配",
            "磁盘空间不足触发只读",
            "分片分配延迟或节点数不足",
            "索引损坏或配置不合理"
        ],
        "check_commands": [
            "curl -s 'http://localhost:9200/_cluster/health?pretty' | jq .",
            "curl -s 'http://localhost:9200/_cat/allocation?v' | sort -k5 -rn",
            "curl -s 'http://localhost:9200/_cat/shards?v' | grep -E 'UNASSIGNED|INITIALIZING'",
            "curl -s 'http://localhost:9200/_cluster/allocation/explain?pretty' | jq ."
        ],
        "fix_commands": [
            "curl -s -XPUT 'http://localhost:9200/_cluster/settings?pretty' -H 'Content-Type: application/json' -d '{\"transient\":{\"cluster.routing.allocation.enable\":\"all\"}}'",
            "curl -s -XPOST 'http://localhost:9200/_cluster/reroute?retry_failed=true&pretty'",
            "curl -s -XPUT 'http://localhost:9200/_all/_settings?pretty' -H 'Content-Type: application/json' -d '{\"index.blocks.read_only_allow_delete\":null}'"
        ]
    },
    {
        "id": "KB012",
        "title": "Prometheus 告警风暴/remote write 失败",
        "keywords": ["prometheus", "alertmanager", "告警风暴", "remote write", "alerts", "alert fatigue"],
        "category": "dependency",
        "common_causes": [
            "配置文件错误或 target 不可达",
            "remote write 目标后端故障",
            "告警规则过于敏感",
            "Prometheus 自身 OOM 或磁盘满"
        ],
        "check_commands": [
            "curl -s 'http://localhost:9090/api/v1/targets' | jq '.data.activeTargets[] | select(.health != \"up\") | .labels.instance'",
            "curl -s 'http://localhost:9090/api/v1/status/config' | jq '.data.yaml' | head -50",
            "curl -s 'http://localhost:9090/api/v1/alerts' | jq '.data.alerts[] | select(.state == \"firing\") | .labels.alertname'",
            "curl -s 'http://localhost:9090/api/v1/status/runtimeinfo' | jq '.data'"
        ],
        "fix_commands": [
            "systemctl restart prometheus",
            "curl -s -XPOST 'http://localhost:9090/api/v1/admin/tsdb/delete_series?match[]={__name__=~\".+\"}'",
            "alertmanager --config.file=/etc/alertmanager/alertmanager.yml --cluster.listen-address=\"\"  # 单节点模式"
        ]
    },
    {
        "id": "KB013",
        "title": "SkyWalking trace 调用链断裂",
        "keywords": ["skywalking", "apm", "trace", "调用链", "调用链断裂", "span missing", "distributed tracing"],
        "category": "application",
        "common_causes": [
            "探针版本不兼容或配置错误",
            "采样率过低导致 span 被丢弃",
            "跨线程/异步调用未正确传递 Context",
            "gRPC 上报连接断开"
        ],
        "check_commands": [
            "curl -s 'http://localhost:12800/graphql' -H 'Content-Type: application/json' -d '{\"query\":\"query example($condition: TraceQueryCondition!){queryBasicTraces(condition:$condition){traces{key endpointNames duration start}}}}\"}'",
            "kubectl logs -n skywalking -l app=oap-server --tail=20",
            "cat /agent/skywalking-agent.jar!/skywalking-plugin.def 2>/dev/null",
            "curl -s 'http://localhost:12800/graphql' -H 'Content-Type: application/json' -d '{\"query\":\"query services{getAllServices{id name}}}\"}' | jq '.data.getAllServices | length'"
        ],
        "fix_commands": [
            "kubectl rollout restart deployment/oap-server -n skywalking",
            "java -javaagent:/agent/skywalking-agent.jar -Dskywalking.agent.service_name=my-service -jar app.jar",
            "kubectl set env deployment/my-service SW_AGENT_COLLECTOR_BACKEND_SERVICES=oap:11800"
        ]
    },
    {
        "id": "KB014",
        "title": "Docker 容器 overlay2 磁盘暴涨",
        "keywords": ["docker", "container", "overlay2", "磁盘暴涨", "disk usage", "container disk"],
        "category": "resource",
        "common_causes": [
            "容器内日志未轮转或未重定向到 stdout",
            "应用数据文件写入了容器可写层",
            "僵尸容器或悬空镜像累积",
            "Docker 数据目录未定期清理"
        ],
        "check_commands": [
            "du -sh /var/lib/docker/overlay2/* | sort -rh | head -10",
            "docker system df",
            "docker ps --size --format 'table {{.ID}}\\t{{.Names}}\\t{{.Size}}'",
            "find /var/lib/docker/overlay2 -type f -size +100M -exec ls -lh {} \\; 2>/dev/null | head -20"
        ],
        "fix_commands": [
            "docker system prune -af --volumes",
            "docker rm $(docker ps -aq --filter status=exited) 2>/dev/null",
            "docker rmi $(docker images -f 'dangling=true' -q) 2>/dev/null",
            "truncate -s 0 /var/lib/docker/containers/*/*-json.log"
        ]
    },
    {
        "id": "KB015",
        "title": "CI/CD 流水线失败（GitLab/Jenkins）",
        "keywords": ["cicd", "pipeline", "gitlab", "jenkins", "流水线", "ci", "build failed"],
        "category": "change",
        "common_causes": [
            "代码合并冲突或语法错误",
            "依赖镜像或包下载失败",
            "测试环境资源不足",
            "凭证过期或密钥轮换未同步"
        ],
        "check_commands": [
            "curl -s --header 'PRIVATE-TOKEN: <token>' 'https://gitlab.example.com/api/v4/projects/<id>/pipelines?status=failed&per_page=5' | jq '.[].web_url'",
            "curl -s -u <user>:<token> 'https://jenkins.example.com/job/<job>/lastBuild/consoleText' | tail -50",
            "kubectl describe pod -n gitlab-runner $(kubectl get pods -n gitlab-runner | grep Running | head -1 | awk '{print $1}')",
            "docker logs <runner-container> --tail 50"
        ],
        "fix_commands": [
            "git reset --hard HEAD~1 && git push --force  # 回退上次提交",
            "kubectl rollout restart deployment/gitlab-runner -n gitlab-runner",
            "curl -X POST -u <user>:<token> 'https://jenkins.example.com/job/<job>/build?delay=0sec'"
        ]
    },
    {
        "id": "KB016",
        "title": "SSL/TLS 证书过期",
        "keywords": ["ssl", "tls", "certificate", "证书过期", "cert expired", "https error", "证书"],
        "category": "external",
        "common_causes": [
            "证书未配置自动续期",
            "Let's Encrypt ACME 客户端故障",
            "Ingress/网关证书未同步更新",
            "过期前未收到告警通知"
        ],
        "check_commands": [
            "openssl s_client -connect example.com:443 -servername example.com 2>/dev/null | openssl x509 -noout -dates",
            "echo | openssl s_client -connect example.com:443 2>/dev/null | openssl x509 -noout -enddate",
            "kubectl get certificate -n production -o wide",
            "curl -vI https://example.com 2>&1 | grep -E 'SSL|TLS|certificate|expire'"
        ],
        "fix_commands": [
            "certbot renew --force-renewal",
            "kubectl delete secret tls-secret -n production && kubectl create secret tls tls-secret --cert=fullchain.pem --key=privkey.pem -n production",
            "kubectl rollout restart ingress nginx-ingress-controller -n ingress-nginx",
            "systemctl reload nginx"
        ]
    },
    {
        "id": "KB017",
        "title": "Nginx/API Gateway 502/504",
        "keywords": ["nginx", "gateway", "502", "504", "bad gateway", "upstream timeout", "反向代理"],
        "category": "dependency",
        "common_causes": [
            "上游服务不可用或重启中",
            "upstream 超时时间配置过短",
            "upstream 连接池或 worker 满",
            "后端服务 Slow Start 导致丢请求"
        ],
        "check_commands": [
            "tail -100 /var/log/nginx/error.log | grep -E '502|504|upstream'",
            "curl -s -o /dev/null -w '%{http_code}' http://localhost:80/health",
            "ss -ant | grep -E ':80|:443' | awk '{print $2}' | sort | uniq -c",
            "kubectl get endpoints -n production -o wide | grep -E 'not-ready|unreachable'"
        ],
        "fix_commands": [
            "systemctl restart nginx",
            "kubectl rollout restart deployment/gateway -n production",
            "sed -i 's/proxy_read_timeout 60s/proxy_read_timeout 300s/' /etc/nginx/conf.d/default.conf && nginx -s reload",
            "kubectl scale deployment/backend-service --replicas=3"
        ]
    },
    {
        "id": "KB018",
        "title": "etcd 集群不稳定",
        "keywords": ["etcd", "etcd cluster", "leader election", "raft", "raft 协议", "etcd 故障"],
        "category": "dependency",
        "common_causes": [
            "磁盘 IO 延迟过高影响 raft 心跳",
            "节点间网络分区",
            "etcd 存储空间配额不足",
            "集群成员变更操作不规范"
        ],
        "check_commands": [
            "ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 --cacert=/etc/etcd/ca.pem endpoint health --write-out=table",
            "ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 --cacert=/etc/etcd/ca.pem endpoint status --write-out=table",
            "ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 --cacert=/etc/etcd/ca.pem member list --write-out=table",
            "cat /var/log/etcd.log | grep -E 'leadership changed|elected leader|raft' | tail -20"
        ],
        "fix_commands": [
            "ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 --cacert=/etc/etcd/ca.pem defrag --command-timeout=30s",
            "ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 --cacert=/etc/etcd/ca.pem --command-timeout=30s alarm list",
            "ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 --cacert=/etc/etcd/ca.pem alarm disarm",
            "systemctl restart etcd"
        ]
    },
    {
        "id": "KB019",
        "title": "K8s Node NotReady",
        "keywords": ["kubernetes", "k8s", "node", "notready", "节点不可用", "node not ready", "kubelet"],
        "category": "resource",
        "common_causes": [
            "kubelet 服务停止或异常退出",
            "节点磁盘/内存/PID 资源耗尽",
            "CNI 网络插件故障",
            "节点与 apiserver 网络失联"
        ],
        "check_commands": [
            "kubectl get nodes -o wide",
            "kubectl describe node $(kubectl get nodes | grep NotReady | head -1 | awk '{print $1}') | grep -A 20 'Conditions:'",
            "ssh <node> 'systemctl status kubelet --no-pager | tail -30'",
            "ssh <node> 'journalctl -u kubelet --no-pager --since \"5 min ago\" | tail -50'"
        ],
        "fix_commands": [
            "ssh <node> 'systemctl restart kubelet'",
            "ssh <node> 'df -h && free -h && cat /proc/sys/kernel/pid_max'",
            "kubectl cordon <node> && ssh <node> 'reboot' && kubectl uncordon <node>",
            "ssh <node> 'systemctl restart docker && systemctl restart kubelet'"
        ]
    },
    {
        "id": "KB020",
        "title": "Pod Pending（资源不足）",
        "keywords": ["pod", "pending", "pod pending", "资源不足", "insufficient resources", "unschedulable"],
        "category": "resource",
        "common_causes": [
            "节点 CPU/内存资源不足以调度",
            "PVC 未绑定或存储不可用",
            "节点亲和性或污点容忍度不匹配",
            "端口冲突或资源配额限制"
        ],
        "check_commands": [
            "kubectl describe pod -n production $(kubectl get pods -n production | grep Pending | head -1 | awk '{print $1}') | grep -A 10 'Events:'",
            "kubectl get nodes -o custom-columns=NAME:.metadata.name,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory,PODS:.status.allocatable.pods",
            "kubectl describe node | grep -E 'Name:|Non-terminated|%cpu|%memory' | head -30",
            "kubectl get pvc -n production -o wide"
        ],
        "fix_commands": [
            "kubectl scale deployment <deploy> --replicas=3  # 缩小副本释放资源",
            "kubectl delete pod $(kubectl get pods -n production | grep Evicted | awk '{print $1}') -n production",
            "kubectl taint nodes <node> key=value:NoSchedule-  # 移除污点",
            "kubectl cordon <full-node> && kubectl drain <full-node> --ignore-daemonsets --delete-emptydir-data"
        ]
    },
    {
        "id": "KB021",
        "title": "Pod ImagePullBackOff",
        "keywords": ["imagepullbackoff", "image pull", "容器镜像", "拉取失败", "ErrImagePull", "registry"],
        "category": "change",
        "common_causes": [
            "镜像标签不存在或拼写错误",
            "私有仓库认证信息缺失或过期",
            "镜像拉取限流（Docker Hub rate limit）",
            "镜像仓库不可达或 DNS 解析失败"
        ],
        "check_commands": [
            "kubectl describe pod -n production $(kubectl get pods -n production | grep ImagePullBackOff | head -1 | awk '{print $1}') | grep -A 10 'Events:'",
            "kubectl get pod -n production $(kubectl get pods -n production | grep ImagePullBackOff | head -1 | awk '{print $1}') -o yaml | grep -E 'image:|imagePullPolicy'",
            "kubectl get secret -n production | grep -E 'docker|registry'",
            "crictl pull <image>:<tag> 2>&1 || docker pull <image>:<tag> 2>&1"
        ],
        "fix_commands": [
            "kubectl set image deployment/<deploy> <container>=<correct-image>:<correct-tag> -n production",
            "kubectl create secret docker-registry regcred --docker-server=<registry> --docker-username=<user> --docker-password=<pass> --docker-email=<email> -n production",
            "kubectl patch serviceaccount default -n production -p '{\"imagePullSecrets\": [{\"name\": \"regcred\"}]}'",
            "docker logout && docker login <registry>"
        ]
    },
    {
        "id": "KB022",
        "title": "TCP TIME_WAIT 连接积压",
        "keywords": ["tcp", "time_wait", "连接积压", "端口耗尽", "ephemeral ports", "connection reuse"],
        "category": "resource",
        "common_causes": [
            "短连接场景下未启用 Keep-Alive",
            "客户端频繁新建连接而不复用",
            "ephemeral 端口范围耗尽",
            "TIME_WAIT socket 回收过慢"
        ],
        "check_commands": [
            "ss -ant | awk '{print $1}' | sort | uniq -c | sort -rn",
            "ss -ant state time-wait | wc -l",
            "cat /proc/sys/net/ipv4/ip_local_port_range",
            "netstat -an | grep TIME_WAIT | wc -l"
        ],
        "fix_commands": [
            "sysctl -w net.ipv4.tcp_tw_reuse=1",
            "sysctl -w net.ipv4.tcp_fin_timeout=15",
            "sysctl -w net.ipv4.ip_local_port_range='1024 65000'",
            "sysctl -w net.ipv4.tcp_max_tw_buckets=2000000"
        ]
    },
    {
        "id": "KB023",
        "title": "文件描述符耗尽",
        "keywords": ["file descriptor", "fd", "too many open files", "文件描述符", "ulimit", "socket"],
        "category": "resource",
        "common_causes": [
            "连接未正确关闭导致 fd 泄漏",
            "日志文件句柄未释放",
            "系统 ulimit 设置过小",
            "大量定时器/文件监视器未清理"
        ],
        "check_commands": [
            "cat /proc/<pid>/limits | grep 'open files'",
            "lsof -p <pid> | wc -l",
            "lsof -p <pid> | awk '{print $5}' | sort | uniq -c | sort -rn | head -20",
            "sysctl fs.file-nr"
        ],
        "fix_commands": [
            "ulimit -n 65536 && sysctl -w fs.file-max=1000000",
            "echo '* soft nofile 1000000' >> /etc/security/limits.conf && echo '* hard nofile 1000000' >> /etc/security/limits.conf",
            "systemctl restart <service>  # 释放所有文件描述符",
            "kubectl set resources deployment/<deploy> --limits='memory=2Gi,cpu=2'"
        ]
    },
    {
        "id": "KB024",
        "title": "Java 线程死锁",
        "keywords": ["deadlock", "线程死锁", "java", "jstack", "thread dump", "BLOCKED", "死锁"],
        "category": "application",
        "common_causes": [
            "多个线程以不同顺序获取锁",
            "同步方法嵌套调用",
            "数据库行锁与代码锁混合导致",
            "线程池核心线程全部阻塞等待"
        ],
        "check_commands": [
            "jstack <pid> | grep -E 'deadlock|BLOCKED|WAITING' | head -40",
            "jstack <pid> | grep -A 30 'Found one Java-level deadlock'",
            "jcmd <pid> Thread.print | grep -E 'deadlock|BLOCKED' | head -30",
            "top -H -p <pid>  # 查看线程状态"
        ],
        "fix_commands": [
            "jstack <pid> > /tmp/threaddump_$(date +%s).txt  # 保存现场",
            "kill -3 <pid>  # 生成 thread dump 到 stdout",
            "systemctl restart <service>  # 临时恢复",
            "kubectl rollout restart deployment/<deploy> -n production"
        ]
    },
    {
        "id": "KB025",
        "title": "HPA 无法扩缩容",
        "keywords": ["hpa", "horizontal pod autoscaler", "autoscaling", "扩缩容", "auto scale", "metrics-server"],
        "category": "resource",
        "common_causes": [
            "metrics-server 未部署或不可用",
            "自定义指标 API 服务异常",
            "HPA target 值设置不合理",
            "Pod 资源 request 未设置"
        ],
        "check_commands": [
            "kubectl get hpa -n production -o wide",
            "kubectl describe hpa <hpa-name> -n production",
            "kubectl get apiservice | grep -E 'metrics|custom-metrics'",
            "kubectl top pod -n production  # 确认 metrics-server 工作"
        ],
        "fix_commands": [
            "kubectl rollout restart deployment/metrics-server -n kube-system",
            "kubectl autoscale deployment <deploy> --cpu-percent=80 --min=2 --max=10 -n production",
            "kubectl set resources deployment <deploy> --requests='cpu=500m,memory=512Mi' -n production",
            "kubectl delete hpa <hpa-name> -n production && kubectl create -f <correct-hpa.yaml>"
        ]
    },
    {
        "id": "KB026",
        "title": "DNS 解析失败",
        "keywords": ["dns", "域名解析", "dns resolution", "coredns", "nslookup", "nxdomain", "DNS 故障"],
        "category": "external",
        "common_causes": [
            "CoreDNS Pod 故障或重启",
            "上游 DNS 服务器不可达",
            "DNS 缓存污染或 TTL 过长",
            "Pod 内 resolv.conf 配置错误"
        ],
        "check_commands": [
            "kubectl run -it --rm dns-test --image=busybox:1.28 -- nslookup kubernetes.default.svc.cluster.local",
            "kubectl get pods -n kube-system -l k8s-app=kube-dns",
            "nslookup <domain>",
            "dig <domain> @8.8.8.8 +trace | tail -20"
        ],
        "fix_commands": [
            "kubectl rollout restart -n kube-system deployment/coredns",
            "kubectl scale deployment/coredns -n kube-system --replicas=2",
            "echo 'nameserver 8.8.8.8' >> /etc/resolv.conf",
            "kubectl edit configmap coredns -n kube-system  # 检查 Corefile 配置"
        ]
    },
    {
        "id": "KB027",
        "title": "ELK 摄入背压/日志延迟",
        "keywords": ["elk", "elasticsearch", "logstash", "filebeat", "日志延迟", "ingest", "背压", "backpressure"],
        "category": "dependency",
        "common_causes": [
            "Logstash/Filebeat 输出队列堵塞",
            "Elasticsearch 索引写入速度跟不上",
            "bulk 队列满或拒绝请求",
            "磁盘 IO 瓶颈"
        ],
        "check_commands": [
            "curl -s 'http://localhost:9600/_node/stats/pipelines' | jq '.pipelines.main.events'",
            "curl -s 'http://localhost:9200/_nodes/stats/thread_pool?pretty' | jq '.nodes[].thread_pool.write'",
            "curl -s 'http://localhost:9200/_cat/indices?v' | sort -k7 -rn | head -10",
            "tail -100 /var/log/logstash/logstash-plain.log | grep -E 'rejected|busy|error|blocked'"
        ],
        "fix_commands": [
            "curl -s -XPUT 'http://localhost:9200/_cluster/settings?pretty' -H 'Content-Type: application/json' -d '{\"transient\":{\"indices.memory.index_buffer_size\":\"20%\"}}'",
            "systemctl restart logstash",
            "sed -i 's/pipeline.workers: 1/pipeline.workers: 4/' /etc/logstash/logstash.yml && systemctl restart logstash",
            "kubectl scale deployment/filebeat --replicas=2"
        ]
    },
    {
        "id": "KB028",
        "title": "熔断器触发/服务降级",
        "keywords": ["circuit breaker", "熔断", "hystrix", "sentinel", "降级", "degradation", "服务降级"],
        "category": "application",
        "common_causes": [
            "下游服务超时或错误率过高",
            "熔断阈值配置过低",
            "半开恢复请求失败",
            "慢调用比例过高"
        ],
        "check_commands": [
            "curl -s http://localhost:8080/actuator/health | jq '.components.circuitBreakers'",
            "curl -s http://localhost:8080/actuator/metrics/resilience4j.circuitbreaker.calls | jq .",
            "curl -s http://localhost:8080/actuator/metrics/hystrix.circuit.breaker.current | jq .",
            "kubectl exec -it <pod> -- curl -s localhost:8080/actuator/health | jq ."
        ],
        "fix_commands": [
            "curl -s -XPOST 'http://localhost:8080/actuator/circuitbreakers/<name>?state=closed'  # 手动关闭熔断器",
            "kubectl rollout restart deployment/<downstream-service> -n production",
            "kubectl scale deployment/<downstream-service> --replicas=5 -n production",
            "sed -i 's/circuitBreaker.slidingWindowSize: 10/circuitBreaker.slidingWindowSize: 50/' application.yml"
        ]
    },
    {
        "id": "KB029",
        "title": "配置中心变更未生效",
        "keywords": ["config center", "配置中心", "nacos", "apollo", "配置变更", "热更新", "refresh"],
        "category": "change",
        "common_causes": [
            "客户端未开启动态刷新功能",
            "配置变更未发布到目标环境",
            "客户端缓存未清除",
            "@RefreshScope / @ConfigurationProperties 缺失"
        ],
        "check_commands": [
            "curl -s 'http://localhost:8080/actuator/env' | jq '.propertySources[] | select(.name | contains(\"nacos\"))' | head -30",
            "curl -s 'http://localhost:8080/actuator/refresh' -XPOST  # 触发刷新",
            "kubectl exec -it <pod> -- curl -s localhost:8080/actuator/env/<key> | jq .",
            "curl -s 'http://nacos-server:8848/nacos/v1/cs/configs?dataId=<dataId>&group=<group>'"
        ],
        "fix_commands": [
            "curl -s -XPOST 'http://localhost:8080/actuator/refresh'  # 手动触发刷新",
            "kubectl rollout restart deployment/<service> -n production",
            "curl -s 'http://nacos-server:8848/nacos/v1/cs/configs?dataId=<dataId>&group=<group>&content=<new-value>' -XPOST",
            "kubectl set env deployment/<service> SPRING_CLOUD_NACOS_CONFIG_REFRESH_ENABLED=true"
        ]
    },
    {
        "id": "KB030",
        "title": "APM 探针内存泄漏",
        "keywords": ["apm", "agent", "探针", "内存泄漏", "memory leak", "skywalking agent", "pinpoint"],
        "category": "application",
        "common_causes": [
            "探针版本与应用 JDK 不兼容",
            "span 缓存未及时释放",
            "采样配置过大导致 OOM",
            "gRPC 上报重试队列积压"
        ],
        "check_commands": [
            "jstat -gcutil <pid> 1000 5  # 观察 GC 频率",
            "jmap -histo:live <pid> | head -30  # 查看对象分布",
            "ps aux | grep -E 'skywalking|pinpoint|elastic-apm' | grep -v grep",
            "cat /proc/<pid>/status | grep -E 'VmRSS|VmSize'"
        ],
        "fix_commands": [
            "kubectl rollout restart deployment/<service> -n production  # 重启释放内存",
            "java -javaagent:/agent/skywalking-agent.jar -Dskywalking.agent.sample_n_per_3_secs=-1 -jar app.jar  # 关闭采样",
            "kubectl set resources deployment/<service> --memory=4Gi --memory=2Gi  # 增大内存限制",
            "kubectl set env deployment/<service> SW_AGENT_COLLECTOR_BACKEND_SERVICES=oap:11800 SW_AGENT_BUFFER_SIZE_LIMIT=5000"
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
