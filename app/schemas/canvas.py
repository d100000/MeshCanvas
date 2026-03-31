"""Canvas-related request / response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateCanvasRequest(BaseModel):
    name: str = "新画布"


class RenameCanvasRequest(BaseModel):
    name: str = Field(..., min_length=1)


class ClusterPositionRequest(BaseModel):
    user_x: float = 0
    user_y: float = 0
    model_y: float = 0
