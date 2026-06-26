"""Helpers to turn validator reports into safe Builder prompts.

The fixer itself must not call Penpot tools. It only converts
`validation_report.auto_fix_plan` into a precise instruction for the Builder.
"""

from __future__ import annotations

import json
from typing import Any


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
    """Build the prompt consumed by the Builder after validation fails.

    Phase 1 is intentionally conservative: only safe layer renames are automated.
    Component creation, token systems, contrast checks and accessibility details stay
    as manual notes until the rename loop is stable.
    """
    if not isinstance(validation_report, dict):
        return (
            "No se puede corregir automáticamente porque validation_report no es un dict válido. "
            "No modifiques Penpot. Devuelve un resumen del problema."
        )

    safe_plan = _safe_rename_plan(validation_report)
    manual_fixes = _as_list(validation_report.get("manual_fixes"))

    if not safe_plan:
        return (
            "El validador no generó auto_fix_plan seguro. "
            "No modifiques Penpot automáticamente. "
            "Resume los problemas principales y pide una validación/manual review.\n\n"
            f"validation_report_summary={json.dumps({
                'score': validation_report.get('score'),
                'status': validation_report.get('status'),
                'summary': validation_report.get('summary'),
                'manual_fixes': manual_fixes,
            }, ensure_ascii=False, default=str)}"
        )

    payload = {
        "fix_iteration": fix_iteration,
        "max_fix_iterations": max_fix_iterations,
        "allowed_actions": ["rename_layer"],
        "forbidden_actions": [
            "move_layer",
            "resize_layer",
            "delete_layer",
            "create_layer",
            "change_text_content",
            "change_color",
            "change_typography",
            "create_component",
            "detach_component",
            "change_layout",
        ],
        "auto_fix_plan": safe_plan,
        "manual_fixes_not_to_apply_now": manual_fixes,
    }

    return (
        "Corrige el diseño actual de Penpot aplicando SOLO el auto_fix_plan seguro.\n\n"
        "Objetivo de esta iteración:\n"
        "- Renombrar capas genéricas usando los IDs reales proporcionados.\n"
        "- No cambiar posición, tamaño, color, texto visible, jerarquía, componentes ni layout.\n"
        "- No intentes resolver accesibilidad, tokens, componentes ni estados interactivos todavía.\n"
        "- Usa herramientas de escritura de Penpot solo para renombrar las capas indicadas.\n"
        "- Si una capa por ID no existe, omítela y reporta cuál falló.\n"
        "- Al terminar, responde con un resumen breve de renombres aplicados.\n\n"
        "PLAN_JSON:\n"
        f"{json.dumps(payload, ensure_ascii=False, default=str, indent=2)}"
    )
