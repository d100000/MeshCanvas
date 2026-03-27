"""First-run setup route — creates admin account and saves global config."""

from __future__ import annotations

import asyncio
import logging
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import is_configured, save_settings
from app.core.middleware import _is_origin_allowed
from app.dependencies import get_database, get_rate_limiter
from app.services.auth_service import AuthManager

logger = logging.getLogger(__name__)

router = APIRouter()

# Prevent concurrent setup requests
_setup_lock = asyncio.Lock()


@router.post("/api/setup")
async def save_setup_config(request: Request) -> JSONResponse:
    # Rate limit by IP
    client_host = request.client.host if request.client else "unknown"
    rate_limiter = get_rate_limiter(request)
    if not await rate_limiter.allow_async(f"setup:{client_host}", limit=5, window_seconds=300):
        return JSONResponse({"detail": "请求过于频繁，请稍后再试。"}, status_code=429)

    # Origin check
    if not _is_origin_allowed(request.headers.get("origin"), request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)

    if is_configured():
        return JSONResponse({"detail": "已完成配置，无法重复设置。"}, status_code=400)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体格式不正确。"}, status_code=400)

    # ---- Admin credentials ----
    admin_username = str(payload.get("admin_username", "")).strip()
    admin_password = str(payload.get("admin_password", "")).strip()
    if not admin_username:
        return JSONResponse({"detail": "请填写管理员用户名。"}, status_code=400)
    if len(admin_username) < 3 or len(admin_username) > 32:
        return JSONResponse({"detail": "用户名需为 3-32 位。"}, status_code=400)
    if not admin_password or len(admin_password) < 8:
        return JSONResponse({"detail": "管理员密码至少需要 8 位。"}, status_code=400)
    if len(admin_password) > 128:
        return JSONResponse({"detail": "密码长度不能超过 128 位。"}, status_code=400)

    # ---- API config ----
    base_url = str(payload.get("base_url", "")).strip()
    api_format = str(payload.get("api_format", "openai")).strip()
    api_key = str(payload.get("API_key", "")).strip()
    models_raw = payload.get("models", [])

    if not base_url:
        return JSONResponse({"detail": "请填写 API 地址。"}, status_code=400)
    if not api_key:
        return JSONResponse({"detail": "请填写 API Key。"}, status_code=400)
    if not isinstance(models_raw, list) or not models_raw:
        return JSONResponse({"detail": "请至少添加一个模型。"}, status_code=400)
    if api_format not in ("openai", "anthropic"):
        return JSONResponse({"detail": "API 格式仅支持 openai 或 anthropic。"}, status_code=400)

    models = []
    seen_ids: set[str] = set()
    for item in models_raw:
        if not isinstance(item, dict):
            return JSONResponse({"detail": "模型格式不正确。"}, status_code=400)
        name = str(item.get("name", "")).strip()
        model_id = str(item.get("id", "")).strip()
        if not name or not model_id:
            return JSONResponse({"detail": "模型名称和模型 ID 不能为空。"}, status_code=400)
        if model_id in seen_ids:
            return JSONResponse({"detail": f"模型 ID \u201c{model_id}\u201d 重复。"}, status_code=400)
        seen_ids.add(model_id)
        models.append({"name": name, "id": model_id})

    # ---- Firecrawl config ----
    firecrawl_api_key = str(payload.get("firecrawl_api_key", "")).strip()
    firecrawl_country = str(payload.get("firecrawl_country", "CN")).strip() or "CN"
    try:
        firecrawl_timeout_ms = max(5000, min(int(payload.get("firecrawl_timeout_ms", 45000)), 120000))
    except (TypeError, ValueError):
        firecrawl_timeout_ms = 45000

    # ---- Acquire lock to prevent concurrent setup ----
    if _setup_lock.locked():
        return JSONResponse({"detail": "初始化正在进行中，请稍候。"}, status_code=409)

    async with _setup_lock:
        # Double-check after acquiring lock
        if is_configured():
            return JSONResponse({"detail": "已完成配置，无法重复设置。"}, status_code=400)

        config_data = {
            "base_url": base_url,
            "api_format": api_format,
            "API_key": api_key,
            "models": models,
            "firecrawl_api_key": firecrawl_api_key,
            "firecrawl_country": firecrawl_country,
            "firecrawl_timeout_ms": firecrawl_timeout_ms,
        }

        try:
            save_settings(config_data)
        except Exception as exc:
            logger.exception("save_setup_config failed: %s", exc)
            return JSONResponse({"detail": "保存配置失败，请检查服务端日志。"}, status_code=500)

        # ---- Initialize database & create admin ----
        database = get_database(request)
        await database.initialize()

        salt = secrets.token_hex(16)
        password_hash = AuthManager._hash_password(admin_password, salt)
        admin_id = await database.create_user(admin_username, password_hash, salt)

        if not admin_id:
            return JSONResponse({"detail": "管理员账号创建失败（用户名可能已存在）。"}, status_code=500)

    logger.info("setup complete: admin user '%s' created, config saved", admin_username)
    return JSONResponse({"ok": True})
