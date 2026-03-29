from __future__ import annotations

import asyncio
import json as _json
import logging
import math
import os
import secrets
import sqlite3
from html import escape
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, perf_counter
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from app.auth import AuthError, AuthManager, SESSION_COOKIE_NAME, ADMIN_SESSION_COOKIE_NAME, SESSION_DAYS
from app.bootstrap_admin import ensure_default_admin_user
from app.chat_service import MultiModelChatService
from app.llm_client import create_llm_client
from app.config import get_settings, is_configured, save_settings
from app.database import LocalDatabase
from app.request_logger import RequestLogger
from app.search_service import FirecrawlSearchService, SearchBundle, SearchItem
from app.captcha import generate as captcha_generate, verify as captcha_verify, check_honeypot
from app.security import RateLimiter, build_security_headers, LANDING_CSP

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ADMIN_LOGIN_TEMPLATE_PATH = BASE_DIR / "templates" / "admin_login.html"

_ADMIN_LOGIN_ERR_TEXT: dict[str, str] = {
    "badcreds": "用户名或密码错误。",
    "forbidden": "该账号没有管理员权限，请使用具备 admin 角色的账号。",
    "invalid": "用户名或密码格式不符合要求。",
    "rate": "登录尝试过于频繁，请稍后再试。",
    "captcha": "验证码错误或已过期，请重新计算后提交。",
    "origin": "非法来源，请从本站页面发起登录请求。",
    "form": (
        "无法解析登录表单（请求体损坏、使用了 multipart 上传但未安装 python-multipart、或被代理/扩展改写）。"
        "请执行 pip install -e . 后重启 uvicorn；默认 urlencoded 登录已不再依赖 multipart。"
    ),
    "server": (
        "服务暂时异常（非表单、非数据库类错误）。请查看运行 uvicorn 的终端里带 "
        "「admin session-login」的报错堆栈。"
    ),
    "db": (
        "数据库无法完成写入（常见于 SQLite 文件只读、目录无写权限、磁盘已满、"
        "或同时开了多个 uvicorn 进程争用同一库文件）。请检查环境变量 LOCAL_DB_PATH 对应路径与权限，"
        "并查看终端完整错误信息。"
    ),
    "session": (
        "未携带有效管理后台登录状态（Cookie 未生效或已过期）。"
        "若使用 HTTPS 或反向代理，请确认已转发 X-Forwarded-Proto: https，"
        "且 uvicorn 使用 --forwarded-allow-ips=*。"
    ),
}


def _render_admin_login_html(request: Request) -> str:
    """服务端注入错误提示。

    所有注入值均经过 html.escape 转义，占位符仅含固定白名单字符，
    不会被用户输入意外展开。
    """
    err = (request.query_params.get("error") or "").strip()
    u_prefill = (request.query_params.get("username") or "").strip()
    has_pw_in_url = "password" in request.query_params

    parts: list[str] = []
    extra_class = ""
    if err:
        parts.append(
            _ADMIN_LOGIN_ERR_TEXT.get(
                err, f"登录出现问题（错误代码：{escape(err)}），请重试或查看服务端日志。"
            )
        )
        extra_class = " error"
    if has_pw_in_url:
        parts.append("请勿把密码写在网址中；请在下方密码框输入（默认密码为 admin）。")
        extra_class = " error"

    # extra_class 只含空格和字母，无需转义；body 和 username 已用 html.escape 处理
    body_esc = escape(" ".join(parts) if parts else "")
    username_attr = f' value="{escape(u_prefill, quote=True)}"' if u_prefill else ""

    # Generate captcha for the admin login form
    cap_question, cap_token = captcha_generate()

    raw = ADMIN_LOGIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    # 严格校验占位符存在，避免静默错位
    for placeholder in ("@@ADMIN_MSG_CLASS@@", "@@ADMIN_MSG_BODY@@", "@@USERNAME_ATTR@@", "@@CAPTCHA_QUESTION@@", "@@CAPTCHA_TOKEN@@"):
        if placeholder not in raw:
            logger.error("admin login template missing placeholder: %s", placeholder)
    return (
        raw.replace("@@ADMIN_MSG_CLASS@@", extra_class)
        .replace("@@ADMIN_MSG_BODY@@", body_esc)
        .replace("@@USERNAME_ATTR@@", username_attr)
        .replace("@@CAPTCHA_QUESTION@@", escape(cap_question))
        .replace("@@CAPTCHA_TOKEN@@", escape(cap_token, quote=True))
    )
request_logger = RequestLogger()
# 注入 DB 回调，让 EventBus 可以同时写 request_events
# 延迟赋值（database 在其后声明），startup 中完成绑定
database = LocalDatabase()
auth_manager = AuthManager(database)
rate_limiter = RateLimiter()
security_headers = build_security_headers()

# 追踪 HTTP 中间件创建的 fire-and-forget 日志任务，防止服务关闭时被强制取消丢日志
_http_log_tasks: set[asyncio.Task[object]] = set()

# 全局用户任务注册表：user_id -> {request_id -> asyncio.Task}
# 任务独立于 WebSocket 生命周期，断线后继续跑完
_user_running_tasks: dict[int, dict[str, asyncio.Task]] = {}
_user_running_tasks_lock = asyncio.Lock()


def _register_user_task(user_id: int, request_id: str, task: asyncio.Task) -> None:
    if user_id not in _user_running_tasks:
        _user_running_tasks[user_id] = {}
    _user_running_tasks[user_id][request_id] = task
    task.add_done_callback(lambda _t: _unregister_user_task(user_id, request_id))


def _unregister_user_task(user_id: int, request_id: str) -> None:
    bucket = _user_running_tasks.get(user_id)
    if bucket:
        bucket.pop(request_id, None)
        if not bucket:
            _user_running_tasks.pop(user_id, None)


def _get_user_pending_request_ids(user_id: int) -> list[str]:
    bucket = _user_running_tasks.get(user_id, {})
    return [rid for rid, t in bucket.items() if not t.done()]


def _cancel_user_task(user_id: int, request_id: str) -> bool:
    bucket = _user_running_tasks.get(user_id, {})
    task = bucket.get(request_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


def _cancel_all_user_tasks(user_id: int) -> list[asyncio.Task]:
    bucket = _user_running_tasks.get(user_id, {})
    cancelled = []
    for task in bucket.values():
        if not task.done():
            task.cancel()
            cancelled.append(task)
    return cancelled

app = FastAPI(title="Multi-Model Web Chat")
# JS/CSS/其他静态资源：1 小时浏览器缓存；HTML 动态内容已由路由设置 no-store
app.mount("/static", StaticFiles(directory=STATIC_DIR, html=False), name="static")
ADMIN_STATIC_DIR = STATIC_DIR / "admin"


# ── JSON 结构化 logging 配置（Phase 4）──────────────────────────────────────

class _JsonFormatter(logging.Formatter):
    """将 Python logging 记录格式化为单行 JSON，便于 ELK / Loki 等工具采集。"""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return _json.dumps(obj, ensure_ascii=False)


def _configure_json_logging() -> None:
    """若环境变量 LOG_FORMAT=json 则启用结构化 JSON 输出。"""
    if os.getenv("LOG_FORMAT", "").lower() != "json":
        return
    json_handler = logging.StreamHandler()
    json_handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(json_handler)
    root.setLevel(logging.INFO)


@app.on_event("startup")
async def initialize_local_database() -> None:
    _configure_json_logging()
    await database.initialize()
    await database.delete_expired_sessions()
    await ensure_default_admin_user(database)
    await _init_default_system_config()
    # Phase 1: 注入 EventBus 的 DB 写回调，实现三通道同步
    request_logger.set_db_callback(database.record_event)
    # Phase 3: 启动时清理旧日志（JSONL + DB）
    retention_days = max(1, int(os.getenv("LOG_RETENTION_DAYS", "30")))
    token_retention_days = max(1, int(os.getenv("TOKEN_LOG_RETENTION_DAYS", "90")))
    await asyncio.to_thread(request_logger.cleanup_old_jsonl)
    try:
        removed = await database.cleanup_old_events(retention_days)
        token_removed = await database.cleanup_old_token_usage(token_retention_days)
        failure_removed = await database.cleanup_old_failure_logs()
        if removed or token_removed or failure_removed:
            logger.info("Startup cleanup: events=%d token_usage=%d failure_logs=%d", removed, token_removed, failure_removed)
    except Exception:
        logger.warning("Startup cleanup failed", exc_info=True)


async def _init_default_system_config() -> None:
    cfg = await database.get_system_config()
    defaults = {
        "config_default_points": "100",
        "config_low_balance_threshold": "10",
        "config_allow_registration": "true",
        "config_search_points_per_call": "5",
        "config_admin_initial_points_granted": "false",
    }
    for key, value in defaults.items():
        if key not in cfg:
            await database.set_system_config(key, value)
    admin_user = await database.get_user_by_username("admin")
    if admin_user:
        await database.ensure_user_balance(admin_user["id"])
        if cfg.get("config_admin_initial_points_granted", "false").lower() != "true":
            balance = await database.get_user_balance(admin_user["id"])
            if balance <= 0:
                await database.add_points(admin_user["id"], 100, admin_user["id"], "系统初始赠送")
            await database.set_system_config("config_admin_initial_points_granted", "true")


async def _get_admin_user(request: Request) -> dict[str, str] | None:
    return await auth_manager.get_user_from_token(request.cookies.get(ADMIN_SESSION_COOKIE_NAME))


async def _require_admin(request: Request) -> dict[str, str]:
    user = await _get_admin_user(request)
    if not user:
        raise AuthError("未登录管理后台或登录已失效。")
    role = await database.get_user_role(user["user_id"])
    if role != "admin":
        raise AuthError("没有管理员权限。")
    return user


async def _load_global_service_settings() -> dict:
    """从 app_meta 加载全局模型/搜索配置，转为与原 user_settings 相同的结构。"""
    import json as _j
    raw = await database.get_global_model_config()
    if not raw:
        logger.warning("_load_global_service_settings: app_meta returned empty, no model_config_* keys found")
    models_raw = raw.get("model_config_models_json", "[]")
    try:
        models = _j.loads(models_raw)
    except Exception:
        models = []
    try:
        timeout_ms = int(raw.get("model_config_firecrawl_timeout_ms", "45000"))
    except ValueError:
        timeout_ms = 45000
    try:
        extra_headers = _json.loads(raw.get("model_config_extra_headers", "{}")) if raw.get("model_config_extra_headers") else {}
    except Exception:
        extra_headers = {}
    return {
        "api_base_url": raw.get("model_config_api_base_url", ""),
        "api_format": raw.get("model_config_api_format", "openai"),
        "api_key": raw.get("model_config_api_key", ""),
        "models": models,
        "firecrawl_api_key": raw.get("model_config_firecrawl_api_key", ""),
        "firecrawl_country": raw.get("model_config_firecrawl_country", "CN"),
        "firecrawl_timeout_ms": timeout_ms,
        "preprocess_model": raw.get("model_config_preprocess_model", ""),
        "user_api_base_url": raw.get("model_config_user_api_base_url", ""),
        "user_api_format": raw.get("model_config_user_api_format", "openai"),
        "extra_params": _json.loads(raw.get("model_config_extra_params", "{}")) if raw.get("model_config_extra_params") else {},
        "extra_headers": extra_headers if isinstance(extra_headers, dict) else {},
    }


_SENSITIVE_HEADER_NAMES = {"authorization", "x-api-key", "api-key", "proxy-authorization"}
_BLOCKED_OVERRIDE_HEADER_NAMES = {"content-type", "content-length", "host"}


def _sanitize_extra_headers(raw_headers: object) -> dict[str, str]:
    if not isinstance(raw_headers, dict):
        return {}
    cleaned: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        lower_name = name.lower()
        if lower_name in _BLOCKED_OVERRIDE_HEADER_NAMES:
            continue
        if any(ch in name for ch in ("\r", "\n", ":")):
            continue
        value = str(raw_value if raw_value is not None else "").strip()
        if not value or any(ch in value for ch in ("\r", "\n")):
            continue
        cleaned[name] = value
    return cleaned


def _mask_header_value(name: str, value: str) -> str:
    if not value:
        return ""
    if name.lower() not in _SENSITIVE_HEADER_NAMES:
        return value
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}***{value[-4:]}"


def _summarize_extra_headers(headers: dict[str, str]) -> dict[str, object]:
    return {
        "count": len(headers),
        "names": sorted(headers.keys()),
        "sensitive_names": sorted([name for name in headers if name.lower() in _SENSITIVE_HEADER_NAMES]),
    }


def _build_openai_default_headers(extra_headers: dict[str, str] | None = None) -> dict[str, str] | None:
    headers = _sanitize_extra_headers(extra_headers or {})
    return headers or None


def _create_llm_client_from_settings(user_settings: dict):
    """Create a unified LLM client from user_settings dict."""
    return create_llm_client(
        user_settings.get("api_format", "openai"),
        api_key=user_settings.get("api_key", ""),
        base_url=user_settings.get("api_base_url", ""),
        default_headers=_build_openai_default_headers(user_settings.get("extra_headers", {})),
    )


BASE_SYSTEM_PROMPT = (
    "你是 NanoBob AI 工作台中的协作模型节点。"
    "回答必须使用清晰的 Markdown。"
    "如果给了联网搜索结果，请优先基于搜索结果回答，并在最后附上「参考来源」列表。"
    "不要虚构来源。"
)

THINK_PROMPT = (
    "思考模式已开启。请先做更严谨的分析和校验，再输出结论。"
    "不要泄露隐藏推理过程，直接输出结构化结论、依据、风险和建议。"
)

BRANCH_PROMPT_TEMPLATE = (
    "你正在从自己先前的回答继续深入。"
    "下面这条消息来自用户的分支指令，请结合你此前到第 {source_round} 轮的内容继续推进。"
)


@dataclass
class ThreadState:
    request_id: str
    models: list[str]
    histories: dict[str, list[dict[str, str]]]
    user_message: str
    discussion_rounds: int
    search_enabled: bool | str  # True / False / "auto"
    think_enabled: bool
    search_bundle: SearchBundle | None = None
    parent_request_id: str | None = None
    source_model: str | None = None
    source_round: int | None = None
    canvas_id: str | None = None
    reserved_points: float = 0.0
    charged_search_points: float = 0.0
    meta: dict[str, str] = field(default_factory=dict)


