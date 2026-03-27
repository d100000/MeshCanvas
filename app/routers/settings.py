"""User settings routes — password change only.

API and search configuration is managed globally via the setup flow.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import get_global_user_settings
from app.core.exceptions import AuthError, OriginError
from app.dependencies import (
    mask_key,
    parse_json_body,
    require_origin,
    require_user,
    get_auth_manager,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/settings")
async def get_user_settings_api(request: Request) -> JSONResponse:
    """Return global config overview (read-only, keys masked) and user info."""
    try:
        user = await require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)

    gs = get_global_user_settings()
    return JSONResponse({
        "username": user["username"],
        "api_base_url": gs["api_base_url"],
        "api_format": gs["api_format"],
        "api_key_masked": mask_key(gs["api_key"]),
        "models": gs["models"],
        "firecrawl_api_key_masked": mask_key(gs.get("firecrawl_api_key", "")),
        "firecrawl_country": gs.get("firecrawl_country", "CN"),
        "firecrawl_timeout_ms": gs.get("firecrawl_timeout_ms", 45000),
        "search_available": bool(gs.get("firecrawl_api_key")),
    })


@router.put("/api/settings/password")
async def change_password(request: Request) -> JSONResponse:
    """Change the current user's password."""
    try:
        user = await require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        await require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)
    try:
        payload = await parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    old_password = str(payload.get("old_password", "")).strip()
    new_password = str(payload.get("new_password", "")).strip()

    if not old_password:
        return JSONResponse({"detail": "请输入当前密码。"}, status_code=400)
    if not new_password or len(new_password) < 8:
        return JSONResponse({"detail": "新密码至少需要 8 位。"}, status_code=400)
    if len(new_password) > 128:
        return JSONResponse({"detail": "密码长度不能超过 128 位。"}, status_code=400)

    auth = get_auth_manager(request)
    try:
        await auth.change_password(user["username"], old_password, new_password)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    logger.info("password changed for user %s", user["username"])
    return JSONResponse({"ok": True})
