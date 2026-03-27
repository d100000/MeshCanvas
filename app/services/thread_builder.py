"""Builds ThreadState objects for main / branch / retry chat flows.

Returns ThreadState on success, or a string error message on failure.
The router layer is responsible for sending the error to the client.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from app.core.prompts import BASE_SYSTEM_PROMPT, BRANCH_PROMPT_TEMPLATE, THINK_PROMPT
from app.database import LocalDatabase
from app.models.chat import ThreadState
from app.services.history_utils import (
    build_initial_history,
    clone_history_before_assistant_round,
    clone_history_until_round,
    parse_bool,
    parse_discussion_rounds,
    parse_source_round,
)

logger = logging.getLogger(__name__)


class ThreadBuilder:
    """Stateless builder — depends only on database for thread recovery."""

    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    def build_main_thread(
        self,
        payload: dict[str, Any],
        available_models: list[str],
    ) -> ThreadState | str:
        message = str(payload.get("message", "")).strip()
        if not message:
            return "消息不能为空。"
        if len(message) > 4000:
            return "消息过长，请控制在 4000 字以内。"

        request_id = uuid4().hex
        discussion_rounds = parse_discussion_rounds(payload.get("discussion_rounds"))
        think_enabled = parse_bool(payload.get("think_enabled"), False)
        search_enabled = parse_bool(payload.get("search_enabled"), True)
        canvas_id = str(payload.get("canvas_id", "")).strip() or None

        histories: dict[str, list[dict[str, str]]] = {}
        for model in available_models:
            histories[model] = build_initial_history(
                user_message=message,
                think_enabled=think_enabled,
                search_bundle=None,
                model=model,
            )

        return ThreadState(
            request_id=request_id,
            models=list(available_models),
            histories=histories,
            user_message=message,
            discussion_rounds=discussion_rounds,
            search_enabled=search_enabled,
            think_enabled=think_enabled,
            search_bundle=None,
            canvas_id=canvas_id,
        )

    async def build_branch_thread(
        self,
        payload: dict[str, Any],
        threads: dict[str, ThreadState],
        user_id: int,
    ) -> ThreadState | str:
        message = str(payload.get("message", "")).strip()
        parent_request_id = str(payload.get("source_request_id", "")).strip()
        source_model = str(payload.get("source_model", "")).strip()
        source_round = parse_source_round(payload.get("source_round"))
        canvas_id = str(payload.get("canvas_id", "")).strip() or None

        if not message:
            return "分支内容不能为空。"
        if len(message) > 4000:
            return "分支内容过长，请控制在 4000 字以内。"

        if parent_request_id not in threads:
            loaded = await self._rebuild_thread_from_db(parent_request_id, user_id)
            if loaded is None:
                return "未找到分支来源会话。"
            threads[parent_request_id] = loaded

        parent_thread = threads[parent_request_id]
        if source_model not in parent_thread.histories:
            return "未找到分支来源模型。"

        request_id = uuid4().hex
        discussion_rounds = parse_discussion_rounds(payload.get("discussion_rounds"))
        think_enabled = parse_bool(payload.get("think_enabled"), False)
        search_enabled = parse_bool(payload.get("search_enabled"), True)
        branch_prompt = BRANCH_PROMPT_TEMPLATE.format(source_round=source_round)
        parent_history = parent_thread.histories[source_model]
        inherited_history = clone_history_until_round(parent_history, source_round)

        branch_history = [dict(item) for item in inherited_history]
        if think_enabled:
            branch_history.append({"role": "system", "content": THINK_PROMPT})
        branch_history.append({"role": "user", "content": f"{branch_prompt}\n\n用户分支指令：{message}"})

        return ThreadState(
            request_id=request_id,
            models=[source_model],
            histories={source_model: branch_history},
            user_message=message,
            discussion_rounds=discussion_rounds,
            search_enabled=search_enabled,
            think_enabled=think_enabled,
            search_bundle=None,
            parent_request_id=parent_request_id,
            source_model=source_model,
            source_round=source_round,
            canvas_id=canvas_id,
        )

    async def build_retry_thread(
        self,
        payload: dict[str, Any],
        threads: dict[str, ThreadState],
        user_id: int,
    ) -> ThreadState | str:
        parent_request_id = str(payload.get("source_request_id", "")).strip()
        source_model = str(payload.get("source_model", "")).strip()
        source_round = parse_source_round(payload.get("source_round"))
        canvas_id = str(payload.get("canvas_id", "")).strip() or None

        if parent_request_id not in threads:
            loaded = await self._rebuild_thread_from_db(parent_request_id, user_id)
            if loaded is None:
                return "未找到重试来源会话。"
            threads[parent_request_id] = loaded

        parent_thread = threads[parent_request_id]
        if source_model not in parent_thread.histories:
            return "未找到重试来源模型。"

        retry_history = clone_history_before_assistant_round(parent_thread.histories[source_model], source_round)
        if retry_history is None:
            return "未找到可重试的轮次内容。"

        request_id = uuid4().hex
        return ThreadState(
            request_id=request_id,
            models=[source_model],
            histories={source_model: retry_history},
            user_message=f"重试 {source_model} 第 {source_round} 轮",
            discussion_rounds=1,
            search_enabled=False,
            think_enabled=parent_thread.think_enabled,
            search_bundle=None,
            parent_request_id=parent_request_id,
            source_model=source_model,
            source_round=source_round,
            canvas_id=canvas_id,
            meta={"display_message": f"重试 {source_model} · 第 {source_round} 轮"},
        )

    async def _rebuild_thread_from_db(self, request_id: str, user_id: int) -> ThreadState | None:
        data = await self.database.get_request_with_results(request_id, user_id)
        if not data:
            return None
        histories: dict[str, list[dict[str, str]]] = {}
        for model in data["models"]:
            model_rounds = sorted(data["model_results"].get(model, []), key=lambda x: x["round"])
            history: list[dict[str, str]] = [
                {"role": "system", "content": f"{BASE_SYSTEM_PROMPT}\n当前模型标识：{model}。"}
            ]
            if data["think_enabled"]:
                history.append({"role": "system", "content": THINK_PROMPT})
            history.append({"role": "user", "content": data["user_message"]})
            for i, round_data in enumerate(model_rounds):
                if round_data["content"]:
                    history.append({"role": "assistant", "content": round_data["content"]})
                    if i < len(model_rounds) - 1:
                        history.append({"role": "user", "content": "请继续下一轮分析。"})
            histories[model] = history
        return ThreadState(
            request_id=request_id,
            models=data["models"],
            histories=histories,
            user_message=data["user_message"],
            discussion_rounds=data["discussion_rounds"],
            search_enabled=data["search_enabled"],
            think_enabled=data["think_enabled"],
            parent_request_id=data["parent_request_id"],
            source_model=data["source_model"],
            source_round=data["source_round"],
        )
