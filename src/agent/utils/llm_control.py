"""Shared LLM rate and token metering utilities."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque

from langchain_core.rate_limiters import InMemoryRateLimiter


REQUESTS_PER_SECOND = float(os.getenv("MISTRAL_REQUESTS_PER_SECOND", "0.6"))

MISTRAL_TOKENS_PER_MINUTE = int(
    os.getenv("MISTRAL_TOKENS_PER_MINUTE", "20000")
)

TOKEN_WINDOW_SECONDS = int(
    os.getenv("TOKEN_WINDOW_SECONDS", "60")
)


shared_rate_limiter = InMemoryRateLimiter(
    requests_per_second=REQUESTS_PER_SECOND,
    check_every_n_seconds=0.1,
    max_bucket_size=1,
)


class TokenWindowLimiter:
    """Simple in-memory token limiter."""

    def __init__(self, max_tokens: int, window_seconds: int = 60):
        self.max_tokens = max_tokens
        self.window_seconds = window_seconds
        self.events: Deque[tuple[float, int]] = deque()
        self.lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        while self.events and now - self.events[0][0] >= self.window_seconds:
            self.events.popleft()

    def _used_tokens(self, now: float) -> int:
        self._prune(now)
        return sum(tokens for _, tokens in self.events)

    async def record(self, tokens: int) -> int:
        if tokens <= 0:
            return 0

        async with self.lock:
            now = time.monotonic()
            self._prune(now)
            self.events.append((now, tokens))
            return self._used_tokens(now)

    async def wait_if_needed(self, estimated_next_tokens: int = 0) -> tuple[int, bool]:
        waited = False

        # Evita loop infinito si la estimación de una sola llamada supera el límite.
        estimated_next_tokens = min(estimated_next_tokens, self.max_tokens)

        while True:
            async with self.lock:
                now = time.monotonic()
                used = self._used_tokens(now)

                if used + estimated_next_tokens <= self.max_tokens:
                    return used, waited

                if not self.events:
                    return used, waited

                oldest_timestamp = self.events[0][0]
                seconds_until_free = self.window_seconds - (now - oldest_timestamp)

            waited = True
            await asyncio.sleep(max(seconds_until_free, 1))


token_limiter = TokenWindowLimiter(
    max_tokens=MISTRAL_TOKENS_PER_MINUTE,
    window_seconds=TOKEN_WINDOW_SECONDS,
)


@dataclass
class MeteredLLMResult:
    ai_message: Any
    input_tokens: int
    output_tokens: int
    total_tokens: int
    token_window_used: int
    token_gate_waited: bool


def estimate_message_tokens(messages: list[Any]) -> int:
    total_chars = 0

    for message in messages:
        content = getattr(message, "content", "")
        total_chars += len(str(content))

    return max(total_chars // 4, 1)


def extract_usage_from_ai_message(ai_message: Any) -> dict[str, int]:
    usage = getattr(ai_message, "usage_metadata", None) or {}

    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)

    if total_tokens == 0:
        response_metadata = getattr(ai_message, "response_metadata", {}) or {}
        token_usage = response_metadata.get("token_usage", {}) or {}

        input_tokens = int(token_usage.get("prompt_tokens") or input_tokens)
        output_tokens = int(token_usage.get("completion_tokens") or output_tokens)
        total_tokens = int(token_usage.get("total_tokens") or total_tokens)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


async def metered_ainvoke(
    llm_runnable: Any,
    messages: list[Any],
    *,
    estimated_completion_tokens: int = 1000,
    extra_estimated_tokens: int = 0,
) -> MeteredLLMResult:
    """
    Wrapper obligatorio para cualquier llamada al LLM.

    Hace:
    1. Estima tokens antes de llamar.
    2. Espera si la ventana de tokens está llena.
    3. Ejecuta el LLM.
    4. Registra consumo real.
    """

    estimated_prompt_tokens = estimate_message_tokens(messages)

    estimated_next_tokens = (
        estimated_prompt_tokens
        + estimated_completion_tokens
        + extra_estimated_tokens
    )

    _, waited = await token_limiter.wait_if_needed(
        estimated_next_tokens=estimated_next_tokens
    )

    ai_message = await llm_runnable.ainvoke(messages)

    usage = extract_usage_from_ai_message(ai_message)

    token_window_used = await token_limiter.record(
        usage["total_tokens"]
    )

    return MeteredLLMResult(
        ai_message=ai_message,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        total_tokens=usage["total_tokens"],
        token_window_used=token_window_used,
        token_gate_waited=waited,
    )


def usage_updates_from_metered_result(
    state: dict[str, Any],
    result: MeteredLLMResult,
) -> dict[str, Any]:
    return {
        "input_tokens": state.get("input_tokens", 0) + result.input_tokens,
        "output_tokens": state.get("output_tokens", 0) + result.output_tokens,
        "total_tokens": state.get("total_tokens", 0) + result.total_tokens,
        "token_window_used": result.token_window_used,
        "token_gate_waited": result.token_gate_waited,
    }
