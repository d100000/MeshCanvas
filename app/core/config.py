from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

load_dotenv()

logger = logging.getLogger(__name__)


class ModelInfo(BaseModel):
    name: str
    id: str


class ModelSettings(BaseModel):
    models: list[ModelInfo] = Field(default_factory=list)
    api_key: str = Field(alias="API_key")
    base_url: str
    api_format: str = "openai"
    firecrawl_api_key: str = ""
    firecrawl_country: str = "CN"
    firecrawl_timeout_ms: int = 45000

    @field_validator("models", mode="before")
    @classmethod
    def normalize_models(cls, v: object) -> list[dict[str, str]]:
        if not isinstance(v, list):
            return []
        result: list[dict[str, str]] = []
        for item in v:
            if isinstance(item, str):
                result.append({"name": item, "id": item})
            elif isinstance(item, dict):
                result.append(item)
            else:
                result.append(item)
        return result

    @field_validator("firecrawl_timeout_ms", mode="before")
    @classmethod
    def clamp_timeout(cls, v: object) -> int:
        try:
            return max(5_000, min(int(v), 120_000))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 45_000

    @property
    def model_names(self) -> list[str]:
        return [m.name for m in self.models]

    @property
    def model_id_map(self) -> dict[str, str]:
        return {m.name: m.id for m in self.models}


def get_config_path() -> Path:
    return Path(os.getenv("MODELS_SETTING_PATH", "models_setting.json")).resolve()


_is_configured_cache: bool | None = None


def is_configured() -> bool:
    global _is_configured_cache
    if _is_configured_cache is True:
        return True
    config_path = get_config_path()
    if not config_path.exists():
        _is_configured_cache = False
        return False
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        settings = ModelSettings.model_validate(data)
        result = bool(settings.models and settings.api_key and settings.base_url)
        _is_configured_cache = result
        return result
    except Exception:
        logger.warning("failed to validate config at %s", config_path, exc_info=True)
        _is_configured_cache = False
        return False


def save_settings(data: dict) -> Path:
    global _is_configured_cache
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _is_configured_cache = None
    get_settings.cache_clear()
    get_global_user_settings.cache_clear()
    return config_path


def clear_config() -> None:
    """Remove the config file and clear all caches (used by re-init)."""
    global _is_configured_cache
    config_path = get_config_path()
    if config_path.exists():
        config_path.unlink()
    _is_configured_cache = None
    get_settings.cache_clear()
    get_global_user_settings.cache_clear()


@lru_cache(maxsize=1)
def get_settings() -> ModelSettings:
    config_path = get_config_path()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    # Merge env-var overrides for firecrawl if not in JSON
    if "firecrawl_api_key" not in data:
        data["firecrawl_api_key"] = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if "firecrawl_country" not in data:
        data["firecrawl_country"] = os.getenv("FIRECRAWL_COUNTRY", "CN").strip() or "CN"
    if "firecrawl_timeout_ms" not in data:
        raw = os.getenv("FIRECRAWL_TIMEOUT_MS", "45000").strip()
        try:
            data["firecrawl_timeout_ms"] = max(5_000, min(int(raw), 120_000))
        except ValueError:
            data["firecrawl_timeout_ms"] = 45_000
    return ModelSettings.model_validate(data)


@lru_cache(maxsize=1)
def get_global_user_settings() -> dict[str, Any]:
    """Return the global config as a dict matching the old per-user settings format.

    This is the single source of truth for API / search config used by all users.
    """
    s = get_settings()
    return {
        "api_key": s.api_key,
        "api_base_url": s.base_url,
        "api_format": s.api_format,
        "models": [{"name": m.name, "id": m.id} for m in s.models],
        "firecrawl_api_key": s.firecrawl_api_key,
        "firecrawl_country": s.firecrawl_country,
        "firecrawl_timeout_ms": s.firecrawl_timeout_ms,
    }


@lru_cache(maxsize=1)
def get_database_path() -> Path:
    raw = os.getenv("LOCAL_DB_PATH", "data/app.db").strip() or "data/app.db"
    return Path(raw).resolve()
