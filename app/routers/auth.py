from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.deps import (
    database,
    auth_manager,
    rate_limiter,
    request_logger,
    _get_request_user,
    _parse_json_body,
    _is_origin_allowed,
    _set_session_cookie,
    _clear_session_cookie,
    _safe_login_username,
    _log_login_failure,
    _log_login_success,
    _http_log_tasks,
    _unauthorized_json,
)
from app.auth import AuthError, SESSION_COOKIE_NAME
from app.captcha import generate as captcha_generate, verify as captcha_verify, check_honeypot

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/captcha")
async def get_captcha() -> JSONResponse:
    """Generate a new arithmetic CAPTCHA challenge."""
    question, token = captcha_generate()
    return JSONResponse({"question": question, "token": token})


@router.get("/api/auth/session")
async def auth_session(request: Request) -> JSONResponse:
    user = await _get_request_user(request)
    if not user:
        return JSONResponse({"authenticated": False}, status_code=200)
    return JSONResponse({"authenticated": True, "username": user["username"]})


@router.get("/api/auth/registration-status")
async def registration_status() -> JSONResponse:
    """Return whether new-user registration is currently allowed."""
    cfg = await database.get_system_config()
    allow = cfg.get("config_allow_registration", "true") != "false"
    return JSONResponse({"allow": allow})


@router.post("/api/auth/register")
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


@router.post("/api/auth/login")
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


@router.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)
    await auth_manager.logout(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse({"ok": True})
    _clear_session_cookie(response)
    return response
