"""Read-only visual Design Validator Agent for Penpot MCP.

This validator is deterministic:
1. It exports the current Penpot selection as PNG via read-only `export_shape`.
2. It sends that PNG as a base64 data URL to a Mistral vision model.
3. It returns the same validation_report / passed / score / status contract used by
   the main graph.

Important architecture rule preserved:
- The Mistral vision call still goes through `metered_ainvoke(...)`.
- The official Mistral SDK is wrapped by `MistralVisionRunnable` so token metering
  and the shared request limiter are still used.
"""

from __future__ import annotations

import json
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from httpx import RequestError
from typing_extensions import NotRequired, TypedDict

from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from agent.utils.llm_control import (
    metered_ainvoke,
    usage_updates_from_metered_result,
)
from agent.utils.mistral_vision_runnable import MistralVisionRunnable
from agent.utils.penpot_image_export import (
    assert_valid_png_base64,
    extract_png_base64_from_export_shape,
    png_base64_to_data_url,
    save_debug_png_export,
)


# ---------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------

AGENT_ROOT_DIR = Path(__file__).resolve().parents[2]

PENPOT_READ_TOOLS_PATH = AGENT_ROOT_DIR / "utils" / "tool_policies" / "penpot_read_tools.md"
PENPOT_WRITE_TOOLS_PATH = AGENT_ROOT_DIR / "utils" / "tool_policies" / "penpot_write_tools.md"


# ---------------------------------------------------------------------
# Visual validator prompt
# ---------------------------------------------------------------------

VISUAL_VALIDATOR_PROMPT = """
Eres un agente validador visual de diseño UI conectado a Penpot mediante MCP.

Vas a recibir una imagen PNG exportada desde Penpot. Tu tarea es evaluar si la pantalla está lista para handoff frontend.

Debes responder únicamente con JSON válido. No uses Markdown. No agregues explicación fuera del JSON.

Evalúa de forma visual:
- existencia de una pantalla clara
- jerarquía visual
- layout y espaciado
- legibilidad de textos
- accesibilidad básica
- consistencia visual
- preparación general para desarrollo frontend

Limitaciones:
- Solo puedes evaluar lo visible en la imagen y el contexto textual recibido.
- Si no puedes verificar nombres de capas, tokens o componentes desde la imagen, marca esos checks como "unknown" o "warning".
- No inventes detalles internos que no sean visibles.

Criterio simple para passed:
- passed=true si score >= 70, la pantalla parece entendible para desarrollo y no hay problemas graves evidentes.
- passed=false si la información es insuficiente, la pantalla no es clara, hay problemas graves o score < 70.

Valores permitidos para checks.*.status:
pass, warning, fail, unknown

Valores permitidos para issues[].severity:
low, medium, high, critical

Valores permitidos para status:
ready, needs_minor_fixes, needs_major_fixes, not_ready

Reglas de status:
- ready: score >= 85 y sin problemas graves
- needs_minor_fixes: score entre 70 y 84
- needs_major_fixes: score entre 50 y 69
- not_ready: score menor a 50 o información insuficiente

Devuelve exactamente esta estructura JSON:

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
      "notes": []
    },
    "componentization": {
      "status": "unknown",
      "score": 0,
      "notes": []
    },
    "layout_spacing": {
      "status": "unknown",
      "score": 0,
      "notes": []
    },
    "text_legibility": {
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
""".strip()


# ---------------------------------------------------------------------
# Markdown loaders and tool policy
# ---------------------------------------------------------------------

