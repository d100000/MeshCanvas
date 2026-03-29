# MeshCanvas / 无限画布多模型聊天室

`MeshCanvas` 是一个可本地部署的多模型 AI 工作台。用户输入一条问题后，系统会同时调用多个大模型，把流式回答以节点方式渲染到一张可缩放、可平移、可分支的无限画布中，并配套提供联网搜索、讨论轮次、积分计费、用户管理和管理后台能力。

它不是单纯的聊天框，而是一套兼顾“多模型使用体验”和“平台运营能力”的完整产品原型。

## 文档导航

- 完整产品说明书：`docs/product-manual.md`
- 功能介绍书：`docs/feature-guide.md`

## 产品定位

这个产品适合以下场景：

- 同时对比多个模型的回答质量、速度和风格
- 在一张画布上保留问题拆解、分支思路和研究过程
- 构建团队内部的 AI 工作台或轻量多模型平台
- 对用户、积分、模型成本和系统配置进行统一管理

## 核心功能总览

### 用户端

- 多模型并发对话：一次提问并发请求多个模型
- 流式展示：各模型回答实时回传、独立显示
- 无限画布：支持缩放、平移、拖拽、小地图、框选
- 多轮讨论：支持 1 到 4 轮模型讨论
- 联网搜索：支持 `自动` / `开` / `关`
- 思考模式：更偏分析与验证的回答风格
- 分支对话：从任意模型某轮回答继续延展
- 单模型重试：失败时只重试某个模型
- 圈选总结：对选中的多个节点做摘要
- 会话分析与结题：对已有会话生成总结或结论
- 多画布管理：新建、切换、重命名、删除
- 账号与用量：查看余额、统计和用量明细
- 自定义 API Key：按模型配置个人 Key，切换计费模式

### 管理后台

- 管理员登录：独立 `admin_session` 会话
- 用户管理：查看用户、充值、扣费、重置密码、调整角色
- 模型配置：统一配置 API 地址、Key、模型列表和格式
- 预处理模型：用于自动判断是否需要联网搜索
- 用户自定义 Key 网关：单独配置用户侧 API 网关
- Firecrawl 配置：搜索 Key、国家代码、超时
- 模型定价：按输入 / 输出 tokens 配置积分单价
- 用量统计：按用户和模型查看调用与消耗
- 系统配置：默认积分、低余额阈值、搜索扣费、注册开关
- 审计日志：记录关键管理操作

## 典型体验流程

### 普通用户

1. 登录系统后进入无限画布
2. 输入问题，设置讨论轮数、联网模式和思考模式
3. 系统并发调用多个模型，流式返回回答
4. 用户在画布上比较结果、拖动节点、框选内容或建立分支
5. 如有需要，可继续追问、生成圈选总结或查看个人用量

### 管理员

1. 访问 `/admin` 登录管理后台
2. 配置模型网关、模型列表、Firecrawl 和用户侧网关
3. 设置模型定价、默认积分和注册策略
4. 管理用户余额、密码和角色
5. 通过用量统计和审计日志追踪平台运行情况

## 主要页面

- `/`：产品首页（Landing Page）
- `/app`：用户主画布（需登录）
- `/login`：用户登录 / 注册页
- `/setup`：首次安装初始化页
- `/settings`：账号与用量页
- `/admin`：管理后台登录页
- `/admin/dashboard`：管理后台控制台

## 技术架构

### 前端

- 原生 JavaScript 实现无限画布交互
- WebSocket 接收多模型流式事件
- DOM 节点渲染用户消息、模型回答、讨论轮次和状态变化

### 后端

- `FastAPI` 提供页面、REST API 和 WebSocket
- `AsyncOpenAI` 调用 OpenAI 兼容接口
- 支持 `openai` 与 `anthropic` 两种接口格式
- `Firecrawl` 提供联网搜索

### 存储

- `SQLite` 保存用户、会话、对话请求、模型结果、画布、积分、定价、审计日志等数据
- `JSONL` 形式保存每日请求日志

## 项目结构

- `app/main.py`：FastAPI 入口、用户端和管理后台路由、WebSocket 调度
- `app/chat_service.py`：多模型并发、流式处理、讨论轮次、计费记录
- `app/database.py`：SQLite 封装、schema 迁移、业务数据读写
- `app/config.py`：模型配置与环境变量加载
- `app/auth.py`：用户注册、登录、密码哈希、会话逻辑
- `app/security.py`：安全头、Origin 校验和限流
- `app/search_service.py`：Firecrawl 搜索封装
- `app/request_logger.py`：每日 JSONL 请求日志
- `app/bootstrap_admin.py`：默认管理员与初始配置种子逻辑
- `app/static/`：用户端静态资源
- `app/static/admin/`：管理后台静态资源
- `app/templates/admin_login.html`：后台登录模板
- `docs/product-manual.md`：完整产品说明书
- `docs/feature-guide.md`：对外功能介绍书

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. 准备模型配置

如果希望直接通过文件启动，可先复制示例配置：

```bash
cp models_setting.example.json models_setting.json
```

`models_setting.example.json` 示例：

