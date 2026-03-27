"""统一事件总线。

所有业务事件通过 EventBus.emit() 写入：
  1. JSONL 文件（logs/requests-YYYY-MM-DD.jsonl）——保留历史，按日分文件
  2. 标准 logging（WARNING 及以上立即可见）——按 level 调用对应方法
  3. 可选：数据库 request_events（通过 db_callback 注入，避免循环依赖）

日志保留策略：
  - LOG_RETENTION_DAYS 环境变量（默认 30 天）控制 JSONL 文件自动清理
  - 数据库清理由 LocalDatabase.cleanup_old_events() 完成
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# 旧名保持兼容，模块内统一用 EventBus
RequestLogger = None  # 在底部赋值


class EventBus:
    """统一事件总线。

    用法：
        bus = EventBus()
        await bus.emit("login_failed", level="warning",
                       user_id=None, client_host="1.2.3.4",
                       data={"username": "admin", "reason": "badcreds"})
    """

    def __init__(
        self,
        log_dir: Path | None = None,
        db_callback: Callable[[str, dict[str, Any], str | None, str | None], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        configured_dir = os.getenv("REQUEST_LOG_DIR")
        base_dir = Path(configured_dir) if configured_dir else (Path.cwd() / "logs")
        self.log_dir = (log_dir or base_dir).resolve()
        self._lock = asyncio.Lock()
        # db_callback(event_type, payload, request_id, client_id) -> None
        self._db_callback = db_callback
        self._retention_days: int = max(1, int(os.getenv("LOG_RETENTION_DAYS", "30")))

    def set_db_callback(
        self,
        cb: Callable[[str, dict[str, Any], str | None, str | None], Coroutine[Any, Any, None]],
    ) -> None:
        self._db_callback = cb

    # ── 主入口 ──────────────────────────────────────────────────────────────

    async def emit(
        self,
        event_type: str,
        *,
        level: str = "info",
        request_id: str | None = None,
        client_id: str | None = None,
        user_id: int | None = None,
        data: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """发出一条事件。

        event_type: 事件名称，如 "login_failed", "chat_request", "admin_action"
        level: "debug" | "info" | "warning" | "error"
        data: 事件专属数据字典
        extra: 便捷键值（会合并到 data）
        """
        payload: dict[str, Any] = {"event": event_type}
        if request_id:
            payload["request_id"] = request_id
        if client_id:
            payload["client_id"] = client_id
        if user_id is not None:
            payload["user_id"] = user_id
        merged = {**(data or {}), **extra}
        if merged:
            payload.update(merged)

        await self._write_jsonl(event_type=event_type, payload=payload)
        self._write_logging(level=level, event_type=event_type, payload=payload)
        if self._db_callback is not None:
            try:
                await self._db_callback(
                    event_type=event_type,
                    payload=payload,
                    request_id=request_id,
                    client_id=client_id,
                )
            except Exception:
                logger.warning("EventBus db_callback failed for %s", event_type, exc_info=True)

    # ── 向后兼容旧的 log_event 接口 ────────────────────────────────────────

    async def log_event(self, payload: dict[str, Any]) -> None:
        """向后兼容旧调用。"""
        event_type = str(payload.get("type") or payload.get("event") or "unknown")
        level = str(payload.get("level", "info"))
        request_id = str(payload["request_id"]) if "request_id" in payload else None
        client_id = str(payload["client_id"]) if "client_id" in payload else None
        user_id_raw = payload.get("user_id")
        user_id = int(user_id_raw) if user_id_raw is not None else None
        await self.emit(
            event_type,
            level=level,
            request_id=request_id,
            client_id=client_id,
            user_id=user_id,
            data={k: v for k, v in payload.items() if k not in ("type", "event", "level", "request_id", "client_id", "user_id")},
        )

    # ── JSONL 写入 ──────────────────────────────────────────────────────────

    async def _write_jsonl(self, *, event_type: str, payload: dict[str, Any]) -> None:
        now = datetime.now(tz=timezone.utc).astimezone()
        record = {"ts": now.isoformat(), **payload}
        path = self._log_path(now)

        def normalize(value: Any) -> Any:
            if is_dataclass(value):
                return asdict(value)
            return str(value)

        line = json.dumps(record, ensure_ascii=False, default=normalize) + "\n"

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(line)

        try:
            async with self._lock:
                await asyncio.to_thread(write)
        except Exception:
            logger.warning(
                "EventBus JSONL write failed (event=%s path=%s)", event_type, path, exc_info=True
            )

    # ── 标准 logging 写入 ───────────────────────────────────────────────────

    @staticmethod
    def _write_logging(*, level: str, event_type: str, payload: dict[str, Any]) -> None:
        msg = "event=%s %s"
        detail = " ".join(f"{k}={v!r}" for k, v in payload.items() if k != "event")
        lvl = level.lower()
        if lvl == "debug":
            logger.debug(msg, event_type, detail)
        elif lvl == "warning":
            logger.warning(msg, event_type, detail)
        elif lvl == "error":
            logger.error(msg, event_type, detail)
        # info 以下默认不打到 uvicorn 控制台（uvicorn 默认 INFO 级别）

    # ── 路径与清理 ──────────────────────────────────────────────────────────

    def _log_path(self, now: datetime) -> Path:
        day = now.strftime("%Y-%m-%d")
        return self.log_dir / f"requests-{day}.jsonl"

    def cleanup_old_jsonl(self) -> int:
        """删除超过 retention_days 的 JSONL 文件，返回删除数量。同步方法，由启动时调用。"""
        if not self.log_dir.exists():
            return 0
        cutoff = datetime.now(tz=timezone.utc).astimezone() - timedelta(days=self._retention_days)
        deleted = 0
        for f in self.log_dir.glob("requests-*.jsonl"):
            try:
                day_str = f.stem[len("requests-"):]  # "YYYY-MM-DD"
                file_date = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).astimezone()
                if file_date < cutoff:
                    f.unlink(missing_ok=True)
                    deleted += 1
            except (ValueError, OSError):
                pass
        if deleted:
            logger.info("EventBus: cleaned up %d old JSONL log file(s)", deleted)
        return deleted


# 向后兼容别名
RequestLogger = EventBus
