# AIOps-Wizard 🧙‍♂️🔧

**AI-powered Ops Fault Analysis Assistant — 智能运维故障分析助手**

一键分析生产环境告警，自动输出根因分析、排查步骤、修复命令。基于 DeepSeek + 32 类运维知识库，专为 SRE / DevOps 工程师设计。

🌐 **在线体验**: https://jianminbai.github.io/AIOps-Wizard/

---

## 📸 界面展示

### 主界面 — 输入告警

![主界面](docs/images/main-interface.jpg)

输入故障描述，选择 AI 模型，一键分析。

### 智能分析结果

![诊断结果](docs/images/diagnostics.jpg)

自动匹配知识库 + DeepSeek 深度分析，按优先级输出根因假设、验证步骤和修复命令。

### 根因分析 + 操作建议

![根因分析](docs/images/analysis-result.jpg)

每条根因假设附带具体排查命令和修复命令，工程师可直接复制执行。

---

## ✨ 核心功能

| 功能 | 说明 |
| --- | --- |
| 🧠 **AI 智能分析** | DeepSeek 驱动的故障分析引擎，输出结构化 JSON 报告 |
| 📚 **32 类知识库** | CPU/OOM/DB/Redis/K8s/ES/Prometheus/SkyWalking/网络/MQ 等 |
| 🔍 **置信度路由** | score≥0.70 直接返回 KB（零成本），≥0.40 带 KB 上下文分析，<0.40 纯 LLM |
| 🔒 **三层安全防护** | API Key 认证 + 频率限制 + 每日 LLM 调用额度（双重限制） |
| 💻 **可执行命令** | 每条建议附带 `kubectl`、`redis-cli`、`jstack` 等具体命令 |
| 🌐 **Web 前端** | 纯静态 UI，支持 GitHub Pages 部署 |
| 🐳 **Docker 支持** | 一键容器化部署 |
| 🔌 **开放 API** | RESTful 接口，可集成飞书/钉钉/自建平台 |

---

## 🔒 安全防护（生产环境必备）

三层防护体系，防止公开 API 被滥用和 token 耗尽：

```
                  ┌─────────────────────────┐
                  │     API Key 认证         │ ← 401（没 key 直接拒绝）
                  │  Header: X-API-Key       │
                  └────────┬────────────────┘
                           ▼
                  ┌─────────────────────────┐
                  │  频率限制（每 IP）        │ ← 429（超限拒绝）
                  │  AIOPS_RATE_LIMIT        │
                  └────────┬────────────────┘
                           ▼
                  ┌─────────────────────────┐
                  │  置信度路由              │ ← DIRECT（≥0.7）免费
                  │  DIRECT / CONTEXT / LLM  │
                  └────────┬────────────────┘
                           ▼
                  ┌─────────────────────────┐
                  │  LLM 额度（双重限制）     │ ← 配额耗尽返回提示
                  │  每 IP: 30次/天          │
                  │  全局: 500次/天          │
                  └─────────────────────────┘
```

### API Key 认证

设置 `AIOPS_API_KEY` 环境变量即开启认证。前端输入 Key 后自动存入 localStorage。

| 方式 | 示例 |
| --- | --- |
| HTTP Header | `X-API-Key: ***` |
| URL 参数 | `?api_key=***` |
| 前端输入 | 页面文本框输入（自动保存） |

### 频率限制

- 每 IP 每分钟最多 N 次请求（默认 5，通过 `AIOPS_RATE_LIMIT` 配置）
- 窗口为滑动的 60 秒，超限返回 HTTP 429
- 基于内存计数，重启后重置

### 每日 LLM 调用额度

| 限制级别 | 默认值 | 环境变量 | 说明 |
| --- | --- | --- | --- |
| 每 IP 上限 | 30 次/天 | `AIOPS_LLM_PER_IP_DAILY` | 单个来源用满即停，不影响他人 |
| 全局总上限 | 500 次/天 | `AIOPS_LLM_GLOBAL_DAILY` | 所有 IP 总和的保险阀 |

- 高置信度匹配（DIRECT 路由，score≥0.70）**不消耗额度**，免费返回
- 额度持久化到 `.llm_quota.json`，容器重启不丢失

### 额度查看

```bash
curl -s http://localhost:8766/health | python3 -m json.tool
```

```json
{
  "status": "ok",
  "kb_count": 32,
  "kb_source": "yaml",
  "auth_required": true,
  "rate_limit": 5,
  "quota": {
    "my_ip": "203.0.113.1",
    "my_llm_today": 3,
    "my_llm_limit": 30,
    "my_direct_today": 12,
    "global_llm_today": 15,
    "global_llm_limit": 500,
    "remaining": 27
  }
}
```