@app.middleware("http")
async def log_http_request(request: Request, call_next):
    started_at = perf_counter()
    response = await call_next(request)
    duration_ms = round((perf_counter() - started_at) * 1000, 2)
    client_host = request.client.host if request.client else "unknown"

    # 静态资源加 Cache-Control（JS/CSS 1h 缓存；HTML 由路由负责 no-store）
    if request.url.path.startswith("/static/"):
        ext = request.url.path.rsplit(".", 1)[-1].lower() if "." in request.url.path else ""
        if ext in ("js", "css", "png", "jpg", "jpeg", "webp", "svg", "ico", "woff", "woff2"):
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        elif ext in ("html",):
            response.headers.setdefault("Cache-Control", "no-store")

    _t = asyncio.create_task(
        request_logger.emit(
            "http_request",
            level="info",
            client_host=client_host,
            data={
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query) or None,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
    )
    _http_log_tasks.add(_t)
    _t.add_done_callback(_http_log_tasks.discard)
    for key, value in security_headers.items():
        if key == "Content-Security-Policy" and request.url.path == "/":
            response.headers[key] = LANDING_CSP
        else:
            response.headers.setdefault(key, value)
    return response


# -- Setup guard middleware ---------------------------------------------------
# Registered AFTER log_http_request so that FastAPI's LIFO ordering ensures
# this middleware runs BEFORE the logger (i.e. outermost layer).
# When the app has not yet been initialised via /setup, ALL pages and API
# endpoints redirect (or return 503) except /setup itself, /api/setup, and
# static assets.

_SETUP_ALLOWED_PREFIXES = ("/setup", "/api/setup", "/static/")


@app.middleware("http")
async def setup_guard(request: Request, call_next):
    """Block every route until initial setup is complete."""
    if not is_configured():
        path = request.url.path
        if not any(path.startswith(p) for p in _SETUP_ALLOWED_PREFIXES):
            # API / XHR calls get a JSON 503; browsers get a redirect.
            accept = request.headers.get("accept", "")
            if "application/json" in accept or path.startswith("/api/") or path.startswith("/ws/"):
                return JSONResponse(
                    {"detail": "系统尚未完成初始化，请先访问 /setup 进行配置。"},
                    status_code=503,
                )
            return RedirectResponse(url="/setup", status_code=303)
    return await call_next(request)


async def _get_request_user(request: Request) -> dict[str, str] | None:
    return await auth_manager.get_user_from_token(request.cookies.get(SESSION_COOKIE_NAME))


async def _get_websocket_user(websocket: WebSocket) -> dict[str, str] | None:
    return await auth_manager.get_user_from_token(websocket.cookies.get(SESSION_COOKIE_NAME))


async def _parse_json_body(request: Request) -> dict[str, object]:
    try:
        payload = await request.json()
    except Exception:
        raise AuthError("请求体格式不正确。")
    if not isinstance(payload, dict):
        raise AuthError("请求体格式不正确。")
    return payload


def _safe_login_username(raw: str, max_len: int = 64) -> str:
    """日志用用户名脱敏：可打印字符、空白规整、截断（不记录密码）。"""
    s = "".join(c if c.isprintable() else "?" for c in raw)
    s = " ".join(s.split())
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s if s else "(empty)"


def _log_login_failure(*, route: str, client_host: str, username: str, reason: str) -> None:
    username_safe = _safe_login_username(username)
    logger.warning(
        "login_failed route=%s client=%s username=%s reason=%s",
        route,
        client_host,
        username_safe,
        reason,
    )
    _t = asyncio.create_task(
        request_logger.emit(
            "login_failed",
            level="warning",
            client_host=client_host,
            data={"route": route, "username": username_safe, "reason": reason},
        )
    )
    _http_log_tasks.add(_t)
    _t.add_done_callback(_http_log_tasks.discard)


def _log_login_success(*, route: str, client_host: str, username: str, user_id: int | None = None) -> None:
    username_safe = _safe_login_username(username)
    logger.info(
        "login_success route=%s client=%s username=%s user_id=%s",
        route,
        client_host,
        username_safe,
        user_id if user_id is not None else "-",
    )
    _t = asyncio.create_task(
        request_logger.emit(
            "login_success",
            level="info",
            user_id=user_id,
            client_host=client_host,
            data={"route": route, "username": username_safe},
        )
    )
    _http_log_tasks.add(_t)
    _t.add_done_callback(_http_log_tasks.discard)


def _emit_admin_audit(
    admin_id: int, action: str,
    target_user_id: int | None = None,
    **detail: object,
) -> None:
    """在后台操作成功后异步写入审计日志（fire-and-forget）。"""
    async def _do() -> None:
        await database.add_admin_audit_log(
            admin_id=admin_id, action=action,
            target_user_id=target_user_id, detail=detail or {},
        )
        await request_logger.emit(
            "admin_action",
            level="info",
            user_id=admin_id,
            data={"action": action, "target_user_id": target_user_id, **{k: str(v) for k, v in detail.items()}},
        )
    _t = asyncio.create_task(_do())
    _http_log_tasks.add(_t)
    _t.add_done_callback(_http_log_tasks.discard)


async def _read_admin_session_login_form(request: Request) -> dict[str, str]:
    """读取管理后台登录表单全部字段（username / password / captcha 等）。

    默认 HTML 表单为 ``application/x-www-form-urlencoded``：用标准库 ``parse_qs`` 解析 ``body``，
    **不依赖 python-multipart**。仅当为 multipart 等类型时才调用 ``request.form()``。
    """
    _FIELDS = ("username", "password", "captcha_token", "captcha_answer", "website")

    body = await request.body()
    main = (request.headers.get("content-type") or "").split(";")[0].strip().lower()

    def _from_qs(q: dict) -> dict[str, str]:
        return {k: str((q.get(k) or [""])[0]).strip() if k != "password" else str((q.get(k) or [""])[0]) for k in _FIELDS}

    if main == "application/x-www-form-urlencoded":
        q = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=100)
        return _from_qs(q)

    if main == "" and body and not body.lstrip().startswith(b"--"):
        q = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=100)
        if "username" in q or "password" in q:
            return _from_qs(q)

    # multipart 等：body 已缓存，Starlette 的 form() 可继续解析
    form = await request.form()
    return {k: str(form.get(k) or "").strip() if k != "password" else str(form.get(k) or "") for k in _FIELDS}


def _request_is_https(request: Request) -> bool:
    """判断客户端是否经 HTTPS 访问（含 Nginx/Caddy 等反代的 X-Forwarded-Proto）。"""
    if request.url.scheme == "https":
        return True
    forwarded = (request.headers.get("x-forwarded-proto") or "").strip().lower()
    if not forwarded:
        return False
    first = forwarded.split(",")[0].strip()
    return first == "https"


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request),
        max_age=SESSION_DAYS * 24 * 60 * 60,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


def _is_origin_allowed(origin: str | None, host: str | None) -> bool:
    if not origin:
        return False
    if not host:
        return False
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    return parsed.netloc == host


def _unauthorized_json(message: str = "未登录或登录已失效。") -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=401)


async def _require_user(request: Request) -> dict[str, str]:
    user = await _get_request_user(request)
    if not user:
        raise AuthError("未登录或登录已失效。")
    return user


class OriginError(Exception):
    """Raised when origin validation fails (403)."""


async def _require_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        raise OriginError("非法来源。")


def _pick_analysis_model(models: list[dict[str, str]]) -> str:
    names = [m["name"] for m in models]
    if "Kimi-K2.5" in names:
        return "Kimi-K2.5"
    for name in names:
        if "kimi" in name.lower():
            return name
    return names[0] if names else ""


def _build_model_id_map(models: list[dict[str, str]]) -> dict[str, str]:
    return {m["name"]: m["id"] for m in models}


def _mask_key(key: str) -> str:
    if not key or len(key) <= 7:
        return "****" if key else ""
    return key[:3] + "****" + key[-4:]


def _pick_saved_model_for_test(
    models_raw: object, *, model_name: str = "", model_id: str = "",
) -> tuple[str, str] | None:
    pairs: list[tuple[str, str]] = []
    if isinstance(models_raw, list):
        for item in models_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            mid = str(item.get("id", "")).strip()
            if name and mid:
                pairs.append((name, mid))
    if not pairs:
        return None
    if model_name and model_id:
        for name, mid in pairs:
            if name == model_name and mid == model_id:
                return name, mid
        return None
    if model_name:
        for name, mid in pairs:
            if name == model_name:
                return name, mid
        return None
    if model_id:
        for name, mid in pairs:
            if mid == model_id:
                return name, mid
        return None
    return pairs[0]


_CONCLUSION_SYSTEM_PROMPT = (
    "你是一位专业的多模型讨论总结专家。"
    "你收到的是各模型在多轮讨论后的最后一轮结论（已压缩整理）。"
    "请输出一份完整 Markdown 文档，直接回答用户问题，并整合关键结论。要求：\n"
    "1. 仅基于输入中的最后一轮结论进行综合，不要假设缺失轮次；\n"
    "2. 优先呈现共识结论、关键依据、风险与可执行建议；\n"
    "3. 对分歧给出取舍理由或适用条件；\n"
    "4. 结构清晰，适合直接交付给用户。"
)


def _compress_for_conclusion(text: str, max_chars: int) -> str:
    cleaned_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not cleaned_lines:
        return ""
    compact = "\n".join(cleaned_lines)
    if len(compact) <= max_chars:
        return compact

    keywords = (
        "结论", "总结", "建议", "风险", "答案", "最终", "推荐", "步骤", "注意", "依据",
        "conclusion", "summary", "recommend", "risk", "therefore", "final",
    )
    prioritized: list[str] = []
    fallback: list[str] = []
    for line in cleaned_lines:
        lowered = line.lower()
        if line.startswith(("#", "-", "*", "1.", "2.", "3.", "4.")) or any(k in lowered for k in keywords):
            prioritized.append(line)
        else:
            fallback.append(line)

    picked: list[str] = []
    seen: set[str] = set()
    budget = max_chars
    for line in prioritized + fallback:
        if line in seen:
            continue
        cost = len(line) + 1
        if budget - cost < 0 and picked:
            break
        seen.add(line)
        picked.append(line)
        budget -= cost

    result = "\n".join(picked).strip()
    if len(result) > max_chars:
        result = result[:max_chars].rstrip()
    return result + "\n\n[已压缩，保留核心结论]"


def _collect_latest_success_results(results: list[dict]) -> list[dict[str, object]]:
    latest_by_model: dict[str, dict[str, object]] = {}
    for item in results:
        if item.get("status") != "success" or not item.get("content"):
            continue
        model = str(item.get("model", "")).strip()
        if not model:
            continue
        try:
            round_num = int(item.get("round", 1))
        except (TypeError, ValueError):
            round_num = 1
        previous = latest_by_model.get(model)
        prev_round = 0
        if previous is not None:
            try:
                prev_round = int(str(previous.get("round", 0)))
            except (TypeError, ValueError):
                prev_round = 0
        if previous is None or round_num >= prev_round:
            latest_by_model[model] = {
                "model": model,
                "round": round_num,
                "content": str(item.get("content", "")),
            }
    return sorted(latest_by_model.values(), key=lambda x: str(x["model"]))


def _collect_latest_success_results_from_map(model_results: dict[str, list[dict]]) -> list[dict[str, object]]:
    flat: list[dict] = []
    for model, rounds in model_results.items():
        for row in rounds:
            flat.append(
                {
                    "model": model,
                    "round": row.get("round", 1),
                    "status": row.get("status", ""),
                    "content": row.get("content", ""),
                }
            )
    return _collect_latest_success_results(flat)


def _build_conclusion_input(results: list[dict], user_message: str) -> str:
    """仅使用每个模型最后一轮成功输出，压缩后构造总结输入。"""
    latest_rounds = _collect_latest_success_results(results)
    parts = [
        f"## 用户原始提问\n\n{user_message}\n",
        "## 可用材料范围\n\n仅包含每个模型最后一轮回复，并已压缩保留核心结论。",
    ]
    for item in latest_rounds:
        compressed = _compress_for_conclusion(
            str(item["content"]),
            _CONCLUSION_PER_MODEL_INPUT_CHARS,
        )
        parts.append(
            f"## 模型：{item['model']}（最后一轮：第 {item['round']} 轮）\n\n{compressed or '[空内容]'}"
        )
    combined = "\n\n".join(parts)
    if len(combined) > _CONCLUSION_MAX_INPUT_CHARS:
        combined = combined[:_CONCLUSION_MAX_INPUT_CHARS] + "\n\n[内容已截断]"
    return combined


def _pick_conclusion_model(models: list[dict[str, str]]) -> str:
    """优先 Kimi，不可用时降级到第一个可用模型。"""
    return _pick_analysis_model(models)


