"""Penpot design builder graph with validator and fixer workflows."""

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

from agent.utils.agents.design_validator import validator_graph
from agent.utils.llm_control import (
    metered_ainvoke,
    shared_rate_limiter,
    usage_updates_from_metered_result,
)

from agent.utils.fixer_prompt import build_fix_design_prompt


# ---------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent

PENPOT_READ_TOOLS_PATH = ROOT_DIR / "utils" / "tool_policies" / "penpot_read_tools.md"
PENPOT_WRITE_TOOLS_PATH = ROOT_DIR / "utils" / "tool_policies" / "penpot_write_tools.md"


# ---------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------

Action = Literal[
    "build",
    "validate_only",
    "build_and_validate",
    "validate_and_fix",
    "build_validate_and_fix",
]


def normalize_action(action: str | None) -> str:
    if not action:
        return "build_and_validate"

    allowed_actions = {
        "build",
        "validate_only",
        "build_and_validate",
        "validate_and_fix",
        "build_validate_and_fix",
    }

    if action not in allowed_actions:
        return "build_and_validate"

    return action


def action_starts_with_validation(action: str) -> bool:
    return action in {
        "validate_only",
        "validate_and_fix",
    }


def action_requires_validation_after_build(action: str) -> bool:
    return action in {
        "build_and_validate",
        "validate_and_fix",
        "build_validate_and_fix",
    }


def action_allows_fixing(action: str) -> bool:
    return action in {
        "validate_and_fix",
        "build_validate_and_fix",
    }


# ---------------------------------------------------------------------
# Builder prompt
# ---------------------------------------------------------------------

BUILDER_SYSTEM_PROMPT = """
Eres un agente conectado a Penpot mediante MCP.

Cuando el usuario pida crear, modificar o inspeccionar diseño:
- Usa las herramientas disponibles.
- Si necesitas entender la API de Penpot, usa high_level_overview.
- Si necesitas detalles técnicos, usa penpot_api_info.
- Para crear o modificar elementos en la página actual, usa execute_code.
- No digas que hiciste un cambio si no ejecutaste una herramienta correctamente.
- No borres elementos existentes salvo que el usuario lo pida explícitamente.

Cuando crees interfaces gráficas:
- Usa nombres semánticos para capas y grupos.
- Evita nombres genéricos como Rectangle 1, Text 2 o Group 3.
- Organiza la interfaz pensando en handoff frontend.
- Usa estructura tipo Atomic Design cuando aplique.
- Usa una escala consistente de espaciado: 4, 8, 12, 16, 24, 32, 48.
- Todo botón debe tener container y label.
- Todo input debe tener label, container y placeholder.

Cuando corrijas un diseño a partir de un reporte de validación:
- Si existe AUTO_FIX_PLAN o auto_fix_plan, aplica únicamente esas acciones.
- Por ahora, las correcciones automáticas seguras son de tipo rename_layer.
- Para rename_layer, renombra solo la capa indicada por id/node_ref y usa exactamente el new_name indicado.
- No apliques manual_fixes automáticamente. Trátalos solo como notas para desarrollo/diseño.
- No cambies posición, tamaño, color, texto visible, layout, componentes ni tokens salvo que el auto_fix_plan lo indique explícitamente.
- Mantén la intención visual original.
- No borres elementos existentes salvo que el reporte lo exija explícitamente.
- Usa execute_code solo cuando necesites modificar Penpot.
- No inventes que corregiste algo si no ejecutaste una herramienta correctamente.
"""


# ---------------------------------------------------------------------
# Markdown policy loaders
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
class BuilderToolPolicy:
    allowed_tools: set[str]
    allowed_keywords: set[str]


def load_builder_tool_policy() -> BuilderToolPolicy:
    """
    El builder puede usar tools de lectura y escritura.

    En penpot_write_tools.md puedes tener:
    - ## deny
    - ## deny_keywords

    Para el validator eso significa bloquear.
    Para el builder eso significa permitir.
    """

    read_content = read_markdown_file(PENPOT_READ_TOOLS_PATH)
    write_content = read_markdown_file(PENPOT_WRITE_TOOLS_PATH)

    read_tools = parse_markdown_list_section(read_content, "allow")
    read_keywords = parse_markdown_list_section(read_content, "allow_keywords")

    write_tools = (
        parse_markdown_list_section(write_content, "allow")
        or parse_markdown_list_section(write_content, "deny")
    )

    write_keywords = (
        parse_markdown_list_section(write_content, "allow_keywords")
        or parse_markdown_list_section(write_content, "deny_keywords")
    )

    return BuilderToolPolicy(
        allowed_tools=read_tools | write_tools,
        allowed_keywords=read_keywords | write_keywords,
    )


