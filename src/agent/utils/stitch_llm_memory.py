"""Compact HTML/Tailwind + screenshot memory for Stitch LLM planning.

The LLM uses this as context only. It must return a safe JSON plan; it never
writes Penpot code directly.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

MAX_HTML_CHARS = int(os.getenv("STITCH_LLM_HTML_CHARS", "9000"))
MAX_CLASS_COUNT = int(os.getenv("STITCH_LLM_CLASS_COUNT", "140"))
MAX_CHILDREN = int(os.getenv("STITCH_LLM_RENDERED_CHILDREN", "50"))
MAX_RENDERED_CHILDREN_IN_PROMPT = int(os.getenv("STITCH_LLM_PROMPT_RENDERED_CHILDREN", "40"))


def _extract_tailwind_classes(html: str) -> list[str]:
    classes: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'class\s*=\s*["\']([^"\']+)["\']', html or ""):
        for cls in re.split(r"\s+", match.group(1).strip()):
            if cls and cls not in seen:
                seen.add(cls)
                classes.append(cls)
                if len(classes) >= MAX_CLASS_COUNT:
                    return classes
    return classes


def _compact_child(child: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "name", "kind", "role", "tag", "text", "bbox", "fill", "stroke", "stroke_width",
        "color", "text_color", "font_size", "font_weight", "font_family", "line_height",
        "text_align", "radius", "opacity", "css_class", "id_attr", "dom_path", "input_type",
        "source_ref", "source_snapshot",
    ]
    out: dict[str, Any] = {}
    for key in keys:
        value = child.get(key)
        if value is None or value == "":
            continue
        if key == "css_class" and isinstance(value, str):
            value = value[:260]
        if key == "text" and isinstance(value, str):
            value = value[:300]
        if key == "source_snapshot" and isinstance(value, dict):
            value = {
                "source_ref": value.get("source_ref"),
                "origin": value.get("origin"),
                "tag": value.get("tag"),
                "dom_path": value.get("dom_path"),
                "expected": value.get("expected"),
            }
        out[key] = value
    return out


def _count_by(children: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for child in children:
        value = str(child.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _screenshot_memory(stitch_payload: dict[str, Any]) -> dict[str, Any]:
    screenshot = ((stitch_payload.get("downloads") or {}).get("screenshot") or {})
    if not isinstance(screenshot, dict):
        return {"available": False}
    content_type = str(screenshot.get("content_type") or "image/png").split(";", 1)[0].strip() or "image/png"
    base64_data = str(screenshot.get("base64") or "")
    data_url = ""
    if base64_data:
        data_url = f"data:{content_type};base64,{base64_data}"
    return {
        "available": bool(screenshot.get("ok") and base64_data),
        "ok": bool(screenshot.get("ok")),
        "content_type": content_type,
        "bytes_read": screenshot.get("bytes_read"),
        "url_present": bool(screenshot.get("url")),
        "error": screenshot.get("error"),
        "data_url": data_url,
        "base64_length": len(base64_data),
    }


def _memory_for_prompt(memory: dict[str, Any], *, include_html: bool = True) -> dict[str, Any]:
    """Return memory suitable for text prompt; never includes image base64."""
    prompt_memory = dict(memory)
    screenshot = dict(prompt_memory.get("reference_image") or {})
    screenshot.pop("data_url", None)
    prompt_memory["reference_image"] = screenshot
    if not include_html:
        prompt_memory.pop("html_sample", None)
    if isinstance(prompt_memory.get("rendered_elements"), list):
        prompt_memory["rendered_elements"] = prompt_memory["rendered_elements"][:MAX_RENDERED_CHILDREN_IN_PROMPT]
    return prompt_memory


def build_stitch_llm_memory(stitch_payload: dict[str, Any], external_spec: dict[str, Any], *, selection_hint: str = "") -> dict[str, Any]:
    html = (((stitch_payload.get("downloads") or {}).get("html") or {}).get("text") or "")
    screen = stitch_payload.get("screen") or {}
    children = [c for c in (external_spec.get("children") or []) if isinstance(c, dict)]
    screenshot_memory = _screenshot_memory(stitch_payload)

    return {
        "schema": "dvcp.stitch_llm_memory.v2",
        "selection_hint": selection_hint,
        "screen": {
            "name": external_spec.get("screen_name"),
            "title": external_spec.get("screen_title"),
            "type": external_spec.get("screen_type"),
            "width": external_spec.get("width"),
            "height": external_spec.get("height"),
            "device_type": screen.get("deviceType") or screen.get("device_type"),
        },
        "source": {
            "stitch_project_id": stitch_payload.get("project_id"),
            "stitch_screen_id": stitch_payload.get("screen_id"),
            "stitch_screen_name": stitch_payload.get("screen_name"),
            "html_bytes_read": ((stitch_payload.get("downloads") or {}).get("html") or {}).get("bytes_read"),
            "screenshot_url_present": bool((((stitch_payload.get("downloads") or {}).get("screenshot") or {}).get("url"))),
        },
        "reference_image": screenshot_memory,
        "tokens": external_spec.get("tokens") or {},
        "tailwind_class_sample": _extract_tailwind_classes(html),
        "generic_component_vocabulary": {
            "surface": "visual container, panel, card, section background",
            "field": "input-like component with optional label, placeholder, leading/trailing icons or actions",
            "action": "button, CTA, link-like or ghost interaction target",
            "control": "checkbox, radio, switch, toggle, slider or small form control",
            "navigation": "menu, tabs, links, breadcrumbs, app/nav bars",
            "content_block": "heading/body/helper/status text, with optional icon",
            "media": "image, video, avatar, illustration or logo block",
            "data_display": "table, chart, metric, stat, list or structured data block",
        },
        "rendered_summary": {
            "child_count": len(children),
            "kind_counts": _count_by(children, "kind"),
            "role_counts": _count_by(children, "role"),
            "layout_extraction_mode": (external_spec.get("metadata") or {}).get("layout_extraction_mode"),
        },
        "rendered_elements": [_compact_child(c) for c in children[:MAX_CHILDREN]],
        "html_sample": html[:MAX_HTML_CHARS],
    }


def compact_memory_for_output(memory: dict[str, Any]) -> dict[str, Any]:
    compact = dict(memory)
    compact.pop("html_sample", None)
    if isinstance(compact.get("rendered_elements"), list):
        compact["rendered_elements"] = compact["rendered_elements"][:20]
    screenshot = dict(compact.get("reference_image") or {})
    screenshot.pop("data_url", None)
    compact["reference_image"] = screenshot
    return compact


def build_stitch_llm_planner_messages(memory: dict[str, Any], *, include_image: bool = True) -> list[Any]:
    """Build multimodal planner messages.

    include_image=True uses the official Stitch screenshot as visual source of truth
    when available. The HTML/Tailwind memory remains text context for exact copy,
    tokens and styling hints.
    """
    screenshot = memory.get("reference_image") or {}
    data_url = str(screenshot.get("data_url") or "")
    can_use_image = bool(include_image and data_url)

    system = SystemMessage(
        content=(
            "Eres DVCP Design Planner. Tu tarea es crear un ExternalDesignSpec seguro "
            "para Penpot a partir de una pantalla de Stitch. No escribas JS ni código Penpot; "
            "devuelve SOLO JSON válido. La imagen de referencia es la verdad visual. "
            "El HTML/Tailwind es memoria contextual para copy, tokens, jerarquía, contraste y estilos. "
            "Evita wrappers, blobs absolute, blur, pointer-events-none, decoración enorme y textos de icon fonts. "
            "El resultado debe ser editable, con capas útiles, sin duplicados y con intención visual clara: superficie, campo, acción, control, navegación, contenido, media o datos."
        )
    )

    schema_instruction = {
        "expected_schema": "dvcp.external_design_spec.v1",
        "output_shape": {
            "schema": "dvcp.external_design_spec.v1",
            "source": "stitch_llm_vision_guided" if can_use_image else "stitch_llm_guided",
            "import_mode": "existing_screen_html_llm_vision_guided" if can_use_image else "existing_screen_html_llm_guided",
            "screen_name": "string",
            "screen_title": "string",
            "screen_type": "string",
            "width": "number",
            "height": "number",
            "tokens": "object",
            "children": [
                {
                    "name": "LayerName",
                    "kind": "surface|card|container|text|input|button|control|icon|svg|media",
                    "role": "semantic role",
                    "bbox": {"x": 0, "y": 0, "width": 100, "height": 40},
                    "text": "optional",
                    "fill": "#RRGGBB optional",
                    "stroke": "#RRGGBB optional",
                    "stroke_width": 1,
                    "color": "#RRGGBB optional",
                    "font_size": 14,
                    "font_weight": "400|500|600|700",
                    "font_family": "Inter",
                    "line_height": 20,
                    "text_align": "start|center|end|left|right",
                    "radius": 8,
                    "opacity": 1,
                    "z_index": "optional number; lower renders behind, text/icons should be higher",
                }
            ],
        },
        "rules": [
            "Usa coordenadas dentro del viewport de la pantalla.",
            "No importes fondos decorativos enormes ni blobs con blur.",
            "Prioriza la estructura real de cualquier interfaz: navegación/header/footer si existen, superficies/paneles, campos, acciones primarias/secundarias, controles, contenido, media y bloques de datos.",
            "No dupliques textos: si un componente ya contiene label/placeholder/content, represéntalo una sola vez como capa útil.",
            "Para acciones primarias, campos, controles y superficies importantes, conserva fill, stroke, radius, color de texto y jerarquía visual con contraste suficiente.",
            "Usa z_index explícito cuando sea útil: surfaces/panels 10000-20000, fields/controls 30000, actions 40000, media/icons 70000, textos 90000.",
            "Si una capa es puramente visual y puede romper la fidelidad, omítela.",
            "Devuelve JSON puro sin markdown.",
        ],
    }

    memory_for_prompt = _memory_for_prompt(memory)
    prompt_text = (
        "Genera un JSON compatible con dvcp.external_design_spec.v1 para reconstruir la pantalla en Penpot.\n"
        "Usa la imagen como fuente visual primaria y la memoria HTML/Tailwind para estilos/copy.\n\n"
        "INSTRUCCIONES_JSON:\n"
        + json.dumps(schema_instruction, ensure_ascii=False, default=str)
        + "\n\nMEMORIA_COMPACTA_JSON:\n"
        + json.dumps(memory_for_prompt, ensure_ascii=False, default=str)
    )

    if can_use_image:
        content = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        return [system, HumanMessage(content=content)]

    return [system, HumanMessage(content=prompt_text)]



def _truncate_text(value: Any, max_chars: int = 280) -> str:
    text = str(value or "")
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def compact_visual_nodes_for_diff(job: dict[str, Any], spec: dict[str, Any] | None = None, *, max_nodes: int = 90) -> list[dict[str, Any]]:
    """Build a compact, component-addressable list for visual diff prompts.

    The list is generic and intentionally based on the DVCP/Penpot structure:
    every node carries its semantic role, bbox, style hints and created shape IDs.
    The visual critic can then attach issues to concrete layers instead of writing
    only a free-form screenshot critique.
    """
    ops = [op for op in (job.get("ops") or []) if isinstance(op, dict)] if isinstance(job, dict) else []
    results = [item for item in (job.get("results") or []) if isinstance(item, dict)] if isinstance(job, dict) else []
    by_index: dict[int, dict[str, Any]] = {}
    for result in results:
        try:
            by_index[int(result.get("op_index"))] = result
        except Exception:
            continue

    nodes: list[dict[str, Any]] = []
    for op in ops:
        op_name = str(op.get("op") or "")
        if op_name in {"create_root", "finalize_import_job"}:
            continue
        result = by_index.get(int(op.get("op_index") or -1), {})
        b = op.get("bbox") if isinstance(op.get("bbox"), dict) else {}
        node = {
            "op_index": op.get("op_index"),
            "name": op.get("name"),
            "op": op_name,
            "kind": op.get("kind"),
            "role": op.get("role"),
            "component_type": op.get("component_type"),
            "component_id": op.get("component_id"),
            "attach_to": op.get("attach_to"),
            "slot": op.get("slot"),
            "bbox": {
                "x": b.get("x"),
                "y": b.get("y"),
                "width": b.get("width"),
                "height": b.get("height"),
            },
            "text": _truncate_text(op.get("text"), 220) if op.get("text") is not None else None,
            "fill": op.get("fill"),
            "stroke": op.get("stroke"),
            "stroke_width": op.get("stroke_width"),
            "color": op.get("color") or op.get("text_color"),
            "opacity": op.get("opacity"),
            "font_size": op.get("font_size"),
            "font_weight": op.get("font_weight"),
            "z_index": op.get("z_index"),
            "created_shape_ids": result.get("created_shape_ids", []),
            "visual_materialization": result.get("visual_materialization"),
        }
        nodes.append({k: v for k, v in node.items() if v is not None and v != [] and v != {}})
        if len(nodes) >= max_nodes:
            break
    return nodes


def build_stitch_visual_diff_messages(
    *,
    reference_image_data_url: str,
    penpot_image_data_url: str,
    screen_summary: dict[str, Any],
    visual_nodes: list[dict[str, Any]],
    queue_summary: dict[str, Any] | None = None,
) -> list[Any]:
    """Build multimodal messages for reference-vs-Penpot visual diff.

    This is report-only. The model must not write code or propose direct Penpot
    operations; it should identify differences and map them to the supplied nodes.
    """
    system = SystemMessage(
        content=(
            "Eres DVCP Visual Diff Critic. Comparas dos imágenes de UI: la primera es la referencia "
            "a replicar y la segunda es el resultado real exportado desde Penpot. Tu tarea es generar "
            "un reporte JSON, no aplicar cambios. Debes evaluar fidelidad visual y mapear problemas a "
            "capas/componentes concretos usando la estructura DVCP/Penpot proporcionada. No hardcodees "
            "patrones de pantalla; razona con conceptos genéricos: surface, field, action, control, "
            "navigation, content_block, media, data_display. Si no puedes mapear un problema a una capa, "
            "usa component_name=null y explica la incertidumbre. Devuelve SOLO JSON válido."
        )
    )

    expected_schema = {
        "schema": "dvcp.stitch_visual_diff_report.v1",
        "status": "completed",
        "overall": {
            "structure_match": "high|medium|low",
            "paint_match": "high|medium|low",
            "typography_match": "high|medium|low",
            "spacing_match": "high|medium|low",
            "depth_match": "high|medium|low",
            "text_visibility": "high|medium|low",
            "summary": "breve síntesis"
        },
        "issues": [
            {
                "issue_id": "VIS-001",
                "severity": "critical|high|medium|low",
                "category": "missing_element|extra_element|wrong_color|low_contrast|wrong_layer_order|wrong_size|wrong_position|missing_shadow|missing_border|text_invisible|text_mismatch|icon_incorrect|spacing_mismatch|opacity_mismatch|surface_depth|unknown",
                "component_name": "nombre de capa si se puede mapear",
                "component_type": "surface|field|action|control|navigation|content_block|media|data_display|unknown|null",
                "role": "rol semántico si existe",
                "op_index": "número si se conoce",
                "created_shape_ids": ["ids si existen"],
                "bbox": {"x": 0, "y": 0, "width": 0, "height": 0},
                "expected_visual": "qué se ve en referencia",
                "observed_visual": "qué se ve en Penpot",
                "suspected_cause": "posible causa técnica: fill/stroke/text/color/z-index/opacity/materialization/etc.",
                "recommended_fix": "acción conceptual segura, sin código",
                "confidence": 0.0,
                "autofixable": False
            }
        ],
        "component_notes": [
            {
                "component_name": "nombre",
                "status": "ok|needs_review|missing|uncertain",
                "note": "observación breve"
            }
        ],
        "safe_next_steps": ["pasos recomendados, report-only"]
    }

    prompt_payload = {
        "task": "Compare reference image vs Penpot export image and produce a structured report mapped to DVCP/Penpot nodes.",
        "image_order": [
            "1_reference_stitch_target",
            "2_penpot_import_result"
        ],
        "expected_schema": expected_schema,
        "rules": [
            "No propongas hardcode específico de esta pantalla; usa reglas genéricas de UI.",
            "Prioriza diferencias visibles importantes sobre detalles pixel-perfect.",
            "Cuando un componente existe estructuralmente pero se ve débil/invisible, categoriza como low_contrast, opacity_mismatch, wrong_color o wrong_layer_order según corresponda.",
            "Cuando la geometría general coincide pero la jerarquía visual no, marca paint_match/depth_match/typography_match como medium o low.",
            "Asocia cada issue al nodo más probable usando name, kind, role, component_type, bbox y created_shape_ids.",
            "No incluyas markdown ni texto fuera del JSON."
        ],
        "screen_summary": screen_summary,
        "queue_summary": queue_summary or {},
        "visual_nodes": visual_nodes,
    }
    text = "CONTEXTO_JSON:\n" + json.dumps(prompt_payload, ensure_ascii=False, default=str)
    return [
        system,
        HumanMessage(
            content=[
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": reference_image_data_url}},
                {"type": "image_url", "image_url": {"url": penpot_image_data_url}},
            ]
        ),
    ]


def parse_visual_diff_report(text: str) -> dict[str, Any]:
    """Parse the LLM visual diff JSON with a safe fallback."""
    raw = str(text or "").strip()
    if not raw:
        return {
            "schema": "dvcp.stitch_visual_diff_report.v1",
            "status": "error",
            "error": "empty_visual_diff_response",
            "issues": [],
        }
    try:
        parsed = json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            return {
                "schema": "dvcp.stitch_visual_diff_report.v1",
                "status": "error",
                "error": "visual_diff_response_not_json",
                "raw_preview": raw[:1200],
                "issues": [],
            }
        try:
            parsed = json.loads(match.group(0))
        except Exception as exc:
            return {
                "schema": "dvcp.stitch_visual_diff_report.v1",
                "status": "error",
                "error": f"visual_diff_json_parse_error: {exc!r}",
                "raw_preview": raw[:1200],
                "issues": [],
            }
    if not isinstance(parsed, dict):
        return {
            "schema": "dvcp.stitch_visual_diff_report.v1",
            "status": "error",
            "error": "visual_diff_json_not_object",
            "issues": [],
        }
    parsed.setdefault("schema", "dvcp.stitch_visual_diff_report.v1")
    parsed.setdefault("status", "completed")
    if not isinstance(parsed.get("issues"), list):
        parsed["issues"] = []
    return parsed
