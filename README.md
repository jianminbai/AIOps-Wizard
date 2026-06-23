# AIOps-Wizard 🧙‍♂️🔧

**告警 → 上下文收集 → 结构化 RCA 报告**

输入一条告警，自动从 K8s、Elasticsearch、Prometheus、OpenTelemetry 收集上下文，交给 DeepSeek 分析，输出一份专业级 Root Cause Analysis 报告。

🌐 **在线体验**: https://jianminbai.github.io/AIOps-Wizard/

---

## 📸 界面展示

### RCA 分析 — 输入告警，自动出报告

![主界面](docs/images/main-interface.jpg)

切换至 "🕵️ RCA 分析" 模式，输入告警 → 点 "执行 RCA 分析" → 自动收集 4 个数据源 → 输出完整报告。

### 报告内容

| 模块 | 内容 |
| --- | --- |
| ⏱️ **时间线** | 按时间顺序还原故障演进过程 |
| 🔎 **根因分析** | 置信度标注 + 详细分析 + 证据链 |
| 💥 **影响范围** | 受影响服务、持续时间、错误率、延迟变化 |
| ⚡ **修复动作** | 按优先级排序，每条附带可复制命令 |
| 🧩 **关联因素** | 配置/代码/基础设施/流程/外部因素 |
| 🛡️ **改进措施** | 防止复发的长期改进建议 |

---

## 🎯 杀手级功能：RCA Agent

这是 AIOps-Wizard 的核心 —— 不是聊天机器人，而是一个**自动根因分析代理**。

```
告警输入 "CPU > 90% on payment-api"
       │
       ▼
┌─────────────────────────────────────┐
│  Context Collector                  │
│                                     │
│  ☸️ K8sPlugin                      │ → kubectl get pods / events / top
│  📄 ElasticPlugin                  │ → 错误日志 / Top Error / 异常 Trace
│  📊 PrometheusPlugin               │ → CPU / Memory / Latency
│  🔗 OTelPlugin                     │ → Trace / Span 数据
│                                     │
│  每个插件先尝试真实数据源命令         │
│  不可用时自动降级为 sample 数据      │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│  DeepSeek LLM 深度分析              │
│                                     │
│  Context Package (结构化上下文)      │
│  → 时间线重建                       │
│  → 根因推理 + 证据匹配             │
│  → 影响评估                         │
│  → 修复建议生成                     │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│  专业级 RCA 报告                    │
│                                     │
│  一份可以直接转发给老板的报告        │
│  或直接用于故障复盘的事后分析        │
└─────────────────────────────────────┘
```

### 和普通 AI Chat 的区别

| 对比项 | 普通 AI Chat | AIOps-Wizard RCA |
| --- | --- | --- |
| **输入** | 用户自己拼 prompt | 一条告警就够了 |
| **上下文** | 用户手动复制粘贴 | 自动从 K8s/ES/Prometheus/Otel 收集 |
| **输出** | 自由文本 | 结构化 RCA 报告（时间线/根因/影响/修复） |
| **可执行性** | 需要自己翻译成命令 | 命令直接复制执行 |
| **证据** | 全靠 LLM 知识 | 每条结论有数据源证据支撑 |
| **故障案例库** | 无（每次从零分析） | 可沉淀为历史案例（coming soon） |

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                        用户访问层                            │
│  ┌────────────────┐   ┌────────────────┐   ┌───────────┐  │
│  │ GitHub Pages   │   │  本地直接访问   │   │ 飞书机器人  │  │
│  │ (RCA 前端)    │   │  :8766         │   │ (coming)   │  │
│  └───────┬────────┘   └───────┬────────┘   └─────┬─────┘  │
│          │                    │                   │         │
│          └──────────┬─────────┘───────────────────┘         │
│                     ▼                                       │
│          ┌──────────────────────┐                           │
│          │ Cloudflare Tunnel   │   (可选)                    │
│          └──────────┬───────────┘                           │
└─────────────────────┼───────────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────────┐
│              FastAPI 后端 (:8766)                            │
│                                                              │
│  ┌──────────┐   ┌───────────┐   ┌────────────┐            │
│  │ 安全中间件│──▶│ 路由分发   │──▶│ /rca       │            │
│  │ API Key  │   │           │   │ /analyze   │            │
│  │ 限流/配额 │   │           │   │ /health    │            │
│  └──────────┘   └───────────┘   └──────┬─────┘            │
│                                        ▼                    │
│  ┌─────────────────────────────────────────────────────┐   │
│  │           RCA Pipeline                               │   │
│  │                                                      │   │
│  │  ┌──────────────────────────────────────────────┐   │   │
│  │  │  Context Collector (context_collector.py)     │   │   │
│  │  │                                                │   │   │
│  │  │  K8sPlugin → kubectl get pods/events/top      │   │   │
│  │  │  ElasticPlugin → curl ES /_search             │   │   │
│  │  │  PrometheusPlugin → curl Prom API             │   │   │
│  │  │  OtelPlugin → OpenTelemetry trace/spans       │   │   │
│  │  │                                                │   │   │
│  │  │  ↓ 降级: 真实源不可用时自动使用 sample 数据    │   │   │
│  │  └───────────────────┬──────────────────────────┘   │   │
│  │                      ▼                              │   │
│  │  ┌──────────────────────────────────────────────┐   │   │
│  │  │  RCA Agent (rca_agent.py)                     │   │   │
│  │  │  Context Package → LLM → 结构化报告           │   │   │
│  │  └──────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────┘   │
│                                        │                    │
│                                        ▼                    │
│  ┌─────────────────────────────────────────────────────┐   │
│  │            DeepSeek API                             │   │
│  │  深度分析、根因推理、修复方案生成                      │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │            知识库 (32条 YAML)                        │   │
│  │  可选增强：RCA 可额外匹配历史知识库                   │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 快速启动

