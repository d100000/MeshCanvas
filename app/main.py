from __future__ import annotations

import asyncio
import json as _json
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI

from app.auth import AuthError, AuthManager, SESSION_COOKIE_NAME, SESSION_DAYS
from app.chat_service import MultiModelChatService
from app.config import get_settings
from app.database import LocalDatabase
from app.request_logger import RequestLogger
from app.search_service import FirecrawlSearchService, SearchBundle, SearchItem
from app.security import RateLimiter, build_security_headers

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
request_logger = RequestLogger()
database = LocalDatabase()
auth_manager = AuthManager(database)
rate_limiter = RateLimiter()
security_headers = build_security_headers()

app = FastAPI(title="Multi-Model Web Chat")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def initialize_local_database() -> None:
    await database.initialize()
    await database.delete_expired_sessions()


BASE_SYSTEM_PROMPT = (
    "你是一个在无限画布里协作的模型节点。"
    "回答必须使用清晰的 Markdown。"
    "如果给了联网搜索结果，请优先基于搜索结果回答，并在最后附上“参考来源”列表。"
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
    search_enabled: bool
    think_enabled: bool
    search_bundle: SearchBundle | None = None
    parent_request_id: str | None = None
    source_model: str | None = None
    source_round: int | None = None
    canvas_id: str | None = None
    meta: dict[str, str] = field(default_factory=dict)


@app.middleware("http")
async def log_http_request(request: Request, call_next):
    started_at = perf_counter()
    response = await call_next(request)
    duration_ms = round((perf_counter() - started_at) * 1000, 2)
    client_host = request.client.host if request.client else "unknown"
    await request_logger.log_event(
        {
            "type": "http_request",
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "status_code": response.status_code,
            "client_id": client_host,
            "duration_ms": duration_ms,
        }
    )
    for key, value in security_headers.items():
        response.headers.setdefault(key, value)
    return response


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


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https"),
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


_shared_openai_client: AsyncOpenAI | None = None


def _get_shared_openai_client() -> AsyncOpenAI:
    global _shared_openai_client
    if _shared_openai_client is None:
        settings = get_settings()
        _shared_openai_client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)
    return _shared_openai_client


def _pick_analysis_model() -> str:
    """Pick Kimi as the dedicated conversation analysis model."""
    settings = get_settings()
    if "Kimi-K2.5" in settings.models:
        return "Kimi-K2.5"
    for model in settings.models:
        if "kimi" in model.lower():
            return model
    return settings.models[0] if settings.models else ""


def _extract_completion_text(response: object) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
            else:
                text = getattr(item, "text", None) or getattr(item, "content", None) or ""
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content or "").strip()


async def _summarize_selection_bundle(bundle: str, count: int) -> tuple[str, str]:
    model = _pick_analysis_model()
    if not model:
        raise RuntimeError("未配置可用的摘要模型。")

    client = _get_shared_openai_client()
    clipped_bundle = bundle[:12000]
    prompt = (
        f"请将用户圈选的 {count} 个无限画布节点压缩成可供下一轮对话继续使用的上下文。\n"
        "输出要求：\n"
        "1. 使用简洁中文；\n"
        "2. 优先保留最终结论、关键依据、核心分歧、下一步建议；\n"
        "3. 不要重复原文，不要展开长篇推理；\n"
        "4. 控制在 220 到 300 字内，可使用 Markdown 列表。\n\n"
        "以下是待压缩的节点内容：\n\n"
        f"{clipped_bundle}"
    )
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "你是无限画布里的上下文压缩助手，只输出给下一轮模型使用的高密度摘要。",
            },
            {"role": "user", "content": prompt},
        ],
        stream=False,
    )
    summary = _extract_completion_text(response)
    if not summary:
        raise RuntimeError("摘要模型未返回可用内容。")
    return summary[:1200], model


async def _analyze_conversation(messages: list[dict[str, str]]) -> tuple[dict[str, str], str]:
    """Use Kimi to analyze a conversation: generate title, key points, and summary."""
    model = _pick_analysis_model()
    if not model:
        raise RuntimeError("未配置可用的分析模型。")

    client = _get_shared_openai_client()

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

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是会话分析助手，只输出 JSON，不要输出任何解释。"},
            {"role": "user", "content": prompt},
        ],
        stream=False,
    )
    raw_text = _extract_completion_text(response)
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


