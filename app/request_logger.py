from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class RequestLogger:
    def __init__(self, log_dir: Path | None = None) -> None:
        configured_dir = os.getenv("REQUEST_LOG_DIR")
        base_dir = Path(configured_dir) if configured_dir else (Path.cwd() / "logs")
        self.log_dir = (log_dir or base_dir).resolve()
        self._lock = asyncio.Lock()

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
            return

    def _log_path(self, now: datetime) -> Path:
        day = now.strftime("%Y-%m-%d")
        return self.log_dir / f"requests-{day}.jsonl"