# ---------------------------------------------------------------------
# LLM configuration
# ---------------------------------------------------------------------

llm = ChatMistralAI(
    model=os.getenv("MISTRAL_MODEL", "mistral-large-2512"),
    temperature=0,
    max_retries=2,
    rate_limiter=shared_rate_limiter,
)


# ---------------------------------------------------------------------
# State schemas
# ---------------------------------------------------------------------

class Context(TypedDict):
    """Runtime context."""


class InputState(TypedDict, total=False):
    changeme: str
    action: Action
    max_fix_iterations: int


class OutputState(TypedDict, total=False):
    response: str | None

    validation_report: dict[str, Any] | str | None
    passed: bool | None
    score: int | None
    status: str | None

    fix_iterations: int
    max_fix_iterations: int

    input_tokens: int
    output_tokens: int
    total_tokens: int
    token_window_used: int
    token_gate_waited: bool


class OverallState(MessagesState, total=False):
    changeme: NotRequired[str]
    action: NotRequired[str]

    response: NotRequired[str | None]

    validation_report: NotRequired[dict[str, Any] | str | None]
    passed: NotRequired[bool | None]
    score: NotRequired[int | None]
    status: NotRequired[str | None]

    fix_iterations: NotRequired[int]
    max_fix_iterations: NotRequired[int]

    input_tokens: NotRequired[int]
    output_tokens: NotRequired[int]
    total_tokens: NotRequired[int]
    token_window_used: NotRequired[int]
    token_gate_waited: NotRequired[bool]

    skip_validation: NotRequired[bool]


# ---------------------------------------------------------------------
# MCP tools for builder
# ---------------------------------------------------------------------

_penpot_client: MultiServerMCPClient | None = None
_builder_tools: list[Any] | None = None
_builder_tools_by_name: dict[str, Any] = {}
_builder_tool_policy: BuilderToolPolicy | None = None


def normalize_tool_name(tool_name: str) -> str:
    if "__" in tool_name:
        return tool_name.split("__")[-1]

    return tool_name


def is_builder_tool(tool: Any, policy: BuilderToolPolicy) -> bool:
    tool_name = getattr(tool, "name", "")
    normalized_name = normalize_tool_name(tool_name)
    lowered = normalized_name.lower()

    if normalized_name in policy.allowed_tools:
        return True

    if any(keyword in lowered for keyword in policy.allowed_keywords):
        return True

    return False


async def get_builder_tools() -> list[Any]:
    global _penpot_client
    global _builder_tools
    global _builder_tools_by_name
    global _builder_tool_policy

    if _builder_tools is not None:
        return _builder_tools

    penpot_mcp_url = os.getenv("PENPOT_MCP_KEY")

    if not penpot_mcp_url:
        raise RuntimeError(
            "Missing PENPOT_MCP_KEY environment variable. "
            "It should contain the Penpot MCP server URL."
        )

    _builder_tool_policy = load_builder_tool_policy()

    _penpot_client = MultiServerMCPClient(
        {
            "penpot": {
                "transport": "http",
                "url": penpot_mcp_url,
            }
        }
    )

    all_tools = await _penpot_client.get_tools()

    _builder_tools = [
        tool
        for tool in all_tools
        if is_builder_tool(tool, _builder_tool_policy)
    ]

    _builder_tools_by_name = {
        tool.name: tool
        for tool in _builder_tools
    }

    if not _builder_tools:
        available_tools = [
            getattr(tool, "name", "unknown")
            for tool in all_tools
        ]

        raise RuntimeError(
            "No tools were allowed for the builder. "
            f"Available tools: {available_tools}"
        )

    return _builder_tools


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


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content

    return json.dumps(content, ensure_ascii=False, default=str)


def validation_passed(state: OverallState) -> bool:
    return bool(state.get("passed", False))


def has_auto_fix_plan(state: OverallState) -> bool:
    report = state.get("validation_report")

    if not isinstance(report, dict):
        return False

    plan = report.get("auto_fix_plan", [])

    if not isinstance(plan, list):
        return False

    for item in plan:
        if not isinstance(item, dict):
            continue

        if item.get("action") != "rename_layer":
            continue

        if item.get("id") and item.get("new_name"):
            return True

    return False


# ---------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------

