# NanoBob 产品说明手册

> 多模型无限画布 AI 工作平台 - 完整使用与管理指南

---

## 目录

1. [产品概述](#一产品概述)
2. [快速开始](#二快速开始)
3. [用户端功能](#三用户端功能)
4. [管理后台](#四管理后台)
5. [计费系统](#五计费系统)
6. [技术架构](#六技术架构)
7. [部署运维](#七部署运维)
8. [安全机制](#八安全机制)
9. [常见问题](#九常见问题)

---

## 一、产品概述

### 1.1 产品定位

**NanoBob** 是一款可本地部署的多模型 AI 工作平台，核心特性：

- **多模型并发对话**：一次提问同时调用多个 LLM，流式响应并排展示
- **无限画布界面**：可缩放、平移、拖拽的无限空间，节点自由布局
- **分支推演**：从任意模型任意轮回复处继续延展对话
- **多轮讨论**：配置模型间互相质询，形成辩证场
- **联网搜索**：集成 Firecrawl，智能判断搜索需求
- **积分计费**：按 Token 消耗计费，支持用户自定义 API Key
- **管理后台**：用户管理、模型配置、用量统计、审计日志

### 1.2 适用场景

| 场景 | 说明 |
|------|------|
| 模型对比评测 | 同时调用 GPT/Claude/Deepseek/Qwen 等对比输出质量 |
| 专业领域问答 | 多模型交叉验证降低幻觉风险 |
| 研究头脑风暴 | 配置讨论轮次让模型互相启发 |
| 团队协作平台 | 管理员统一配置模型与计费，成员共享画布 |
| API 成本优化 | 用户挂载自有 Key 绕过全局计费 |

---

## 二、快速开始

### 2.1 安装

```bash
# 克隆项目
git clone <repository_url>
cd <project_dir>

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# 安装依赖
pip install -e .
```

### 2.2 首次启动

**步骤 1：访问应用**

```bash
uvicorn app.main:app --reload
```

浏览器打开 http://127.0.0.1:8000

**步骤 2：初始化配置**

首次访问会自动跳转到 `/setup` 页面，填写：

| 字段 | 说明 | 示例 |
|------|------|------|
| API 地址 | OpenAI/Anthropic 兼容端点 | `https://api.openai.com/v1` |
| API 格式 | `openai` 或 `anthropic` | `openai` |
| API Key | 密钥 | `sk-xxxxx` |
| 模型列表 | 名称和 ID | `[{name: "GPT-5", id: "gpt-5"}]` |

保存后系统自动：
1. 创建 `models_setting.json` 配置文件
2. 初始化 SQLite 数据库
3. 创建默认管理员账号 `admin` / `admin`

**步骤 3：登录**

跳转到 `/login`，使用管理员账号登录即可进入画布界面。

### 2.3 配置文件说明

**models_setting.json**（必需，已 git-ignore）：

```json
{
  "base_url": "https://api.openai.com/v1",
  "api_format": "openai",
  "API_key": "sk-xxxxx",
  "models": [
    { "name": "GPT-5", "id": "gpt-5" },
    { "name": "Claude 4", "id": "claude-4-20250514" },
    { "name": "Kimi-K2.5", "id": "kimi-k2.5" }
  ]
}
```

**.env 环境变量**（可选）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MODELS_SETTING_PATH` | `models_setting.json` | 配置文件路径 |
| `LOCAL_DB_PATH` | `data/app.db` | 数据库路径 |
| `REQUEST_LOG_DIR` | `logs` | 日志目录 |
| `FIRECRAWL_API_KEY` | - | Firecrawl 搜索 API Key |
| `FIRECRAWL_COUNTRY` | `CN` | 搜索国家代码 |
| `FIRECRAWL_TIMEOUT_MS` | `45000` | 搜索超时（5-120秒） |

---

## 三、用户端功能

### 3.1 注册与登录

#### 注册流程

**入口**：`/login` 页面点击"注册"

**规则**：
- 用户名：3-32 位，仅支持字母、数字、点、下划线、短横线
- 密码：8-128 位
- 必须填写算术验证码 + 蜜罐字段（防机器人）
- 注册开关可由管理员控制

**注册成功**：
- 自动赠送默认积分（可配置，默认 0）
- 自动跳转登录页

#### 登录流程

**规则**：
- Session Cookie 名称：`canvas_session`
- 有效期：14 天
- 限流：每 IP 每 10 分钟最多 15 次登录

### 3.2 画布操作

#### 平移与缩放

| 操作 | 方式 |
|------|------|
| 平移 | 鼠标左键拖拽空白区域 / 空格键+拖拽 |
| 缩放 | Ctrl+滚轮 / 右下角 +/- 按钮 / 键盘 +/-/0 |
| 小地图 | 右下角显示全局视图，拖拽视口框快速定位 |

#### 节点交互

| 操作 | 说明 |
|------|------|
| 拖拽节点 | 左键拖动节点头部区域 |
| 框选 | 点击空白开始，拖拽矩形框选多个节点 |
| 分支对话 | 点击节点底部"分支"按钮，从该回复继续 |
| 重试模型 | 点击节点底部"重试"，仅重试失败的模型 |

#### 节点类型

| 类型 | 宽度 | 说明 |
|------|------|------|
| 用户消息节点 | 500px | 包含提问文本、搜索结果（如有） |
| 模型回复节点 | 420px | 包含模型名称、轮次标记、流式内容 |
| 结论节点 | 520px | 讨论结束后生成的综合总结 |

### 3.3 对话流程

#### 发送消息

**参数**：

| 参数 | 说明 |
|------|------|
| `message` | 用户提问文本 |
| `models` | 选中的模型 ID 列表 |
| `discussion_rounds` | 讨论轮次（1-4） |
| `search_enabled` | 搜索模式：`auto` / `true` / `false` |
| `think_enabled` | 启用思考模式（如模型支持） |

#### WebSocket 事件流

```
meta           → 连接建立，返回模型列表、余额
user           → 创建用户消息节点
search_started → 开始联网搜索
search_complete → 搜索完成
preprocess_start → 预处理分析开始
preprocess_result → 预处理结果（判断是否需搜索）
search_organized → 搜索结果整理完成
round_start    → 讨论轮次开始
start          → 模型开始生成
delta          → 流式文本增量（多次）
done           → 模型完成
error          → 模型错误
round_complete → 讨论轮次完成
conclusion_start → 开始生成结论
conclusion_done → 结论完成
usage          → 用量和积分消耗
```

#### 分支对话

从任意模型的任意轮回复处继续延展：

**参数**：
- `parent_request_id`：父请求 ID
- `source_model`：来源模型 ID
- `source_round`：来源轮次编号

### 3.4 搜索功能

#### 搜索模式

| 模式 | 说明 |
|------|------|
| `auto` | 预处理模型智能判断是否需要搜索 |
| `true` | 强制开启搜索 |
| `false` | 强制关闭搜索 |

#### 搜索流程

1. **预处理分析**：调用预处理模型判断搜索需求
2. **智能搜索**：多方向搜索 + 深度评估
3. **结果整理**：提取关键信息注入对话上下文

#### 扣费

每次搜索扣除配置的积分（`config_search_points_per_call`）

### 3.5 自定义 API Key

用户可设置每模型的自定义 Key，绕过全局计费：

**接口**：`/api/user/custom-api-key`

**参数**：
```json
{
  "model_id": "gpt-5",
  "api_base_url": "https://custom-api.example.com/v1",
  "api_format": "openai",
  "api_key": "sk-custom-xxx"
}
```

使用自定义 Key 时：
- 不扣除全局积分
- 不记录 token 用量日志

---

## 四、管理后台

### 4.1 管理员登录

**入口**：`/admin`

**Session**：`admin_session`（独立于用户 session）

**默认账号**：`admin` / `admin`（首次启动后建议立即修改密码）

### 4.2 用户管理

#### 功能列表

| 功能 | 接口 | 说明 |
|------|------|------|
| 用户列表 | `GET /api/admin/users` | 查看所有用户信息 |
| 充值积分 | `POST /api/admin/recharge` | 为用户充值积分 |
| 扣减积分 | `POST /api/admin/recharge` | amount 为负数 |
| 重置密码 | `POST /api/admin/reset-password` | 重置指定用户密码 |
| 调整角色 | `POST /api/admin/set-role` | 设置用户为 `admin` 或 `user` |
| 修改密码 | `POST /api/admin/change-password` | 修改自己的密码 |

#### 权限保护

- 至少保留 1 个管理员账号
- 管理员不能移除自己的管理员权限

### 4.3 模型配置

**接口**：`GET/PUT /api/admin/model-config`

**配置项**：

| 字段 | 说明 |
|------|------|
| `api_base_url` | 全局 API 地址 |
| `api_format` | API 格式（`openai` / `anthropic`） |
| `api_key` | 全局 API 密钥 |
| `models` | 模型列表 `[{name, id}]` |
| `firecrawl_api_key` | Firecrawl 搜索 Key |
| `firecrawl_country` | 搜索国家代码 |
| `firecrawl_timeout_ms` | 搜索超时 |
| `preprocess_model` | 预处理模型 ID |
| `user_api_base_url` | 用户自定义 Key 的 API 网关 |
| `user_api_format` | 用户自定义 Key 的 API 格式 |
| `extra_params` | 额外请求参数 |
| `extra_headers` | 额外请求头 |

#### 模型测试

**接口**：`POST /api/admin/model-config/test`

测试模型连通性，限流：12 次/分钟/管理员

### 4.4 定价管理

**接口**：`GET/PUT /api/admin/pricing`

按模型设置 input/output 每 1000 tokens 的积分单价：

```json
{
  "model_id": "gpt-5",
  "input_points_per_1k": 0.5,
  "output_points_per_1k": 1.5
}
```

### 4.5 系统配置

**接口**：`GET/PUT /api/admin/config`

| 配置键 | 说明 |
|------|------|
| `config_default_points` | 注册默认积分 |
| `config_low_balance_threshold` | 低余额阈值（提示充值） |
| `config_allow_registration` | 注册开关（`true`/`false`） |
| `config_search_points_per_call` | 搜索扣费积分 |

### 4.6 审计日志

**接口**：`GET /api/admin/audit-logs`

记录所有管理员操作：
- 充值/扣费
- 角色调整
- 密码重置
- 配置修改

---

## 五、计费系统

### 5.1 积分机制

| 概念 | 说明 |
|------|------|
| 积分余额 | 用户可用积分 |
| 总充值 | 历史充值总额 |
| 总消耗 | 历史消耗总额 |

### 5.2 扣费流程

```
请求开始 → 预扣积分（模型预估 + 搜索扣费）
        → 并发调用模型
        → 记录实际 token 消耗
        → 计算实际费用
        → 返还/扣减差额
        → 发送 usage 事件
```

### 5.3 计费公式

```
实际费用 = (prompt_tokens * input_per_1k / 1000)
         + (completion_tokens * output_per_1k / 1000)
```

### 5.4 用户自定义 Key

用户设置自定义 API Key 后：
- 调用该模型时使用用户 Key
- 不扣除全局积分
- 不记录用量日志

---

## 六、技术架构

### 6.1 前端架构

**技术栈**：原生 JavaScript + Tailwind CSS（无框架）

**模块划分**：

| 文件 | 职责 |
|------|------|
| `app.js` | 入口、事件绑定、消息发送 |
| `canvas.js` | 平移、缩放、小地图、Cluster 边界 |
| `websocket.js` | WebSocket 连接、事件分发 |
| `state.js` | 全局状态、DOM 引用、Map/Set 数据结构 |
| `nodes.js` | 节点渲染、模型轮次、结论节点 |
| `edges.js` | 连线渲染 |
| `clusters.js` | 请求集群管理 |
| `selection.js` | 框选、圈选总结 |
| `sidebar.js` | 侧边栏、画布列表 |

### 6.2 后端架构

**技术栈**：FastAPI + SQLite + asyncio

**服务分层**：

| 层级 | 文件 | 职责 |
|------|------|------|
| 路由层 | `main.py` / `routers/*.py` | HTTP/WebSocket 路由、中间件 |
| 业务层 | `chat_service.py` | 多模型并发、流式处理、讨论轮次 |
| 客户端层 | `llm_client.py` | OpenAI/Anthropic 统一抽象 |
| 数据层 | `database.py` | SQLite 封装、Schema 管理 |
| 安全层 | `security.py` | 限流、安全头、Origin 校验 |
| 认证层 | `auth.py` | 注册、登录、Session 管理 |

### 6.3 数据库 Schema

**版本**：7

**核心表**：

| 表名 | 说明 |
|------|------|
| `users` | 用户账号（含 role 字段） |
| `sessions` | 用户会话 |
| `chat_requests` | 对话请求记录 |
| `model_results` | 模型回复记录 |
| `canvases` | 画布信息 |
| `cluster_positions` | 节点坐标 |
| `user_settings` | 用户自定义 API 配置 |
| `user_balances` | 积分余额 |
| `model_pricing` | 模型定价 |
| `token_usage_logs` | Token 消耗日志 |
| `recharge_logs` | 充值记录 |
| `admin_audit_logs` | 管理员审计日志 |
| `request_summaries` | 请求摘要 |
| `app_meta` | 系统配置 |

---

## 七、部署运维

### 7.1 生产部署

```bash
# 反向代理后启动
uvicorn app.main:app --host 0.0.0.0 --port 8000 --forwarded-allow-ips='*'
```

### 7.2 数据库维护

```bash
# 手动初始化数据库
python -m app.init_db

# 数据库路径
data/app.db（WAL 模式）
```

### 7.3 日志管理

**路径**：`logs/requests-YYYY-MM-DD.jsonl`

**保留策略**：
- 普通日志：30 天（可配置）
- Token 用量日志：90 天（可配置）

### 7.4 监控指标

| 指标 | 说明 |
|------|------|
| 活跃用户数 | `sessions` 表 |
| 积分余额 | `user_balances` 表 |
| Token 消耗 | `token_usage_logs` 表 |
| 模型调用成功率 | `model_results` 表 `status` 字段 |

---

## 八、安全机制

### 8.1 认证安全

| 机制 | 说明 |
|------|------|
| 密码哈希 | PBKDF2-HMAC-SHA256，100,000 iterations |
| Token 哈希 | SHA-256 |
| Session Cookie | HttpOnly, SameSite=Lax, Secure |
| 验证码 | HMAC 签名，答案不嵌入 token，5 分钟有效期 |

### 8.2 限流策略

| 类型 | 限制 |
|------|------|
| 注册 | 10 次 / 10 分钟 / IP |
| 登录 | 15 次 / 10 分钟 / IP |
| WebSocket | 180 次 / 分钟 / 用户 |
| 管理后台测试 | 12 次 / 分钟 / 管理员 |

### 8.3 安全头

| 头 | 值 |
|------|------|
| X-Content-Type-Options | `nosniff` |
| X-Frame-Options | `DENY` |
| Content-Security-Policy | `default-src 'self'` |
| Referrer-Policy | `same-origin` |

### 8.4 Origin 校验

POST 请求和 WebSocket 升级必须同源，防止 CSRF。

---

## 九、常见问题

### Q1：如何添加新模型？

**方法 1：管理后台**

访问 `/admin/dashboard` → 模型配置 → 添加模型 → 测试连通性

**方法 2：修改配置文件**

编辑 `models_setting.json`，添加模型项后重启服务。

### Q2：如何修改管理员密码？

首次登录后立即访问 `/admin/dashboard` → 右上角"修改密码"

### Q3：用户余额不足怎么办？

管理员在后台"用户管理"中为用户充值积分。

用户也可设置自定义 API Key 绕过全局计费。

### Q4：搜索功能不工作？

检查：
1. 管理后台模型配置中是否设置 `firecrawl_api_key`
2. Firecrawl API 是否正常
3. `.env` 中 `FIRECRAWL_TIMEOUT_MS` 是否合理

### Q5：如何备份数据？

备份以下文件/目录：
- `data/app.db`（数据库）
- `models_setting.json`（配置）
- `logs/`（日志）

### Q6：如何查看用量统计？

管理员后台"用量统计"页面，可按用户/模型筛选。

### Q7：对话上下文丢失？

**原因**：当前对话上下文仅保存在 WebSocket 连接内存中

**影响**：刷新页面或重启服务后，无法继续历史对话

**解决**：历史对话仍可查看，但无法继续延展

---

## 附录：快捷键

| 功能 | 快捷键 |
|------|--------|
| 放大 | `+` / `Ctrl+滚轮上` |
| 缩小 | `-` / `Ctrl+滚轮下` |
| 重置缩放 | `0` |
| 平移 | 空格+拖拽 / 左键拖拽空白 |

---

**版本**：v1.0
**更新日期**：2026-04-01
**维护者**：NanoBob Team