from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.auth import AuthError
from app.deps import (
    database,
    rate_limiter,
    request_logger,
    _get_request_user,
    _unauthorized_json,
    _load_global_service_settings,
    _load_user_settings_or_error,
    _pick_analysis_model,
    _summarize_selection_bundle,
    _analyze_conversation,
    _require_origin,
    _parse_json_body,
    OriginError,
)
from app.schemas.models import SelectionSummaryRequest, ConversationAnalysisRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/models")
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


@router.post("/api/selection-summary")
async def selection_summary(request: Request, body: SelectionSummaryRequest) -> JSONResponse:
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

    bundle = body.bundle.strip()
    count = body.count

    if not bundle:
        return JSONResponse({"detail": "缺少待总结的节点内容。"}, status_code=400)

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


@router.post("/api/conversation-analysis")
async def conversation_analysis(request: Request, body: ConversationAnalysisRequest) -> JSONResponse:
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

    request_id = body.request_id.strip()
    messages = [{"role": m.role, "content": m.content} for m in body.messages] if body.messages else None

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
