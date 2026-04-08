from __future__ import annotations
import asyncio
import logging
from time import monotonic
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.deps import (
    database, auth_manager, rate_limiter, request_logger,
    _get_websocket_user, _is_origin_allowed, _http_log_tasks,
    _load_global_service_settings, _build_effective_user_settings,
    _pick_analysis_model, _calculate_model_points_cost,
    _estimate_thread_reserve_points, _generate_conclusion,
    _collect_latest_success_results_from_map,
    _register_user_task, _get_user_pending_request_ids, _cancel_user_task, _cancel_all_user_tasks,
    _build_main_thread, _build_branch_thread, _build_retry_thread,
    _prepare_thread_for_stream, ThreadState,
)
from app.chat_service import MultiModelChatService
from app.search_service import FirecrawlSearchService
from app.config import is_configured

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/chat")
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
            # v8: insert DB row BEFORE emitting user event so the frontend's
            # immediate schedulePositionSave PUT can find its owner row.
            # (Previously, a race allowed the PUT to arrive as 404.)
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
                context_node_ids=thread.context_node_ids,
            )
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
