# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本地 Web 应用，提供无限画布界面，支持多模型并发 LLM 对话。用户发送一条提示词，同时调用多个 OpenAI 兼容模型，流式响应以节点形式渲染在可缩放/平移的画布上。支持对话分支、跨模型讨论轮次、Firecrawl 网络搜索，以及完整的管理后台（用户管理、计费、模型配置、审计日志）。

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

**首次运行流程**：若 `models_setting.json` 不存在，应用会将用户重定向到 `/setup` 页面完成初始配置（API 地址、Key、模型列表），保存后自动初始化数据库并创建默认管理员 `admin` / `admin`。

手动初始化数据库：`python -m app.init_db`（会确保默认管理员存在；`uvicorn` 启动时也执行相同逻辑）。已存在的 `admin` 仅纠正 `role`，**不会**在每次启动时重置密码。

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
| `app/main.py` | FastAPI 应用、HTTP 路由（含管理后台 API）、WebSocket 分发 |
| `app/chat_service.py` | 并发模型调用、流式处理、讨论轮次、Token 用量记录 |
| `app/database.py` | SQLite 封装（WAL 模式，schema 版本管理至 v7） |
| `app/auth.py` | 注册、登录、PBKDF2-HMAC-SHA256 密码哈希 |
| `app/security.py` | 安全头中间件、按 IP/用户的速率限制 |
| `app/config.py` | 加载 `models_setting.json` 和环境变量，支持 `is_configured()` / `save_settings()` |
| `app/search_service.py` | Firecrawl API 集成 |
| `app/request_logger.py` | 每日 JSONL 日志，路径为 `logs/requests-YYYY-MM-DD.jsonl` |
| `app/bootstrap_admin.py` | 确保默认管理员存在，种子化管理员设置 |

### 管理后台

独立的管理后台系统，使用单独的 `admin_session` Cookie 鉴权：

- **入口**：`/admin`（登录页）→ `/admin/dashboard`（控制面板）
- **前端**：`app/static/admin/` 下的 `dashboard.html`、`admin.js`、`admin.css`、`login.js`
- **登录模板**：`app/templates/admin_login.html`

管理后台 API（`/api/admin/*`）功能：
- **用户管理**：列表、充值/扣费、重置密码、角色变更
- **模型配置**：全局 API 地址/Key/模型列表/Firecrawl 设置、API 连通性测试
- **定价管理**：按模型设置 input/output 每千 token 积分单价
- **用量统计**：按用户/模型筛选的 Token 消耗报表
- **系统配置**：默认积分、注册开关、低余额阈值、搜索积分消耗
- **审计日志**：所有管理操作记录（`admin_audit_logs` 表）

### 计费系统

- **积分制**：用户有积分余额（`user_balances` 表），管理员通过充值/扣费管理
- **模型定价**：`model_pricing` 表按 input/output tokens 分别定价
- **用量记录**：每次请求的 token 消耗记入 `token_usage_logs`
- **自定义 API Key**：用户可通过 `/api/user/custom-api-key` 设置每模型的自定义 Key，绕过全局计费
- **系统配置键**：`config_default_points`、`config_low_balance_threshold`、`config_allow_registration`、`config_search_points_per_call`（存储在 `app_meta` 表）

### 数据持久化

- **SQLite**（`data/app.db`，schema v7）：
  - 核心表：`users`（含 `role` 字段）、`sessions`、`chat_requests`、`model_results`、`request_events`、`app_meta`、`canvases`、`cluster_positions`
  - 计费表：`user_balances`、`model_pricing`、`token_usage_logs`、`recharge_logs`
  - 配置表：`user_settings`（每用户 API 配置覆盖）
  - 管理表：`admin_audit_logs`
  - 摘要表：`request_summaries`
- **JSONL 日志**：每日文件存于 `logs/`，已 git-ignore
- **注意**：当前对话上下文（`ThreadState`）仅保存在 WebSocket 连接内存中，刷新页面或重启服务后会重置

### 认证与安全

- **双会话体系**：用户端 Session Cookie + 管理后台 `admin_session` Cookie（均 HttpOnly，14 天过期）
- 登录失败（用户端 `/api/auth/login`、管理后台 `/admin/session-login` 与 `/api/admin/login`）写入标准日志 **`WARNING`**：`login_failed route=… client=… username=… reason=…`（不记录密码）
- 速率限制：每 IP 每 10 分钟最多 10 次注册 / 15 次登录；每用户每分钟最多 180 次 WebSocket 操作
- CSP 限制所有资源来源为 `'self'`；`X-Frame-Options: DENY`
- POST 请求和 WebSocket 升级均进行 Origin 校验
- 管理后台 API Key 显示时自动脱敏（仅展示前 8 字符）
- 角色保护：至少保留 1 个管理员，不可移除自身管理员权限
