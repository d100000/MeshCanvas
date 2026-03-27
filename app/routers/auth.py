"""Authentication routes: register, login, logout, session check."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.exceptions import AuthError
from app.core.middleware import _is_origin_allowed
from app.dependencies import (
    clear_session_cookie,
    get_auth_manager,
    get_rate_limiter,
    get_request_user,
    parse_json_body,
    set_session_cookie,
)
from app.services.auth_service import SESSION_COOKIE_NAME

router = APIRouter(prefix="/api/auth")


@router.get("/session")
async def auth_session(request: Request) -> JSONResponse:
    user = await get_request_user(request)
    if not user:
        return JSONResponse({"authenticated": False}, status_code=200)
    return JSONResponse({"authenticated": True, "username": user["username"]})


@router.post("/register")
async def register(request: Request) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    rate_limiter = get_rate_limiter(request)
    if not await rate_limiter.allow_async(f"auth-register:{client_host}", limit=10, window_seconds=600):
        return JSONResponse({"detail": "注册过于频繁，请稍后再试。"}, status_code=429)
    if not _is_origin_allowed(request.headers.get("origin"), request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)

    auth = get_auth_manager(request)
    try:
        payload = await parse_json_body(request)
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        user, token, _ = await auth.register(username, password)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    response = JSONResponse({"ok": True, "username": user["username"]})
    set_session_cookie(response, token, request)
    return response


@router.post("/login")
async def login(request: Request) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    rate_limiter = get_rate_limiter(request)
    if not await rate_limiter.allow_async(f"auth-login:{client_host}", limit=15, window_seconds=600):
        return JSONResponse({"detail": "登录过于频繁，请稍后再试。"}, status_code=429)
    if not _is_origin_allowed(request.headers.get("origin"), request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)

    auth = get_auth_manager(request)
    try:
        payload = await parse_json_body(request)
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        user, token, _ = await auth.login(username, password)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    response = JSONResponse({"ok": True, "username": user["username"]})
    set_session_cookie(response, token, request)
    return response


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    if not _is_origin_allowed(request.headers.get("origin"), request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)
    auth = get_auth_manager(request)
    await auth.logout(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response
