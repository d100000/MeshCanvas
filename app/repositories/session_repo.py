"""Session persistence."""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepository


class SessionRepository(BaseRepository):
    async def create_session(self, user_id: int, token_hash: str, expires_at: str) -> None:
        await self._run_write(self._create_session_sync, user_id, token_hash, expires_at)

    async def get_session_user(self, token_hash: str) -> dict[str, Any] | None:
        return await self._run_read(self._get_session_user_sync, None, token_hash)

    async def touch_session(self, token_hash: str) -> None:
        await self._run_write(self._touch_session_sync, token_hash, suppress=True)

    async def delete_session(self, token_hash: str) -> None:
        await self._run_write(self._delete_session_sync, token_hash)

    async def delete_expired_sessions(self) -> None:
        await self._run_write(self._delete_expired_sessions_sync)

    # ---- sync ----

    def _create_session_sync(self, user_id: int, token_hash: str, expires_at: str) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO sessions(token_hash, user_id, expires_at, created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(token_hash) DO UPDATE SET
                       user_id=excluded.user_id, expires_at=excluded.expires_at, last_seen_at=excluded.last_seen_at""",
                (token_hash, user_id, expires_at, now, now),
            )
            conn.commit()

    def _get_session_user_sync(self, token_hash: str) -> dict[str, Any] | None:
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT s.token_hash, s.user_id, s.expires_at, u.username
                   FROM sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = ? AND s.expires_at > ?""",
                (token_hash, now),
            ).fetchone()
        if not row:
            return None
        return {"token_hash": row[0], "user_id": int(row[1]), "expires_at": row[2], "username": row[3]}

    def _touch_session_sync(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (self._now(), token_hash))
            conn.commit()

    def _delete_session_sync(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            conn.commit()

    def _delete_expired_sessions_sync(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (self._now(),))
            conn.commit()
