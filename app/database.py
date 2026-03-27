"""Database facade — composes domain repositories and owns schema initialization.

Existing code can continue calling `database.create_user(...)` etc.
New code should prefer injecting the specific repository directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import get_database_path
from app.repositories.base import BaseRepository
from app.repositories.canvas_repo import CanvasRepository
from app.repositories.chat_repo import ChatRepository
from app.repositories.event_repo import EventRepository
from app.repositories.session_repo import SessionRepository
from app.repositories.user_repo import UserRepository

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4


class LocalDatabase:
    """Thin facade — delegates to domain-specific repositories."""

    def __init__(self, db_path: Path | None = None) -> None:
        configured_path = db_path or get_database_path()
        self.db_path = Path(configured_path).resolve()

        # All repos share the same db_path (each manages its own thread-local connection).
        self.users = UserRepository(self.db_path)
        self.sessions = SessionRepository(self.db_path)
        self.chats = ChatRepository(self.db_path)
        self.canvases = CanvasRepository(self.db_path)
        self.events = EventRepository(self.db_path)

        # For schema init we still need our own connection resources.
        self._lock = asyncio.Lock()
        self._local = threading.local()

    # -------- Initialization / Migration --------

    async def initialize(self) -> Path:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)
        return self.db_path

    def initialize_sync(self) -> Path:
        self._initialize_sync()
        return self.db_path

    # -------- Delegated facade methods --------
    # These keep backward compatibility so existing call sites keep working.

    async def delete_expired_sessions(self) -> None:
        await self.sessions.delete_expired_sessions()

    async def count_users(self) -> int:
        return await self.users.count_users()

    async def create_user(self, username: str, password_hash: str, password_salt: str) -> int | None:
        return await self.users.create_user(username, password_hash, password_salt)

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        return await self.users.get_user_by_username(username)

    async def update_user_password(self, username: str, password_hash: str, password_salt: str) -> None:
        await self.users.update_user_password(username, password_hash, password_salt)

    async def get_user_settings(self, user_id: int) -> dict[str, Any] | None:
        return await self.users.get_user_settings(user_id)

    async def upsert_user_settings(self, user_id: int, **kwargs) -> None:
        await self.users.upsert_user_settings(user_id, **kwargs)

    async def create_session(self, user_id: int, token_hash: str, expires_at: str) -> None:
        await self.sessions.create_session(user_id, token_hash, expires_at)

    async def get_session_user(self, token_hash: str) -> dict[str, Any] | None:
        return await self.sessions.get_session_user(token_hash)

    async def touch_session(self, token_hash: str) -> None:
        await self.sessions.touch_session(token_hash)

    async def delete_session(self, token_hash: str) -> None:
        await self.sessions.delete_session(token_hash)

    async def record_chat_request(self, **kwargs) -> None:
        await self.chats.record_chat_request(**kwargs)

    async def mark_request_status(self, request_id: str, status: str) -> None:
        await self.chats.mark_request_status(request_id, status)

    async def record_model_result(self, **kwargs) -> None:
        await self.chats.record_model_result(**kwargs)

    async def record_event(self, **kwargs) -> None:
        await self.events.record_event(**kwargs)

    async def create_canvas(self, user_id: int, name: str) -> str:
        return await self.canvases.create_canvas(user_id, name)

    async def get_canvases(self, user_id: int) -> list[dict[str, Any]]:
        return await self.canvases.get_canvases(user_id)

    async def rename_canvas(self, canvas_id: str, user_id: int, name: str) -> bool:
        return await self.canvases.rename_canvas(canvas_id, user_id, name)

    async def delete_canvas(self, canvas_id: str, user_id: int) -> bool:
        return await self.canvases.delete_canvas(canvas_id, user_id)

    async def get_canvas_state(self, canvas_id: str, user_id: int) -> dict[str, Any] | None:
        return await self.canvases.get_canvas_state(canvas_id, user_id)

    async def upsert_cluster_position(self, request_id: str, user_id: int, user_x: float, user_y: float, model_y: float) -> bool:
        return await self.canvases.upsert_cluster_position(request_id, user_id, user_x, user_y, model_y)

    async def get_request_with_results(self, request_id: str, user_id: int) -> dict[str, Any] | None:
        return await self.chats.get_request_with_results(request_id, user_id)

    async def clear_canvas_requests(self, canvas_id: str, user_id: int) -> None:
        await self.canvases.clear_canvas_requests(canvas_id, user_id)

    # -------- Schema init (private) --------

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conn = None
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        self._local.conn = conn
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat()

    def _initialize_sync(self) -> None:
        logger.info("initializing database at %s", self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_DDL)
            self._ensure_chat_requests_schema_sync(conn)
            current_version = self._get_schema_version_sync(conn)
            if current_version < SCHEMA_VERSION:
                logger.info("migrating database schema from v%d to v%d", current_version, SCHEMA_VERSION)
            if current_version < 3:
                self._migrate_v2_to_v3_sync(conn)
            if current_version < 4:
                self._migrate_v3_to_v4_sync(conn)
            conn.execute(
                """INSERT INTO app_meta(key, value, updated_at)
                   VALUES(?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                ("schema_version", str(SCHEMA_VERSION), self._now()),
            )
            conn.commit()
        # Clean up expired sessions on every init.
        self.sessions._delete_expired_sessions_sync()
        logger.info("database ready, schema version %d", SCHEMA_VERSION)

    def _get_schema_version_sync(self, conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute("SELECT value FROM app_meta WHERE key = 'schema_version'").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _ensure_chat_requests_schema_sync(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_requests)").fetchall()}
        for column_name, col_sql in {"canvas_id": "canvas_id TEXT", "user_id": "user_id INTEGER"}.items():
            if column_name in columns:
                continue
            conn.execute(f"ALTER TABLE chat_requests ADD COLUMN {col_sql}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_requests_canvas_id ON chat_requests(canvas_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_requests_user_id ON chat_requests(user_id)")
        conn.commit()

    def _migrate_v2_to_v3_sync(self, conn: sqlite3.Connection) -> None:
        self._ensure_chat_requests_schema_sync(conn)

    def _migrate_v3_to_v4_sync(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """CREATE TABLE IF NOT EXISTS user_settings (
                   user_id INTEGER PRIMARY KEY,
                   api_base_url TEXT NOT NULL DEFAULT '',
                   api_format TEXT NOT NULL DEFAULT 'openai',
                   api_key TEXT NOT NULL DEFAULT '',
                   models_json TEXT NOT NULL DEFAULT '[]',
                   firecrawl_api_key TEXT NOT NULL DEFAULT '',
                   firecrawl_country TEXT NOT NULL DEFAULT 'CN',
                   firecrawl_timeout_ms INTEGER NOT NULL DEFAULT 45000,
                   created_at TEXT NOT NULL DEFAULT '',
                   updated_at TEXT NOT NULL DEFAULT '',
                   FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
               );"""
        )


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, password_salt TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS chat_requests (
    request_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, canvas_id TEXT, user_id INTEGER,
    parent_request_id TEXT, source_model TEXT, source_round INTEGER,
    models_json TEXT NOT NULL, user_message TEXT NOT NULL, discussion_rounds INTEGER NOT NULL,
    search_enabled INTEGER NOT NULL, think_enabled INTEGER NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
    model TEXT NOT NULL, round_number INTEGER NOT NULL, status TEXT NOT NULL,
    content TEXT, error_text TEXT, duration_ms REAL, response_length INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES chat_requests(request_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS request_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, client_id TEXT,
    event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canvases (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS cluster_positions (
    request_id TEXT PRIMARY KEY, user_x REAL NOT NULL, user_y REAL NOT NULL,
    model_y REAL NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES chat_requests(request_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    api_base_url TEXT NOT NULL DEFAULT '', api_format TEXT NOT NULL DEFAULT 'openai',
    api_key TEXT NOT NULL DEFAULT '', models_json TEXT NOT NULL DEFAULT '[]',
    firecrawl_api_key TEXT NOT NULL DEFAULT '', firecrawl_country TEXT NOT NULL DEFAULT 'CN',
    firecrawl_timeout_ms INTEGER NOT NULL DEFAULT 45000,
    created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_chat_requests_created_at ON chat_requests(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_requests_status ON chat_requests(status);
CREATE INDEX IF NOT EXISTS idx_model_results_request_round ON model_results(request_id, round_number, model);
CREATE INDEX IF NOT EXISTS idx_request_events_request_id ON request_events(request_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_canvases_user_id ON canvases(user_id);
"""


def init_database_sync(db_path: Path | None = None) -> Path:
    return LocalDatabase(db_path=db_path).initialize_sync()
