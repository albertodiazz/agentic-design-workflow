"""Penpot design builder graph with validator and fixer workflows."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
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
from agent.utils.resource_loader import (
    load_js,
    read_skill,
)

from agent.utils.fixer_prompt import (
    canvas_auto_fix_enabled,
    canvas_confidence_threshold,
    extract_known_canvas_targets,
)


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
# Builder prompt resources live in utils/skills
# ---------------------------------------------------------------------

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

    auto_fix_verified: bool
    auto_fix_event: dict[str, Any] | None
    auto_fix_verification: dict[str, Any] | None

    semantic_auto_fix_result: dict[str, Any] | None


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

    # Debug/event state for post-fix verification.
    # This is intentionally separate from validation_report / passed / score / status.
    last_auto_fix_plan: NotRequired[list[dict[str, Any]]]
    last_canvas_fix_targets: NotRequired[list[dict[str, Any]]]
    last_canvas_fix_plan: NotRequired[list[dict[str, Any]]]
    last_semantic_fix_plan: NotRequired[list[dict[str, Any]]]
    canvas_auto_fix_result: NotRequired[dict[str, Any]]
    semantic_auto_fix_result: NotRequired[dict[str, Any]]
    post_fix_validation_mode: NotRequired[str | None]
    auto_fix_verified: NotRequired[bool]
    auto_fix_event: NotRequired[dict[str, Any] | None]
    auto_fix_verification: NotRequired[dict[str, Any] | None]


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

    _builder_tools_by_name = {}

    for tool in _builder_tools:
        tool_name = getattr(tool, "name", "")
        normalized_name = normalize_tool_name(tool_name)

        _builder_tools_by_name[tool_name] = tool
        _builder_tools_by_name[normalized_name] = tool

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


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def get_auto_fix_plan_from_report(
    validation_report: Any,
) -> list[dict[str, Any]]:
    """Return executable auto-fix actions from a validation report.

    The validator is stateless and only proposes auto_fix_plan.
    fix_design copies the plan into last_auto_fix_plan when it attempts to apply it.
    """
    if not isinstance(validation_report, dict):
        return []

    raw_plan = validation_report.get("auto_fix_plan", [])

    if not isinstance(raw_plan, list):
        return []

    executable_plan: list[dict[str, Any]] = []

    for item in raw_plan:
        if not isinstance(item, dict):
            continue

        if item.get("action") != "rename_layer":
            continue

        if not item.get("id") or not item.get("new_name"):
            continue

        executable_plan.append(item)

    return executable_plan


def has_auto_fix_plan(state: OverallState) -> bool:
    return bool(get_auto_fix_plan_from_report(state.get("validation_report")))


def has_canvas_auto_fix_candidates(state: OverallState) -> bool:
    """Return True when canvas auto-fix may run without a rename phase.

    This is allowed only when the env flag is enabled and the validator has
    already mapped known targets with sufficient confidence.
    """
    validation_report = state.get("validation_report")
    if not canvas_auto_fix_enabled() or not isinstance(validation_report, dict):
        return False

    return bool(extract_known_canvas_targets(validation_report))


def has_executable_fix(state: OverallState) -> bool:
    """Return True when validate_and_fix has something executable to do.

    Rename still has priority. If no rename_layer plan exists, canvas auto-fix
    can run as a rename_phase=no_op only when the canvas flag is enabled and
    safe known targets exist.
    """
    return has_auto_fix_plan(state) or has_canvas_auto_fix_candidates(state)


def parse_tool_json_result(value: Any) -> dict[str, Any]:
    """Parse JSON-ish output returned by Penpot execute_code.

    MCP responses often wrap the real JSON inside {"text": "..."} and that
    inner JSON may wrap the plugin result again inside {"result": "..."}.
    This function recursively unwraps those envelopes before building events.
    """

    def parse_text(text: str) -> Any:
        stripped = text.strip()

        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

        start = stripped.find("{")
        end = stripped.rfind("}")

        if start >= 0 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                pass

        return None

    def unwrap(obj: Any, depth: int = 0) -> Any:
        if depth > 8:
            return obj

        if isinstance(obj, str):
            parsed = parse_text(obj)
            if parsed is None:
                return None
            return unwrap(parsed, depth + 1)

        if isinstance(obj, list):
            # MCP frequently returns a list like:
            # [{"type": "text", "text": "{\"result\": \"{...}\"}"}].
            # The useful JSON may be nested inside any item, so inspect all items.
            for item in obj:
                parsed_item = unwrap(item, depth + 1)
                if isinstance(parsed_item, dict):
                    return parsed_item
            return None

        if isinstance(obj, dict):
            # Prefer known MCP/text envelopes over returning the wrapper itself.
            for key in ("result", "text", "content"):
                inner = obj.get(key)
                if isinstance(inner, (str, dict, list)):
                    parsed_inner = unwrap(inner, depth + 1)
                    if isinstance(parsed_inner, dict):
                        return parsed_inner

            return obj

        return None

    parsed = unwrap(value)

    if isinstance(parsed, dict):
        return parsed

    text = stringify_tool_result(value).strip()
    parsed = unwrap(text)

    if isinstance(parsed, dict):
        return parsed

    return {
        "all_applied": False,
        "error": "could_not_parse_tool_result",
        "raw": text[:2000],
    }


def build_apply_rename_script(rename_plan: list[dict[str, Any]]) -> str:
    """Render the Penpot Plugin API script that applies rename_layer actions."""
    plan_json = json.dumps(rename_plan, ensure_ascii=False, default=str)
    return load_js("penpot_apply_rename_plan.js").replace(
        "__RENAME_PLAN_JSON__",
        plan_json,
    )


def build_verify_rename_script(expected_plan: list[dict[str, Any]]) -> str:
    """Render the read-only Penpot Plugin API script that verifies rename_layer actions."""
    expected_json = json.dumps(expected_plan, ensure_ascii=False, default=str)
    return load_js("penpot_verify_rename_plan.js").replace(
        "__EXPECTED_PLAN_JSON__",
        expected_json,
    )


def build_apply_canvas_fix_script(canvas_plan: list[dict[str, Any]]) -> str:
    """Render the Penpot Plugin API script that applies deterministic canvas edits."""
    plan_json = json.dumps(canvas_plan, ensure_ascii=False, default=str)
    return load_js("penpot_apply_canvas_fix_plan.js").replace(
        "__CANVAS_FIX_PLAN_JSON__",
        plan_json,
    )


def build_apply_semantic_fix_script(semantic_plan: list[dict[str, Any]]) -> str:
    """Render the Penpot Plugin API script that applies semantic/token edits."""
    plan_json = json.dumps(semantic_plan, ensure_ascii=False, default=str)
    return load_js("penpot_apply_semantic_fix_plan.js").replace(
        "__SEMANTIC_FIX_PLAN_JSON__",
        plan_json,
    )


def semantic_auto_fix_enabled() -> bool:
    """Semantic fixes create helper layers/groups, so they can be disabled explicitly.

    Default: enabled whenever canvas auto-fix is enabled.
    Set PENPOT_ENABLE_SEMANTIC_AUTO_FIX=0 to disable.
    """
    raw = os.getenv("PENPOT_ENABLE_SEMANTIC_AUTO_FIX")
    if raw is None:
        return canvas_auto_fix_enabled()
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def semantic_fallback_annotations_enabled() -> bool:
    """Allow visible DVCP annotations only as an explicit fallback/debug mode.

    Native Penpot assets/tokens are the preferred semantic representation.
    Set PENPOT_SEMANTIC_FALLBACK_ANNOTATIONS=1 to create visible helper panels
    when native APIs are unavailable or during debugging.
    """
    raw = os.getenv("PENPOT_SEMANTIC_FALLBACK_ANNOTATIONS", "0")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bbox(item: dict[str, Any]) -> dict[str, float]:
    raw = item.get("bbox") if isinstance(item, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    return {
        "x": _num(raw.get("x")),
        "y": _num(raw.get("y")),
        "width": _num(raw.get("width")),
        "height": _num(raw.get("height")),
    }


def _issue_refs(validation_report: Any, categories: set[str] | None = None) -> set[str]:
    if not isinstance(validation_report, dict):
        return set()

    refs: set[str] = set()
    for issue in validation_report.get("issues", []) or []:
        if not isinstance(issue, dict):
            continue
        category = str(issue.get("category") or "")
        if categories is not None and category not in categories:
            continue
        for layer in issue.get("affected_layers", []) or []:
            if isinstance(layer, dict) and layer.get("node_ref"):
                refs.add(str(layer["node_ref"]))
    return refs


def _role(item: dict[str, Any]) -> str:
    return str(item.get("inferred_role") or item.get("role") or "").lower()


def _region(item: dict[str, Any]) -> str:
    return str(item.get("visual_region") or "").lower()


def _target_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or "")


def _is_text(item: dict[str, Any]) -> bool:
    return str(item.get("type") or "").lower() == "text"


def _canonical_number(value: float) -> int:
    return int(round(value))


def _add_plan_action(
    plan: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    action: dict[str, Any],
) -> None:
    node_ref = str(action.get("node_ref") or "")
    action_type = str(action.get("action") or "")
    key = (node_ref, action_type)
    if not node_ref or not action_type or key in seen:
        return
    seen.add(key)
    plan.append(action)


def build_deterministic_canvas_fix_plan(
    validation_report: Any,
    known_targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a stronger deterministic canvas_fix_plan from the report.

    This planner is still conservative: it only targets known_targets mapped by the
    validator. Unlike the earlier version, it applies a clear login/form layout
    rhythm, centers paired text, and adds concrete contrast/focus affordance edits.
    """
    if not isinstance(validation_report, dict) or not known_targets:
        return []

    by_ref = {
        str(item.get("node_ref")): item
        for item in known_targets
        if isinstance(item, dict) and item.get("node_ref") and item.get("id")
    }
    if not by_ref:
        return []

    all_issue_refs = _issue_refs(validation_report)
    layout_refs = _issue_refs(validation_report, {"layout_spacing", "layout"})
    text_refs = _issue_refs(validation_report, {"text_legibility", "typography"})
    accessibility_refs = _issue_refs(validation_report, {"accessibility"})
    handoff_refs = _issue_refs(validation_report, {"frontend_handoff"})

    plan: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add_position(target: dict[str, Any], x: float, y: float, reason: str) -> None:
        bbox = _bbox(target)
        current_x = _canonical_number(bbox["x"])
        current_y = _canonical_number(bbox["y"])
        new_x = _canonical_number(x)
        new_y = _canonical_number(y)
        if current_x == new_x and current_y == new_y:
            return
        _add_plan_action(
            plan,
            seen,
            {
                "action": "set_position",
                "node_ref": target.get("node_ref"),
                "id": target.get("id"),
                "name": target.get("name"),
                "type": target.get("type"),
                "current": {"x": current_x, "y": current_y},
                "target": {"x": new_x, "y": new_y},
                "reason": reason,
                "safety": "safe_canvas_fix_known_target",
            },
        )

    def add_font_size(target: dict[str, Any], min_size: int, reason: str) -> None:
        if not _is_text(target):
            return
        _add_plan_action(
            plan,
            seen,
            {
                "action": "set_min_font_size",
                "node_ref": target.get("node_ref"),
                "id": target.get("id"),
                "name": target.get("name"),
                "type": target.get("type"),
                "min_font_size": min_size,
                "reason": reason,
                "safety": "safe_canvas_fix_known_target",
            },
        )

    def add_text_color(target: dict[str, Any], color: str, reason: str) -> None:
        if not _is_text(target):
            return
        _add_plan_action(
            plan,
            seen,
            {
                "action": "set_text_color",
                "node_ref": target.get("node_ref"),
                "id": target.get("id"),
                "name": target.get("name"),
                "type": target.get("type"),
                "text_color": color,
                "reason": reason,
                "safety": "safe_canvas_fix_known_target",
            },
        )

    def add_fill_color(target: dict[str, Any], color: str, reason: str) -> None:
        if str(target.get("type") or "").lower() == "text":
            return
        _add_plan_action(
            plan,
            seen,
            {
                "action": "set_fill_color",
                "node_ref": target.get("node_ref"),
                "id": target.get("id"),
                "name": target.get("name"),
                "type": target.get("type"),
                "fill_color": color,
                "reason": reason,
                "safety": "safe_canvas_fix_known_target",
            },
        )

    def add_focus_border(target: dict[str, Any], color: str, width: int, reason: str) -> None:
        if str(target.get("type") or "").lower() == "text":
            return
        _add_plan_action(
            plan,
            seen,
            {
                "action": "set_stroke",
                "node_ref": target.get("node_ref"),
                "id": target.get("id"),
                "name": target.get("name"),
                "type": target.get("type"),
                "stroke_color": color,
                "stroke_width": width,
                "reason": reason,
                "safety": "safe_canvas_fix_known_target",
            },
        )

    targets = list(by_ref.values())

    card = next(
        (
            t for t in targets
            if "card" in _role(t) or "card" in _region(t) or "container" in _role(t)
        ),
        None,
    )
    title = next(
        (
            t for t in targets
            if "heading" in _role(t) or "title" in _region(t) or "title" in _target_name(t).lower()
        ),
        None,
    )
    input_bgs = sorted(
        [
            t for t in targets
            if (
                "input" in _role(t)
                or "input_background" in _region(t)
                or "inputbackground" in _target_name(t).lower()
            )
            and str(t.get("type") or "").lower() != "text"
        ],
        key=lambda t: _bbox(t)["y"],
    )
    button_bg = next(
        (
            t for t in targets
            if ("button" in _role(t) or "button_background" in _region(t))
            and str(t.get("type") or "").lower() != "text"
        ),
        None,
    )
    button_text = next(
        (t for t in targets if "button_text" in _role(t) or "button_text" in _region(t)),
        None,
    )

    # Strong layout rhythm for a common login card.
    if layout_refs:
        cb = _bbox(card) if card else None
        card_x = cb["x"] if cb else min((_bbox(t)["x"] for t in targets), default=0)
        card_y = cb["y"] if cb else min((_bbox(t)["y"] for t in targets), default=0)
        card_w = cb["width"] if cb and cb["width"] else 0

        form_width = max(
            (_bbox(t)["width"] for t in input_bgs + ([button_bg] if button_bg else [])),
            default=0,
        )
        target_x = (
            card_x + max((card_w - form_width) / 2, 0)
            if card_w and form_width
            else min((_bbox(t)["x"] for t in input_bgs), default=0)
        )

        gap = 24
        if title:
            tb = _bbox(title)
            title_y = card_y + 40 if card_y else tb["y"]
            title_x = card_x + max((card_w - tb["width"]) / 2, 0) if card_w and tb["width"] else tb["x"]
            if str(title.get("node_ref")) in layout_refs or str(title.get("node_ref")) in all_issue_refs:
                add_position(title, title_x, title_y, "Apply 8px-grid title placement inside the card.")
            first_y = title_y + (tb["height"] or 28) + gap
        else:
            first_y = min((_bbox(t)["y"] for t in input_bgs), default=card_y + 96)

        y = first_y
        for input_bg in input_bgs:
            ref = str(input_bg.get("node_ref"))
            ib = _bbox(input_bg)
            if ref in layout_refs or ref in all_issue_refs:
                add_position(input_bg, target_x, y, "Apply consistent 24px vertical rhythm to form fields.")

            prefix = _region(input_bg).replace("_input_background", "")
            paired_label = next(
                (
                    t for t in targets
                    if _is_text(t)
                    and (
                        (prefix and prefix in _region(t))
                        or (prefix and prefix in _target_name(t).lower())
                    )
                ),
                None,
            )
            if paired_label:
                lb = _bbox(paired_label)
                label_x = target_x + 12
                label_y = y + max(((ib["height"] or 40) - (lb["height"] or 16)) / 2, 0)
                add_position(
                    paired_label,
                    label_x,
                    label_y,
                    "Center label vertically inside its input background.",
                )
            y += (ib["height"] or 40) + gap

        if button_bg:
            bb = _bbox(button_bg)
            button_y = y
            if str(button_bg.get("node_ref")) in layout_refs or str(button_bg.get("node_ref")) in all_issue_refs:
                add_position(button_bg, target_x, button_y, "Apply consistent 24px spacing before the primary button.")
            if button_text:
                tb = _bbox(button_text)
                text_x = target_x + max(((bb["width"] or form_width) - (tb["width"] or 0)) / 2, 0)
                text_y = button_y + max(((bb["height"] or 45) - (tb["height"] or 20)) / 2, 0)
                add_position(
                    button_text,
                    text_x,
                    text_y,
                    "Center button text inside the button background.",
                )

    # Typography and legibility.
    for target in targets:
        ref = str(target.get("node_ref"))
        role = _role(target)
        region = _region(target)
        name = _target_name(target).lower()
        if not _is_text(target):
            continue

        if "heading" in role or "title" in region or "title" in name:
            add_font_size(target, 24, "Use a minimum 24px heading size for hierarchy.")
            add_text_color(target, "#111827", "Use dark heading text for stronger contrast.")
        elif "button_text" in role or "button" in region:
            add_font_size(target, 16, "Use a minimum 16px button text size.")
            add_text_color(target, "#FFFFFF", "Use white button text over primary button fill.")
        elif ref in text_refs or ref in accessibility_refs or "label" in role or "label" in region:
            add_font_size(target, 14, "Use a minimum 14px label size.")
            add_text_color(target, "#111827", "Use dark label text for WCAG-oriented contrast.")

    # Accessibility and handoff affordances.
    for target in targets:
        ref = str(target.get("node_ref"))
        role = _role(target)
        region = _region(target)
        name = _target_name(target).lower()
        target_kind = " ".join([role, region, name])
        is_input = "input" in target_kind and not _is_text(target)
        is_button = "button" in target_kind and not _is_text(target)
        is_card = "card" in target_kind and not _is_text(target)

        if is_input and (ref in accessibility_refs or accessibility_refs or ref in handoff_refs):
            add_fill_color(target, "#FFFFFF", "Use white input fill for readable text contrast.")
            add_focus_border(target, "#94A3B8", 1, "Add visible input border/focus affordance.")
        elif is_button and (ref in accessibility_refs or ref in handoff_refs or handoff_refs):
            add_fill_color(target, "#2563EB", "Use primary button fill for clear call-to-action contrast.")
            add_focus_border(target, "#1D4ED8", 1, "Add visible button border/focus affordance.")
        elif is_card and ref in handoff_refs:
            add_fill_color(target, "#F8FAFC", "Use a neutral card fill to clarify the login surface.")

    return plan