---

## 🚀 快速启动

### 方式一：Python 直接运行

```bash
# 1. 安装依赖
pip install fastapi uvicorn openai pydantic httpx pyyaml

# 2. 配置环境变量
export DEEPSEEK_API_KEY="sk-xxx"
export AIOPS_API_KEY="your-api-key"   # 可选，不设置则公开访问
export AIOPS_RATE_LIMIT=5             # 可选，默认 5次/分钟/IP
export AIOPS_LLM_PER_IP_DAILY=30      # 可选，默认 30次/天/IP
export AIOPS_LLM_GLOBAL_DAILY=500     # 可选，默认 500次/天

# 3. 启动服务
python3 api.py

# 4. 访问
open http://localhost:8766
```

### 方式二：Docker 部署（推荐）

```bash
# 先生成 API Key
AIOPS_KEY=$(head -c 32 /dev/urandom | base64)
echo "你的 API Key: $AIOPS_KEY"

# 设置所有环境变量
export DEEPSEEK_API_KEY="sk-xxx"
export AIOPS_API_KEY="***"
export AIOPS_RATE_LIMIT=5
export AIOPS_LLM_PER_IP_DAILY=30
export AIOPS_LLM_GLOBAL_DAILY=500

# 启动
docker compose build --no-cache
docker compose up -d

# 查看日志
docker logs -f aiops-wizard
```

### 方式三：GitHub Pages + Cloudflare Tunnel

前端托管在 GitHub Pages，API 通过 Cloudflare Tunnel 转发到内网服务器：

```bash
# 在服务器上设置所有变量
export DEEPSEEK_API_KEY="sk-xxx"
export AIOPS_API_KEY="***"
export AIOPS_RATE_LIMIT=5
export AIOPS_LLM_PER_IP_DAILY=30
export AIOPS_LLM_GLOBAL_DAILY=500

# 启动 API
docker compose up -d

# 启动 Cloudflare Tunnel
docker run -d --name cloudflared --restart unless-stopped \
  --network host \
  cloudflare/cloudflared tunnel --url http://localhost:8766

# 前端访问
open https://jianminbai.github.io/AIOps-Wizard/
```

> 前端支持通过 URL 参数 `?api=https://your-tunnel-url` 或页面内输入来配置 API 地址和 API Key。

---

## 📖 API 文档

### `POST /analyze` — 分析故障

**请求头（如有 API Key 认证）：**

```
X-API-Key: ***
```

**请求体：**

