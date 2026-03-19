# 无限画布多模型聊天室

这是一个使用 Python 构建的本地网页端项目：后端读取 `models_setting.json`，按 OpenAI 兼容接口并发调用多个大模型，并把每个模型的流式输出实时推送到网页端。

## 当前能力

- 账号注册 / 登录，基于 HttpOnly Session Cookie 保护整站访问
- 安全头、同源校验、认证限流、WebSocket 鉴权
- 同一次提问并发调用多个模型
- 无限画布：拖动画布平移、滚轮缩放、节点自由拖拽
- 每个模型独立卡片展示多轮发言，按 Tab 切换轮次
- 模型流式输出时自动切到最新 Tab，并自动滚到底部
- Firecrawl 联网搜索能力，默认可提供给所有模型
- `联网` / `思考` 开关
- 卡片内 `分支` 功能：从某个模型当前轮继续下一步对话
- 服务端统一转发流式增量
- 按天记录请求日志，保存到 `logs/requests-YYYY-MM-DD.jsonl`
- 本地 SQLite 数据库：自动初始化并保存请求、模型结果、搜索事件

## 项目结构

- `app/main.py`：FastAPI 入口、WebSocket 调度、主线程与分支线程管理
- `app/chat_service.py`：多模型并发流式调用逻辑
- `app/config.py`：读取 `models_setting.json` 与环境变量
- `app/search_service.py`：Firecrawl 搜索服务封装
- `app/request_logger.py`：按天写入 JSONL 请求日志
- `app/database.py`：本地 SQLite 数据库与自动建表
- `app/auth.py`：用户注册、登录、密码哈希、会话管理
- `app/security.py`：安全头与基础限流
- `app/init_db.py`：手动初始化本地数据库
- `app/static/index.html`：无限画布页面入口
- `app/static/login.html`：登录 / 注册页面
- `app/static/auth.js`：登录页交互逻辑
- `app/static/app.js`：画布交互、节点拖拽、分支、流式渲染
- `app/static/markdown.js`：Markdown 渲染
- `app/static/styles.css`：界面样式
- `models_setting.example.json`：模型配置示例

## 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp models_setting.example.json models_setting.json
# 编辑 models_setting.json，填入真实模型 API Key
```

如果要启用 Firecrawl 搜索，在项目根目录创建 `.env`：

```bash
cat > .env <<'ENV'
FIRECRAWL_API_KEY=your-firecrawl-key
FIRECRAWL_COUNTRY=CN
FIRECRAWL_TIMEOUT_MS=45000
ENV
```

首次安装后也可以手动初始化数据库：

```bash
python -m app.init_db
```

启动服务：

```bash
uvicorn app.main:app --reload
```

浏览器打开：

```text
http://127.0.0.1:8000
```

先访问 `http://127.0.0.1:8000`，未登录时会进入登录页。首次可直接注册账号。

## 配置

默认读取项目根目录下的 `models_setting.json`。

也可以通过环境变量指定配置文件路径：

```bash
export MODELS_SETTING_PATH=/absolute/path/to/models_setting.json
```

日志目录默认是项目根目录下的 `logs/`，也可以通过环境变量指定：

```bash
export REQUEST_LOG_DIR=/absolute/path/to/logs
```

## 配置文件格式

```json
{
  "models": ["GLM-5", "Kimi-K2.5"],
  "API_key": "your-api-key",
  "base_url": "https://your-openai-compatible-endpoint/v1"
}
```

## 说明

- 模型 API Key 仅在服务端使用，不会下发到浏览器。
- 首次启动服务时会自动初始化本地数据库；如果数据库文件不存在会自动创建。
- Firecrawl Key 建议放在 `.env` 中，不要提交到仓库。
- 当前会话上下文保存在 WebSocket 连接内存中，刷新页面后会重置。
- 如果某个模型报错，其它模型会继续返回结果。
- 日志写入失败不会影响主流程。

## 安全说明

- 登录态使用 `HttpOnly` Session Cookie，浏览器脚本无法直接读取。
- 密码使用 `PBKDF2-HMAC-SHA256` 做本地哈希存储，不保存明文密码。
- WebSocket 与受保护 API 都要求已登录会话。
- 已加入同源校验、基础限流和常见安全响应头。
- 静态资源公开可访问，但核心数据接口与对话 WebSocket 已受登录态保护。
