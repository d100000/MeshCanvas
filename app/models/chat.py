"""Chat-related data models."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.search import SearchBundle


@dataclass
class ThreadState:
    request_id: str
    models: list[str]
    histories: dict[str, list[dict[str, str]]]
    user_message: str
    discussion_rounds: int
    search_enabled: bool
    think_enabled: bool
    search_bundle: SearchBundle | None = None
    parent_request_id: str | None = None
    source_model: str | None = None
    source_round: int | None = None
    canvas_id: str | None = None
    meta: dict[str, str] = field(default_factory=dict)