```json
{
  "incident": "order-service P95 latency from 200ms to 3s, CPU 98%",
  "metrics": {"cpu": 98, "memory": 85},
  "logs": "2026-06-21 10:00:00 ERROR [http-nio-8080] ...",
  "model": "deepseek-chat",
  "force_llm": false
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `incident` | string | ✅ | 故障描述文本 |
| `metrics` | object | ❌ | 指标数据 |
| `logs` | string | ❌ | 日志片段 |
| `model` | string | ❌ | 模型名（默认 deepseek-chat） |
| `force_llm` | bool | ❌ | 强制走 LLM（跳过 KB 匹配） |

**响应：**

```json
{
  "success": true,
  "confidence_route": "CONTEXT",
  "confidence_score": 0.55,
  "quota_remaining": 29,
  "kb_direct_answer": null,
  "quick_matches": [
    {"kb_id": "KB001", "title": "CPU 飙高 - 应用线程阻塞", "category": "resource"}
  ],
  "analysis": {
    "classification": "资源",
    "severity_assessment": "true_fault",
    "root_cause_hypotheses": [
      {
        "rank": 1,
        "cause": "CPU 资源耗尽导致请求排队",
        "probability": "high",
        "evidence": "CPU 98%, P95 latency 上升 15x",
        "verify_steps": ["top -H -p $(pgrep -f order-service)", "jstack ..."]
      }
    ],
    "immediate_actions": ["扩容 Pod / 增加限流"]
  }
}
```

| 响应字段 | 说明 |
| --- | --- |
| `confidence_route` | 路由方式：`DIRECT` / `CONTEXT` / `LLM_ONLY` |
| `confidence_score` | 知识库匹配置信度（0-1） |
| `quota_remaining` | 本 IP 今日剩余额度 |
| `kb_direct_answer` | DIRECT 路由时直接返回的 KB 条目 |
| `quick_matches` | 匹配到的知识库列表 |

### 错误码

| HTTP 状态码 | 场景 | 说明 |
| --- | --- | --- |
| 200 | 分析成功 | 正常返回分析结果 |
| 401 | 未提供 API Key | `X-API-Key` 缺失或错误 |
| 429 | 频率超限 | 当前 IP 每分钟超过 `AIOPS_RATE_LIMIT` 次 |
| 429 | 日额度耗尽 | `detail` 字段会说明是 IP 限制还是全局限制 |
| 422 | 参数错误 | 请求体格式不对 |

### `GET /health` — 健康检查

```json
{
  "status": "ok",
  "kb_count": 32,
  "kb_source": "yaml",
  "auth_required": true,
  "rate_limit": 5,
  "quota": {
    "my_ip": "203.0.113.1",
    "my_llm_today": 3,
    "my_llm_limit": 30,
    "my_direct_today": 12,
    "global_llm_today": 15,
    "global_llm_limit": 500,
    "remaining": 27
  }
}
```

### `GET /kb` — 知识库列表

```json
{
  "entries": [
    {"id": "KB001", "title": "CPU 飙高 - 应用线程阻塞", "category": "resource"},
    ...
  ]
}
```

支持筛选：`GET /kb?category=resource` 或 `GET /kb?search=cpu`

### `GET /kb/{id}` — 知识库详情

返回单条 KB 的完整内容，含 `common_causes`、`check_commands`、`fix_commands`。

### `POST /kb` — 添加知识库

```bash
curl -X POST http://localhost:8766/kb \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{
    "title": "你的故障场景",
    "keywords": ["关键词1", "keyword2"],
    "category": "resource",
    "common_causes": ["原因1", "原因2"],
    "check_commands": ["kubectl ...", "curl ..."],
    "fix_commands": ["systemctl restart ..."]
  }'
```

---

## 🧠 企业知识库管理

### 架构

```
kb/                    ← YAML 知识库文件（可直接编辑）
├── resource.yaml      ← 资源类（CPU/OOM/磁盘等）
├── dependency.yaml    ← 依赖类（DB/Redis/ES/MQ等）
├── application.yaml   ← 应用类（CrashLoop/慢SQL/死锁等）
├── change.yaml        ← 变更类（发布/CI-CD/证书等）
├── external.yaml      ← 外部类（网络/DNS等）
└── template.yaml      ← 新增条目的模板

kb_loader.py           ← 加载器 + 置信度匹配引擎
```

### 如何扩充知识库

**方式一：直接编辑 YAML 文件（推荐）**

```bash
# 1. 用模板创建新条目
cp kb/template.yaml kb/new_entry.yaml

# 2. 编辑文件，填写你的经验
vim kb/new_entry.yaml

