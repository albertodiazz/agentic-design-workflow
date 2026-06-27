"""Helpers to turn validator reports into Builder prompts.

Default behavior is conservative: only safe rename_layer operations are sent to
Builder. A separate .env flag can opt into broader canvas-level fixes, but only
after rename_layer fixes were applied and verified.
"""

from __future__ import annotations

import json
import os
from typing import Any

from agent.utils.resource_loader import (
    load_json_resource,
    render_skill,
)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def canvas_auto_fix_enabled() -> bool:
    """Return True when validate_and_fix may run the gated canvas-fix phase."""
    return env_flag("PENPOT_ENABLE_CANVAS_AUTO_FIX", False) or env_flag(
        "PENPOT_CANVAS_AUTO_FIX",
        False,
    )


def canvas_confidence_threshold() -> float:
    raw = os.getenv("PENPOT_CANVAS_AUTO_FIX_MIN_CONFIDENCE", "0.8")
    try:
        return float(raw)
    except ValueError:
        return 0.8


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_rename_plan(validation_report: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only safe rename operations from validator auto_fix_plan."""
    raw_plan = _as_list(validation_report.get("auto_fix_plan"))
    safe_plan: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in raw_plan:
        if not isinstance(item, dict):
            continue

        if item.get("action") != "rename_layer":
            continue

        if item.get("safety") != "safe_auto_fix":
            continue

        layer_id = str(item.get("id") or "").strip()
        current_name = str(item.get("current_name") or "").strip()
        new_name = str(item.get("new_name") or "").strip()

        if not layer_id or not current_name or not new_name:
            continue

        if layer_id in seen_ids:
            continue

        seen_ids.add(layer_id)
        safe_plan.append(
            {
                "action": "rename_layer",
                "node_ref": item.get("node_ref", ""),
                "id": layer_id,
                "current_name": current_name,
                "new_name": new_name,
                "type": item.get("type", ""),
                "path": item.get("path", ""),
                "reason": item.get("reason", ""),
            }
        )

    return safe_plan


def extract_known_canvas_targets(
    validation_report: dict[str, Any] | str | None,
    *,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Return layers the model mapped with enough confidence for canvas edits.

    Canvas-level edits are allowed only on these known targets. The validator
    report is expected to be normalized before this function is called.
    """
    if not isinstance(validation_report, dict):
        return []

    min_confidence = canvas_confidence_threshold() if threshold is None else threshold
    visual_map = _as_list(validation_report.get("visual_structure_map"))
    targets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in visual_map:
        if not isinstance(item, dict):
            continue

        try:
            confidence = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0

        if confidence < min_confidence:
            continue

        layer = item.get("matched_layer")
        if not isinstance(layer, dict):
            continue

        layer_id = str(layer.get("id") or "").strip()
        node_ref = str(layer.get("node_ref") or "").strip()

        if not layer_id or not node_ref:
            continue

        if layer_id in seen_ids:
            continue

        seen_ids.add(layer_id)
        targets.append(
            {
                "node_ref": node_ref,
                "id": layer_id,
                "name": layer.get("name", ""),
                "type": layer.get("type", ""),
                "path": layer.get("path", ""),
                "bbox": layer.get("bbox"),
                "visual_region": item.get("visual_region", ""),
                "inferred_role": item.get("inferred_role", ""),
                "confidence": confidence,
            }
        )

    return targets


def _compact_validation_report_for_canvas_fix(
    validation_report: dict[str, Any],
) -> dict[str, Any]:
    """Keep the report useful for canvas fixes without dumping everything."""
    return {
        "passed": validation_report.get("passed"),
        "score": validation_report.get("score"),
        "status": validation_report.get("status"),
        "summary": validation_report.get("summary"),
        "checks": validation_report.get("checks", {}),
        "issues": _as_list(validation_report.get("issues"))[:20],
        "required_fixes": _as_list(validation_report.get("required_fixes"))[:20],
        "suggested_structure": validation_report.get("suggested_structure", ""),
        "developer_notes": _as_list(validation_report.get("developer_notes"))[:20],
        "manual_fixes": _as_list(validation_report.get("manual_fixes"))[:20],
        "design_context_summary": validation_report.get("design_context_summary", {}),
        "can_be_sent_to_development": validation_report.get("can_be_sent_to_development"),
    }


def build_canvas_fix_prompt(
    validation_report: dict[str, Any],
    *,
    known_targets: list[dict[str, Any]],
    rename_verification: dict[str, Any] | None = None,
    fix_iteration: int = 0,
    max_fix_iterations: int = 1,
) -> str:
    """Build gated canvas-fix prompt after rename verification succeeds."""
    constraints = load_json_resource("auto_fix_constraints.json")
    threshold = canvas_confidence_threshold()

    rename_verification = rename_verification or {}
    rename_status = rename_verification.get("rename_status")
    precondition = (
        "rename_phase=no_op; no safe rename_layer plan was needed before this canvas-fix phase"
        if rename_status == "not_needed"
        else "rename_layer auto_fix_plan was applied and structurally verified before this canvas-fix phase"
    )

    payload = {
        "fix_mode": "canvas_auto_fix_known_targets_only",
        "canvas_auto_fix_enabled": True,
        "env_flag": "PENPOT_ENABLE_CANVAS_AUTO_FIX=1",
        "confidence_threshold": threshold,
        "fix_iteration": fix_iteration,
        "max_fix_iterations": max_fix_iterations,
        "precondition": precondition,
        "rename_verification": rename_verification,
        "known_targets": known_targets,
        "allowed_actions": constraints.get("canvas_allowed_actions", []),
        "forbidden_actions": constraints.get("canvas_forbidden_actions", []),
        "validation_report": _compact_validation_report_for_canvas_fix(validation_report),
    }

    return render_skill(
        "fixer.md",
        {
            "MODE": "canvas_auto_fix_known_targets_only",
            "PLAN_JSON": json.dumps(
                payload,
                ensure_ascii=False,
                default=str,
                indent=2,
            ),
            "VALIDATION_REPORT_SUMMARY_JSON": "{}",
        },
    )


def build_fix_design_prompt(
    validation_report: dict[str, Any] | str | None,
    *,
    fix_iteration: int = 0,
    max_fix_iterations: int = 1,
) -> str:
    """Build the initial rename-only prompt consumed by the Builder."""
    if not isinstance(validation_report, dict):
        return render_skill(
            "fixer.md",
            {
                "MODE": "invalid_report",
                "PLAN_JSON": "{}",
                "VALIDATION_REPORT_SUMMARY_JSON": json.dumps(
                    {"error": "validation_report is not a valid dict"},
                    ensure_ascii=False,
                    default=str,
                    indent=2,
                ),
            },
        )

    constraints = load_json_resource("auto_fix_constraints.json")
    safe_plan = _safe_rename_plan(validation_report)
    manual_fixes = _as_list(validation_report.get("manual_fixes"))

    if not safe_plan:
        summary = {
            "score": validation_report.get("score"),
            "status": validation_report.get("status"),
            "summary": validation_report.get("summary"),
            "manual_fixes": manual_fixes,
            "canvas_auto_fix_note": (
                "Canvas auto-fix is gated: it runs after verified rename_layer fixes, or directly when rename_phase=no_op and safe known targets exist."
            ),
        }
        return render_skill(
            "fixer.md",
            {
                "MODE": "no_auto_fix",
                "PLAN_JSON": "{}",
                "VALIDATION_REPORT_SUMMARY_JSON": json.dumps(
                    summary,
                    ensure_ascii=False,
                    default=str,
                    indent=2,
                ),
            },
        )

    payload = {
        "fix_mode": "rename_only",
        "fix_iteration": fix_iteration,
        "max_fix_iterations": max_fix_iterations,
        "allowed_actions": constraints.get("allowed_actions", ["rename_layer"]),
        "forbidden_actions": constraints.get("forbidden_actions", []),
        "auto_fix_plan": safe_plan,
        "manual_fixes_not_to_apply_now": manual_fixes,
        "next_phase_if_enabled": (
            "If PENPOT_ENABLE_CANVAS_AUTO_FIX=1, a separate canvas-fix phase may run only after these renames are verified."
        ),
    }

    return render_skill(
        "fixer.md",
        {
            "MODE": "auto_fix",
            "PLAN_JSON": json.dumps(
                payload,
                ensure_ascii=False,
                default=str,
                indent=2,
            ),
            "VALIDATION_REPORT_SUMMARY_JSON": "{}",
        },
    )