def read_markdown_file(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Markdown file not found: {path}")

    return path.read_text(encoding="utf-8")


def parse_markdown_list_section(content: str, section_name: str) -> set[str]:
    lines = content.splitlines()
    inside_section = False
    values: set[str] = set()

    expected_heading = f"## {section_name}".strip().lower()

    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower()

        if lowered.startswith("## "):
            inside_section = lowered == expected_heading
            continue

        if inside_section and stripped.startswith("- "):
            value = stripped.removeprefix("- ").strip()
            if value:
                values.add(value)

    return values


@dataclass(frozen=True)
class ValidatorToolPolicy:
    allow_tools: set[str]
    deny_tools: set[str]
    allow_keywords: set[str]
    deny_keywords: set[str]


def load_validator_tool_policy() -> ValidatorToolPolicy:
    read_content = read_markdown_file(PENPOT_READ_TOOLS_PATH)
    write_content = read_markdown_file(PENPOT_WRITE_TOOLS_PATH)

    allow_tools = parse_markdown_list_section(read_content, "allow")
    allow_keywords = parse_markdown_list_section(read_content, "allow_keywords")

    deny_tools = (
        parse_markdown_list_section(write_content, "deny")
        or parse_markdown_list_section(write_content, "allow")
    )

    deny_keywords = (
        parse_markdown_list_section(write_content, "deny_keywords")
        or parse_markdown_list_section(write_content, "allow_keywords")
    )

    return ValidatorToolPolicy(
        allow_tools=allow_tools,
        deny_tools=deny_tools,
        allow_keywords=allow_keywords,
        deny_keywords=deny_keywords,
    )


# ---------------------------------------------------------------------
# State schemas
# ---------------------------------------------------------------------

class Context(TypedDict):
    """Runtime context."""


class ValidatorInputState(TypedDict):
    changeme: str


class ValidatorOutputState(TypedDict, total=False):
    validation_report: dict[str, Any] | str | None
    passed: bool | None
    score: int | None
    status: str | None

    input_tokens: int
    output_tokens: int
    total_tokens: int
    token_window_used: int
    token_gate_waited: bool


class ValidatorState(TypedDict, total=False):
    changeme: NotRequired[str]

    validation_report: NotRequired[dict[str, Any] | str | None]
    passed: NotRequired[bool | None]
    score: NotRequired[int | None]
    status: NotRequired[str | None]

    input_tokens: NotRequired[int]
    output_tokens: NotRequired[int]
    total_tokens: NotRequired[int]
    token_window_used: NotRequired[int]
    token_gate_waited: NotRequired[bool]


# ---------------------------------------------------------------------
# MCP tools: read-only
# ---------------------------------------------------------------------

_penpot_client: MultiServerMCPClient | None = None
_validator_tools: list[Any] | None = None
_validator_tools_by_name: dict[str, Any] = {}
_validator_tool_policy: ValidatorToolPolicy | None = None


def normalize_tool_name(tool_name: str) -> str:
    if "__" in tool_name:
        return tool_name.split("__")[-1]

    return tool_name


def is_read_only_tool(tool: Any, policy: ValidatorToolPolicy) -> bool:
    tool_name = getattr(tool, "name", "")
    normalized_name = normalize_tool_name(tool_name)
    lowered = normalized_name.lower()

    if normalized_name in policy.deny_tools:
        return False

    if any(keyword in lowered for keyword in policy.deny_keywords):
        return False

    if normalized_name in policy.allow_tools:
        return True

    if any(keyword in lowered for keyword in policy.allow_keywords):
        return True

    return False


async def get_validator_tools() -> list[Any]:
    global _penpot_client
    global _validator_tools
    global _validator_tools_by_name
    global _validator_tool_policy

    if _validator_tools is not None:
        return _validator_tools

    penpot_mcp_url = os.getenv("PENPOT_MCP_KEY")

    if not penpot_mcp_url:
        raise RuntimeError(
            "Missing PENPOT_MCP_KEY environment variable. "
            "It should contain the Penpot MCP server URL."
        )

    _validator_tool_policy = load_validator_tool_policy()

    _penpot_client = MultiServerMCPClient(
        {
            "penpot": {
                "transport": "http",
                "url": penpot_mcp_url,
            }
        }
    )

    all_tools = await _penpot_client.get_tools()

    _validator_tools = [
        tool
        for tool in all_tools
        if is_read_only_tool(tool, _validator_tool_policy)
    ]

    _validator_tools_by_name = {}
    for tool in _validator_tools:
        tool_name = getattr(tool, "name", "")
        normalized_name = normalize_tool_name(tool_name)
        _validator_tools_by_name[tool_name] = tool
        _validator_tools_by_name[normalized_name] = tool

    if "export_shape" not in _validator_tools_by_name:
        available_tools = [getattr(tool, "name", "unknown") for tool in all_tools]
        allowed_tools = [getattr(tool, "name", "unknown") for tool in _validator_tools]

        raise RuntimeError(
            "The visual validator requires the read-only `export_shape` tool. "
            f"Available tools: {available_tools}. Allowed tools: {allowed_tools}."
        )

    return _validator_tools


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def parse_json_report(content: Any) -> dict[str, Any] | str:
    if isinstance(content, dict):
        return content

    if not isinstance(content, str):
        return str(content)

    cleaned = content.strip()

    if cleaned.startswith("```json"):
        cleaned = cleaned.removeprefix("```json").strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").strip()

    if cleaned.endswith("```"):
        cleaned = cleaned.removesuffix("```").strip()

    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else content
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            return parsed if isinstance(parsed, dict) else content
        except json.JSONDecodeError:
            pass

    return content


def extract_output_from_report(report: dict[str, Any] | str) -> Dict[str, Any]:
    if isinstance(report, dict):
        return {
            "validation_report": report,
            "passed": bool(report.get("passed", False)),
            "score": int(report.get("score", 0) or 0),
            "status": report.get("status", "not_ready"),
        }

    return {
        "validation_report": report,
        "passed": False,
        "score": 0,
        "status": "not_ready",
    }


def make_error_report(
    summary: str,
    category: str,
    message: str,
    recommendation: str,
) -> dict[str, Any]:
    return {
        "passed": False,
        "score": 0,
        "status": "not_ready",
        "summary": summary,
        "checks": {
            "screen_structure": {
                "status": "unknown",
                "score": 0,
                "notes": [],
            },
            "layer_naming": {
                "status": "unknown",
                "score": 0,
                "notes": [],
            },
            "componentization": {
                "status": "unknown",
                "score": 0,
                "notes": [],
            },
            "layout_spacing": {
                "status": "unknown",
                "score": 0,
                "notes": [],
            },
            "text_legibility": {
                "status": "unknown",
                "score": 0,
                "notes": [],
            },
            "accessibility": {
                "status": "unknown",
                "score": 0,
                "notes": [],
            },
            "frontend_handoff": {
                "status": "unknown",
                "score": 0,
                "notes": [],
            },
        },
        "issues": [
            {
                "severity": "critical",
                "category": category,
                "message": message,
                "affected_layers": [],
                "recommendation": recommendation,
            }
        ],
        "required_fixes": [recommendation],
        "suggested_structure": "",
        "developer_notes": [],
        "can_be_sent_to_development": False,
    }


def build_visual_prompt(context: str | None) -> str:
    prompt = VISUAL_VALIDATOR_PROMPT

    if context:
        prompt += (
            "\n\nContexto adicional del workflow o solicitud original del usuario:\n"
            f"{context}"
        )

    return prompt


def make_mistral_error_report(exc: Exception) -> dict[str, Any]:
    status_code = getattr(exc, "status_code", None)

    if status_code == 429:
        return make_error_report(
            summary="Mistral devolvió 429 por rate limit durante validación visual.",
            category="mistral_rate_limit",
            message=str(exc),
            recommendation=(
                "Reducir MISTRAL_REQUESTS_PER_SECOND, reducir concurrencia, "
                "ajustar MISTRAL_TOKENS_PER_MINUTE o esperar antes de reintentar."
            ),
        )

    if status_code == 400:
        return make_error_report(
            summary="Mistral rechazó el formato de la solicitud visual.",
            category="mistral_bad_request",
            message=str(exc),
            recommendation=(
                "Revisar formato image_url, modelo visual, tamaño de imagen y prompt JSON."
            ),
        )

    if isinstance(exc, RequestError):
        return make_error_report(
            summary="Error de conexión con Mistral durante validación visual.",
            category="mistral_network",
            message=repr(exc),
            recommendation="Revisar red, timeout o disponibilidad del proveedor.",
        )

    return make_error_report(
        summary="Error inesperado durante validación visual con Mistral.",
        category="mistral_vision",
        message=repr(exc),
        recommendation="Revisar logs del validador visual.",
    )


# ---------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------

async def validator_prepare_input(
    state: ValidatorState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    user_input = state.get("changeme")

    if not user_input:
        user_input = (
            "Valida visualmente la pantalla actual de Penpot y genera un reporte "
            "de handoff frontend en JSON válido."
        )

    return {
        "changeme": user_input,
        "validation_report": None,
        "passed": None,
        "score": None,
        "status": None,
    }


async def validator_visual_call(
    state: ValidatorState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    """
    Export current Penpot selection as PNG and validate it with Mistral Vision.

    All LLM usage goes through `metered_ainvoke(...)` using MistralVisionRunnable.
    """
    try:
        await get_validator_tools()
        export_shape = _validator_tools_by_name["export_shape"]

        shape_id = os.getenv("PENPOT_VALIDATOR_SHAPE_ID", "selection")

        export_result = await export_shape.ainvoke(
            {
                "shapeId": shape_id,
                "format": "png",
                "mode": "shape",
            }
        )

        image_b64 = extract_png_base64_from_export_shape(export_result)

        if not image_b64:
            report = make_error_report(
                summary="No se pudo exportar PNG desde Penpot.",
                category="penpot_export",
                message=(
                    "export_shape no devolvió un bloque image/base64. "
                    f"shapeId usado: {shape_id!r}."
                ),
                recommendation=(
                    "Selecciona un board, grupo o pantalla en Penpot, o define "
                    "PENPOT_VALIDATOR_SHAPE_ID con un shapeId exportable."
                ),
            )
            return extract_output_from_report(report)

        assert_valid_png_base64(image_b64)
        if os.getenv("PENPOT_VALIDATOR_DEBUG_EXPORT", "0") == "1":
            await asyncio.to_thread(
                save_debug_png_export,
                image_b64,
                output_dir=os.getenv("PENPOT_DEBUG_OUT", "/tmp/penpot_debug"),
                stem="validator_export",
            )


        image_data_url = png_base64_to_data_url(image_b64)

        vision_runnable = MistralVisionRunnable(
            image_data_url=image_data_url,
            model=os.getenv("MISTRAL_VISION_MODEL", "mistral-large-2512"),
            temperature=0,
            response_format={"type": "json_object"},
        )

        visual_prompt = build_visual_prompt(state.get("changeme"))

        try:
            metered_result = await metered_ainvoke(
                vision_runnable,
                [HumanMessage(content=visual_prompt)],
                estimated_completion_tokens=int(
                    os.getenv("MISTRAL_VISION_ESTIMATED_COMPLETION_TOKENS", "1500")
                ),
                extra_estimated_tokens=int(
                    os.getenv("MISTRAL_VISION_EXTRA_ESTIMATED_TOKENS", "3000")
                ),
            )
        except Exception as exc:
            report = make_mistral_error_report(exc)
            return extract_output_from_report(report)

        report = parse_json_report(metered_result.ai_message.content)

        if not isinstance(report, dict):
            report = make_error_report(
                summary="Mistral Vision no devolvió JSON parseable.",
                category="mistral_json",
                message=str(report),
                recommendation="Revisar prompt visual y response_format json_object.",
            )

        return {
            **extract_output_from_report(report),
            **usage_updates_from_metered_result(state, metered_result),
        }

    except Exception as exc:
        report = make_error_report(
            summary="Error inesperado durante la validación visual.",
            category="validator_runtime",
            message=repr(exc),
            recommendation="Revisar logs del validador visual, MCP y configuración de Mistral.",
        )

        return extract_output_from_report(report)


# ---------------------------------------------------------------------
# Build validator graph
# ---------------------------------------------------------------------

validator_builder = StateGraph(
    ValidatorState,
    context_schema=Context,
    input_schema=ValidatorInputState,
    output_schema=ValidatorOutputState,
)

validator_builder.add_node("validator_prepare_input", validator_prepare_input)
validator_builder.add_node("validator_visual_call", validator_visual_call)

validator_builder.add_edge(START, "validator_prepare_input")
validator_builder.add_edge("validator_prepare_input", "validator_visual_call")
validator_builder.add_edge("validator_visual_call", END)

validator_graph = validator_builder.compile(name="Design Visual Validator Graph")
