"""Chat request and model result persistence."""

from __future__ import annotations

import json
from typing import Any

from app.repositories.base import BaseRepository


class ChatRepository(BaseRepository):
    async def record_chat_request(
        self, *, request_id: str, client_id: str, models: list[str], user_message: str,
        discussion_rounds: int, search_enabled: bool, think_enabled: bool,
        parent_request_id: str | None = None, source_model: str | None = None,
        source_round: int | None = None, status: str = "queued",
        canvas_id: str | None = None, user_id: int | None = None,
    ) -> None:
        await self._run_write(
            self._record_chat_request_sync,
            request_id, client_id, models, user_message, discussion_rounds,
            search_enabled, think_enabled, parent_request_id, source_model,
            source_round, status, canvas_id, user_id,
        )

    async def mark_request_status(self, request_id: str, status: str) -> None:
        await self._run_write(self._mark_request_status_sync, request_id, status, suppress=True)

    async def record_model_result(
        self, *, request_id: str, model: str, round_number: int, status: str,
        content: str | None = None, error_text: str | None = None,
        duration_ms: float | None = None, response_length: int | None = None,
    ) -> None:
        await self._run_write(
            self._record_model_result_sync,
            request_id, model, round_number, status, content, error_text, duration_ms, response_length,
        )

    async def get_request_with_results(self, request_id: str, user_id: int) -> dict[str, Any] | None:
        return await self._run_read(self._get_request_with_results_sync, None, request_id, user_id)

    # ---- sync ----

    def _record_chat_request_sync(
        self, request_id, client_id, models, user_message, discussion_rounds,
        search_enabled, think_enabled, parent_request_id, source_model,
        source_round, status, canvas_id, user_id,
    ) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO chat_requests(
                       request_id, client_id, canvas_id, user_id, parent_request_id, source_model, source_round,
                       models_json, user_message, discussion_rounds, search_enabled, think_enabled, status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO UPDATE SET
                       client_id=excluded.client_id, canvas_id=excluded.canvas_id, user_id=excluded.user_id,
                       parent_request_id=excluded.parent_request_id, source_model=excluded.source_model,
                       source_round=excluded.source_round, models_json=excluded.models_json,
                       user_message=excluded.user_message, discussion_rounds=excluded.discussion_rounds,
                       search_enabled=excluded.search_enabled, think_enabled=excluded.think_enabled,
                       status=excluded.status, updated_at=excluded.updated_at""",
                (request_id, client_id, canvas_id, user_id, parent_request_id, source_model, source_round,
                 json.dumps(models, ensure_ascii=False), user_message, discussion_rounds,
                 int(search_enabled), int(think_enabled), status, now, now),
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
        self, request_id, model, round_number, status, content, error_text, duration_ms, response_length,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO model_results(
                       request_id, model, round_number, status, content, error_text, duration_ms, response_length, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, model, round_number, status, content, error_text, duration_ms, response_length, self._now()),
            )
            conn.commit()

    def _get_request_with_results_sync(self, request_id: str, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT request_id, user_message, models_json, discussion_rounds,
                          search_enabled, think_enabled, parent_request_id, source_model, source_round
                   FROM chat_requests WHERE request_id = ? AND user_id = ?""",
                (request_id, user_id),
            ).fetchone()
            if not row:
                return None
            result_rows = conn.execute(
                """SELECT model, round_number, status, content
                   FROM model_results WHERE request_id = ?
                   ORDER BY model ASC, round_number ASC""",
                (request_id,),
            ).fetchall()
        model_results: dict[str, list[dict[str, Any]]] = {}
        for r in result_rows:
            model_results.setdefault(r[0], []).append({"round": r[1], "status": r[2], "content": r[3] or ""})
        return {
            "request_id": row[0], "user_message": row[1], "models": json.loads(row[2]),
            "discussion_rounds": row[3], "search_enabled": bool(row[4]), "think_enabled": bool(row[5]),
            "parent_request_id": row[6], "source_model": row[7], "source_round": row[8],
            "model_results": model_results,
        }
