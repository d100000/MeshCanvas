"""Model / analysis request / response models."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class SelectionSummaryRequest(BaseModel):
    bundle: str = Field(..., min_length=1)
    count: int = Field(default=1, ge=1, le=200)


class ConversationMessage(BaseModel):
    role: str
    content: str


class ConversationAnalysisRequest(BaseModel):
    request_id: str = ""
    messages: List[ConversationMessage] = Field(default_factory=list)
