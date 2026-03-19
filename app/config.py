from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


class ModelSettings(BaseModel):
    models: list[str] = Field(default_factory=list)
    api_key: str = Field(alias="API_key")
    base_url: str


@lru_cache(maxsize=1)
def get_settings() -> ModelSettings:
    config_path = Path(os.getenv("MODELS_SETTING_PATH", "models_setting.json")).resolve()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    return ModelSettings.model_validate(data)


@lru_cache(maxsize=1)
def get_firecrawl_api_key() -> str:
    return os.getenv("FIRECRAWL_API_KEY", "").strip()


@lru_cache(maxsize=1)
def get_firecrawl_country() -> str:
    return os.getenv("FIRECRAWL_COUNTRY", "CN").strip() or "CN"


@lru_cache(maxsize=1)
def get_database_path() -> Path:
    raw = os.getenv("LOCAL_DB_PATH", "data/app.db").strip() or "data/app.db"
    return Path(raw).resolve()


@lru_cache(maxsize=1)
def get_firecrawl_timeout_ms() -> int:
    raw = os.getenv("FIRECRAWL_TIMEOUT_MS", "45000").strip()
    try:
        return max(5_000, min(int(raw), 120_000))
    except ValueError:
        return 45_000
