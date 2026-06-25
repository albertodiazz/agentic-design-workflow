"""Read-only Design Validator Agent for Penpot MCP."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Literal

from httpx import HTTPStatusError, RequestError
from typing_extensions import NotRequired, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mistralai import ChatMistralAI

from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.runtime import Runtime

from agent.utils.llm_control import (
    metered_ainvoke,
    shared_rate_limiter,
    usage_updates_from_metered_result,
)


# ---------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------

AGENT_ROOT_DIR = Path(__file__).resolve().parents[2]

VALIDATOR_SKILL_PATH = AGENT_ROOT_DIR / "utils" / "skills" / "design_validator.md"
PENPOT_READ_TOOLS_PATH = AGENT_ROOT_DIR / "utils" / "tool_policies" / "penpot_read_tools.md"
PENPOT_WRITE_TOOLS_PATH = AGENT_ROOT_DIR / "utils" / "tool_policies" / "penpot_write_tools.md"


# ---------------------------------------------------------------------
# Markdown loaders
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


_validator_skill_prompt: str | None = None


def get_validator_skill_prompt() -> str:
    global _validator_skill_prompt

    if _validator_skill_prompt is None:
        _validator_skill_prompt = read_markdown_file(VALIDATOR_SKILL_PATH)

    return _validator_skill_prompt


# ---------------------------------------------------------------------
# LLM config
# ---------------------------------------------------------------------

validator_llm = ChatMistralAI(
    model=os.getenv("MISTRAL_MODEL", "mistral-large-latest"),
    temperature=0,
    max_retries=2,
    rate_limiter=shared_rate_limiter,
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


class ValidatorState(MessagesState, total=False):
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

    tool_iterations: NotRequired[int]


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

    _validator_tools_by_name = {
        tool.name: tool
        for tool in _validator_tools
    }

    if not _validator_tools:
        available_tools = [
            getattr(tool, "name", "unknown")
            for tool in all_tools
        ]

        raise RuntimeError(
            "No read-only tools were found for the validator. "
            f"Available tools: {available_tools}"
        )

    return _validator_tools


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def stringify_tool_result(observation: Any) -> str:
    if isinstance(observation, str):
        return observation

    return json.dumps(
        observation,
        ensure_ascii=False,
        default=str,
    )


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
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return content


def extract_output_from_report(
    report: dict[str, Any] | str,
) -> Dict[str, Any]:
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
        "checks": {},
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
            "Valida la pantalla actual de Penpot y genera un reporte de handoff "
            "frontend en JSON válido."
        )

    return {
        "messages": [HumanMessage(content=user_input)],
        "validation_report": None,
        "passed": None,
        "score": None,
        "status": None,
        "tool_iterations": 0,
    }


async def validator_llm_call(
    state: ValidatorState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    """
    Validator LLM call.

    Toda llamada al modelo pasa por metered_ainvoke.
    """

    try:
        tools = await get_validator_tools()
        llm_with_tools = validator_llm.bind_tools(tools)

        messages = state.get("messages", [])

        if not messages:
            report = make_error_report(
                summary="No hay mensajes para validar.",
                category="runtime",
                message="El estado del validador no contiene mensajes.",
                recommendation="Invocar el validador con un input válido.",
            )

            return {
                "messages": [AIMessage(content=json.dumps(report, ensure_ascii=False))],
                **extract_output_from_report(report),
            }

        last_message = messages[-1]

        if isinstance(last_message, AIMessage):
            report = parse_json_report(last_message.content)
            return extract_output_from_report(report)

        validator_skill_prompt = get_validator_skill_prompt()

        llm_messages = [
            SystemMessage(content=validator_skill_prompt),
            *messages,
        ]

        metered_result = await metered_ainvoke(
            llm_with_tools,
            llm_messages,
            estimated_completion_tokens=1500,
            extra_estimated_tokens=2000,
        )

        ai_message = metered_result.ai_message

        updates: Dict[str, Any] = {
            "messages": [ai_message],
            **usage_updates_from_metered_result(state, metered_result),
        }

        if not getattr(ai_message, "tool_calls", None):
            report = parse_json_report(ai_message.content)
            updates.update(extract_output_from_report(report))

        return updates

    except HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response else None

        report = make_error_report(
            summary=f"Error HTTP al llamar al modelo validador: {status_code}",
            category="llm_call",
            message=str(exc),
            recommendation="Revisar límites, orden de mensajes o configuración del modelo.",
        )

        return {
            "messages": [AIMessage(content=json.dumps(report, ensure_ascii=False))],
            **extract_output_from_report(report),
        }

    except RequestError as exc:
        report = make_error_report(
            summary="Error de conexión al llamar al modelo validador.",
            category="network",
            message=str(exc),
            recommendation="Revisar conexión con el proveedor del modelo.",
        )

        return {
            "messages": [AIMessage(content=json.dumps(report, ensure_ascii=False))],
            **extract_output_from_report(report),
        }

    except Exception as exc:
        report = make_error_report(
            summary="Error inesperado durante la validación.",
            category="runtime",
            message=repr(exc),
            recommendation="Revisar logs del validador.",
        )

        return {
            "messages": [AIMessage(content=json.dumps(report, ensure_ascii=False))],
            **extract_output_from_report(report),
        }


async def validator_tool_node(
    state: ValidatorState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    await get_validator_tools()

    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", [])

    result = []

    for tool_call in tool_calls:
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]
        tool_call_id = tool_call["id"]

        tool = _validator_tools_by_name.get(tool_name)

        if tool is None:
            observation = {
                "error": "tool_not_allowed_or_not_found",
                "tool": tool_name,
                "message": (
                    "La herramienta solicitada no está permitida para el validador. "
                    "Este agente solo puede usar herramientas de lectura."
                ),
            }
        else:
            try:
                observation = await tool.ainvoke(tool_args)
            except Exception as exc:
                observation = {
                    "error": "tool_execution_error",
                    "tool": tool_name,
                    "message": repr(exc),
                }

        result.append(
            ToolMessage(
                content=stringify_tool_result(observation),
                tool_call_id=tool_call_id,
            )
        )

    previous_iterations = state.get("tool_iterations", 0)

    return {
        "messages": result,
        "tool_iterations": previous_iterations + 1,
    }


async def validator_force_finish(
    state: ValidatorState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    report = make_error_report(
        summary="El validador alcanzó el límite máximo de iteraciones de tools.",
        category="validator_loop",
        message=(
            "El modelo siguió solicitando herramientas y no generó un reporte final "
            "dentro del límite configurado."
        ),
        recommendation=(
            "Reducir el alcance de validación o revisar si las tools de lectura "
            "devuelven suficiente información."
        ),
    )

    return {
        "messages": [AIMessage(content=json.dumps(report, ensure_ascii=False))],
        **extract_output_from_report(report),
    }


# ---------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------

MAX_VALIDATOR_TOOL_ITERATIONS = int(
    os.getenv("MAX_VALIDATOR_TOOL_ITERATIONS", "4")
)


def validator_should_continue(
    state: ValidatorState,
) -> Literal["validator_tool_node", "validator_force_finish", "__end__"]:
    messages = state.get("messages", [])

    if not messages:
        return END

    last_message = messages[-1]

    if getattr(last_message, "tool_calls", None):
        tool_iterations = state.get("tool_iterations", 0)

        if tool_iterations >= MAX_VALIDATOR_TOOL_ITERATIONS:
            return "validator_force_finish"

        return "validator_tool_node"

    return END


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
validator_builder.add_node("validator_llm_call", validator_llm_call)
validator_builder.add_node("validator_tool_node", validator_tool_node)
validator_builder.add_node("validator_force_finish", validator_force_finish)

validator_builder.add_edge(START, "validator_prepare_input")
validator_builder.add_edge("validator_prepare_input", "validator_llm_call")

validator_builder.add_conditional_edges(
    "validator_llm_call",
    validator_should_continue,
    ["validator_tool_node", "validator_force_finish", END],
)

validator_builder.add_edge("validator_tool_node", "validator_llm_call")
validator_builder.add_edge("validator_force_finish", END)

validator_graph = validator_builder.compile(name="Design Validator Graph")
