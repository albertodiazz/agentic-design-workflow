"""Mistral Vision client for Penpot UI validation."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

try:
    from mistralai import Mistral
except ImportError:  # Older SDK import path used in Mistral docs.
    from mistralai.client import Mistral  # type: ignore

from agent.utils.llm_control import token_limiter


VISION_VALIDATOR_PROMPT = """
Eres un validador visual de diseño UI conectado a Penpot.

Vas a recibir una imagen PNG de una pantalla UI. Evalúa si está lista para handoff frontend.

Debes devolver únicamente JSON válido. No uses Markdown. No expliques fuera del JSON.

Evalúa:
- existencia de pantalla clara
- jerarquía visual
- layout y espaciado
- legibilidad de textos
- accesibilidad básica
- consistencia visual
- preparación general para desarrollo frontend

Usa exactamente esta estructura JSON:

{
  "passed": false,
  "score": 0,
  "status": "not_ready",
  "summary": "",
  "checks": {
    "screen_structure": {
      "status": "unknown",
      "score": 0,
      "notes": []
    },
    "layer_naming": {
      "status": "unknown",
      "score": 0,
      "notes": ["No evaluable visualmente si solo se recibe PNG."]
    },
    "componentization": {
      "status": "unknown",
      "score": 0,
      "notes": ["No evaluable visualmente si solo se recibe PNG."]
    },
    "layout_spacing": {
      "status": "unknown",
      "score": 0,
      "notes": []
    },
    "accessibility": {
      "status": "unknown",
      "score": 0,
      "notes": []
    },
    "frontend_handoff": {
      "status": "unknown",
      "score": 0,
      "notes": []
    }
  },
  "issues": [],
  "required_fixes": [],
  "suggested_structure": "",
  "developer_notes": [],
  "can_be_sent_to_development": false
}

Valores permitidos para checks.*.status:
pass, warning, fail, unknown

Valores permitidos para status:
ready, needs_minor_fixes, needs_major_fixes, not_ready

Valores permitidos para issues.*.severity:
low, medium, high, critical

