# AIOps-Wizard 🧙‍♂️🔧

**AI-powered Ops Fault Analysis Assistant — 智能运维故障分析助手**

一键分析生产环境告警，自动输出根因分析、排查步骤、修复命令。基于 DeepSeek + 30 类运维知识库，专为 SRE / DevOps 工程师设计。

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
| 📚 **30 类知识库** | CPU/OOM/DB/Redis/K8s/ES/Prometheus/SkyWalking/网络/MQ 等 |
| 🔍 **快速模式匹配** | 关键词命中知识库，秒级给出排查方向 |
| 💻 **可执行命令** | 每条建议附带 `kubectl`、`redis-cli`、`jstack` 等具体命令 |
| 🌐 **Web 前端** | 纯静态 UI，支持 GitHub Pages 部署 |
| 🐳 **Docker 支持** | 一键容器化部署 |
| 🔌 **开放 API** | RESTful 接口，可集成飞书/钉钉/自建平台 |

---

## 🚀 快速启动

### 方式一：Python 直接运行

```bash
# 1. 安装依赖
pip install fastapi uvicorn openai pydantic httpx

# 2. 配置 API Key
export DEEPSEEK_API_KEY="sk-your-key-here"

# 3. 启动服务
python3 api.py

# 4. 访问
open http://localhost:8766
```

### 方式二：Docker 部署（推荐）

```bash
# 一键启动
DEEPSEEK_API_KEY="sk-your-key-here" docker compose up -d

# 查看日志
docker logs -f aiops-wizard

# 访问
open http://localhost:8766
```

### 方式三：GitHub Pages + Cloudflare Tunnel

前端托管在 GitHub Pages，API 通过 Cloudflare Tunnel 转发到内网服务器：

```bash
# 在服务器上启动 API
DEEPSEEK_API_KEY="sk-your-key-here" docker compose up -d

# 启动 Cloudflare Tunnel
docker run -d --name cloudflared --restart unless-stopped \
  --network host \
  cloudflare/cloudflared tunnel --url http://localhost:8766

# 前端访问
open https://jianminbai.github.io/AIOps-Wizard/
```

> 前端支持通过 URL 参数 `?api=https://your-tunnel-url` 或页面内输入来配置 API 地址。

---

## 📖 API 文档

### `POST /analyze` — 分析故障

**请求：**

```json
{
  "incident": "order-service P95 latency from 200ms to 3s, CPU 98%",
  "model": "deepseek-chat",
  "provider": "deepseek"
}
```

**响应：**

```json
{
  "success": true,
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
    "immediate_actions": ["..."]
  }
}
```

### `GET /health` — 健康检查

```json
{"status": "ok", "kb_count": 30}
```

### `GET /kb` — 知识库列表

```json
{"entries": [
  {"id": "KB001", "title": "CPU 飙高 - 应用线程阻塞", "category": "resource"},
  ...
]}
```

---

## 🧠 知识库（30 类）

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
| KB011 | 依赖 | **Elasticsearch 集群状态 RED/yellow** |
| KB012 | 依赖 | **Prometheus 告警风暴/remote write 失败** |
| KB013 | 应用 | **SkyWalking 调用链断裂** |
| KB014 | 资源 | **Docker overlay2 磁盘暴涨** |
| KB015 | 变更 | **CI/CD 流水线失败** |
| KB016 | 变更 | **SSL/TLS 证书过期** |
| KB017 | 依赖 | **Nginx/API Gateway 502/504** |
| KB018 | 依赖 | **etcd 集群不稳定** |
| KB019 | 资源 | **K8s Node NotReady** |
| KB020 | 资源 | **Pod Pending（资源不足）** |
| KB021 | 变更 | **Pod ImagePullBackOff** |
| KB022 | 资源 | **TCP TIME_WAIT 连接积压** |
| KB023 | 资源 | **文件描述符耗尽** |
| KB024 | 应用 | **Java 线程死锁** |
| KB025 | 资源 | **HPA 无法扩缩容** |
| KB026 | 外部 | **DNS 解析失败** |
| KB027 | 依赖 | **ELK 摄入背压/日志延迟** |
| KB028 | 应用 | **熔断器触发/服务降级** |
| KB029 | 变更 | **配置中心变更未生效** |
| KB030 | 应用 | **APM 探针内存泄漏** |

每条知识库包含 `common_causes`、`check_commands`、`fix_commands`，覆盖从 Elasticsearch 到 K8s 到 CI/CD 的全栈运维场景。

---

## 🏗️ 项目结构

```
AIOps-Wizard/
├── engine.py              # 核心分析引擎（System Prompt + 30 条知识库）
├── api.py                 # FastAPI 后端服务
├── index.html             # 本地前端页面
├── docs/
│   ├── index.html         # GitHub Pages 前端（可配置 API 地址）
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
DEEPSEEK_API_KEY="sk-your-key" docker compose up -d

# 查看运行状态
docker ps
docker logs aiops-wizard

# 验证
curl http://localhost:8766/health

# 停止
docker compose down
```

环境变量：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 必填 |
| `PORT` | 服务端口 | `8766` |

---

## 🌐 GitHub Pages + Cloudflare Tunnel 生产部署

### 1. 服务器端

```bash
# 启动 API
DEEPSEEK_API_KEY="sk-xxx" docker compose up -d

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

- **自动**：GitHub Pages 前端已有默认 API 地址配置
- **手动**：打开前端 → API Endpoint 输入框 → 粘贴 Tunnel URL → Save
- **URL 参数**：`https://jianminbai.github.io/AIOps-Wizard/?api=https://your-tunnel-url`

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
| **前端** | Vanilla HTML/CSS/JS |
| **知识库** | 30 类运维场景，中英双语关键词 |
| **容器化** | Docker + Docker Compose |
| **CI/CD** | GitHub Actions + GitHub Pages |
| **网络** | Cloudflare Tunnel（可选）|

---

## 📋 使用场景

- **值班排障** — 收到告警 → 粘贴到前端 → 秒级获取诊断建议
- **故障复盘** — 结构化分析报告可直接用于事后回顾
- **运维新人培训** — 内置知识库是现成的排障指南
- **飞书/钉钉集成** — 通过 API 对接告警机器人
- **Prometheus Alertmanager Webhook** — 自动分析告警

---

## 🤝 贡献

欢迎提交 Issue 和 PR！如果你有生产环境排障经验，欢迎扩充知识库。

---

## 📄 许可证

MIT License

---

*Made with ❤️ by jianminbai*
