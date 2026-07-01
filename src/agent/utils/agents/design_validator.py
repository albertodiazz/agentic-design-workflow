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

import asyncio
import json
import os
from collections import Counter
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
    to_plain,
)
from agent.utils.resource_loader import (
    load_js,
    load_json_resource,
    render_skill,
)


# ---------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------

AGENT_ROOT_DIR = Path(__file__).resolve().parents[2]

PENPOT_READ_TOOLS_PATH = AGENT_ROOT_DIR / "utils" / "tool_policies" / "penpot_read_tools.md"
PENPOT_WRITE_TOOLS_PATH = AGENT_ROOT_DIR / "utils" / "tool_policies" / "penpot_write_tools.md"


# ---------------------------------------------------------------------
# Prompt resources live in utils/skills and utils/json
# ---------------------------------------------------------------------

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

    design_context: dict[str, Any]


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

    design_context: NotRequired[dict[str, Any]]


# ---------------------------------------------------------------------
# MCP tools: read-only
# ---------------------------------------------------------------------

_penpot_client: MultiServerMCPClient | None = None
_validator_tools: list[Any] | None = None
_validator_tools_by_name: dict[str, Any] = {}
_internal_tools_by_name: dict[str, Any] = {}
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
    global _internal_tools_by_name
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

    # Internal tools are NOT exposed to the model.
    # Python uses execute_code only with a fixed read-only script.
    _internal_tools_by_name = {}
    for tool in all_tools:
        tool_name = getattr(tool, "name", "")
        normalized_name = normalize_tool_name(tool_name)

        if normalized_name == "execute_code":
            _internal_tools_by_name[tool_name] = tool
            _internal_tools_by_name[normalized_name] = tool

    # Read-only validator tools. These remain safe to expose to validator logic.
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
        "manual_fixes": [],
        "auto_fix_plan": [],
        "can_be_sent_to_development": False,
    }


MAX_CONTEXT_CHARS = int(os.getenv("PENPOT_VALIDATOR_CONTEXT_CHARS", "9000"))
MAX_CONTEXT_NODES = int(os.getenv("PENPOT_VALIDATOR_MAX_CONTEXT_NODES", "80"))


# Penpot read-structure JavaScript lives in utils/js/penpot_read_structure.js



def extract_text_from_tool_result(result: Any) -> str:
    plain = to_plain(result)

    if isinstance(plain, str):
        return plain

    if isinstance(plain, dict):
        for key in ["result", "text", "output", "content"]:
            value = plain.get(key)

            if isinstance(value, str):
                return value

            if isinstance(value, list):
                parts: list[str] = []
                for item in value:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str):
                            parts.append(text)
                    elif isinstance(item, str):
                        parts.append(item)

                if parts:
                    return "\n".join(parts)

        return json.dumps(plain, ensure_ascii=False, default=str)

    if isinstance(plain, list):
        parts: list[str] = []

        for item in plain:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
                elif item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)

        if parts:
            return "\n".join(parts)

        return json.dumps(plain, ensure_ascii=False, default=str)

    return str(plain)


