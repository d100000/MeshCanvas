"""FastAPI dependency injection providers.

All shared state is stored on `app.state` during lifespan and retrieved via Depends().
"""

from __future__ import annotations

import json as _json
from typing import Any

from fastapi import Request, Response, WebSocket
from fastapi.responses import JSONResponse

from app.core.exceptions import AuthError, OriginError
from app.core.middleware import _is_origin_allowed
from app.core.request_logger import RequestLogger
from app.core.security import RateLimiter
from app.database import LocalDatabase
from app.services.auth_service import SESSION_COOKIE_NAME, SESSION_DAYS, AuthManager


def get_database(request: Request) -> LocalDatabase:
    return request.app.state.database


def get_auth_manager(request: Request) -> AuthManager:
    return request.app.state.auth_manager


def get_request_logger(request: Request) -> RequestLogger:
    return request.app.state.request_logger


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


# ---- Auth helpers ----

async def get_request_user(request: Request) -> dict[str, Any] | None:
    auth: AuthManager = request.app.state.auth_manager
    return await auth.get_user_from_token(request.cookies.get(SESSION_COOKIE_NAME))


async def get_websocket_user(websocket: WebSocket) -> dict[str, Any] | None:
    auth: AuthManager = websocket.app.state.auth_manager
    return await auth.get_user_from_token(websocket.cookies.get(SESSION_COOKIE_NAME))


async def require_user(request: Request) -> dict[str, Any]:
    user = await get_request_user(request)
    if not user:
        raise AuthError("未登录或登录已失效。")
    return user


async def require_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        raise OriginError("非法来源。")


async def parse_json_body(request: Request) -> dict[str, object]:
    try:
        payload = await request.json()
    except Exception:
        raise AuthError("请求体格式不正确。")
    if not isinstance(payload, dict):
        raise AuthError("请求体格式不正确。")
    return payload


# ---- Cookie helpers ----

def set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https"),
        max_age=SESSION_DAYS * 24 * 60 * 60,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


# ---- Misc helpers ----

def unauthorized_json(message: str = "未登录或登录已失效。") -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=401)


def mask_key(key: str) -> str:
    if not key or len(key) <= 7:
        return "****" if key else ""
    return key[:3] + "****" + key[-4:]
