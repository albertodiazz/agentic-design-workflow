"""LangChain-compatible runnable adapter for Mistral Vision.

This adapter lets the visual validator keep using the existing project rule:
all model calls go through `metered_ainvoke(...)`.

`ChatMistralAI` is still used for text/tool workflows. This adapter only exists
because the LangChain Mistral wrapper does not expose image input in the current
integration, while the official Mistral SDK does.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

try:
    from mistralai import Mistral
except ImportError:  # compatibility with older SDK layout
    from mistralai.client import Mistral  # type: ignore

from agent.utils.llm_control import shared_rate_limiter


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
    ) -> None:
        self.image_data_url = image_data_url
        self.model = model or os.getenv("MISTRAL_VISION_MODEL", "mistral-large-2512")
        self.temperature = temperature
        self.response_format = response_format or {"type": "json_object"}
        self.client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        """
        Execute one Mistral Vision chat completion.

        The request limiter is acquired here because ChatMistralAI normally owns
        request-rate limiting internally. Since this adapter uses the official
        SDK, it must acquire the shared limiter manually.
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

        response = await self.client.chat.complete_async(
            model=self.model,
            messages=sdk_messages,
            temperature=self.temperature,
            response_format=self.response_format,
        )

        content = response.choices[0].message.content

        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )

        content_text = str(content)
        usage = self._extract_usage(
            response,
            prompt_text=prompt_text,
            content_text=content_text,
        )

        return AIMessage(
            content=content_text,
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

    def _extract_usage(
        self,
        response: Any,
        *,
        prompt_text: str,
        content_text: str,
    ) -> dict[str, int]:
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

        # Fallback: protect token accounting if the SDK response does not expose usage.
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
