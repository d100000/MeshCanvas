from __future__ import annotations

import json as _json
import logging
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.deps import (
    database,
    auth_manager,
    _get_request_user,
    _get_admin_user,
    _require_admin,
    _html_response,
    _render_admin_login_html,
    _is_origin_allowed,
    STATIC_DIR,
    ADMIN_STATIC_DIR,
    ADMIN_SESSION_COOKIE_NAME,
    _load_global_service_settings,
)
from app.auth import AuthError, AuthManager, ADMIN_SESSION_COOKIE_NAME
from app.config import is_configured, save_settings
from app.schemas.setup import SetupRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def landing(request: Request) -> Response:
    return _html_response(STATIC_DIR / "landing.html")


@router.get("/app")
async def canvas_app(request: Request) -> Response:
    user = await _get_request_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _html_response(STATIC_DIR / "index.html")


@router.get("/setup")
async def setup_page(request: Request) -> Response:
    if is_configured():
        return RedirectResponse(url="/login", status_code=303)
    return _html_response(STATIC_DIR / "setup.html")


@router.post("/api/setup")
async def save_setup_config(request: Request, body: SetupRequest) -> JSONResponse:
    if is_configured():
        return JSONResponse({"detail": "已完成配置，无法重复设置。"}, status_code=400)
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)

    models = [{"name": m.name.strip(), "id": m.id.strip()} for m in body.models if m.name.strip() and m.id.strip()]
    if not models:
        return JSONResponse({"detail": "请至少添加一个模型。"}, status_code=400)

    config_data = {
        "base_url": body.base_url.strip(),
        "api_format": body.api_format,
        "API_key": body.API_key.strip(),
        "models": models,
    }

    try:
        save_settings(config_data)
    except Exception as exc:
        logger.exception("save_settings failed: %s", exc)
        return JSONResponse({"detail": "保存配置失败，请查看服务端日志。"}, status_code=500)

    await database.initialize()

    salt = secrets.token_hex(16)
    password_hash = AuthManager._hash_password("admin", salt)
    admin_id = await database.create_user("admin", password_hash, salt)
    if admin_id:
        await database.set_user_role(admin_id, "admin")

    if admin_id and models:
        await database.upsert_user_settings(
            admin_id,
            api_base_url=base_url,
            api_format=api_format,
            api_key=api_key,
            models_json=_json.dumps(models, ensure_ascii=False),
            firecrawl_api_key="",
            firecrawl_country="CN",
            firecrawl_timeout_ms=45000,
        )
    await database.set_global_model_config(
        api_base_url=base_url,
        api_format=api_format,
        api_key=api_key,
        models_json=_json.dumps(models, ensure_ascii=False),
        firecrawl_api_key="",
        firecrawl_country="CN",
        firecrawl_timeout_ms=45000,
        preprocess_model="",
        user_api_base_url=base_url,
        user_api_format=api_format,
        extra_params={},
        extra_headers={},
    )

    return JSONResponse({"ok": True})


@router.get("/settings")
async def settings_page(request: Request) -> Response:
    if not is_configured():
        return RedirectResponse(url="/setup", status_code=303)
    user = await _get_request_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _html_response(STATIC_DIR / "settings.html")


@router.get("/login")
async def login_page(request: Request):
    if not is_configured():
        return RedirectResponse(url="/setup", status_code=303)
    user = await _get_request_user(request)
    if user:
        return RedirectResponse(url="/app", status_code=303)
    return _html_response(STATIC_DIR / "login.html")


@router.get("/admin")
async def admin_login_page(request: Request) -> Response:
    admin = await _get_admin_user(request)
    if admin:
        role = await database.get_user_role(admin["user_id"])
        if role == "admin":
            return RedirectResponse(url="/admin/dashboard", status_code=303)
        # 有 admin_session 但账号已不是管理员：清掉无效会话并给出说明
        token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
        await auth_manager.logout(token)
        resp = RedirectResponse(url="/admin?error=forbidden", status_code=303)
        resp.delete_cookie(key=ADMIN_SESSION_COOKIE_NAME, path="/")
        return resp
    return HTMLResponse(
        _render_admin_login_html(request),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/admin/dashboard")
async def admin_dashboard_page(request: Request) -> Response:
    try:
        await _require_admin(request)
    except AuthError:
        # 必须带 error，否则用户只看到「又回到登录页」而没有任何红字说明
        return RedirectResponse(url="/admin?error=session", status_code=303)
    return _html_response(ADMIN_STATIC_DIR / "dashboard.html")
