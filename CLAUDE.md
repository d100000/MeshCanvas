# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本地 Web 应用，提供无限画布界面，支持多模型并发 LLM 对话。用户发送一条提示词，同时调用多个 OpenAI 兼容模型，流式响应以节点形式渲染在可缩放/平移的画布上。支持对话分支、跨模型讨论轮次和 Firecrawl 网络搜索。

## 环境配置

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**.env**（可选）：
```bash
MODELS_SETTING_PATH=/path/to/config   # 默认 models_setting.json
LOCAL_DB_PATH=data/app.db
REQUEST_LOG_DIR=/path/to/logs
```

## 初始化与启动

```bash
uvicorn app.main:app --reload
# 访问 http://127.0.0.1:8000
```

### 首次启动流程

1. 启动服务后，访问任意页面会自动重定向至 `/setup` 初始化页面
2. 在初始化页面配置：
   - **管理员账号**：用户名和密码（密码至少 8 位）
   - **大模型 API**：API 地址、格式（OpenAI/Anthropic）、API Key、模型列表
   - **联网搜索**（可选）：Firecrawl API Key、国家代码、超时时间
3. 提交后自动创建数据库、管理员账号和全局配置文件 `models_setting.json`
4. 跳转到登录页面，使用刚设置的管理员账号登录

### 全局配置

所有用户共享同一套 API 和搜索配置，存储在 `models_setting.json`（已 git-ignore）：
```json
{
  "base_url": "https://your-openai-compatible-endpoint/v1",
  "api_format": "openai",
  "API_key": "sk-xxxxx",
  "models": [
    { "name": "GPT-5", "id": "gpt-5" },
    { "name": "Kimi-K2.5", "id": "kimi-k2.5" }
  ],
  "firecrawl_api_key": "fc-xxxxx",
  "firecrawl_country": "CN",
  "firecrawl_timeout_ms": 45000
}
```

用户无法自行修改 API/搜索配置，只能修改自己的密码。如需修改全局配置，可直接编辑 `models_setting.json` 并重启服务。

### 重新初始化

```bash
python -m app.init_db --reset   # 删除数据库和配置文件，下次启动重新进入 /setup 流程
python -m app.init_db           # 仅初始化数据库（不重置）
```

## 架构说明

### 请求生命周期

1. 已认证客户端通过 WebSocket 向 `/ws/chat` 发送消息
2. `chat_ws.py` 将请求分发给 `MultiModelChatService`
3. 服务为每个模型创建独立的 `asyncio` 并发任务，使用 `AsyncOpenAI` 调用
4. 每个任务将 delta 流式块通过 `MessageSink` 协议返回
5. 前端 `app/static/app.js` 实时将流式内容渲染为画布节点

### 三种请求类型

- **主对话（Main chat）**：新建对话线程
- **分支对话（Branch chat）**：从某模型某轮回复处分叉，携带 `parent_request_id`、`source_model`、`source_round`
- **重试模型（Retry model）**：重新请求单个失败的模型

### 单次请求状态

`models/chat.py` 中的 `ThreadState` 数据类负责跟踪：每个模型的对话历史、request ID、搜索结果（`SearchBundle`）及分支元数据。讨论轮次机制允许模型在多轮中互相回应对方的输出。每个模型流有 **70 秒超时**，超时视为错误但不影响其他模型。对话历史上限为 **80,000 字符**，超出时自动裁剪（保留系统消息和最新消息）。

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

其他静态文件：`auth.js`（注册/登录 UI）、`setup.js`（首次配置页）、`settings.js`（密码修改页）、`markdown.js`（Markdown 渲染）。

### 后端模块

```
app/
├── core/               # 横切关注点
│   ├── config.py       # 全局配置加载（models_setting.json）、get_global_user_settings()
│   ├── exceptions.py   # AuthError, OriginError, ConfigError
│   ├── middleware.py    # HTTP 日志 + 安全头中间件
│   ├── prompts.py      # 系统提示词常量
│   ├── request_logger.py # JSONL 日志 + 自动清理
│   └── security.py     # RateLimiter, CSP
├── models/             # 数据模型
│   ├── chat.py         # ThreadState 数据类
│   └── search.py       # SearchItem, SearchBundle
├── services/           # 业务逻辑层
│   ├── auth_service.py # 注册/登录/改密
│   ├── chat_service.py # 多模型并发流式（MessageSink 协议）
│   ├── search_service.py # Firecrawl 搜索
│   ├── analysis_service.py # 摘要/对话分析
│   ├── thread_builder.py   # 构建对话线程
│   ├── history_utils.py    # 历史消息处理
│   └── llm_client_factory.py # AsyncOpenAI 客户端工厂
├── repositories/       # 数据访问层
│   ├── base.py         # 连接管理基类
│   ├── user_repo.py    # 用户 CRUD
│   ├── session_repo.py # 会话管理
│   ├── chat_repo.py    # 聊天记录
│   ├── canvas_repo.py  # 画布和节点坐标
│   └── event_repo.py   # 事件日志
├── routers/            # 薄路由层
│   ├── pages.py        # HTML 页面路由
│   ├── auth.py         # /api/auth/* 注册/登录/登出
│   ├── setup.py        # /api/setup 首次初始化
│   ├── settings.py     # /api/settings 查看配置 + 改密
│   ├── canvas.py       # /api/canvases/* CRUD
│   ├── models.py       # /api/models 模型列表
│   ├── analysis.py     # /api/selection-summary, /api/conversation-analysis
│   └── chat_ws.py      # /ws/chat WebSocket + WebSocketSink
├── dependencies.py     # FastAPI 依赖注入
├── database.py         # Facade（组合仓库 + 初始化/迁移）
├── init_db.py          # CLI 初始化/重置工具
└── main.py             # 精简入口（lifespan, 路由, 中间件）
```

### 数据持久化

- **SQLite**（`data/app.db`）：Schema 版本 4（通过 `app_meta` 表管理迁移）；包含表 `users`、`sessions`、`chat_requests`、`model_results`、`request_events`、`app_meta`、`canvases`、`cluster_positions`、`user_settings`
- **全局配置**：`models_setting.json`，所有用户共享，首次 setup 或手动编辑创建
- **JSONL 日志**：每日文件存于 `logs/`，已 git-ignore，自动清理（默认保留 30 天）
- **注意**：当前对话上下文（`ThreadState`）仅保存在 WebSocket 连接内存中，刷新页面或重启服务后会重置

### 认证与安全

- HttpOnly Session Cookie（14 天过期），每次 HTTP 请求和 WebSocket 握手均验证
- 速率限制：每 IP 每 10 分钟最多 10 次注册 / 15 次登录；每用户每分钟最多 180 次 WebSocket 操作
- CSP 限制所有资源来源为 `'self'`；`X-Frame-Options: DENY`
- POST 请求和 WebSocket 升级均进行 Origin 校验