async def _generate_conclusion(
    *,
    user_settings: dict,
    results: list[dict],
    user_message: str,
    request_id: str,
    canvas_id: str | None,
    websocket: WebSocket,
    send_event,
    db: "LocalDatabase",
    user_id: int,
    bill_points: bool,
) -> None:
    """后台任务：生成最终结论文档并推送给前端。"""
    latest_round_results = _collect_latest_success_results(results)
    if not latest_round_results:
        await db.upsert_request_summary(
            request_id=request_id,
            canvas_id=canvas_id,
            status="failed",
            error_message="无可用的最终轮结果",
        )
        await send_event(
            websocket,
            {
                "type": "conclusion_error",
                "request_id": request_id,
                "content": "结论生成失败：没有可用的模型最终轮结果。",
                "retryable": True,
            },
        )
        return

    models = user_settings.get("models", [])
    model_name = _pick_conclusion_model(models)
    if not model_name:
        await send_event(websocket, {
            "type": "conclusion_error",
            "request_id": request_id,
            "content": "无可用模型生成结论。",
            "retryable": True,
        })
        await db.upsert_request_summary(
            request_id=request_id, canvas_id=canvas_id,
            status="failed", error_message="无可用模型",
        )
        return

    id_map = _build_model_id_map(models)
    model_id = id_map.get(model_name, model_name)

    await send_event(websocket, {
        "type": "conclusion_start",
        "request_id": request_id,
        "model": model_name,
    })
    await db.upsert_request_summary(
        request_id=request_id, canvas_id=canvas_id,
        summary_model=model_name, status="pending",
    )

    conclusion_input = _build_conclusion_input(
        [dict(item, status="success") for item in latest_round_results],
        user_message,
    )
    llm = _create_llm_client_from_settings(user_settings)
    reserved_points = 0.0
    if bill_points:
        reserved_points = await _estimate_conclusion_reserve_points(db, user_settings=user_settings)
        if reserved_points > 0 and not await db.deduct_points(user_id, reserved_points):
            await db.upsert_request_summary(
                request_id=request_id,
                canvas_id=canvas_id,
                summary_model=model_name,
                status="failed",
                error_message="余额不足，无法生成最终结论",
            )
            await send_event(websocket, {
                "type": "conclusion_error",
                "request_id": request_id,
                "content": "余额不足，无法生成最终结论。",
                "retryable": True,
            })
            return

    try:
        llm_resp = await asyncio.wait_for(
            llm.complete(
                model=model_id,
                messages=[
                    {"role": "system", "content": _CONCLUSION_SYSTEM_PROMPT},
                    {"role": "user", "content": conclusion_input},
                ],
                temperature=0.3,
            ),
            timeout=_CONCLUSION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning("conclusion generation timed out request_id=%s model=%s", request_id, model_name)
        await db.upsert_request_summary(
            request_id=request_id, canvas_id=canvas_id,
            summary_model=model_name, status="failed",
            error_message=f"结论生成超时（>{_CONCLUSION_TIMEOUT_SECONDS}s）",
        )
        await send_event(websocket, {
            "type": "conclusion_error",
            "request_id": request_id,
            "content": f"结论生成超时（>{_CONCLUSION_TIMEOUT_SECONDS}s），不影响已有对话结果。",
            "retryable": True,
        })
        if reserved_points > 0:
            await db.add_points(user_id, reserved_points, user_id, "结论预扣返还")
        return
    except Exception as exc:
        logger.warning("conclusion generation failed request_id=%s error=%s", request_id, exc, exc_info=True)
        await db.upsert_request_summary(
            request_id=request_id, canvas_id=canvas_id,
            summary_model=model_name, status="failed",
            error_message=str(exc)[:500],
        )
        await send_event(websocket, {
            "type": "conclusion_error",
            "request_id": request_id,
            "content": "结论生成失败，不影响已有对话结果。",
            "retryable": True,
        })
        if reserved_points > 0:
            await db.add_points(user_id, reserved_points, user_id, "结论预扣返还")
        return

    markdown = llm_resp.text
    if len(markdown) > _CONCLUSION_MAX_OUTPUT_CHARS:
        markdown = markdown[:_CONCLUSION_MAX_OUTPUT_CHARS] + "\n\n[结论已截断]"

    if bill_points and reserved_points > 0:
        prompt_tokens = llm_resp.usage.prompt_tokens
        completion_tokens = llm_resp.usage.completion_tokens
        actual_points = await _calculate_model_points_cost(
            db,
            model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        refund = max(0.0, reserved_points - actual_points)
        extra_due = max(0.0, actual_points - reserved_points)
        if extra_due > 0:
            extra_ok = await db.deduct_points(user_id, extra_due)
            if not extra_ok:
                logger.warning("conclusion_extra_charge_failed request_id=%s user_id=%s amount=%.4f", request_id, user_id, extra_due)
        if refund > 0:
            await db.add_points(user_id, refund, user_id, "结论预扣返还")

    await db.upsert_request_summary(
        request_id=request_id, canvas_id=canvas_id,
        summary_model=model_name, summary_markdown=markdown, status="success",
    )
    await send_event(websocket, {
        "type": "conclusion_done",
        "request_id": request_id,
        "model": model_name,
        "markdown": markdown,
    })


async def _summarize_selection_bundle(
    bundle: str, count: int, *, user_settings: dict,
) -> tuple[str, str]:
    models = user_settings["models"]
    model = _pick_analysis_model(models)
    if not model:
        raise RuntimeError("未配置可用的摘要模型。")

    id_map = _build_model_id_map(models)
    llm = _create_llm_client_from_settings(user_settings)
    clipped_bundle = bundle[:12000]
    prompt = (
        f"请将用户圈选的 {count} 个画布节点压缩成可供下一轮对话继续使用的上下文。\n"
        "输出要求：\n"
        "1. 使用简洁中文；\n"
        "2. 优先保留最终结论、关键依据、核心分歧、下一步建议；\n"
        "3. 不要重复原文，不要展开长篇推理；\n"
        "4. 控制在 220 到 300 字内，可使用 Markdown 列表。\n\n"
        "以下是待压缩的节点内容：\n\n"
        f"{clipped_bundle}"
    )
    llm_resp = await llm.complete(
        model=id_map.get(model, model),
        messages=[
            {
                "role": "system",
                "content": "你是 NanoBob 工作台的上下文压缩助手，只输出给下一轮模型使用的高密度摘要。",
            },
            {"role": "user", "content": prompt},
        ],
    )
    summary = llm_resp.text
    if not summary:
        raise RuntimeError("摘要模型未返回可用内容。")
    return summary[:1200], model


async def _analyze_conversation(
    messages: list[dict[str, str]], *, user_settings: dict,
) -> tuple[dict[str, str], str]:
    models = user_settings["models"]
    model = _pick_analysis_model(models)
    if not model:
        raise RuntimeError("未配置可用的分析模型。")

    id_map = _build_model_id_map(models)
    llm = _create_llm_client_from_settings(user_settings)

    conversation_text = ""
    for msg in messages:
        role_label = {"user": "用户", "assistant": "模型", "system": "系统"}.get(msg.get("role", ""), msg.get("role", ""))
        content = msg.get("content", "").strip()
        if content:
            conversation_text += f"【{role_label}】{content}\n\n"
    conversation_text = conversation_text[:16000]

    prompt = (
        "请分析以下对话内容，输出 JSON 格式（不要输出其他内容）：\n"
        '{"title": "一句话标题（15字以内）", "key_points": ["要点1", "要点2", ...], "summary": "整体摘要（100-200字）", "topic_tags": ["标签1", "标签2"]}\n\n'
        "要求：\n"
        "1. title：用一句话概括对话主题；\n"
        "2. key_points：提取 3-5 个核心要点；\n"
        "3. summary：简明概括对话的来龙去脉和关键结论；\n"
        "4. topic_tags：2-4 个话题标签。\n\n"
        f"对话内容：\n\n{conversation_text}"
    )

    llm_resp = await llm.complete(
        model=id_map.get(model, model),
        messages=[
            {"role": "system", "content": "你是会话分析助手，只输出 JSON，不要输出任何解释。"},
            {"role": "user", "content": prompt},
        ],
    )
    raw_text = llm_resp.text
    if not raw_text:
        raise RuntimeError("分析模型未返回可用内容。")

    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        result = _json.loads(cleaned)
    except _json.JSONDecodeError:
        result = {"title": "", "key_points": [], "summary": raw_text[:500], "topic_tags": []}

    return result, model


async def _load_user_settings_or_error(request: Request) -> tuple[dict | None, dict | None, JSONResponse | None]:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return None, None, JSONResponse({"detail": str(exc)}, status_code=401)
    gs = await _load_global_service_settings()
    if not gs.get("api_key"):
        return None, None, JSONResponse({"detail": "管理员尚未完成模型配置，请联系管理员。"}, status_code=503)
    return user, gs, None


def _html_response(path: Path) -> FileResponse:
    resp = FileResponse(path)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
async def landing(request: Request) -> Response:
    return _html_response(STATIC_DIR / "landing.html")


@app.get("/app")
async def canvas_app(request: Request) -> Response:
    user = await _get_request_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _html_response(STATIC_DIR / "index.html")


@app.get("/setup")
async def setup_page(request: Request) -> Response:
    if is_configured():
        return RedirectResponse(url="/login", status_code=303)
    return _html_response(STATIC_DIR / "setup.html")


@app.post("/api/setup")
async def save_setup_config(request: Request) -> JSONResponse:
    if is_configured():
        return JSONResponse({"detail": "已完成配置，无法重复设置。"}, status_code=400)
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体格式不正确。"}, status_code=400)

    base_url = str(payload.get("base_url", "")).strip()
    api_format = str(payload.get("api_format", "openai")).strip()
    api_key = str(payload.get("API_key", "")).strip()
    models_raw = payload.get("models", [])

    if not base_url:
        return JSONResponse({"detail": "请填写 API 地址。"}, status_code=400)
    if not api_key:
        return JSONResponse({"detail": "请填写 API Key。"}, status_code=400)
    if not isinstance(models_raw, list) or not models_raw:
        return JSONResponse({"detail": "请至少添加一个模型。"}, status_code=400)
    if api_format not in ("openai", "anthropic"):
        return JSONResponse({"detail": "API 格式仅支持 openai 或 anthropic。"}, status_code=400)

    models = []
    for item in models_raw:
        if not isinstance(item, dict):
            return JSONResponse({"detail": "模型格式不正确。"}, status_code=400)
        name = str(item.get("name", "")).strip()
        model_id = str(item.get("id", "")).strip()
        if not name or not model_id:
            return JSONResponse({"detail": "模型名称和模型 ID 不能为空。"}, status_code=400)
        models.append({"name": name, "id": model_id})

    config_data = {
        "base_url": base_url,
        "api_format": api_format,
        "API_key": api_key,
        "models": models,
    }

    try:
        save_settings(config_data)
    except Exception as exc:
        logger.exception("save_settings failed: %s", exc)
        return JSONResponse({"detail": "保存配置失败，请查看服务端日志。"}, status_code=500)

    await database.initialize()

    salt = secrets.token_hex(16)
    password_hash = AuthManager._hash_password("admin", salt)
    admin_id = await database.create_user("admin", password_hash, salt)
    if admin_id:
        await database.set_user_role(admin_id, "admin")

    if admin_id and models:
        await database.upsert_user_settings(
            admin_id,
            api_base_url=base_url,
            api_format=api_format,
            api_key=api_key,
            models_json=_json.dumps(models, ensure_ascii=False),
            firecrawl_api_key="",
            firecrawl_country="CN",
            firecrawl_timeout_ms=45000,
        )
    await database.set_global_model_config(
        api_base_url=base_url,
        api_format=api_format,
        api_key=api_key,
        models_json=_json.dumps(models, ensure_ascii=False),
        firecrawl_api_key="",
        firecrawl_country="CN",
        firecrawl_timeout_ms=45000,
        preprocess_model="",
        user_api_base_url=base_url,
        user_api_format=api_format,
        extra_params={},
        extra_headers={},
    )

    return JSONResponse({"ok": True})


@app.get("/settings")
async def settings_page(request: Request) -> Response:
    if not is_configured():
        return RedirectResponse(url="/setup", status_code=303)
    user = await _get_request_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _html_response(STATIC_DIR / "settings.html")


@app.get("/api/settings")
async def get_user_settings_api(request: Request) -> JSONResponse:
    """仅返回账号基本信息。API 配置由管理员统一管理，不再对普通用户暴露。"""
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    return JSONResponse({"authenticated": True, "username": user["username"]})


@app.put("/api/settings")
async def update_user_settings_api(request: Request) -> JSONResponse:
    """大模型 API 和搜索配置已收归管理员后台，普通用户不允许修改。"""
    try:
        await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    return JSONResponse(
        {"detail": "API 和搜索配置由管理员统一管理，请联系管理员。"},
        status_code=403,
    )


@app.get("/api/user/usage-detail")
async def user_usage_detail(request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        limit = min(int(request.query_params.get("limit", "200")), 500)
    except ValueError:
        limit = 200
    detail = await database.get_user_usage_detail(user["user_id"], limit)
    return JSONResponse({"detail": detail})


@app.get("/api/user/usage-summary")
async def user_usage_summary(request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    summary = await database.get_user_usage_summary(user["user_id"])
    return JSONResponse({"summary": summary})


@app.get("/api/user/custom-api-key")
async def get_user_custom_api_key_api(request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    user_keys = await database.get_user_custom_keys(user["user_id"])
    gs = await _load_global_service_settings()
    model_keys_masked = {k: _mask_key(v) for k, v in user_keys["model_keys"].items() if v}
    return JSONResponse({
        "model_keys": model_keys_masked,
        "use_custom_key": user_keys["use_custom_key"],
        "models": gs.get("models", []),
        "user_api_base_url": gs.get("user_api_base_url", ""),
        "user_api_format": gs.get("user_api_format", "openai"),
    })


@app.put("/api/user/custom-api-key")
async def set_user_custom_api_key_api(request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    raw_keys = payload.get("model_keys", {})
    if not isinstance(raw_keys, dict):
        return JSONResponse({"detail": "model_keys 格式不正确。"}, status_code=400)
    use_custom_key = bool(payload.get("use_custom_key", False))
    existing = await database.get_user_custom_keys(user["user_id"])
    existing_keys = existing.get("model_keys", {})
    model_keys: dict[str, str] = {}
    for k, v in raw_keys.items():
        key_name = str(k).strip()
        key_val = str(v).strip()
        if key_val == "__KEEP__":
            if key_name in existing_keys:
                model_keys[key_name] = existing_keys[key_name]
        elif key_val:
            model_keys[key_name] = key_val
    await database.set_user_custom_keys(user["user_id"], model_keys, use_custom_key)
    return JSONResponse({"ok": True})


@app.delete("/api/user/custom-api-key")
async def delete_user_custom_api_key_api(request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)
    await database.set_user_custom_keys(user["user_id"], {}, False)
    return JSONResponse({"ok": True})


@app.get("/api/captcha")
async def get_captcha() -> JSONResponse:
    """Generate a new arithmetic CAPTCHA challenge."""
    question, token = captcha_generate()
    return JSONResponse({"question": question, "token": token})


@app.get("/login")
async def login_page(request: Request):
    if not is_configured():
        return RedirectResponse(url="/setup", status_code=303)
    user = await _get_request_user(request)
    if user:
        return RedirectResponse(url="/app", status_code=303)
    return _html_response(STATIC_DIR / "login.html")


@app.get("/api/auth/session")
async def auth_session(request: Request) -> JSONResponse:
    user = await _get_request_user(request)
    if not user:
        return JSONResponse({"authenticated": False}, status_code=200)
    return JSONResponse({"authenticated": True, "username": user["username"]})


@app.get("/api/auth/registration-status")
async def registration_status() -> JSONResponse:
    """Return whether new-user registration is currently allowed."""
    cfg = await database.get_system_config()
    allow = cfg.get("config_allow_registration", "true") != "false"
    return JSONResponse({"allow": allow})


@app.post("/api/auth/register")
async def register(request: Request) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    if not await rate_limiter.allow_async(f"auth-register:{client_host}", limit=10, window_seconds=600):
        return JSONResponse({"detail": "注册过于频繁，请稍后再试。"}, status_code=429)
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)

    cfg = await database.get_system_config()
    if cfg.get("config_allow_registration") == "false":
        return JSONResponse({"detail": "当前不允许注册新用户。"}, status_code=403)

    try:
        payload = await _parse_json_body(request)
        # --- captcha / honeypot ---
        if check_honeypot(payload.get("website")):
            return JSONResponse({"detail": "请求异常。"}, status_code=400)
        cap_err = captcha_verify(str(payload.get("captcha_token", "")), str(payload.get("captcha_answer", "")))
        if cap_err:
            return JSONResponse({"detail": cap_err}, status_code=400)
        # --- end captcha ---
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        user, token, _ = await auth_manager.register(username, password)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    default_points = float(cfg.get("config_default_points", "100"))
    if default_points > 0:
        await database.add_points(user["id"], default_points, user["id"], "注册赠送")

    response = JSONResponse({"ok": True, "username": user["username"]})
    _set_session_cookie(response, token, request)
    _t = asyncio.create_task(request_logger.emit(
        "register_success",
        level="info",
        user_id=user["id"],
        client_host=client_host,
        data={"username": _safe_login_username(user["username"])},
    ))
    _http_log_tasks.add(_t); _t.add_done_callback(_http_log_tasks.discard)
    return response


@app.post("/api/auth/login")
async def login(request: Request) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    if not await rate_limiter.allow_async(f"auth-login:{client_host}", limit=15, window_seconds=600):
        _log_login_failure(
            route="/api/auth/login",
            client_host=client_host,
            username="-",
            reason="rate_limited",
        )
        return JSONResponse({"detail": "登录过于频繁，请稍后再试。"}, status_code=429)
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        _log_login_failure(
            route="/api/auth/login",
            client_host=client_host,
            username="-",
            reason="origin_denied",
        )
        return JSONResponse({"detail": "非法来源。"}, status_code=403)

    username = ""
    try:
        payload = await _parse_json_body(request)
        # --- captcha / honeypot ---
        if check_honeypot(payload.get("website")):
            return JSONResponse({"detail": "请求异常。"}, status_code=400)
        cap_err = captcha_verify(str(payload.get("captcha_token", "")), str(payload.get("captcha_answer", "")))
        if cap_err:
            return JSONResponse({"detail": cap_err}, status_code=400)
        # --- end captcha ---
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        user, token, _ = await auth_manager.login(username, password)
    except AuthError as exc:
        _log_login_failure(
            route="/api/auth/login",
            client_host=client_host,
            username=username,
            reason=str(exc),
        )
        return JSONResponse({"detail": str(exc)}, status_code=400)

    response = JSONResponse({"ok": True, "username": user["username"]})
    _set_session_cookie(response, token, request)
    _log_login_success(
        route="/api/auth/login",
        client_host=client_host,
        username=user["username"],
        user_id=user["user_id"],
    )
    return response


@app.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)
    await auth_manager.logout(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse({"ok": True})
    _clear_session_cookie(response)
    return response


@app.get("/api/models")
async def list_models(request: Request):
    user = await _get_request_user(request)
    if not user:
        return _unauthorized_json()
    gs = await _load_global_service_settings()
    models = gs.get("models", [])
    return {
        "models": models,
        "analysis_model": _pick_analysis_model(models),
    }


@app.post("/api/selection-summary")
async def selection_summary(request: Request) -> JSONResponse:
    user, us, err = await _load_user_settings_or_error(request)
    if err:
        return err
    try:
        await _require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    client_host = request.client.host if request.client else "unknown"
    if not await rate_limiter.allow_async(f"selection-summary:{user['user_id']}:{client_host}", limit=24, window_seconds=300):
        return JSONResponse({"detail": "总结请求过于频繁，请稍后再试。"}, status_code=429)

    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    bundle = str(payload.get("bundle", "")).strip()
    try:
        count = max(0, min(int(payload.get("count", 0)), 200))
    except (TypeError, ValueError):
        count = 0

    if not bundle:
        return JSONResponse({"detail": "缺少待总结的节点内容。"}, status_code=400)
    if count <= 0:
        count = 1

    try:
        summary, model = await _summarize_selection_bundle(bundle=bundle, count=count, user_settings=us)
    except RuntimeError as exc:
        # Business logic errors (e.g., no analysis model)
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("selection_summary failed: user=%s count=%d error=%s", user["user_id"], count, exc)
        await request_logger.log_event(
            {
                "type": "selection_summary_error",
                "user_id": user["user_id"],
                "client_id": client_host,
                "count": count,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        )
        return JSONResponse({"detail": "摘要生成失败，请稍后重试。"}, status_code=502)

    await request_logger.log_event(
        {
            "type": "selection_summary",
            "user_id": user["user_id"],
            "client_id": client_host,
            "count": count,
            "model": model,
            "bundle_length": len(bundle),
            "summary_length": len(summary),
        }
    )
    return JSONResponse({"summary": summary, "model": model, "count": count})


@app.post("/api/conversation-analysis")
async def conversation_analysis(request: Request) -> JSONResponse:
    user, us, err = await _load_user_settings_or_error(request)
    if err:
        return err
    try:
        await _require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    client_host = request.client.host if request.client else "unknown"
    if not await rate_limiter.allow_async(f"conv-analysis:{user['user_id']}:{client_host}", limit=20, window_seconds=300):
        return JSONResponse({"detail": "分析请求过于频繁，请稍后再试。"}, status_code=429)

    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    request_id = str(payload.get("request_id", "")).strip()
    messages = payload.get("messages")

    if not messages or not isinstance(messages, list):
        if request_id:
            data = await database.get_request_with_results(request_id, user["user_id"])
            if not data:
                return JSONResponse({"detail": "未找到对应的会话记录。"}, status_code=404)
            messages = []
            messages.append({"role": "user", "content": data["user_message"]})
            for model_name, rounds in data.get("model_results", {}).items():
                for r in sorted(rounds, key=lambda x: x["round"]):
                    if r.get("content"):
                        messages.append({"role": "assistant", "content": f"[{model_name}] {r['content']}"})
        else:
            return JSONResponse({"detail": "请提供 messages 或 request_id。"}, status_code=400)

    if not messages:
        return JSONResponse({"detail": "会话内容为空。"}, status_code=400)

    try:
        result, model = await _analyze_conversation(messages, user_settings=us)
    except RuntimeError as exc:
        await request_logger.log_event(
            {
                "type": "conversation_analysis_error",
                "user_id": user["user_id"],
                "client_id": client_host,
                "request_id": request_id,
                "error": str(exc),
            }
        )
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception(
            "conversation_analysis failed: user=%s request_id=%s",
            user["user_id"], request_id,
        )
        await request_logger.log_event(
            {
                "type": "conversation_analysis_error",
                "user_id": user["user_id"],
                "client_id": client_host,
                "request_id": request_id,
                "error": str(exc),
            }
        )
        return JSONResponse({"detail": "对话分析失败，请稍后重试。"}, status_code=502)

    await request_logger.log_event(
        {
            "type": "conversation_analysis",
            "user_id": user["user_id"],
            "client_id": client_host,
            "request_id": request_id,
            "model": model,
            "message_count": len(messages),
        }
    )
    return JSONResponse({"analysis": result, "model": model})


# ── Admin routes ──

_ADMIN_RECHARGE_REMARK_MAX = 500
_PRICING_POINTS_MAX = 1e9
_CONFIG_NUM_MAX = 1e12
_RECHARGE_POINTS_ABS_MAX = 1e12
_ADMIN_MODEL_TEST_TIMEOUT_SECONDS = 15
_ADMIN_MODEL_TEST_MAX_PREVIEW_CHARS = 120
_ADMIN_MODEL_TEST_MAX_TOKENS = 16
_ADMIN_MODEL_TEST_RATE_LIMIT = 12
_PREPROCESS_TIMEOUT_SECONDS = 15
_PREPROCESS_ORGANIZE_TIMEOUT_SECONDS = 20
_PREPROCESS_MAX_TOKENS = 512

_CONCLUSION_TIMEOUT_SECONDS = 90
_CONCLUSION_MAX_INPUT_CHARS = 16000
_CONCLUSION_MAX_OUTPUT_CHARS = 8000
_CONCLUSION_PER_MODEL_INPUT_CHARS = 2200
_ESTIMATED_PROMPT_TOKENS_PER_CALL = 900
_ESTIMATED_COMPLETION_TOKENS_PER_CALL = 1800
_ESTIMATED_PROMPT_GROWTH_PER_ROUND = 0.45
_ESTIMATED_CONCLUSION_PROMPT_TOKENS = 2200
_ESTIMATED_CONCLUSION_COMPLETION_TOKENS = 1600
_FALLBACK_POINTS_PER_CALL = 3.0


def _build_effective_user_settings(
    base_settings: dict,
    *,
    api_key: str,
    base_url: str,
    api_format: str | None = None,
) -> dict:
    effective = dict(base_settings)
    effective["api_key"] = api_key
    effective["api_base_url"] = base_url
    if api_format:
        effective["api_format"] = api_format
    effective["extra_headers"] = _sanitize_extra_headers(base_settings.get("extra_headers", {}))
    return effective


async def _calculate_model_points_cost(
    database: "LocalDatabase",
    api_model_id: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    pricing = await database.get_pricing_for_model(api_model_id)
    if pricing:
        return (prompt_tokens / 1000.0) * pricing["input_points_per_1k"] + (completion_tokens / 1000.0) * pricing["output_points_per_1k"]
    return (prompt_tokens + completion_tokens) / 1000.0


async def _estimate_model_call_points(
    database: "LocalDatabase",
    api_model_id: str,
    *,
    round_number: int,
    user_message: str,
) -> float:
    growth = 1.0 + max(0, round_number - 1) * _ESTIMATED_PROMPT_GROWTH_PER_ROUND
    prompt_tokens = int(max(_ESTIMATED_PROMPT_TOKENS_PER_CALL, len(user_message) // 2) * growth)
    completion_tokens = _ESTIMATED_COMPLETION_TOKENS_PER_CALL
    estimated = await _calculate_model_points_cost(
        database,
        api_model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    return max(estimated, _FALLBACK_POINTS_PER_CALL)


async def _estimate_thread_reserve_points(
    database: "LocalDatabase",
    *,
    thread: ThreadState,
    model_id_map: dict[str, str],
    bill_model_tokens: bool,
) -> float:
    reserve = 0.0
    if bill_model_tokens:
        for model in thread.models:
            api_model_id = model_id_map.get(model, model)
            for round_number in range(1, thread.discussion_rounds + 1):
                reserve += await _estimate_model_call_points(
                    database,
                    api_model_id,
                    round_number=round_number,
                    user_message=thread.user_message,
                )
    cfg = await database.get_system_config()
    try:
        search_points = max(0.0, float(cfg.get("config_search_points_per_call", "0") or 0))
    except (TypeError, ValueError):
        search_points = 0.0
    if thread.search_enabled is not False:
        reserve += search_points
    return round(reserve, 4)


async def _estimate_conclusion_reserve_points(
    database: "LocalDatabase",
    *,
    user_settings: dict,
) -> float:
    model_name = _pick_conclusion_model(user_settings.get("models", []))
    if not model_name:
        return _FALLBACK_POINTS_PER_CALL
    model_id = _build_model_id_map(user_settings.get("models", [])).get(model_name, model_name)
    estimated = await _calculate_model_points_cost(
        database,
        model_id,
        prompt_tokens=_ESTIMATED_CONCLUSION_PROMPT_TOKENS,
        completion_tokens=_ESTIMATED_CONCLUSION_COMPLETION_TOKENS,
    )
    return max(estimated, _FALLBACK_POINTS_PER_CALL)


def _parse_optional_user_id_query(raw: str | None) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        uid = int(str(raw).strip(), 10)
    except ValueError as exc:
        raise ValueError("查询参数 user_id 须为正整数。") from exc
    if uid <= 0:
        raise ValueError("查询参数 user_id 须为正整数。")
    return uid


def _normalize_system_config_value(key: str, value: object) -> str | None:
    s = str(value).strip()
    if key == "config_allow_registration":
        low = s.lower()
        if low in ("true", "1", "yes", "on"):
            return "true"
        if low in ("false", "0", "no", "off"):
            return "false"
        return None
    try:
        x = float(s)
    except ValueError:
        return None
    if not math.isfinite(x) or x < 0 or x > _CONFIG_NUM_MAX:
        return None
    if x.is_integer():
        return str(int(x))
    return str(x)


@app.get("/admin")
async def admin_login_page(request: Request) -> Response:
    admin = await _get_admin_user(request)
    if admin:
        role = await database.get_user_role(admin["user_id"])
        if role == "admin":
            return RedirectResponse(url="/admin/dashboard", status_code=303)
        # 有 admin_session 但账号已不是管理员：清掉无效会话并给出说明
        token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
        await auth_manager.logout(token)
        resp = RedirectResponse(url="/admin?error=forbidden", status_code=303)
        resp.delete_cookie(key=ADMIN_SESSION_COOKIE_NAME, path="/")
        return resp
    return HTMLResponse(
        _render_admin_login_html(request),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/admin/dashboard")
async def admin_dashboard_page(request: Request) -> Response:
    try:
        await _require_admin(request)
    except AuthError:
        # 必须带 error，否则用户只看到「又回到登录页」而没有任何红字说明
        return RedirectResponse(url="/admin?error=session", status_code=303)
    return _html_response(ADMIN_STATIC_DIR / "dashboard.html")


@app.post("/admin/session-login")
async def admin_session_login(request: Request) -> Response:
    """浏览器原生表单登录：303 重定向 + Set-Cookie，避免 fetch 跳转时部分环境下 Cookie 未生效。"""
    client_host = request.client.host if request.client else "unknown"
    try:
        await _require_origin(request)
    except OriginError:
        _log_login_failure(
            route="/admin/session-login",
            client_host=client_host,
            username="-",
            reason="origin_denied",
        )
        return RedirectResponse(url="/admin?error=origin", status_code=303)
    if not await rate_limiter.allow_async(f"admin-login:{client_host}", limit=10, window_seconds=600):
        _log_login_failure(
            route="/admin/session-login",
            client_host=client_host,
            username="-",
            reason="rate_limited",
        )
        return RedirectResponse(url="/admin?error=rate", status_code=303)
    try:
        form_data = await _read_admin_session_login_form(request)
    except Exception:
        logger.exception("admin session-login: failed to read form body")
        return RedirectResponse(url="/admin?error=form", status_code=303)

    username = form_data.get("username", "")
    password = form_data.get("password", "")

    # --- captcha / honeypot ---
    if check_honeypot(form_data.get("website")):
        return RedirectResponse(url="/admin?error=badcreds", status_code=303)
    cap_err = captcha_verify(form_data.get("captcha_token", ""), form_data.get("captcha_answer", ""))
    if cap_err:
        return RedirectResponse(url="/admin?error=captcha", status_code=303)
    # --- end captcha ---

    try:
        _user_info, token, _ = await auth_manager.admin_login(username, password)
    except AuthError as exc:
        detail = str(exc)
        if "管理员权限" in detail:
            code = "forbidden"
        elif "用户名需为" in detail or "密码长度" in detail or "至少需要" in detail:
            code = "invalid"
        else:
            code = "badcreds"
        _log_login_failure(
            route="/admin/session-login",
            client_host=client_host,
            username=username,
            reason=f"{code}:{detail}",
        )
        return RedirectResponse(url=f"/admin?error={code}", status_code=303)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        logger.exception("admin session-login: database or filesystem error (%s)", exc)
        return RedirectResponse(url="/admin?error=db", status_code=303)
    except Exception:
        logger.exception("admin session-login failed")
        return RedirectResponse(url="/admin?error=server", status_code=303)

    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request),
        max_age=SESSION_DAYS * 24 * 60 * 60,
        path="/",
    )
    return response


@app.post("/api/admin/login")
async def admin_login(request: Request) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    if not await rate_limiter.allow_async(f"admin-login:{client_host}", limit=10, window_seconds=600):
        _log_login_failure(
            route="/api/admin/login",
            client_host=client_host,
            username="-",
            reason="rate_limited",
        )
        return JSONResponse({"detail": "登录过于频繁。"}, status_code=429)
    username = ""
    try:
        payload = await _parse_json_body(request)
        # --- captcha / honeypot ---
        if check_honeypot(payload.get("website")):
            return JSONResponse({"detail": "请求异常。"}, status_code=400)
        cap_err = captcha_verify(str(payload.get("captcha_token", "")), str(payload.get("captcha_answer", "")))
        if cap_err:
            return JSONResponse({"detail": cap_err}, status_code=400)
        # --- end captcha ---
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        user_info, token, _ = await auth_manager.admin_login(username, password)
    except AuthError as exc:
        _log_login_failure(
            route="/api/admin/login",
            client_host=client_host,
            username=username,
            reason=str(exc),
        )
        return JSONResponse({"detail": str(exc)}, status_code=400)
    response = JSONResponse({"ok": True, "username": user_info["username"]})
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME, value=token, httponly=True,
        samesite="lax", secure=_request_is_https(request),
        max_age=SESSION_DAYS * 24 * 60 * 60, path="/",
    )
    return response


@app.post("/api/admin/logout")
async def admin_logout(request: Request) -> JSONResponse:
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)
    await auth_manager.logout(request.cookies.get(ADMIN_SESSION_COOKIE_NAME))
    response = JSONResponse({"ok": True})
    response.delete_cookie(key=ADMIN_SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/api/admin/users")
async def admin_list_users(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    users = await database.list_users_admin()
    return JSONResponse({"users": users})


@app.post("/api/admin/recharge")
async def admin_recharge(request: Request) -> JSONResponse:
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    try:
        user_id = int(payload.get("user_id", 0))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "用户 ID 格式不正确。"}, status_code=400)
    try:
        points = float(payload.get("points", 0))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "点数格式不正确。"}, status_code=400)
    if not math.isfinite(points) or abs(points) > _RECHARGE_POINTS_ABS_MAX:
        return JSONResponse({"detail": "点数无效或超出允许范围。"}, status_code=400)
    remark = str(payload.get("remark", "")).strip()
    if len(remark) > _ADMIN_RECHARGE_REMARK_MAX:
        remark = remark[:_ADMIN_RECHARGE_REMARK_MAX]
    if user_id <= 0 or points == 0:
        return JSONResponse({"detail": "用户 ID 和点数不能为空。"}, status_code=400)
    if not await database.get_user_by_id(user_id):
        return JSONResponse({"detail": "用户不存在。"}, status_code=404)
    ok = await database.add_points_non_negative(user_id, points, admin["user_id"], remark)
    if not ok:
        current_balance = await database.get_user_balance(user_id)
        return JSONResponse(
            {"detail": f"余额不足，当前余额 {current_balance:.2f}，无法扣减 {abs(points):.2f}。"},
            status_code=400,
        )
    new_balance = await database.get_user_balance(user_id)
    _emit_admin_audit(
        admin["user_id"], "recharge",
        target_user_id=user_id,
        points=points, remark=remark, new_balance=round(new_balance, 2),
    )
    return JSONResponse({"ok": True, "balance": new_balance})


@app.post("/api/admin/set-role")
async def admin_set_role(request: Request) -> JSONResponse:
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    try:
        user_id = int(payload.get("user_id", 0))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "用户 ID 格式不正确。"}, status_code=400)
    if user_id <= 0:
        return JSONResponse({"detail": "用户 ID 无效。"}, status_code=400)
    role = str(payload.get("role", "user")).strip()
    if role not in ("user", "admin"):
        return JSONResponse({"detail": "角色只能是 user 或 admin。"}, status_code=400)
    target = await database.get_user_by_id(user_id)
    if not target:
        return JSONResponse({"detail": "用户不存在。"}, status_code=404)
    if user_id == admin["user_id"] and role == "user":
        return JSONResponse({"detail": "不能取消自己的管理员权限。"}, status_code=400)
    current_role = str(target.get("role") or "user")
    if current_role == "admin" and role == "user":
        if await database.count_users_with_role("admin") <= 1:
            return JSONResponse({"detail": "至少需要保留一名管理员。"}, status_code=400)
    await database.set_user_role(user_id, role)
    _emit_admin_audit(
        admin["user_id"], "set_role",
        target_user_id=user_id,
        new_role=role, prev_role=current_role,
    )
    return JSONResponse({"ok": True})


@app.post("/api/admin/reset-password")
async def admin_reset_password(request: Request) -> JSONResponse:
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    try:
        user_id = int(payload.get("user_id", 0))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "用户 ID 格式不正确。"}, status_code=400)
    if user_id <= 0:
        return JSONResponse({"detail": "用户 ID 无效。"}, status_code=400)
    new_password = str(payload.get("new_password", "")).strip()
    if len(new_password) < 8:
        return JSONResponse({"detail": "密码至少需要 8 位。"}, status_code=400)
    target = await database.get_user_by_id(user_id)
    if not target:
        return JSONResponse({"detail": "用户不存在。"}, status_code=404)
    username = target.get("username", "")
    salt = secrets.token_hex(16)
    password_hash = await asyncio.to_thread(AuthManager._hash_password, new_password, salt)
    await database.update_user_password(username, password_hash, salt)
    _emit_admin_audit(
        admin["user_id"], "reset_password",
        target_user_id=user_id,
    )
    return JSONResponse({"ok": True})


@app.get("/api/admin/pricing")
async def admin_get_pricing(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    pricing = await database.get_all_pricing()
    return JSONResponse({"pricing": pricing})


@app.put("/api/admin/pricing")
async def admin_update_pricing(request: Request) -> JSONResponse:
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    model_id = str(payload.get("model_id", "")).strip()
    display_name = str(payload.get("display_name", model_id)).strip()
    try:
        input_per_1k = float(payload.get("input_points_per_1k", 1.0))
        output_per_1k = float(payload.get("output_points_per_1k", 2.0))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "单价格式不正确。"}, status_code=400)
    if (
        not math.isfinite(input_per_1k)
        or not math.isfinite(output_per_1k)
        or input_per_1k < 0
        or output_per_1k < 0
        or input_per_1k > _PRICING_POINTS_MAX
        or output_per_1k > _PRICING_POINTS_MAX
    ):
        return JSONResponse({"detail": "单价须为有限非负数且不超过上限。"}, status_code=400)
    try:
        is_active = int(payload.get("is_active", 1))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "状态 is_active 须为 0 或 1。"}, status_code=400)
    if is_active not in (0, 1):
        return JSONResponse({"detail": "状态 is_active 须为 0 或 1。"}, status_code=400)
    if not model_id:
        return JSONResponse({"detail": "模型 ID 不能为空。"}, status_code=400)
    await database.upsert_pricing(model_id, display_name, input_per_1k, output_per_1k, is_active)
    _emit_admin_audit(
        admin["user_id"], "upsert_pricing",
        model_id=model_id, input_per_1k=input_per_1k, output_per_1k=output_per_1k,
    )
    return JSONResponse({"ok": True})