Criterio:
- passed=true solo si score >= 70 y no hay problemas critical/high evidentes.
- can_be_sent_to_development=true solo si la pantalla es entendible para frontend.
- Si un punto no puede evaluarse visualmente desde PNG, usa status="unknown" en ese check.
- Cuando detectes un problema visual, incluye una corrección concreta en required_fixes.
""".strip()


@dataclass(frozen=True)
class MistralVisionResult:
    report: dict[str, Any]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    token_window_used: int
    token_gate_waited: bool


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value

    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}

    return {}


def _get_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    usage_dict = _as_dict(usage)

    input_tokens = int(
        usage_dict.get("prompt_tokens")
        or usage_dict.get("input_tokens")
        or 0
    )
    output_tokens = int(
        usage_dict.get("completion_tokens")
        or usage_dict.get("output_tokens")
        or 0
    )
    total_tokens = int(
        usage_dict.get("total_tokens")
        or input_tokens + output_tokens
        or 0
    )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", item)))
            else:
                parts.append(str(getattr(item, "text", item)))
        return "\n".join(parts)

    return str(content)


def parse_json_report(text: str) -> dict[str, Any]:
    cleaned = text.strip()

    if cleaned.startswith("```json"):
        cleaned = cleaned.removeprefix("```json").strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").strip()

    if cleaned.endswith("```"):
        cleaned = cleaned.removesuffix("```").strip()

    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start >= 0 and end > start:
        try:
            value = json.loads(cleaned[start : end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            pass

    return {
        "passed": False,
        "score": 0,
        "status": "not_ready",
        "summary": "No se pudo parsear JSON desde la respuesta de Mistral Vision.",
        "checks": {},
        "issues": [
            {
                "severity": "critical",
                "category": "vision_validator",
                "message": "Respuesta de Mistral Vision no parseable como JSON.",
                "affected_layers": [],
                "recommendation": "Revisar prompt visual y response_format.",
            }
        ],
        "required_fixes": ["Revisar prompt visual y response_format."],
        "suggested_structure": "",
        "developer_notes": [text[:4000]],
        "can_be_sent_to_development": False,
    }


def normalize_report(report: dict[str, Any]) -> dict[str, Any]:
    allowed_global_status = {
        "ready",
        "needs_minor_fixes",
        "needs_major_fixes",
        "not_ready",
    }
    allowed_check_status = {"pass", "warning", "fail", "unknown"}
    allowed_severity = {"low", "medium", "high", "critical"}

    defaults: dict[str, Any] = {
        "passed": False,
        "score": 0,
        "status": "not_ready",
        "summary": "",
        "checks": {
            "screen_structure": {"status": "unknown", "score": 0, "notes": []},
            "layer_naming": {"status": "unknown", "score": 0, "notes": []},
            "componentization": {"status": "unknown", "score": 0, "notes": []},
            "layout_spacing": {"status": "unknown", "score": 0, "notes": []},
            "accessibility": {"status": "unknown", "score": 0, "notes": []},
            "frontend_handoff": {"status": "unknown", "score": 0, "notes": []},
        },
        "issues": [],
        "required_fixes": [],
        "suggested_structure": "",
        "developer_notes": [],
        "can_be_sent_to_development": False,
    }

    merged = {**defaults, **report}

    try:
        score = int(merged.get("score", 0) or 0)
    except Exception:
        score = 0
    merged["score"] = max(0, min(score, 100))

    if merged.get("status") not in allowed_global_status:
        if merged["score"] >= 85:
            merged["status"] = "ready"
        elif merged["score"] >= 70:
            merged["status"] = "needs_minor_fixes"
        elif merged["score"] >= 50:
            merged["status"] = "needs_major_fixes"
        else:
            merged["status"] = "not_ready"

    checks = merged.get("checks")
    if not isinstance(checks, dict):
        checks = {}

    normalized_checks: dict[str, Any] = {}
    for key, default_value in defaults["checks"].items():
        value = checks.get(key, default_value)
        if not isinstance(value, dict):
            value = default_value

        check_status = value.get("status", "unknown")
        if check_status not in allowed_check_status:
            check_status = "unknown"

        try:
            check_score = int(value.get("score", 0) or 0)
        except Exception:
            check_score = 0

        notes = value.get("notes", [])
        if not isinstance(notes, list):
            notes = [str(notes)]

        normalized_checks[key] = {
            "status": check_status,
            "score": max(0, min(check_score, 100)),
            "notes": [str(note) for note in notes],
        }

    merged["checks"] = normalized_checks

    raw_issues = merged.get("issues", [])
    issues = raw_issues if isinstance(raw_issues, list) else []
    normalized_issues: list[dict[str, Any]] = []

    for issue in issues:
        if not isinstance(issue, dict):
            continue

        severity = issue.get("severity", "medium")
        if severity not in allowed_severity:
            severity = "medium"

        normalized_issues.append({**issue, "severity": severity})

    merged["issues"] = normalized_issues

    for list_key in ["required_fixes", "developer_notes"]:
        value = merged.get(list_key, [])
        if not isinstance(value, list):
            value = [str(value)]
        merged[list_key] = [str(item) for item in value]

    blocking = [
        issue for issue in normalized_issues
        if issue.get("severity") in {"critical", "high"}
    ]

    merged["passed"] = bool(merged.get("passed", False))
    merged["can_be_sent_to_development"] = bool(
        merged.get("can_be_sent_to_development", False)
    )

    if merged["score"] < 70 or blocking:
        merged["passed"] = False

    if not merged["passed"]:
        merged["can_be_sent_to_development"] = False

    return merged


async def validate_design_image_with_mistral(
    *,
    image_data_url: str,
    extra_context: str | None = None,
) -> MistralVisionResult:
    api_key = os.environ["MISTRAL_API_KEY"]
    model = os.getenv("MISTRAL_VISION_MODEL", "mistral-large-2512")

    prompt = VISION_VALIDATOR_PROMPT

    if extra_context:
        prompt += f"\n\nContexto adicional del usuario:\n{extra_context}"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": image_data_url},
            ],
        }
    ]

    estimated_tokens = int(os.getenv("MISTRAL_VISION_ESTIMATED_TOKENS", "3000"))
    _, waited = await token_limiter.wait_if_needed(estimated_next_tokens=estimated_tokens)

    client = Mistral(api_key=api_key)

    response = await asyncio.to_thread(
        client.chat.complete,
        model=model,
        messages=messages,
        temperature=0,
        response_format={"type": "json_object"},
    )

    usage = _get_usage(response)
    token_window_used = await token_limiter.record(usage["total_tokens"])

    content = response.choices[0].message.content
    report = normalize_report(parse_json_report(_content_to_text(content)))

    return MistralVisionResult(
        report=report,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        total_tokens=usage["total_tokens"],
        token_window_used=token_window_used,
        token_gate_waited=waited,
    )
