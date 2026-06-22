# AIOps-Wizard 🧙‍♂️🔧

**AI-powered Ops Fault Analysis Assistant — 智能运维故障分析助手**

一键分析告警信息，给出故障根因分析、影响范围、排查步骤和修复建议。基于 DeepSeek + 运维知识库，专为生产环境运维人员设计。

## 技术栈

- **引擎**: Python + LangChain + DeepSeek API
- **知识库**: 10 类常见故障（CPU/OOM/DB/Redis/Pod/Disk/Network/MQ/SQL/配置）
- **API**: FastAPI
- **前端**: 纯静态 HTML + CSS + JS
- **运行**: 单进程，零依赖安装

## 快速启动

```bash
# 1. 配置 API Key
echo "DEEPSEEK_API_KEY=your_key_here" > .env

# 2. 启动服务
python3 api.py

# 3. 打开浏览器访问
open http://127.0.0.1:8766
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/analyze` | 分析告警/故障描述 |
| GET | `/health` | 健康检查 |
| GET | `/kb` | 查看知识库 |

### 示例

```bash
curl -X POST http://127.0.0.1:8766/analyze \
  -H "Content-Type: application/json" \
  -d '{"alert": "order-service P95 latency 200ms to 3s, CPU 98%"}'
```

## 项目结构

```
├── engine.py      # 核心分析引擎（System Prompt + 知识库）
├── api.py         # FastAPI 后端服务
├── index.html     # Web 前端页面
└── .env           # API Key 配置（请自行创建）
```

## 使用场景

- 钉钉/飞书告警快速分析
- 值班排障辅助决策
- 故障复盘知识沉淀
- 运维新人培训工具

## 许可证

MIT
