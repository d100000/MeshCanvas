"""WebSocket chat endpoint — protocol handling only, business logic delegated."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import get_global_user_settings, is_configured
from app.core.middleware import _is_origin_allowed
from app.core.request_logger import RequestLogger
from app.core.security import RateLimiter
from app.database import LocalDatabase
from app.models.chat import ThreadState
from app.models.search import SearchItem
from app.services.chat_service import MessageSink, MultiModelChatService
from app.services.history_utils import inject_search_bundle
from app.services.llm_client_factory import LLMClientFactory
from app.services.search_service import FirecrawlSearchService
from app.services.thread_builder import ThreadBuilder

logger = logging.getLogger(__name__)

router = APIRouter()


class WebSocketSink(MessageSink):
    """Adapts a WebSocket connection to the MessageSink protocol."""

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        self._lock = asyncio.Lock()

    async def send(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            try:
                await self._ws.send_json(payload)
            except (WebSocketDisconnect, RuntimeError):
                return


@router.websocket("/ws/chat")
async def chat_socket(websocket: WebSocket) -> None:
    if not is_configured():
        await websocket.close(code=4503)
        return
    if not _is_origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=4403)
        return

    # Auth
    auth_manager = websocket.app.state.auth_manager
    user = await auth_manager.get_user_from_token(websocket.cookies.get("canvas_session"))
    if not user:
        await websocket.close(code=4401)
        return

    await websocket.accept()

    client_host = websocket.client.host if websocket.client else "unknown"
    client_port = websocket.client.port if websocket.client else "unknown"
    client_id = f"{user['username']}@{client_host}:{client_port}"
    logger.info("websocket connected: %s", client_id)

    database: LocalDatabase = websocket.app.state.database
    request_logger: RequestLogger = websocket.app.state.request_logger
    rate_limiter: RateLimiter = websocket.app.state.rate_limiter

    # Use global config for all users
    global_settings = get_global_user_settings()

    service = MultiModelChatService(
        api_key=global_settings["api_key"],
        base_url=global_settings["api_base_url"],
        models=global_settings["models"],
        request_logger=request_logger,
        database=database,
    )

    search_service = FirecrawlSearchService(
        api_key=global_settings.get("firecrawl_api_key", ""),
        country=global_settings.get("firecrawl_country", "CN"),
        timeout_ms=global_settings.get("firecrawl_timeout_ms", 45000),
    )

    sink = WebSocketSink(websocket)
    thread_builder = ThreadBuilder(database)
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
            await sink.send({
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
            })
            await database.record_chat_request(
                request_id=thread.request_id, client_id=client_id,
                models=thread.models, user_message=thread.user_message,
                discussion_rounds=thread.discussion_rounds,
                search_enabled=thread.search_enabled, think_enabled=thread.think_enabled,
                parent_request_id=thread.parent_request_id,
                source_model=thread.source_model, source_round=thread.source_round,
                status="queued", canvas_id=thread.canvas_id, user_id=user["user_id"],
            )
            create_background_task(request_logger.log_event({
                "type": "chat_request", "request_id": thread.request_id,
                "client_id": client_id, "models": thread.models,
                "message": thread.user_message, "message_length": len(thread.user_message),
                "discussion_rounds": thread.discussion_rounds,
                "search_enabled": thread.search_enabled, "think_enabled": thread.think_enabled,
                "parent_request_id": thread.parent_request_id,
                "source_model": thread.source_model, "source_round": thread.source_round,
            }))
            await _prepare_thread_for_stream(thread, sink, search_service, database, client_id, request_logger)
            create_background_task(database.mark_request_status(thread.request_id, "streaming"))
            results = await service.stream_round(
                histories=thread.histories, sink=sink,
                request_id=thread.request_id, client_id=client_id,
                user_message=thread.user_message, discussion_rounds=thread.discussion_rounds,
            )
            await sink.send({"type": "round_complete", "request_id": thread.request_id})
            create_background_task(request_logger.log_event({
                "type": "chat_round_complete", "request_id": thread.request_id,
                "client_id": client_id, "message_length": len(thread.user_message),
                "discussion_rounds": thread.discussion_rounds,
                "search_enabled": thread.search_enabled, "think_enabled": thread.think_enabled,
                "models": thread.models,
                "results": [{k: v for k, v in item.items() if k != "content"} for item in results],
            }))
            create_background_task(database.mark_request_status(thread.request_id, "completed"))
        except asyncio.CancelledError:
            await sink.send({"type": "cancelled", "request_id": thread.request_id, "content": "已取消当前请求。"})
            create_background_task(database.mark_request_status(thread.request_id, "cancelled"))
            create_background_task(database.record_event(
                event_type="request_cancelled", request_id=thread.request_id,
                client_id=client_id, payload={"request_id": thread.request_id, "reason": "cancelled"},
            ))
            raise
        except Exception as exc:
            logger.exception("request %s failed: %s", thread.request_id, exc)
            await sink.send({"type": "error", "request_id": thread.request_id, "content": "请求处理失败，请稍后重试。"})
            create_background_task(database.mark_request_status(thread.request_id, "failed"))
            create_background_task(database.record_event(
                event_type="request_failed", request_id=thread.request_id,
                client_id=client_id, payload={"request_id": thread.request_id, "error": str(exc)},
            ))
        finally:
            request_tasks.pop(thread.request_id, None)

    # Send meta on connect
    user_models = service.models
    await request_logger.log_event({"type": "ws_connect", "client_id": client_id, "models": user_models, "search_enabled": search_service.enabled})
    await websocket.send_json({
        "type": "meta", "models": user_models,
        "analysis_model": LLMClientFactory.pick_analysis_model(global_settings["models"]),
        "search_available": search_service.enabled,
        "username": user["username"],
    })

    try:
        while True:
            payload = await websocket.receive_json()
            if not await rate_limiter.allow_async(f"ws-action:{user['username']}", limit=180, window_seconds=60):
                await websocket.send_json({"type": "error", "content": "请求过于频繁，请稍后再试。"})
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
                await request_logger.log_event({"type": "ws_clear", "client_id": client_id, "models": user_models})
                await database.record_event(event_type="ws_clear", client_id=client_id, payload={"models": user_models})
                await websocket.send_json({"type": "cleared"})
                continue

            if action == "cancel_request":
                rid = str(payload.get("request_id", "")).strip()
                task = request_tasks.get(rid)
                if not rid or task is None or task.done():
                    await websocket.send_json({"type": "error", "request_id": rid, "content": "未找到可取消的请求。"})
                    continue
                task.cancel()
                await websocket.send_json({"type": "cancel_requested", "request_id": rid})
                continue

            # Build thread based on action type
            thread: ThreadState | str | None = None
            if action == "chat":
                thread = thread_builder.build_main_thread(payload=payload, available_models=service.models)
            elif action == "branch_chat":
                thread = await thread_builder.build_branch_thread(payload=payload, threads=threads, user_id=user["user_id"])
            elif action == "retry_model":
                thread = await thread_builder.build_retry_thread(payload=payload, threads=threads, user_id=user["user_id"])
            else:
                await request_logger.log_event({"type": "ws_unsupported_action", "client_id": client_id, "payload": payload})
                await websocket.send_json({"type": "error", "content": "Unsupported action."})
                continue

            if isinstance(thread, str):
                await websocket.send_json({"type": "error", "content": thread})
                continue
            if thread is None:
                continue

            threads[thread.request_id] = thread
            request_tasks[thread.request_id] = asyncio.create_task(run_thread(thread))
    except WebSocketDisconnect:
        logger.info("websocket disconnected: %s", client_id)
        for task in list(request_tasks.values()):
            task.cancel()
        if request_tasks:
            await asyncio.gather(*request_tasks.values(), return_exceptions=True)
        await wait_for_background_tasks()
        await request_logger.log_event({"type": "ws_disconnect", "client_id": client_id})
        return


# ---- Search orchestration helpers ----

async def _prepare_thread_for_stream(
    thread: ThreadState,
    sink: MessageSink,
    search_service: FirecrawlSearchService,
    database: LocalDatabase,
    client_id: str,
    request_logger: RequestLogger,
) -> None:
    if not thread.search_enabled:
        return
    search_bundle = await _run_search_if_needed(
        sink=sink, search_service=search_service,
        request_id=thread.request_id, query=thread.user_message,
        think_enabled=thread.think_enabled, enabled=True,
        database=database, client_id=client_id,
    )
    thread.search_bundle = search_bundle
    if search_bundle:
        inject_search_bundle(thread.histories, search_bundle)


async def _run_search_if_needed(
    sink: MessageSink,
    search_service: FirecrawlSearchService,
    request_id: str, query: str, think_enabled: bool, enabled: bool,
    database: LocalDatabase | None = None, client_id: str | None = None,
):
    if not enabled:
        return None
    if not search_service.enabled:
        payload = {"type": "search_error", "request_id": request_id, "provider": "firecrawl", "content": "未配置 Firecrawl API Key，已跳过联网搜索。"}
        await sink.send(payload)
        if database is not None:
            await database.record_event(event_type="search_error", request_id=request_id, client_id=client_id, payload=payload)
        return None

    started_payload = {"type": "search_started", "request_id": request_id, "provider": "firecrawl", "query": query, "think_enabled": think_enabled}
    await sink.send(started_payload)
    if database is not None:
        await database.record_event(event_type="search_started", request_id=request_id, client_id=client_id, payload=started_payload)

    try:
        search_bundle = await search_service.search(query=query, think_enabled=think_enabled)
        completed_payload = {
            "type": "search_complete", "request_id": request_id,
            "provider": search_bundle.provider, "query": search_bundle.query,
            "count": len(search_bundle.items),
            "results": [_serialize_search_item(item) for item in search_bundle.items],
        }
        await sink.send(completed_payload)
        if database is not None:
            await database.record_event(event_type="search_complete", request_id=request_id, client_id=client_id, payload=completed_payload)
        return search_bundle
    except Exception as exc:
        logger.warning("search failed for request %s: %s", request_id, exc)
        client_payload = {"type": "search_error", "request_id": request_id, "provider": "firecrawl", "content": "联网搜索失败，请稍后重试。"}
        await sink.send(client_payload)
        if database is not None:
            log_payload = {**client_payload, "error_detail": str(exc)}
            await database.record_event(event_type="search_error", request_id=request_id, client_id=client_id, payload=log_payload)
        return None


def _serialize_search_item(item: SearchItem) -> dict[str, str | int]:
    return {"title": item.title, "url": item.url, "snippet": item.snippet, "rank": item.rank}
