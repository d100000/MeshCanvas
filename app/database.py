from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_database_path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 8


_TOUCH_SESSION_DEBOUNCE_SECONDS = 300  # 同一 token 5 分钟内最多写一次
_TOUCH_CACHE_MAX_ENTRIES = 10_000


def _safe_json_list(raw: Any) -> list:
    """Defensively deserialize a JSON array column. Never raises."""
    try:
        if not raw:
            return []
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def _safe_json_dict(raw: Any) -> dict:
    """Defensively deserialize a JSON object column. Never raises."""
    try:
        if not raw:
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


class LocalDatabase:
    def __init__(self, db_path: Path | None = None) -> None:
        configured_path = db_path or get_database_path()
        self.db_path = Path(configured_path).resolve()
        self._lock = asyncio.Lock()
        self._local = threading.local()
        # touch_session 去抖：token_hash -> 上次写入的 monotonic 时间戳
        self._touch_cache: dict[str, float] = {}
        self._touch_cache_lock = threading.Lock()

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
        # 写操作：失败时抛出，避免被误判为"用户名已存在"
        async with self._lock:
            return await asyncio.to_thread(self._create_user_sync, username, password_hash, password_salt)

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        return await self._run_read(self._get_user_by_username_sync, None, username)

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """返回 id / username / role（不含密码哈希）。"""
        return await self._run_read(self._get_user_by_id_sync, None, user_id)

    async def count_users_with_role(self, role: str) -> int:
        """统计具备指定 role 的用户数（NULL/空视为 user）。"""
        return await self._run_read(self._count_users_with_role_sync, 0, role)

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
            user_id,
            api_base_url,
            api_format,
            api_key,
            models_json,
            firecrawl_api_key,
            firecrawl_country,
            firecrawl_timeout_ms,
        )

    async def create_session(self, user_id: int, token_hash: str, expires_at: str) -> None:
        # 会话写入失败必须向上抛出，避免已下发 Cookie 但库中无记录导致「永远登不进后台」
        async with self._lock:
            await asyncio.to_thread(self._create_session_sync, user_id, token_hash, expires_at)

    async def get_session_user(self, token_hash: str) -> dict[str, Any] | None:
        return await self._run_read(self._get_session_user_sync, None, token_hash)

    async def touch_session(self, token_hash: str) -> None:
        now = time.monotonic()
        with self._touch_cache_lock:
            last = self._touch_cache.get(token_hash, 0.0)
            if now - last < _TOUCH_SESSION_DEBOUNCE_SECONDS:
                return
            if len(self._touch_cache) >= _TOUCH_CACHE_MAX_ENTRIES:
                oldest_key = min(self._touch_cache, key=self._touch_cache.get)
                del self._touch_cache[oldest_key]
            self._touch_cache[token_hash] = now
        await self._run_write_silent(self._touch_session_sync, token_hash)

    async def delete_session(self, token_hash: str) -> None:
        with self._touch_cache_lock:
            self._touch_cache.pop(token_hash, None)
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
        context_node_ids: list[str] | None = None,
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
            context_node_ids,
        )

    async def mark_request_status(self, request_id: str, status: str) -> None:
        await self._run_write_silent(self._mark_request_status_sync, request_id, status)

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
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        await self._run_write_silent(
            self._record_model_result_sync,
            request_id,
            model,
            round_number,
            status,
            content,
            error_text,
            duration_ms,
            response_length,
            prompt_tokens,
            completion_tokens,
        )

    async def record_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        request_id: str | None = None,
        client_id: str | None = None,
    ) -> None:
        await self._run_write_silent(self._record_event_sync, event_type, payload, request_id, client_id)

    async def create_canvas(self, user_id: int, name: str) -> str:
        return await self._run_read(self._create_canvas_sync, "", user_id, name)

    async def get_canvases(self, user_id: int) -> list[dict[str, Any]]:
        return await self._run_read(self._get_canvases_sync, [], user_id)

    async def rename_canvas(self, canvas_id: str, user_id: int, name: str) -> bool:
        # 写操作：改名失败应抛出；_rename_canvas_sync 返回 bool 表示「找到了行」
        try:
            async with self._lock:
                return await asyncio.to_thread(self._rename_canvas_sync, canvas_id, user_id, name)
        except Exception:
            logger.exception("rename_canvas failed canvas_id=%s", canvas_id)
            raise

    async def delete_canvas(self, canvas_id: str, user_id: int) -> bool:
        try:
            async with self._lock:
                return await asyncio.to_thread(self._delete_canvas_sync, canvas_id, user_id)
        except Exception:
            logger.exception("delete_canvas failed canvas_id=%s", canvas_id)
            raise

    async def get_canvas_state(self, canvas_id: str, user_id: int) -> dict[str, Any] | None:
        return await self._run_read(self._get_canvas_state_sync, None, canvas_id, user_id)

    async def upsert_cluster_position(
        self,
        request_id: str,
        user_id: int,
        user_x: float,
        user_y: float,
        model_y: float,
        model_positions_json: str = "{}",
        conclusion_x: float | None = None,
        conclusion_y: float | None = None,
    ) -> bool:
        try:
            async with self._lock:
                return await asyncio.to_thread(
                    self._upsert_cluster_position_sync,
                    request_id,
                    user_id,
                    user_x,
                    user_y,
                    model_y,
                    model_positions_json,
                    conclusion_x,
                    conclusion_y,
                )
        except Exception:
            logger.exception("upsert_cluster_position failed request_id=%s", request_id)
            return False

    async def get_request_with_results(self, request_id: str, user_id: int) -> dict[str, Any] | None:
        return await self._run_read(self._get_request_with_results_sync, None, request_id, user_id)

    async def get_request_events(self, request_id: str, user_id: int) -> list[dict[str, Any]]:
        return await self._run_read(self._get_request_events_sync, [], request_id, user_id)

    async def clear_canvas_requests(self, canvas_id: str, user_id: int) -> None:
        await self._run_write(self._clear_canvas_requests_sync, canvas_id, user_id)

    # ── Admin / billing helpers ──

    async def get_user_role(self, user_id: int) -> str:
        return await self._run_read(self._get_user_role_sync, "user", user_id)

    async def set_user_role(self, user_id: int, role: str) -> None:
        await self._run_write(self._set_user_role_sync, user_id, role)

    async def get_user_balance(self, user_id: int) -> float:
        return await self._run_read(self._get_user_balance_sync, 0.0, user_id)

    async def ensure_user_balance(self, user_id: int) -> None:
        await self._run_write(self._ensure_user_balance_sync, user_id)

    async def add_points(self, user_id: int, points: float, admin_id: int, remark: str = "") -> None:
        await self._run_write(self._add_points_sync, user_id, points, admin_id, remark)

    async def add_points_non_negative(self, user_id: int, points: float, admin_id: int, remark: str = "") -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._add_points_non_negative_sync, user_id, points, admin_id, remark)

    async def deduct_points(self, user_id: int, points: float) -> bool:
        """原子扣减余额。余额不足返回 False，数据库异常向上抛出。"""
        async with self._lock:
            return await asyncio.to_thread(self._deduct_points_sync, user_id, points)

    async def record_token_usage(
        self, *, user_id: int, request_id: str, model: str, round_number: int,
        prompt_tokens: int, completion_tokens: int, total_tokens: int, points_consumed: float,
    ) -> None:
        await self._run_write_silent(
            self._record_token_usage_sync, user_id, request_id, model, round_number,
            prompt_tokens, completion_tokens, total_tokens, points_consumed,
        )

    async def get_user_usage_detail(self, user_id: int, limit: int = 200) -> list[dict[str, Any]]:
        return await self._run_read(self._get_user_usage_detail_sync, [], user_id, limit)

    async def get_user_usage_summary(self, user_id: int) -> dict[str, Any]:
        return await self._run_read(self._get_user_usage_summary_sync, {}, user_id)

    async def get_user_custom_keys(self, user_id: int) -> dict[str, Any]:
        """Returns {model_keys: {model_name: key, ...}, use_custom_key: bool}."""
        settings = await self.get_user_settings(user_id)
        if not settings:
            return {"model_keys": {}, "use_custom_key": False}
        raw = settings.get("api_key", "")
        if not raw:
            return {"model_keys": {}, "use_custom_key": False}
        try:
            model_keys = json.loads(raw)
            if not isinstance(model_keys, dict):
                model_keys = {"default": raw} if raw else {}
        except (json.JSONDecodeError, TypeError):
            model_keys = {"default": raw} if raw else {}
        use_flag = bool(settings.get("use_custom_key", False))
        return {"model_keys": model_keys, "use_custom_key": use_flag}

    async def set_user_custom_keys(self, user_id: int, model_keys: dict[str, str], use_custom_key: bool) -> None:
        await self._run_write(self._set_user_custom_keys_sync, user_id, model_keys, use_custom_key)

    async def get_all_pricing(self) -> list[dict[str, Any]]:
        return await self._run_read(self._get_all_pricing_sync, [])

    # ── Admin audit log ──

    async def add_admin_audit_log(
        self,
        *,
        admin_id: int,
        action: str,
        target_user_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        await self._run_write_silent(
            self._add_admin_audit_log_sync, admin_id, action, target_user_id, detail or {}
        )

    async def get_admin_audit_logs(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        action_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._run_read(
            self._get_admin_audit_logs_sync, [], limit, offset, action_filter
        )

    async def cleanup_old_model_failures(self, retention_hours: int = 24) -> int:
        """Delete failed model_results older than retention_hours."""
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_old_model_failures_sync, retention_hours)

    async def cleanup_old_events(self, retention_days: int = 30) -> int:
        """删除 request_events 和 admin_audit_logs 中超过 retention_days 的记录，返回删除总行数。"""
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_old_events_sync, retention_days)

    async def cleanup_old_token_usage(self, retention_days: int = 90) -> int:
        """删除 token_usage_logs 中超过 retention_days 的记录，返回删除总行数。"""
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_old_token_usage_sync, retention_days)

    async def cleanup_old_failure_logs(self) -> int:
        """删除超过 24 小时的失败/重试日志。"""
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_old_failure_logs_sync)
    async def upsert_pricing(self, model_id: str, display_name: str, input_per_1k: float, output_per_1k: float, is_active: int = 1) -> None:
        await self._run_write(self._upsert_pricing_sync, model_id, display_name, input_per_1k, output_per_1k, is_active)

    async def delete_pricing(self, model_id: str) -> None:
        await self._run_write(self._delete_pricing_sync, model_id)

    async def get_pricing_for_model(self, model_id: str) -> dict[str, Any] | None:
        return await self._run_read(self._get_pricing_for_model_sync, None, model_id)

    async def list_users_admin(self) -> list[dict[str, Any]]:
        return await self._run_read(self._list_users_admin_sync, [])

    async def get_usage_stats(self, user_id: int | None = None) -> list[dict[str, Any]]:
        return await self._run_read(self._get_usage_stats_sync, [], user_id)

    async def get_recharge_logs(self, user_id: int | None = None) -> list[dict[str, Any]]:
        return await self._run_read(self._get_recharge_logs_sync, [], user_id)

    async def get_system_config(self) -> dict[str, str]:
        return await self._run_read(self._get_system_config_sync, {})

    async def set_system_config(self, key: str, value: str) -> None:
        await self._run_write(self._set_system_config_sync, key, value)

    # ── Request summaries (conclusion documents) ──

    async def upsert_request_summary(
        self,
        *,
        request_id: str,
        canvas_id: str | None = None,
        summary_model: str = "",
        summary_markdown: str = "",
        status: str = "pending",
        error_message: str | None = None,
    ) -> None:
        await self._run_write_silent(
            self._upsert_request_summary_sync,
            request_id, canvas_id, summary_model, summary_markdown, status, error_message,
        )

    async def get_request_summary(self, request_id: str) -> dict[str, Any] | None:
        return await self._run_read(self._get_request_summary_sync, None, request_id)

    async def get_summaries_for_canvas(self, canvas_id: str) -> dict[str, dict[str, Any]]:
        return await self._run_read(self._get_summaries_for_canvas_sync, {}, canvas_id)

    # ── Global model / API config ──

    async def get_global_model_config(self) -> dict[str, str]:
        """读取全局模型与 API 配置（存于 app_meta，key 前缀 model_config_）。"""
        return await self._run_read(self._get_global_model_config_sync, {})

    async def set_global_model_config(
        self,
        *,
        api_base_url: str,
        api_format: str,
        api_key: str,
        models_json: str,
        firecrawl_api_key: str,
        firecrawl_country: str,
        firecrawl_timeout_ms: int,
        preprocess_model: str = "",
        user_api_base_url: str = "",
        user_api_format: str = "openai",
        extra_params: dict | None = None,
        extra_headers: dict | None = None,
    ) -> None:
        await self._run_write(
            self._set_global_model_config_sync,
            api_base_url, api_format, api_key,
            models_json, firecrawl_api_key, firecrawl_country, firecrawl_timeout_ms,
            preprocess_model, user_api_base_url, user_api_format, extra_params, extra_headers,
        )

    async def _run_write(self, fn, *args) -> None:
        """执行写操作。异常会向上抛出——调用方可决定是降级还是传播给用户。"""
        async with self._lock:
            await asyncio.to_thread(fn, *args)

    async def _run_write_silent(self, fn, *args) -> None:
        """执行写操作，记录日志但不抛出。仅用于「失败不影响主流程」的辅助写入（例如统计）。"""
        try:
            async with self._lock:
                await asyncio.to_thread(fn, *args)
        except Exception:
            logger.exception("database write error in %s (non-critical)", fn.__name__)

    async def _run_read(self, fn, default: Any, *args) -> Any:
        try:
            async with self._lock:
                return await asyncio.to_thread(fn, *args)
        except (sqlite3.DatabaseError, OSError) as exc:
            logger.exception("database read error in %s: %s", fn.__name__, exc)
            return default
        except Exception:
            logger.exception("unexpected read error in %s (re-raising)", fn.__name__)
            raise

    def _initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # executescript 会隐式提交当前事务，后续的 schema 迁移和 schema_version 写入
            # 在独立逻辑事务中完成，中途崩溃可能导致 schema_version 与实际结构不符。
            # 当前迁移量小（每步独立、幂等），实践中风险极低；若迁移变复杂可改为显式事务包裹。
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
                    updated_at TEXT NOT NULL,
                    context_node_ids TEXT NOT NULL DEFAULT '[]'
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
                    model_positions_json TEXT NOT NULL DEFAULT '{}',
                    conclusion_x REAL,
                    conclusion_y REAL,
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

                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    api_base_url TEXT NOT NULL DEFAULT '',
                    api_format TEXT NOT NULL DEFAULT 'openai',
                    api_key TEXT NOT NULL DEFAULT '',
                    models_json TEXT NOT NULL DEFAULT '[]',
                    firecrawl_api_key TEXT NOT NULL DEFAULT '',
                    firecrawl_country TEXT NOT NULL DEFAULT 'CN',
                    firecrawl_timeout_ms INTEGER NOT NULL DEFAULT 45000,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS user_balances (
                    user_id INTEGER PRIMARY KEY,
                    points REAL NOT NULL DEFAULT 0,
                    total_recharged REAL NOT NULL DEFAULT 0,
                    total_consumed REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS model_pricing (
                    model_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    input_points_per_1k REAL NOT NULL DEFAULT 1.0,
                    output_points_per_1k REAL NOT NULL DEFAULT 2.0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS token_usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    request_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    round_number INTEGER NOT NULL DEFAULT 1,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    points_consumed REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_token_usage_user ON token_usage_logs(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS recharge_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    admin_id INTEGER NOT NULL,
                    points REAL NOT NULL,
                    remark TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(admin_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS admin_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    target_user_id INTEGER,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(admin_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_admin ON admin_audit_logs(admin_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created ON admin_audit_logs(created_at DESC);

                CREATE TABLE IF NOT EXISTS request_summaries (
                    request_id TEXT PRIMARY KEY,
                    canvas_id TEXT,
                    summary_model TEXT NOT NULL DEFAULT '',
                    summary_markdown TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES chat_requests(request_id) ON DELETE CASCADE
                );
                """
            )
            now = self._now()
            self._ensure_chat_requests_schema_sync(conn)
            self._ensure_cluster_positions_schema_sync(conn)
            current_version = self._get_schema_version_sync(conn)
            if current_version < 3:
                self._migrate_v2_to_v3_sync(conn)
            if current_version < 4:
                self._migrate_v3_to_v4_sync(conn)
            if current_version < 5:
                self._migrate_v4_to_v5_sync(conn)
            if current_version < 6:
                self._migrate_v5_to_v6_sync(conn)
            if current_version < 7:
                self._migrate_v6_to_v7_sync(conn)
            if current_version < 8:
                self._migrate_v7_to_v8_sync(conn)
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
            try:
                row = conn.execute(
                    "SELECT id, username, password_hash, password_salt, created_at, role FROM users WHERE username = ?",
                    (username,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = conn.execute(
                    "SELECT id, username, password_hash, password_salt, created_at FROM users WHERE username = ?",
                    (username,),
                ).fetchone()
                if not row:
                    return None
                return {"id": int(row[0]), "username": row[1], "password_hash": row[2], "password_salt": row[3], "created_at": row[4], "role": "user"}
        if not row:
            return None
        return {
            "id": int(row[0]),
            "username": row[1],
            "password_hash": row[2],
            "password_salt": row[3],
            "created_at": row[4],
            "role": row[5] if len(row) > 5 else "user",
        }

    def _get_user_by_id_sync(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, role FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        r = row[2] if row[2] else "user"
        return {"id": int(row[0]), "username": row[1], "role": str(r)}

    def _count_users_with_role_sync(self, role: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM users WHERE COALESCE(NULLIF(TRIM(role), ''), 'user') = ?",
                (role,),
            ).fetchone()
        return int(row[0]) if row else 0

    def _update_user_password_sync(self, username: str, password_hash: str, password_salt: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, updated_at = ? WHERE username = ?",
                (password_hash, password_salt, self._now(), username),
            )
            conn.commit()

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
        context_node_ids: list[str] | None,
    ) -> None:
        now = self._now()
        ctx_json = json.dumps(context_node_ids or [], ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_requests(
                    request_id, client_id, canvas_id, user_id,
                    parent_request_id, source_model, source_round,
                    models_json, user_message, discussion_rounds, search_enabled,
                    think_enabled, status, created_at, updated_at, context_node_ids
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    updated_at=excluded.updated_at,
                    context_node_ids=excluded.context_node_ids
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
                    ctx_json,
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
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO model_results(
                    request_id, model, round_number, status, content,
                    error_text, duration_ms, response_length,
                    prompt_tokens, completion_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    prompt_tokens,
                    completion_tokens,
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
            "context_node_ids": "context_node_ids TEXT NOT NULL DEFAULT '[]'",
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

    def _ensure_cluster_positions_schema_sync(self, conn: sqlite3.Connection) -> None:
        """Idempotent add-column for cluster_positions (v8 schema)."""
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(cluster_positions)").fetchall()
        }
        for column_name, col_sql in {
            "model_positions_json": "model_positions_json TEXT NOT NULL DEFAULT '{}'",
            "conclusion_x": "conclusion_x REAL",
            "conclusion_y": "conclusion_y REAL",
        }.items():
            if column_name in columns:
                continue
            conn.execute(f"ALTER TABLE cluster_positions ADD COLUMN {col_sql}")
        conn.commit()

    def _migrate_v2_to_v3_sync(self, conn: sqlite3.Connection) -> None:
        self._ensure_chat_requests_schema_sync(conn)

    def _migrate_v3_to_v4_sync(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
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
            );
            """
        )

    def _migrate_v4_to_v5_sync(self, conn: sqlite3.Connection) -> None:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        mr_cols = {r[1] for r in conn.execute("PRAGMA table_info(model_results)").fetchall()}
        if "prompt_tokens" not in mr_cols:
            conn.execute("ALTER TABLE model_results ADD COLUMN prompt_tokens INTEGER DEFAULT 0")
        if "completion_tokens" not in mr_cols:
            conn.execute("ALTER TABLE model_results ADD COLUMN completion_tokens INTEGER DEFAULT 0")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_balances (
                user_id INTEGER PRIMARY KEY,
                points REAL NOT NULL DEFAULT 0,
                total_recharged REAL NOT NULL DEFAULT 0,
                total_consumed REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS model_pricing (
                model_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                input_points_per_1k REAL NOT NULL DEFAULT 1.0,
                output_points_per_1k REAL NOT NULL DEFAULT 2.0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS token_usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                request_id TEXT NOT NULL,
                model TEXT NOT NULL,
                round_number INTEGER NOT NULL DEFAULT 1,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                points_consumed REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_token_usage_user ON token_usage_logs(user_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS recharge_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                admin_id INTEGER NOT NULL,
                points REAL NOT NULL,
                remark TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(admin_id) REFERENCES users(id)
            );
            """
        )

    def _migrate_v5_to_v6_sync(self, conn: sqlite3.Connection) -> None:
        """添加管理员操作审计日志表。"""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_user_id INTEGER,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(admin_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_admin ON admin_audit_logs(admin_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created ON admin_audit_logs(created_at DESC);
            """
        )
        conn.execute("UPDATE users SET role = 'admin' WHERE username = 'admin' AND role = 'user'")
        conn.commit()

    def _migrate_v6_to_v7_sync(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS request_summaries (
                request_id TEXT PRIMARY KEY,
                canvas_id TEXT,
                summary_model TEXT NOT NULL DEFAULT '',
                summary_markdown TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(request_id) REFERENCES chat_requests(request_id) ON DELETE CASCADE
            );
            """
        )

    def _migrate_v7_to_v8_sync(self, conn: sqlite3.Connection) -> None:
        """v8: persist context_node_ids (chat_requests) + per-model / conclusion
        positions (cluster_positions) so multi-select placements, custom node
        rearrangements, and context-continuation edges survive page reload."""
        self._ensure_chat_requests_schema_sync(conn)
        self._ensure_cluster_positions_schema_sync(conn)

    # ── billing / admin sync helpers ──

    def _get_user_role_sync(self, user_id: int) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or not row[0]:
            return "user"
        return str(row[0])

    def _set_user_role_sync(self, user_id: int, role: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, self._now(), user_id))
            conn.commit()

    def _get_user_balance_sync(self, user_id: int) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT points FROM user_balances WHERE user_id = ?", (user_id,)).fetchone()
        return float(row[0]) if row else 0.0

    def _ensure_user_balance_sync(self, user_id: int) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_balances(user_id, points, total_recharged, total_consumed, updated_at) VALUES (?, 0, 0, 0, ?)",
                (user_id, now),
            )
            conn.commit()

    def _add_points_sync(self, user_id: int, points: float, admin_id: int, remark: str) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_balances(user_id, points, total_recharged, total_consumed, updated_at) VALUES (?, 0, 0, 0, ?)",
                (user_id, now),
            )
            conn.execute(
                "UPDATE user_balances SET points = points + ?, total_recharged = total_recharged + ?, total_consumed = total_consumed + ?, updated_at = ? WHERE user_id = ?",
                (points, max(0, points), max(0, -points), now, user_id),
            )
            conn.execute(
                "INSERT INTO recharge_logs(user_id, admin_id, points, remark, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, admin_id, points, remark, now),
            )
            conn.commit()

    def _add_points_non_negative_sync(self, user_id: int, points: float, admin_id: int, remark: str) -> bool:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_balances(user_id, points, total_recharged, total_consumed, updated_at) VALUES (?, 0, 0, 0, ?)",
                (user_id, now),
            )
            if points < 0:
                cursor = conn.execute(
                    "UPDATE user_balances SET points = points + ?, total_recharged = total_recharged + ?, total_consumed = total_consumed + ?, updated_at = ? WHERE user_id = ? AND points + ? >= 0",
                    (points, max(0, points), max(0, -points), now, user_id, points),
                )
                if cursor.rowcount == 0:
                    conn.rollback()
                    return False
            else:
                conn.execute(
                    "UPDATE user_balances SET points = points + ?, total_recharged = total_recharged + ?, total_consumed = total_consumed + ?, updated_at = ? WHERE user_id = ?",
                    (points, max(0, points), max(0, -points), now, user_id),
                )
            conn.execute(
                "INSERT INTO recharge_logs(user_id, admin_id, points, remark, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, admin_id, points, remark, now),
            )
            conn.commit()
        return True

    def _deduct_points_sync(self, user_id: int, points: float) -> bool:
        """原子扣减余额。余额不足时不扣减，返回 False。"""
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_balances(user_id, points, total_recharged, total_consumed, updated_at) VALUES (?, 0, 0, 0, ?)",
                (user_id, now),
            )
            cursor = conn.execute(
                "UPDATE user_balances SET points = points - ?, total_consumed = total_consumed + ?, updated_at = ? WHERE user_id = ? AND points >= ?",
                (points, points, now, user_id, points),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                return False
            conn.commit()
        return True

    def _record_token_usage_sync(
        self, user_id: int, request_id: str, model: str, round_number: int,
        prompt_tokens: int, completion_tokens: int, total_tokens: int, points_consumed: float,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO token_usage_logs(user_id, request_id, model, round_number, prompt_tokens, completion_tokens, total_tokens, points_consumed, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, request_id, model, round_number, prompt_tokens, completion_tokens, total_tokens, points_consumed, self._now()),
            )
            conn.commit()

    def _get_all_pricing_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT model_id, display_name, input_points_per_1k, output_points_per_1k, is_active FROM model_pricing ORDER BY model_id").fetchall()
        return [{"model_id": r[0], "display_name": r[1], "input_points_per_1k": r[2], "output_points_per_1k": r[3], "is_active": r[4]} for r in rows]

    def _upsert_pricing_sync(self, model_id: str, display_name: str, input_per_1k: float, output_per_1k: float, is_active: int) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO model_pricing(model_id, display_name, input_points_per_1k, output_points_per_1k, is_active, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT(model_id) DO UPDATE SET
                   display_name=excluded.display_name, input_points_per_1k=excluded.input_points_per_1k,
                   output_points_per_1k=excluded.output_points_per_1k, is_active=excluded.is_active, updated_at=excluded.updated_at""",
                (model_id, display_name, input_per_1k, output_per_1k, is_active, now, now),
            )
            conn.commit()

    def _delete_pricing_sync(self, model_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM model_pricing WHERE model_id = ?", (model_id,))
            conn.commit()

    def _get_pricing_for_model_sync(self, model_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT model_id, display_name, input_points_per_1k, output_points_per_1k, is_active FROM model_pricing WHERE model_id = ?", (model_id,)).fetchone()
        if not row:
            return None
        return {"model_id": row[0], "display_name": row[1], "input_points_per_1k": row[2], "output_points_per_1k": row[3], "is_active": row[4]}

    def _list_users_admin_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT u.id, u.username, u.role, u.created_at,
                          COALESCE(b.points, 0), COALESCE(b.total_recharged, 0), COALESCE(b.total_consumed, 0)
                   FROM users u LEFT JOIN user_balances b ON b.user_id = u.id ORDER BY u.id""",
            ).fetchall()
        return [{"id": r[0], "username": r[1], "role": r[2] if r[2] else "user", "created_at": r[3],
                 "balance": r[4], "total_recharged": r[5], "total_consumed": r[6]} for r in rows]

    def _get_usage_stats_sync(self, user_id: int | None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if user_id:
                rows = conn.execute(
                    "SELECT model, SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens), SUM(points_consumed), COUNT(*) FROM token_usage_logs WHERE user_id = ? GROUP BY model",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT model, SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens), SUM(points_consumed), COUNT(*) FROM token_usage_logs GROUP BY model",
                ).fetchall()
        return [{"model": r[0], "prompt_tokens": r[1], "completion_tokens": r[2], "total_tokens": r[3], "points_consumed": r[4], "count": r[5]} for r in rows]

    def _get_user_usage_detail_sync(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT t.model, t.prompt_tokens, t.completion_tokens, t.total_tokens,
                          t.points_consumed, t.round_number, t.request_id, t.created_at,
                          mr.duration_ms
                   FROM token_usage_logs t
                   LEFT JOIN model_results mr ON mr.request_id = t.request_id
                       AND mr.model = t.model AND mr.round_number = t.round_number
                   WHERE t.user_id = ?
                   ORDER BY t.created_at DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return [
            {
                "model": r[0], "prompt_tokens": r[1], "completion_tokens": r[2],
                "total_tokens": r[3], "points_consumed": r[4], "round_number": r[5],
                "request_id": r[6], "created_at": r[7],
                "duration_ms": r[8],
            }
            for r in rows
        ]

    def _get_user_usage_summary_sync(self, user_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            today_row = conn.execute(
                "SELECT COALESCE(SUM(points_consumed), 0), COUNT(*) FROM token_usage_logs WHERE user_id = ? AND created_at >= date('now', 'start of day')",
                (user_id,),
            ).fetchone()
            week_row = conn.execute(
                "SELECT COALESCE(SUM(points_consumed), 0), COUNT(*) FROM token_usage_logs WHERE user_id = ? AND created_at >= date('now', '-7 days')",
                (user_id,),
            ).fetchone()
            total_row = conn.execute(
                "SELECT COALESCE(SUM(points_consumed), 0), COUNT(*) FROM token_usage_logs WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            balance_row = conn.execute(
                "SELECT COALESCE(points, 0) FROM user_balances WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return {
            "today_points": round(today_row[0], 4) if today_row else 0,
            "today_count": today_row[1] if today_row else 0,
            "week_points": round(week_row[0], 4) if week_row else 0,
            "week_count": week_row[1] if week_row else 0,
            "total_points": round(total_row[0], 4) if total_row else 0,
            "total_count": total_row[1] if total_row else 0,
            "balance": round(balance_row[0], 2) if balance_row else 0,
        }

    def _set_user_custom_keys_sync(self, user_id: int, model_keys: dict[str, str], use_custom_key: bool) -> None:
        now = self._now()
        keys_json = json.dumps(model_keys, ensure_ascii=False)
        use_flag = 1 if use_custom_key else 0
        with self._connect() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(user_settings)").fetchall()}
            if "use_custom_key" not in cols:
                conn.execute("ALTER TABLE user_settings ADD COLUMN use_custom_key INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                """INSERT INTO user_settings(user_id, api_base_url, api_format, api_key, models_json,
                       firecrawl_api_key, firecrawl_country, firecrawl_timeout_ms, use_custom_key, created_at, updated_at)
                   VALUES (?, '', 'openai', ?, '[]', '', 'CN', 45000, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       api_key=excluded.api_key,
                       use_custom_key=excluded.use_custom_key,
                       updated_at=excluded.updated_at""",
                (user_id, keys_json, use_flag, now, now),
            )
            conn.commit()

    def _get_recharge_logs_sync(self, user_id: int | None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if user_id:
                rows = conn.execute(
                    "SELECT r.id, r.user_id, u.username, r.admin_id, a.username, r.points, r.remark, r.created_at FROM recharge_logs r JOIN users u ON u.id=r.user_id JOIN users a ON a.id=r.admin_id WHERE r.user_id=? ORDER BY r.created_at DESC LIMIT 200",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT r.id, r.user_id, u.username, r.admin_id, a.username, r.points, r.remark, r.created_at FROM recharge_logs r JOIN users u ON u.id=r.user_id JOIN users a ON a.id=r.admin_id ORDER BY r.created_at DESC LIMIT 200",
                ).fetchall()
        return [{"id": r[0], "user_id": r[1], "username": r[2], "admin_id": r[3], "admin_name": r[4], "points": r[5], "remark": r[6], "created_at": r[7]} for r in rows]

    def _get_system_config_sync(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_meta WHERE key LIKE 'config_%'").fetchall()
        return {r[0]: r[1] for r in rows}

    def _upsert_request_summary_sync(
        self,
        request_id: str,
        canvas_id: str | None,
        summary_model: str,
        summary_markdown: str,
        status: str,
        error_message: str | None,
    ) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO request_summaries(request_id, canvas_id, summary_model, summary_markdown, status, error_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    summary_model=excluded.summary_model,
                    summary_markdown=excluded.summary_markdown,
                    status=excluded.status,
                    error_message=excluded.error_message,
                    updated_at=excluded.updated_at
                """,
                (request_id, canvas_id, summary_model, summary_markdown, status, error_message, now, now),
            )
            conn.commit()

    def _get_request_summary_sync(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_id, canvas_id, summary_model, summary_markdown, status, error_message, created_at, updated_at FROM request_summaries WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "request_id": row[0], "canvas_id": row[1], "summary_model": row[2],
            "summary_markdown": row[3], "status": row[4], "error_message": row[5],
            "created_at": row[6], "updated_at": row[7],
        }

    def _get_summaries_for_canvas_sync(self, canvas_id: str) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT rs.request_id, rs.summary_model, rs.summary_markdown, rs.status, rs.error_message
                FROM request_summaries rs
                JOIN chat_requests cr ON cr.request_id = rs.request_id
                WHERE cr.canvas_id = ? AND rs.status = 'success'
                """,
                (canvas_id,),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for r in rows:
            result[r[0]] = {
                "request_id": r[0], "summary_model": r[1],
                "summary_markdown": r[2], "status": r[3], "error_message": r[4],
            }
        return result

    def _get_global_model_config_sync(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_meta WHERE key LIKE 'model_config_%'").fetchall()
        return {r[0]: r[1] for r in rows}

    def _set_global_model_config_sync(
        self,
        api_base_url: str,
        api_format: str,
        api_key: str,
        models_json: str,
        firecrawl_api_key: str,
        firecrawl_country: str,
        firecrawl_timeout_ms: int,
        preprocess_model: str = "",
        user_api_base_url: str = "",
        user_api_format: str = "openai",
        extra_params: dict | None = None,
        extra_headers: dict | None = None,
    ) -> None:
        now = self._now()
        fields = {
            "model_config_api_base_url": api_base_url,
            "model_config_api_format": api_format,
            "model_config_api_key": api_key,
            "model_config_models_json": models_json,
            "model_config_firecrawl_api_key": firecrawl_api_key,
            "model_config_firecrawl_country": firecrawl_country,
            "model_config_firecrawl_timeout_ms": str(firecrawl_timeout_ms),
            "model_config_preprocess_model": preprocess_model,
            "model_config_user_api_base_url": user_api_base_url,
            "model_config_user_api_format": user_api_format,
            "model_config_extra_params": json.dumps(extra_params or {}, ensure_ascii=False),
            "model_config_extra_headers": json.dumps(extra_headers or {}, ensure_ascii=False),
        }
        with self._connect() as conn:
            for k, v in fields.items():
                conn.execute(
                    "INSERT INTO app_meta(key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (k, v, now),
                )
            conn.commit()

    def _set_system_config_sync(self, key: str, value: str) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_meta(key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now),
            )
            conn.commit()

    def _add_admin_audit_log_sync(
        self,
        admin_id: int,
        action: str,
        target_user_id: int | None,
        detail: dict[str, Any],
    ) -> None:
        import json as _json_mod
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO admin_audit_logs(admin_id, action, target_user_id, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (admin_id, action, target_user_id, _json_mod.dumps(detail, ensure_ascii=False), self._now()),
            )
            conn.commit()

    def _get_admin_audit_logs_sync(
        self, limit: int, offset: int, action_filter: str | None
    ) -> list[dict[str, Any]]:
        import json as _json_mod
        with self._connect() as conn:
            if action_filter:
                rows = conn.execute(
                    """SELECT l.id, l.admin_id, u.username, l.action, l.target_user_id,
                              tu.username, l.detail_json, l.created_at
                       FROM admin_audit_logs l
                       JOIN users u ON u.id = l.admin_id
                       LEFT JOIN users tu ON tu.id = l.target_user_id
                       WHERE l.action = ?
                       ORDER BY l.created_at DESC LIMIT ? OFFSET ?""",
                    (action_filter, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT l.id, l.admin_id, u.username, l.action, l.target_user_id,
                              tu.username, l.detail_json, l.created_at
                       FROM admin_audit_logs l
                       JOIN users u ON u.id = l.admin_id
                       LEFT JOIN users tu ON tu.id = l.target_user_id
                       ORDER BY l.created_at DESC LIMIT ? OFFSET ?""",
                    (limit, offset),
                ).fetchall()
        result = []
        for r in rows:
            try:
                detail = _json_mod.loads(r[6]) if r[6] else {}
            except Exception:
                detail = {}
            result.append({
                "id": r[0],
                "admin_id": r[1],
                "admin_name": r[2],
                "action": r[3],
                "target_user_id": r[4],
                "target_username": r[5],
                "detail": detail,
                "created_at": r[7],
            })
        return result

    def _cleanup_old_events_sync(self, retention_days: int) -> int:
        cutoff = self._now_offset(-retention_days)
        with self._connect() as conn:
            c1 = conn.execute("DELETE FROM request_events WHERE created_at < ?", (cutoff,))
            c2 = conn.execute("DELETE FROM admin_audit_logs WHERE created_at < ?", (cutoff,))
            conn.commit()
        total = (c1.rowcount or 0) + (c2.rowcount or 0)
        if total:
            logger.info("DB cleanup: removed %d old event row(s) (retention=%dd)", total, retention_days)
        return total

    def _cleanup_old_failure_logs_sync(self) -> int:
        cutoff = self._now_offset(-1)
        with self._connect() as conn:
            c = conn.execute(
                "DELETE FROM request_events WHERE event_type IN ('model_retry', 'search_error') AND created_at < ?",
                (cutoff,),
            )
            conn.commit()
        total = c.rowcount or 0
        if total:
            logger.info("DB cleanup: removed %d failure/retry log(s) older than 24h", total)
        return total

    def _cleanup_old_token_usage_sync(self, retention_days: int) -> int:
        cutoff = self._now_offset(-retention_days)
        with self._connect() as conn:
            c = conn.execute("DELETE FROM token_usage_logs WHERE created_at < ?", (cutoff,))
            conn.commit()
        total = c.rowcount or 0
        if total:
            logger.info("DB cleanup: removed %d old token_usage_log row(s) (retention=%dd)", total, retention_days)
        return total

    @staticmethod
    def _now_offset(days: int) -> str:
        """返回 now + days 天的 ISO 字符串（days 为负时表示过去）。"""
        from datetime import datetime, timedelta, timezone
        return (datetime.now(tz=timezone.utc).astimezone() + timedelta(days=days)).isoformat()

    def _get_user_settings_sync(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(user_settings)").fetchall()}
            has_use_custom = "use_custom_key" in cols
            if has_use_custom:
                row = conn.execute(
                    """SELECT api_base_url, api_format, api_key, models_json,
                              firecrawl_api_key, firecrawl_country, firecrawl_timeout_ms, use_custom_key
                       FROM user_settings WHERE user_id = ?""",
                    (user_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT api_base_url, api_format, api_key, models_json,
                              firecrawl_api_key, firecrawl_country, firecrawl_timeout_ms
                       FROM user_settings WHERE user_id = ?""",
                    (user_id,),
                ).fetchone()
        if not row:
            return None
        return {
            "api_base_url": row[0],
            "api_format": row[1],
            "api_key": row[2],
            "models": json.loads(row[3]) if row[3] else [],
            "firecrawl_api_key": row[4],
            "firecrawl_country": row[5],
            "firecrawl_timeout_ms": row[6],
            "use_custom_key": bool(row[7]) if has_use_custom and len(row) > 7 else False,
        }

    def _upsert_user_settings_sync(
        self,
        user_id: int,
        api_base_url: str,
        api_format: str,
        api_key: str,
        models_json: str,
        firecrawl_api_key: str,
        firecrawl_country: str,
        firecrawl_timeout_ms: int,
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
                       api_base_url=excluded.api_base_url,
                       api_format=excluded.api_format,
                       api_key=excluded.api_key,
                       models_json=excluded.models_json,
                       firecrawl_api_key=excluded.firecrawl_api_key,
                       firecrawl_country=excluded.firecrawl_country,
                       firecrawl_timeout_ms=excluded.firecrawl_timeout_ms,
                       updated_at=excluded.updated_at""",
                (
                    user_id, api_base_url, api_format, api_key, models_json,
                    firecrawl_api_key, firecrawl_country, firecrawl_timeout_ms,
                    now, now,
                ),
            )
            conn.commit()

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
                       cp.user_x, cp.user_y, cp.model_y,
                       cr.context_node_ids,
                       cp.model_positions_json, cp.conclusion_x, cp.conclusion_y
                FROM chat_requests cr
                LEFT JOIN cluster_positions cp ON cp.request_id = cr.request_id
                WHERE cr.canvas_id = ? AND cr.user_id = ?
                ORDER BY cr.created_at ASC
                """,
                (canvas_id, user_id),
            ).fetchall()
            requests = []
            if request_rows:
                request_ids = [row[0] for row in request_rows]
                placeholders = ",".join("?" * len(request_ids))
                result_rows_all = conn.execute(
                    f"""
                    SELECT request_id, model, round_number, status, content, error_text
                    FROM model_results WHERE request_id IN ({placeholders})
                    ORDER BY request_id ASC, round_number ASC, model ASC
                    """,
                    request_ids,
                ).fetchall()
                results_by_request: dict[str, list[dict[str, Any]]] = {rid: [] for rid in request_ids}
                for rr in result_rows_all:
                    results_by_request[rr[0]].append(
                        {"model": rr[1], "round": rr[2], "status": rr[3], "content": rr[4], "error_text": rr[5]}
                    )
            else:
                results_by_request = {}

            for row in request_rows:
                request_id = row[0]
                results = results_by_request.get(request_id, [])
                position = None
                if row[11] is not None:
                    position = {
                        "user_x": row[11],
                        "user_y": row[12],
                        "model_y": row[13],
                        "model_positions": _safe_json_dict(row[15]),
                        "conclusion_x": row[16],  # may be None
                        "conclusion_y": row[17],  # may be None
                    }
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
                    "context_node_ids": _safe_json_list(row[14]),
                    "results": results,
                })
            summaries_map: dict[str, dict[str, Any]] = {}
            if request_rows:
                summary_rows = conn.execute(
                    f"SELECT request_id, summary_model, summary_markdown, status FROM request_summaries WHERE request_id IN ({placeholders}) AND status = 'success'",
                    request_ids,
                ).fetchall()
                for sr in summary_rows:
                    summaries_map[sr[0]] = {"summary_model": sr[1], "summary_markdown": sr[2], "status": sr[3]}

            for req in requests:
                req["summary"] = summaries_map.get(req["request_id"])

        return {"canvas_id": canvas_row[0], "name": canvas_row[1], "created_at": canvas_row[2], "requests": requests}

    def _upsert_cluster_position_sync(
        self,
        request_id: str,
        user_id: int,
        user_x: float,
        user_y: float,
        model_y: float,
        model_positions_json: str = "{}",
        conclusion_x: float | None = None,
        conclusion_y: float | None = None,
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
                INSERT INTO cluster_positions(
                    request_id, user_x, user_y, model_y, updated_at,
                    model_positions_json, conclusion_x, conclusion_y
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    user_x=excluded.user_x, user_y=excluded.user_y,
                    model_y=excluded.model_y, updated_at=excluded.updated_at,
                    model_positions_json=excluded.model_positions_json,
                    conclusion_x=excluded.conclusion_x,
                    conclusion_y=excluded.conclusion_y
                """,
                (
                    request_id,
                    user_x,
                    user_y,
                    model_y,
                    now,
                    model_positions_json,
                    conclusion_x,
                    conclusion_y,
                ),
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

    def _get_request_events_sync(self, request_id: str, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            owner = conn.execute(
                "SELECT 1 FROM chat_requests WHERE request_id = ? AND user_id = ?",
                (request_id, user_id),
            ).fetchone()
            if not owner:
                return []
            rows = conn.execute(
                "SELECT event_type, payload_json, created_at FROM request_events WHERE request_id = ? ORDER BY created_at ASC",
                (request_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for event_type, payload_json, created_at in rows:
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except Exception:
                payload = {}
            result.append({"event_type": event_type, "payload": payload, "created_at": created_at})
        return result

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
        # 注：使用 threading.local 实现线程本地连接复用（asyncio.to_thread 每次可能用不同线程）。
        # 连接数受线程池大小约束（默认 min(32, cpu+4)），在进程存活期间不主动关闭（文件句柄占用）。
        # 若需限制连接数，可改用连接池（如 aiosqlite），但会引入额外依赖和复杂度。
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        self._local.conn = conn
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat()


def init_database_sync(db_path: Path | None = None) -> Path:
    return LocalDatabase(db_path=db_path).initialize_sync()
