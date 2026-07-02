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
from agent.utils.stitch_external_design import build_external_design_spec_from_stitch
from agent.utils.stitch_mcp_client import fetch_existing_stitch_screen
from agent.utils.stitch_import_queue import (
    aggregate_stitch_import_queue_results,
    build_stitch_import_queue,
    compact_imported_design_spec_for_output,
)
from agent.utils.stitch_llm_memory import (
    build_stitch_llm_memory,
    build_stitch_llm_planner_messages,
    build_stitch_visual_diff_messages,
    compact_memory_for_output,
    compact_visual_nodes_for_diff,
    parse_visual_diff_report,
)
from agent.utils.stitch_llm_planner import (
    build_external_design_spec_from_deterministic_transform,
    build_external_design_spec_from_llm_plan,
    parse_llm_build_plan,
    sanitize_external_design_spec_for_import,
)
from agent.utils.penpot_image_export import (
    assert_valid_png_base64,
    extract_png_base64_from_export_shape,
    png_base64_to_data_url,
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
    "validate_and_polish",
    "build_validate_and_fix",
    "import_from_stitch",
]


def normalize_action(action: str | None) -> str:
    if not action:
        return "build_and_validate"

    allowed_actions = {
        "build",
        "validate_only",
        "build_and_validate",
        "validate_and_fix",
        "validate_and_polish",
        "build_validate_and_fix",
        "import_from_stitch",
    }

    # Backwards-compatible aliases from older debug builds.
    # Public Stitch UX is intentionally a single action: import_from_stitch.
    stitch_aliases = {
        "import_from_stitch_and_validate",
        "import_from_stitch_queue",
        "import_from_stitch_queue_and_validate",
        "prepare_stitch_import_queue",
        "run_stitch_import_queue",
        "resume_stitch_import_queue",
        "import_from_stitch_llm_guided",
    }
    if action in stitch_aliases:
        return "import_from_stitch"

    if action not in allowed_actions:
        return "build_and_validate"

    return action


def action_starts_with_validation(action: str) -> bool:
    return action in {
        "validate_only",
        "validate_and_fix",
        "validate_and_polish",
    }


def action_requires_validation_after_build(action: str) -> bool:
    return action in {
        "build_and_validate",
        "validate_and_fix",
        "validate_and_polish",
        "build_validate_and_fix",
    }


def action_allows_fixing(action: str) -> bool:
    return action in {
        "validate_and_fix",
        "validate_and_polish",
        "build_validate_and_fix",
    }


def action_is_polish(action: str) -> bool:
    return action == "validate_and_polish"


def action_imports_from_stitch(action: str) -> bool:
    # Public Stitch UX: one action only. Queue/LLM/rendered/visual steps are internal.
    return action == "import_from_stitch"


def action_requires_validation_after_import(action: str) -> bool:
    # Import has its own internal plan/execution summary. Run the normal validator
    # only when explicitly enabled to avoid surprising writes/loops after import.
    if action != "import_from_stitch":
        return False
    return os.getenv("STITCH_IMPORT_RUN_VALIDATOR", "0").strip().lower() in {"1", "true", "yes", "on"}


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
    stitch_project_id: str
    stitch_project_name: str
    stitch_screen_id: str
    stitch_screen_name: str


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

    stitch_import_result: dict[str, Any] | None
    imported_design_spec: dict[str, Any] | None
    stitch_import_queue: dict[str, Any] | None
    stitch_import_queue_result: dict[str, Any] | None
    stitch_llm_memory: dict[str, Any] | None
    stitch_llm_plan_summary: dict[str, Any] | None
    stitch_visual_diff_report: dict[str, Any] | None
    stitch_source_trace_report: dict[str, Any] | None
    stitch_source_to_penpot_map: list[dict[str, Any]] | None

    stitch_project_id: str | None
    stitch_project_name: str | None
    stitch_screen_id: str | None
    stitch_screen_name: str | None


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
    stitch_import_result: NotRequired[dict[str, Any]]
    imported_design_spec: NotRequired[dict[str, Any]]
    stitch_import_queue: NotRequired[dict[str, Any]]
    stitch_import_queue_result: NotRequired[dict[str, Any]]
    stitch_llm_memory: NotRequired[dict[str, Any]]
    stitch_llm_plan_summary: NotRequired[dict[str, Any]]
    stitch_visual_diff_report: NotRequired[dict[str, Any] | None]
    stitch_source_trace_report: NotRequired[dict[str, Any] | None]
    stitch_source_to_penpot_map: NotRequired[list[dict[str, Any]] | None]
    stitch_project_id: NotRequired[str | None]
    stitch_project_name: NotRequired[str | None]
    stitch_screen_id: NotRequired[str | None]
    stitch_screen_name: NotRequired[str | None]
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


def merge_metered_usage_updates(
    state: dict[str, Any],
    current_updates: dict[str, Any],
    metered_result: Any,
) -> dict[str, Any]:
    """Accumulate LLM usage across multiple metered calls in one node."""
    usage_state = {
        "input_tokens": current_updates.get("input_tokens", state.get("input_tokens", 0)),
        "output_tokens": current_updates.get("output_tokens", state.get("output_tokens", 0)),
        "total_tokens": current_updates.get("total_tokens", state.get("total_tokens", 0)),
    }
    current_updates.update(usage_updates_from_metered_result(usage_state, metered_result))
    return current_updates


def stitch_visual_diff_enabled() -> bool:
    return os.getenv("STITCH_IMPORT_VISUAL_DIFF", "1").strip().lower() in {"1", "true", "yes", "on"}


