"""Unified LLM client abstraction supporting OpenAI and Anthropic API formats.

Usage:
    client = create_llm_client("openai", api_key="...", base_url="...", headers={})
    # Non-streaming
    resp = await client.complete(model="gpt-4o", messages=[...])
    print(resp.text, resp.prompt_tokens)
    # Streaming
    stream = client.stream(model="gpt-4o", messages=[...])
    async for delta in stream:
        print(delta, end="")
    print(stream.usage)  # available after iteration
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared data structures
# ---------------------------------------------------------------------------

@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """Normalised non-streaming response."""
    text: str
    usage: TokenUsage = field(default_factory=TokenUsage)


# ---------------------------------------------------------------------------
# Base protocol
# ---------------------------------------------------------------------------

class LLMClient:
    """Abstract LLM client with complete() and stream() methods."""

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        raise NotImplementedError

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        extra_params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> LLMStream:
        raise NotImplementedError


class LLMStream:
    """Async-iterable stream wrapper. After iteration, `.usage` is populated."""

    usage: TokenUsage

    def __init__(self) -> None:
        self.usage = TokenUsage()

    def __aiter__(self) -> AsyncIterator[str]:
        raise NotImplementedError

    async def __anext__(self) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OpenAI implementation
# ---------------------------------------------------------------------------

def _is_unsupported_stream_option_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status != 400:
        return False
    msg = str(exc).lower()
    return (
        "stream_options" in msg
        or "include_usage" in msg
        or "unknown parameter" in msg
        or "unrecognized request argument" in msg
        or "extra fields not permitted" in msg
    )


class OpenAIClient(LLMClient):
    def __init__(self, api_key: str, base_url: str, default_headers: dict[str, str] | None = None) -> None:
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers or None,
        )

    async def complete(self, *, model, messages, temperature=None, max_tokens=None, **kwargs) -> LLMResponse:
        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            create_kwargs["temperature"] = temperature
        if max_tokens is not None:
            create_kwargs["max_tokens"] = max_tokens
        create_kwargs.update(kwargs)

        response = await self.client.chat.completions.create(**create_kwargs)
        text = _extract_openai_completion_text(response)
        usage = _extract_openai_usage(response)
        return LLMResponse(text=text, usage=usage)

    def stream(self, *, model, messages, extra_params=None, **kwargs) -> OpenAIStream:
        return OpenAIStream(
            client=self.client,
            model=model,
            messages=messages,
            extra_params=extra_params or {},
            kwargs=kwargs,
        )


class OpenAIStream(LLMStream):
    def __init__(self, client, model, messages, extra_params, kwargs) -> None:
        super().__init__()
        self._client = client
        self._create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if extra_params:
            self._create_kwargs.update(extra_params)
        self._create_kwargs.update(kwargs)
        self._stream = None
        self._started = False

    async def _ensure_stream(self):
        if self._stream is not None:
            return
        try:
            self._stream = await self._client.chat.completions.create(**self._create_kwargs)
        except Exception as exc:
            if self._create_kwargs.get("stream_options") and _is_unsupported_stream_option_error(exc):
                logger.info("openai_stream: fallback without stream_options, reason=%s", type(exc).__name__)
                self._create_kwargs.pop("stream_options", None)
                self._stream = await self._client.chat.completions.create(**self._create_kwargs)
            else:
                raise

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        await self._ensure_stream()
        while True:
            try:
                chunk = await self._stream.__anext__()
            except StopAsyncIteration:
                raise
            # Capture usage from final chunk
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage:
                self.usage = TokenUsage(
                    prompt_tokens=getattr(chunk_usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(chunk_usage, "completion_tokens", 0) or 0,
                    total_tokens=getattr(chunk_usage, "total_tokens", 0) or 0,
                )
            # Extract delta text
            text = _extract_openai_delta_text(chunk)
            if text:
                return text
            # Skip empty deltas, keep iterating


def _extract_openai_completion_text(response: object) -> str:
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


def _extract_openai_delta_text(chunk: object) -> str:
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


def _extract_openai_usage(response: object) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        total_tokens=getattr(usage, "total_tokens", 0) or 0,
    )


# ---------------------------------------------------------------------------
# Anthropic implementation
# ---------------------------------------------------------------------------

def _split_system_messages(messages: list[dict[str, str]]) -> tuple[str | None, list[dict[str, str]]]:
    """Anthropic requires system prompt as a top-level parameter, not in messages."""
    system_parts: list[str] = []
    non_system: list[dict[str, str]] = []
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content", "").strip()
            if content:
                system_parts.append(content)
        else:
            non_system.append(m)
    # Anthropic requires messages to start with "user" role.
    # If first message is "assistant", prepend a minimal user message.
    if non_system and non_system[0].get("role") != "user":
        non_system.insert(0, {"role": "user", "content": "请继续。"})
    # Anthropic doesn't allow consecutive same-role messages; merge them.
    merged: list[dict[str, str]] = []
    for m in non_system:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1] = {
                "role": m["role"],
                "content": merged[-1]["content"] + "\n\n" + m.get("content", ""),
            }
        else:
            merged.append(dict(m))
    system_text = "\n\n".join(system_parts) if system_parts else None
    return system_text, merged


class AnthropicClient(LLMClient):
    def __init__(self, api_key: str, base_url: str | None = None, default_headers: dict[str, str] | None = None) -> None:
        from anthropic import AsyncAnthropic
        init_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            # Anthropic SDK expects base_url without /v1 suffix
            clean_url = base_url.rstrip("/")
            if clean_url.endswith("/v1"):
                clean_url = clean_url[:-3]
            init_kwargs["base_url"] = clean_url
        if default_headers:
            init_kwargs["default_headers"] = default_headers
        self.client = AsyncAnthropic(**init_kwargs)

    async def complete(self, *, model, messages, temperature=None, max_tokens=None, **kwargs) -> LLMResponse:
        system_text, clean_messages = _split_system_messages(messages)
        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": clean_messages,
            "max_tokens": max_tokens or 4096,
        }
        if system_text:
            create_kwargs["system"] = system_text
        if temperature is not None:
            create_kwargs["temperature"] = temperature
        # Filter out OpenAI-only kwargs that Anthropic doesn't support
        for key in ("stream_options", "stream"):
            kwargs.pop(key, None)

        response = await self.client.messages.create(**create_kwargs)
        text = _extract_anthropic_text(response)
        usage = _extract_anthropic_usage(response)
        return LLMResponse(text=text, usage=usage)

    def stream(self, *, model, messages, extra_params=None, **kwargs) -> AnthropicStream:
        return AnthropicStream(
            client=self.client,
            model=model,
            messages=messages,
            extra_params=extra_params or {},
            kwargs=kwargs,
        )


class AnthropicStream(LLMStream):
    def __init__(self, client, model, messages, extra_params, kwargs) -> None:
        super().__init__()
        self._client = client
        self._model = model
        self._messages = messages
        self._extra_params = extra_params
        self._kwargs = kwargs
        self._stream_ctx = None
        self._stream = None

    async def _ensure_stream(self):
        if self._stream is not None:
            return
        system_text, clean_messages = _split_system_messages(self._messages)
        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": clean_messages,
            "max_tokens": self._extra_params.pop("max_tokens", None) or self._kwargs.pop("max_tokens", None) or 4096,
        }
        if system_text:
            create_kwargs["system"] = system_text
        # Pass through compatible params (use sentinel to handle 0/0.0 correctly)
        _MISSING = object()
        for key in ("temperature", "top_p", "top_k"):
            val = self._extra_params.pop(key, _MISSING)
            if val is _MISSING:
                val = self._kwargs.pop(key, _MISSING)
            if val is not _MISSING:
                create_kwargs[key] = val
        # Filter out OpenAI-only params
        for key in ("stream_options", "stream", "frequency_penalty", "presence_penalty",
                     "logprobs", "top_logprobs", "n", "seed"):
            self._extra_params.pop(key, None)
            self._kwargs.pop(key, None)
        # Apply remaining extra_params cautiously
        # (most extra params are OpenAI-specific, skip them for Anthropic)

        self._stream_ctx = self._client.messages.stream(**create_kwargs)
        self._stream = await self._stream_ctx.__aenter__()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        await self._ensure_stream()
        while True:
            try:
                event = await self._stream.__anext__()
            except StopAsyncIteration:
                # Capture final usage from the accumulated message
                await self._finalize_usage()
                raise

            # Anthropic stream events have different types
            event_type = getattr(event, "type", "")
            if event_type == "content_block_delta":
                delta = getattr(event, "delta", None)
                if delta:
                    text = getattr(delta, "text", "")
                    if text:
                        return text

            # Capture usage from message_start or message_delta events
            if event_type == "message_start":
                msg = getattr(event, "message", None)
                if msg:
                    usage = getattr(msg, "usage", None)
                    if usage:
                        self.usage.prompt_tokens = getattr(usage, "input_tokens", 0) or 0
            elif event_type == "message_delta":
                usage = getattr(event, "usage", None)
                if usage:
                    self.usage.completion_tokens = getattr(usage, "output_tokens", 0) or 0
                    self.usage.total_tokens = self.usage.prompt_tokens + self.usage.completion_tokens

    async def _finalize_usage(self):
        """Try to get final usage after stream ends."""
        if self._stream_ctx is not None:
            try:
                final_message = await self._stream_ctx.get_final_message()
                if final_message and hasattr(final_message, "usage"):
                    u = final_message.usage
                    self.usage = TokenUsage(
                        prompt_tokens=getattr(u, "input_tokens", 0) or 0,
                        completion_tokens=getattr(u, "output_tokens", 0) or 0,
                        total_tokens=(getattr(u, "input_tokens", 0) or 0) + (getattr(u, "output_tokens", 0) or 0),
                    )
            except Exception:
                pass  # Best-effort
            await self._close()

    async def _close(self):
        """Clean up the stream context manager (safe to call multiple times)."""
        ctx = self._stream_ctx
        if ctx is not None:
            self._stream_ctx = None
            self._stream = None
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass

    def __del__(self):
        # Warn if stream was opened but never properly closed
        if self._stream_ctx is not None:
            logger.debug("AnthropicStream was not properly closed; consider awaiting full iteration")


def _extract_anthropic_text(response: object) -> str:
    content = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in content:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _extract_anthropic_usage(response: object) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    return TokenUsage(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_llm_client(
    api_format: str,
    *,
    api_key: str,
    base_url: str,
    default_headers: dict[str, str] | None = None,
) -> LLMClient:
    """Create an LLM client based on the API format."""
    fmt = (api_format or "openai").strip().lower()
    if fmt == "anthropic":
        return AnthropicClient(api_key=api_key, base_url=base_url, default_headers=default_headers)
    return OpenAIClient(api_key=api_key, base_url=base_url, default_headers=default_headers)
