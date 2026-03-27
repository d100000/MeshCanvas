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
- `app/init_db.py`：手动初始化本地数据库，并创建默认管理员 `admin` / `admin`
- `app/bootstrap_admin.py`：确保管理员账号存在（服务启动与 `init_db` 共用）
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
# 依赖中含 python-multipart（multipart 上传等场景）；管理后台默认表单为 urlencoded，由服务端用标准库解析，一般不再因缺 multipart 而无法登录
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

上述命令会初始化数据库并写入默认管理员：**用户名 `admin`，密码 `admin`**（管理后台 `/admin` 与用户端登录共用该账号；生产环境请尽快修改密码）。服务启动时也会自动执行同样的检查：**仅保证 `admin` 用户存在且具备管理员角色，不会把已修改过的密码改回 `admin`**。

启动服务：

```bash
uvicorn app.main:app --reload
```

浏览器打开：

```text
http://127.0.0.1:8000
```

先访问 `http://127.0.0.1:8000`，未登录时会进入登录页。首次可直接注册账号。

### 管理后台 `/admin` 登录说明

- 默认管理员账号为 **`admin` / `admin`**（注意是 **admin** 不是 amdin；服务启动或执行 `python -m app.init_db` 后会自动保证该账号存在且具备管理员角色）。
- 打开 `/admin` 后须在页面里输入密码并点击登录；**不会在网址里加 `?password=...` 就自动登录**（也不会读取该参数，避免密码进浏览器历史与服务器日志）。
- 登录页的 **`?error=...` 提示由服务端写入 HTML**（模板在 `app/templates/admin_login.html`，交互脚本为 `/static/admin/login.js`）。全站 CSP 为 `script-src 'self'`，**内联 `<script>` 不会执行**；若只靠内联 JS 解析网址参数，会出现「明明重定向带 error 却没有任何红字」。
- 请**始终用同一主机名**访问站点（例如全程使用 `http://127.0.0.1:8000` 或全程使用 `http://localhost:8000`）。混用会导致 `admin_session` Cookie 无法带上，表现为登录接口成功但一进控制台又回到登录页。
- 若提示「该账号没有管理员权限」，说明当前用户名在库里不是 `admin` 角色；可重启一次服务（仅会把 **用户名为 `admin` 的账号** 的 `role` 纠正为管理员，**不重置密码**），或由已有管理员在后台调整角色。后台禁止取消自己的管理员身份，且须至少保留一名管理员。
- 若页面提示 **session / Cookie 未生效**：常见于 **HTTPS + 反向代理**（Nginx 等）后面跑 uvicorn 时，浏览器需要带 **`Secure`** 的会话 Cookie，但应用若看不到 HTTPS 会误设 `Secure=false` 导致 Cookie 被丢弃。请让反代传入 **`X-Forwarded-Proto: https`**，并用例如：  
  `uvicorn app.main:app --host 0.0.0.0 --port 8000 --forwarded-allow-ips='*'`  
  （生产请把 `*` 换成可信代理 IP。）
- 若提示 **「无法解析登录表单」**（`error=form`）：多为请求体异常或 **multipart** 场景缺依赖；请 `pip install -e .` 后重启，并排除改写 POST 的扩展/代理。（默认 **urlencoded** 登录已不依赖 python-multipart。）
- 若提示 **「服务暂时异常」**（`error=server`）：请看运行 `uvicorn` 的终端里 **`admin session-login`** 相关堆栈。
- 若提示 **数据库无法写入**（`error=db`）：多为 **SQLite 无法写入**。请检查：① 环境变量 **`LOCAL_DB_PATH`**（默认 `data/app.db`）所在目录是否存在、是否可写；② 数据库文件是否被设为只读；③ 磁盘是否已满；④ **不要**用多个 worker / 多进程同时打开同一库文件（SQLite 并发写入能力有限）。处理完后重启服务再试。

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

SQLite 数据库路径（可选，默认项目下 `data/app.db`）：

```bash
export LOCAL_DB_PATH=/absolute/path/to/app.db
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
- 用户端 **`POST /api/auth/login`**、管理后台 **`POST /admin/session-login`** 与 **`POST /api/admin/login`** 在失败时会在 uvicorn 控制台输出 **`WARNING`** 行，格式为 `login_failed route=... client=... username=... reason=...`（**不记录密码**）。`reason` 含 `rate_limited`、`origin_denied`（仅用户端）、或认证错误文案等。

## 安全说明

- 登录态使用 `HttpOnly` Session Cookie，浏览器脚本无法直接读取。
- 密码使用 `PBKDF2-HMAC-SHA256` 做本地哈希存储，不保存明文密码。
- WebSocket 与受保护 API 都要求已登录会话。
- 已加入同源校验、基础限流和常见安全响应头。
- 静态资源公开可访问，但核心数据接口与对话 WebSocket 已受登录态保护。