def _semantic_issue_present(validation_report: Any) -> bool:
    if not isinstance(validation_report, dict):
        return False
    categories = {
        "componentization",
        "accessibility",
        "frontend_handoff",
        "design_tokens",
        "handoff",
    }
    for issue in validation_report.get("issues", []) or []:
        if isinstance(issue, dict) and str(issue.get("category") or "") in categories:
            return True
    return False


def _semantic_child(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_ref": item.get("node_ref"),
        "id": item.get("id"),
        "name": item.get("name"),
        "type": item.get("type"),
    }


def _bbox_union(items: list[dict[str, Any]]) -> dict[str, int]:
    boxes = [_bbox(item) for item in items if isinstance(item, dict)]
    if not boxes:
        return {"x": 0, "y": 0, "width": 0, "height": 0}
    min_x = min(box["x"] for box in boxes)
    min_y = min(box["y"] for box in boxes)
    max_x = max(box["x"] + box["width"] for box in boxes)
    max_y = max(box["y"] + box["height"] for box in boxes)
    return {
        "x": _canonical_number(min_x),
        "y": _canonical_number(min_y),
        "width": _canonical_number(max_x - min_x),
        "height": _canonical_number(max_y - min_y),
    }


def build_deterministic_semantic_fix_plan(
    validation_report: Any,
    known_targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build native Penpot semantic/tokenization fixes.

    Preferred representation:
    - native Penpot token set in Assets/Tokens;
    - native Penpot components/assets created from known target layers;
    - token bindings applied to existing layers.

    Visible DVCP annotation panels are now an explicit fallback/debug option,
    because they contaminate the main canvas. Enable them with:

        PENPOT_SEMANTIC_FALLBACK_ANNOTATIONS=1
    """
    if not semantic_auto_fix_enabled():
        return []

    if not isinstance(validation_report, dict) or not known_targets:
        return []

    if not _semantic_issue_present(validation_report):
        return []

    targets = [item for item in known_targets if isinstance(item, dict) and item.get("id")]
    if not targets:
        return []

    fallback_annotations = semantic_fallback_annotations_enabled()

    def kind(item: dict[str, Any]) -> str:
        return " ".join([
            _role(item),
            _region(item),
            _target_name(item).lower(),
        ])

    card = next((t for t in targets if "card" in kind(t) or "container" in kind(t)), None)
    title = next((t for t in targets if "title" in kind(t) or "heading" in kind(t)), None)
    email_bg = next((t for t in targets if "email" in kind(t) and "background" in kind(t) and not _is_text(t)), None)
    email_label = next((t for t in targets if "email" in kind(t) and _is_text(t)), None)
    password_bg = next((t for t in targets if "password" in kind(t) and "background" in kind(t) and not _is_text(t)), None)
    password_label = next((t for t in targets if "password" in kind(t) and _is_text(t)), None)
    button_bg = next((t for t in targets if "button" in kind(t) and "background" in kind(t) and not _is_text(t)), None)
    button_text = next((t for t in targets if "button" in kind(t) and _is_text(t)), None)

    plan: list[dict[str, Any]] = []

    token_specs = [
        {"name": "color.primary", "type": "color", "value": "#2563EB"},
        {"name": "color.primary.hover", "type": "color", "value": "#1D4ED8"},
        {"name": "color.text.default", "type": "color", "value": "#111827"},
        {"name": "color.text.inverse", "type": "color", "value": "#FFFFFF"},
        {"name": "color.border.input", "type": "color", "value": "#94A3B8"},
        {"name": "color.surface.input", "type": "color", "value": "#FFFFFF"},
        {"name": "spacing.12", "type": "spacing", "value": "12px"},
        {"name": "spacing.16", "type": "spacing", "value": "16px"},
        {"name": "spacing.24", "type": "spacing", "value": "24px"},
        {"name": "typography.heading.size", "type": "fontSizes", "value": "24px"},
        {"name": "typography.label.size", "type": "fontSizes", "value": "14px"},
        {"name": "typography.button.size", "type": "fontSizes", "value": "16px"},
        {"name": "border.input.width", "type": "borderWidth", "value": "1px"},
        {"name": "radius.input", "type": "borderRadius", "value": "6px"},
    ]

    plan.append({
        "action": "ensure_native_tokens",
        "set_name": "DVCP/Core",
        "tokens": token_specs,
        "fallback_annotations": fallback_annotations,
        "reason": "Create native Penpot tokens for colors, spacing, typography and borders.",
        "safety": "safe_native_token_library_write",
    })

    assignments: list[dict[str, Any]] = []

    def add_token_assignment(target: dict[str, Any] | None, token: str, properties: list[str], reason: str) -> None:
        if not isinstance(target, dict) or not target.get("id"):
            return
        assignments.append({
            "id": target.get("id"),
            "node_ref": target.get("node_ref"),
            "name": target.get("name"),
            "token": token,
            "properties": properties,
            "reason": reason,
        })

    add_token_assignment(title, "typography.heading.size", ["fontSize"], "Bind title typography size token.")
    add_token_assignment(title, "color.text.default", ["fill"], "Bind title text color token.")
    add_token_assignment(email_label, "typography.label.size", ["fontSize"], "Bind email label typography token.")
    add_token_assignment(email_label, "color.text.default", ["fill"], "Bind email label color token.")
    add_token_assignment(password_label, "typography.label.size", ["fontSize"], "Bind password label typography token.")
    add_token_assignment(password_label, "color.text.default", ["fill"], "Bind password label color token.")
    add_token_assignment(button_text, "typography.button.size", ["fontSize"], "Bind button text typography token.")
    add_token_assignment(button_text, "color.text.inverse", ["fill"], "Bind button text inverse color token.")
    for input_bg in [email_bg, password_bg]:
        add_token_assignment(input_bg, "color.surface.input", ["fill"], "Bind input surface token.")
        add_token_assignment(input_bg, "color.border.input", ["stroke"], "Bind input border token.")
        add_token_assignment(input_bg, "border.input.width", ["strokeWidth"], "Bind input border width token.")
        add_token_assignment(input_bg, "radius.input", ["borderRadius"], "Bind input radius token.")
    add_token_assignment(button_bg, "color.primary", ["fill"], "Bind primary button fill token.")
    add_token_assignment(button_bg, "color.primary.hover", ["stroke"], "Bind primary button stroke/hover token.")

    if assignments:
        plan.append({
            "action": "apply_native_tokens",
            "set_name": "DVCP/Core",
            "assignments": assignments,
            "fallback_annotations": fallback_annotations,
            "reason": "Bind native tokens to existing known target layers.",
            "safety": "safe_native_token_bindings_known_targets",
        })

    def native_component(name: str, role: str, children: list[dict[str, Any]], reason: str) -> None:
        clean_children = [c for c in children if isinstance(c, dict) and c.get("id")]
        if len(clean_children) < 2:
            return
        plan.append({
            "action": "create_native_component",
            "component_name": name,
            "semantic_role": role,
            "children": [_semantic_child(c) for c in clean_children],
            "bbox": _bbox_union(clean_children),
            "fallback_annotations": fallback_annotations,
            "reason": reason,
            "safety": "safe_native_component_from_known_targets",
        })

    native_component(
        "TextInput/Email",
        "text_input_component",
        [email_bg, email_label],
        "Create native Penpot asset/component for email input.",
    )
    native_component(
        "TextInput/Password",
        "text_input_component",
        [password_bg, password_label],
        "Create native Penpot asset/component for password input.",
    )
    native_component(
        "Button/Primary",
        "button_component",
        [button_bg, button_text],
        "Create native Penpot asset/component for primary login button.",
    )

    if card and title and button_bg:
        children = [item for item in [card, title, email_bg, email_label, password_bg, password_label, button_bg, button_text] if item]
        native_component(
            "Login/Card",
            "login_card_component",
            children,
            "Create native Penpot asset/component for the full login card pattern.",
        )

    # Store interaction semantics as native token metadata/spec where possible.
    # Visible state examples are now a fallback/debug concern, not the primary path.
    if button_bg:
        plan.append({
            "action": "ensure_native_component_state_metadata",
            "component_name": "Button/Primary",
            "states": [
                {"name": "default", "fill_token": "color.primary", "text_token": "color.text.inverse"},
                {"name": "hover", "fill_token": "color.primary.hover", "text_token": "color.text.inverse"},
                {"name": "disabled", "fill": "#CBD5E1", "text": "#64748B"},
                {"name": "focus", "stroke_token": "color.primary.hover", "stroke_width": 2},
            ],
            "fallback_annotations": fallback_annotations,
            "reason": "Document interaction states as native component metadata when supported.",
            "safety": "safe_native_component_metadata",
        })

    if fallback_annotations:
        cb = _bbox(card) if card else _bbox_union(targets)
        panel_x = _canonical_number(cb["x"] + cb["width"] + 80)
        panel_y = _canonical_number(cb["y"])
        plan.append({
            "action": "create_design_tokens_annotation",
            "name": "DesignTokensFallback",
            "semantic_role": "design_tokens_fallback",
            "bbox": {"x": panel_x, "y": panel_y, "width": 300, "height": 220},
            "tokens": {t["name"]: t["value"] for t in token_specs},
            "reason": "Fallback only: visible token documentation when native tokens are unavailable.",
            "safety": "safe_semantic_fix_create_helper_layer",
        })
        plan.append({
            "action": "create_handoff_annotation",
            "name": "HandoffNotesFallback",
            "semantic_role": "frontend_handoff_notes_fallback",
            "bbox": {"x": panel_x, "y": panel_y + 248, "width": 360, "height": 220},
            "text": "HandoffNotes\nNative target: Penpot Assets + Tokens\nComponents: TextInput/Email, TextInput/Password, Button/Primary, Login/Card\nStates: default, hover, focus, disabled\nAccessibility: labels grouped with inputs; focus state documented.",
            "reason": "Fallback only: visible handoff documentation when native assets/tokens are unavailable.",
            "safety": "safe_semantic_fix_create_helper_layer",
        })
        plan.append({
            "action": "create_component_index_annotation",
            "name": "ComponentIndexFallback",
            "semantic_role": "component_index_fallback",
            "bbox": {"x": panel_x, "y": panel_y + 496, "width": 360, "height": 160},
            "components": ["TextInput/Email", "TextInput/Password", "Button/Primary", "Login/Card", "DVCP/Core"],
            "reason": "Fallback only: visible index for validator/handoff.",
            "safety": "safe_semantic_fix_create_helper_layer",
        })

    return plan


def build_auto_fix_event(
    *,
    verification: dict[str, Any],
    fix_iteration: int,
) -> dict[str, Any]:
    all_applied = bool(verification.get("all_applied", False))
    error = verification.get("error")

    if error:
        status = "error"
    elif all_applied:
        status = "applied"
    elif int(verification.get("applied_count", 0) or 0) > 0:
        status = "partial"
    else:
        status = "not_applied"

    return {
        "type": "auto_fix_verification",
        "status": status,
        "verified_at": utc_now_iso(),
        "fix_iteration": fix_iteration,
        "checked_count": int(verification.get("checked_count", 0) or 0),
        "applied_count": int(verification.get("applied_count", 0) or 0),
        "failed_count": int(verification.get("failed_count", 0) or 0),
        "results": verification.get("results", []),
        "error": error,
    }


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
        "last_auto_fix_plan": [],
        "last_canvas_fix_targets": [],
        "last_canvas_fix_plan": [],
        "last_semantic_fix_plan": [],
        "canvas_auto_fix_result": None,
        "semantic_auto_fix_result": None,
        "post_fix_validation_mode": None,
        "auto_fix_verified": False,
        "auto_fix_event": None,
        "auto_fix_verification": None,
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
            SystemMessage(content=read_skill("builder.md")),
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
        validator_context = state.get("changeme") or ""

        validation_result = await validator_graph.ainvoke(
            {"changeme": validator_context}
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

    Importante: el validator sigue siendo stateless. Aquí copiamos el plan
    propuesto a last_auto_fix_plan únicamente para verificar esta ejecución.
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

    auto_fix_plan = get_auto_fix_plan_from_report(validation_report)
    is_canvas_mode = canvas_auto_fix_enabled()

    if not auto_fix_plan:
        known_targets = (
            extract_known_canvas_targets(validation_report)
            if is_canvas_mode and isinstance(validation_report, dict)
            else []
        )

        if not is_canvas_mode:
            message = (
                "El validation_report no contiene auto_fix_plan ejecutable y "
                "PENPOT_ENABLE_CANVAS_AUTO_FIX no está activo."
            )

            return {
                "messages": [AIMessage(content=message)],
                "response": message,
                "skip_validation": True,
            }

        if not known_targets:
            message = (
                "Canvas auto-fix omitido: no hay rename_layer pendiente ni "
                f"known_targets con confidence >= {canvas_confidence_threshold():.2f}."
            )

            return {
                "messages": [AIMessage(content=message)],
                "response": message,
                "skip_validation": True,
            }

        next_iteration = fix_iterations + 1
        rename_verification = {
            "all_applied": True,
            "rename_status": "not_needed",
            "checked_count": 0,
            "applied_count": 0,
            "failed_count": 0,
            "results": [],
            "reason": (
                "No rename_layer auto_fix_plan was generated; the canvas phase "
                "is allowed as rename_phase=no_op because safe known targets exist."
            ),
        }
        rename_event = build_auto_fix_event(
            verification=rename_verification,
            fix_iteration=next_iteration,
        )
        rename_event["status"] = "not_needed"
        rename_event["rename_status"] = "not_needed"

        canvas_fix_plan = build_deterministic_canvas_fix_plan(validation_report, known_targets)
        semantic_fix_plan = build_deterministic_semantic_fix_plan(validation_report, known_targets)

        if not canvas_fix_plan and not semantic_fix_plan:
            message = (
                "Auto-fix omitido: hay known_targets, pero no se pudo generar "
                "canvas_fix_plan ni semantic_fix_plan determinístico ejecutable."
            )
            return {
                "messages": [AIMessage(content=message)],
                "response": message,
                "skip_validation": True,
            }

        return {
            "fix_iterations": next_iteration,
            "last_auto_fix_plan": [],
            "last_canvas_fix_targets": known_targets,
            "last_canvas_fix_plan": canvas_fix_plan,
            "last_semantic_fix_plan": semantic_fix_plan,
            "post_fix_validation_mode": "canvas_fix_pending" if canvas_fix_plan else "semantic_fix_pending",
            "auto_fix_verified": True,
            "auto_fix_event": rename_event,
            "auto_fix_verification": rename_verification,
            "response": None,
            "skip_validation": False,
        }

    post_fix_mode = "structure_then_canvas" if is_canvas_mode else "structure_only"

    return {
        "fix_iterations": fix_iterations + 1,
        "last_auto_fix_plan": auto_fix_plan,
        "last_canvas_fix_targets": [],
        "post_fix_validation_mode": post_fix_mode,
        "response": None,
        "skip_validation": False,
    }


async def apply_auto_fix_plan_deterministic(
    state: OverallState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    """Apply rename_layer actions directly with Penpot execute_code.

    rename_layer is deterministic, safe and verifiable by id, so it should not
    depend on a builder LLM tool-calling turn. Broader canvas edits still go
    through the builder only after this phase is verified.
    """
    auto_fix_plan = state.get("last_auto_fix_plan", [])
    fix_iteration = int(state.get("fix_iterations", 0) or 0)

    if not isinstance(auto_fix_plan, list) or not auto_fix_plan:
        return {
            "post_fix_validation_mode": None,
            "response": "No hay last_auto_fix_plan para aplicar determinísticamente.",
        }

    await get_builder_tools()
    execute_code = _builder_tools_by_name.get("execute_code")

    if execute_code is None:
        verification = {
            "all_applied": False,
            "error": "execute_code_not_available",
            "checked_count": len(auto_fix_plan),
            "applied_count": 0,
            "failed_count": len(auto_fix_plan),
            "results": [],
        }
        event = build_auto_fix_event(
            verification=verification,
            fix_iteration=fix_iteration,
        )

        return {
            "auto_fix_verified": False,
            "auto_fix_event": event,
            "auto_fix_verification": verification,
            "post_fix_validation_mode": None,
            "response": "No se pudo aplicar rename_layer porque execute_code no está disponible.",
        }

    script = build_apply_rename_script(auto_fix_plan)

    try:
        raw_result = await execute_code.ainvoke({"code": script})
        application = parse_tool_json_result(raw_result)
    except Exception as exc:
        application = {
            "all_applied": False,
            "error": repr(exc),
            "checked_count": len(auto_fix_plan),
            "applied_count": 0,
            "failed_count": len(auto_fix_plan),
            "results": [],
        }

    if application.get("error"):
        event = build_auto_fix_event(
            verification=application,
            fix_iteration=fix_iteration,
        )

        return {
            "auto_fix_verified": False,
            "auto_fix_event": event,
            "auto_fix_verification": application,
            "post_fix_validation_mode": None,
            "response": "No se pudo aplicar rename_layer determinísticamente.",
        }

    return {
        "auto_fix_verification": {
            "type": "rename_apply_result",
            **application,
        },
        "response": None,
    }


async def verify_auto_fix_plan_applied(
    state: OverallState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    """
    Verifica si el auto_fix_plan aplicado por el builder realmente quedó en Penpot.

    No usa Mistral.
    No valida si el diseño está listo para desarrollo.
    No modifica validation_report / passed / score / status.
    Solo agrega una bandera y un evento con timestamp para debugging/persistencia futura.
    """

    auto_fix_plan = state.get("last_auto_fix_plan", [])
    fix_iteration = int(state.get("fix_iterations", 0) or 0)

    if not isinstance(auto_fix_plan, list) or not auto_fix_plan:
        verification = {
            "all_applied": False,
            "error": "missing_last_auto_fix_plan",
            "checked_count": 0,
            "applied_count": 0,
            "failed_count": 0,
            "results": [],
        }
        event = build_auto_fix_event(
            verification=verification,
            fix_iteration=fix_iteration,
        )

        return {
            "auto_fix_verified": False,
            "auto_fix_event": event,
            "auto_fix_verification": verification,
            "post_fix_validation_mode": None,
            "response": (
                "No se pudo verificar el auto-fix porque no hay "
                "last_auto_fix_plan en esta ejecución."
            ),
        }

    await get_builder_tools()
    execute_code = _builder_tools_by_name.get("execute_code")

    if execute_code is None:
        verification = {
            "all_applied": False,
            "error": "execute_code_not_available",
            "checked_count": len(auto_fix_plan),
            "applied_count": 0,
            "failed_count": len(auto_fix_plan),
            "results": [],
        }
        event = build_auto_fix_event(
            verification=verification,
            fix_iteration=fix_iteration,
        )

        return {
            "auto_fix_verified": False,
            "auto_fix_event": event,
            "auto_fix_verification": verification,
            "post_fix_validation_mode": None,
            "response": "No se pudo verificar el auto-fix porque execute_code no está disponible.",
        }

    script = build_verify_rename_script(auto_fix_plan)

    try:
        raw_result = await execute_code.ainvoke({"code": script})
        verification = parse_tool_json_result(raw_result)
    except Exception as exc:
        verification = {
            "all_applied": False,
            "error": repr(exc),
            "checked_count": len(auto_fix_plan),
            "applied_count": 0,
            "failed_count": len(auto_fix_plan),
            "results": [],
        }

    event = build_auto_fix_event(
        verification=verification,
        fix_iteration=fix_iteration,
    )
    all_applied = bool(verification.get("all_applied", False))

    if all_applied:
        response = (
            "Auto-fix verificado: todos los cambios automáticos de esta ejecución "
            "quedaron aplicados en Penpot. No se ejecutó una segunda validación "
            "visual; el validation_report conserva el resultado del validator."
        )
    else:
        response = (
            "Auto-fix no verificado completamente. Revisa auto_fix_event.results "
            "para ver qué capas no coinciden."
        )

    should_try_canvas = (
        all_applied
        and canvas_auto_fix_enabled()
        and state.get("post_fix_validation_mode") == "structure_then_canvas"
        and isinstance(state.get("validation_report"), dict)
    )

    if should_try_canvas:
        known_targets = extract_known_canvas_targets(state.get("validation_report"))

        if known_targets:
            canvas_fix_plan = build_deterministic_canvas_fix_plan(
                state["validation_report"],
                known_targets,
            )

            semantic_fix_plan = build_deterministic_semantic_fix_plan(
                state["validation_report"],
                known_targets,
            )

            if canvas_fix_plan or semantic_fix_plan:
                return {
                    "auto_fix_verified": all_applied,
                    "auto_fix_event": event,
                    "auto_fix_verification": verification,
                    "last_canvas_fix_targets": known_targets,
                    "last_canvas_fix_plan": canvas_fix_plan,
                    "last_semantic_fix_plan": semantic_fix_plan,
                    "post_fix_validation_mode": "canvas_fix_pending" if canvas_fix_plan else "semantic_fix_pending",
                    "response": None,
                }

            response += " Canvas/semantic auto-fix omitido: no se generó plan determinístico."

        response += (
            f" Canvas auto-fix omitido: no hay known_targets con "
            f"confidence >= {canvas_confidence_threshold():.2f}."
        )

    return {
        "auto_fix_verified": all_applied,
        "auto_fix_event": event,
        "auto_fix_verification": verification,
        "post_fix_validation_mode": None,
        "response": response,
    }



async def apply_canvas_fix_plan_deterministic(
    state: OverallState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    """Apply explicit canvas_fix_plan actions through Penpot execute_code.

    This replaces broad, generic builder instructions with concrete operations
    such as set_position, set_min_font_size and set_stroke. It still does not
    claim visual success; a fresh validate_only run is required for scoring.
    """
    canvas_fix_plan = state.get("last_canvas_fix_plan", [])

    if not isinstance(canvas_fix_plan, list) or not canvas_fix_plan:
        return {
            "canvas_auto_fix_result": {
                "all_applied": False,
                "error": "missing_canvas_fix_plan",
                "checked_count": 0,
                "applied_count": 0,
                "failed_count": 0,
                "results": [],
            },
            "post_fix_validation_mode": "canvas_auto_fix_unverified",
            "response": "No hay canvas_fix_plan determinístico para aplicar.",
        }

    await get_builder_tools()
    execute_code = _builder_tools_by_name.get("execute_code")

    if execute_code is None:
        return {
            "canvas_auto_fix_result": {
                "all_applied": False,
                "error": "execute_code_not_available",
                "checked_count": len(canvas_fix_plan),
                "applied_count": 0,
                "failed_count": len(canvas_fix_plan),
                "results": [],
            },
            "post_fix_validation_mode": "canvas_auto_fix_unverified",
            "response": "No se pudo aplicar canvas_fix_plan porque execute_code no está disponible.",
        }

    script = build_apply_canvas_fix_script(canvas_fix_plan)

    try:
        raw_result = await execute_code.ainvoke({"code": script})
        application = parse_tool_json_result(raw_result)
    except Exception as exc:
        application = {
            "all_applied": False,
            "error": repr(exc),
            "checked_count": len(canvas_fix_plan),
            "applied_count": 0,
            "failed_count": len(canvas_fix_plan),
            "results": [],
        }

    semantic_fix_plan = state.get("last_semantic_fix_plan", [])
    has_semantic_plan = isinstance(semantic_fix_plan, list) and bool(semantic_fix_plan)

    return {
        "canvas_auto_fix_result": {
            "type": "canvas_apply_result",
            **application,
        },
        "post_fix_validation_mode": "semantic_fix_pending" if has_semantic_plan else "canvas_auto_fix_unverified",
        "response": None,
    }


async def apply_semantic_fix_plan_deterministic(
    state: OverallState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    """Apply semantic/tokenization fixes through Penpot execute_code.

    This phase may create helper layers, annotations, state samples or groups.
    It still requires a fresh validate_only run for scoring.
    """
    semantic_fix_plan = state.get("last_semantic_fix_plan", [])

    if not isinstance(semantic_fix_plan, list) or not semantic_fix_plan:
        return {
            "semantic_auto_fix_result": {
                "all_applied": False,
                "error": "missing_semantic_fix_plan",
                "checked_count": 0,
                "applied_count": 0,
                "failed_count": 0,
                "results": [],
            },
            "post_fix_validation_mode": "canvas_auto_fix_unverified",
            "response": "No hay semantic_fix_plan determinístico para aplicar.",
        }

    await get_builder_tools()
    execute_code = _builder_tools_by_name.get("execute_code")

    if execute_code is None:
        return {
            "semantic_auto_fix_result": {
                "all_applied": False,
                "error": "execute_code_not_available",
                "checked_count": len(semantic_fix_plan),
                "applied_count": 0,
                "failed_count": len(semantic_fix_plan),
                "results": [],
            },
            "post_fix_validation_mode": "canvas_auto_fix_unverified",
            "response": "No se pudo aplicar semantic_fix_plan porque execute_code no está disponible.",
        }

    script = build_apply_semantic_fix_script(semantic_fix_plan)

    try:
        raw_result = await execute_code.ainvoke({"code": script})
        application = parse_tool_json_result(raw_result)
    except Exception as exc:
        application = {
            "all_applied": False,
            "error": repr(exc),
            "checked_count": len(semantic_fix_plan),
            "applied_count": 0,
            "failed_count": len(semantic_fix_plan),
            "results": [],
        }

    return {
        "semantic_auto_fix_result": {
            "type": "semantic_apply_result",
            **application,
        },
        "post_fix_validation_mode": "canvas_auto_fix_unverified",
        "response": None,
    }


async def record_canvas_auto_fix_event(
    state: OverallState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    """Record that broad canvas auto-fix mode ran.

    This does not validate the design again and does not claim deterministic
    success, because broad visual/layout edits cannot be verified with the
    rename-only structural checker.
    """
    fix_iteration = int(state.get("fix_iterations", 0) or 0)
    validation_report = state.get("validation_report")

    known_targets = state.get("last_canvas_fix_targets", [])
    canvas_fix_plan = state.get("last_canvas_fix_plan", [])
    canvas_result = state.get("canvas_auto_fix_result")
    semantic_fix_plan = state.get("last_semantic_fix_plan", [])
    semantic_result = state.get("semantic_auto_fix_result")
    rename_event = state.get("auto_fix_event")

    if isinstance(canvas_result, dict) and canvas_result.get("error"):
        canvas_status = "error_unverified"
    elif isinstance(canvas_result, dict) and canvas_result.get("applied_count"):
        canvas_status = "applied_unverified"
    else:
        canvas_status = "unverified"

    event = {
        "type": "canvas_auto_fix_execution",
        "status": canvas_status,
        "verified_at": utc_now_iso(),
        "fix_iteration": fix_iteration,
        "mode": "canvas_auto_fix_known_targets_only",
        "env_flag": "PENPOT_ENABLE_CANVAS_AUTO_FIX",
        "confidence_threshold": canvas_confidence_threshold(),
        "known_target_count": len(known_targets) if isinstance(known_targets, list) else 0,
        "known_targets": known_targets if isinstance(known_targets, list) else [],
        "canvas_fix_plan_count": len(canvas_fix_plan) if isinstance(canvas_fix_plan, list) else 0,
        "canvas_fix_plan": canvas_fix_plan if isinstance(canvas_fix_plan, list) else [],
        "canvas_apply_result": canvas_result if isinstance(canvas_result, dict) else None,
        "semantic_fix_plan_count": len(semantic_fix_plan) if isinstance(semantic_fix_plan, list) else 0,
        "semantic_fix_plan": semantic_fix_plan if isinstance(semantic_fix_plan, list) else [],
        "semantic_apply_result": semantic_result if isinstance(semantic_result, dict) else None,
        "rename_verification_event": rename_event,
        "message": (
            "Canvas/semantic auto-fix mode ran after rename_layer fixes were "
            "structurally verified, or after rename_phase=no_op when no rename "
            "was needed. The executor was allowed to modify known targets and "
            "create bounded semantic/token helper artifacts for handoff. "
            "Run validate_only to score the updated design."
        ),
        "score_before_fix": (
            validation_report.get("score") if isinstance(validation_report, dict) else None
        ),
        "status_before_fix": (
            validation_report.get("status") if isinstance(validation_report, dict) else None
        ),
    }

    return {
        "auto_fix_verified": False,
        "auto_fix_event": event,
        "auto_fix_verification": {
            "all_applied": None,
            "verification_type": "canvas_auto_fix_unverified",
            "reason": "known-target canvas/semantic changes require a fresh validate_only run",
            "canvas_apply_result": canvas_result if isinstance(canvas_result, dict) else None,
            "semantic_apply_result": semantic_result if isinstance(semantic_result, dict) else None,
        },
        "post_fix_validation_mode": None,
        "response": (
            "Canvas/semantic auto-fix ejecutado en modo ampliado. "
            "Se aplicaron cambios visuales y artefactos semánticos/tokens cuando existía plan. "
            "Corre validate_only para obtener un nuevo score del diseño actualizado."
        ),
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
) -> Literal["tool_node", "run_validator", "verify_auto_fix_plan_applied", "apply_canvas_fix_plan_deterministic", "apply_semantic_fix_plan_deterministic", "record_canvas_auto_fix_event", "__end__"]:
    if state.get("skip_validation"):
        return END

    messages = state.get("messages", [])

    if not messages:
        return END

    last_message = messages[-1]

    if getattr(last_message, "tool_calls", None):
        return "tool_node"

    post_fix_mode = state.get("post_fix_validation_mode")

    if post_fix_mode in {"structure_only", "structure_then_canvas"}:
        return "verify_auto_fix_plan_applied"

    if post_fix_mode == "canvas_fix_pending":
        return "apply_canvas_fix_plan_deterministic"

    if post_fix_mode == "semantic_fix_pending":
        return "apply_semantic_fix_plan_deterministic"

    if post_fix_mode == "canvas_auto_fix_unverified":
        return "record_canvas_auto_fix_event"

    action = normalize_action(state.get("action"))

    if action_requires_validation_after_build(action):
        return "run_validator"

    return END


def route_after_fix_design(
    state: OverallState,
) -> Literal["apply_auto_fix_plan_deterministic", "apply_canvas_fix_plan_deterministic", "apply_semantic_fix_plan_deterministic", "__end__"]:
    post_fix_mode = state.get("post_fix_validation_mode")

    if post_fix_mode == "canvas_fix_pending":
        return "apply_canvas_fix_plan_deterministic"

    if post_fix_mode == "semantic_fix_pending":
        return "apply_semantic_fix_plan_deterministic"

    if post_fix_mode in {"structure_only", "structure_then_canvas"}:
        return "apply_auto_fix_plan_deterministic"

    return END


def route_after_auto_fix_verification(
    state: OverallState,
) -> Literal["apply_canvas_fix_plan_deterministic", "apply_semantic_fix_plan_deterministic", "__end__"]:
    if state.get("post_fix_validation_mode") == "canvas_fix_pending":
        return "apply_canvas_fix_plan_deterministic"

    if state.get("post_fix_validation_mode") == "semantic_fix_pending":
        return "apply_semantic_fix_plan_deterministic"

    return END


def route_after_validator(
    state: OverallState,
) -> Literal["fix_design", "__end__"]:
    action = normalize_action(state.get("action"))

    if not action_allows_fixing(action):
        return END

    if validation_passed(state):
        return END

    if not has_executable_fix(state):
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
agent_builder.add_node("apply_auto_fix_plan_deterministic", apply_auto_fix_plan_deterministic)
agent_builder.add_node("verify_auto_fix_plan_applied", verify_auto_fix_plan_applied)
agent_builder.add_node("apply_canvas_fix_plan_deterministic", apply_canvas_fix_plan_deterministic)
agent_builder.add_node("apply_semantic_fix_plan_deterministic", apply_semantic_fix_plan_deterministic)
agent_builder.add_node("record_canvas_auto_fix_event", record_canvas_auto_fix_event)

agent_builder.add_edge(START, "prepare_input")

agent_builder.add_conditional_edges(
    "prepare_input",
    route_after_prepare,
    ["llm_call", "run_validator"],
)

agent_builder.add_conditional_edges(
    "llm_call",
    should_continue_after_llm,
    [
        "tool_node",
        "run_validator",
        "verify_auto_fix_plan_applied",
        "apply_canvas_fix_plan_deterministic",
        "apply_semantic_fix_plan_deterministic",
        "record_canvas_auto_fix_event",
        END,
    ],
)

agent_builder.add_edge("tool_node", "llm_call")

agent_builder.add_conditional_edges(
    "run_validator",
    route_after_validator,
    ["fix_design", END],
)

agent_builder.add_conditional_edges(
    "fix_design",
    route_after_fix_design,
    ["apply_auto_fix_plan_deterministic", "apply_canvas_fix_plan_deterministic", "apply_semantic_fix_plan_deterministic", END],
)
agent_builder.add_edge("apply_auto_fix_plan_deterministic", "verify_auto_fix_plan_applied")
agent_builder.add_conditional_edges(
    "verify_auto_fix_plan_applied",
    route_after_auto_fix_verification,
    ["apply_canvas_fix_plan_deterministic", "apply_semantic_fix_plan_deterministic", END],
)
agent_builder.add_conditional_edges(
    "apply_canvas_fix_plan_deterministic",
    lambda state: "apply_semantic_fix_plan_deterministic" if state.get("post_fix_validation_mode") == "semantic_fix_pending" else "record_canvas_auto_fix_event",
    ["apply_semantic_fix_plan_deterministic", "record_canvas_auto_fix_event"],
)
agent_builder.add_edge("apply_semantic_fix_plan_deterministic", "record_canvas_auto_fix_event")
agent_builder.add_edge("record_canvas_auto_fix_event", END)

graph = agent_builder.compile(name="Penpot Design Workflow")