def parse_json_object_from_text(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        inner = parsed.get("result")
        if isinstance(inner, str):
            inner_parsed = parse_json_object_from_text(inner)
            if isinstance(inner_parsed, dict):
                return inner_parsed

        return parsed

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None

        if isinstance(parsed, dict):
            inner = parsed.get("result")
            if isinstance(inner, str):
                inner_parsed = parse_json_object_from_text(inner)
                if isinstance(inner_parsed, dict):
                    return inner_parsed

            return parsed

    return None


def compact_for_prompt(
    value: Any,
    *,
    depth: int = 0,
    max_depth: int = 6,
    max_items: int = 40,
    max_string: int = 600,
) -> Any:
    value = to_plain(value)

    if depth >= max_depth:
        return "<truncated: max depth>"

    if isinstance(value, str):
        if len(value) > max_string:
            return value[:max_string] + "...<truncated>"
        return value

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    if isinstance(value, list):
        items = [
            compact_for_prompt(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
            )
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            items.append(f"<truncated: {len(value) - max_items} more items>")
        return items

    if isinstance(value, dict):
        preferred_keys = [
            "id", "name", "type", "path", "x", "y", "width", "height",
            "visible", "text", "fontFamily", "fontSize", "fontWeight",
            "componentId", "componentName", "children", "roots", "nodes",
            "summary", "source_errors", "overview_preview",
        ]
        ordered_keys = [key for key in preferred_keys if key in value]
        ordered_keys.extend(key for key in value.keys() if key not in ordered_keys)

        result: dict[str, Any] = {}
        for key in ordered_keys[:max_items]:
            result[str(key)] = compact_for_prompt(
                value[key],
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
            )

        if len(value) > max_items:
            result["<truncated>"] = f"{len(value) - max_items} more keys"

        return result

    return str(value)


def json_for_prompt(value: Any, *, max_chars: int = MAX_CONTEXT_CHARS) -> str:
    # Native token/component evidence is shallow but nested enough that the
    # default max_depth=6 can truncate token names. Keep a little more depth
    # so the validator can see actual token names and state assets.
    text = json.dumps(
        compact_for_prompt(value, max_depth=9, max_items=80, max_string=800),
        ensure_ascii=False,
        default=str,
        indent=2,
    )

    if len(text) > max_chars:
        return text[:max_chars] + "\n...<truncated design_context>"

    return text


def infer_role_guess(node: dict[str, Any]) -> str | None:
    """Small deterministic hint for the model; it does not replace validation."""
    node_type = str(node.get("type") or "").lower()
    name = str(node.get("name") or "").lower()
    text = str(node.get("text") or "").lower()
    width = node.get("width") or 0
    height = node.get("height") or 0

    combined = f"{name} {text} {node_type}"

    if node_type == "text" or "text" in node_type:
        if any(word in combined for word in ["login", "iniciar", "button", "submit"]):
            return "button_text"
        if any(word in combined for word in ["email", "password", "contraseña", "usuario"]):
            return "label_or_placeholder"
        return "text"

    if any(word in combined for word in ["button", "btn", "login", "submit", "iniciar"]):
        return "button"

    if any(word in combined for word in ["input", "field", "email", "password", "contraseña"]):
        return "input"

    try:
        numeric_width = float(width or 0)
        numeric_height = float(height or 0)
    except Exception:
        numeric_width = 0
        numeric_height = 0

    if numeric_width >= 180 and 32 <= numeric_height <= 80:
        return "input_or_button_rect"

    if numeric_width >= 250 and numeric_height >= 250:
        return "container_or_background"

    if node_type in {"group", "frame", "board"} or "group" in node_type:
        return "container"

    return None


def make_layer_ref_from_node(node: dict[str, Any]) -> dict[str, Any]:
    bbox = node.get("bbox")
    if not isinstance(bbox, dict):
        bbox = {
            "x": node.get("x"),
            "y": node.get("y"),
            "width": node.get("width"),
            "height": node.get("height"),
        }

    return {
        "node_ref": node.get("node_ref", ""),
        "id": node.get("id", ""),
        "name": node.get("name", ""),
        "type": node.get("type", ""),
        "path": node.get("path", ""),
        "bbox": bbox,
    }


def flatten_design_nodes(
    roots: list[Any],
    *,
    max_nodes: int = MAX_CONTEXT_NODES,
) -> list[dict[str, Any]]:
    """
    Flatten the Penpot tree into stable, model-friendly nodes.

    Adds:
    - node_ref: compact stable reference for the model in this validation run.
    - unique path: repeated sibling labels get [1], [2], etc.
    - raw_path: path as reported by the Penpot script.
    - bbox: grouped geometry object.
    - role_guess: deterministic hint to improve visual mapping.
    """
    nodes: list[dict[str, Any]] = []

    def base_label(node: Any) -> str:
        if not isinstance(node, dict):
            return "unnamed"

        name = str(node.get("name") or "").strip()
        node_type = str(node.get("type") or "").strip()
        node_id = str(node.get("id") or "").strip()
        return name or node_type or node_id or "unnamed"

    def child_labels(children: list[Any]) -> list[str]:
        return [base_label(child) for child in children]

    def walk(node: Any, *, path: str, depth: int) -> None:
        if len(nodes) >= max_nodes:
            return

        if not isinstance(node, dict):
            return

        node_ref = f"n_{len(nodes):03d}"
        bbox = {
            "x": node.get("x"),
            "y": node.get("y"),
            "width": node.get("width"),
            "height": node.get("height"),
        }

        flat_node = {
            "node_ref": node_ref,
            "id": node.get("id", ""),
            "name": node.get("name", ""),
            "type": node.get("type", ""),
            "path": path,
            "raw_path": node.get("path", ""),
            "depth": depth,
            "x": node.get("x"),
            "y": node.get("y"),
            "width": node.get("width"),
            "height": node.get("height"),
            "bbox": bbox,
            "visible": node.get("visible", True),
            "text": node.get("text"),
            "fontFamily": node.get("fontFamily"),
            "fontSize": node.get("fontSize"),
            "fontWeight": node.get("fontWeight"),
            "componentId": node.get("componentId"),
            "componentName": node.get("componentName"),
        }
        flat_node["role_guess"] = infer_role_guess(flat_node)
        nodes.append(flat_node)

        children = node.get("children", [])
        if not isinstance(children, list):
            return

        labels = child_labels(children)
        counts = Counter(labels)
        seen: Counter[str] = Counter()

        for child, label in zip(children, labels):
            seen[label] += 1
            unique_label = label
            if counts[label] > 1:
                unique_label = f"{label}[{seen[label]}]"

            child_path = f"{path} / {unique_label}" if path else unique_label
            walk(child, path=child_path, depth=depth + 1)

    root_labels = child_labels(roots)
    root_counts = Counter(root_labels)
    root_seen: Counter[str] = Counter()

    for root, label in zip(roots, root_labels):
        root_seen[label] += 1
        unique_label = label
        if root_counts[label] > 1:
            unique_label = f"{label}[{root_seen[label]}]"
        walk(root, path=unique_label, depth=0)

    return nodes

async def collect_design_context(shape_id: str) -> dict[str, Any]:
    """
    Read Penpot structure deterministically before the vision call.

    The model never decides the code. Python invokes execute_code with a fixed
    read-only script and combines it with high_level_overview.
    """
    await get_validator_tools()

    source_errors: dict[str, str] = {}
    available_sources: list[str] = []
    overview_preview: str | None = None

    overview_tool = _validator_tools_by_name.get("high_level_overview")
    if overview_tool is not None:
        try:
            overview_result = await overview_tool.ainvoke({})
            overview_text = extract_text_from_tool_result(overview_result)
            overview_preview = overview_text[:800]
            available_sources.append("high_level_overview")
        except Exception as exc:
            source_errors["high_level_overview"] = repr(exc)
    else:
        source_errors["high_level_overview"] = "tool_not_available"

    execute_code = _internal_tools_by_name.get("execute_code")
    structure: dict[str, Any] = {}

    if execute_code is None:
        source_errors["execute_code"] = (
            "execute_code no está disponible en el MCP server. "
            "No se puede obtener estructura real de capas."
        )
    else:
        try:
            raw_result = await execute_code.ainvoke(
                {
                    "code": load_js("penpot_read_structure.js"),
                }
            )
            raw_text = extract_text_from_tool_result(raw_result)
            parsed = parse_json_object_from_text(raw_text)

            if not isinstance(parsed, dict):
                raise ValueError(
                    "execute_code no devolvió JSON parseable. "
                    f"Preview: {raw_text[:1000]}"
                )

            structure = parsed
            available_sources.append("execute_code")

        except Exception as exc:
            source_errors["execute_code"] = repr(exc)

    roots = structure.get("roots", [])
    if not isinstance(roots, list):
        roots = []

    nodes = flatten_design_nodes(roots)

    return {
        "root_shape_id": shape_id,
        "summary": {
            "root_shape_id": shape_id,
            "page": structure.get("page", {}),
            "file": structure.get("file", {}),
            "root_source": structure.get("root_source"),
            "selection_count": structure.get("selection_count", 0),
            "root_count": structure.get("root_count", 0),
            "node_count": len(nodes),
            "available_sources": available_sources,
            "failed_sources": list(source_errors.keys()),
            "library_available": bool(structure.get("library", {}).get("available")) if isinstance(structure.get("library"), dict) else False,
        },
        "source_errors": source_errors,
        "overview_preview": overview_preview,
        # Native Penpot library metadata is intentionally global to the file, even
        # when the user validates only a selected root. This lets the validator
        # recognize real Assets/Tokens instead of requiring visible canvas notes.
        "library": structure.get("library", {}),
        # Keep prompt payload compact. The full tree can be huge and can cause
        # vision request timeouts. Use the flattened node list for matching.
        "nodes": nodes[:MAX_CONTEXT_NODES],
        "roots": [],
        "purpose": "visual_layer_mapping",
    }


def build_node_indexes(design_context: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    nodes = design_context.get("nodes", [])
    if not isinstance(nodes, list):
        nodes = []

    by_ref: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}

    for node in nodes:
        if not isinstance(node, dict):
            continue

        node_ref = str(node.get("node_ref") or "").strip()
        node_id = str(node.get("id") or "").strip()

        if node_ref:
            by_ref[node_ref] = node
        if node_id:
            by_id[node_id] = node

    return by_ref, by_id


def normalize_layer_reference(
    layer: Any,
    *,
    nodes_by_ref: dict[str, dict[str, Any]],
    nodes_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(layer, dict):
        return None

    node_ref = str(layer.get("node_ref") or "").strip()
    node_id = str(layer.get("id") or "").strip()

    node: dict[str, Any] | None = None

    if node_ref and node_ref in nodes_by_ref:
        node = nodes_by_ref[node_ref]
    elif node_id and node_id in nodes_by_id:
        node = nodes_by_id[node_id]

    if node is None:
        return None

    return make_layer_ref_from_node(node)


def normalize_layers_list(
    layers: Any,
    *,
    nodes_by_ref: dict[str, dict[str, Any]],
    nodes_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(layers, list):
        return [], 0

    normalized: list[dict[str, Any]] = []
    invalid_count = 0
    seen: set[tuple[str, str]] = set()

    for layer in layers:
        normalized_layer = normalize_layer_reference(
            layer,
            nodes_by_ref=nodes_by_ref,
            nodes_by_id=nodes_by_id,
        )

        if normalized_layer is None:
            invalid_count += 1
            continue

        key = (
            str(normalized_layer.get("node_ref") or ""),
            str(normalized_layer.get("id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        normalized.append(normalized_layer)

    return normalized, invalid_count



def _blank_layer_ref() -> dict[str, Any]:
    return {
        "node_ref": "",
        "id": "",
        "name": "",
        "type": "",
        "path": "",
        "bbox": None,
    }


def _complete_checks(checks: Any) -> dict[str, Any]:
    check_names = [
        "screen_structure",
        "visual_structure_mapping",
        "layer_naming",
        "componentization",
        "layout_spacing",
        "text_legibility",
        "accessibility",
        "frontend_handoff",
    ]
    result: dict[str, Any] = {}
    source = checks if isinstance(checks, dict) else {}
    for name in check_names:
        item = source.get(name, {}) if isinstance(source.get(name, {}), dict) else {}
        notes = item.get("notes", [])
        if not isinstance(notes, list):
            notes = [str(notes)] if notes else []
        result[name] = {
            "status": item.get("status", "unknown"),
            "score": int(item.get("score", 0) or 0),
            "notes": [str(note) for note in notes[:2]],
        }
    return result


def expand_dvcp_delta_report(
    report: dict[str, Any],
    design_context: dict[str, Any],
) -> dict[str, Any]:
    """Expand compact DVCP refs into the legacy internal report shape.

    The LLM speaks compact DVCP (`visual_map.ref`, `issues.affected_refs`).
    Downstream Python keeps using the existing expanded report contract.
    """
    if not isinstance(report, dict):
        return report

    is_compact = "visual_map" in report or any(
        isinstance(issue, dict) and "affected_refs" in issue
        for issue in report.get("issues", [])
        if isinstance(report.get("issues", []), list)
    )
    if not is_compact:
        return report

    nodes_by_ref, _nodes_by_id = build_node_indexes(design_context)

    def layer_from_ref(ref: Any) -> dict[str, Any]:
        node = nodes_by_ref.get(str(ref or "").strip())
        if node is None:
            return _blank_layer_ref()
        return make_layer_ref_from_node(node)

    visual_map = report.get("visual_map", [])
    expanded_visual_map: list[dict[str, Any]] = []
    if isinstance(visual_map, list):
        for item in visual_map[:20]:
            if not isinstance(item, dict):
                continue
            expanded_visual_map.append(
                {
                    "visual_region": item.get("region") or item.get("visual_region") or "",
                    "inferred_role": item.get("role") or item.get("inferred_role") or "",
                    "matched_layer": layer_from_ref(item.get("ref") or item.get("node_ref")),
                    "confidence": item.get("confidence", 0),
                }
            )

    issues = report.get("issues", [])
    expanded_issues: list[dict[str, Any]] = []
    required_fixes: list[str] = []
    if isinstance(issues, list):
        for issue in issues[:8]:
            if not isinstance(issue, dict):
                continue
            refs = issue.get("affected_refs", [])
            if not isinstance(refs, list):
                refs = []
            affected_layers = [layer_from_ref(ref) for ref in refs[:12]]
            affected_layers = [layer for layer in affected_layers if layer.get("node_ref")]
            recommendation = str(issue.get("recommendation") or "").strip()
            if recommendation and recommendation not in required_fixes:
                required_fixes.append(recommendation)
            expanded_issues.append(
                {
                    "severity": issue.get("severity", "medium"),
                    "category": issue.get("category", ""),
                    "message": issue.get("message", ""),
                    "affected_layers": affected_layers,
                    "recommendation": recommendation,
                }
            )

    expanded = {
        "passed": bool(report.get("passed", False)),
        "score": int(report.get("score", 0) or 0),
        "status": report.get("status", "not_ready"),
        "summary": report.get("summary", ""),
        "design_context_summary": design_context.get("summary", {}),
        "source_errors": design_context.get("source_errors", {}),
        "visual_structure_map": expanded_visual_map,
        "checks": _complete_checks(report.get("checks", {})),
        "issues": expanded_issues,
        "required_fixes": required_fixes[:8],
        "suggested_structure": str(report.get("suggested_structure") or "")[:300],
        "developer_notes": [],
        "manual_fixes": [],
        "auto_fix_plan": [],
        "can_be_sent_to_development": bool(report.get("can_be_sent_to_development", False)),
        "dvcp": {
            "protocol_version": report.get("protocol_version", "DVCP/0.1"),
            "compact_input": True,
            "llm_output_compact": True,
            "expanded_by_python": True,
        },
    }
    return expanded


def normalize_report_layer_references(
    report: dict[str, Any],
    design_context: dict[str, Any],
) -> dict[str, Any]:
    """
    Make model layer references safe before the fixer uses them.

    - Uses node_ref first, id second.
    - Corrects stale/wrong name/type/path/bbox from the model.
    - Removes references that do not exist in design_context.nodes.
    """
    nodes_by_ref, nodes_by_id = build_node_indexes(design_context)
    invalid_refs = 0

    visual_map = report.get("visual_structure_map", [])
    if isinstance(visual_map, list):
        normalized_map: list[dict[str, Any]] = []

        for item in visual_map:
            if not isinstance(item, dict):
                continue

            matched_layer = item.get("matched_layer")
            normalized_layer = normalize_layer_reference(
                matched_layer,
                nodes_by_ref=nodes_by_ref,
                nodes_by_id=nodes_by_id,
            )

            if normalized_layer is None:
                invalid_refs += 1
                normalized_layer = {
                    "node_ref": "",
                    "id": "",
                    "name": "",
                    "type": "",
                    "path": "",
                    "bbox": None,
                }

            normalized_map.append({**item, "matched_layer": normalized_layer})

        report["visual_structure_map"] = normalized_map

    issues = report.get("issues", [])
    if isinstance(issues, list):
        normalized_issues: list[dict[str, Any]] = []

        for issue in issues:
            if not isinstance(issue, dict):
                continue

            normalized_layers, issue_invalid = normalize_layers_list(
                issue.get("affected_layers", []),
                nodes_by_ref=nodes_by_ref,
                nodes_by_id=nodes_by_id,
            )
            invalid_refs += issue_invalid
            normalized_issues.append({**issue, "affected_layers": normalized_layers})

        report["issues"] = normalized_issues

    if invalid_refs:
        notes = report.get("developer_notes", [])
        if not isinstance(notes, list):
            notes = [str(notes)]
        notes.append(
            f"Layer reference normalization removed or corrected {invalid_refs} invalid model references."
        )
        report["developer_notes"] = notes

    return report


def is_generic_layer_name(name: Any) -> bool:
    """Return True when a Penpot layer name is too generic for handoff."""
    value = str(name or "").strip().lower()
    if not value:
        return True

    generic_names = {
        "rectangle",
        "rect",
        "text",
        "group",
        "shape",
        "path",
        "board",
        "frame",
        "ellipse",
        "image",
    }

    if value in generic_names:
        return True

    for generic_name in generic_names:
        if value.startswith(generic_name + " "):
            suffix = value.removeprefix(generic_name + " ").strip()
            if suffix.isdigit():
                return True

        if value.startswith(generic_name + "-"):
            suffix = value.removeprefix(generic_name + "-").strip()
            if suffix.isdigit():
                return True

    return False


def pascal_case(value: str) -> str:
    """Small dependency-free PascalCase helper for generated layer names."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in value)
    parts = [part for part in cleaned.strip().split() if part]
    if not parts:
        return "Layer"
    return "".join(part[:1].upper() + part[1:] for part in parts)


def semantic_layer_name_from_mapping(
    *,
    visual_region: str,
    inferred_role: str,
    layer: dict[str, Any],
) -> str | None:
    """Generate a generic semantic name from DVCP visual mapping.

    This function must not assume a particular template such as a login screen.
    It converts detected region/role/type into stable UI-oriented names like
    `PrimaryButtonBackground`, `SearchInputLabelText`, `MetricCardBackground`,
    `MainTableContainer`, etc.
    """
    region = str(visual_region or "").strip().lower()
    role = str(inferred_role or "").strip().lower()
    current_name = str(layer.get("name") or "").strip().lower()
    layer_type = str(layer.get("type") or "").strip().lower()
    text_value = str(layer.get("text") or "").strip().lower()
    combined = f"{region} {role} {current_name} {text_value}"

    is_text = "text" in layer_type or "text" in role or "label" in role or "heading" in role
    is_shape = any(token in layer_type for token in ["rect", "shape", "path", "ellipse", "board", "frame", "group"])

    def has_any(tokens: list[str]) -> bool:
        return any(token in combined for token in tokens)

    def variant(default: str = "Main") -> str:
        candidates = [
            ("primary", "Primary"), ("secondary", "Secondary"), ("danger", "Danger"),
            ("submit", "Submit"), ("save", "Save"), ("search", "Search"),
            ("email", "Email"), ("password", "Password"), ("filter", "Filter"),
            ("metric", "Metric"), ("product", "Product"), ("profile", "Profile"),
            ("sidebar", "Sidebar"), ("header", "Header"), ("footer", "Footer"),
            ("table", "Table"), ("chart", "Chart"), ("card", "Card"),
        ]
        for token, label in candidates:
            if token in combined:
                return label
        source = region or role or current_name or default
        return pascal_case(source)[:48] or default

    v = variant()

    if has_any(["button", "btn", "cta", "submit"]):
        return f"{v}ButtonText" if is_text else f"{v}ButtonBackground"

    if has_any(["input", "field", "textbox", "search", "email", "password", "select"]):
        if is_text or "label" in combined or "placeholder" in combined:
            return f"{v}InputLabelText"
        return f"{v}InputBackground"

    if has_any(["checkbox", "radio", "toggle", "switch"]):
        return f"{v}ControlLabelText" if is_text else f"{v}ControlBackground"

    if has_any(["card", "tile", "panel"]):
        return f"{v}CardText" if is_text else f"{v}CardBackground"

    if has_any(["table", "row", "column", "cell"]):
        return f"{v}TableText" if is_text else f"{v}TableContainer"

    if has_any(["chart", "graph", "metric", "kpi", "stat"]):
        return f"{v}DataText" if is_text else f"{v}DataVizContainer"

    if has_any(["nav", "sidebar", "menu", "tab", "breadcrumb"]):
        return f"{v}NavigationText" if is_text else f"{v}NavigationContainer"

    if has_any(["image", "avatar", "photo", "thumbnail", "icon", "logo"]):
        return f"{v}MediaText" if is_text else f"{v}Media"

    if has_any(["title", "heading", "headline", "h1", "h2"]):
        return f"{v}HeadingText"

    if has_any(["container", "frame", "board", "surface", "background"]):
        return f"{v}Container" if not is_text else f"{v}Text"

    if role:
        base = pascal_case(role)
        if is_text and not base.endswith("Text"):
            base += "Text"
        if is_shape and not base.endswith(("Background", "Container", "Frame")):
            base += "Background"
        return base

    return None


def suggest_semantic_layer_name(
    *,
    visual_region: str,
    inferred_role: str,
    layer: dict[str, Any],
) -> str | None:
    """Backward-compatible wrapper for semantic naming."""
    return semantic_layer_name_from_mapping(
        visual_region=visual_region,
        inferred_role=inferred_role,
        layer=layer,
    )


def _rename_confidence_threshold() -> float:
    raw_value = os.getenv(
        "PENPOT_RENAME_AUTO_FIX_MIN_CONFIDENCE",
        os.getenv("PENPOT_CANVAS_AUTO_FIX_MIN_CONFIDENCE", "0.8"),
    )
    try:
        return float(raw_value)
    except Exception:
        return 0.8


def _naming_issue_refs(report: dict[str, Any]) -> set[str]:
    issues = report.get("issues", [])
    if not isinstance(issues, list):
        return set()

    naming_categories = {
        "naming",
        "layer_naming",
        "naming_convention",
        "semantic_naming",
    }
    refs: set[str] = set()

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        category = str(issue.get("category") or "").strip().lower()
        if category not in naming_categories:
            continue
        layers = issue.get("affected_layers", [])
        if not isinstance(layers, list):
            continue
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            node_ref = str(layer.get("node_ref") or "").strip()
            if node_ref:
                refs.add(node_ref)

    return refs


def build_auto_fix_plan(report: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Build a deterministic, safe auto-fix plan from normalized DVCP output.

    Phase 1 remains rename-only. It now handles two safe cases:
    - generic layer names, e.g. Rectangle 1 -> EmailInputBackground
    - explicit layer_naming issues, e.g. EmailInputLabel -> EmailInputLabelText

    The LLM never emits commands. It only maps refs and raises issues.
    """
    visual_map = report.get("visual_structure_map", [])
    if not isinstance(visual_map, list):
        return []

    confidence_threshold = _rename_confidence_threshold()
    naming_refs = _naming_issue_refs(report)
    plan: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    used_new_names: set[str] = set()

    for item in visual_map:
        if not isinstance(item, dict):
            continue

        layer = item.get("matched_layer")
        if not isinstance(layer, dict):
            continue

        node_ref = str(layer.get("node_ref") or "").strip()
        layer_id = str(layer.get("id") or "").strip()
        current_name = str(layer.get("name") or "").strip()

        if not node_ref or not layer_id or layer_id in used_ids:
            continue

        try:
            confidence = float(item.get("confidence", 0) or 0)
        except Exception:
            confidence = 0.0

        if confidence < confidence_threshold:
            continue

        is_naming_issue_target = node_ref in naming_refs
        if not is_naming_issue_target and not is_generic_layer_name(current_name):
            continue

        suggested_name = semantic_layer_name_from_mapping(
            visual_region=str(item.get("visual_region") or ""),
            inferred_role=str(item.get("inferred_role") or ""),
            layer=layer,
        )

        if not suggested_name or suggested_name == current_name:
            continue

        base_name = suggested_name
        counter = 2
        while suggested_name in used_new_names:
            suggested_name = f"{base_name}{counter}"
            counter += 1

        used_ids.add(layer_id)
        used_new_names.add(suggested_name)

        plan.append(
            {
                "action": "rename_layer",
                "node_ref": node_ref,
                "id": layer_id,
                "current_name": current_name,
                "new_name": suggested_name,
                "type": layer.get("type", ""),
                "path": layer.get("path", ""),
                "bbox": layer.get("bbox"),
                "confidence": confidence,
                "reason": (
                    "Layer is affected by a naming issue or has a generic name; "
                    "Python generated a deterministic canonical semantic name."
                ),
                "safety": "safe_auto_fix",
            }
        )

    return plan


def build_manual_fixes(report: dict[str, Any]) -> list[str]:
    """Extract non-safe or non-automatic fixes as manual handoff notes."""
    issues = report.get("issues", [])
    if not isinstance(issues, list):
        return []

    manual_categories = {
        "accessibility",
        "componentization",
        "design_system",
        "design_tokens",
        "layout",
        "frontend_handoff",
        "interaction_states",
    }

    manual_fixes: list[str] = []
    seen: set[str] = set()

    for issue in issues:
        if not isinstance(issue, dict):
            continue

        category = str(issue.get("category") or "").strip().lower()
        severity = str(issue.get("severity") or "").strip().lower()
        recommendation = str(issue.get("recommendation") or "").strip()

        if not recommendation:
            continue

        if category in manual_categories or severity in {"critical", "high"} and category not in {"naming", "semantic_naming", "layer_naming"}:
            if recommendation not in seen:
                manual_fixes.append(recommendation)
                seen.add(recommendation)

    return manual_fixes


def attach_fix_plans(report: dict[str, Any]) -> dict[str, Any]:
    """Attach deterministic auto/manual fix plans to the validator report."""
    auto_fix_plan = build_auto_fix_plan(report)
    manual_fixes = build_manual_fixes(report)

    report["auto_fix_plan"] = auto_fix_plan
    report["manual_fixes"] = manual_fixes

    if auto_fix_plan:
        developer_notes = report.get("developer_notes", [])
        if not isinstance(developer_notes, list):
            developer_notes = [str(developer_notes)]
        developer_notes.append(
            "auto_fix_plan generated deterministically: first phase only renames generic layers."
        )
        report["developer_notes"] = developer_notes

    return report


def _node_refs_where(nodes: list[dict[str, Any]], predicate: Any) -> list[str]:
    refs: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_ref = str(node.get("node_ref") or "").strip()
        if node_ref and predicate(node):
            refs.append(node_ref)
    return refs


def _dvcp_normalize_evidence_name(value: Any) -> str:
    return (
        str(value or "")
        .replace(" / ", "/")
        .replace("/ ", "/")
        .replace(" /", "/")
        .replace("_", "")
        .replace("-", "")
        .replace(" ", "")
        .lower()
    )


def _dvcp_has_all(parts: list[str], text: str) -> bool:
    return all(_dvcp_normalize_evidence_name(part) in text for part in parts)


def compact_native_library_evidence(library: Any) -> dict[str, Any]:
    """Return compact native Penpot Assets/Tokens evidence for DVCP prompt.

    Pattern-agnostic version. It detects whether the library contains reusable
    components, interactive state assets, focus evidence and complete token sets
    without assuming login-specific names like Email/Password/Button Primary.
    """
    if not isinstance(library, dict):
        return {
            "available": False,
            "token_sets": [],
            "components": [],
            "interactive_state_evidence": {},
        }

    token_sets_out: list[dict[str, Any]] = []
    for token_set in (library.get("token_sets") or [])[:12]:
        if not isinstance(token_set, dict):
            continue
        tokens_out: list[dict[str, Any]] = []
        for token in (token_set.get("tokens") or [])[:160]:
            if not isinstance(token, dict):
                continue
            tokens_out.append({
                "name": token.get("name", ""),
                "type": token.get("type", ""),
                "value": token.get("value"),
            })
        token_sets_out.append({
            "name": token_set.get("name", ""),
            "token_count": token_set.get("token_count", len(tokens_out)),
            "tokens": tokens_out,
        })

    components_out: list[dict[str, Any]] = []
    for component in (library.get("components") or [])[:220]:
        if not isinstance(component, dict):
            continue
        full_name = (
            component.get("full_name")
            or component.get("plugin_full_name")
            or component.get("path")
            or component.get("name")
            or ""
        )
        semantic_role = component.get("semantic_role") or component.get("plugin_semantic_role") or ""
        components_out.append({
            "name": component.get("name", ""),
            "full_name": full_name,
            "path": component.get("path", ""),
            "semantic_role": semantic_role,
            "type": component.get("type", ""),
            "id": component.get("id", ""),
            "dvcp_states": component.get("dvcpStatesParsed") or component.get("dvcpStates") or component.get("dvcpStatesSerialized"),
            "is_focus_state": bool(component.get("is_focus_state")),
            "is_hover_state": bool(component.get("is_hover_state")),
            "is_disabled_state": bool(component.get("is_disabled_state")),
        })

    token_names: list[str] = []
    token_set_names: list[str] = []
    for token_set in token_sets_out:
        name = str(token_set.get("name") or "")
        if name:
            token_set_names.append(name)
        for token in token_set.get("tokens") or []:
            token_name = str(token.get("name") or "")
            if token_name:
                token_names.append(token_name)

    component_names = [str(item.get("name") or "") for item in components_out if item.get("name")]
    component_full_names = [str(item.get("full_name") or item.get("path") or item.get("name") or "") for item in components_out]
    component_semantic_roles = [str(item.get("semantic_role") or "") for item in components_out if item.get("semantic_role")]

    normalized_components = _dvcp_normalize_evidence_name(
        " / ".join(component_full_names + component_names + component_semantic_roles + [str(item.get("dvcp_states") or "") for item in components_out])
    )
    normalized_tokens = _dvcp_normalize_evidence_name(" / ".join(token_names))
    reader_evidence = library.get("interactive_state_evidence") if isinstance(library.get("interactive_state_evidence"), dict) else {}

    def has_word(word: str) -> bool:
        return _dvcp_normalize_evidence_name(word) in normalized_components

    component_count = len([c for c in components_out if c.get("name") or c.get("full_name")])
    interactive_component_count = len([
        c for c in components_out
        if any(term in _dvcp_normalize_evidence_name(" ".join([str(c.get("full_name") or ""), str(c.get("semantic_role") or ""), str(c.get("dvcp_states") or "")])) for term in ["button", "input", "control", "select", "toggle", "navigation", "tab", "menu"])
    ])

    evidence = {
        "component_count": component_count,
        "interactive_component_count": interactive_component_count,
        "has_input_component": bool(reader_evidence.get("has_input_component")) or has_word("input") or has_word("field") or has_word("textinput"),
        "has_button_component": bool(reader_evidence.get("has_button_component")) or has_word("button") or has_word("cta"),
        "has_control_component": bool(reader_evidence.get("has_control_component")) or has_word("control") or has_word("checkbox") or has_word("toggle") or has_word("radio"),
        "has_navigation_component": bool(reader_evidence.get("has_navigation_component")) or has_word("navigation") or has_word("sidebar") or has_word("menu") or has_word("tab"),
        "has_card_component": bool(reader_evidence.get("has_card_component")) or has_word("card") or has_word("surface") or has_word("panel"),
        "has_table_component": bool(reader_evidence.get("has_table_component")) or has_word("table") or has_word("row") or has_word("cell"),
        "has_focus_state": bool(reader_evidence.get("has_focus_state")) or has_word("focus"),
        "has_hover_state": bool(reader_evidence.get("has_hover_state")) or has_word("hover"),
        "has_disabled_state": bool(reader_evidence.get("has_disabled_state")) or has_word("disabled"),
        "has_focus_tokens": bool(reader_evidence.get("has_focus_tokens")) or "colorfocusring" in normalized_tokens or "borderfocuswidth" in normalized_tokens,
        "has_interactive_color_tokens": bool(reader_evidence.get("has_interactive_color_tokens")) or ("hover" in normalized_tokens and "disabled" in normalized_tokens),
        "has_spacing_tokens": bool(reader_evidence.get("has_spacing_tokens")) or "spacingformgap" in normalized_tokens or "spacinginputpaddingx" in normalized_tokens or "spacing24" in normalized_tokens,
        "has_typography_tokens": bool(reader_evidence.get("has_typography_tokens")) or "typographyheading" in normalized_tokens or "typographybody" in normalized_tokens,
    }
    evidence["interactive_tokens_complete"] = evidence["has_focus_tokens"] and evidence["has_interactive_color_tokens"]
    evidence["focus_complete"] = evidence["has_focus_state"] and evidence["has_focus_tokens"]
    evidence["button_states_complete"] = evidence["has_button_component"] and evidence["has_hover_state"] and evidence["has_disabled_state"]
    # Generic component state completeness: there is at least one interactive component with focus metadata,
    # plus button hover/disabled when a button component exists.
    evidence["component_states_complete"] = evidence["focus_complete"] and (not evidence["has_button_component"] or evidence["button_states_complete"])
    evidence["tokens_complete"] = evidence["interactive_tokens_complete"] and evidence["has_spacing_tokens"] and evidence["has_typography_tokens"]

    # Backward-compatible aliases used by existing report post-processing.
    evidence["all_focus_states"] = evidence["focus_complete"]
    evidence["all_button_states"] = evidence["button_states_complete"]

    return {
        "available": bool(library.get("available")),
        "token_sets": token_sets_out,
        "token_set_names": token_set_names,
        "token_names": token_names[:260],
        "components": components_out,
        "component_names": component_names[:260],
        "component_full_names": component_full_names[:260],
        "component_semantic_roles": component_semantic_roles[:260],
        "interactive_state_evidence": evidence,
        "colors": [item.get("name", "") for item in (library.get("colors") or [])[:40] if isinstance(item, dict)],
        "typographies": [item.get("name", "") for item in (library.get("typographies") or [])[:40] if isinstance(item, dict)],
    }


def apply_native_interactive_evidence_to_report(
    report: dict[str, Any],
    design_context: dict[str, Any],
) -> dict[str, Any]:
    """Deterministically credit native state assets/tokens discovered by the reader.

    The vision model can miss library-only evidence because it is not visible on the
    selected canvas. This pass does not invent design quality; it only prevents
    repeated warnings when `penpot_read_structure.js` proves that DVCP/Core tokens
    and native state assets exist in the Penpot library.
    """
    if not isinstance(report, dict):
        return report

    native = compact_native_library_evidence(design_context.get("library"))
    evidence = native.get("interactive_state_evidence") or {}
    if not isinstance(evidence, dict):
        return report

    tokens_complete = bool(evidence.get("interactive_tokens_complete"))
    states_complete = bool(evidence.get("component_states_complete"))
    focus_complete = bool(evidence.get("all_focus_states"))
    button_states_complete = bool(evidence.get("all_button_states"))

    if not (tokens_complete or states_complete or focus_complete or button_states_complete):
        return report

    checks = report.get("checks")
    if not isinstance(checks, dict):
        checks = {}
        report["checks"] = checks

    def upsert_check(name: str, min_score: int, status: str, note: str) -> None:
        item = checks.get(name)
        if not isinstance(item, dict):
            item = {"status": status, "score": min_score, "notes": []}
            checks[name] = item
        item["score"] = max(int(item.get("score") or 0), min_score)
        current_status = str(item.get("status") or "unknown")
        if current_status in {"fail", "unknown"} or status == "pass":
            item["status"] = status
        notes = item.get("notes")
        if not isinstance(notes, list):
            notes = []
        if note not in notes:
            notes.append(note)
        item["notes"] = notes[:2]

    if tokens_complete:
        upsert_check(
            "frontend_handoff",
            85,
            "pass" if states_complete else "warning",
            "Tokens interactivos nativos detectados en DVCP/Core.",
        )
        upsert_check(
            "layout_spacing",
            90,
            "pass",
            "Tokens de spacing nativos detectados.",
        )

    if states_complete:
        upsert_check(
            "componentization",
            85,
            "pass",
            "Assets nativos de estados interactivos detectados.",
        )
        upsert_check(
            "accessibility",
            85,
            "pass",
            "Estados de focus nativos detectados para componentes interactivos.",
        )

    elif focus_complete:
        upsert_check(
            "accessibility",
            80,
            "warning",
            "Estados de focus nativos detectados parcialmente.",
        )

    # Remove issues that are fully satisfied by native library evidence.
    filtered_issues: list[dict[str, Any]] = []
    removed_categories: list[str] = []
    for issue in report.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        category = str(issue.get("category") or "").lower()
        text = " ".join([
            str(issue.get("message") or ""),
            str(issue.get("recommendation") or ""),
        ]).lower()
        is_interaction_issue = any(term in text for term in ["focus", "hover", "disabled", "interactivo", "estado", "states"])
        is_spacing_issue = "spacing" in text or "espaciado" in text
        remove = False
        if states_complete and is_interaction_issue and category in {"componentization", "accessibility", "frontend_handoff"}:
            remove = True
        if tokens_complete and is_spacing_issue and category in {"layout_spacing", "frontend_handoff"}:
            remove = True
        if remove:
            removed_categories.append(category or "unknown")
            continue
        filtered_issues.append(issue)
    report["issues"] = filtered_issues

    # Clean stale required/manual fixes after deterministic native evidence removes
    # their underlying issue categories. The LLM may still emit textual
    # recommendations such as "add focus states" even when the reader proves
    # that DVCP/Core tokens and native state assets already exist. At this point
    # `issues` is the source of truth; required_fixes/manual_fixes must not
    # contradict the deterministic evidence.
    def _normalize_fix_text(value: Any) -> str:
        import unicodedata

        text = str(value or "").lower()
        text = unicodedata.normalize("NFKD", text)
        return "".join(ch for ch in text if not unicodedata.combining(ch))

    def _should_remove_stale_fix(value: Any) -> bool:
        text = _normalize_fix_text(value)
        if not text:
            return True

        interaction_terms = [
            "focus", "hover", "disabled", "disable", "interactivo",
            "interactivos", "estado", "estados", "states", "variant",
            "variante", "variantes", "outline", "aria",
        ]
        token_terms = [
            "token", "tokens", "dvcp/core", "color.focus.ring",
            "color.action.primary", "color.text.disabled",
        ]
        spacing_terms = [
            "spacing", "espaciado", "gap", "margen", "padding",
            "spacing.form.gap", "spacing.16", "spacing.24", "spacing.32",
        ]
        component_terms = [
            "componente", "componentes", "component", "components",
            "asset", "assets", "textinput", "button/primary", "button", "input", "control",
        ]

        is_interaction_fix = any(term in text for term in interaction_terms)
        is_token_fix = any(term in text for term in token_terms)
        is_spacing_fix = any(term in text for term in spacing_terms)
        is_component_fix = any(term in text for term in component_terms)

        if states_complete and (is_interaction_fix or is_component_fix):
            return True
        if focus_complete and "focus" in text:
            return True
        if button_states_complete and any(term in text for term in ["hover", "disabled", "disable", "button/primary", "button", "input", "control", "boton"]):
            return True
        if tokens_complete and (is_token_fix or is_spacing_fix):
            return True
        return False

    def _clean_fix_list(field: str) -> None:
        values = report.get(field)
        if not isinstance(values, list):
            return
        cleaned: list[Any] = []
        seen: set[str] = set()
        for value in values:
            if _should_remove_stale_fix(value):
                continue
            key = _normalize_fix_text(value)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(value)
        report[field] = cleaned[:8]

    _clean_fix_list("required_fixes")
    _clean_fix_list("manual_fixes")

    if states_complete and tokens_complete:
        report["score"] = max(int(report.get("score") or 0), 88)
        report["passed"] = True
        report["status"] = "ready"
        report["can_be_sent_to_development"] = True

    dvcp = report.get("dvcp")
    if not isinstance(dvcp, dict):
        dvcp = {}
    dvcp["native_interactive_evidence"] = {
        "tokens_complete": tokens_complete,
        "states_complete": states_complete,
        "focus_complete": focus_complete,
        "button_states_complete": button_states_complete,
        "removed_issue_categories": removed_categories[:12],
    }
    report["dvcp"] = dvcp

    return report

def make_dvcp_design_snapshot(design_context: dict[str, Any]) -> dict[str, Any]:
    """Build a compact set/reference snapshot for the LLM.

    DVCP rule: the LLM sees the universe U and refers to layers by node_ref.
    It must not repeat Penpot ids/names/paths in every issue.
    """
    raw_nodes = design_context.get("nodes", [])
    nodes = [node for node in raw_nodes if isinstance(node, dict)][:MAX_CONTEXT_NODES]

    def node_type(node: dict[str, Any]) -> str:
        return str(node.get("type") or "").lower()

    def semantic_text(node: dict[str, Any]) -> str:
        return f"{node.get('name') or ''} {node.get('text') or ''} {node.get('role_guess') or ''}".lower()

    node_table = []
    for node in nodes:
        node_table.append(
            {
                "ref": node.get("node_ref", ""),
                "name": node.get("name", ""),
                "type": node.get("type", ""),
                "text": node.get("text"),
                "role_guess": node.get("role_guess"),
                "bbox": {
                    "x": node.get("x"),
                    "y": node.get("y"),
                    "w": node.get("width"),
                    "h": node.get("height"),
                },
            }
        )

    sets = {
        "Text": _node_refs_where(nodes, lambda n: "text" in node_type(n)),
        "Shape": _node_refs_where(nodes, lambda n: any(t in node_type(n) for t in ["rect", "ellipse", "path", "shape"])),
        "Container": _node_refs_where(nodes, lambda n: any(t in node_type(n) for t in ["group", "frame", "board"])),
        "Component": _node_refs_where(nodes, lambda n: bool(n.get("componentId") or n.get("componentName"))),
        "InputCandidates": _node_refs_where(nodes, lambda n: any(w in semantic_text(n) for w in ["input", "field", "email", "password", "contraseña"])),
        "ButtonCandidates": _node_refs_where(nodes, lambda n: any(w in semantic_text(n) for w in ["button", "btn", "login", "submit", "iniciar"])),
        "LabelCandidates": _node_refs_where(nodes, lambda n: "label" in semantic_text(n) or "placeholder" in semantic_text(n)),
        "GenericNames": _node_refs_where(nodes, lambda n: is_generic_layer_name(n.get("name"))),
    }

    return {
        "protocol_version": "DVCP/0.1",
        "root_shape_id": design_context.get("root_shape_id"),
        "summary": design_context.get("summary", {}),
        "source_errors": design_context.get("source_errors", {}),
        "U": [node.get("node_ref", "") for node in nodes if node.get("node_ref")],
        "node_table": node_table,
        "sets": sets,
        "native_library": compact_native_library_evidence(design_context.get("library")),
        "rules": {
            "reference_only": "Use only refs from U in visual_map.ref and issues.affected_refs.",
            "no_layer_objects": "Do not repeat id/name/type/path in the LLM output.",
            "no_fix_commands": "Do not generate auto_fix_plan or manual_fixes; Python generates plans after normalization.",
        },
    }


def make_compact_design_context(design_context: dict[str, Any]) -> dict[str, Any]:
    """Return the compact DVCP snapshot Mistral needs for visual-layer mapping."""
    return make_dvcp_design_snapshot(design_context)


def attach_design_context_to_report(
    report: dict[str, Any],
    design_context: dict[str, Any],
) -> dict[str, Any]:
    """Preserve context diagnostics even when Mistral/export validation fails."""
    report.setdefault("design_context_summary", design_context.get("summary", {}))
    report.setdefault("source_errors", design_context.get("source_errors", {}))
    report.setdefault("visual_structure_map", [])
    return report


def build_visual_prompt(
    context: str | None,
    design_context: dict[str, Any] | None = None,
) -> str:
    contract_json = json.dumps(
        load_json_resource("schemas/validator_delta_contract.json"),
        ensure_ascii=False,
        indent=2,
        default=str,
    )

    return render_skill(
        "validator.md",
        {
            "USER_REQUEST": context or "Valida visualmente la pantalla actual de Penpot para handoff frontend.",
            "DESIGN_CONTEXT_JSON": json_for_prompt(design_context or {}),
            "VALIDATOR_REPORT_CONTRACT_JSON": contract_json,
        },
    )

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
        user_input = ""

    return {
        "changeme": user_input,
        "validation_report": None,
        "passed": None,
        "score": None,
        "status": None,
        "design_context": {},
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

        design_context = await collect_design_context(shape_id)

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
            report = attach_design_context_to_report(report, design_context)
            return {
                **extract_output_from_report(report),
                "design_context": design_context,
            }

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
            max_tokens=int(os.getenv("MISTRAL_VISION_MAX_TOKENS", "2500")),
        )

        compact_design_context = make_compact_design_context(design_context)

        visual_prompt = build_visual_prompt(
            state.get("changeme"),
            design_context=compact_design_context,
        )

        try:
            metered_result = await metered_ainvoke(
                vision_runnable,
                [HumanMessage(content=visual_prompt)],
                estimated_completion_tokens=int(
                    os.getenv("MISTRAL_VISION_ESTIMATED_COMPLETION_TOKENS", "2500")
                ),
                extra_estimated_tokens=int(
                    os.getenv("MISTRAL_VISION_EXTRA_ESTIMATED_TOKENS", "1500")
                ),
            )
        except Exception as exc:
            report = make_mistral_error_report(exc)
            report = attach_design_context_to_report(report, design_context)
            return {
                **extract_output_from_report(report),
                "design_context": design_context,
            }

        report = parse_json_report(metered_result.ai_message.content)

        if not isinstance(report, dict):
            report = make_error_report(
                summary="Mistral Vision no devolvió JSON parseable.",
                category="mistral_json",
                message=str(report),
                recommendation="Revisar prompt visual y response_format json_object.",
            )

        if isinstance(report, dict):
            report = expand_dvcp_delta_report(report, design_context)
            report = attach_design_context_to_report(report, design_context)
            report = normalize_report_layer_references(report, design_context)
            report = apply_native_interactive_evidence_to_report(report, design_context)
            report = attach_fix_plans(report)

        return {
            **extract_output_from_report(report),
            **usage_updates_from_metered_result(state, metered_result),
            "design_context": design_context,
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