async def prepare_input(
    state: OverallState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    action = normalize_action(state.get("action"))
    user_input = state.get("changeme")

    if not user_input:
        if action in {"validate_only", "validate_and_fix"}:
            user_input = "Valida el diseño actual de Penpot para handoff frontend."
        else:
            user_input = "Inspecciona o modifica el diseño actual en Penpot."

    max_fix_iterations = int(state.get("max_fix_iterations", 2) or 2)

    updates: Dict[str, Any] = {
        "action": action,
        "response": None,
        "validation_report": None,
        "passed": None,
        "score": None,
        "status": None,
        "skip_validation": False,
        "fix_iterations": 0,
        "max_fix_iterations": max_fix_iterations,
    }

    # Si la acción empieza validando, no mandamos todavía un mensaje al builder.
    if not action_starts_with_validation(action):
        updates["messages"] = [HumanMessage(content=user_input)]

    return updates


async def llm_call(
    state: OverallState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    """
    Builder LLM call.

    Toda llamada al modelo pasa por metered_ainvoke.
    """

    try:
        tools = await get_builder_tools()
        llm_with_tools = llm.bind_tools(tools)

        messages = state.get("messages", [])

        if not messages:
            error_message = "No hay mensajes para enviar al modelo."

            return {
                "messages": [AIMessage(content=error_message)],
                "response": error_message,
                "skip_validation": True,
            }

        last_message = messages[-1]

        if isinstance(last_message, AIMessage):
            error_message = (
                "No puedo volver a llamar al modelo porque el historial termina "
                "con un mensaje del asistente. Debe agregarse un HumanMessage "
                "o ToolMessage antes de invocar Mistral."
            )

            return {
                "messages": [AIMessage(content=error_message)],
                "response": error_message,
                "skip_validation": True,
            }

        llm_messages = [
            SystemMessage(content=BUILDER_SYSTEM_PROMPT),
            *messages,
        ]

        metered_result = await metered_ainvoke(
            llm_with_tools,
            llm_messages,
            estimated_completion_tokens=1000,
            extra_estimated_tokens=3000,
        )

        ai_message = metered_result.ai_message

        updates: Dict[str, Any] = {
            "messages": [ai_message],
            **usage_updates_from_metered_result(state, metered_result),
        }

        if not getattr(ai_message, "tool_calls", None):
            updates["response"] = content_to_text(ai_message.content)

        return updates

    except HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response else None

        if status_code == 429:
            error_message = (
                "No pude completar la solicitud porque Mistral devolvió un error 429 "
                "por límite de uso o demasiadas solicitudes."
            )
        elif status_code == 400:
            error_message = (
                "No pude completar la solicitud porque Mistral rechazó el orden "
                "o formato de mensajes."
            )
        else:
            error_message = (
                f"No pude completar la solicitud porque Mistral devolvió un error HTTP "
                f"{status_code or 'desconocido'}."
            )

        return {
            "messages": [AIMessage(content=error_message)],
            "response": error_message,
            "skip_validation": True,
        }

    except RequestError as exc:
        error_message = (
            "No pude completar la solicitud porque hubo un problema de conexión "
            f"con Mistral: {exc!r}"
        )

        return {
            "messages": [AIMessage(content=error_message)],
            "response": error_message,
            "skip_validation": True,
        }

    except Exception as exc:
        error_message = (
            "No pude completar la solicitud por un error inesperado durante "
            f"la llamada al modelo: {exc!r}"
        )

        return {
            "messages": [AIMessage(content=error_message)],
            "response": error_message,
            "skip_validation": True,
        }


async def tool_node(
    state: OverallState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    await get_builder_tools()

    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", [])

    result = []

    for tool_call in tool_calls:
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]
        tool_call_id = tool_call["id"]

        tool = _builder_tools_by_name.get(tool_name)

        if tool is None:
            observation = {
                "error": "tool_not_allowed_or_not_found",
                "tool": tool_name,
                "message": "La herramienta solicitada no está permitida para el builder.",
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

    return {"messages": result}


async def run_validator(
    state: OverallState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    """
    Ejecuta el grafo validador read-only.

    El validador también usa metered_ainvoke internamente.
    """

    try:
        validator_prompt = (
            "Valida visualmente la pantalla actual de Penpot para handoff frontend. "
            "Usa la imagen PNG exportada desde Penpot como fuente principal. "
            "Evalúa estructura visual, layout, espaciado, legibilidad, accesibilidad básica "
            "y preparación general para desarrollo frontend. Devuelve JSON válido."
        )

        original_user_request = state.get("changeme")

        if original_user_request:
            validator_prompt += (
                "\n\nSolicitud original del usuario/contexto de diseño:\n"
                f"{original_user_request}"
            )

        validation_result = await validator_graph.ainvoke(
            {"changeme": validator_prompt}
        )

        validator_input_tokens = int(validation_result.get("input_tokens") or 0)
        validator_output_tokens = int(validation_result.get("output_tokens") or 0)
        validator_total_tokens = int(validation_result.get("total_tokens") or 0)

        return {
            "validation_report": validation_result.get("validation_report"),
            "passed": validation_result.get("passed"),
            "score": validation_result.get("score"),
            "status": validation_result.get("status"),

            # Sumamos consumo del subgrafo validador al consumo total del grafo principal.
            "input_tokens": state.get("input_tokens", 0) + validator_input_tokens,
            "output_tokens": state.get("output_tokens", 0) + validator_output_tokens,
            "total_tokens": state.get("total_tokens", 0) + validator_total_tokens,
            "token_window_used": validation_result.get(
                "token_window_used",
                state.get("token_window_used", 0),
            ),
            "token_gate_waited": bool(
                state.get("token_gate_waited", False)
                or validation_result.get("token_gate_waited", False)
            ),
        }

    except Exception as exc:
        report = {
            "passed": False,
            "score": 0,
            "status": "not_ready",
            "summary": "No se pudo ejecutar el validador.",
            "checks": {},
            "issues": [
                {
                    "severity": "critical",
                    "category": "validator",
                    "message": repr(exc),
                    "affected_layers": [],
                    "recommendation": "Revisar logs del validador y configuración MCP.",
                }
            ],
            "required_fixes": [
                "Corregir el error del validador antes de enviar a desarrollo."
            ],
            "suggested_structure": "",
            "developer_notes": [],
            "can_be_sent_to_development": False,
        }

        return {
            "validation_report": report,
            "passed": False,
            "score": 0,
            "status": "not_ready",
        }


async def fix_design(
    state: OverallState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    """
    Fixer node.

    No ejecuta tools directamente.
    Convierte validation_report.auto_fix_plan en una instrucción concreta
    y segura para que el builder aplique únicamente correcciones automáticas.
    """

    validation_report = state.get("validation_report")
    fix_iterations = int(state.get("fix_iterations", 0) or 0)
    max_fix_iterations = int(state.get("max_fix_iterations", 1) or 1)

    if not validation_report:
        error_message = "No hay validation_report disponible para corregir."

        return {
            "messages": [AIMessage(content=error_message)],
            "response": error_message,
            "skip_validation": True,
        }

    if not has_auto_fix_plan(state):
        message = (
            "El validation_report no contiene auto_fix_plan ejecutable. "
            "No se aplicarán correcciones automáticas."
        )

        return {
            "messages": [AIMessage(content=message)],
            "response": message,
            "skip_validation": True,
        }

    fix_prompt = build_fix_design_prompt(
        validation_report,
        fix_iteration=fix_iterations + 1,
        max_fix_iterations=max_fix_iterations,
    )

    return {
        "messages": [HumanMessage(content=fix_prompt)],
        "fix_iterations": fix_iterations + 1,
        "response": None,
        "skip_validation": False,
    }


# ---------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------

def route_after_prepare(
    state: OverallState,
) -> Literal["llm_call", "run_validator"]:
    action = normalize_action(state.get("action"))

    if action_starts_with_validation(action):
        return "run_validator"

    return "llm_call"


def should_continue_after_llm(
    state: OverallState,
) -> Literal["tool_node", "run_validator", "__end__"]:
    if state.get("skip_validation"):
        return END

    messages = state.get("messages", [])

    if not messages:
        return END

    last_message = messages[-1]

    if getattr(last_message, "tool_calls", None):
        return "tool_node"

    action = normalize_action(state.get("action"))

    if action_requires_validation_after_build(action):
        return "run_validator"

    return END


def route_after_validator(
    state: OverallState,
) -> Literal["fix_design", "__end__"]:
    action = normalize_action(state.get("action"))

    if not action_allows_fixing(action):
        return END

    if validation_passed(state):
        return END

    if not has_auto_fix_plan(state):
        return END

    fix_iterations = int(state.get("fix_iterations", 0) or 0)
    max_fix_iterations = int(state.get("max_fix_iterations", 2) or 2)

    if fix_iterations >= max_fix_iterations:
        return END

    return "fix_design"


# ---------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------

agent_builder = StateGraph(
    OverallState,
    context_schema=Context,
    input_schema=InputState,
    output_schema=OutputState,
)

agent_builder.add_node("prepare_input", prepare_input)
agent_builder.add_node("llm_call", llm_call)
agent_builder.add_node("tool_node", tool_node)
agent_builder.add_node("run_validator", run_validator)
agent_builder.add_node("fix_design", fix_design)

agent_builder.add_edge(START, "prepare_input")

agent_builder.add_conditional_edges(
    "prepare_input",
    route_after_prepare,
    ["llm_call", "run_validator"],
)

agent_builder.add_conditional_edges(
    "llm_call",
    should_continue_after_llm,
    ["tool_node", "run_validator", END],
)

agent_builder.add_edge("tool_node", "llm_call")

agent_builder.add_conditional_edges(
    "run_validator",
    route_after_validator,
    ["fix_design", END],
)

agent_builder.add_edge("fix_design", "llm_call")

graph = agent_builder.compile(name="Penpot Design Workflow")