@app.get("/")
async def index(request: Request) -> FileResponse:
    user = await _get_request_user(request)
    if not user:
        return FileResponse(STATIC_DIR / "login.html")
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login")
async def login_page(request: Request):
    user = await _get_request_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/api/auth/session")
async def auth_session(request: Request) -> JSONResponse:
    user = await _get_request_user(request)
    if not user:
        return JSONResponse({"authenticated": False}, status_code=200)
    return JSONResponse({"authenticated": True, "username": user["username"]})


@app.post("/api/auth/register")
async def register(request: Request) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    if not await rate_limiter.allow_async(f"auth-register:{client_host}", limit=10, window_seconds=600):
        return JSONResponse({"detail": "注册过于频繁，请稍后再试。"}, status_code=429)
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)

    try:
        payload = await _parse_json_body(request)
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        user, token, _ = await auth_manager.register(username, password)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    response = JSONResponse({"ok": True, "username": user["username"]})
    _set_session_cookie(response, token, request)
    return response


@app.post("/api/auth/login")
async def login(request: Request) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    if not await rate_limiter.allow_async(f"auth-login:{client_host}", limit=15, window_seconds=600):
        return JSONResponse({"detail": "登录过于频繁，请稍后再试。"}, status_code=429)
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)

    try:
        payload = await _parse_json_body(request)
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        user, token, _ = await auth_manager.login(username, password)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    response = JSONResponse({"ok": True, "username": user["username"]})
    _set_session_cookie(response, token, request)
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
    settings = get_settings()
    return {"models": settings.models, "analysis_model": _pick_analysis_model()}


