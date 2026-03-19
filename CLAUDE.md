# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本地 Web 应用，提供无限画布界面，支持多模型并发 LLM 对话。用户发送一条提示词，同时调用多个 OpenAI 兼容模型，流式响应以节点形式渲染在可缩放/平移的画布上。支持对话分支、跨模型讨论轮次和 Firecrawl 网络搜索。

## 环境配置

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 复制并编辑模型配置文件，填入 API Key 和 Base URL
cp models_setting.example.json models_setting.json
```

**models_setting.json**（必须，已 git-ignore）：
```json
{
  "models": ["model-name-1", "model-name-2"],
  "API_key": "sk-xxxxx",
  "base_url": "https://your-openai-compatible-endpoint/v1"
}
```

**.env**（可选）：
```bash
FIRECRAWL_API_KEY=fc-xxxxx
FIRECRAWL_COUNTRY=CN
FIRECRAWL_TIMEOUT_MS=45000   # 搜索超时（5000-120000ms，默认 45000）
MODELS_SETTING_PATH=/path/to/config
LOCAL_DB_PATH=data/app.db
REQUEST_LOG_DIR=/path/to/logs
```

## 启动

```bash
uvicorn app.main:app --reload
# 访问 http://127.0.0.1:8000
```

数据库在首次启动时自动初始化。手动初始化：`python -m app.init_db`

## 架构说明

### 请求生命周期

1. 已认证客户端通过 WebSocket 向 `/ws/chat` 发送消息
2. `app/main.py` 将请求分发给 `app/chat_service.py` 中的 `MultiModelChatService`
3. 服务为每个模型创建独立的 `asyncio` 并发任务，使用 `AsyncOpenAI` 调用
4. 每个任务将 delta 流式块通过同一 WebSocket 连接返回
5. 前端 `app/static/app.js` 实时将流式内容渲染为画布节点

### 三种请求类型

- **主对话（Main chat）**：新建对话线程
- **分支对话（Branch chat）**：从某模型某轮回复处分叉，携带 `parent_request_id`、`source_model`、`source_round`
- **重试模型（Retry model）**：重新请求单个失败的模型

### 单次请求状态

`chat_service.py` 中的 `ThreadState` 数据类负责跟踪：每个模型的对话历史、request ID、搜索结果（`SearchBundle`）及分支元数据。讨论轮次机制允许模型在多轮中互相回应对方的输出。每个模型流有 **70 秒超时**，超时视为错误但不影响其他模型。

### WebSocket 消息协议

客户端发送 JSON，`type` 字段区分消息类型：
- `chat`：主对话（含 `models`、`message`、`search_enabled`、`think_enabled`、`discussion_rounds`）
- `branch_chat`：分支对话（额外含 `parent_request_id`、`source_model`、`source_round`）
- `retry_model`：重试单个模型（含 `request_id`、`model`）
- `save_cluster_position`：保存节点坐标

服务端推送 JSON 事件流，`event` 字段：`stream_start`、`stream_delta`、`stream_end`、`stream_error`、`search_result`、`round_start`。

### 无测试 / 无 Lint 配置

当前无测试文件，无 ruff/flake8/mypy 配置。需调试时直接运行服务并通过浏览器或 WebSocket 客户端验证。

### 前端画布（`app/static/app.js`）

纯原生 JS 无限画布，无任何框架。核心概念：
- **节点（Nodes）**：用户消息节点（约 500px 宽）和模型回复节点（约 420px 宽）
- **集群（Clusters）**：将同一请求的节点归组
- 平移（空格键 + 拖拽）、缩放（滚轮）、节点拖动、框选、小地图导航
- WebSocket 客户端管理流式状态，将 delta 文本追加到节点

### 后端模块

| 文件 | 职责 |
|------|------|
| `app/main.py` | FastAPI 应用、HTTP 路由、WebSocket 分发 |
| `app/chat_service.py` | 并发模型调用、流式处理、讨论轮次 |
| `app/database.py` | SQLite 封装（WAL 模式，通过 `app_meta` 进行 schema 版本管理） |
| `app/auth.py` | 注册、登录、PBKDF2-HMAC-SHA256 密码哈希 |
| `app/security.py` | 安全头中间件、按 IP/用户的速率限制 |
| `app/config.py` | 加载 `models_setting.json` 和环境变量为类型化配置 |
| `app/search_service.py` | Firecrawl API 集成 |
| `app/request_logger.py` | 每日 JSONL 日志，路径为 `logs/requests-YYYY-MM-DD.jsonl` |

### 数据持久化

- **SQLite**（`data/app.db`）：包含表 `users`、`sessions`、`chat_requests`、`model_results`、`request_events`、`app_meta`、`canvases`、`cluster_positions`（画布列表及节点坐标）
- **JSONL 日志**：每日文件存于 `logs/`，已 git-ignore
- **注意**：当前对话上下文（`ThreadState`）仅保存在 WebSocket 连接内存中，刷新页面或重启服务后会重置

### 认证与安全

- HttpOnly Session Cookie（14 天过期），每次 HTTP 请求和 WebSocket 握手均验证
- 速率限制：每 IP 每 10 分钟最多 10 次注册 / 15 次登录；每用户每分钟最多 180 次 WebSocket 操作
- CSP 限制所有资源来源为 `'self'`；`X-Frame-Options: DENY`
- POST 请求和 WebSocket 升级均进行 Origin 校验