# 3. 重启 API 即可生效（无需其他操作）
# API 会自动加载所有 YAML 文件
```

**方式二：通过 API 添加（同上方 API 文档）**

### 置信度路由机制

```
score >= 0.70 ──→ DIRECT   直接返回知识库条目（不调 LLM，零成本，不消耗额度）
score >= 0.40 ──→ CONTEXT  知识库作为上下文 + LLM 深度分析（消耗额度）
score <  0.40 ──→ LLM_ONLY 纯 LLM 分析（消耗额度）
```

**各字段对置信度的影响：**

| 匹配类型 | 加分 | 说明 |
| --- | --- | --- |
| 单个关键词匹配 | +0.15 | 如 "redis"、"cpu" |
| 标题包含匹配词 | +0.30 | 告警内容命中条目标题 |
| 精确短语匹配 | +0.10 | 引号包裹的连续词组 |
| 3+ 个关键词匹配 | +0.20（额外） | 多条关键词锁定场景 |
| confidence_weight | ×权重 | YAML 中可自定义（默认 1.0） |

### 企业应用场景

**场景一：积累排障经验**

每次故障复盘后，把结论写成一条 KB 条目，存入对应分类的 YAML 文件。下次同样的告警来的时候，系统会直接返回你的经验，不需要再查。

**场景二：新员工 onboarding**

新人值班时，输入告警就能看到老司机写的排查步骤和修复命令，直接复制执行。

**场景三：多团队共享**

不同团队有各自的 YAML 文件（`team-a.yaml`、`team-b.yaml`），API 自动合并。每个团队独立维护自己的知识库。

| ID | 类别 | 故障场景 |
| --- | --- | --- |
| KB001 | 资源 | CPU 飙高 - 应用线程阻塞 |
| KB002 | 资源 | 内存溢出 (OOMKilled) |
| KB003 | 依赖 | 数据库连接池耗尽 |
| KB004 | 依赖 | Redis 延迟飙升 |
| KB005 | 应用 | Pod CrashLoopBackOff |
| KB006 | 资源 | 磁盘空间满 |
| KB007 | 外部 | 网络延迟/丢包 |
| KB008 | 依赖 | 消息队列积压 |
| KB009 | 应用 | 慢 SQL/数据库性能 |
| KB010 | 变更 | 配置错误/发布回滚 |
| KB011 | 依赖 | Elasticsearch 集群状态 RED/yellow |
| KB012 | 依赖 | Prometheus 告警风暴/remote write 失败 |
| KB013 | 应用 | SkyWalking 调用链断裂 |
| KB014 | 资源 | Docker overlay2 磁盘暴涨 |
| KB015 | 变更 | CI/CD 流水线失败 |
| KB016 | 变更 | SSL/TLS 证书过期 |
| KB017 | 依赖 | Nginx/API Gateway 502/504 |
| KB018 | 依赖 | etcd 集群不稳定 |
| KB019 | 资源 | K8s Node NotReady |
| KB020 | 资源 | Pod Pending（资源不足） |
| KB021 | 变更 | Pod ImagePullBackOff |
| KB022 | 资源 | TCP TIME_WAIT 连接积压 |
| KB023 | 资源 | 文件描述符耗尽 |
| KB024 | 应用 | Java 线程死锁 |
| KB025 | 资源 | HPA 无法扩缩容 |
| KB026 | 外部 | DNS 解析失败 |
| KB027 | 依赖 | ELK 摄入背压/日志延迟 |
| KB028 | 应用 | 熔断器触发/服务降级 |
| KB029 | 变更 | 配置中心变更未生效 |
| KB030 | 应用 | APM 探针内存泄漏 |

每条知识库包含 `common_causes`、`check_commands`、`fix_commands`，覆盖从 Elasticsearch 到 K8s 到 CI/CD 的全栈运维场景。

---

## 🏗️ 项目结构

```
AIOps-Wizard/
├── engine.py              # 核心分析引擎（System Prompt + 知识库集成）
├── api.py                 # FastAPI 后端服务（含三层安全防护）
├── auth_middleware.py     # API Key 认证中间件
├── rate_limiter.py        # 频率限制 + 每日 LLM 额度中间件
├── kb_loader.py           # YAML 知识库加载器
├── kb/                    # YAML 知识库目录
│   ├── resource.yaml
│   ├── dependency.yaml
│   ├── application.yaml
│   ├── change.yaml
│   ├── external.yaml
│   └── template.yaml
├── .llm_quota.json        # 额度持久化文件
├── index.html             # 本地前端页面
├── docs/
│   ├── index.html         # GitHub Pages 前端（可配置 API 地址和 Key）
│   └── images/            # 截图资源
│       ├── main-interface.jpg
│       ├── diagnostics.jpg
│       └── analysis-result.jpg
├── Dockerfile             # Docker 镜像构建
├── docker-compose.yml     # Docker Compose 编排
├── .dockerignore
├── .github/workflows/
│   └── deploy-pages.yml   # GitHub Actions 自动部署
└── README.md
```

---

## 🐳 Docker 部署详细说明

```bash
# 构建并启动
cd AIOps-Wizard
export DEEPSEEK_API_KEY="sk-xxx"
export AIOPS_API_KEY="***"
export AIOPS_RATE_LIMIT=5
export AIOPS_LLM_PER_IP_DAILY=30
export AIOPS_LLM_GLOBAL_DAILY=500
docker compose build --no-cache
docker compose up -d

# 查看运行状态
docker ps
docker logs aiops-wizard

# 验证
curl -s http://localhost:8766/health | python3 -m json.tool

# 带 Key 调用分析
curl -s http://localhost:8766/analyze -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{"incident":"order-service CPU 98%, 503 错误"}' | python3 -m json.tool

