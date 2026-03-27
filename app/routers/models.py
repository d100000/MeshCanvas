"""Model listing route."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.config import get_global_user_settings
from app.dependencies import get_request_user, unauthorized_json
from app.services.llm_client_factory import LLMClientFactory

router = APIRouter()


@router.get("/api/models")
async def list_models(request: Request):
    user = await get_request_user(request)
    if not user:
        return unauthorized_json()
    global_settings = get_global_user_settings()
    models = global_settings["models"]
    return {
        "models": models,
        "analysis_model": LLMClientFactory.pick_analysis_model(models),
    }
