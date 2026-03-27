"""Canvas and cluster position persistence."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from app.repositories.base import BaseRepository


class CanvasRepository(BaseRepository):
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
        self, request_id: str, user_id: int, user_x: float, user_y: float, model_y: float,
    ) -> bool:
        return await self._run_read(self._upsert_cluster_position_sync, False, request_id, user_id, user_x, user_y, model_y)

    async def clear_canvas_requests(self, canvas_id: str, user_id: int) -> None:
        await self._run_write(self._clear_canvas_requests_sync, canvas_id, user_id)

    # ---- sync ----

    def _create_canvas_sync(self, user_id: int, name: str) -> str:
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
            cursor = conn.execute("DELETE FROM canvases WHERE id = ? AND user_id = ?", (canvas_id, user_id))
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
                """SELECT cr.request_id, cr.user_message, cr.models_json, cr.discussion_rounds,
                          cr.search_enabled, cr.think_enabled, cr.parent_request_id,
                          cr.source_model, cr.source_round, cr.status, cr.created_at,
                          cp.user_x, cp.user_y, cp.model_y
                   FROM chat_requests cr
                   LEFT JOIN cluster_positions cp ON cp.request_id = cr.request_id
                   WHERE cr.canvas_id = ? AND cr.user_id = ?
                   ORDER BY cr.created_at ASC""",
                (canvas_id, user_id),
            ).fetchall()

            request_ids = [row[0] for row in request_rows]
            results_by_request: dict[str, list[dict[str, Any]]] = {rid: [] for rid in request_ids}
            if request_ids:
                placeholders = ",".join("?" * len(request_ids))
                all_results = conn.execute(
                    f"""SELECT request_id, model, round_number, status, content, error_text
                        FROM model_results WHERE request_id IN ({placeholders})
                        ORDER BY round_number ASC, model ASC""",
                    request_ids,
                ).fetchall()
                for r in all_results:
                    results_by_request[r[0]].append(
                        {"model": r[1], "round": r[2], "status": r[3], "content": r[4], "error_text": r[5]}
                    )

            requests = []
            for row in request_rows:
                position = (
                    {"user_x": row[11], "user_y": row[12], "model_y": row[13]}
                    if row[11] is not None else None
                )
                requests.append({
                    "request_id": row[0], "user_message": row[1], "models": json.loads(row[2]),
                    "discussion_rounds": row[3], "search_enabled": bool(row[4]), "think_enabled": bool(row[5]),
                    "parent_request_id": row[6], "source_model": row[7], "source_round": row[8],
                    "status": row[9], "created_at": row[10], "position": position,
                    "results": results_by_request.get(row[0], []),
                })
        return {"canvas_id": canvas_row[0], "name": canvas_row[1], "created_at": canvas_row[2], "requests": requests}

    def _upsert_cluster_position_sync(
        self, request_id: str, user_id: int, user_x: float, user_y: float, model_y: float,
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
                """INSERT INTO cluster_positions(request_id, user_x, user_y, model_y, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO UPDATE SET
                       user_x=excluded.user_x, user_y=excluded.user_y,
                       model_y=excluded.model_y, updated_at=excluded.updated_at""",
                (request_id, user_x, user_y, model_y, now),
            )
            conn.commit()
        return True

    def _clear_canvas_requests_sync(self, canvas_id: str, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM chat_requests WHERE canvas_id = ? AND user_id = ?", (canvas_id, user_id))
            conn.commit()
