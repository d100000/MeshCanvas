"""Ensure default administrator user exists (username admin, password admin).

Used on app startup and by `python -m app.init_db`.

- **新建**库时：创建 `admin` / `admin` 并赋予管理员角色。
- **已存在** `admin` 用户时：仅保证其 `role=admin`，并补全空设置；**不会**因密码不是默认 `admin` 而覆盖密码（避免生产环境每次重启被改回弱口令）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets

logger = logging.getLogger(__name__)

from app.auth import AuthManager
from app.config import get_settings, is_configured
from app.database import LocalDatabase


async def seed_admin_settings_if_empty(database: LocalDatabase, user_id: int) -> None:
    existing_settings = await database.get_user_settings(user_id)
    if existing_settings and existing_settings.get("api_key"):
        return
    if not is_configured():
        return
    try:
        settings = get_settings()
        models = [{"name": m.name, "id": m.id} for m in settings.models]
        await database.upsert_user_settings(
            user_id,
            api_base_url=settings.base_url,
            api_format=settings.api_format,
            api_key=settings.api_key,
            models_json=json.dumps(models, ensure_ascii=False),
            firecrawl_api_key="",
            firecrawl_country="CN",
            firecrawl_timeout_ms=45000,
        )
    except Exception:
        logger.warning("seed_admin_settings_if_empty failed for user_id=%s", user_id, exc_info=True)


async def ensure_default_admin_user(database: LocalDatabase) -> None:
    """若不存在则创建 admin/admin；若已存在则保证角色与设置，不覆盖已有密码。"""
    existing = await database.get_user_by_username("admin")
    if existing:
        role = existing.get("role", "user")
        if role != "admin":
            await database.set_user_role(existing["id"], "admin")
        await seed_admin_settings_if_empty(database, existing["id"])
        return
    salt = secrets.token_hex(16)
    password_hash = await asyncio.to_thread(AuthManager._hash_password, "admin", salt)
    admin_id = await database.create_user("admin", password_hash, salt)
    if admin_id:
        await database.set_user_role(admin_id, "admin")
        await seed_admin_settings_if_empty(database, admin_id)
