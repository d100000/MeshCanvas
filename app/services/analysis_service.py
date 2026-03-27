"""LLM-powered summarization and conversation analysis."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.llm_client_factory import LLMClientFactory

logger = logging.getLogger(__name__)


class AnalysisService:
    def __init__(self, factory: LLMClientFactory | None = None) -> None:
        self._factory = factory or LLMClientFactory()

    async def summarize_selection(
        self, bundle: str, count: int, *, user_settings: dict[str, Any],
    ) -> tuple[str, str]:
        models = user_settings["models"]
        model = self._factory.pick_analysis_model(models)
        if not model:
            raise RuntimeError("未配置可用的摘要模型。")

        id_map = self._factory.build_model_id_map(models)
        client = self._factory.create(user_settings)
        clipped_bundle = bundle[:12000]
        prompt = (
            f"请将用户圈选的 {count} 个无限画布节点压缩成可供下一轮对话继续使用的上下文。\n"
            "输出要求：\n"
            "1. 使用简洁中文；\n"
            "2. 优先保留最终结论、关键依据、核心分歧、下一步建议；\n"
            "3. 不要重复原文，不要展开长篇推理；\n"
            "4. 控制在 220 到 300 字内，可使用 Markdown 列表。\n\n"
            "以下是待压缩的节点内容：\n\n"
            f"{clipped_bundle}"
        )
        response = await client.chat.completions.create(
            model=id_map.get(model, model),
            messages=[
                {
                    "role": "system",
                    "content": "你是无限画布里的上下文压缩助手，只输出给下一轮模型使用的高密度摘要。",
                },
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
        summary = self._factory.extract_completion_text(response)
        if not summary:
            raise RuntimeError("摘要模型未返回可用内容。")
        return summary[:1200], model

    async def analyze_conversation(
        self, messages: list[dict[str, str]], *, user_settings: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        models = user_settings["models"]
        model = self._factory.pick_analysis_model(models)
        if not model:
            raise RuntimeError("未配置可用的分析模型。")

        id_map = self._factory.build_model_id_map(models)
        client = self._factory.create(user_settings)

        conversation_text = ""
        for msg in messages:
            role_label = {"user": "用户", "assistant": "模型", "system": "系统"}.get(msg.get("role", ""), msg.get("role", ""))
            content = msg.get("content", "").strip()
            if content:
                conversation_text += f"【{role_label}】{content}\n\n"
        conversation_text = conversation_text[:16000]

        prompt = (
            "请分析以下对话内容，输出 JSON 格式（不要输出其他内容）：\n"
            '{"title": "一句话标题（15字以内）", "key_points": ["要点1", "要点2", ...], "summary": "整体摘要（100-200字）", "topic_tags": ["标签1", "标签2"]}\n\n'
            "要求：\n"
            "1. title：用一句话概括对话主题；\n"
            "2. key_points：提取 3-5 个核心要点；\n"
            "3. summary：简明概括对话的来龙去脉和关键结论；\n"
            "4. topic_tags：2-4 个话题标签。\n\n"
            f"对话内容：\n\n{conversation_text}"
        )

        response = await client.chat.completions.create(
            model=id_map.get(model, model),
            messages=[
                {"role": "system", "content": "你是会话分析助手，只输出 JSON，不要输出任何解释。"},
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
        raw_text = self._factory.extract_completion_text(response)
        if not raw_text:
            raise RuntimeError("分析模型未返回可用内容。")

        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            result = {"title": "", "key_points": [], "summary": raw_text[:500], "topic_tags": []}

        return result, model