@app.post("/api/selection-summary")
async def selection_summary(request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
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
        summary, model = await _summarize_selection_bundle(bundle=bundle, count=count)
    except Exception as exc:
        await request_logger.log_event(
            {
                "type": "selection_summary_error",
                "user_id": user["user_id"],
                "client_id": client_host,
                "count": count,
                "error": str(exc),
            }
        )
        return JSONResponse({"detail": str(exc)}, status_code=502)

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
    """Use Kimi to analyze a conversation thread: generate title, key points, summary, and tags."""
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
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
        result, model = await _analyze_conversation(messages)
    except Exception as exc:
        await request_logger.log_event(
            {
                "type": "conversation_analysis_error",
                "user_id": user["user_id"],
                "client_id": client_host,
                "request_id": request_id,
                "error": str(exc),
            }
        )
        return JSONResponse({"detail": str(exc)}, status_code=502)

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
    if not _is_origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=4403)
        return

    user = await _get_websocket_user(websocket)
    if not user:
        await websocket.close(code=4401)
        return

    await websocket.accept()

    client_host = websocket.client.host if websocket.client else "unknown"
    client_port = websocket.client.port if websocket.client else "unknown"
    client_id = f"{user['username']}@{client_host}:{client_port}"

    service = MultiModelChatService(request_logger=request_logger, database=database)
    search_service = FirecrawlSearchService()
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
                search_enabled=thread.search_enabled,
                think_enabled=thread.think_enabled,
                parent_request_id=thread.parent_request_id,
                source_model=thread.source_model,
                source_round=thread.source_round,
                status="queued",
                canvas_id=thread.canvas_id,
                user_id=user["user_id"],
            )
            create_background_task(
                request_logger.log_event(
                    {
                        "type": "chat_request",
                        "request_id": thread.request_id,
                        "client_id": client_id,
                        "models": thread.models,
                        "message": thread.user_message,
                        "message_length": len(thread.user_message),
                        "discussion_rounds": thread.discussion_rounds,
                        "search_enabled": thread.search_enabled,
                        "think_enabled": thread.think_enabled,
                        "parent_request_id": thread.parent_request_id,
                        "source_model": thread.source_model,
                        "source_round": thread.source_round,
                    }
                )
            )
            await _prepare_thread_for_stream(
                thread=thread,
                websocket=websocket,
                search_service=search_service,
                database=database,
                client_id=client_id,
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
        except asyncio.CancelledError:
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

    await request_logger.log_event(
        {
            "type": "ws_connect",
            "client_id": client_id,
            "models": service.models,
            "search_enabled": search_service.enabled,
        }
    )
    await websocket.send_json(
        {
            "type": "meta",
            "models": service.models,
            "analysis_model": _pick_analysis_model(),
            "search_available": search_service.enabled,
            "username": user["username"],
        }
    )

    try:
        while True:
            payload = await websocket.receive_json()
            if not await rate_limiter.allow_async(f"ws-action:{user['username']}", limit=180, window_seconds=60):
                await service._send_event(websocket, {"type": "error", "content": "请求过于频繁，请稍后再试。"})
                continue

            action = payload.get("action")

            if action == "clear":
                canvas_id_to_clear = str(payload.get("canvas_id", "")).strip()
                for task in list(request_tasks.values()):
                    task.cancel()
                if request_tasks:
                    await asyncio.gather(*request_tasks.values(), return_exceptions=True)
                request_tasks.clear()
                await wait_for_background_tasks()
                threads.clear()
                if canvas_id_to_clear:
                    await database.clear_canvas_requests(canvas_id_to_clear, user["user_id"])
                await request_logger.log_event(
                    {
                        "type": "ws_clear",
                        "client_id": client_id,
                        "models": service.models,
                    }
                )
                await database.record_event(
                    event_type="ws_clear",
                    client_id=client_id,
                    payload={"models": service.models},
                )
                await websocket.send_json({"type": "cleared"})
                continue

            if action == "cancel_request":
                request_id = str(payload.get("request_id", "")).strip()
                task = request_tasks.get(request_id)
                if not request_id or task is None or task.done():
                    await websocket.send_json({"type": "error", "request_id": request_id, "content": "未找到可取消的请求。"})
                    continue
                task.cancel()
                await websocket.send_json({"type": "cancel_requested", "request_id": request_id})
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
                await request_logger.log_event(
                    {
                        "type": "ws_unsupported_action",
                        "client_id": client_id,
                        "payload": payload,
                    }
                )
                await websocket.send_json({"type": "error", "content": "Unsupported action."})
                continue

            if thread is None:
                continue

            threads[thread.request_id] = thread
            request_tasks[thread.request_id] = asyncio.create_task(run_thread(thread))
    except WebSocketDisconnect:
        for task in list(request_tasks.values()):
            task.cancel()
        if request_tasks:
            await asyncio.gather(*request_tasks.values(), return_exceptions=True)
        await wait_for_background_tasks()
        await request_logger.log_event(
            {
                "type": "ws_disconnect",
                "client_id": client_id,
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
    search_enabled = _parse_bool(payload.get("search_enabled"), True)
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
    search_enabled = _parse_bool(payload.get("search_enabled"), True)
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
    histories: dict[str, list[dict[str, str]]] = {}
    for model in data["models"]:
        model_rounds = sorted(data["model_results"].get(model, []), key=lambda x: x["round"])
        history: list[dict[str, str]] = [
            {"role": "system", "content": f"{BASE_SYSTEM_PROMPT}\n当前模型标识：{model}。"}
        ]
        if data["think_enabled"]:
            history.append({"role": "system", "content": THINK_PROMPT})
        history.append({"role": "user", "content": data["user_message"]})
        for i, round_data in enumerate(model_rounds):
            if round_data["content"]:
                history.append({"role": "assistant", "content": round_data["content"]})
                if i < len(model_rounds) - 1:
                    history.append({"role": "user", "content": "请继续下一轮分析。"})
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


async def _prepare_thread_for_stream(
    thread: ThreadState,
    websocket: WebSocket,
    search_service: FirecrawlSearchService,
    database: LocalDatabase,
    client_id: str,
) -> None:
    if not thread.search_enabled:
        return

    search_bundle = await _run_search_if_needed(
        websocket=websocket,
        search_service=search_service,
        request_id=thread.request_id,
        query=thread.user_message,
        think_enabled=thread.think_enabled,
        enabled=True,
        database=database,
        client_id=client_id,
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
        error_payload = {
            "type": "search_error",
            "request_id": request_id,
            "provider": "firecrawl",
            "content": str(exc),
        }
        await websocket.send_json(error_payload)
        if database is not None:
            await database.record_event(event_type="search_error", request_id=request_id, client_id=client_id, payload=error_payload)
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
