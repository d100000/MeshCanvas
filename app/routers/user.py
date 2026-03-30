from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.auth import AuthError
from app.deps import (
    database,
    _require_user,
    _is_origin_allowed,
    _parse_json_body,
    _load_global_service_settings,
    _mask_key,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/settings")
async def get_user_settings_api(request: Request) -> JSONResponse:
    """仅返回账号基本信息。API 配置由管理员统一管理，不再对普通用户暴露。"""
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    return JSONResponse({"authenticated": True, "username": user["username"]})


@router.put("/api/settings")
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


@router.get("/api/user/usage-detail")
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


@router.get("/api/user/usage-summary")
async def user_usage_summary(request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    summary = await database.get_user_usage_summary(user["user_id"])
    return JSONResponse({"summary": summary})


@router.get("/api/user/custom-api-key")
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


@router.put("/api/user/custom-api-key")
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


@router.delete("/api/user/custom-api-key")
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
