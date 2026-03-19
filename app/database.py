from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_database_path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3


class LocalDatabase:
    def __init__(self, db_path: Path | None = None) -> None:
        configured_path = db_path or get_database_path()
        self.db_path = Path(configured_path).resolve()
        self._lock = asyncio.Lock()
        self._local = threading.local()

    async def initialize(self) -> Path:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)
        return self.db_path

    def initialize_sync(self) -> Path:
        self._initialize_sync()
        return self.db_path

    async def delete_expired_sessions(self) -> None:
        await self._run_write(self._delete_expired_sessions_sync)

    async def count_users(self) -> int:
        return await self._run_read(self._count_users_sync, 0)

    async def create_user(self, username: str, password_hash: str, password_salt: str) -> int | None:
        return await self._run_read(self._create_user_sync, None, username, password_hash, password_salt)

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        return await self._run_read(self._get_user_by_username_sync, None, username)

    async def create_session(self, user_id: int, token_hash: str, expires_at: str) -> None:
        await self._run_write(self._create_session_sync, user_id, token_hash, expires_at)

    async def get_session_user(self, token_hash: str) -> dict[str, Any] | None:
        return await self._run_read(self._get_session_user_sync, None, token_hash)

    async def touch_session(self, token_hash: str) -> None:
        await self._run_write(self._touch_session_sync, token_hash)

    async def delete_session(self, token_hash: str) -> None:
        await self._run_write(self._delete_session_sync, token_hash)

    async def record_chat_request(
        self,
        *,
        request_id: str,
        client_id: str,
        models: list[str],
        user_message: str,
        discussion_rounds: int,
        search_enabled: bool,
        think_enabled: bool,
        parent_request_id: str | None = None,
        source_model: str | None = None,
        source_round: int | None = None,
        status: str = "queued",
        canvas_id: str | None = None,
        user_id: int | None = None,
    ) -> None:
        await self._run_write(
            self._record_chat_request_sync,
            request_id,
            client_id,
            models,
            user_message,
            discussion_rounds,
            search_enabled,
            think_enabled,
            parent_request_id,
            source_model,
            source_round,
            status,
            canvas_id,
            user_id,
        )

    async def mark_request_status(self, request_id: str, status: str) -> None:
        await self._run_write(self._mark_request_status_sync, request_id, status)

    async def record_model_result(
        self,
        *,
        request_id: str,
        model: str,
        round_number: int,
        status: str,
        content: str | None = None,
        error_text: str | None = None,
        duration_ms: float | None = None,
        response_length: int | None = None,
    ) -> None:
        await self._run_write(
            self._record_model_result_sync,
            request_id,
            model,
            round_number,
            status,
            content,
            error_text,
            duration_ms,
            response_length,
        )

    async def record_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        request_id: str | None = None,
        client_id: str | None = None,
    ) -> None:
        await self._run_write(self._record_event_sync, event_type, payload, request_id, client_id)

    async def create_canvas(self, user_id: int, name: str) -> str:
        return await self._run_read(self._create_canvas_sync, "", user_id, name)

    async def get_canvases(self, user_id: int) -> list[dict[str, Any]]:
        return await self._run_read(self._get_canvases_sync, [], user_id)

    async def rename_canvas(self, canvas_id: str, user_id: int, name: str) -> bool:
        return await self._run_read(self._rename_canvas_sync, False, canvas_id, user_id, name)

    async def delete_canvas(self, canvas_id: str, user_id: int) -> bool:
        return await self._run_read(self._delete_canvas_sync, False, canvas_id, user_id)

    async def get_canvas_state(self, canvas_id: str, user_id: int) -> dict[str, Any] | None:
        return await self._run_read(self._get_canvas_state_sync, None, canvas_id, user_id)

    async def upsert_cluster_position(
        self, request_id: str, user_id: int, user_x: float, user_y: float, model_y: float
    ) -> bool:
        return await self._run_read(
            self._upsert_cluster_position_sync,
            False,
            request_id,
            user_id,
            user_x,
            user_y,
            model_y,
        )

    async def get_request_with_results(self, request_id: str, user_id: int) -> dict[str, Any] | None:
        return await self._run_read(self._get_request_with_results_sync, None, request_id, user_id)

    async def clear_canvas_requests(self, canvas_id: str, user_id: int) -> None:
        await self._run_write(self._clear_canvas_requests_sync, canvas_id, user_id)

    async def _run_write(self, fn, *args) -> None:
        try:
            async with self._lock:
                await asyncio.to_thread(fn, *args)
        except Exception:
            logger.exception("database write error in %s", fn.__name__)

    async def _run_read(self, fn, default: Any, *args) -> Any:
        try:
            async with self._lock:
                return await asyncio.to_thread(fn, *args)
        except Exception:
            logger.exception("database read error in %s", fn.__name__)
            return default

    def _initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS chat_requests (
                    request_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    canvas_id TEXT,
                    user_id INTEGER,
                    parent_request_id TEXT,
                    source_model TEXT,
                    source_round INTEGER,
                    models_json TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    discussion_rounds INTEGER NOT NULL,
                    search_enabled INTEGER NOT NULL,
                    think_enabled INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    round_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    content TEXT,
                    error_text TEXT,
                    duration_ms REAL,
                    response_length INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES chat_requests(request_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS request_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT,
                    client_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS canvases (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS cluster_positions (
                    request_id TEXT PRIMARY KEY,
                    user_x REAL NOT NULL,
                    user_y REAL NOT NULL,
                    model_y REAL NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES chat_requests(request_id) ON DELETE CASCADE
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
            )
            now = self._now()
            # Ensure older local databases pick up later-added chat request columns.
            self._ensure_chat_requests_schema_sync(conn)
            current_version = self._get_schema_version_sync(conn)
            if current_version < 3:
                self._migrate_v2_to_v3_sync(conn)
            conn.execute(
                """
                INSERT INTO app_meta(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                ("schema_version", str(SCHEMA_VERSION), now),
            )
            conn.commit()
        self._delete_expired_sessions_sync()

    def _count_users_sync(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(row[0] if row else 0)

    def _create_user_sync(self, username: str, password_hash: str, password_salt: str) -> int | None:
        now = self._now()
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO users(username, password_hash, password_salt, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
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
        return {
            "id": int(row[0]),
            "username": row[1],
            "password_hash": row[2],
            "password_salt": row[3],
            "created_at": row[4],
        }

    def _create_session_sync(self, user_id: int, token_hash: str, expires_at: str) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions(token_hash, user_id, expires_at, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(token_hash) DO UPDATE SET
                    user_id=excluded.user_id,
                    expires_at=excluded.expires_at,
                    last_seen_at=excluded.last_seen_at
                """,
                (token_hash, user_id, expires_at, now, now),
            )
            conn.commit()

    def _get_session_user_sync(self, token_hash: str) -> dict[str, Any] | None:
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.token_hash, s.user_id, s.expires_at, u.username
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
        if not row:
            return None
        return {
            "token_hash": row[0],
            "user_id": int(row[1]),
            "expires_at": row[2],
            "username": row[3],
        }

    def _touch_session_sync(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                (self._now(), token_hash),
            )
            conn.commit()

    def _delete_session_sync(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            conn.commit()

    def _delete_expired_sessions_sync(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (self._now(),))
            conn.commit()

    def _record_chat_request_sync(
        self,
        request_id: str,
        client_id: str,
        models: list[str],
        user_message: str,
        discussion_rounds: int,
        search_enabled: bool,
        think_enabled: bool,
        parent_request_id: str | None,
        source_model: str | None,
        source_round: int | None,
        status: str,
        canvas_id: str | None,
        user_id: int | None,
    ) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_requests(
                    request_id, client_id, canvas_id, user_id,
                    parent_request_id, source_model, source_round,
                    models_json, user_message, discussion_rounds, search_enabled,
                    think_enabled, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    client_id=excluded.client_id,
                    canvas_id=excluded.canvas_id,
                    user_id=excluded.user_id,
                    parent_request_id=excluded.parent_request_id,
                    source_model=excluded.source_model,
                    source_round=excluded.source_round,
                    models_json=excluded.models_json,
                    user_message=excluded.user_message,
                    discussion_rounds=excluded.discussion_rounds,
                    search_enabled=excluded.search_enabled,
                    think_enabled=excluded.think_enabled,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    request_id,
                    client_id,
                    canvas_id,
                    user_id,
                    parent_request_id,
                    source_model,
                    source_round,
                    json.dumps(models, ensure_ascii=False),
                    user_message,
                    discussion_rounds,
                    int(search_enabled),
                    int(think_enabled),
                    status,
                    now,
                    now,
                ),
            )
            conn.commit()

    def _mark_request_status_sync(self, request_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_requests SET status = ?, updated_at = ? WHERE request_id = ?",
                (status, self._now(), request_id),
            )
            conn.commit()

    def _record_model_result_sync(
        self,
        request_id: str,
        model: str,
        round_number: int,
        status: str,
        content: str | None,
        error_text: str | None,
        duration_ms: float | None,
        response_length: int | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO model_results(
                    request_id, model, round_number, status, content,
                    error_text, duration_ms, response_length, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    model,
                    round_number,
                    status,
                    content,
                    error_text,
                    duration_ms,
                    response_length,
                    self._now(),
                ),
            )
            conn.commit()

    def _record_event_sync(
        self,
        event_type: str,
        payload: dict[str, Any],
        request_id: str | None,
        client_id: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO request_events(request_id, client_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    client_id,
                    event_type,
                    json.dumps(payload, ensure_ascii=False),
                    self._now(),
                ),
            )
            conn.commit()

    def _get_schema_version_sync(self, conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute("SELECT value FROM app_meta WHERE key = 'schema_version'").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _ensure_chat_requests_schema_sync(self, conn: sqlite3.Connection) -> None:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(chat_requests)").fetchall()
        }
        for column_name, col_sql in {
            "canvas_id": "canvas_id TEXT",
            "user_id": "user_id INTEGER",
        }.items():
            if column_name in columns:
                continue
            conn.execute(f"ALTER TABLE chat_requests ADD COLUMN {col_sql}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_requests_canvas_id ON chat_requests(canvas_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_requests_user_id ON chat_requests(user_id)"
        )
        conn.commit()

    def _migrate_v2_to_v3_sync(self, conn: sqlite3.Connection) -> None:
        self._ensure_chat_requests_schema_sync(conn)

    def _create_canvas_sync(self, user_id: int, name: str) -> str:
        from uuid import uuid4
        canvas_id = uuid4().hex
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO canvases(id, user_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (canvas_id, user_id, name, now, now),
            )
            conn.commit()
        return canvas_id

    def _get_canvases_sync(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, created_at, updated_at FROM canvases WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
        return [{"id": r[0], "name": r[1], "created_at": r[2], "updated_at": r[3]} for r in rows]

    def _rename_canvas_sync(self, canvas_id: str, user_id: int, name: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE canvases SET name = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (name, self._now(), canvas_id, user_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def _delete_canvas_sync(self, canvas_id: str, user_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM canvases WHERE id = ? AND user_id = ?",
                (canvas_id, user_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def _get_canvas_state_sync(self, canvas_id: str, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            canvas_row = conn.execute(
                "SELECT id, name, created_at FROM canvases WHERE id = ? AND user_id = ?",
                (canvas_id, user_id),
            ).fetchone()
            if not canvas_row:
                return None
            request_rows = conn.execute(
                """
                SELECT cr.request_id, cr.user_message, cr.models_json, cr.discussion_rounds,
                       cr.search_enabled, cr.think_enabled, cr.parent_request_id,
                       cr.source_model, cr.source_round, cr.status, cr.created_at,
                       cp.user_x, cp.user_y, cp.model_y
                FROM chat_requests cr
                LEFT JOIN cluster_positions cp ON cp.request_id = cr.request_id
                WHERE cr.canvas_id = ? AND cr.user_id = ?
                ORDER BY cr.created_at ASC
                """,
                (canvas_id, user_id),
            ).fetchall()
            requests = []
            for row in request_rows:
                request_id = row[0]
                result_rows = conn.execute(
                    """
                    SELECT model, round_number, status, content, error_text
                    FROM model_results WHERE request_id = ?
                    ORDER BY round_number ASC, model ASC
                    """,
                    (request_id,),
                ).fetchall()
                results = [
                    {"model": r[0], "round": r[1], "status": r[2], "content": r[3], "error_text": r[4]}
                    for r in result_rows
                ]
                position = (
                    {"user_x": row[11], "user_y": row[12], "model_y": row[13]}
                    if row[11] is not None else None
                )
                requests.append({
                    "request_id": row[0],
                    "user_message": row[1],
                    "models": json.loads(row[2]),
                    "discussion_rounds": row[3],
                    "search_enabled": bool(row[4]),
                    "think_enabled": bool(row[5]),
                    "parent_request_id": row[6],
                    "source_model": row[7],
                    "source_round": row[8],
                    "status": row[9],
                    "created_at": row[10],
                    "position": position,
                    "results": results,
                })
        return {"canvas_id": canvas_row[0], "name": canvas_row[1], "created_at": canvas_row[2], "requests": requests}

    def _upsert_cluster_position_sync(
        self, request_id: str, user_id: int, user_x: float, user_y: float, model_y: float
    ) -> bool:
        now = self._now()
        with self._connect() as conn:
            owner = conn.execute(
                "SELECT 1 FROM chat_requests WHERE request_id = ? AND user_id = ?",
                (request_id, user_id),
            ).fetchone()
            if not owner:
                return False
            conn.execute(
                """
                INSERT INTO cluster_positions(request_id, user_x, user_y, model_y, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    user_x=excluded.user_x, user_y=excluded.user_y,
                    model_y=excluded.model_y, updated_at=excluded.updated_at
                """,
                (request_id, user_x, user_y, model_y, now),
            )
            conn.commit()
        return True

    def _get_request_with_results_sync(self, request_id: str, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT request_id, user_message, models_json, discussion_rounds,
                       search_enabled, think_enabled, parent_request_id, source_model, source_round
                FROM chat_requests WHERE request_id = ? AND user_id = ?
                """,
                (request_id, user_id),
            ).fetchone()
            if not row:
                return None
            result_rows = conn.execute(
                """
                SELECT model, round_number, status, content
                FROM model_results WHERE request_id = ?
                ORDER BY model ASC, round_number ASC
                """,
                (request_id,),
            ).fetchall()
        model_results: dict[str, list[dict[str, Any]]] = {}
        for r in result_rows:
            model = r[0]
            if model not in model_results:
                model_results[model] = []
            model_results[model].append({"round": r[1], "status": r[2], "content": r[3] or ""})
        return {
            "request_id": row[0],
            "user_message": row[1],
            "models": json.loads(row[2]),
            "discussion_rounds": row[3],
            "search_enabled": bool(row[4]),
            "think_enabled": bool(row[5]),
            "parent_request_id": row[6],
            "source_model": row[7],
            "source_round": row[8],
            "model_results": model_results,
        }

    def _clear_canvas_requests_sync(self, canvas_id: str, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM chat_requests WHERE canvas_id = ? AND user_id = ?",
                (canvas_id, user_id),
            )
            conn.commit()

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


def init_database_sync(db_path: Path | None = None) -> Path:
    return LocalDatabase(db_path=db_path).initialize_sync()
