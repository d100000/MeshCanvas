"""Base repository — SQLite connection management and async helpers."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BaseRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._local = threading.local()

    async def _run_write(self, fn, *args, suppress: bool = False) -> None:
        try:
            async with self._lock:
                await asyncio.to_thread(fn, *args)
        except Exception:
            logger.exception("database write error in %s", fn.__name__)
            if not suppress:
                raise

    async def _run_read(self, fn, default: Any, *args) -> Any:
        try:
            async with self._lock:
                return await asyncio.to_thread(fn, *args)
        except Exception:
            logger.exception("database read error in %s", fn.__name__)
            return default

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
