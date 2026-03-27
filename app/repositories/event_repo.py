"""Request event persistence."""

from __future__ import annotations

import json
from typing import Any

from app.repositories.base import BaseRepository


class EventRepository(BaseRepository):
    async def record_event(
        self, *, event_type: str, payload: dict[str, Any],
        request_id: str | None = None, client_id: str | None = None,
    ) -> None:
        await self._run_write(self._record_event_sync, event_type, payload, request_id, client_id, suppress=True)

    def _record_event_sync(self, event_type: str, payload: dict[str, Any], request_id: str | None, client_id: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO request_events(request_id, client_id, event_type, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (request_id, client_id, event_type, json.dumps(payload, ensure_ascii=False), self._now()),
            )
            conn.commit()
