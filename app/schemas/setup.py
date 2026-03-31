"""Initial setup request model."""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field, field_validator

from .admin import ModelItem


class SetupRequest(BaseModel):
    base_url: str = Field(..., min_length=1)
    api_format: str = "openai"
    API_key: str = Field(..., min_length=1)
    models: List[ModelItem] = Field(..., min_length=1)

    @field_validator("api_format")
    @classmethod
    def format_must_be_valid(cls, v: str) -> str:
        if v not in ("openai", "anthropic"):
            raise ValueError("api_format must be 'openai' or 'anthropic'")
        return v
