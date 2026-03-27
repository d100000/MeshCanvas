from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default: keep logs for 30 days; override with LOG_RETENTION_DAYS env var
DEFAULT_RETENTION_DAYS = 30


class RequestLogger:
    def __init__(self, log_dir: Path | None = None, retention_days: int | None = None) -> None:
        configured_dir = os.getenv("REQUEST_LOG_DIR")
        base_dir = Path(configured_dir) if configured_dir else (Path.cwd() / "logs")
        self.log_dir = (log_dir or base_dir).resolve()
        self._lock = asyncio.Lock()
        try:
            env_days = int(os.getenv("LOG_RETENTION_DAYS", ""))
        except (ValueError, TypeError):
            env_days = None
        self.retention_days = retention_days or env_days or DEFAULT_RETENTION_DAYS

    async def log_event(self, payload: dict[str, Any]) -> None:
        now = datetime.now().astimezone()
        record = {"ts": now.isoformat(), **payload}
        path = self._log_path(now)

        def normalize(value: Any) -> Any:
            if is_dataclass(value):
                return asdict(value)
            return value

        line = json.dumps(record, ensure_ascii=False, default=normalize) + "\n"

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(line)

        try:
            async with self._lock:
                await asyncio.to_thread(write)
        except Exception:
            logger.warning("failed to write request log to %s", path, exc_info=True)

    async def cleanup_old_logs(self) -> int:
        """Delete JSONL log files older than retention_days. Returns count of deleted files."""
        def _cleanup() -> int:
            if not self.log_dir.exists():
                return 0
            cutoff = datetime.now().astimezone() - timedelta(days=self.retention_days)
            cutoff_str = cutoff.strftime("%Y-%m-%d")
            deleted = 0
            for path in sorted(self.log_dir.glob("requests-*.jsonl")):
                stem = path.stem  # requests-YYYY-MM-DD
                date_part = stem.replace("requests-", "", 1)
                if date_part < cutoff_str:
                    try:
                        path.unlink()
                        deleted += 1
                    except OSError as exc:
                        logger.warning("failed to delete old log %s: %s", path, exc)
            return deleted

        try:
            count = await asyncio.to_thread(_cleanup)
            if count > 0:
                logger.info("cleaned up %d old log file(s) older than %d days", count, self.retention_days)
            return count
        except Exception:
            logger.warning("log cleanup failed", exc_info=True)
            return 0

    def _log_path(self, now: datetime) -> Path:
        day = now.strftime("%Y-%m-%d")
        return self.log_dir / f"requests-{day}.jsonl"
