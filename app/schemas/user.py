"""User-related request / response models."""

from __future__ import annotations

from typing import Dict

from pydantic import BaseModel


class CustomApiKeyRequest(BaseModel):
    model_keys: Dict[str, str] = {}
    use_custom_key: bool = False
