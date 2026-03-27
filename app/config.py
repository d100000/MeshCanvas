from __future__ import annotations

import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

load_dotenv()


class ModelInfo(BaseModel):
    name: str
    id: str


class ModelSettings(BaseModel):
    models: list[ModelInfo] = Field(default_factory=list)
    api_key: str = Field(alias="API_key")
    base_url: str
    api_format: str = "openai"

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

    @property
    def model_names(self) -> list[str]:
        return [m.name for m in self.models]

    @property
    def model_id_map(self) -> dict[str, str]:
        return {m.name: m.id for m in self.models}


def get_config_path() -> Path:
    return Path(os.getenv("MODELS_SETTING_PATH", "models_setting.json")).resolve()


def is_configured() -> bool:
    """每次都从磁盘检查，不缓存 True——避免配置文件被删后仍认为已配置。

    缓存 False 会导致删除文件后须重启才能再次通过 setup；
    缓存 True 则更危险（配置丢失后系统继续运行出错）。
    因此统一不缓存，依赖文件读取本身足够快。
    """
    config_path = get_config_path()
    if not config_path.exists():
        return False
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        settings = ModelSettings.model_validate(data)
        return bool(settings.models and settings.api_key and settings.base_url)
    except Exception:
        return False


def save_settings(data: dict) -> Path:
    """原子写入配置文件（写临时文件后 os.replace），避免并发或崩溃时损坏 JSON。"""
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=config_path.parent, prefix=".tmp_settings_", suffix=".json"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, config_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    get_settings.cache_clear()
    return config_path


@lru_cache(maxsize=1)
def get_settings() -> ModelSettings:
    config_path = get_config_path()
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
