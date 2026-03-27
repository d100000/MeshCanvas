from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from app.database import LocalDatabase

SESSION_COOKIE_NAME = "canvas_session"
ADMIN_SESSION_COOKIE_NAME = "admin_session"
SESSION_DAYS = 14
PASSWORD_ITERATIONS = 100_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


class AuthError(Exception):
    pass


class AuthManager:
    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    async def register(self, username: str, password: str) -> tuple[dict[str, Any], str, str]:
        normalized = self._normalize_username(username)
        self._validate_password(password)
        salt = secrets.token_hex(16)
        password_hash = await asyncio.to_thread(self._hash_password, password, salt)
        user_id = await self.database.create_user(normalized, password_hash, salt)
        if user_id is None:
            raise AuthError("用户名已存在。")
        user = {"id": user_id, "username": normalized}
        token, expires_at = await self._create_session(user_id)
        return user, token, expires_at

    async def login(self, username: str, password: str) -> tuple[dict[str, Any], str, str]:
        normalized = self._normalize_username(username)
        user = await self.database.get_user_by_username(normalized)
        if not user:
            raise AuthError("用户名或密码错误。")
        expected = await asyncio.to_thread(self._hash_password, password, user["password_salt"])
        if not hmac.compare_digest(expected, user["password_hash"]):
            raise AuthError("用户名或密码错误。")
        user_id = int(user["id"])
        # 角色以数据库 users.role 为准，避免 get_user_by_username 降级路径返回错误的默认 user
        role = await self.database.get_user_role(user_id)
        token, expires_at = await self._create_session(user_id)
        return {"id": user_id, "user_id": user_id, "username": user["username"], "role": role}, token, expires_at

    async def admin_login(self, username: str, password: str) -> tuple[dict[str, Any], str, str]:
        user_info, token, expires_at = await self.login(username, password)
        if user_info.get("role") != "admin":
            # 角色不符：清除已写入 DB 的 session，避免残留孤儿记录
            await self.logout(token)
            raise AuthError("该账号没有管理员权限。")
        return user_info, token, expires_at

    async def get_user_from_token(self, raw_token: str | None) -> dict[str, Any] | None:
        if not raw_token:
            return None
        token_hash = self._hash_token(raw_token)
        session = await self.database.get_session_user(token_hash)
        if not session:
            return None
        await self.database.touch_session(token_hash)
        user_id = int(session["user_id"])
        return {
            "id": user_id,
            "user_id": user_id,
            "username": session["username"],
            "expires_at": session["expires_at"],
        }

    async def logout(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        await self.database.delete_session(self._hash_token(raw_token))

    async def has_any_users(self) -> bool:
        return (await self.database.count_users()) > 0

    async def _create_session(self, user_id: int) -> tuple[str, str]:
        raw_token = secrets.token_urlsafe(32)
        expires_at = (datetime.now().astimezone() + timedelta(days=SESSION_DAYS)).isoformat()
        await self.database.create_session(user_id, self._hash_token(raw_token), expires_at)
        return raw_token, expires_at

    @staticmethod
    def _normalize_username(username: str) -> str:
        normalized = username.strip()
        if not USERNAME_RE.fullmatch(normalized):
            raise AuthError("用户名需为 3-32 位，仅支持字母、数字、点、下划线和短横线。")
        return normalized

    @staticmethod
    def _validate_password(password: str) -> None:
        """校验用户通过接口提交的密码。不适用于系统内部生成的初始密码（如 admin/admin）。"""
        if len(password) < 8:
            raise AuthError("密码至少需要 8 位。")
        if len(password) > 128:
            raise AuthError("密码长度不能超过 128 位。")

    @staticmethod
    def _hash_password(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            PASSWORD_ITERATIONS,
        ).hex()

    @staticmethod
    def _hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
