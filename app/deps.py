from __future__ import annotations

import asyncio
import hmac
import json as _json
import logging
import math
import re as _re
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi import Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from app.auth import AuthError, AuthManager, SESSION_COOKIE_NAME, ADMIN_SESSION_COOKIE_NAME, SESSION_DAYS
from app.chat_service import MultiModelChatService
from app.llm_client import create_llm_client
from app.database import LocalDatabase
from app.request_logger import RequestLogger
from app.search_service import FirecrawlSearchService, SearchBundle, SearchItem
from app.captcha import generate as captcha_generate
from app.security import RateLimiter, build_security_headers

logger = logging.getLogger(__name__)

# ── Path constants ────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ADMIN_LOGIN_TEMPLATE_PATH = BASE_DIR / "templates" / "admin_login.html"
ADMIN_STATIC_DIR = STATIC_DIR / "admin"

# ── Asset versioning ─────────────────────────────────────────────────────────

ASSET_VERSION = "20260401a"
_STATIC_RE = _re.compile(r'(/static/[^"\'?]+\.(css|js|svg|png|ico))(\?v=[^"\']*)?')


def _inject_asset_version(html: str) -> str:
    """给 HTML 中所有 /static/ 资源引用追加 ?v=ASSET_VERSION。"""
    return _STATIC_RE.sub(rf'\1?v={ASSET_VERSION}', html)


# ── Admin login error text ───────────────────────────────────────────────────

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

# ── Singleton instances ──────────────────────────────────────────────────────

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

# ── Task tracking ────────────────────────────────────────────────────────────


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


# ── Prompts & constants ──────────────────────────────────────────────────────

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

_SETUP_ALLOWED_PREFIXES = ("/setup", "/api/setup", "/static/")

_SENSITIVE_HEADER_NAMES = {"authorization", "x-api-key", "api-key", "proxy-authorization"}
_BLOCKED_OVERRIDE_HEADER_NAMES = {"content-type", "content-length", "host"}

# ── Admin / estimation / conclusion constants ────────────────────────────────

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

_CONCLUSION_SYSTEM_PROMPT = (
    "你是一位专业的多模型讨论总结专家。"
    "你收到的是各模型在多轮讨论后的最后一轮结论（已压缩整理）。"
    "请输出一份完整 Markdown 文档，直接回答用户问题，并整合关键结论。要求：\n"
    "1. 仅基于输入中的最后一轮结论进行综合，不要假设缺失轮次；\n"
    "2. 优先呈现共识结论、关键依据、风险与可执行建议；\n"
    "3. 对分歧给出取舍理由或适用条件；\n"
    "4. 结构清晰，适合直接交付给用户。"
)

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

# ── ThreadState dataclass ────────────────────────────────────────────────────


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


# ── Auth helpers ─────────────────────────────────────────────────────────────


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


async def _require_user(request: Request) -> dict[str, str]:
    user = await _get_request_user(request)
    if not user:
        raise AuthError("未登录或登录已失效。")
    return user


def _unauthorized_json(message: str = "未登录或登录已失效。") -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=401)


class OriginError(Exception):
    """Raised when origin validation fails (403)."""


async def _require_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        raise OriginError("非法来源。")


# ── Login logging helpers ────────────────────────────────────────────────────


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


# ── Session helpers ──────────────────────────────────────────────────────────


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


# ── Settings helpers ─────────────────────────────────────────────────────────


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


# ── Analysis helpers ─────────────────────────────────────────────────────────


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


# ── Conclusion helpers ───────────────────────────────────────────────────────


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


# ── Analysis endpoint helpers ────────────────────────────────────────────────


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


# ── Cost helpers ─────────────────────────────────────────────────────────────


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


# ── HTML helpers ─────────────────────────────────────────────────────────────


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
    result = (
        raw.replace("@@ADMIN_MSG_CLASS@@", extra_class)
        .replace("@@ADMIN_MSG_BODY@@", body_esc)
        .replace("@@USERNAME_ATTR@@", username_attr)
        .replace("@@CAPTCHA_QUESTION@@", escape(cap_question))
        .replace("@@CAPTCHA_TOKEN@@", escape(cap_token, quote=True))
    )
    return _inject_asset_version(result)


def _html_response(path: Path) -> HTMLResponse:
    html = path.read_text(encoding="utf-8")
    html = _inject_asset_version(html)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ── Load user settings ───────────────────────────────────────────────────────


async def _load_user_settings_or_error(request: Request) -> tuple[dict | None, dict | None, JSONResponse | None]:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return None, None, JSONResponse({"detail": str(exc)}, status_code=401)
    gs = await _load_global_service_settings()
    if not gs.get("api_key"):
        return None, None, JSONResponse({"detail": "管理员尚未完成模型配置，请联系管理员。"}, status_code=503)
    return user, gs, None


# ── Admin helpers ────────────────────────────────────────────────────────────


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


# ── WebSocket helpers ────────────────────────────────────────────────────────


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


# ── History utils ────────────────────────────────────────────────────────────


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


# ── Parsing helpers ──────────────────────────────────────────────────────────


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


# ── Thread builders ──────────────────────────────────────────────────────────


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
