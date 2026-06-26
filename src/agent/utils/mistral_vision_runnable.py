"""LangChain-compatible runnable adapter for Mistral Vision.

This adapter lets the visual validator keep using the existing project rule:
all model calls go through `metered_ainvoke(...)`.

`ChatMistralAI` is still used for text/tool workflows. This adapter is only
for image input. It uses the Mistral chat-completions HTTP endpoint directly so
we can control read/write/connect timeouts explicitly.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
from langchain_core.messages import AIMessage, BaseMessage

try:
    from langsmith import traceable
except Exception:  # pragma: no cover - tracing is optional at runtime
    def traceable(*args: Any, **kwargs: Any):  # type: ignore[misc]
        def decorator(func: Any) -> Any:
            return func

        return decorator

from agent.utils.llm_control import shared_rate_limiter


MISTRAL_CHAT_COMPLETIONS_URL = "https://api.mistral.ai/v1/chat/completions"


def _int_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default

    try:
        return int(raw)
    except ValueError:
        return default


def _float_from_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default

    try:
        return float(raw)
    except ValueError:
        return default


def _read_timeout_seconds(default: float = 180.0) -> float:
    """Read timeout from env, supporting seconds and millisecond variants."""
    if os.getenv("MISTRAL_VISION_TIMEOUT_MS"):
        return max(_float_from_env("MISTRAL_VISION_TIMEOUT_MS", default * 1000) / 1000.0, 1.0)

    # Backward-compatible with the shorter name discussed during debugging.
    if os.getenv("MISTRAL_TIMEOUT_MS"):
        return max(_float_from_env("MISTRAL_TIMEOUT_MS", default * 1000) / 1000.0, 1.0)

    return max(_float_from_env("MISTRAL_VISION_TIMEOUT_SECONDS", default), 1.0)


@traceable(
    name="mistral_vision_sdk_call",
    run_type="llm",
)
async def traced_mistral_vision_call(
    *,
    api_key: str,
    api_url: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    response_format: dict[str, Any] | None,
    max_tokens: int | None,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    write_timeout_seconds: float,
    pool_timeout_seconds: float,
) -> dict[str, Any]:
    """Execute the Mistral Vision request with explicit HTTP timeouts.

    The function name intentionally preserves `mistral_vision_sdk_call` so the
    existing LangSmith trace remains easy to compare with previous runs.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }

    if response_format is not None:
        payload["response_format"] = response_format

    if max_tokens is not None and max_tokens > 0:
        payload["max_tokens"] = max_tokens

    timeout = httpx.Timeout(
        connect=connect_timeout_seconds,
        read=read_timeout_seconds,
        write=write_timeout_seconds,
        pool=pool_timeout_seconds,
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # The validator's make_mistral_error_report checks `status_code`.
        setattr(exc, "status_code", response.status_code)
        setattr(exc, "response_text", response.text[:4000])
        raise

    return response.json()


class MistralVisionRunnable:
    """
    Minimal adapter with an `ainvoke(messages)` method.

    It returns an AIMessage with usage_metadata so the existing
    `metered_ainvoke(...)` wrapper can extract and record token usage normally.
    """

    def __init__(
        self,
        *,
        image_data_url: str,
        model: str | None = None,
        temperature: float = 0.0,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.image_data_url = image_data_url
        self.model = model or os.getenv("MISTRAL_VISION_MODEL", "mistral-large-2512")
        self.temperature = temperature
        self.response_format = response_format or {"type": "json_object"}
        self.api_key = os.environ["MISTRAL_API_KEY"]
        self.api_url = os.getenv("MISTRAL_API_URL", MISTRAL_CHAT_COMPLETIONS_URL)

        self.max_tokens = (
            max_tokens
            if max_tokens is not None
            else _int_from_env("MISTRAL_VISION_MAX_TOKENS", 6000)
        )
        self.connect_timeout_seconds = _float_from_env("MISTRAL_VISION_CONNECT_TIMEOUT_SECONDS", 10.0)
        self.read_timeout_seconds = timeout_seconds if timeout_seconds is not None else _read_timeout_seconds(180.0)
        self.write_timeout_seconds = _float_from_env("MISTRAL_VISION_WRITE_TIMEOUT_SECONDS", 60.0)
        self.pool_timeout_seconds = _float_from_env("MISTRAL_VISION_POOL_TIMEOUT_SECONDS", 10.0)

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        """
        Execute one Mistral Vision chat completion.

        The request limiter is acquired here because ChatMistralAI normally owns
        request-rate limiting internally. Since this adapter uses a direct HTTP
        call, it must acquire the shared limiter manually.
        """
        await self._acquire_request_slot()

        prompt_text = self._messages_to_text(messages)

        sdk_messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt_text,
                    },
                    {
                        "type": "image_url",
                        "image_url": self.image_data_url,
                    },
                ],
            }
        ]

        response_payload = await traced_mistral_vision_call(
            api_key=self.api_key,
            api_url=self.api_url,
            model=self.model,
            messages=sdk_messages,
            temperature=self.temperature,
            response_format=self.response_format,
            max_tokens=self.max_tokens,
            connect_timeout_seconds=self.connect_timeout_seconds,
            read_timeout_seconds=self.read_timeout_seconds,
            write_timeout_seconds=self.write_timeout_seconds,
            pool_timeout_seconds=self.pool_timeout_seconds,
        )

        content = self._extract_content(response_payload)
        usage = self._extract_usage(
            response_payload,
            prompt_text=prompt_text,
            content_text=content,
        )

        payload_bytes = self._estimate_payload_bytes(sdk_messages)

        return AIMessage(
            content=content,
            usage_metadata={
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "total_tokens": usage["total_tokens"],
            },
            response_metadata={
                "model": self.model,
                "token_usage": {
                    "prompt_tokens": usage["input_tokens"],
                    "completion_tokens": usage["output_tokens"],
                    "total_tokens": usage["total_tokens"],
                },
                "mistral_vision": {
                    "transport": "httpx",
                    "api_url": self.api_url,
                    "max_tokens": self.max_tokens,
                    "read_timeout_seconds": self.read_timeout_seconds,
                    "payload_bytes": payload_bytes,
                },
            },
        )

    async def _acquire_request_slot(self) -> None:
        async_acquire = getattr(shared_rate_limiter, "aacquire", None)

        if async_acquire is not None:
            try:
                await async_acquire(blocking=True)
            except TypeError:
                await async_acquire()
            return

        acquire = getattr(shared_rate_limiter, "acquire", None)

        if acquire is None:
            return

        try:
            await asyncio.to_thread(acquire, True)
        except TypeError:
            await asyncio.to_thread(acquire)

    def _messages_to_text(self, messages: list[BaseMessage]) -> str:
        parts: list[str] = []

        for message in messages:
            content = getattr(message, "content", "")

            if isinstance(content, str):
                parts.append(content)
            else:
                parts.append(str(content))

        return "\n\n".join(parts)

    def _extract_content(self, response_payload: dict[str, Any]) -> str:
        choices = response_payload.get("choices") or []
        if not choices:
            return ""

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return str(first_choice)

        message = first_choice.get("message") or {}
        if not isinstance(message, dict):
            return str(message)

        content = message.get("content", "")

        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )

        return str(content)

    def _extract_usage(
        self,
        response: Any,
        *,
        prompt_text: str,
        content_text: str,
    ) -> dict[str, int]:
        if isinstance(response, dict):
            usage = response.get("usage") or {}
        else:
            usage = getattr(response, "usage", None) or {}

        if isinstance(usage, dict):
            input_tokens = int(
                usage.get("prompt_tokens")
                or usage.get("input_tokens")
                or 0
            )
            output_tokens = int(
                usage.get("completion_tokens")
                or usage.get("output_tokens")
                or 0
            )
            total_tokens = int(
                usage.get("total_tokens")
                or input_tokens + output_tokens
                or 0
            )
        else:
            input_tokens = int(
                getattr(usage, "prompt_tokens", None)
                or getattr(usage, "input_tokens", None)
                or 0
            )
            output_tokens = int(
                getattr(usage, "completion_tokens", None)
                or getattr(usage, "output_tokens", None)
                or 0
            )
            total_tokens = int(
                getattr(usage, "total_tokens", None)
                or input_tokens + output_tokens
                or 0
            )

        # Fallback: protect token accounting if the HTTP response does not expose usage.
        if total_tokens <= 0:
            estimated_image_tokens = int(
                os.getenv("MISTRAL_VISION_EXTRA_ESTIMATED_TOKENS", "3000")
            )
            input_tokens = max(len(prompt_text) // 4, 1) + estimated_image_tokens
            output_tokens = max(len(content_text) // 4, 1)
            total_tokens = input_tokens + output_tokens

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

    def _estimate_payload_bytes(self, sdk_messages: list[dict[str, Any]]) -> int:
        payload = {
            "model": self.model,
            "messages": sdk_messages,
            "temperature": self.temperature,
            "response_format": self.response_format,
            "max_tokens": self.max_tokens,
        }
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
