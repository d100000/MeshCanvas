from __future__ import annotations

import asyncio
import logging
from time import perf_counter

from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.database import LocalDatabase
from app.llm_client import LLMClient, LLMStream, create_llm_client
from app.request_logger import EventBus, RequestLogger

logger = logging.getLogger(__name__)

MODEL_STREAM_TIMEOUT_SECONDS = 70
MAX_HISTORY_CHARS = 80_000
MAX_AUTO_RETRIES = 2
RETRY_BACKOFF_SECONDS = [2, 5]
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _is_retryable_error(exc: Exception) -> bool:
    # Check HTTP status codes (works for both OpenAI and Anthropic SDK errors)
    status = getattr(exc, "status_code", None)
    if status and status in _RETRYABLE_STATUS_CODES:
        return True
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    exc_name = type(exc).__name__
    if any(k in exc_name for k in ("Timeout", "Connection", "ServiceUnavailable",
                                    "OverloadedError", "InternalServerError")):
        return True
    return False


class MultiModelChatService:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        models: list[dict[str, str]],
        api_format: str = "openai",
        request_logger: EventBus | None = None,
        database: LocalDatabase | None = None,
        extra_params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.models = [m["name"] for m in models]
        self.model_id_map = {m["name"]: m["id"] for m in models}
        self.api_format = (api_format or "openai").strip().lower()
        self.llm_client: LLMClient = create_llm_client(
            self.api_format,
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers or None,
        )
        self.request_logger = request_logger or RequestLogger()
        self.database = database
        self.extra_params = extra_params or {}
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
                self._append_discussion_prompts(
                    histories,
                    round_inputs,
                    round_number,
                    discussion_rounds,
                )

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

        last_error: Exception | None = None
        for attempt in range(1 + MAX_AUTO_RETRIES):
            try:
                full_text, usage = await asyncio.wait_for(
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
                prompt_tokens = usage["prompt_tokens"] if usage else 0
                completion_tokens = usage["completion_tokens"] if usage else 0
                total_tokens = usage["total_tokens"] if usage else 0
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
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                }
                if attempt > 0:
                    logger.info("model_retry_success model=%s attempt=%d request_id=%s", model, attempt + 1, request_id)
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
                        "attempt": attempt + 1,
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
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
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
                last_error = exc
                if attempt < MAX_AUTO_RETRIES and _is_retryable_error(exc):
                    backoff = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                    logger.warning(
                        "model_retry model=%s attempt=%d/%d backoff=%ds error=%s request_id=%s",
                        model, attempt + 1, 1 + MAX_AUTO_RETRIES, backoff,
                        type(exc).__name__, request_id,
                    )
                    await self.request_logger.log_event(
                        {
                            "type": "model_retry",
                            "request_id": request_id,
                            "client_id": client_id,
                            "model": model,
                            "round": round_number,
                            "attempt": attempt + 1,
                            "max_attempts": 1 + MAX_AUTO_RETRIES,
                            "error": str(exc)[:200],
                            "error_type": type(exc).__name__,
                            "backoff_s": backoff,
                        }
                    )
                    await asyncio.sleep(backoff)
                    continue
                break

        duration_ms = round((perf_counter() - started_at) * 1000, 2)
        exc = last_error
        if isinstance(exc, asyncio.TimeoutError):
            error_text = "模型超时未完成回复。"
        else:
            error_text_raw = str(exc) if exc else "未知错误"
            if len(error_text_raw) > 200:
                error_text_raw = error_text_raw[:200] + "…"
            error_text = f"模型调用失败：{error_text_raw}"
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
            "attempts": MAX_AUTO_RETRIES + 1,
        }
        logger.warning(
            "model_failed_after_retries model=%s attempts=%d request_id=%s error=%s",
            model, MAX_AUTO_RETRIES + 1, request_id, error_text,
        )
        await self.request_logger.log_event(
            {
                **result,
                "user_message": user_message,
                "error_type": type(exc).__name__ if exc else "unknown",
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
    ) -> tuple[str, dict[str, int] | None]:
        chunks: list[str] = []
        trimmed_history = self._trim_history(history)
        api_model_id = self.model_id_map.get(model, model)

        stream: LLMStream = self.llm_client.stream(
            model=api_model_id,
            messages=trimmed_history,
            extra_params=dict(self.extra_params) if self.extra_params else None,
        )

        async for delta_text in stream:
            if delta_text:
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

        usage_dict: dict[str, int] | None = None
        if stream.usage.total_tokens > 0 or stream.usage.prompt_tokens > 0:
            usage_dict = {
                "prompt_tokens": stream.usage.prompt_tokens,
                "completion_tokens": stream.usage.completion_tokens,
                "total_tokens": stream.usage.total_tokens,
            }
        return "".join(chunks), usage_dict

    async def _send_event(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            try:
                await websocket.send_json(payload)
            except asyncio.CancelledError:
                raise
            except (WebSocketDisconnect, RuntimeError, OSError):
                return
            except Exception:
                logger.debug("_send_event unexpected error", exc_info=True)
                return

    def _append_discussion_prompts(
        self,
        histories: dict[str, list[dict[str, str]]],
        round_inputs: dict[str, str],
        round_number: int,
        total_rounds: int,
    ) -> None:
        for model, history in histories.items():
            history.append(
                {
                    "role": "user",
                    "content": self._build_discussion_prompt(
                        current_model=model,
                        round_inputs=round_inputs,
                        round_number=round_number,
                        total_rounds=total_rounds,
                    ),
                }
            )

    @staticmethod
    def _build_discussion_prompt(
        current_model: str,
        round_inputs: dict[str, str],
        round_number: int,
        total_rounds: int,
    ) -> str:
        is_final_round = round_number >= total_rounds
        peer_sections: list[str] = []
        for model, content in round_inputs.items():
            if model == current_model:
                continue
            cleaned = content.strip()
            if len(cleaned) > 1200:
                cleaned = cleaned[:1200] + "..."
            peer_sections.append(f"{model}:\n{cleaned}")

        if not peer_sections:
            if is_final_round:
                return (
                    f"现在进入第 {round_number}/{total_rounds} 轮（最终轮）。"
                    "请基于前几轮内容给出你的最终结论版回答，"
                    "要求直接回应用户问题，保留核心依据与建议，避免重复过程描述。"
                )
            return (
                f"现在进入第 {round_number} 轮讨论。请继续完善你刚才的观点，"
                "补充最关键的事实、风险或建议，避免重复。"
            )

        peers_text = "\n\n".join(peer_sections)
        if is_final_round:
            return (
                f"现在进入第 {round_number}/{total_rounds} 轮多人讨论（最终轮）。以下是其他模型刚刚的观点：\n\n"
                f"{peers_text}\n\n"
                "请输出你的最终结论版回答：\n"
                "1. 必须直接回答用户原始问题；\n"
                "2. 综合前几轮讨论，保留关键依据、风险与建议；\n"
                "3. 可标注与你他人观点的一致点/分歧点，但避免展开冗长过程；\n"
                "4. 内容要完整且可执行。"
            )
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
        """按字符数裁剪历史（上限 MAX_HISTORY_CHARS）。

        注意：字符数与 token 数不等价，中文字符约 1 token/字，英文约 4 字符/token。
        当前设定的 80_000 字符对纯中文内容约为 80k token，请确认目标模型的上下文窗口
        足够大（如 gpt-4o 支持 128k token）；对上下文较短的模型可酌情降低该常量。
        """
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