```bash
# 安装依赖
pip install fastapi uvicorn openai pydantic httpx pyyaml

# 设置环境变量
export DEEPSEEK_API_KEY="sk-x...port AIOPS_API_KEY=*** AIOPS_RATE_LIMIT=5
export AIOPS_LLM_PER_IP_DAILY=30
export AIOPS_LLM_GLOBAL_DAILY=500

# 启动
python3 api.py

# 访问
open http://localhost:8766
```

### Docker 部署

```bash
export DEEPSEEK_API_KEY="sk-x...port AIOPS_API_KEY=*** AIOPS_RATE_LIMIT=5
export AIOPS_LLM_PER_IP_DAILY=30
export AIOPS_LLM_GLOBAL_DAILY=500

docker compose build --no-cache && docker compose up -d
```

---

## 📖 API 文档

### `POST /rca` — 根因分析（核心功能）

**请求：**

```json
{
  "alert": "CPU > 90% on payment-api, P99 latency 3s, 503 errors",
  "service": "payment-api",
  "namespace": "default"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `alert` | string | ✅ | 告警内容 |
| `service` | string | ❌ | 服务名称（影响 K8s/ES/Prometheus 查询） |
| `namespace` | string | ❌ | K8s Namespace（默认 default） |

**响应：**

```json
{
  "success": true,
  "alert": "CPU > 90% on payment-api...",
  "service": "payment-api",
  "report": {
    "title": "payment-api CPU 过载导致服务不可用",
    "severity": "P1",
    "summary": "Prometheus Collector 证书过期 → HPA 无法获取指标 → 未能扩容 → CPU 持续升高至 95% → 503 错误",
    "timeline": [
      {"time": "14:01", "event": "CPU 开始从 35% 上升", "source": "Prometheus"},
      {"time": "14:03", "event": "HPA 扩容失败：missing metrics", "source": "K8s Events"},
      {"time": "14:05", "event": "错误日志激增：503", "source": "Elasticsearch"}
    ],
    "root_cause": {
      "summary": "Prometheus Collector 证书过期导致指标采集中断",
      "confidence": "high",
      "evidence": ["K8s Event: FailedScaleUp", "Log: connection refused"]
    },
    "fix_actions": [
      {"priority": 1, "action": "重启 Pod", "command": "kubectl rollout restart deployment/payment-api"},
      {"priority": 2, "action": "更新证书", "command": "kubectl delete secret prometheus-cert..."}
    ],
    "prevention": ["证书到期前 30 天告警", "HPA fallback 机制"]
  },
  "context_summary": {
    "kubernetes": "sample",
    "elasticsearch": "sample",
    "prometheus": "sample",
    "otel": "sample"
  }
}
```

### `POST /analyze` — 快速分析（原有）

快速故障分析，适合不需要全量上下文的场景。

### `GET /health` — 健康检查 + 配额

---

## 🔒 安全防护

三层防护防止 API 被滥用：

| 防护层 | 配置 | 默认值 |
| --- | --- | --- |
| API Key | `AIOPS_API_KEY` | 不设置则公开 |
| 频率限制 | `AIOPS_RATE_LIMIT` | 5次/分钟/IP |
| IP 日额度 | `AIOPS_LLM_PER_IP_DAILY` | 30次/天 |
| 全局日额度 | `AIOPS_LLM_GLOBAL_DAILY` | 500次/天 |

---

## 🧠 32 类知识库

内置 32 条运维知识库（YAML），覆盖全栈运维场景。RCA 分析时自动匹配知识库增强推理。

---

## 🗂️ 项目结构

```
AIOps-Wizard/
├── api.py                 # FastAPI 后端（RCA + 分析 + 安全）
├── context_collector.py   # 上下文收集引擎（K8s/ES/Prometheus/Otel）
├── rca_agent.py           # RCA 分析代理 + 系统提示词
├── engine.py              # 原有分析引擎
├── kb_loader.py           # YAML 知识库加载器
├── kb/                    # 32 条运维知识库
├── index.html             # 本地前端
├── docs/
│   ├── index.html         # GitHub Pages 前端（RCA 模式 + 快速分析）
│   └── images/
├── Dockerfile
└── docker-compose.yml
```

---

## 🛠️ 技术栈

| 层级 | 技术 |
| --- | --- |
| **AI 引擎** | DeepSeek API |
| **后端** | FastAPI + Uvicorn |
| **上下文收集** | kubectl / Elasticsearch / Prometheus / OpenTelemetry |
| **前端** | Vanilla HTML/CSS/JS + GitHub Pages |
| **安全** | API Key + 频率限制 + 双维度日额度 |
| **容器化** | Docker + Docker Compose |

---

## 📋 路线图

- [x] RCA Agent（告警 → 上下文 → 报告）
- [x] Context Collector（K8s/ES/Prometheus/Otel 插件）
- [x] 结构化 RCA 报告（时间线/根因/影响/修复）
- [x] 32 条知识库 + 置信度路由
- [x] 三层安全防护
- [ ] **飞书机器人集成**（@AIOps-Wizard 分析）
- [ ] 故障案例库（向量检索 + 历史匹配）
- [ ] 真实数据源接入（不依赖 sample）
- [ ] Demo GIF + Benchmark

---

## 📄 许可证

MIT License

*Made with ❤️ by jianminbai — 把排障经验变成产品*
