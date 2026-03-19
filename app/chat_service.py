from __future__ import annotations

import asyncio
from collections.abc import Iterable
from time import perf_counter

from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from openai import AsyncOpenAI

from app.config import get_settings
from app.database import LocalDatabase
from app.request_logger import RequestLogger

MODEL_STREAM_TIMEOUT_SECONDS = 70
MAX_HISTORY_CHARS = 80_000


class MultiModelChatService:
    def __init__(self, request_logger: RequestLogger | None = None, database: LocalDatabase | None = None) -> None:
        settings = get_settings()
        self.models = settings.models
        self.client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)
        self.request_logger = request_logger or RequestLogger()
        self.database = database
        self._send_lock = asyncio.Lock()

    async def stream_round(
        self,
        histories: dict[str, list[dict[str, str]]],
        websocket: WebSocket,
        request_id: str,
        client_id: str,
        user_message: str,
        discussion_rounds: int = 1,
    ) -> list[dict[str, Any]]:
        all_results: list[dict[str, Any]] = []
        round_inputs: dict[str, str] = {}
        active_models = list(histories.keys())

        for round_number in range(1, discussion_rounds + 1):
            if round_number > 1:
                self._append_discussion_prompts(histories, round_inputs, round_number)

            await self._send_event(
                websocket,
                {
                    "type": "round_start",
                    "request_id": request_id,
                    "round": round_number,
                    "total_rounds": discussion_rounds,
                },
            )

            tasks = [
                self._stream_single_model(
                    model=model,
                    history=histories[model],
                    websocket=websocket,
                    request_id=request_id,
                    client_id=client_id,
                    user_message=user_message,
                    round_number=round_number,
                    total_rounds=discussion_rounds,
                )
                for model in active_models
            ]
            round_results = await asyncio.gather(*tasks)
            all_results.extend(round_results)

            round_inputs = {
                item["model"]: item["content"]
                for item in round_results
                if item.get("status") == "success" and item.get("content")
            }

            if round_number < discussion_rounds and not round_inputs:
                await self._send_event(
                    websocket,
                    {
                        "type": "discussion_stopped",
                        "request_id": request_id,
                        "round": round_number,
                        "total_rounds": discussion_rounds,
                        "reason": "本轮没有成功回复，已停止后续多人讨论。",
                    },
                )
                break

        return all_results

    async def _stream_single_model(
        self,
        model: str,
        history: list[dict[str, str]],
        websocket: WebSocket,
        request_id: str,
        client_id: str,
        user_message: str,
        round_number: int,
        total_rounds: int,
    ) -> dict[str, Any]:
        started_at = perf_counter()
        full_text = ""
        await self._send_event(
            websocket,
            {
                "type": "start",
                "request_id": request_id,
                "model": model,
                "round": round_number,
                "total_rounds": total_rounds,
            },
        )

        try:
            full_text = await asyncio.wait_for(
                self._collect_model_stream(
                    model=model,
                    history=history,
                    websocket=websocket,
                    request_id=request_id,
                    round_number=round_number,
                    total_rounds=total_rounds,
                ),
                timeout=MODEL_STREAM_TIMEOUT_SECONDS,
            )

            if not full_text:
                full_text = "[模型未返回文本内容]"

            history.append({"role": "assistant", "content": full_text})
            duration_ms = round((perf_counter() - started_at) * 1000, 2)
            result = {
                "type": "model_result",
                "status": "success",
                "request_id": request_id,
                "client_id": client_id,
                "model": model,
                "round": round_number,
                "duration_ms": duration_ms,
                "response_length": len(full_text),
                "history_size": len(history),
                "content": full_text,
            }
            await self.request_logger.log_event(
                {
                    "type": "model_result",
                    "status": "success",
                    "request_id": request_id,
                    "client_id": client_id,
                    "model": model,
                    "round": round_number,
                    "duration_ms": duration_ms,
                    "response_length": len(full_text),
                    "history_size": len(history),
                    "user_message": user_message,
                }
            )
            if self.database is not None:
                await self.database.record_model_result(
                    request_id=request_id,
                    model=model,
                    round_number=round_number,
                    status="success",
                    content=full_text,
                    duration_ms=duration_ms,
                    response_length=len(full_text),
                )
            await self._send_event(
                websocket,
                {
                    "type": "done",
                    "request_id": request_id,
                    "model": model,
                    "round": round_number,
                    "total_rounds": total_rounds,
                    "content": full_text,
                },
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if history and history[-1]["role"] == "user":
                history.pop()

            duration_ms = round((perf_counter() - started_at) * 1000, 2)
            error_text = "模型超时未完成回复。" if isinstance(exc, asyncio.TimeoutError) else str(exc)
            result = {
                "type": "model_result",
                "status": "error",
                "request_id": request_id,
                "client_id": client_id,
                "model": model,
                "round": round_number,
                "duration_ms": duration_ms,
                "error": error_text,
                "history_size": len(history),
            }
            await self.request_logger.log_event(
                {
                    **result,
                    "user_message": user_message,
                }
            )
            if self.database is not None:
                await self.database.record_model_result(
                    request_id=request_id,
                    model=model,
                    round_number=round_number,
                    status="error",
                    error_text=error_text,
                    duration_ms=duration_ms,
                )
            await self._send_event(
                websocket,
                {
                    "type": "error",
                    "request_id": request_id,
                    "model": model,
                    "round": round_number,
                    "total_rounds": total_rounds,
                    "content": error_text,
                },
            )
            return result

    async def _collect_model_stream(
        self,
        model: str,
        history: list[dict[str, str]],
        websocket: WebSocket,
        request_id: str,
        round_number: int,
        total_rounds: int,
    ) -> str:
        chunks: list[str] = []
        trimmed_history = self._trim_history(history)
        stream = await self.client.chat.completions.create(
            model=model,
            messages=trimmed_history,
            stream=True,
        )

        async for chunk in stream:
            delta_text = self._extract_delta_text(chunk)
            if not delta_text:
                continue
            chunks.append(delta_text)
            await self._send_event(
                websocket,
                {
                    "type": "delta",
                    "request_id": request_id,
                    "model": model,
                    "round": round_number,
                    "total_rounds": total_rounds,
                    "content": delta_text,
                },
            )
        return "".join(chunks)

    async def _send_event(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            try:
                await websocket.send_json(payload)
            except (WebSocketDisconnect, RuntimeError):
                return

    def _append_discussion_prompts(
        self,
        histories: dict[str, list[dict[str, str]]],
        round_inputs: dict[str, str],
        round_number: int,
    ) -> None:
        for model, history in histories.items():
            history.append(
                {
                    "role": "user",
                    "content": self._build_discussion_prompt(
                        current_model=model,
                        round_inputs=round_inputs,
                        round_number=round_number,
                    ),
                }
            )

    @staticmethod
    def _build_discussion_prompt(
        current_model: str,
        round_inputs: dict[str, str],
        round_number: int,
    ) -> str:
        peer_sections: list[str] = []
        for model, content in round_inputs.items():
            if model == current_model:
                continue
            cleaned = content.strip()
            if len(cleaned) > 1200:
                cleaned = cleaned[:1200] + "..."
            peer_sections.append(f"{model}:\n{cleaned}")

        if not peer_sections:
            return (
                f"现在进入第 {round_number} 轮讨论。请继续完善你刚才的观点，"
                "补充最关键的事实、风险或建议，避免重复。"
            )

        peers_text = "\n\n".join(peer_sections)
        return (
            f"现在进入第 {round_number} 轮多人讨论。以下是其他模型刚刚的观点：\n\n"
            f"{peers_text}\n\n"
            "请你像群聊中的一位同事继续发言：\n"
            "1. 明确指出你赞同、补充或反驳的观点；\n"
            "2. 优先回应最关键的分歧；\n"
            "3. 不要重复自己上一轮的原话；\n"
            "4. 保持简洁，控制在 3 到 6 句。"
        )

    @staticmethod
    def _trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
        total = sum(len(m.get("content", "")) for m in history)
        if total <= MAX_HISTORY_CHARS:
            return history
        # Keep system messages + last user message, trim middle messages
        system_msgs = [m for m in history if m["role"] == "system"]
        non_system = [m for m in history if m["role"] != "system"]
        budget = MAX_HISTORY_CHARS - sum(len(m.get("content", "")) for m in system_msgs)
        kept: list[dict[str, str]] = []
        for msg in reversed(non_system):
            cost = len(msg.get("content", ""))
            if budget - cost < 0 and kept:
                break
            budget -= cost
            kept.append(msg)
        kept.reverse()
        return system_msgs + kept

    @staticmethod
    def _extract_delta_text(chunk: Any) -> str:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return ""

        delta = getattr(choices[0], "delta", None)
        if delta is None:
            return ""

        content = getattr(delta, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, Iterable):
            parts: list[str] = []
            for item in content:
                text = getattr(item, "text", None)
                if text:
                    parts.append(text)
            return "".join(parts)
        return ""
