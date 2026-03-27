"""Shared factory for creating OpenAI-compatible LLM clients."""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI


class LLMClientFactory:
    @staticmethod
    def create(user_settings: dict[str, Any]) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=user_settings["api_key"],
            base_url=user_settings["api_base_url"],
        )

    @staticmethod
    def pick_analysis_model(models: list[dict[str, str]]) -> str:
        names = [m["name"] for m in models]
        if "Kimi-K2.5" in names:
            return "Kimi-K2.5"
        for name in names:
            if "kimi" in name.lower():
                return name
        return names[0] if names else ""

    @staticmethod
    def build_model_id_map(models: list[dict[str, str]]) -> dict[str, str]:
        return {m["name"]: m["id"] for m in models}

    @staticmethod
    def extract_completion_text(response: object) -> str:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        if message is None:
            return ""
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                else:
                    text = getattr(item, "text", None) or getattr(item, "content", None) or ""
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n".join(parts).strip()
        return str(content or "").strip()
