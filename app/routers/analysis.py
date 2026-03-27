"""Selection summary and conversation analysis routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import get_global_user_settings
from app.core.exceptions import AuthError, OriginError
from app.dependencies import (
    get_database,
    get_rate_limiter,
    get_request_logger,
    parse_json_body,
    require_origin,
    require_user,
)
from app.services.analysis_service import AnalysisService

logger = logging.getLogger(__name__)

router = APIRouter()

_analysis = AnalysisService()


@router.post("/api/selection-summary")
async def selection_summary(request: Request) -> JSONResponse:
    try:
        user = await require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        await require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    client_host = request.client.host if request.client else "unknown"
    rl = get_rate_limiter(request)
    if not await rl.allow_async(f"selection-summary:{user['user_id']}:{client_host}", limit=24, window_seconds=300):
        return JSONResponse({"detail": "总结请求过于频繁，请稍后再试。"}, status_code=429)

    try:
        payload = await parse_json_body(request)
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

    global_settings = get_global_user_settings()
    req_logger = get_request_logger(request)
    try:
        summary, model = await _analysis.summarize_selection(bundle=bundle, count=count, user_settings=global_settings)
    except RuntimeError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("selection summary failed: %s", exc)
        await req_logger.log_event({"type": "selection_summary_error", "user_id": user["user_id"], "client_id": client_host, "count": count, "error": str(exc)})
        return JSONResponse({"detail": "摘要生成失败，请稍后重试。"}, status_code=502)

    await req_logger.log_event({"type": "selection_summary", "user_id": user["user_id"], "client_id": client_host, "count": count, "model": model, "bundle_length": len(bundle), "summary_length": len(summary)})
    return JSONResponse({"summary": summary, "model": model, "count": count})


@router.post("/api/conversation-analysis")
async def conversation_analysis(request: Request) -> JSONResponse:
    try:
        user = await require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        await require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    client_host = request.client.host if request.client else "unknown"
    rl = get_rate_limiter(request)
    if not await rl.allow_async(f"conv-analysis:{user['user_id']}:{client_host}", limit=20, window_seconds=300):
        return JSONResponse({"detail": "分析请求过于频繁，请稍后再试。"}, status_code=429)

    try:
        payload = await parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    request_id = str(payload.get("request_id", "")).strip()
    messages = payload.get("messages")

    if not messages or not isinstance(messages, list):
        if request_id:
            db = get_database(request)
            data = await db.get_request_with_results(request_id, user["user_id"])
            if not data:
                return JSONResponse({"detail": "未找到对应的会话记录。"}, status_code=404)
            messages = [{"role": "user", "content": data["user_message"]}]
            for model_name, rounds in data.get("model_results", {}).items():
                for r in sorted(rounds, key=lambda x: x["round"]):
                    if r.get("content"):
                        messages.append({"role": "assistant", "content": f"[{model_name}] {r['content']}"})
        else:
            return JSONResponse({"detail": "请提供 messages 或 request_id。"}, status_code=400)

    if not messages:
        return JSONResponse({"detail": "会话内容为空。"}, status_code=400)

    global_settings = get_global_user_settings()
    req_logger = get_request_logger(request)
    try:
        result, model = await _analysis.analyze_conversation(messages, user_settings=global_settings)
    except RuntimeError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("conversation analysis failed: %s", exc)
        await req_logger.log_event({"type": "conversation_analysis_error", "user_id": user["user_id"], "client_id": client_host, "request_id": request_id, "error": str(exc)})
        return JSONResponse({"detail": "对话分析失败，请稍后重试。"}, status_code=502)

    await req_logger.log_event({"type": "conversation_analysis", "user_id": user["user_id"], "client_id": client_host, "request_id": request_id, "model": model, "message_count": len(messages)})
    return JSONResponse({"analysis": result, "model": model})