```json
{
  "base_url": "https://your-openai-compatible-endpoint/v1",
  "api_format": "openai",
  "API_key": "YOUR_API_KEY",
  "models": [
    { "name": "GPT-5", "id": "gpt-5" },
    { "name": "Kimi-K2.5", "id": "kimi-k2.5" },
    { "name": "DeepSeek-V3", "id": "deepseek-v3" }
  ]
}
```

如果不提前创建 `models_setting.json`，系统首次访问时会自动跳转到 `/setup` 页面完成初始化。

### 3. 可选配置 `.env`

```bash
cat > .env <<'ENV'
FIRECRAWL_API_KEY=your-firecrawl-key
FIRECRAWL_COUNTRY=CN
FIRECRAWL_TIMEOUT_MS=45000
ENV
```

### 4. 启动服务

```bash
uvicorn app.main:app --reload
```

浏览器打开：

```text
http://127.0.0.1:8000
```

## 首次安装与初始化

当 `models_setting.json` 不存在时：

1. 访问任意页面会跳转到 `/setup`
2. 填写 API 地址、接口格式、API Key 和模型列表
3. 保存后自动初始化数据库
4. 系统自动确保默认管理员账号存在

也可以手动执行：

```bash
python -m app.init_db
```

默认管理员账号通常为：

- 用户名：`admin`
- 密码：`admin`

系统启动时只会确保 `admin` 用户存在且具备管理员角色，不会在每次启动时把已修改过的密码重置回 `admin`。

## 管理后台说明

### 入口

- 登录页：`/admin`
- 控制台：`/admin/dashboard`

### 管理后台可做什么

- 配置全局 API 地址、API Key 和接口格式
- 配置模型列表和预处理模型
- 配置用户自定义 Key 的独立 API 网关
- 配置 Firecrawl
- 管理用户余额和角色
- 设置模型积分价格
- 查看平台用量和积分流水
- 设置默认积分、低余额阈值、搜索扣费和注册开关
- 查看审计日志

### 管理后台登录注意事项

- 请使用同一主机名访问站点，不要混用 `127.0.0.1` 和 `localhost`
- 如果部署在 HTTPS 反向代理之后，需要正确传递 `X-Forwarded-Proto`
- 若使用反向代理，建议启动方式类似：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --forwarded-allow-ips='*'
```

生产环境请将 `*` 改为可信代理 IP。

## 计费与权限机制

### 积分体系

- 用户拥有独立积分余额
- 模型调用按模型定价扣减积分
- 搜索功能可按次扣减积分
- 管理员可充值或扣减

### 模型定价

- 按模型维度配置
- 输入 tokens 和输出 tokens 分别计价
- 用量会被记录到数据库中，支持前台查看和后台统计

### 自定义 API Key

- 用户可在 `/settings` 配置按模型自定义 Key
- 可在“使用积分”和“使用自定义 API Key”之间切换
- 使用自定义 Key 时，可绕开平台的模型积分计费逻辑

## 配置项

默认读取项目根目录下的 `models_setting.json`，也支持以下环境变量：

```bash
export MODELS_SETTING_PATH=/absolute/path/to/models_setting.json
export REQUEST_LOG_DIR=/absolute/path/to/logs
export LOCAL_DB_PATH=/absolute/path/to/app.db
```

常用可选环境变量：

```bash
FIRECRAWL_API_KEY=fc-xxxxx
FIRECRAWL_COUNTRY=CN
FIRECRAWL_TIMEOUT_MS=45000
MODELS_SETTING_PATH=/path/to/models_setting.json
LOCAL_DB_PATH=/path/to/data/app.db
REQUEST_LOG_DIR=/path/to/logs
```

## 数据与日志

### SQLite

数据库默认位于 `data/app.db`，主要保存：

- 用户、角色、会话
- 对话请求、模型结果、请求事件
- 画布、集群位置
- 用户设置
- 积分余额、模型定价、用量日志、充值流水
- 管理操作审计日志
- 请求摘要

### 日志

- 请求日志按天保存到 `logs/requests-YYYY-MM-DD.jsonl`
- 登录失败会在服务端输出 `WARNING` 日志

## 安全机制

- `HttpOnly` Session Cookie
- 密码使用 `PBKDF2-HMAC-SHA256` 哈希存储
- WebSocket 与受保护 API 要求登录态
- POST 与 WebSocket 升级执行 Origin 校验
- 全站 CSP、`X-Frame-Options: DENY` 等安全响应头
- 注册、登录、后台登录和部分 API 具有基础限流

## 当前限制

- 当前没有自动化测试与完整 lint 体系
- 当前对话运行时上下文主要保存在 WebSocket 连接内存中，刷新或重启后不会完整恢复
- SQLite 更适合单机和轻量并发场景，不建议多进程同时高频写同一数据库
- 联网搜索依赖 Firecrawl 的可用性

## 适合二次扩展的方向

- 增加自动化测试和 CI
- 增加更多模型供应商适配
- 引入更强的会话恢复能力
- 支持团队、组织和更细粒度权限
- 增加导出、分享和协作能力