@app.delete("/api/admin/pricing/{model_id}")
async def admin_delete_pricing(model_id: str, request: Request) -> JSONResponse:
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    await database.delete_pricing(model_id)
    _emit_admin_audit(admin["user_id"], "delete_pricing", model_id=model_id)
    return JSONResponse({"ok": True})


@app.get("/api/admin/usage")
async def admin_usage_stats(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        uid = _parse_optional_user_id_query(request.query_params.get("user_id"))
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    stats = await database.get_usage_stats(uid)
    return JSONResponse({"stats": stats})


@app.get("/api/admin/recharge-logs")
async def admin_recharge_logs(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        uid = _parse_optional_user_id_query(request.query_params.get("user_id"))
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    logs = await database.get_recharge_logs(uid)
    return JSONResponse({"logs": logs})


@app.get("/api/admin/config")
async def admin_get_config(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    cfg = await database.get_system_config()
    return JSONResponse({"config": cfg})


@app.put("/api/admin/config")
async def admin_update_config(request: Request) -> JSONResponse:
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    allowed_keys = {"config_default_points", "config_low_balance_threshold", "config_allow_registration", "config_search_points_per_call"}
    changes: dict[str, str] = {}
    for key, value in payload.items():
        if key not in allowed_keys:
            continue
        normalized = _normalize_system_config_value(key, value)
        if normalized is None:
            return JSONResponse({"detail": f"配置项 {key} 的值无效。"}, status_code=400)
        await database.set_system_config(key, normalized)
        changes[key] = normalized
    if changes:
        _emit_admin_audit(admin["user_id"], "update_config", **changes)
    return JSONResponse({"ok": True})


@app.get("/api/admin/audit-logs")
async def admin_get_audit_logs(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        limit = min(int(request.query_params.get("limit", "200")), 500)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        return JSONResponse({"detail": "limit/offset 须为整数。"}, status_code=400)
    action_filter = request.query_params.get("action") or None
    logs = await database.get_admin_audit_logs(limit=limit, offset=offset, action_filter=action_filter)
    return JSONResponse({"logs": logs})


@app.get("/api/admin/model-config")
async def admin_get_model_config(request: Request) -> JSONResponse:
    """读取全局模型/API/搜索配置（API Key 脱敏）。"""
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    gs = await _load_global_service_settings()
    return JSONResponse({
        "api_base_url": gs.get("api_base_url", ""),
        "api_format": gs.get("api_format", "openai"),
        "api_key_masked": _mask_key(gs.get("api_key", "")),
        "models": gs.get("models", []),
        "firecrawl_api_key_masked": _mask_key(gs.get("firecrawl_api_key", "")),
        "firecrawl_country": gs.get("firecrawl_country", "CN"),
        "firecrawl_timeout_ms": gs.get("firecrawl_timeout_ms", 45000),
        "preprocess_model": gs.get("preprocess_model", ""),
        "user_api_base_url": gs.get("user_api_base_url", ""),
        "user_api_format": gs.get("user_api_format", "openai"),
        "extra_params": gs.get("extra_params", {}),
        "extra_headers": _sanitize_extra_headers(gs.get("extra_headers", {})),
    })


@app.put("/api/admin/model-config")
async def admin_update_model_config(request: Request) -> JSONResponse:
    """更新全局模型/API/搜索配置。"""
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    api_base_url = str(payload.get("api_base_url", "")).strip()
    api_format = str(payload.get("api_format", "openai")).strip()
    api_key_raw = str(payload.get("api_key", "")).strip()
    models_raw = payload.get("models", [])
    firecrawl_key_raw = str(payload.get("firecrawl_api_key", "")).strip()
    firecrawl_country = str(payload.get("firecrawl_country", "CN")).strip() or "CN"
    try:
        firecrawl_timeout = max(5000, min(int(payload.get("firecrawl_timeout_ms", 45000)), 120000))
    except (TypeError, ValueError):
        firecrawl_timeout = 45000

    if not api_base_url:
        return JSONResponse({"detail": "请填写 API 地址。"}, status_code=400)
    if api_format not in ("openai", "anthropic"):
        return JSONResponse({"detail": "API 格式仅支持 openai 或 anthropic。"}, status_code=400)

    models: list[dict[str, str]] = []
    if isinstance(models_raw, list):
        for item in models_raw:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                mid = str(item.get("id", "")).strip()
                if name and mid:
                    models.append({"name": name, "id": mid})
    if not models:
        return JSONResponse({"detail": "请至少添加一个模型。"}, status_code=400)

    # 留空 key 时保留已有值
    existing = await _load_global_service_settings()
    if not api_key_raw:
        api_key_raw = existing.get("api_key", "")
    if not api_key_raw:
        return JSONResponse({"detail": "请填写 API Key。"}, status_code=400)
    if not firecrawl_key_raw:
        firecrawl_key_raw = existing.get("firecrawl_api_key", "")

    preprocess_model = str(payload.get("preprocess_model", "")).strip()
    extra_params_raw = payload.get("extra_params", {})
    if not isinstance(extra_params_raw, dict):
        extra_params_raw = {}
    extra_headers_raw = _sanitize_extra_headers(payload.get("extra_headers", {}))
    user_api_base_url = str(payload.get("user_api_base_url", "")).strip()
    user_api_format = str(payload.get("user_api_format", "openai")).strip()
    if user_api_format not in ("openai", "anthropic"):
        user_api_format = "openai"

    await database.set_global_model_config(
        api_base_url=api_base_url,
        api_format=api_format,
        api_key=api_key_raw,
        models_json=_json.dumps(models, ensure_ascii=False),
        firecrawl_api_key=firecrawl_key_raw,
        firecrawl_country=firecrawl_country,
        firecrawl_timeout_ms=firecrawl_timeout,
        preprocess_model=preprocess_model,
        user_api_base_url=user_api_base_url,
        user_api_format=user_api_format,
        extra_params=extra_params_raw,
        extra_headers=extra_headers_raw,
    )
    _emit_admin_audit(
        admin["user_id"], "update_model_config",
        api_base_url=api_base_url, api_format=api_format, model_count=len(models),
        preprocess_model=preprocess_model,
        extra_headers=_summarize_extra_headers(extra_headers_raw),
    )
    return JSONResponse({"ok": True})


@app.post("/api/admin/model-config/test")
async def admin_test_model_config(request: Request) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if not await rate_limiter.allow_async(
        f"admin-model-test:{admin['user_id']}:{client_host}",
        limit=_ADMIN_MODEL_TEST_RATE_LIMIT,
        window_seconds=60,
    ):
        return JSONResponse({"detail": "测试过于频繁，请稍后再试。"}, status_code=429)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    model_name = str(payload.get("model_name", "")).strip()
    model_id = str(payload.get("model_id", "")).strip()
    gs = await _load_global_service_settings()
    api_key = str(gs.get("api_key", "")).strip()
    api_base_url = str(gs.get("api_base_url", "")).strip()
    if not api_base_url or not api_key:
        return JSONResponse({"detail": "请先保存 API 地址和 API Key 后再测试。"}, status_code=400)

    selected = _pick_saved_model_for_test(gs.get("models", []), model_name=model_name, model_id=model_id)
    if not selected:
        return JSONResponse({"detail": "模型不存在或尚未保存，请先保存模型配置。"}, status_code=400)
    selected_name, selected_id = selected

    llm = create_llm_client(
        gs.get("api_format", "openai"),
        api_key=api_key,
        base_url=api_base_url,
        default_headers=_build_openai_default_headers(gs.get("extra_headers", {})),
    )
    started_at = monotonic()
    try:
        llm_resp = await asyncio.wait_for(
            llm.complete(
                model=selected_id,
                messages=[
                    {"role": "system", "content": "You are a concise assistant."},
                    {"role": "user", "content": "Reply with exactly: pong"},
                ],
                temperature=0,
                max_tokens=_ADMIN_MODEL_TEST_MAX_TOKENS,
            ),
            timeout=_ADMIN_MODEL_TEST_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        _emit_admin_audit(
            admin["user_id"],
            "test_model_connectivity",
            model_name=selected_name,
            model_id=selected_id,
            status="timeout",
            timeout_s=_ADMIN_MODEL_TEST_TIMEOUT_SECONDS,
        )
        return JSONResponse(
            {"detail": f"模型测试超时（>{_ADMIN_MODEL_TEST_TIMEOUT_SECONDS}s），请检查接口连通性。"},
            status_code=504,
        )
    except Exception as exc:
        error_type = exc.__class__.__name__
        logger.warning(
            "admin model connectivity test failed model_id=%s error_type=%s",
            selected_id,
            error_type,
            exc_info=True,
        )
        _emit_admin_audit(
            admin["user_id"],
            "test_model_connectivity",
            model_name=selected_name,
            model_id=selected_id,
            status="failed",
            error_type=error_type,
        )
        return JSONResponse(
            {"detail": "模型接口调用失败，请检查 API 地址、API Key 与模型 ID 是否有效。"},
            status_code=502,
        )

    latency_ms = int(max(0, round((monotonic() - started_at) * 1000)))
    preview = llm_resp.text
    if len(preview) > _ADMIN_MODEL_TEST_MAX_PREVIEW_CHARS:
        preview = preview[: _ADMIN_MODEL_TEST_MAX_PREVIEW_CHARS - 1] + "…"
    usage = {
        "prompt_tokens": llm_resp.usage.prompt_tokens,
        "completion_tokens": llm_resp.usage.completion_tokens,
        "total_tokens": llm_resp.usage.total_tokens,
    } if llm_resp.usage.total_tokens > 0 else None

    audit_detail: dict[str, object] = {
        "model_name": selected_name,
        "model_id": selected_id,
        "status": "ok",
        "latency_ms": latency_ms,
        "preview_length": len(preview),
    }
    if usage:
        audit_detail.update(usage)
    _emit_admin_audit(admin["user_id"], "test_model_connectivity", **audit_detail)

    return JSONResponse(
        {
            "ok": True,
            "model_name": selected_name,
            "model_id": selected_id,
            "latency_ms": latency_ms,
            "preview": preview,
            "usage": usage,
        }
    )


@app.get("/api/canvases")
async def list_canvases(request: Request) -> JSONResponse:
    user = await _get_request_user(request)
    if not user:
        return _unauthorized_json()
    canvases = await database.get_canvases(user["user_id"])
    return JSONResponse({"canvases": canvases})


@app.post("/api/canvases")
async def create_canvas(request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        await _require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    name = str(payload.get("name", "")).strip() or "新画布"
    canvas_id = await database.create_canvas(user["user_id"], name)
    return JSONResponse({"canvas_id": canvas_id, "name": name})


@app.patch("/api/canvases/{canvas_id}")
async def rename_canvas(canvas_id: str, request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        await _require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    name = str(payload.get("name", "")).strip()
    if not name:
        return JSONResponse({"detail": "名称不能为空。"}, status_code=400)
    ok = await database.rename_canvas(canvas_id, user["user_id"], name)
    if not ok:
        return JSONResponse({"detail": "画布不存在。"}, status_code=404)
    return JSONResponse({"ok": True})


@app.delete("/api/canvases/{canvas_id}")
async def delete_canvas(canvas_id: str, request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        await _require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)
    ok = await database.delete_canvas(canvas_id, user["user_id"])
    if not ok:
        return JSONResponse({"detail": "画布不存在。"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/api/canvases/{canvas_id}/state")
async def get_canvas_state(canvas_id: str, request: Request) -> JSONResponse:
    user = await _get_request_user(request)
    if not user:
        return _unauthorized_json()
    state = await database.get_canvas_state(canvas_id, user["user_id"])
    if state is None:
        return JSONResponse({"detail": "画布不存在。"}, status_code=404)
    return JSONResponse(state)


@app.put("/api/cluster-positions/{request_id}")
async def save_cluster_position(request_id: str, request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        await _require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    try:
        user_x = float(payload.get("user_x", 0))
        user_y = float(payload.get("user_y", 0))
        model_y = float(payload.get("model_y", 0))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "坐标格式错误。"}, status_code=400)
    ok = await database.upsert_cluster_position(request_id, user["user_id"], user_x, user_y, model_y)
    if not ok:
        return JSONResponse({"detail": "请求不存在或无权限。"}, status_code=404)
    return JSONResponse({"ok": True})


@app.websocket("/ws/chat")
async def chat_socket(websocket: WebSocket) -> None:
    if not is_configured():
        await websocket.close(code=4503)
        return

    if not _is_origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=4403)
        return

    user = await _get_websocket_user(websocket)
    if not user:
        _t = asyncio.create_task(request_logger.emit(
            "ws_auth_failed",
            level="warning",
            client_host=websocket.client.host if websocket.client else "unknown",
            data={"reason": "no_valid_session"},
        ))
        _http_log_tasks.add(_t); _t.add_done_callback(_http_log_tasks.discard)
        await websocket.close(code=4401)
        return

    await websocket.accept()

    client_host = websocket.client.host if websocket.client else "unknown"
    client_port = websocket.client.port if websocket.client else "unknown"
    client_id = f"{user['username']}@{client_host}:{client_port}"

    user_settings = await _load_global_service_settings()
    needs_setup = not user_settings.get("api_key")
    if needs_setup:
        logger.warning(
            "ws_needs_setup: client=%s api_key_empty=%s base_url=%r models_count=%d",
            client_id, not user_settings.get("api_key"), user_settings.get("api_base_url", ""), len(user_settings.get("models", [])),
        )
        user_settings = {"models": [], "api_key": "", "api_base_url": "", "firecrawl_api_key": "", "firecrawl_country": "CN", "firecrawl_timeout_ms": 45000}
    else:
        logger.info(
            "ws_config_loaded: client=%s base_url=%r models=%s api_key_len=%d",
            client_id, user_settings.get("api_base_url", ""),
            [m.get("name", m.get("id", "?")) for m in user_settings.get("models", [])],
            len(user_settings.get("api_key", "")),
        )

    using_custom_key = False
    user_keys_data = await database.get_user_custom_keys(user["user_id"])
    if user_keys_data["use_custom_key"] and user_keys_data["model_keys"]:
        user_api_base = user_settings.get("user_api_base_url", "")
        if user_api_base:
            using_custom_key = True
            effective_api_key = user_keys_data["model_keys"].get("default", "")
            if not effective_api_key:
                first_key = next(iter(user_keys_data["model_keys"].values()), "")
                effective_api_key = first_key
            effective_base_url = user_api_base
        else:
            effective_api_key = user_settings.get("api_key", "")
            effective_base_url = user_settings.get("api_base_url", "")
    else:
        effective_api_key = user_settings.get("api_key", "")
        effective_base_url = user_settings.get("api_base_url", "")
    effective_user_settings = _build_effective_user_settings(
        user_settings,
        api_key=effective_api_key,
        base_url=effective_base_url,
        api_format=user_settings.get("user_api_format") if using_custom_key else user_settings.get("api_format"),
    )

    effective_api_format = user_settings.get("user_api_format") if using_custom_key else user_settings.get("api_format", "openai")
    service = MultiModelChatService(
        api_key=effective_api_key,
        base_url=effective_base_url,
        models=user_settings.get("models", []),
        api_format=effective_api_format or "openai",
        request_logger=request_logger,
        database=database,
        extra_params=user_settings.get("extra_params") or {},
        extra_headers=user_settings.get("extra_headers") or {},
    ) if not needs_setup else None
    search_service = FirecrawlSearchService(
        api_key=user_settings.get("firecrawl_api_key", ""),
        country=user_settings.get("firecrawl_country", "CN"),
        timeout_ms=user_settings.get("firecrawl_timeout_ms", 45000),
    )
    threads: dict[str, ThreadState] = {}
    request_tasks: dict[str, asyncio.Task[None]] = {}
    background_tasks: set[asyncio.Task[object]] = set()

    def create_background_task(coro) -> None:
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    async def wait_for_background_tasks() -> None:
        if not background_tasks:
            return
        await asyncio.gather(*list(background_tasks), return_exceptions=True)
        background_tasks.clear()

    async def run_thread(thread: ThreadState) -> None:
        try:
            await service._send_event(
                websocket,
                {
                    "type": "user",
                    "request_id": thread.request_id,
                    "content": thread.meta.get("display_message", thread.user_message),
                    "discussion_rounds": thread.discussion_rounds,
                    "models": thread.models,
                    "search_enabled": thread.search_enabled,
                    "think_enabled": thread.think_enabled,
                    "parent_request_id": thread.parent_request_id,
                    "source_model": thread.source_model,
                    "source_round": thread.source_round,
                },
            )
            await database.record_chat_request(
                request_id=thread.request_id,
                client_id=client_id,
                models=thread.models,
                user_message=thread.user_message,
                discussion_rounds=thread.discussion_rounds,
                search_enabled=thread.search_enabled is not False,
                think_enabled=thread.think_enabled,
                parent_request_id=thread.parent_request_id,
                source_model=thread.source_model,
                source_round=thread.source_round,
                status="queued",
                canvas_id=thread.canvas_id,
                user_id=user["user_id"],
            )
            create_background_task(
                request_logger.emit(
                    "chat_request",
                    level="info",
                    request_id=thread.request_id,
                    client_id=client_id,
                    user_id=user["user_id"],
                    data={
                        "models": thread.models,
                        "message_length": len(thread.user_message),  # 不记录原文防隐私泄露
                        "discussion_rounds": thread.discussion_rounds,
                        "search_enabled": thread.search_enabled,
                        "think_enabled": thread.think_enabled,
                        "parent_request_id": thread.parent_request_id,
                        "source_model": thread.source_model,
                        "source_round": thread.source_round,
                    },
                )
            )
            await _prepare_thread_for_stream(
                thread=thread,
                websocket=websocket,
                search_service=search_service,
                database=database,
                client_id=client_id,
                user_settings=effective_user_settings,
                send_event=service._send_event,
                user_id=user["user_id"],
            )
            create_background_task(database.mark_request_status(thread.request_id, "streaming"))
            results = await service.stream_round(
                histories=thread.histories,
                websocket=websocket,
                request_id=thread.request_id,
                client_id=client_id,
                user_message=thread.user_message,
                discussion_rounds=thread.discussion_rounds,
            )
            await service._send_event(websocket, {"type": "round_complete", "request_id": thread.request_id})
            create_background_task(
                request_logger.log_event(
                    {
                        "type": "chat_round_complete",
                        "request_id": thread.request_id,
                        "client_id": client_id,
                        "message_length": len(thread.user_message),
                        "discussion_rounds": thread.discussion_rounds,
                        "search_enabled": thread.search_enabled,
                        "think_enabled": thread.think_enabled,
                        "models": thread.models,
                        "results": [
                            {key: value for key, value in item.items() if key != "content"}
                            for item in results
                        ],
                    }
                )
            )
            create_background_task(database.mark_request_status(thread.request_id, "completed"))

            successful_results = [r for r in results if r.get("status") == "success" and r.get("content")]
            if successful_results:
                create_background_task(
                    _generate_conclusion(
                        user_settings=effective_user_settings,
                        results=results,
                        user_message=thread.user_message,
                        request_id=thread.request_id,
                        canvas_id=thread.canvas_id,
                        websocket=websocket,
                        send_event=service._send_event,
                        db=database,
                        user_id=user["user_id"],
                        bill_points=not using_custom_key,
                    )
                )

            actual_points_spent = thread.charged_search_points
            for r in results:
                if r.get("status") != "success":
                    continue
                pt = r.get("prompt_tokens", 0)
                ct = r.get("completion_tokens", 0)
                tt = r.get("total_tokens", 0) or (pt + ct)
                model_name = r.get("model", "")
                api_model_id = service.model_id_map.get(model_name, model_name)
                if using_custom_key:
                    await database.record_token_usage(
                        user_id=user["user_id"], request_id=thread.request_id,
                        model=model_name, round_number=r.get("round", 1),
                        prompt_tokens=pt, completion_tokens=ct,
                        total_tokens=tt, points_consumed=0.0,
                    )
                else:
                    points_cost = await _calculate_model_points_cost(
                        database,
                        api_model_id,
                        prompt_tokens=pt,
                        completion_tokens=ct,
                    )
                    actual_points_spent += points_cost
                    await database.record_token_usage(
                        user_id=user["user_id"], request_id=thread.request_id,
                        model=model_name, round_number=r.get("round", 1),
                        prompt_tokens=pt, completion_tokens=ct,
                        total_tokens=tt, points_consumed=points_cost,
                    )
            if not using_custom_key and thread.reserved_points > 0:
                refund = max(0.0, thread.reserved_points - actual_points_spent)
                extra_due = max(0.0, actual_points_spent - thread.reserved_points)
                if extra_due > 0:
                    extra_ok = await database.deduct_points(user["user_id"], extra_due)
                    if not extra_ok:
                        logger.warning(
                            "extra_points_charge_failed user=%s request_id=%s amount=%.4f",
                            user["user_id"], thread.request_id, extra_due,
                        )
                if refund > 0:
                    await database.add_points(user["user_id"], refund, user["user_id"], "请求预扣返还")
            new_balance = await database.get_user_balance(user["user_id"])
            for r in results:
                if r.get("status") != "success":
                    continue
                model_name = r.get("model", "")
                api_model_id = service.model_id_map.get(model_name, model_name)
                pt = r.get("prompt_tokens", 0)
                ct = r.get("completion_tokens", 0)
                points_cost = await _calculate_model_points_cost(
                    database,
                    api_model_id,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                )
                await service._send_event(websocket, {
                    "type": "usage",
                    "request_id": thread.request_id,
                    "model": model_name,
                    "round": r.get("round", 1),
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "points": round(points_cost, 4),
                    "balance": round(new_balance, 2),
                })

        except asyncio.CancelledError:
            if not using_custom_key and thread.reserved_points > 0:
                refund = max(0.0, thread.reserved_points - thread.charged_search_points)
                if refund > 0:
                    await database.add_points(user["user_id"], refund, user["user_id"], "请求取消预扣返还")
            await service._send_event(
                websocket,
                {"type": "cancelled", "request_id": thread.request_id, "content": "已取消当前请求。"},
            )
            create_background_task(database.mark_request_status(thread.request_id, "cancelled"))
            create_background_task(
                database.record_event(
                    event_type="request_cancelled",
                    request_id=thread.request_id,
                    client_id=client_id,
                    payload={"request_id": thread.request_id, "reason": "cancelled"},
                )
            )
            raise
        except Exception as exc:
            if not using_custom_key and thread.reserved_points > 0:
                refund = max(0.0, thread.reserved_points - thread.charged_search_points)
                if refund > 0:
                    await database.add_points(user["user_id"], refund, user["user_id"], "请求失败预扣返还")
            await service._send_event(
                websocket,
                {"type": "error", "request_id": thread.request_id, "content": f"请求失败：{exc}"},
            )
            create_background_task(database.mark_request_status(thread.request_id, "failed"))
            create_background_task(
                database.record_event(
                    event_type="request_failed",
                    request_id=thread.request_id,
                    client_id=client_id,
                    payload={"request_id": thread.request_id, "error": str(exc)},
                )
            )
        finally:
            request_tasks.pop(thread.request_id, None)

    user_models = service.models if service else []
    await request_logger.log_event(
        {
            "type": "ws_connect",
            "client_id": client_id,
            "models": user_models,
            "search_enabled": search_service.enabled,
        }
    )
    pending_requests = _get_user_pending_request_ids(user["user_id"])
    await websocket.send_json(
        {
            "type": "meta",
            "models": user_models,
            "analysis_model": _pick_analysis_model(user_settings.get("models", [])),
            "search_available": search_service.enabled,
            "preprocess_available": bool(user_settings.get("preprocess_model", "")),
            "using_custom_key": using_custom_key,
            "needs_setup": needs_setup,
            "username": user["username"],
            "balance": round(await database.get_user_balance(user["user_id"]), 2),
            "pending_requests": pending_requests,
        }
    )

    _WS_REAUTH_INTERVAL = 300  # 每 5 分钟重验证一次 session，检测吊销/降权
    _last_reauth_at = monotonic()

    try:
        while True:
            payload = await websocket.receive_json()
            # 定期重验证 session（检测 session 吊销或权限变更）
            now_mono = monotonic()
            if now_mono - _last_reauth_at >= _WS_REAUTH_INTERVAL:
                _last_reauth_at = now_mono
                fresh = await _get_websocket_user(websocket)
                if not fresh:
                    await websocket.send_json({"type": "error", "content": "登录已失效，请刷新页面重新登录。"})
                    await websocket.close(code=4401)
                    return
            if not await rate_limiter.allow_async(f"ws-action:{user['username']}", limit=180, window_seconds=60):
                await websocket.send_json({"type": "error", "content": "请求过于频繁，请稍后再试。"})
                continue

            action = payload.get("action")

            if action == "clear":
                canvas_id_to_clear = str(payload.get("canvas_id", "")).strip()
                cancelled_tasks = _cancel_all_user_tasks(user["user_id"])
                if cancelled_tasks:
                    await asyncio.gather(*cancelled_tasks, return_exceptions=True)
                request_tasks.clear()
                await wait_for_background_tasks()
                threads.clear()
                if canvas_id_to_clear:
                    await database.clear_canvas_requests(canvas_id_to_clear, user["user_id"])
                await request_logger.emit(
                    "ws_clear",
                    level="info",
                    user_id=user["user_id"],
                    client_id=client_id,
                    data={"models": user_models},
                )
                create_background_task(database.record_event(
                    event_type="ws_clear",
                    client_id=client_id,
                    payload={"models": user_models},
                ))
                await websocket.send_json({"type": "cleared"})
                continue

            if action == "cancel_request":
                request_id = str(payload.get("request_id", "")).strip()
                cancelled = _cancel_user_task(user["user_id"], request_id)
                if not cancelled:
                    await websocket.send_json({"type": "error", "request_id": request_id, "content": "未找到可取消的请求。"})
                    continue
                await websocket.send_json({"type": "cancel_requested", "request_id": request_id})
                continue

            if needs_setup or service is None:
                await websocket.send_json({"type": "error", "content": "请先在设置中配置 API 连接信息。"})
                continue

            _MAX_CONCURRENT_REQUESTS = 3
            active_count = len(_get_user_pending_request_ids(user["user_id"]))
            if active_count >= _MAX_CONCURRENT_REQUESTS:
                await websocket.send_json({"type": "error", "content": f"同时运行的请求已达上限（{_MAX_CONCURRENT_REQUESTS}），请等待当前对话完成。"})
                continue

            user_balance = await database.get_user_balance(user["user_id"])
            if user_balance <= 0:
                await websocket.send_json({"type": "error", "content": "点数余额不足，请联系管理员充值。"})
                continue

            if action == "retry_conclusion":
                source_request_id = str(payload.get("source_request_id", "")).strip()
                if not source_request_id:
                    await websocket.send_json({"type": "error", "content": "缺少 request_id，无法重试结论。"})
                    continue
                source_data = await database.get_request_with_results(source_request_id, user["user_id"])
                if not source_data:
                    await websocket.send_json({"type": "error", "content": "未找到可重试结论的会话。"})
                    continue
                latest_results = _collect_latest_success_results_from_map(source_data.get("model_results", {}))
                if not latest_results:
                    await websocket.send_json({"type": "error", "content": "缺少可用的最终轮结果，无法重试结论。"})
                    continue
                canvas_id = str(payload.get("canvas_id", "")).strip() or None
                create_background_task(
                    _generate_conclusion(
                        user_settings=effective_user_settings,
                        results=[
                            {
                                "model": r["model"],
                                "round": r["round"],
                                "status": "success",
                                "content": r["content"],
                            }
                            for r in latest_results
                        ],
                        user_message=str(source_data.get("user_message", "")),
                        request_id=source_request_id,
                        canvas_id=canvas_id,
                        websocket=websocket,
                        send_event=service._send_event,
                        db=database,
                        user_id=user["user_id"],
                        bill_points=not using_custom_key,
                    )
                )
                await websocket.send_json({"type": "conclusion_retry_queued", "request_id": source_request_id})
                continue

            if action == "chat":
                thread = await _build_main_thread(payload=payload, service=service, websocket=websocket)
            elif action == "branch_chat":
                thread = await _build_branch_thread(
                    payload=payload,
                    threads=threads,
                    websocket=websocket,
                    database=database,
                    user_id=user["user_id"],
                )
            elif action == "retry_model":
                thread = await _build_retry_thread(
                    payload=payload,
                    threads=threads,
                    websocket=websocket,
                    database=database,
                    user_id=user["user_id"],
                )
            else:
                await request_logger.emit(
                    "ws_unsupported_action",
                    level="warning",
                    client_id=client_id,
                    user_id=user["user_id"],
                    data={"action": str(payload.get("action", ""))},  # 不记录完整 payload 防敏感信息泄露
                )
                await websocket.send_json({"type": "error", "content": "Unsupported action."})
                continue

            if thread is None:
                continue

            reserve_points = await _estimate_thread_reserve_points(
                database,
                thread=thread,
                model_id_map=service.model_id_map,
                bill_model_tokens=not using_custom_key,
            )
            if reserve_points > 0 and not await database.deduct_points(user["user_id"], reserve_points):
                await websocket.send_json({"type": "error", "content": f"余额不足，当前请求至少需要预留 {reserve_points:.2f} 点。"})
                continue
            thread.reserved_points = reserve_points

            threads[thread.request_id] = thread
            task = asyncio.create_task(run_thread(thread))
            request_tasks[thread.request_id] = task
            _register_user_task(user["user_id"], thread.request_id, task)
    except WebSocketDisconnect:
        await request_logger.log_event(
            {
                "type": "ws_disconnect",
                "client_id": client_id,
                "pending_tasks": len([t for t in request_tasks.values() if not t.done()]),
            }
        )
        return


async def _build_main_thread(
    payload: dict[str, object],
    service: MultiModelChatService,
    websocket: WebSocket,
) -> ThreadState | None:
    message = str(payload.get("message", "")).strip()
    if not message:
        await websocket.send_json({"type": "error", "content": "消息不能为空。"})
        return None
    if len(message) > 4000:
        await websocket.send_json({"type": "error", "content": "消息过长，请控制在 4000 字以内。"})
        return None

    request_id = uuid4().hex
    discussion_rounds = _parse_discussion_rounds(payload.get("discussion_rounds"))
    think_enabled = _parse_bool(payload.get("think_enabled"), False)
    search_enabled = _parse_search_mode(payload.get("search_enabled"))
    canvas_id = str(payload.get("canvas_id", "")).strip() or None

    histories: dict[str, list[dict[str, str]]] = {}
    for model in service.models:
        histories[model] = _build_initial_history(
            user_message=message,
            think_enabled=think_enabled,
            search_bundle=None,
            model=model,
        )

    return ThreadState(
        request_id=request_id,
        models=list(service.models),
        histories=histories,
        user_message=message,
        discussion_rounds=discussion_rounds,
        search_enabled=search_enabled,
        think_enabled=think_enabled,
        search_bundle=None,
        canvas_id=canvas_id,
    )


async def _build_branch_thread(
    payload: dict[str, object],
    threads: dict[str, ThreadState],
    websocket: WebSocket,
    database: LocalDatabase,
    user_id: int,
) -> ThreadState | None:
    message = str(payload.get("message", "")).strip()
    parent_request_id = str(payload.get("source_request_id", "")).strip()
    source_model = str(payload.get("source_model", "")).strip()
    source_round = _parse_source_round(payload.get("source_round"))
    canvas_id = str(payload.get("canvas_id", "")).strip() or None

    if not message:
        await websocket.send_json({"type": "error", "content": "分支内容不能为空。"})
        return None
    if len(message) > 4000:
        await websocket.send_json({"type": "error", "content": "分支内容过长，请控制在 4000 字以内。"})
        return None

    if parent_request_id not in threads:
        loaded = await _rebuild_thread_from_db(parent_request_id, database, user_id)
        if loaded is None:
            await websocket.send_json({"type": "error", "content": "未找到分支来源会话。"})
            return None
        threads[parent_request_id] = loaded

    parent_thread = threads[parent_request_id]
    if source_model not in parent_thread.histories:
        await websocket.send_json({"type": "error", "content": "未找到分支来源模型。"})
        return None

    request_id = uuid4().hex
    discussion_rounds = _parse_discussion_rounds(payload.get("discussion_rounds"))
    think_enabled = _parse_bool(payload.get("think_enabled"), False)
    search_enabled = _parse_search_mode(payload.get("search_enabled"))
    branch_prompt = BRANCH_PROMPT_TEMPLATE.format(source_round=source_round)
    parent_history = parent_thread.histories[source_model]
    inherited_history = _clone_history_until_round(parent_history, source_round)

    branch_history = [dict(item) for item in inherited_history]
    if think_enabled:
        branch_history.append({"role": "system", "content": THINK_PROMPT})
    branch_history.append({"role": "user", "content": f"{branch_prompt}\n\n用户分支指令：{message}"})

    return ThreadState(
        request_id=request_id,
        models=[source_model],
        histories={source_model: branch_history},
        user_message=message,
        discussion_rounds=discussion_rounds,
        search_enabled=search_enabled,
        think_enabled=think_enabled,
        search_bundle=None,
        parent_request_id=parent_request_id,
        source_model=source_model,
        source_round=source_round,
        canvas_id=canvas_id,
    )


async def _build_retry_thread(
    payload: dict[str, object],
    threads: dict[str, ThreadState],
    websocket: WebSocket,
    database: LocalDatabase,
    user_id: int,
) -> ThreadState | None:
    parent_request_id = str(payload.get("source_request_id", "")).strip()
    source_model = str(payload.get("source_model", "")).strip()
    source_round = _parse_source_round(payload.get("source_round"))
    canvas_id = str(payload.get("canvas_id", "")).strip() or None

    if parent_request_id not in threads:
        loaded = await _rebuild_thread_from_db(parent_request_id, database, user_id)
        if loaded is None:
            await websocket.send_json({"type": "error", "content": "未找到重试来源会话。"})
            return None
        threads[parent_request_id] = loaded

    parent_thread = threads[parent_request_id]
    if source_model not in parent_thread.histories:
        await websocket.send_json({"type": "error", "content": "未找到重试来源模型。"})
        return None

    retry_history = _clone_history_before_assistant_round(parent_thread.histories[source_model], source_round)
    if retry_history is None:
        await websocket.send_json({"type": "error", "content": "未找到可重试的轮次内容。"})
        return None

    request_id = uuid4().hex
    return ThreadState(
        request_id=request_id,
        models=[source_model],
        histories={source_model: retry_history},
        user_message=f"重试 {source_model} 第 {source_round} 轮",
        discussion_rounds=1,
        search_enabled=False,
        think_enabled=parent_thread.think_enabled,
        search_bundle=None,
        parent_request_id=parent_request_id,
        source_model=source_model,
        source_round=source_round,
        canvas_id=canvas_id,
        meta={"display_message": f"重试 {source_model} · 第 {source_round} 轮"},
    )


async def _rebuild_thread_from_db(request_id: str, database: LocalDatabase, user_id: int) -> ThreadState | None:
    data = await database.get_request_with_results(request_id, user_id)
    if not data:
        return None
    events = await database.get_request_events(request_id, user_id)
    search_system_prompt = ""
    for event in reversed(events):
        payload = event.get("payload") or {}
        if event.get("event_type") == "search_organized":
            search_system_prompt = str(payload.get("organized_markdown", "")).strip()
            break
        if event.get("event_type") == "search_complete":
            results = payload.get("results", [])
            if isinstance(results, list) and results:
                items: list[SearchItem] = []
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    items.append(
                        SearchItem(
                            title=str(item.get("title", "")).strip() or "Untitled",
                            url=str(item.get("url", "")).strip(),
                            snippet=str(item.get("snippet", "")).strip(),
                            markdown_excerpt=str(item.get("snippet", "")).strip(),
                            rank=int(item.get("rank", len(items) + 1) or (len(items) + 1)),
                        )
                    )
                if items:
                    search_system_prompt = SearchBundle(
                        query=str(payload.get("query", data["user_message"])),
                        items=items,
                    ).as_prompt_block()
                    break
    histories: dict[str, list[dict[str, str]]] = {}
    model_results_map = data["model_results"]
    total_rounds = max(int(data.get("discussion_rounds") or 1), 1)
    for model in data["models"]:
        model_rounds = sorted(model_results_map.get(model, []), key=lambda x: x["round"])
        history: list[dict[str, str]] = [
            {"role": "system", "content": f"{BASE_SYSTEM_PROMPT}\n当前模型标识：{model}。"}
        ]
        if data["think_enabled"]:
            history.append({"role": "system", "content": THINK_PROMPT})
        if search_system_prompt:
            history.append({"role": "system", "content": search_system_prompt})
        history.append({"role": "user", "content": data["user_message"]})
        for i, round_data in enumerate(model_rounds):
            if round_data["content"]:
                history.append({"role": "assistant", "content": round_data["content"]})
                if i < len(model_rounds) - 1:
                    next_round = int(round_data["round"]) + 1
                    round_inputs = {
                        peer_model: peer_rounds[-1]["content"]
                        for peer_model, peer_rounds in (
                            (
                                peer_model,
                                [row for row in rows if int(row.get("round", 0) or 0) == int(round_data["round"]) and row.get("status") == "success" and row.get("content")],
                            )
                            for peer_model, rows in model_results_map.items()
                        )
                        if peer_rounds
                    }
                    prompt = MultiModelChatService._build_discussion_prompt(
                        current_model=model,
                        round_inputs=round_inputs,
                        round_number=next_round,
                        total_rounds=total_rounds,
                    )
                    history.append({"role": "user", "content": prompt})
        histories[model] = history
    return ThreadState(
        request_id=request_id,
        models=data["models"],
        histories=histories,
        user_message=data["user_message"],
        discussion_rounds=data["discussion_rounds"],
        search_enabled=data["search_enabled"],
        think_enabled=data["think_enabled"],
        parent_request_id=data["parent_request_id"],
        source_model=data["source_model"],
        source_round=data["source_round"],
    )


def _build_initial_history(
    user_message: str,
    think_enabled: bool,
    search_bundle: SearchBundle | None,
    model: str,
) -> list[dict[str, str]]:
    history: list[dict[str, str]] = [
        {
            "role": "system",
            "content": f"{BASE_SYSTEM_PROMPT}\n当前模型标识：{model}。",
        }
    ]
    if think_enabled:
        history.append({"role": "system", "content": THINK_PROMPT})
    if search_bundle:
        history.append({"role": "system", "content": search_bundle.as_prompt_block()})
    history.append({"role": "user", "content": user_message})
    return history


_PREPROCESS_ANALYZE_PROMPT = (
    "你是搜索预处理助手。分析用户的问题，判断是否需要联网搜索来获取最新或外部信息。\n"
    "请严格以 JSON 格式回复（不要输出其他内容）：\n"
    '{"need_search": true或false, "keywords": ["搜索关键词1", "搜索关键词2"], "reason": "简要说明"}\n\n'
    "判断原则：\n"
    "- 需要最新信息、时事新闻、具体数据、产品对比、技术文档等 → need_search=true\n"
    "- 纯粹的逻辑推理、创意写作、代码编写、数学计算等 → need_search=false\n"
    "- keywords 应是精炼的搜索关键词（2-4个），不是原文复述\n"
)

_PREPROCESS_ORGANIZE_PROMPT = (
    "你是搜索结果整理助手。请根据用户的原始问题，对以下搜索结果进行整理：\n"
    "1. 去除重复和不相关的内容\n"
    "2. 按相关度排序\n"
    "3. 提取关键信息，保留来源链接\n"
    "4. 输出精炼的参考资料摘要（Markdown 格式）\n\n"
    "要求简洁、准确、有结构，控制在 800 字以内。"
)


async def _preprocess_analyze(
    user_message: str,
    *,
    user_settings: dict,
    websocket: WebSocket,
    request_id: str,
    send_event,
) -> dict | None:
    """调用预处理模型分析是否需要搜索，返回解析后的 JSON 或 None（失败时）。"""
    preprocess_model = user_settings.get("preprocess_model", "")
    if not preprocess_model:
        return None

    models = user_settings.get("models", [])
    id_map = _build_model_id_map(models)
    model_id = id_map.get(preprocess_model, preprocess_model)

    await send_event(websocket, {
        "type": "preprocess_start",
        "request_id": request_id,
        "model": preprocess_model,
    })

    llm = _create_llm_client_from_settings(user_settings)
    try:
        llm_resp = await asyncio.wait_for(
            llm.complete(
                model=model_id,
                messages=[
                    {"role": "system", "content": _PREPROCESS_ANALYZE_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0,
                max_tokens=_PREPROCESS_MAX_TOKENS,
            ),
            timeout=_PREPROCESS_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("preprocess analyze failed request_id=%s error=%s", request_id, exc)
        return None

    raw_text = llm_resp.text
    if not raw_text:
        return None

    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        result = _json.loads(cleaned)
    except _json.JSONDecodeError:
        logger.warning("preprocess analyze returned invalid JSON: %s", cleaned[:200])
        return None

    await send_event(websocket, {
        "type": "preprocess_result",
        "request_id": request_id,
        "model": preprocess_model,
        "need_search": bool(result.get("need_search", False)),
        "keywords": result.get("keywords", []),
        "reason": str(result.get("reason", ""))[:200],
    })
    return result


async def _preprocess_organize_results(
    user_message: str,
    search_bundle: "SearchBundle",
    *,
    user_settings: dict,
    websocket: WebSocket,
    request_id: str,
    send_event,
) -> str | None:
    """调用预处理模型整理搜索结果，返回精炼后的 Markdown 摘要或 None。"""
    preprocess_model = user_settings.get("preprocess_model", "")
    if not preprocess_model or not search_bundle or not search_bundle.items:
        return None

    models = user_settings.get("models", [])
    id_map = _build_model_id_map(models)
    model_id = id_map.get(preprocess_model, preprocess_model)

    raw_results = search_bundle.as_prompt_block()
    if len(raw_results) > 12000:
        raw_results = raw_results[:12000] + "\n\n[结果已截断]"

    llm = _create_llm_client_from_settings(user_settings)
    try:
        llm_resp = await asyncio.wait_for(
            llm.complete(
                model=model_id,
                messages=[
                    {"role": "system", "content": _PREPROCESS_ORGANIZE_PROMPT},
                    {"role": "user", "content": f"用户问题：{user_message}\n\n搜索结果：\n{raw_results}"},
                ],
                temperature=0.2,
            ),
            timeout=_PREPROCESS_ORGANIZE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("preprocess organize failed request_id=%s error=%s", request_id, exc)
        return None

    organized = llm_resp.text
    if organized:
        await send_event(websocket, {
            "type": "search_organized",
            "request_id": request_id,
            "model": preprocess_model,
            "organized_markdown": organized[:3000],
        })
    return organized


async def _prepare_thread_for_stream(
    thread: ThreadState,
    websocket: WebSocket,
    search_service: FirecrawlSearchService,
    database: LocalDatabase,
    client_id: str,
    user_settings: dict | None = None,
    send_event=None,
    user_id: int | None = None,
) -> None:
    search_mode = thread.search_enabled  # True / False / "auto"

    if search_mode is False:
        return

    preprocess_model = (user_settings or {}).get("preprocess_model", "") if user_settings else ""
    _send = send_event

    if preprocess_model and search_mode in (True, "auto") and _send and user_settings:
        analysis = await _preprocess_analyze(
            thread.user_message,
            user_settings=user_settings,
            websocket=websocket,
            request_id=thread.request_id,
            send_event=_send,
        )
        if analysis is not None:
            need_search = bool(analysis.get("need_search", False))
            keywords = analysis.get("keywords", [])

            if not need_search and search_mode == "auto":
                return
            if not need_search and search_mode is True:
                pass  # forced search, use original message
            else:
                search_query = " ".join(keywords) if keywords else thread.user_message
                search_bundle = await _run_search_if_needed(
                    websocket=websocket,
                    search_service=search_service,
                    request_id=thread.request_id,
                    query=search_query,
                    think_enabled=thread.think_enabled,
                    enabled=True,
                    database=database,
                    client_id=client_id,
                    user_id=user_id,
                    thread=thread,
                    )
                if search_bundle and search_bundle.items:
                    organized = await _preprocess_organize_results(
                        thread.user_message, search_bundle,
                        user_settings=user_settings,
                        websocket=websocket,
                        request_id=thread.request_id,
                        send_event=_send,
                    )
                    if organized:
                        for history in thread.histories.values():
                            insert_at = len(history)
                            if history and history[-1]["role"] == "user":
                                insert_at = len(history) - 1
                            history.insert(insert_at, {"role": "system", "content": organized})
                    else:
                        _inject_search_bundle(thread.histories, search_bundle)
                thread.search_bundle = search_bundle
                return

        if search_mode == "auto":
            return

    if search_mode is True or search_mode == "auto":
        search_bundle = await _run_search_if_needed(
            websocket=websocket,
            search_service=search_service,
            request_id=thread.request_id,
            query=thread.user_message,
            think_enabled=thread.think_enabled,
            enabled=True,
            database=database,
            client_id=client_id,
            user_id=user_id,
            thread=thread,
        )
        thread.search_bundle = search_bundle
        if search_bundle:
            _inject_search_bundle(thread.histories, search_bundle)


async def _run_search_if_needed(
    websocket: WebSocket,
    search_service: FirecrawlSearchService,
    request_id: str,
    query: str,
    think_enabled: bool,
    enabled: bool,
    database: LocalDatabase | None = None,
    client_id: str | None = None,
    user_id: int | None = None,
    thread: ThreadState | None = None,
) -> SearchBundle | None:
    if not enabled:
        return None
    if not search_service.enabled:
        payload = {
            "type": "search_error",
            "request_id": request_id,
            "provider": "firecrawl",
            "content": "未配置 Firecrawl API Key，已跳过联网搜索。",
        }
        await websocket.send_json(payload)
        if database is not None:
            await database.record_event(event_type="search_error", request_id=request_id, client_id=client_id, payload=payload)
        return None

    started_payload = {
        "type": "search_started",
        "request_id": request_id,
        "provider": "firecrawl",
        "query": query,
        "think_enabled": think_enabled,
    }
    await websocket.send_json(started_payload)
    if database is not None:
        await database.record_event(event_type="search_started", request_id=request_id, client_id=client_id, payload=started_payload)

    try:
        search_bundle = await search_service.search(query=query, think_enabled=think_enabled)
        if database is not None and user_id is not None:
            cfg = await database.get_system_config()
            try:
                search_points = max(0.0, float(cfg.get("config_search_points_per_call", "0") or 0))
            except (TypeError, ValueError):
                search_points = 0.0
            if search_points > 0 and thread is not None:
                thread.charged_search_points += search_points
        completed_payload = {
            "type": "search_complete",
            "request_id": request_id,
            "provider": search_bundle.provider,
            "query": search_bundle.query,
            "count": len(search_bundle.items),
            "results": [_serialize_search_item(item) for item in search_bundle.items],
        }
        await websocket.send_json(completed_payload)
        if database is not None:
            await database.record_event(event_type="search_complete", request_id=request_id, client_id=client_id, payload=completed_payload)
        return search_bundle
    except Exception as exc:
        logger.warning("firecrawl search failed: request_id=%s query=%r error=%s", request_id, query, exc, exc_info=True)
        error_payload = {
            "type": "search_error",
            "request_id": request_id,
            "provider": "firecrawl",
            "content": str(exc),
        }
        await websocket.send_json(error_payload)
        if database is not None:
            log_payload = {**error_payload, "error_detail": str(exc)}
            await database.record_event(event_type="search_error", request_id=request_id, client_id=client_id, payload=log_payload)
        return None


def _serialize_search_item(item: SearchItem) -> dict[str, str | int]:
    return {
        "title": item.title,
        "url": item.url,
        "snippet": item.snippet,
        "rank": item.rank,
    }


def _inject_search_bundle(
    histories: dict[str, list[dict[str, str]]],
    search_bundle: SearchBundle,
) -> None:
    prompt_block = search_bundle.as_prompt_block()
    for history in histories.values():
        insert_at = len(history)
        if history and history[-1]["role"] == "user":
            insert_at = len(history) - 1
        history.insert(insert_at, {"role": "system", "content": prompt_block})


def _clone_history_to_round(
    history: list[dict[str, str]], source_round: int, *, include_assistant: bool = True
) -> list[dict[str, str]] | None:
    cloned: list[dict[str, str]] = []
    assistant_rounds = 0
    for item in history:
        if item["role"] == "assistant":
            assistant_rounds += 1
            if assistant_rounds >= source_round:
                if include_assistant:
                    cloned.append({"role": item["role"], "content": item["content"]})
                return cloned
        cloned.append({"role": item["role"], "content": item["content"]})
    return None if not include_assistant else cloned


def _clone_history_before_assistant_round(
    history: list[dict[str, str]], source_round: int
) -> list[dict[str, str]] | None:
    return _clone_history_to_round(history, source_round, include_assistant=False)


def _clone_history_until_round(history: list[dict[str, str]], source_round: int) -> list[dict[str, str]]:
    result = _clone_history_to_round(history, source_round, include_assistant=True)
    return result if result is not None else []


def _parse_discussion_rounds(value: object) -> int:
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        rounds = 2
    return max(1, min(rounds, 4))


def _parse_source_round(value: object) -> int:
    try:
        round_number = int(value)
    except (TypeError, ValueError):
        round_number = 1
    return max(1, round_number)


def _parse_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_search_mode(value: object) -> bool | str:
    if isinstance(value, bool):
        return value
    if value is None:
        return "auto"
    s = str(value).strip().lower()
    if s == "auto":
        return "auto"
    return s in {"1", "true", "yes", "on"}
