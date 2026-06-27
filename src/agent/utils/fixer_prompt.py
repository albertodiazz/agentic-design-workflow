"""Helpers to turn validator reports into safe Builder prompts.

The fixer itself must not call Penpot tools. It only converts
`validation_report.auto_fix_plan` into a precise instruction for the Builder.
"""

from __future__ import annotations

import json
from typing import Any

from agent.utils.resource_loader import (
    load_json_resource,
    render_skill,
)


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


def build_fix_design_prompt(
    validation_report: dict[str, Any] | str | None,
    *,
    fix_iteration: int = 0,
    max_fix_iterations: int = 1,
) -> str:
    """Build the prompt consumed by the Builder after validation fails."""
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

    safe_plan = _safe_rename_plan(validation_report)
    manual_fixes = _as_list(validation_report.get("manual_fixes"))

    if not safe_plan:
        summary = {
            "score": validation_report.get("score"),
            "status": validation_report.get("status"),
            "summary": validation_report.get("summary"),
            "manual_fixes": manual_fixes,
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

    constraints = load_json_resource("auto_fix_constraints.json")
    payload = {
        "fix_iteration": fix_iteration,
        "max_fix_iterations": max_fix_iterations,
        "allowed_actions": constraints.get("allowed_actions", ["rename_layer"]),
        "forbidden_actions": constraints.get("forbidden_actions", []),
        "auto_fix_plan": safe_plan,
        "manual_fixes_not_to_apply_now": manual_fixes,
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
