"""Canvas-related request / response models."""

from __future__ import annotations

from typing import Dict, Optional

from pydantic import BaseModel, Field


class CreateCanvasRequest(BaseModel):
    name: str = "新画布"


class RenameCanvasRequest(BaseModel):
    name: str = Field(..., min_length=1)


class ClusterPositionRequest(BaseModel):
    user_x: float = 0
    user_y: float = 0
    model_y: float = 0
    # v8: per-model absolute positions (modelName → {x, y}) and optional
    # conclusion node position. Keep all backward-compatible defaults.
    # Using typing.Dict/Optional for Python 3.9 compatibility (pydantic v2
    # evaluates type hints at model-construction time even under
    # `from __future__ import annotations`).
    model_positions: Dict[str, Dict[str, float]] = Field(default_factory=dict)
    conclusion_x: Optional[float] = None
    conclusion_y: Optional[float] = None
