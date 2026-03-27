"""User and user_settings persistence."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.repositories.base import BaseRepository


class UserRepository(BaseRepository):
    async def count_users(self) -> int:
        return await self._run_read(self._count_users_sync, 0)

    async def create_user(self, username: str, password_hash: str, password_salt: str) -> int | None:
        return await self._run_read(self._create_user_sync, None, username, password_hash, password_salt)

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        return await self._run_read(self._get_user_by_username_sync, None, username)

    async def update_user_password(self, username: str, password_hash: str, password_salt: str) -> None:
        await self._run_write(self._update_user_password_sync, username, password_hash, password_salt)

    async def get_user_settings(self, user_id: int) -> dict[str, Any] | None:
        return await self._run_read(self._get_user_settings_sync, None, user_id)

    async def upsert_user_settings(
        self,
        user_id: int,
        *,
        api_base_url: str,
        api_format: str,
        api_key: str,
        models_json: str,
        firecrawl_api_key: str,
        firecrawl_country: str,
        firecrawl_timeout_ms: int,
    ) -> None:
        await self._run_write(
            self._upsert_user_settings_sync,
            user_id, api_base_url, api_format, api_key, models_json,
            firecrawl_api_key, firecrawl_country, firecrawl_timeout_ms,
        )

    # ---- sync implementations ----

    def _count_users_sync(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(row[0] if row else 0)

    def _create_user_sync(self, username: str, password_hash: str, password_salt: str) -> int | None:
        now = self._now()
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO users(username, password_hash, password_salt, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (username, password_hash, password_salt, now, now),
                )
            except sqlite3.IntegrityError:
                return None
            conn.commit()
            return int(cursor.lastrowid)

    def _get_user_by_username_sync(self, username: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, password_salt, created_at FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if not row:
            return None
        return {"id": int(row[0]), "username": row[1], "password_hash": row[2], "password_salt": row[3], "created_at": row[4]}

    def _update_user_password_sync(self, username: str, password_hash: str, password_salt: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, updated_at = ? WHERE username = ?",
                (password_hash, password_salt, self._now(), username),
            )
            conn.commit()

    def _get_user_settings_sync(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT api_base_url, api_format, api_key, models_json,
                          firecrawl_api_key, firecrawl_country, firecrawl_timeout_ms
                   FROM user_settings WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "api_base_url": row[0], "api_format": row[1], "api_key": row[2],
            "models": json.loads(row[3]) if row[3] else [],
            "firecrawl_api_key": row[4], "firecrawl_country": row[5], "firecrawl_timeout_ms": row[6],
        }

    def _upsert_user_settings_sync(
        self, user_id: int, api_base_url: str, api_format: str, api_key: str,
        models_json: str, firecrawl_api_key: str, firecrawl_country: str, firecrawl_timeout_ms: int,
    ) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO user_settings(
                       user_id, api_base_url, api_format, api_key, models_json,
                       firecrawl_api_key, firecrawl_country, firecrawl_timeout_ms,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       api_base_url=excluded.api_base_url, api_format=excluded.api_format,
                       api_key=excluded.api_key, models_json=excluded.models_json,
                       firecrawl_api_key=excluded.firecrawl_api_key, firecrawl_country=excluded.firecrawl_country,
                       firecrawl_timeout_ms=excluded.firecrawl_timeout_ms, updated_at=excluded.updated_at""",
                (user_id, api_base_url, api_format, api_key, models_json,
                 firecrawl_api_key, firecrawl_country, firecrawl_timeout_ms, now, now),
            )
            conn.commit()