def make_visual_diff_status(
    status: str,
    *,
    reason: str | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = {
        "schema": "dvcp.stitch_visual_diff_report.v1",
        "status": status,
        "mode": "report_only",
        "compared_at": utc_now_iso(),
        "issues": [],
    }
    if reason:
        report["reason"] = reason
    if error:
        report["error"] = error
    if extra:
        report.update(extra)
    return report


def root_shape_id_from_import_job(job: dict[str, Any]) -> str | None:
    for result in job.get("results") or []:
        if not isinstance(result, dict):
            continue
        if result.get("op") == "create_root":
            ids = result.get("created_shape_ids") or []
            if ids:
                return str(ids[0])
    ids = job.get("created_shape_ids") or []
    return str(ids[0]) if ids else None


def compact_queue_summary_for_visual_diff(queue_result: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "all_applied", "status", "total_ops", "created_shape_count",
        "failed_count", "component_assembly_summary", "visual_materialization_summary",
    ]
    return {key: queue_result.get(key) for key in keys if key in queue_result}


async def build_stitch_visual_diff_report(
    *,
    state: OverallState,
    job: dict[str, Any],
    spec: dict[str, Any] | None,
    stitch_memory: dict[str, Any] | None,
    queue_result: dict[str, Any],
    export_shape_tool: Any | None,
    token_updates: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Export the imported Penpot root and ask the LLM for a visual diff report.

    This is intentionally report-only. It never applies fixes and never changes
    the imported design. It is enabled by default and can be disabled with
    STITCH_IMPORT_VISUAL_DIFF=0.
    """
    if not stitch_visual_diff_enabled():
        return make_visual_diff_status("skipped", reason="disabled_by_STITCH_IMPORT_VISUAL_DIFF"), token_updates

    reference_data_url = str((((stitch_memory or {}).get("reference_image") or {}).get("data_url") or ""))
    if not reference_data_url:
        return make_visual_diff_status("skipped", reason="reference_stitch_screenshot_unavailable"), token_updates

    if export_shape_tool is None:
        return make_visual_diff_status("skipped", reason="export_shape_not_available"), token_updates

    shape_id = os.getenv("STITCH_IMPORT_VISUAL_DIFF_SHAPE_ID", "").strip() or root_shape_id_from_import_job(job)
    if not shape_id:
        return make_visual_diff_status("skipped", reason="imported_root_shape_id_unavailable"), token_updates

    try:
        export_result = await export_shape_tool.ainvoke({"shapeId": shape_id, "format": "png", "mode": "shape"})
        penpot_b64 = extract_png_base64_from_export_shape(export_result)
        if not penpot_b64:
            return make_visual_diff_status(
                "error",
                error="export_shape_returned_no_png_base64",
                extra={"shape_id": shape_id},
            ), token_updates
        assert_valid_png_base64(penpot_b64)
        penpot_data_url = png_base64_to_data_url(penpot_b64)
    except Exception as exc:  # noqa: BLE001
        return make_visual_diff_status(
            "error",
            error=f"penpot_export_failed: {exc!r}",
            extra={"shape_id": shape_id},
        ), token_updates

    screen_summary = {
        "screen_name": (spec or {}).get("screen_name") if isinstance(spec, dict) else job.get("screen_name"),
        "screen_title": (spec or {}).get("screen_title") if isinstance(spec, dict) else job.get("screen_title"),
        "screen_type": (spec or {}).get("screen_type") if isinstance(spec, dict) else job.get("screen_type"),
        "width": (spec or {}).get("width") if isinstance(spec, dict) else job.get("width"),
        "height": (spec or {}).get("height") if isinstance(spec, dict) else job.get("height"),
        "tokens": (spec or {}).get("tokens", {}) if isinstance(spec, dict) else {},
        "exported_shape_id": shape_id,
    }
    visual_nodes = compact_visual_nodes_for_diff(job, spec)
    messages = build_stitch_visual_diff_messages(
        reference_image_data_url=reference_data_url,
        penpot_image_data_url=penpot_data_url,
        screen_summary=screen_summary,
        visual_nodes=visual_nodes,
        queue_summary=compact_queue_summary_for_visual_diff(queue_result),
    )

    try:
        metered = await metered_ainvoke(
            llm,
            messages,
            estimated_completion_tokens=int(os.getenv("STITCH_IMPORT_VISUAL_DIFF_COMPLETION_TOKENS", "2400")),
        )
        token_updates = merge_metered_usage_updates(state, token_updates, metered)
        report = parse_visual_diff_report(content_to_text(metered.ai_message.content))
        report.setdefault("mode", "report_only")
        report.setdefault("compared_at", utc_now_iso())
        report["export"] = {
            "shape_id": shape_id,
            "format": "png",
            "reference_image_available": True,
            "penpot_image_base64_length": len(penpot_b64),
        }
        report["node_count"] = len(visual_nodes)
        report["queue_summary"] = compact_queue_summary_for_visual_diff(queue_result)
        return report, token_updates
    except Exception as exc:  # noqa: BLE001
        return make_visual_diff_status(
            "error",
            error=f"visual_diff_llm_failed: {exc!r}",
            extra={"shape_id": shape_id, "node_count": len(visual_nodes)},
        ), token_updates


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


def has_semantic_polish_candidates(state: OverallState) -> bool:
    """Return True when validate_and_polish can run a semantic polish plan.

    This path intentionally ignores the normal `passed=true -> END` shortcut.
    It is used after the design already passes, but still has minor/medium
    issues around native tokens, interactive states, component metadata or
    handoff documentation.
    """
    validation_report = state.get("validation_report")
    if not isinstance(validation_report, dict):
        return False
    if not _semantic_issue_present(validation_report):
        return False
    return bool(extract_known_canvas_targets(validation_report))


def parse_tool_json_result(value: Any) -> dict[str, Any]:
    """Parse JSON-ish output returned by Penpot execute_code.

    Important: some MCP responses are wrappers like
    {"type":"text", "text":"...", "id":"..."}. The old parser returned that
    wrapper as if it were the plugin result, which produced null batch counts and
    stopped the batched Stitch import after the first batch. This parser only
    accepts dictionaries that look like a DVCP/Penpot result. Otherwise it returns
    a clear diagnostic with a raw preview.
    """

    def looks_like_result(obj: Any) -> bool:
        if not isinstance(obj, dict):
            return False
        result_keys = {
            "all_applied",
            "action",
            "checked_count",
            "applied_count",
            "failed_count",
            "created_shape_count",
            "__dvcp_result_marker",
            "__dvcp_import_op_marker",
            "job_id",
            "op_index",
        }
        return bool(result_keys.intersection(obj.keys()))

    def parse_text(text: str) -> Any:
        stripped = text.strip()
        if not stripped:
            return None

        # Common successful shape: the script returns a JSON string.
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

        # Some MCP wrappers prepend/append non-JSON text. Recover the largest
        # object if possible.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                pass

        return None

    def unwrap(obj: Any, depth: int = 0) -> Any:
        if depth > 10:
            return None

        if looks_like_result(obj):
            return obj

        if isinstance(obj, str):
            parsed = parse_text(obj)
            if parsed is None:
                return None
            return unwrap(parsed, depth + 1)

        if isinstance(obj, list):
            for item in obj:
                parsed_item = unwrap(item, depth + 1)
                if looks_like_result(parsed_item):
                    return parsed_item
            return None

        if isinstance(obj, dict):
            # Prefer known MCP/text envelopes over returning the wrapper itself.
            for key in ("result", "text", "content", "structuredContent"):
                inner = obj.get(key)
                if isinstance(inner, (str, dict, list)):
                    parsed_inner = unwrap(inner, depth + 1)
                    if looks_like_result(parsed_inner):
                        return parsed_inner

            # If this is a wrapper like {type,text,id}, do not accept it as a
            # result. Return None so the caller gets a useful raw preview.
            if set(obj.keys()).issubset({"type", "text", "id", "name", "mimeType"}):
                return None

            # As a last resort, accept a dict only when it has explicit error
            # information and not merely MCP wrapper fields.
            if obj.get("error") and not {"type", "text"}.issubset(obj.keys()):
                return obj

        return None

    parsed = unwrap(value)
    if looks_like_result(parsed) or (isinstance(parsed, dict) and parsed.get("error")):
        return parsed

    text = stringify_tool_result(value).strip()
    parsed = unwrap(text)
    if looks_like_result(parsed) or (isinstance(parsed, dict) and parsed.get("error")):
        return parsed

    return {
        "all_applied": False,
        "action": "import_external_design_spec",
        "error": "could_not_parse_tool_result",
        "checked_count": 0,
        "applied_count": 0,
        "failed_count": 1,
        "created_shape_count": 0,
        "raw_type": type(value).__name__,
        "raw_preview": text[:2000],
    }


def build_apply_rename_script(rename_plan: list[dict[str, Any]]) -> str:
    """Render the Penpot Plugin API script that applies rename_layer actions."""
    plan_json = json.dumps(rename_plan, ensure_ascii=False, default=str)
    return load_js("penpot_apply_rename_plan.js").replace(
        "__RENAME_PLAN_JSON__",
        plan_json,
    )


def build_import_external_design_script(spec: dict[str, Any]) -> str:
    """Render the legacy Penpot Plugin API script that imports an ExternalDesignSpec."""
    spec_json = json.dumps(spec, ensure_ascii=False, default=str)
    return load_js("penpot_import_external_design_spec.js").replace(
        "__EXTERNAL_DESIGN_SPEC_JSON__",
        spec_json,
    )


def build_apply_import_op_script(op: dict[str, Any]) -> str:
    """Render one tiny queue operation for Penpot execute_code.

    execute_code interprets code as a function body, so the JS template must
    return directly and must not wrap the result in an IIFE.
    """
    op_json = json.dumps(op, ensure_ascii=False, default=str)
    return load_js("penpot_apply_import_op.js").replace(
        "__IMPORT_OP_JSON__",
        op_json,
    )


def _safe_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except Exception:
        return default
    return max(value, 1)


def _compact_stitch_metadata_for_penpot(metadata: Any) -> dict[str, Any]:
    """Keep import metadata small for Penpot execute_code.

    This does not drop design content. It only removes debug payloads such as
    html_preview/extracted_elements_preview from the script sent to Penpot, which
    can trigger MCP timeouts.
    """
    if not isinstance(metadata, dict):
        return {}
    keys = {
        "stitch_mode",
        "stitch_project_id",
        "stitch_project_name",
        "stitch_screen_id",
        "stitch_screen_name",
        "stitch_screen_resource",
        "selection_hint",
        "device_type",
        "html_file",
        "html_download_url_present",
        "screenshot_file",
        "screenshot_download_url_present",
        "html_bytes_read",
        "html_content_type",
        "extracted_element_count",
    }
    return {key: metadata.get(key) for key in keys if key in metadata}


def _spec_for_penpot_batch(spec: dict[str, Any], children: list[dict[str, Any]], index: int, total: int, start: int, end: int) -> dict[str, Any]:
    chunk = dict(spec)
    chunk["children"] = children
    chunk["metadata"] = _compact_stitch_metadata_for_penpot(spec.get("metadata"))
    chunk["_dvcp_batch"] = {
        "index": index,
        "total": total,
        "start": start,
        "end": end,
        "child_count": len(spec.get("children") or []),
        "create_root": index == 0,
    }
    return chunk


def split_external_design_spec_for_penpot(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Split one ExternalDesignSpec into Penpot-safe execute_code batches.

    This is not a design limit. Every child is imported; the only difference is
    that we avoid a single long-running MCP call by applying chunks sequentially.
    """
    children_raw = spec.get("children") or []
    children = [child for child in children_raw if isinstance(child, dict)]
    batch_size = _safe_int_env("STITCH_IMPORT_BATCH_SIZE", 8)

    if not children:
        return [_spec_for_penpot_batch(spec, [], 0, 1, 0, 0)]

    total = (len(children) + batch_size - 1) // batch_size
    chunks: list[dict[str, Any]] = []
    for index in range(total):
        start = index * batch_size
        end = min(start + batch_size, len(children))
        chunks.append(_spec_for_penpot_batch(spec, children[start:end], index, total, start, end))
    return chunks


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def aggregate_penpot_batch_results(batch_results: list[dict[str, Any]]) -> dict[str, Any]:
    checked = sum(_as_int(item.get("checked_count")) for item in batch_results)
    applied = sum(_as_int(item.get("applied_count")) for item in batch_results)
    failed = sum(_as_int(item.get("failed_count")) for item in batch_results)
    created_shapes = sum(_as_int(item.get("created_shape_count")) for item in batch_results)
    all_applied = bool(batch_results) and all(bool(item.get("all_applied")) for item in batch_results)
    first_error = next((item.get("error") for item in batch_results if item.get("error")), None)

    return {
        "all_applied": all_applied,
        "action": "import_external_design_spec",
        "import_strategy": "batched_execute_code",
        "batch_count": len(batch_results),
        "checked_count": checked,
        "applied_count": applied,
        "failed_count": failed,
        "created_shape_count": created_shapes,
        "batches": [
            {
                "batch_index": item.get("batch_index"),
                "batch_total": item.get("batch_total"),
                "batch_start": item.get("batch_start"),
                "batch_end": item.get("batch_end"),
                "all_applied": item.get("all_applied"),
                "checked_count": item.get("checked_count"),
                "applied_count": item.get("applied_count"),
                "failed_count": item.get("failed_count"),
                "created_shape_count": item.get("created_shape_count"),
                "error": item.get("error"),
                "raw_type": item.get("raw_type"),
                "raw_preview": item.get("raw_preview"),
                "message": item.get("message"),
            }
            for item in batch_results
        ],
        "error": None if all_applied else (first_error or "one_or_more_batches_failed"),
    }


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
        "layout_spacing",
        "text_legibility",
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


def _slug_words(value: Any, *, max_words: int = 3) -> str:
    raw = str(value or "").strip()
    cleaned = "".join(ch if ch.isalnum() else " " for ch in raw)
    words = [w for w in cleaned.split() if w]
    if not words:
        return "Generic"
    return "".join(w[:1].upper() + w[1:] for w in words[:max_words])


def _semantic_kind(item: dict[str, Any]) -> str:
    return " ".join([
        _role(item),
        _region(item),
        _target_name(item).lower(),
        str(item.get("path") or "").lower(),
        str(item.get("text") or "").lower(),
    ])


def _classify_ui_role(item: dict[str, Any]) -> str:
    text = _semantic_kind(item)
    if any(t in text for t in ["button", "btn", "cta", "submit", "primary", "secondary"]):
        return "button"
    if any(t in text for t in ["input", "field", "textbox", "search", "email", "password", "form", "select"]):
        if _is_text(item) and any(t in text for t in ["label", "placeholder"]):
            return "label"
        return "input"
    if any(t in text for t in ["checkbox", "toggle", "switch", "radio"]):
        return "control"
    if any(t in text for t in ["table", "row", "column", "cell", "grid"]):
        return "table"
    if any(t in text for t in ["chart", "graph", "metric", "kpi", "stat"]):
        return "data_viz"
    if any(t in text for t in ["card", "tile", "panel"]):
        return "card"
    if any(t in text for t in ["nav", "sidebar", "menu", "tab", "breadcrumb"]):
        return "navigation"
    if any(t in text for t in ["header", "topbar", "toolbar", "footer"]):
        return "layout"
    if any(t in text for t in ["image", "avatar", "photo", "thumbnail", "icon", "logo"]):
        return "media"
    if _is_text(item) and any(t in text for t in ["heading", "title", "headline", "h1", "h2"]):
        return "heading"
    if _is_text(item):
        return "text"
    if any(t in text for t in ["container", "frame", "board", "background", "surface"]):
        return "surface"
    return "component"


def _component_family_for_role(role: str) -> str:
    return {
        "button": "Button",
        "input": "Input",
        "control": "Control",
        "table": "Table",
        "data_viz": "DataViz",
        "card": "Card",
        "navigation": "Navigation",
        "layout": "Layout",
        "media": "Media",
        "surface": "Surface",
        "heading": "Typography",
        "text": "Typography",
        "component": "Component",
    }.get(role, "Component")


def _component_variant_from_target(item: dict[str, Any], role: str, index: int) -> str:
    text = _semantic_kind(item)
    # Stable variants by common UI semantics, without assuming a screen template.
    if role == "button":
        if any(t in text for t in ["primary", "submit", "login", "save", "confirm", "cta"]):
            return "Primary"
        if "secondary" in text:
            return "Secondary"
        if any(t in text for t in ["danger", "delete", "remove"]):
            return "Danger"
    if role == "input":
        for token, label in [
            ("email", "Email"), ("password", "Password"), ("search", "Search"),
            ("name", "Name"), ("phone", "Phone"), ("date", "Date"),
            ("select", "Select"), ("filter", "Filter"),
        ]:
            if token in text:
                return label
    if role == "card":
        for token, label in [("product", "Product"), ("metric", "Metric"), ("profile", "Profile"), ("login", "Login"), ("summary", "Summary")]:
            if token in text:
                return label
    if role == "navigation":
        for token, label in [("sidebar", "Sidebar"), ("tab", "Tabs"), ("menu", "Menu"), ("breadcrumb", "Breadcrumb")]:
            if token in text:
                return label
    if role == "table":
        for token, label in [("row", "Row"), ("cell", "Cell"), ("header", "Header"), ("main", "Main")]:
            if token in text:
                return label
    name_source = _region(item) or _target_name(item) or role
    variant = _slug_words(name_source, max_words=3)
    if variant.lower() in {"component", "rectangle", "group", "text", "background", "container", "frame", "board"}:
        variant = f"{_component_family_for_role(role)}{index}"
    return variant


def _component_name_for_target(item: dict[str, Any], index: int) -> str:
    role = _classify_ui_role(item)
    family = _component_family_for_role(role)
    variant = _component_variant_from_target(item, role, index)
    return f"{family}/{variant}"


def _nearby_text_children(target: dict[str, Any], targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find nearby text layers that likely belong to a non-text component."""
    if _is_text(target):
        return []
    box = _bbox(target)
    tx = box["x"]; ty = box["y"]; tw = box["width"]; th = box["height"]
    out: list[dict[str, Any]] = []
    for candidate in targets:
        if candidate is target or not _is_text(candidate):
            continue
        cb = _bbox(candidate)
        cx = cb["x"]; cy = cb["y"]; cw = cb["width"]; ch = cb["height"]
        x_overlap = max(0, min(tx + tw, cx + cw) - max(tx, cx))
        vertically_inside = (cy >= ty - 12 and cy + ch <= ty + th + 12)
        close_above = 0 <= (ty - (cy + ch)) <= 20 and x_overlap > 0
        close_below = 0 <= (cy - (ty + th)) <= 20 and x_overlap > 0
        horizontally_related = x_overlap >= min(tw, max(cw, 1)) * 0.25
        if horizontally_related and (vertically_inside or close_above or close_below):
            out.append(candidate)
    return out[:4]


def build_deterministic_semantic_fix_plan(
    validation_report: Any,
    known_targets: list[dict[str, Any]],
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Build pattern-agnostic native Penpot semantic/tokenization fixes.

    DVCP core must not assume a login screen. This planner derives tokens,
    components and interactive-state metadata from detected roles in the visual
    map: buttons, inputs, cards, navigation, tables, charts, media, text and
    generic surfaces. Login, dashboard, ecommerce, settings, modal, table/list,
    and unknown screens all pass through the same role-based planner.

    Visible DVCP annotation panels remain fallback/debug only:
        PENPOT_SEMANTIC_FALLBACK_ANNOTATIONS=1
    """
    if not force and not semantic_auto_fix_enabled():
        return []
    if not isinstance(validation_report, dict) or not known_targets:
        return []
    if not force and not _semantic_issue_present(validation_report):
        return []

    targets = [item for item in known_targets if isinstance(item, dict) and item.get("id")]
    if not targets:
        return []

    fallback_annotations = semantic_fallback_annotations_enabled()
    plan: list[dict[str, Any]] = []

    token_specs = [
        # Universal action/interaction tokens
        {"name": "color.action.primary.default", "type": "color", "value": "#2563EB"},
        {"name": "color.action.primary.hover", "type": "color", "value": "#1D4ED8"},
        {"name": "color.action.primary.disabled", "type": "color", "value": "#CBD5E1"},
        {"name": "color.focus.ring", "type": "color", "value": "#38BDF8"},
        # Universal text/surface/border tokens
        {"name": "color.text.default", "type": "color", "value": "#111827"},
        {"name": "color.text.muted", "type": "color", "value": "#64748B"},
        {"name": "color.text.inverse", "type": "color", "value": "#FFFFFF"},
        {"name": "color.text.disabled", "type": "color", "value": "#64748B"},
        {"name": "color.border.default", "type": "color", "value": "#CBD5E1"},
        {"name": "color.border.input", "type": "color", "value": "#94A3B8"},
        {"name": "color.surface.canvas", "type": "color", "value": "#FFFFFF"},
        {"name": "color.surface.card", "type": "color", "value": "#F8FAFC"},
        {"name": "color.surface.input", "type": "color", "value": "#FFFFFF"},
        # Universal spacing scale
        {"name": "spacing.4", "type": "spacing", "value": "4px"},
        {"name": "spacing.8", "type": "spacing", "value": "8px"},
        {"name": "spacing.12", "type": "spacing", "value": "12px"},
        {"name": "spacing.16", "type": "spacing", "value": "16px"},
        {"name": "spacing.24", "type": "spacing", "value": "24px"},
        {"name": "spacing.32", "type": "spacing", "value": "32px"},
        {"name": "spacing.form.gap", "type": "spacing", "value": "24px"},
        {"name": "spacing.input.padding.x", "type": "spacing", "value": "12px"},
        # Universal typography scale
        {"name": "typography.heading.size", "type": "fontSizes", "value": "24px"},
        {"name": "typography.body.size", "type": "fontSizes", "value": "16px"},
        {"name": "typography.label.size", "type": "fontSizes", "value": "14px"},
        {"name": "typography.button.size", "type": "fontSizes", "value": "16px"},
        {"name": "typography.caption.size", "type": "fontSizes", "value": "12px"},
        {"name": "typography.heading.weight", "type": "fontWeights", "value": "600"},
        {"name": "typography.body.weight", "type": "fontWeights", "value": "400"},
        {"name": "typography.label.weight", "type": "fontWeights", "value": "400"},
        {"name": "typography.button.weight", "type": "fontWeights", "value": "500"},
        # Radius/border
        {"name": "border.default.width", "type": "borderWidth", "value": "1px"},
        {"name": "border.input.width", "type": "borderWidth", "value": "1px"},
        {"name": "border.focus.width", "type": "borderWidth", "value": "2px"},
        {"name": "radius.sm", "type": "borderRadius", "value": "4px"},
        {"name": "radius.input", "type": "borderRadius", "value": "6px"},
        {"name": "radius.button", "type": "borderRadius", "value": "6px"},
        {"name": "radius.card", "type": "borderRadius", "value": "12px"},
    ]

    plan.append({
        "action": "ensure_native_tokens",
        "set_name": "DVCP/Core",
        "tokens": token_specs,
        "fallback_annotations": fallback_annotations,
        "reason": "Create pattern-agnostic native Penpot tokens for UI structure, interaction and handoff.",
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

    for target in targets:
        role = _classify_ui_role(target)
        text = _semantic_kind(target)
        if _is_text(target):
            if role == "heading":
                add_token_assignment(target, "typography.heading.size", ["fontSize"], "Bind heading typography token.")
                add_token_assignment(target, "color.text.default", ["fill"], "Bind heading color token.")
            elif role == "label" or "label" in text:
                add_token_assignment(target, "typography.label.size", ["fontSize"], "Bind label typography token.")
                add_token_assignment(target, "color.text.default", ["fill"], "Bind label color token.")
            elif role == "button" or "button" in text:
                add_token_assignment(target, "typography.button.size", ["fontSize"], "Bind button text typography token.")
                add_token_assignment(target, "color.text.inverse", ["fill"], "Bind inverse text token for action text.")
            else:
                add_token_assignment(target, "typography.body.size", ["fontSize"], "Bind body text typography token.")
                add_token_assignment(target, "color.text.default", ["fill"], "Bind body text color token.")
            continue

        if role == "button":
            add_token_assignment(target, "color.action.primary.default", ["fill"], "Bind action fill token.")
            add_token_assignment(target, "radius.button", ["borderRadius"], "Bind button radius token.")
            add_token_assignment(target, "color.focus.ring", ["stroke"], "Bind focus ring token.")
        elif role == "input" or role == "control":
            add_token_assignment(target, "color.surface.input", ["fill"], "Bind input/control surface token.")
            add_token_assignment(target, "color.border.input", ["stroke"], "Bind input/control border token.")
            add_token_assignment(target, "border.input.width", ["strokeWidth"], "Bind input/control border width token.")
            add_token_assignment(target, "radius.input", ["borderRadius"], "Bind input/control radius token.")
        elif role in {"card", "surface", "layout"}:
            add_token_assignment(target, "color.surface.card", ["fill"], "Bind surface/card token.")
            add_token_assignment(target, "radius.card", ["borderRadius"], "Bind card/surface radius token.")
            add_token_assignment(target, "color.border.default", ["stroke"], "Bind default border token.")
        elif role in {"table", "navigation", "data_viz", "media", "component"}:
            add_token_assignment(target, "color.border.default", ["stroke"], "Bind default structural border token.")

    if assignments:
        plan.append({
            "action": "apply_native_tokens",
            "set_name": "DVCP/Core",
            "assignments": assignments,
            "fallback_annotations": fallback_annotations,
            "reason": "Bind native tokens to detected UI role targets.",
            "safety": "safe_native_token_bindings_known_targets",
        })

    def native_component(name: str, role: str, children: list[dict[str, Any]], reason: str) -> None:
        clean_children = []
        seen: set[str] = set()
        for c in children:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            cid = str(c.get("id"))
            if cid in seen:
                continue
            seen.add(cid)
            clean_children.append(c)
        if not clean_children:
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

    component_items: list[tuple[str, str, list[dict[str, Any]]]] = []
    used_names: set[str] = set()
    for idx, target in enumerate(targets, start=1):
        role = _classify_ui_role(target)
        if role in {"label", "text", "heading"} and _is_text(target):
            continue
        if role not in {"button", "input", "control", "card", "surface", "layout", "navigation", "table", "data_viz", "media", "component"}:
            continue
        name = _component_name_for_target(target, idx)
        base_name = name
        counter = 2
        while name in used_names:
            name = f"{base_name}{counter}"
            counter += 1
        used_names.add(name)
        children = [target] + _nearby_text_children(target, targets)
        semantic_role = f"{role}_component"
        component_items.append((name, semantic_role, children))
        native_component(name, semantic_role, children, f"Create native component asset for detected {role} role.")

    # If the full selection/root is a meaningful container, create a screen-level pattern asset.
    container_targets = [t for t in targets if _classify_ui_role(t) in {"card", "surface", "layout"}]
    if len(targets) >= 4:
        screen_hint = str((validation_report.get("design_context_summary") or {}).get("root_shape_id") or "Screen")
        screen_name = f"Screen/{_slug_words(screen_hint if screen_hint != 'selection' else 'Main', max_words=2)}"
        screen_children = container_targets[:2] + [t for t in targets if _classify_ui_role(t) in {"heading", "button", "input", "card", "navigation", "table", "data_viz"}][:10]
        if screen_children:
            native_component(screen_name, "screen_pattern_component", screen_children, "Create native screen-level pattern asset from detected UI roles.")

    def state_metadata_for(role: str) -> list[dict[str, Any]]:
        if role == "button":
            return [
                {"name": "default", "fill_token": "color.action.primary.default", "text_token": "color.text.inverse"},
                {"name": "hover", "fill_token": "color.action.primary.hover", "text_token": "color.text.inverse"},
                {"name": "disabled", "fill_token": "color.action.primary.disabled", "text_token": "color.text.disabled"},
                {"name": "focus", "stroke_token": "color.focus.ring", "stroke_width_token": "border.focus.width"},
            ]
        if role in {"input", "control"}:
            return [
                {"name": "default", "fill_token": "color.surface.input", "stroke_token": "color.border.input"},
                {"name": "focus", "stroke_token": "color.focus.ring", "stroke_width_token": "border.focus.width"},
                {"name": "disabled", "fill_token": "color.action.primary.disabled", "text_token": "color.text.disabled"},
            ]
        if role in {"navigation", "table", "card"}:
            return [
                {"name": "default", "surface_token": "color.surface.card", "border_token": "color.border.default"},
                {"name": "focus", "stroke_token": "color.focus.ring", "stroke_width_token": "border.focus.width"},
            ]
        return []

    for name, semantic_role, children in component_items:
        role = semantic_role.replace("_component", "")
        states = state_metadata_for(role)
        if not states:
            continue
        plan.append({
            "action": "ensure_native_component_state_metadata",
            "component_name": name,
            "semantic_role": semantic_role,
            "states": states,
            "fallback_annotations": fallback_annotations,
            "reason": "Document interaction states as native component metadata when supported.",
            "safety": "safe_native_component_metadata",
        })
        for state in states:
            state_name = str(state.get("name") or "")
            if state_name and state_name != "default":
                native_component(
                    f"{name}/{_slug_words(state_name, max_words=1)}",
                    f"{role}_{state_name}_state",
                    children,
                    f"Create native {state_name} state asset for {name}.",
                )

    if fallback_annotations:
        cb = _bbox_union(targets)
        panel_x = _canonical_number(cb["x"] + cb["width"] + 80)
        panel_y = _canonical_number(cb["y"])
        component_names = [item[0] for item in component_items]
        plan.append({
            "action": "create_design_tokens_annotation",
            "name": "DesignTokensFallback",
            "semantic_role": "design_tokens_fallback",
            "bbox": {"x": panel_x, "y": panel_y, "width": 340, "height": 260},
            "tokens": {t["name"]: t["value"] for t in token_specs},
            "reason": "Fallback only: visible token documentation when native tokens are unavailable.",
            "safety": "safe_semantic_fix_create_helper_layer",
        })
        plan.append({
            "action": "create_handoff_annotation",
            "name": "HandoffNotesFallback",
            "semantic_role": "frontend_handoff_notes_fallback",
            "bbox": {"x": panel_x, "y": panel_y + 288, "width": 400, "height": 220},
            "text": "HandoffNotes\nNative target: Penpot Assets + Tokens\nPattern: role-based, screen-agnostic\nStates: default, hover, focus, disabled where applicable\nAccessibility: semantic grouping and focus metadata documented.",
            "reason": "Fallback only: visible handoff documentation when native assets/tokens are unavailable.",
            "safety": "safe_semantic_fix_create_helper_layer",
        })
        plan.append({
            "action": "create_component_index_annotation",
            "name": "ComponentIndexFallback",
            "semantic_role": "component_index_fallback",
            "bbox": {"x": panel_x, "y": panel_y + 536, "width": 400, "height": 180},
            "components": component_names[:24] + ["DVCP/Core"],
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
        "stitch_import_result": None,
        "imported_design_spec": None,
        "stitch_import_queue": None,
        "stitch_import_queue_result": None,
        "stitch_llm_memory": None,
        "stitch_llm_plan_summary": None,
        "stitch_visual_diff_report": None,
        "post_fix_validation_mode": None,
        "auto_fix_verified": False,
        "auto_fix_event": None,
        "auto_fix_verification": None,
    }

    # Si la acción empieza validando, no mandamos todavía un mensaje al builder.
    if not action_starts_with_validation(action) and not action_imports_from_stitch(action):
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


async def _run_stitch_import_queue_job(
    job: dict[str, Any],
    execute_code: Any,
    *,
    max_ops: int | None = None,
) -> dict[str, Any]:
    """Run a prepared import queue from its current cursor.

    Each execute_code call receives one tiny operation. This makes the import
    resumable and avoids sending full HTML/spec/debug metadata to Penpot.
    """
    ops = [op for op in (job.get("ops") or []) if isinstance(op, dict)]
    cursor = int(job.get("cursor") or 0)
    if cursor < 0:
        cursor = 0
    if cursor > len(ops):
        cursor = len(ops)

    job.setdefault("results", [])
    job.setdefault("created_shape_ids", [])
    job["status"] = "running"
    job["started_at"] = job.get("started_at") or utc_now_iso()

    run_count = 0
    for index in range(cursor, len(ops)):
        if max_ops is not None and run_count >= max_ops:
            job["status"] = "paused"
            break

        op = dict(ops[index])
        op["op_index"] = index
        op["op_total"] = len(ops)
        script = build_apply_import_op_script(op)

        try:
            raw_result = await execute_code.ainvoke({"code": script})
            penpot_result = parse_tool_json_result(raw_result)
        except Exception as exc:
            penpot_result = {
                "all_applied": False,
                "action": "dvcp_apply_import_op",
                "import_strategy": "queue_execute_code",
                "job_id": job.get("job_id"),
                "op_index": index,
                "op_total": len(ops),
                "op": op.get("op"),
                "name": op.get("name"),
                "checked_count": 1,
                "applied_count": 0,
                "failed_count": 1,
                "created_shape_count": 0,
                "created_shape_ids": [],
                "error": repr(exc),
            }

        penpot_result.setdefault("job_id", job.get("job_id"))
        penpot_result.setdefault("op_index", index)
        penpot_result.setdefault("op_total", len(ops))
        penpot_result.setdefault("op", op.get("op"))
        penpot_result.setdefault("name", op.get("name"))
        job["results"].append(penpot_result)

        for shape_id in penpot_result.get("created_shape_ids", []) or []:
            if shape_id and shape_id not in job["created_shape_ids"]:
                job["created_shape_ids"].append(shape_id)

        if not penpot_result.get("all_applied"):
            job["status"] = "paused"
            job["cursor"] = index
            job["last_error"] = penpot_result.get("error") or "op_failed"
            return job

        job["cursor"] = index + 1
        run_count += 1

    if int(job.get("cursor") or 0) >= len(ops):
        job["status"] = "completed"
        job["finished_at"] = utc_now_iso()
    else:
        job["status"] = "paused"

    return job


async def import_from_stitch(
    state: OverallState,
    runtime: Runtime[Context],
) -> Dict[str, Any]:
    """Single public Stitch import action.

    Internal strategy:
    1. Fetch existing Stitch screen (read-only Stitch MCP).
    2. Build rendered Playwright ExternalDesignSpec as deterministic baseline.
    3. Prefer deterministic transform T : StitchRenderedElement -> Pow(PenpotLayer).
    4. Optionally fall back to the previous LLM planner when explicitly enabled.
    5. Execute via internal queue_execute_code.

    Public action remains only: {"action": "import_from_stitch"}.
    """
    selection_hint = state.get("changeme") or ""
    token_updates: dict[str, Any] = {}

    spec: dict[str, Any] | None = None
    base_spec: dict[str, Any] | None = None
    stitch_payload: dict[str, Any] | None = None
    stitch_memory: dict[str, Any] | None = None
    llm_plan: dict[str, Any] | None = None
    llm_plan_summary: dict[str, Any] = {"used": False, "reason": "not_started"}

    project_id = state.get("stitch_project_id") or None
    project_name = state.get("stitch_project_name") or None
    screen_id = state.get("stitch_screen_id") or None
    screen_name = state.get("stitch_screen_name") or None

    try:
        stitch_payload = await fetch_existing_stitch_screen(
            project_id=project_id,
            project_name=project_name,
            screen_id=screen_id,
            screen_name=screen_name,
        )
        base_spec = build_external_design_spec_from_stitch(stitch_payload, selection_hint)
        stitch_memory = build_stitch_llm_memory(
            stitch_payload,
            base_spec,
            selection_hint=selection_hint,
        )

        # v06.13 default: no LLM structural planner. Build the Penpot operation
        # graph by deterministic transformation T : StitchRenderedElement -> Pow(PenpotLayer).
        # The older LLM planner remains available by setting
        # STITCH_IMPORT_DETERMINISTIC_TRANSFORM=0 and STITCH_IMPORT_LLM_PLANNER=1.
        enable_deterministic_transform = os.getenv("STITCH_IMPORT_DETERMINISTIC_TRANSFORM", "1").strip().lower() in {"1", "true", "yes", "on"}
        enable_llm_planner = os.getenv("STITCH_IMPORT_LLM_PLANNER", "0").strip().lower() in {"1", "true", "yes", "on"}
        enable_llm_vision = os.getenv("STITCH_IMPORT_LLM_VISION", "1").strip().lower() in {"1", "true", "yes", "on"}
        screenshot_available = bool(((stitch_memory or {}).get("reference_image") or {}).get("available"))
        planner_mode = "not_started"
        planner_error = None

        if enable_deterministic_transform:
            spec, llm_plan_summary = build_external_design_spec_from_deterministic_transform(base_spec)
            planner_mode = "deterministic_transform_T"
        elif enable_llm_planner:
            try:
                planner_mode = "vision" if enable_llm_vision and screenshot_available else "text"
                messages = build_stitch_llm_planner_messages(
                    stitch_memory,
                    include_image=bool(enable_llm_vision and screenshot_available),
                )
                metered = await metered_ainvoke(
                    llm,
                    messages,
                    estimated_completion_tokens=int(os.getenv("STITCH_IMPORT_LLM_COMPLETION_TOKENS", "3200")),
                )
                token_updates = merge_metered_usage_updates(state, token_updates, metered)
                llm_plan = parse_llm_build_plan(content_to_text(metered.ai_message.content))
                spec, llm_plan_summary = build_external_design_spec_from_llm_plan(llm_plan, base_spec)
            except Exception as exc:  # noqa: BLE001
                planner_error = repr(exc)
                # Some model adapters/accounts may not support image inputs. Retry once
                # text-only so the public action still works and token accounting stays
                # inside metered_ainvoke.
                if enable_llm_vision and os.getenv("STITCH_IMPORT_LLM_TEXT_RETRY", "1").strip().lower() in {"1", "true", "yes", "on"}:
                    try:
                        planner_mode = "text_retry_after_vision_error"
                        messages = build_stitch_llm_planner_messages(stitch_memory, include_image=False)
                        metered = await metered_ainvoke(
                            llm,
                            messages,
                            estimated_completion_tokens=int(os.getenv("STITCH_IMPORT_LLM_COMPLETION_TOKENS", "3200")),
                        )
                        token_updates = merge_metered_usage_updates(state, token_updates, metered)
                        llm_plan = parse_llm_build_plan(content_to_text(metered.ai_message.content))
                        spec, llm_plan_summary = build_external_design_spec_from_llm_plan(llm_plan, base_spec)
                    except Exception as retry_exc:  # noqa: BLE001
                        spec, sanitize_summary = sanitize_external_design_spec_for_import(base_spec)
                        llm_plan_summary = {
                            "used": False,
                            "reason": "llm_planner_failed_then_text_retry_failed",
                            "planner_error": planner_error,
                            "text_retry_error": repr(retry_exc),
                            "fallback_sanitize": sanitize_summary,
                        }
                else:
                    spec, sanitize_summary = sanitize_external_design_spec_for_import(base_spec)
                    llm_plan_summary = {
                        "used": False,
                        "reason": "llm_planner_failed",
                        "planner_error": planner_error,
                        "fallback_sanitize": sanitize_summary,
                    }
        else:
            spec, sanitize_summary = sanitize_external_design_spec_for_import(base_spec)
            llm_plan_summary = {
                "used": False,
                "reason": "disabled_by_STITCH_IMPORT_LLM_PLANNER",
                "fallback_sanitize": sanitize_summary,
            }

        llm_plan_summary = dict(llm_plan_summary or {})
        llm_plan_summary.update(
            {
                "planner_mode": planner_mode,
                "vision_requested": bool(enable_llm_vision),
                "vision_used": bool(planner_mode == "vision"),
                "screenshot_available": bool(screenshot_available),
            }
        )

        job = build_stitch_import_queue(spec)
    except Exception as exc:
        result = {
            "all_applied": False,
            "stage": "stitch_llm_guided_prepare",
            "error": repr(exc),
            "checked_count": 0,
            "applied_count": 0,
            "failed_count": 1,
            "strategy": {
                "public_action": "import_from_stitch",
                "planning": "deterministic_transform_with_optional_llm_fallback",
                "layout": "playwright_rendered",
                "execution": "queue_execute_code_v06_13_6_icon_only_no_label_fidelity",
            },
            "hint": "Revisa STITCH_API_KEY, STITCH_PROJECT_ID/STITCH_SCREEN_ID y Playwright.",
        }
        return {
            **token_updates,
            "stitch_import_result": result,
            "stitch_import_queue": None,
            "stitch_import_queue_result": result,
            "imported_design_spec": None,
            "stitch_visual_diff_report": None,
            "skip_validation": True,
            "response": "No se pudo preparar import_from_stitch.",
        }

    await get_builder_tools()
    execute_code = _builder_tools_by_name.get("execute_code")

    if execute_code is None:
        result = {
            "all_applied": False,
            "stage": "penpot_execute_code_lookup",
            "error": "execute_code_not_available",
            "checked_count": 1,
            "applied_count": 0,
            "failed_count": 1,
        }
        return {
            **token_updates,
            "stitch_import_result": result,
            "stitch_import_queue": job,
            "stitch_import_queue_result": result,
            "imported_design_spec": compact_imported_design_spec_for_output(spec) if isinstance(spec, dict) else spec,
            "stitch_llm_memory": compact_memory_for_output(stitch_memory or {}),
            "stitch_llm_plan_summary": llm_plan_summary,
            "stitch_visual_diff_report": None,
            "skip_validation": True,
            "response": "No se pudo importar porque execute_code no está disponible en Penpot MCP.",
        }

    # Internal queue execution. The user never needs a separate queue action.
    max_ops_raw = os.getenv("STITCH_IMPORT_QUEUE_MAX_OPS_PER_RUN", "0").strip()
    try:
        max_ops = int(max_ops_raw)
    except Exception:
        max_ops = 0
    max_ops_arg = max_ops if max_ops > 0 else None

    job = await _run_stitch_import_queue_job(job, execute_code, max_ops=max_ops_arg)
    queue_result = aggregate_stitch_import_queue_results(job)

    # v06.13 intentionally skips LLM visual diff. The authoritative diagnostic
    # is now the deterministic source-to-Penpot trace generated by the queue:
    # each Penpot shape is mapped back to a rendered source element and its
    # expected computed values.
    visual_diff_report: dict[str, Any] | None = make_visual_diff_status(
        "skipped", reason="replaced_by_source_to_penpot_trace_v06_13_6_icon_only_no_label_fidelity"
    )
    source_trace_report = queue_result.get("source_trace_report")
    source_to_penpot_map = queue_result.get("source_to_penpot_map")

    stitch_info = {}
    if isinstance(stitch_payload, dict):
        stitch_info = {
            "mode": stitch_payload.get("mode"),
            "project_id": stitch_payload.get("project_id"),
            "project_name": stitch_payload.get("project_name"),
            "screen_id": stitch_payload.get("screen_id"),
            "screen_name": stitch_payload.get("screen_name"),
            "tools_detected": stitch_payload.get("tools_detected", []),
            "api_key_redacted": stitch_payload.get("api_key_redacted"),
        }

    result = {
        "type": "stitch_import_execution",
        "imported_at": utc_now_iso(),
        "screen_name": job.get("screen_name") or (spec or {}).get("screen_name") if isinstance(spec, dict) else None,
        "screen_type": job.get("screen_type") or (spec or {}).get("screen_type") if isinstance(spec, dict) else None,
        "source": "stitch",
        "import_mode": (spec or {}).get("import_mode") if isinstance(spec, dict) else "existing_screen_html_llm_guided",
        "strategy": {
            "public_action": "import_from_stitch",
            "planning": ("deterministic_transform" if llm_plan_summary.get("deterministic") else ("llm_vision_guided" if llm_plan_summary.get("vision_used") else ("llm_guided" if llm_plan_summary.get("used") else "rendered_fallback"))),
            "layout": ((base_spec or {}).get("metadata") or {}).get("layout_extraction_mode", "rendered_playwright") if isinstance(base_spec, dict) else "unknown",
            "assembly": "generic_semantic_component_assembly",
            "execution": "queue_execute_code_v06_13_6_icon_only_no_label_fidelity",
                        "validation": "normal_validator_optional_env_STITCH_IMPORT_RUN_VALIDATOR",
        },
        "stitch": stitch_info,
        "llm_plan_summary": llm_plan_summary,
        "memory_summary": compact_memory_for_output(stitch_memory or {}),
        "penpot_apply_result": queue_result,
        "component_assembly_summary": queue_result.get("component_assembly_summary"),
        "visual_materialization_summary": queue_result.get("visual_materialization_summary"),
        "source_trace_report": source_trace_report,
        "source_to_penpot_map": source_to_penpot_map,
        "visual_diff_report": visual_diff_report,
        "all_applied": bool(queue_result.get("all_applied")),
        "status": queue_result.get("status"),
        "cursor": queue_result.get("cursor"),
        "total_ops": queue_result.get("total_ops"),
        "checked_count": queue_result.get("checked_count"),
        "applied_count": queue_result.get("applied_count"),
        "failed_count": queue_result.get("failed_count"),
        "created_shape_count": queue_result.get("created_shape_count"),
        "created_shape_ids": queue_result.get("created_shape_ids", []),
        "error": queue_result.get("error"),
    }

    all_applied = bool(result["all_applied"])
    return {
        **token_updates,
        "stitch_import_result": result,
        "stitch_import_queue": job,
        "stitch_import_queue_result": queue_result,
        "imported_design_spec": compact_imported_design_spec_for_output(spec) if isinstance(spec, dict) else spec,
        "stitch_llm_memory": compact_memory_for_output(stitch_memory or {}),
        "stitch_llm_plan_summary": llm_plan_summary,
        "stitch_visual_diff_report": visual_diff_report,
        "stitch_source_trace_report": source_trace_report,
        "stitch_source_to_penpot_map": source_to_penpot_map,
        "skip_validation": not all_applied,
        "response": (
            f"import_from_stitch completado: {job.get('screen_name')} "
            f"({queue_result.get('total_ops')} operaciones, {queue_result.get('created_shape_count')} shapes). "
            f"Plan: {result['strategy']['planning']}."
            if all_applied else
            f"import_from_stitch pausado/falló en cursor {queue_result.get('cursor')}/{queue_result.get('total_ops')}: {queue_result.get('error')}"
        ),
    }


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

    action = normalize_action(state.get("action"))

    if action_is_polish(action):
        known_targets = extract_known_canvas_targets(validation_report)
        if not known_targets:
            message = (
                "validate_and_polish omitido: no hay known_targets con "
                f"confidence >= {canvas_confidence_threshold():.2f}."
            )
            return {
                "messages": [AIMessage(content=message)],
                "response": message,
                "skip_validation": True,
            }

        semantic_fix_plan = build_deterministic_semantic_fix_plan(
            validation_report,
            known_targets,
            force=True,
        )
        if not semantic_fix_plan:
            message = (
                "validate_and_polish omitido: no se generó semantic_fix_plan "
                "de polish para tokens/assets/estados interactivos."
            )
            return {
                "messages": [AIMessage(content=message)],
                "response": message,
                "skip_validation": True,
            }

        next_iteration = fix_iterations + 1
        polish_event = {
            "type": "polish_auto_fix_start",
            "status": "pending",
            "verified_at": utc_now_iso(),
            "fix_iteration": next_iteration,
            "mode": "semantic_polish_native_tokens_assets",
            "trigger": "action:validate_and_polish",
            "confidence_threshold": canvas_confidence_threshold(),
            "known_target_count": len(known_targets),
        }

        return {
            "fix_iterations": next_iteration,
            "last_auto_fix_plan": [],
            "last_canvas_fix_targets": known_targets,
            "last_canvas_fix_plan": [],
            "last_semantic_fix_plan": semantic_fix_plan,
            "post_fix_validation_mode": "semantic_fix_pending",
            "auto_fix_verified": False,
            "auto_fix_event": polish_event,
            "auto_fix_verification": None,
            "response": None,
            "skip_validation": False,
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

    action = normalize_action(state.get("action"))
    is_polish_mode = action_is_polish(action)

    event = {
        "type": "polish_auto_fix_execution" if is_polish_mode else "canvas_auto_fix_execution",
        "status": canvas_status,
        "verified_at": utc_now_iso(),
        "fix_iteration": fix_iteration,
        "mode": "semantic_polish_native_tokens_assets" if is_polish_mode else "canvas_auto_fix_known_targets_only",
        "trigger": "action:validate_and_polish" if is_polish_mode else "env:PENPOT_ENABLE_CANVAS_AUTO_FIX",
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
            "validate_and_polish ran semantic/native-token polish on known targets. "
            "It may create or update native Penpot tokens, component assets, and "
            "interactive-state metadata without broad canvas layout edits. "
            "Run validate_only to score the updated design."
            if is_polish_mode else
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
            "verification_type": "semantic_polish_unverified" if is_polish_mode else "canvas_auto_fix_unverified",
            "reason": "semantic polish changes require a fresh validate_only run" if is_polish_mode else "known-target canvas/semantic changes require a fresh validate_only run",
            "canvas_apply_result": canvas_result if isinstance(canvas_result, dict) else None,
            "semantic_apply_result": semantic_result if isinstance(semantic_result, dict) else None,
        },
        "post_fix_validation_mode": None,
        "response": (
            "validate_and_polish ejecutado. Se aplicó polish semántico con tokens/assets "
            "nativos y estados interactivos cuando existía plan. Corre validate_only para "
            "obtener el nuevo score del diseño actualizado."
            if is_polish_mode else
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
) -> Literal["llm_call", "run_validator", "import_from_stitch"]:
    action = normalize_action(state.get("action"))

    if action_imports_from_stitch(action):
        return "import_from_stitch"

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


def route_after_stitch_import(
    state: OverallState,
) -> Literal["run_validator", "__end__"]:
    action = normalize_action(state.get("action"))

    if state.get("skip_validation"):
        return END

    if action_requires_validation_after_import(action):
        return "run_validator"

    return END


def route_after_validator(
    state: OverallState,
) -> Literal["fix_design", "__end__"]:
    action = normalize_action(state.get("action"))

    if not action_allows_fixing(action):
        return END

    fix_iterations = int(state.get("fix_iterations", 0) or 0)
    max_fix_iterations = int(state.get("max_fix_iterations", 2) or 2)

    if fix_iterations >= max_fix_iterations:
        return END

    if action_is_polish(action):
        if has_semantic_polish_candidates(state):
            return "fix_design"
        return END

    if validation_passed(state):
        return END

    if not has_executable_fix(state):
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
agent_builder.add_node("import_from_stitch", import_from_stitch)
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
    ["llm_call", "run_validator", "import_from_stitch"],
)

agent_builder.add_conditional_edges(
    "import_from_stitch",
    route_after_stitch_import,
    ["run_validator", END],
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