# 停止
docker compose down
```

### 环境变量

| 变量 | 说明 | 默认值 | 必填 |
| --- | --- | --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | — | ✅ |
| `AIOPS_API_KEY` | API Key 认证（不设置则公开访问） | — | ❌ |
| `AIOPS_RATE_LIMIT` | 每 IP 每分钟最大请求数 | `5` | ❌ |
| `AIOPS_LLM_PER_IP_DAILY` | 每 IP 每日 LLM 调用上限（DIRECT 不计算） | `30` | ❌ |
| `AIOPS_LLM_GLOBAL_DAILY` | 全局每日 LLM 调用总上限 | `500` | ❌ |
| `AIOPS_DIRECT_THRESHOLD` | DIRECT 路由置信度阈值 | `0.7` | ❌ |
| `PORT` | 服务端口 | `8766` | ❌ |

> **注意**：`docker-compose.yml` 通过 `$变量名` 引用环境变量，请使用 `export` 设置或在 `.env` 文件中定义。

---

## 🌐 GitHub Pages + Cloudflare Tunnel 生产部署

### 1. 服务器端

```bash
# 设置所有环境变量
export DEEPSEEK_API_KEY="sk-xxx"
export AIOPS_API_KEY="***"
export AIOPS_RATE_LIMIT=5
export AIOPS_LLM_PER_IP_DAILY=30
export AIOPS_LLM_GLOBAL_DAILY=500

# 启动 API
docker compose up -d

# 启动 Cloudflare Tunnel（后台持久运行）
docker run -d \
  --name cloudflared \
  --restart unless-stopped \
  --network host \
  cloudflare/cloudflared tunnel --url http://localhost:8766

# 查看 Tunnel URL
docker logs cloudflared 2>&1 | grep -i "tunnel has been created"
```

### 2. 前端配置

Tunnel URL 会打印在日志中，例如 `https://xxx.trycloudflare.com`

| 方式 | 操作 |
| --- | --- |
| **自动** | GitHub Pages 前端已有默认 API 地址配置 |
| **手动** | 打开前端 → API Endpoint 输入框 → 粘贴 Tunnel URL → Save |
| **URL 参数** | `https://jianminbai.github.io/AIOps-Wizard/?api=https://your-tunnel-url` |
| **API Key** | 前端输入框填写 Key（自动保存到 localStorage） |

---

## 🧪 使用示例

### Redis 延迟飙升

```text
Incident: Cache read timeout spikes
Service: product-service
Severity: P2
Error: "JedisConnectionException: Unexpected end of stream"
Metrics: Redis CPU 90%, latency P99 500ms
Signs: some cache keys are large JSON blobs (100KB+)
```

**分析结果：**
- 🔍 快速匹配：Redis 延迟飙升
- 📌 分类：依赖
- ⚠️ 严重度：true_fault
- #1 **[high]** Redis CPU 过载，大键导致序列化/反序列化开销高
- #2 **[medium]** 连接池配置不足（Jedis 默认 maxTotal=8 可能过低）
- 验证命令：`redis-cli --bigkeys`、`redis-cli SLOWLOG GET 10`
- 修复命令：`kubectl exec redis-pod -- redis-cli CONFIG SET maxmemory-policy allkeys-lru`

### Pod CrashLoopBackOff

```text
Incident: order-service Pod CrashLoopBackOff
Service: order-service
Severity: P1
Pod status: CrashLoopBackOff, restarted 5 times in 10 minutes
Logs: "java.lang.OutOfMemoryError: Java heap space" at startup
Resources: memory limit 512Mi, CPU limit 500m
```

---

## 🛠️ 技术栈

| 组件 | 技术 |
| --- | --- |
| **AI 引擎** | DeepSeek API |
| **后端框架** | FastAPI + Uvicorn |
| **安全防护** | API Key 认证 + 频率限制 + 双重额度控制 |
| **知识库** | 32 类运维场景，YAML 文件，中英双语关键词 |
| **置信度路由** | 三重路由控制（DIRECT/CONTEXT/LLM_ONLY），自动省成本 |
| **前端** | Vanilla HTML/CSS/JS |
| **容器化** | Docker + Docker Compose |
| **CI/CD** | GitHub Actions + GitHub Pages |
| **网络** | Cloudflare Tunnel（可选） |

---

## 📋 使用场景

- **值班排障** — 收到告警 → 粘贴到前端 → 秒级获取诊断建议
- **故障复盘** — 结构化分析报告可直接用于事后回顾
- **运维新人培训** — 内置知识库是现成的排障指南
- **飞书/钉钉集成** — 通过 API 对接告警机器人
- **Prometheus Alertmanager Webhook** — 自动分析告警
- **公开 API 安全暴露** — 三层防护保证线上服务不被滥用

---

## 🤝 贡献

欢迎提交 Issue 和 PR！如果你有生产环境排障经验，欢迎扩充知识库。

---

## 📄 许可证

MIT License

---

*Made with ❤️ by jianminbai*
