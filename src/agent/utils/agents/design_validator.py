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
    text = json.dumps(
        compact_for_prompt(value),
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
        },
        "source_errors": source_errors,
        "overview_preview": overview_preview,
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


def suggest_semantic_layer_name(
    *,
    visual_region: str,
    inferred_role: str,
    layer: dict[str, Any],
) -> str | None:
    """Generate a conservative rename suggestion from visual mapping."""
    region = str(visual_region or "").strip().lower()
    role = str(inferred_role or "").strip().lower()
    layer_type = str(layer.get("type") or "").strip().lower()
    text_value = str(layer.get("text") or "").strip().lower()
    combined = f"{region} {role} {text_value}"

    # Specific UI roles first.
    if "email" in combined:
        if "background" in combined or "input" in combined or "rectangle" in layer_type:
            return "EmailInputBackground"
        return "EmailInputLabel"

    if "password" in combined or "contraseña" in combined:
        if "background" in combined or "input" in combined or "rectangle" in layer_type:
            return "PasswordInputBackground"
        return "PasswordInputLabel"

    if "button" in combined or "login" in combined or "iniciar" in combined:
        if "text" in role or "text" in layer_type:
            return "LoginButtonText"
        return "LoginButtonBackground"

    if "title" in combined or "heading" in combined:
        return "LoginTitleText"

    if "container" in combined or "background" in combined:
        return "LoginCardBackground"

    # Generic fallback based on model role + layer type.
    if role:
        base = pascal_case(role)
        if "text" in layer_type and not base.endswith("Text"):
            base += "Text"
        if "rectangle" in layer_type and not base.endswith(("Background", "Container")):
            base += "Background"
        return base

    return None


def build_auto_fix_plan(report: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Build a deterministic, safe auto-fix plan from the normalized visual map.

    First phase is intentionally conservative: only rename generic layers.
    No layout, color, text, component, or accessibility mutation is planned here.
    """
    visual_map = report.get("visual_structure_map", [])
    if not isinstance(visual_map, list):
        return []

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

        if not node_ref or not layer_id:
            continue

        if layer_id in used_ids:
            continue

        if not is_generic_layer_name(current_name):
            continue

        suggested_name = suggest_semantic_layer_name(
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
                "reason": "Layer name is generic and does not describe its visual/frontend role.",
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


def make_compact_design_context(design_context: dict[str, Any]) -> dict[str, Any]:
    """Return only the information Mistral needs for visual-layer mapping."""
    return {
        "root_shape_id": design_context.get("root_shape_id"),
        "summary": design_context.get("summary", {}),
        "source_errors": design_context.get("source_errors", {}),
        "overview_preview": design_context.get("overview_preview"),
        "nodes": design_context.get("nodes", [])[:MAX_CONTEXT_NODES],
        "purpose": design_context.get("purpose", "visual_layer_mapping"),
    }


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
        load_json_resource("schemas/validator_report_contract.json"),
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
            max_tokens=int(os.getenv("MISTRAL_VISION_MAX_TOKENS", "6000")),
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
                    os.getenv("MISTRAL_VISION_ESTIMATED_COMPLETION_TOKENS", "6000")
                ),
                extra_estimated_tokens=int(
                    os.getenv("MISTRAL_VISION_EXTRA_ESTIMATED_TOKENS", "2500")
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
            report = attach_design_context_to_report(report, design_context)
            report = normalize_report_layer_references(report, design_context)
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
