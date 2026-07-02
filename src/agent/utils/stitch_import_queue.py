"""Build a resumable queue from a DVCP ExternalDesignSpec.

This module is intentionally pure Python: it does not call Penpot and it does not
call Stitch. It only converts the already parsed ExternalDesignSpec into tiny
operations that are safe to send one-by-one to Penpot MCP execute_code.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("px", ""))
    except Exception:
        return default


def _bbox(item: dict[str, Any], fallback_width: float = 100, fallback_height: float = 40) -> dict[str, float]:
    raw = item.get("bbox") if isinstance(item, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    return {
        "x": _num(raw.get("x"), 0),
        "y": _num(raw.get("y"), 0),
        "width": max(_num(raw.get("width"), fallback_width), 1),
        "height": max(_num(raw.get("height"), fallback_height), 1),
    }


def _safe_name(value: Any, fallback: str) -> str:
    text = str(value or fallback).strip()
    if not text:
        text = fallback
    text = re.sub(r"[^A-Za-z0-9_ÁÉÍÓÚÜÑáéíóúüñ -]+", "", text)
    text = re.sub(r"\s+", "", text)
    return text[:80] or fallback


def _compact_metadata(metadata: Any) -> dict[str, Any]:
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


def compact_imported_design_spec_for_output(spec: dict[str, Any]) -> dict[str, Any]:
    """Return the spec without heavy debug payloads such as full HTML preview."""
    compact = dict(spec)
    metadata = dict(compact.get("metadata") or {})
    metadata.pop("html_preview", None)
    metadata.pop("payload_preview", None)
    metadata.pop("download_url", None)
    if isinstance(metadata.get("extracted_elements_preview"), list):
        metadata["extracted_elements_preview"] = metadata["extracted_elements_preview"][:20]
    compact["metadata"] = metadata
    return compact


def _root_offset(spec: dict[str, Any]) -> dict[str, float]:
    return {
        "x": _num(spec.get("canvas_x"), 120),
        "y": _num(spec.get("canvas_y"), 80),
    }



def _semantic_z(item: dict[str, Any], index: int) -> int:
    """Stable layer priority for Penpot export.

    Penpot MCP can append children in different internal orders depending on the
    runtime. Carry an explicit z_index so the JS executor can restack the final
    board deterministically. This is intentionally generic and pattern-agnostic.
    """
    if item.get("z_index") is not None:
        try:
            return int(float(str(item.get("z_index"))))
        except Exception:
            pass

    kind = str(item.get("kind") or item.get("type") or "").lower()
    role = str(item.get("role") or "").lower()
    tag = str(item.get("tag") or "").lower()

    if kind in {"surface", "card", "container", "form", "section", "navigation", "data_display", "media"}:
        if role in {"background", "screen_background", "canvas"}:
            base = 0
        elif role in {"header", "footer", "surface", "card"} or tag in {"header", "footer"}:
            base = 10
        else:
            base = 15
    elif kind in {"input", "control", "toggle", "upload"}:
        base = 30
    elif kind == "button":
        base = 40 if "primary" in role else 35
    elif kind == "svg":
        base = 60
    elif kind == "icon":
        # Icons must be above their container/background but below explicit text
        # only when the text belongs to the same component. This works well for
        # most UI screens and can be overridden by the LLM with z_index.
        base = 70
    elif kind in {"text", "heading"} or role in {"label", "placeholder", "link", "button_text", "body_text"}:
        base = 90
    else:
        base = 50

    # Preserve source order within each class without letting it dominate role.
    return base * 1000 + index


def _sorted_children_for_queue(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(item_with_idx: tuple[int, dict[str, Any]]) -> tuple[int, float, float, int]:
        idx, item = item_with_idx
        b = _bbox(item)
        return (_semantic_z(item, idx), b["y"], b["x"], idx)
    return [item for _, item in sorted(enumerate(children), key=key)]



def _intersects_or_contains(container: dict[str, float], child: dict[str, float], pad: float = 0.0) -> bool:
    return not (
        child["x"] + child["width"] < container["x"] - pad
        or child["x"] > container["x"] + container["width"] + pad
        or child["y"] + child["height"] < container["y"] - pad
        or child["y"] > container["y"] + container["height"] + pad
    )


def _center_distance(a: dict[str, float], b: dict[str, float]) -> float:
    ax, ay = a["x"] + a["width"] / 2, a["y"] + a["height"] / 2
    bx, by = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _component_type_for(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or item.get("type") or "").lower()
    role = str(item.get("role") or "").lower()
    tag = str(item.get("tag") or "").lower()
    if role in {"header", "topbar", "appbar"} or tag == "header":
        return "header"
    if role in {"footer", "bottombar"} or tag == "footer":
        return "footer"
    if kind == "input" or role in {"input", "field", "textbox", "select", "textarea"}:
        return "field"
    if kind == "button" or "button" in role or role in {"action", "link", "cta"}:
        return "action"
    if kind in {"control", "toggle", "upload"} or role in {"checkbox", "radio", "switch", "control", "toggle"}:
        return "control"
    if kind in {"card", "surface", "container", "form"} or role in {"card", "surface", "section", "panel", "form"}:
        return "surface"
    if kind in {"media", "svg"} or role in {"media", "image", "video", "avatar"}:
        return "media"
    if role in {"navigation", "nav", "menu", "tabs", "breadcrumb"}:
        return "navigation"
    if role in {"table", "chart", "data_display", "metric", "stat"}:
        return "data_display"
    if kind in {"text", "heading", "icon"} or role in {"label", "placeholder", "heading", "body_text", "link", "icon"}:
        return "content_block"
    return "content_block"


def _slot_for_attachment(child: dict[str, Any], target: dict[str, Any]) -> str:
    ckind = str(child.get("kind") or "").lower()
    crole = str(child.get("role") or "").lower()
    ttype = str(target.get("component_type") or _component_type_for(target)).lower()
    cb = _bbox(child)
    tb = _bbox(target)
    text = str(child.get("text") or "").strip()
    if ckind == "icon" or "icon" in crole:
        if cb["x"] < tb["x"] + tb["width"] * 0.35:
            return "leading_icon"
        if cb["x"] > tb["x"] + tb["width"] * 0.65:
            return "trailing_icon"
        return "icon"
    if crole == "link" and ttype == "field":
        return "trailing_action"
    if ("button" in crole or crole == "action") and ttype == "field":
        return "trailing_action"
    if crole == "label" or (ckind == "text" and cb["y"] + cb["height"] <= tb["y"] + 8 and ttype in {"field", "control"}):
        return "label"
    if crole in {"placeholder", "body_text"} and ttype == "field":
        return "placeholder"
    if crole == "link" or "button" in crole or ttype == "action":
        return "label" if text else "action"
    if ckind == "text":
        if ttype == "action":
            return "label"
        return "content"
    return "content"


def _nearest_component(child: dict[str, Any], components: list[dict[str, Any]]) -> dict[str, Any] | None:
    cb = _bbox(child)
    ckind = str(child.get("kind") or "").lower()
    crole = str(child.get("role") or "").lower()
    candidates: list[tuple[float, int, dict[str, Any]]] = []
    for index, comp in enumerate(components):
        if comp is child:
            continue
        tb = _bbox(comp)
        ttype = str(comp.get("component_type") or "").lower()
        # Text/icon inside a component bbox, or label just above a field/control.
        inside = _intersects_or_contains(tb, cb, pad=3)
        above_field = (
            ttype in {"field", "control"}
            and ckind == "text"
            and cb["y"] + cb["height"] <= tb["y"] + 10
            and tb["y"] - (cb["y"] + cb["height"]) <= 36
            and cb["x"] <= tb["x"] + tb["width"]
            and cb["x"] + cb["width"] >= tb["x"]
        )
        same_row_action = (
            ttype == "field"
            and (crole == "link" or "button" in crole)
            and abs(cb["y"] - (tb["y"] - 24)) <= 28
            and cb["x"] >= tb["x"] + tb["width"] * 0.35
        )
        same_row_control = (
            ttype == "control"
            and ckind == "text"
            and abs((cb["y"] + cb["height"] / 2) - (tb["y"] + tb["height"] / 2)) <= 18
            and cb["x"] >= tb["x"] + tb["width"]
            and cb["x"] <= tb["x"] + tb["width"] + 260
        )
        if inside or above_field or same_row_action or same_row_control:
            dist = _center_distance(cb, tb)
            # Prefer semantic controls/fields/actions over broad surfaces when a
            # text/icon could match both. This keeps labels/placeholders attached
            # to fields instead of the enclosing card, and button labels attached
            # to actions instead of the larger section.
            area = tb["width"] * tb["height"]
            priority = 0.0
            if above_field or same_row_action or same_row_control:
                priority -= 220.0
            if inside and ttype in {"field", "action", "control", "navigation", "data_display"}:
                priority -= 120.0
            if ttype in {"surface", "header", "footer"}:
                priority += 80.0
            candidates.append((dist + area / 100000.0 + priority, index, comp))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: (x[0], x[1]))[0][2]


def semantic_component_assembly(children: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Annotate flat visual layers with generic component relationships.

    This is deliberately pattern-agnostic. It does not know what a login screen is;
    it only identifies reusable UI concepts: surfaces, fields, actions, controls,
    navigation/content/media/data blocks, and attaches nearby labels/icons/text to
    the most plausible parent component.
    """
    assembled: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    attachments = 0

    # First pass: clone items and create stable generic component ids.
    for idx, raw in enumerate(children):
        item = dict(raw)
        component_type = str(item.get("component_type") or _component_type_for(item))
        item["component_type"] = component_type
        counts[component_type] = counts.get(component_type, 0) + 1
        if component_type in {"surface", "header", "footer", "field", "action", "control", "navigation", "media", "data_display"}:
            item.setdefault("component_id", _safe_name(item.get("name"), f"Component{idx}"))
            components.append(item)
        assembled.append(item)

    component_by_id = {str(c.get("component_id")): c for c in components if c.get("component_id")}

    # Second pass: attach content layers to nearby component containers.
    for item in assembled:
        if item.get("attach_to"):
            continue
        component_type = str(item.get("component_type") or "").lower()
        kind = str(item.get("kind") or "").lower()
        role = str(item.get("role") or "").lower()
        if component_type in {"surface", "header", "footer", "field", "action", "control", "navigation", "media", "data_display"} and kind not in {"text", "icon"}:
            continue
        if kind not in {"text", "icon", "svg"} and role not in {"label", "link", "placeholder", "heading", "body_text", "button_text"}:
            continue
        target = _nearest_component(item, components)
        if target is not None and target.get("component_id"):
            target_id = str(target.get("component_id"))
            item["attach_to"] = target_id
            item["component_id"] = item.get("component_id") or target_id
            item["slot"] = item.get("slot") or _slot_for_attachment(item, target)
            attachments += 1

    # Third pass: avoid invisible ghost action failure. A transparent action with
    # no own text is a hitbox/semantic action; mark it so the executor may create
    # a no-op/invisible hitbox without pausing the queue.
    ghost_actions = 0
    for item in assembled:
        if str(item.get("component_type") or "").lower() == "action" and str(item.get("kind") or "").lower() == "button":
            fill = str(item.get("fill") or "").lower().strip()
            text = str(item.get("text") or "").strip()
            if (not text) and (not fill or fill in {"transparent", "none", "rgba(0,0,0,0)", "rgba(0, 0, 0, 0)"}):
                item["ghost"] = True
                item["allow_no_shape"] = True
                ghost_actions += 1

    summary = {
        "strategy": "generic_semantic_component_assembly_v1",
        "input_count": len(children),
        "output_count": len(assembled),
        "component_count": len(components),
        "attachments_count": attachments,
        "ghost_action_count": ghost_actions,
        "component_type_counts": counts,
        "component_ids_preview": list(component_by_id.keys())[:30],
    }
    return assembled, summary



def _visible_paint(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() in {"transparent", "none", "rgba(0,0,0,0)", "rgba(0, 0, 0, 0)"}:
        return None
    return text


def _paint_for(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _visible_paint(item.get(key))
        if value:
            return value
    return None


def _bbox_area(b: dict[str, float]) -> float:
    return max(float(b.get("width") or 0), 0.0) * max(float(b.get("height") or 0), 0.0)


def _center_inside(container: dict[str, float], child: dict[str, float], pad: float = 0.0) -> bool:
    cx = child["x"] + child["width"] / 2
    cy = child["y"] + child["height"] / 2
    return (
        container["x"] - pad <= cx <= container["x"] + container["width"] + pad
        and container["y"] - pad <= cy <= container["y"] + container["height"] + pad
    )


def _visual_parent_for(item: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the nearest visual background for a child layer.

    This is intentionally generic: prefer explicit semantic attachment first,
    then fall back to the smallest visible container that spatially contains the
    layer center. It works for forms, dashboards, cards, navbars, modals, etc.
    """
    attach_to = str(item.get("attach_to") or "").strip()
    component_id = str(item.get("component_id") or "").strip()
    if attach_to:
        for candidate in items:
            if candidate is item:
                continue
            if str(candidate.get("component_id") or "") == attach_to:
                return candidate
    # If a content layer inherited a component_id but did not get attach_to,
    # use that component as the background when it is a different object.
    kind = str(item.get("kind") or "").lower()
    if component_id and kind in {"text", "icon", "svg"}:
        for candidate in items:
            if candidate is item:
                continue
            if str(candidate.get("component_id") or "") == component_id and _paint_for(candidate, "fill"):
                return candidate

    cb = _bbox(item)
    candidates: list[tuple[float, int, dict[str, Any]]] = []
    for idx, candidate in enumerate(items):
        if candidate is item:
            continue
        if not _paint_for(candidate, "fill"):
            continue
        ctype = str(candidate.get("component_type") or _component_type_for(candidate)).lower()
        ckind = str(candidate.get("kind") or "").lower()
        if ctype not in {"surface", "header", "footer", "field", "action", "control", "navigation", "media", "data_display"} and ckind not in {"card", "surface", "container", "form", "input", "button"}:
            continue
        tb = _bbox(candidate)
        if _bbox_area(tb) <= _bbox_area(cb):
            continue
        if _center_inside(tb, cb, pad=2) or _intersects_or_contains(tb, cb, pad=2):
            # Smaller containing surfaces are more likely to be the actual local
            # background than the page/card behind them.
            candidates.append((_bbox_area(tb), idx, candidate))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: (x[0], x[1]))[0][2]


def _compact_tokens_for_visual_context(tokens: Any) -> dict[str, Any]:
    if not isinstance(tokens, dict):
        return {}
    keep = {
        "color.background.canvas",
        "color.surface.default",
        "color.text.default",
        "color.text.muted",
        "color.text.link",
        "color.border.default",
        "color.action.primary.default",
        "color.action.primary.text",
        "color.control.checkbox",
    }
    return {key: tokens.get(key) for key in keep if tokens.get(key) is not None}


def _visual_context_for(item: dict[str, Any], items: list[dict[str, Any]], tokens: dict[str, Any]) -> dict[str, Any]:
    compact_tokens = _compact_tokens_for_visual_context(tokens)
    root_fill = (
        _visible_paint(compact_tokens.get("color.background.canvas"))
        or _visible_paint(compact_tokens.get("color.surface.default"))
        or "#FFFFFF"
    )
    parent = _visual_parent_for(item, items)
    parent_fill = _paint_for(parent or {}, "fill") or root_fill
    return {
        "root_fill": root_fill,
        "parent_fill": parent_fill,
        "parent_component_type": (parent or {}).get("component_type"),
        "parent_role": (parent or {}).get("role"),
        "parent_name": (parent or {}).get("name"),
        "tokens": compact_tokens,
    }

SOURCE_EXPECTED_KEYS = (
    "fill", "stroke", "stroke_width", "color", "text_color", "font_size", "font_weight",
    "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align", "radius", "opacity", "fill_opacity",
    "box_shadow", "input_type", "media_alt", "is_material_symbol", "material_symbol_name",
    "text_no_wrap", "expected_line_count", "source_line_count", "penpot_grow_type",
)


def _expected_from_item(item: dict[str, Any]) -> dict[str, Any]:
    expected: dict[str, Any] = {"bbox": _bbox(item)}
    if item.get("text") is not None:
        expected["text"] = str(item.get("text") or "")[:500]
    for key in SOURCE_EXPECTED_KEYS:
        value = item.get(key)
        if value is not None and value != "":
            expected[key] = value
    return expected


def _source_snapshot_for_item(item: dict[str, Any], index: int) -> dict[str, Any]:
    source_ref = str(item.get("source_ref") or f"queue_{index:03d}")
    snap = item.get("source_snapshot") if isinstance(item.get("source_snapshot"), dict) else {}
    out = dict(snap)
    out.setdefault("schema", "dvcp.source_element_snapshot.v1")
    out.setdefault("source_ref", source_ref)
    out.setdefault("origin", "external_design_spec")
    out.setdefault("name", item.get("source_name") or item.get("name"))
    out.setdefault("kind", item.get("kind"))
    out.setdefault("role", item.get("role"))
    out.setdefault("tag", item.get("tag"))
    out.setdefault("dom_path", item.get("dom_path"))
    out.setdefault("css_class", item.get("css_class"))
    out.setdefault("id_attr", item.get("id_attr"))
    out.setdefault("expected", _expected_from_item(item))
    return out


def _op_visual_values(op: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {"bbox": op.get("bbox") or {}}
    for key in ("text",) + SOURCE_EXPECTED_KEYS:
        value = op.get(key)
        if value is not None and value != "":
            values[key] = value
    return values


def _norm_cmp(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return "|".join(f"{k}:{_norm_cmp(value.get(k))}" for k in sorted(value))
    return str(value).strip().lower()


def _bbox_delta(expected: Any, actual: Any) -> dict[str, Any] | None:
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return None
    keys = ("x", "y", "width", "height")
    delta = {k: round(_num(actual.get(k), 0) - _num(expected.get(k), 0), 2) for k in keys}
    max_abs = max(abs(v) for v in delta.values()) if delta else 0
    return {"delta": delta, "max_abs_delta": max_abs}


def _is_text_like_trace(kind: str, role: str, slot: str, component_type: str) -> bool:
    return (
        kind in {"text", "heading"}
        or role in {"label", "placeholder", "heading", "body_text", "link", "button_text", "content", "footer_text"}
        or slot in {"label", "placeholder", "content", "trailing_action"}
        or component_type == "content_block"
    )


def _comparison_profile_for(trace: dict[str, Any], source_snapshot: dict[str, Any], op_values: dict[str, Any], slot_projection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the fields that are meaningful to compare for this mapping.

    Browser computed styles include inherited text properties on every DOM node;
    comparing all of them against every Penpot shape creates noise. v06.4+ also
    projects composite source nodes into semantic slots, so button labels, input
    placeholders, and icons are compared as child slots rather than full parent
    rectangles.
    """
    kind = str(trace.get("kind") or source_snapshot.get("kind") or "").lower()
    role = str(trace.get("role") or source_snapshot.get("role") or "").lower()
    slot = str(trace.get("slot") or "").lower()
    component_type = str(trace.get("component_type") or "").lower()
    tag = str(source_snapshot.get("tag") or "").lower()
    expected = (slot_projection or {}).get("expected") if isinstance((slot_projection or {}).get("expected"), dict) else (source_snapshot.get("expected") if isinstance(source_snapshot.get("expected"), dict) else {})
    slot_kind = str((slot_projection or {}).get("slot_kind") or "").lower()

    if slot_kind == "text_slot" or _is_text_like_trace(kind, role, slot, component_type):
        fields = {"bbox", "text", "color", "text_color", "font_size", "font_weight", "font_family", "line_height", "text_align", "opacity"}
        intent = "text"
    elif component_type in {"field"} or kind in {"input", "textarea", "select"} or role in {"field", "input", "textbox"}:
        # Text inside an input is often split into a separate placeholder/content
        # layer.  The field surface itself should only be judged as a container.
        fields = {"bbox", "fill", "stroke", "stroke_width", "radius", "opacity"}
        intent = "field_surface"
    elif component_type in {"action"} or kind == "button" or role in {"action", "button", "cta", "link"}:
        fields = {"bbox", "fill", "stroke", "stroke_width", "radius", "opacity"}
        # Composite actions may be split into a button surface plus a separate
        # label/icon layer. Only compare text attributes on this mapping when
        # this actual Penpot op carries text.
        if op_values.get("text") is not None:
            fields |= {"text", "color", "font_size", "font_weight", "line_height", "text_align"}
        intent = "action"
    elif component_type in {"control"} or kind in {"control", "toggle", "checkbox", "radio"} or role in {"control", "checkbox", "radio", "switch"}:
        fields = {"bbox", "fill", "stroke", "stroke_width", "radius", "opacity"}
        intent = "control"
    elif component_type == "icon_container":
        # v06.13: visible wrappers around icon glyphs are surfaces. Compare
        # their own fill/radius, not glyph color/font properties.
        fields = {"bbox", "fill", "stroke", "stroke_width", "radius", "opacity", "box_shadow"}
        intent = "icon_container_surface"
    elif slot_kind == "icon" or component_type in {"media"} or kind in {"icon", "svg", "media"} or role in {"icon", "media", "image", "avatar"}:
        fields = {"bbox", "fill", "color", "opacity"}
        if expected.get("text") is not None or op_values.get("text") is not None:
            fields.add("text")
        intent = "media"
    elif component_type in {"surface", "header", "footer", "navigation", "data_display"} or kind in {"surface", "card", "container", "section", "navigation", "form"} or tag in {"header", "footer"}:
        fields = {"bbox", "fill", "stroke", "stroke_width", "radius", "opacity", "box_shadow"}
        intent = "surface"
    else:
        # Safe generic fallback: compare geometry and any explicit visual paint,
        # but avoid inherited font noise unless text is truly present on the op.
        fields = {"bbox", "fill", "stroke", "stroke_width", "radius", "opacity"}
        if expected.get("text") is not None or op_values.get("text") is not None:
            fields |= {"text", "color", "font_size", "font_weight", "line_height", "text_align"}
        intent = "generic"

    return {
        "schema": "dvcp.source_comparison_profile.v1",
        "intent": intent,
        "component_type": component_type or None,
        "kind": kind or None,
        "role": role or None,
        "slot": slot or None,
        "fields": [field for field in ("bbox", "fill", "stroke", "stroke_width", "color", "text_color", "text", "font_size", "font_weight", "font_family", "line_height", "text_align", "radius", "opacity", "fill_opacity", "box_shadow", "input_type") if field in fields],
    }


def _field_values_equal(field: str, expected: Any, actual: Any) -> bool:
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    if field in {"x", "y", "width", "height", "font_size", "radius", "stroke_width", "opacity", "fill_opacity"}:
        return abs(_num(actual, 0) - _num(expected, 0)) <= 0.75
    if field == "line_height":
        return abs(_num(actual, 0) - _num(expected, 0)) <= 0.75
    if field == "font_weight":
        return _normalise_font_weight(expected) == _normalise_font_weight(actual)
    if field in {"fill", "stroke", "color", "text_color"}:
        return _norm_cmp(expected).replace(" ", "") == _norm_cmp(actual).replace(" ", "")
    return _norm_cmp(expected) == _norm_cmp(actual)




def _line_height_values_equivalent(expected: Any, actual: Any, op_values: dict[str, Any]) -> bool:
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    exp = _num(expected, 0)
    act = _num(actual, 0)
    if exp <= 0 and act <= 0:
        return True
    # v06.13: CSS/source line-height may be px, while Penpot behaves more
    # predictably with a unitless ratio. If the source px is preserved in the
    # op, the mapping is faithful even when op.line_height is the Penpot ratio.
    src_px = _num(op_values.get("source_line_height_px"), 0)
    ratio = _num(op_values.get("penpot_line_height_ratio"), 0)
    font_size = max(_num(op_values.get("font_size"), 14), 1)
    if exp > 4 and act <= 4:
        return abs(src_px - exp) <= 0.75 or abs(act - (exp / font_size)) <= 0.05 or abs(ratio - (exp / font_size)) <= 0.05
    if exp <= 4 and act > 4:
        return abs((act / font_size) - exp) <= 0.05
    return abs(act - exp) <= 0.75


def _normalise_font_name(value: Any) -> str:
    raw = str(value or "").strip().lower().replace('"', '').replace("'", "")
    if not raw:
        return ""
    raw = raw.split(',')[0].strip()
    return re.sub(r"[^a-z0-9]+", "", raw)



def _normalise_font_weight(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    names = {
        "thin": 100, "hairline": 100, "extralight": 200, "extra-light": 200, "ultralight": 200,
        "light": 300, "regular": 400, "normal": 400, "book": 400, "medium": 500,
        "semibold": 600, "semi-bold": 600, "demibold": 600, "demi-bold": 600,
        "bold": 700, "extrabold": 800, "extra-bold": 800, "ultrabold": 800, "black": 900, "heavy": 900,
    }
    m = re.search(r"\d+", raw)
    if m:
        n = int(m.group(0))
    else:
        n = names.get(raw, 0)
    if not n:
        return re.sub(r"[^a-z0-9]+", "", raw)
    n = int(round(n / 100.0) * 100)
    n = max(100, min(900, n))
    return str(n)

def _first_readback_text_style(readbacks: list[dict[str, Any]]) -> dict[str, Any]:
    for read in readbacks or []:
        if not isinstance(read, dict):
            continue
        if read.get("characters") is not None or read.get("text") is not None or read.get("fontFamily") is not None:
            return read
    return {}

def _readback_root_origin(results: list[dict[str, Any]]) -> dict[str, float]:
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("op") != "create_root":
            continue
        reads = result.get("penpot_readback") if isinstance(result.get("penpot_readback"), list) else []
        for read in reads:
            if isinstance(read, dict) and (read.get("x") is not None or read.get("y") is not None):
                return {"x": _num(read.get("x"), 0), "y": _num(read.get("y"), 0)}
    return {"x": 0.0, "y": 0.0}


def _normalise_readback(readbacks: list[Any], root_origin: dict[str, float]) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    ox = _num(root_origin.get("x"), 0)
    oy = _num(root_origin.get("y"), 0)
    for raw in readbacks:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if raw.get("x") is not None:
            item["relative_x"] = round(_num(raw.get("x"), 0) - ox, 3)
        if raw.get("y") is not None:
            item["relative_y"] = round(_num(raw.get("y"), 0) - oy, 3)
        width = raw.get("width", raw.get("w"))
        height = raw.get("height", raw.get("h"))
        if item.get("relative_x") is not None and item.get("relative_y") is not None and width is not None and height is not None:
            item["relative_bbox"] = {
                "x": item["relative_x"],
                "y": item["relative_y"],
                "width": _num(width, 0),
                "height": _num(height, 0),
            }
        normalised.append(item)
    return normalised


def _first_relative_bbox(readbacks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for read in readbacks:
        b = read.get("relative_bbox") if isinstance(read.get("relative_bbox"), dict) else None
        if b:
            return b
    return None






def _bbox_contains_bbox(container: Any, child: Any, pad: float = 0.0) -> bool:
    if not isinstance(container, dict) or not isinstance(child, dict):
        return False
    c = {
        "x": _num(container.get("x"), 0),
        "y": _num(container.get("y"), 0),
        "width": _num(container.get("width"), 0),
        "height": _num(container.get("height"), 0),
    }
    b = {
        "x": _num(child.get("x"), 0),
        "y": _num(child.get("y"), 0),
        "width": _num(child.get("width"), 0),
        "height": _num(child.get("height"), 0),
    }
    return (
        b["x"] >= c["x"] - pad
        and b["y"] >= c["y"] - pad
        and b["x"] + b["width"] <= c["x"] + c["width"] + pad
        and b["y"] + b["height"] <= c["y"] + c["height"] + pad
    )


def _text_without_icon_tokens(text: Any) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    # Material Symbols often arrive as plain words concatenated with labels.
    # Keep this generic: remove common snake_case icon-like tokens while leaving
    # normal copy intact. If nothing remains, return the original text.
    parts = [part for part in re.split(r"\s+", raw) if part]
    filtered = [
        part for part in parts
        if not (
            "_" in part
            and re.match(r"^[A-Za-z0-9_]+$", part)
            and len(part) <= 40
        )
    ]
    return " ".join(filtered).strip() or raw


def _slot_kind_for_trace(trace: dict[str, Any], source_snapshot: dict[str, Any], op_values: dict[str, Any]) -> str:
    slot = str(trace.get("slot") or "").lower()
    target_kind = str(trace.get("kind") or "").lower()
    target_role = str(trace.get("role") or "").lower()
    target_component_type = str(trace.get("component_type") or "").lower()
    source_kind = str(source_snapshot.get("kind") or "").lower()
    source_role = str(source_snapshot.get("role") or "").lower()
    if target_component_type == "icon_container":
        return "source_element"
    if slot in {"leading_icon", "trailing_icon", "icon"} or target_kind in {"icon", "svg"}:
        return "icon"
    if slot in {"label", "placeholder", "content", "trailing_action"} or _is_text_like_trace(target_kind, target_role, slot, target_component_type):
        # If a text layer maps to a larger source component (button/input/card),
        # treat it as a semantic slot instead of comparing it against the whole component.
        if source_kind in {"button", "input", "textarea", "select", "control"} or source_role in {"button_primary", "button_secondary", "input", "field", "control"}:
            return "text_slot"
        return "text"
    if target_component_type in {"field", "action", "control"} or target_kind in {"input", "button", "control"}:
        return "container"
    return "source_element"


def _project_expected_for_slot(
    source_snapshot: dict[str, Any],
    op_values: dict[str, Any],
    trace: dict[str, Any],
) -> dict[str, Any]:
    """Project a full source DOM node into the expected values for one Penpot slot.

    v06.3 compared children such as ButtonText or InputPlaceholder against the
    full source button/input box. v06.4+ keeps the original source snapshot, but
    derives a slot-level expectation for diagnostics. This is deterministic and
    generic: no screen-specific names or LLM interpretation are required.
    """
    expected = dict(source_snapshot.get("expected") if isinstance(source_snapshot.get("expected"), dict) else {})
    slot_kind = _slot_kind_for_trace(trace, source_snapshot, op_values)
    source_bbox = expected.get("bbox") if isinstance(expected.get("bbox"), dict) else None
    target_bbox = op_values.get("bbox") if isinstance(op_values.get("bbox"), dict) else None
    out = dict(expected)
    notes: list[str] = []
    bbox_mode = "equal"

    if slot_kind in {"text_slot", "icon"}:
        # Child layers are expected to live inside the source component, not to
        # have the same rectangle as that component. Keep the source bbox for
        # containment evidence and compare the emitted target bbox against readback.
        if target_bbox:
            out["bbox"] = target_bbox
            bbox_mode = "inside_source"
            notes.append("slot_bbox_projected_from_penpot_op")
        if source_bbox:
            out["source_container_bbox"] = source_bbox

    if slot_kind == "text_slot":
        source_text = str(expected.get("text") or "").strip()
        actual_text = str(op_values.get("text") or "").strip()
        if actual_text:
            if source_text and (_norm_cmp(actual_text) == _norm_cmp(source_text) or _norm_cmp(actual_text) in _norm_cmp(source_text)):
                out["text"] = actual_text
                notes.append("slot_text_derived_from_source_substring")
            elif not source_text:
                out["text"] = actual_text
                notes.append("slot_text_accepted_from_penpot_op_no_source_text")
            # When the source component text contains an icon token, compare the
            # human label against the text slot and leave the icon to its own slot.
            human = _text_without_icon_tokens(source_text)
            if source_text and human and _norm_cmp(actual_text) == _norm_cmp(human):
                out["text"] = actual_text
                notes.append("slot_text_excludes_icon_token")
        # Slot text should not be judged against parent paint properties.
        for key in ("fill", "stroke", "stroke_width", "radius", "box_shadow", "input_type"):
            out.pop(key, None)

    elif slot_kind == "icon":
        # v06.13: distinguish real Material Symbols glyphs from inferred icon
        # slots. Real glyphs must preserve the icon-font family and ligature
        # text; inferred slots still compare only bbox/paint/text.
        fam = str(expected.get("font_family") or expected.get("source_font_family") or op_values.get("font_family") or op_values.get("source_font_family") or "").lower()
        is_material_symbol = bool(expected.get("is_material_symbol") or op_values.get("is_material_symbol") or "material symbols" in fam or "material icons" in fam)
        pop_keys = ["box_shadow", "input_type", "stroke", "stroke_width", "radius"]
        if not is_material_symbol:
            pop_keys += ["font_size", "font_weight", "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align"]
        for key in pop_keys:
            out.pop(key, None)
        icon_color = (
            expected.get("color")
            or expected.get("text_color")
            or op_values.get("color")
            or op_values.get("text_color")
            or op_values.get("fill")
        )
        if icon_color:
            out["fill"] = icon_color
            out["color"] = icon_color
            notes.append("icon_paint_projected_from_source_color")
        if is_material_symbol:
            out["is_material_symbol"] = True
            out["material_symbol_name"] = op_values.get("material_symbol_name") or expected.get("material_symbol_name") or op_values.get("text") or expected.get("text")
            out.setdefault("penpot_line_height_ratio", 1)
            notes.append("material_symbol_font_expected")
        if op_values.get("text") is not None:
            out["text"] = op_values.get("text")
            notes.append("icon_identity_from_penpot_op")

    elif slot_kind == "container":
        # A container/surface keeps the full source node comparison.
        bbox_mode = "equal"

    return {
        "schema": "dvcp.source_slot_projection.v1",
        "version": "v06.13.6",
        "source_ref": source_snapshot.get("source_ref"),
        "slot_kind": slot_kind,
        "slot": trace.get("slot"),
        "component_id": trace.get("component_id"),
        "component_type": trace.get("component_type"),
        "kind": trace.get("kind"),
        "role": trace.get("role"),
        "bbox_mode": bbox_mode,
        "source_container_bbox": source_bbox,
        "expected": out,
        "notes": notes,
    }


def _missing_field_is_implicit_default(field: str, expected: Any) -> bool:
    if field in {"opacity", "fill_opacity"} and abs(_num(expected, 1) - 1) <= 0.01:
        return True
    if field == "stroke_width" and abs(_num(expected, 0)) <= 0.01:
        return True
    if field == "radius" and abs(_num(expected, 0)) <= 0.01:
        return True
    return False


def _missing_field_is_diagnostic_warning(field: str) -> bool:
    # Properties that are valid source evidence but not guaranteed to be emitted
    # as first-class Penpot operation values in the stable SVG materializer.
    return field in {"box_shadow", "font_family", "text_align"}


def _mismatched_field_is_diagnostic_warning(field: str) -> bool:
    # These fields are often normalized differently by Penpot/SVG text rendering.
    # Keep them visible in the report, but do not let them dominate the quality
    # score unless other harder geometry/paint mismatches are present.
    return field in {"font_family", "text_align"}

def _style_drift_diagnostics(
    source_snapshot: dict[str, Any],
    op_values: dict[str, Any],
    shape_ids: list[Any],
    trace: dict[str, Any],
    readback_normalized: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    slot_projection = _project_expected_for_slot(source_snapshot, op_values, trace)
    expected = slot_projection.get("expected") if isinstance(slot_projection.get("expected"), dict) else (source_snapshot.get("expected") if isinstance(source_snapshot.get("expected"), dict) else {})
    profile = _comparison_profile_for(trace, source_snapshot, op_values, slot_projection)
    fields = set(profile.get("fields") or [])
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    origin = str(source_snapshot.get("origin") or "")
    if origin and origin != "rendered_playwright":
        warnings.append({
            "category": "source_match_fallback",
            "severity": "low",
            "origin": origin,
            "message": "Target is traceable, but it was not matched to a direct rendered DOM source.",
            "autofixable": False,
        })

    if not shape_ids:
        issues.append({
            "category": "target_missing",
            "severity": "high",
            "expected": "one_or_more_penpot_shapes",
            "actual": "none",
            "autofixable": False,
        })

    for key, value in expected.items():
        if key == "bbox" or value is None or value == "":
            continue
        if key not in fields:
            # Computed CSS may contain inherited values that do not belong on
            # this Penpot shape profile. Filter them instead of reporting noise.
            continue
        if key not in op_values:
            if _missing_field_is_implicit_default(key, value):
                warnings.append({
                    "category": "expected_value_implicit_default",
                    "field": key,
                    "severity": "info",
                    "expected": value,
                    "actual": "implicit_default",
                    "autofixable": False,
                })
                continue
            if _missing_field_is_diagnostic_warning(key):
                warnings.append({
                    "category": "expected_value_not_sent_to_penpot",
                    "field": key,
                    "severity": "low",
                    "expected": value,
                    "actual": None,
                    "autofixable": True,
                })
                continue
            issues.append({
                "category": "expected_value_not_sent_to_penpot",
                "field": key,
                "severity": "medium" if key not in {"fill", "color", "text"} else "high",
                "expected": value,
                "actual": None,
                "autofixable": True,
            })
        elif key == "line_height" and _line_height_values_equivalent(value, op_values.get(key), op_values):
            continue
        elif not _field_values_equal(key, value, op_values.get(key)):
            if _mismatched_field_is_diagnostic_warning(key):
                warnings.append({
                    "category": "source_vs_penpot_op_warning",
                    "field": key,
                    "severity": "low",
                    "expected": value,
                    "actual": op_values.get(key),
                    "autofixable": True,
                })
            else:
                issues.append({
                    "category": "source_vs_penpot_op_mismatch",
                    "field": key,
                    "severity": "medium" if key not in {"fill", "color", "text"} else "high",
                    "expected": value,
                    "actual": op_values.get(key),
                    "autofixable": True,
                })

    if isinstance(expected.get("bbox"), dict) and isinstance(op_values.get("bbox"), dict):
        if slot_projection.get("bbox_mode") == "inside_source" and isinstance(slot_projection.get("source_container_bbox"), dict):
            if not _bbox_contains_bbox(slot_projection.get("source_container_bbox"), op_values.get("bbox"), pad=3):
                issues.append({
                    "category": "slot_bbox_outside_source",
                    "field": "bbox",
                    "severity": "medium",
                    "expected": {"inside": slot_projection.get("source_container_bbox")},
                    "actual": op_values.get("bbox"),
                    "autofixable": True,
                })
        else:
            bd = _bbox_delta(expected.get("bbox"), op_values.get("bbox"))
            if bd and bd["max_abs_delta"] > 2:
                issues.append({
                    "category": "bbox_mismatch",
                    "field": "bbox",
                    "severity": "medium" if bd["max_abs_delta"] <= 12 else "high",
                    "expected": expected.get("bbox"),
                    "actual": op_values.get("bbox"),
                    "delta": bd,
                    "autofixable": True,
                })

    readback_normalized = readback_normalized or []
    if not readback_normalized and shape_ids:
        warnings.append({
            "category": "penpot_readback_missing",
            "severity": "low",
            "message": "Shape was created, but no readback geometry was available for deterministic comparison.",
            "autofixable": False,
        })
    elif isinstance(op_values.get("bbox"), dict):
        rb = _first_relative_bbox(readback_normalized)
        if rb:
            rd = _bbox_delta(op_values.get("bbox"), rb)
            if rd and rd["max_abs_delta"] > 2:
                issues.append({
                    "category": "penpot_readback_bbox_mismatch",
                    "field": "bbox",
                    "severity": "medium" if rd["max_abs_delta"] <= 12 else "high",
                    "expected": op_values.get("bbox"),
                    "actual": rb,
                    "delta": rd,
                    "coordinate_space": "root_relative",
                    "autofixable": True,
                })

    # v06.13.1: readback text fidelity diagnostics. The deterministic
    # contract is source computed style -> Penpot op -> Penpot readback.
    # Geometry is checked above; here we verify text, font size, weight, family
    # and line-height as first-class values instead of treating them as visual hints.
    if shape_ids and ({"font_family", "font_weight", "font_size", "line_height", "text"} & set(fields)):
        rb_style = _first_readback_text_style(readback_normalized)
        actual_font = rb_style.get("fontFamily") if isinstance(rb_style, dict) else None
        expected_font = expected.get("font_family") or op_values.get("font_family") or op_values.get("source_font_family")
        if expected_font and actual_font and _normalise_font_name(expected_font) != _normalise_font_name(actual_font):
            expected_norm = _normalise_font_name(expected_font)
            is_material_expected = "materialsymbols" in expected_norm or "materialicons" in expected_norm
            item = {
                "category": "material_symbol_font_mismatch" if is_material_expected else "font_family_fallback",
                "field": "font_family",
                "severity": "high" if is_material_expected else "low",
                "expected": expected_font,
                "actual": actual_font,
                "message": "Material Symbol glyph rendered with a normal text font; the ligature will appear as a word." if is_material_expected else "Penpot readback font differs from the source font; layout/content passed but font fidelity is approximate.",
                "autofixable": bool(is_material_expected),
            }
            if is_material_expected:
                issues.append(item)
            else:
                warnings.append(item)
        expected_weight = expected.get("font_weight") or op_values.get("font_weight")
        actual_weight = rb_style.get("fontWeight") if isinstance(rb_style, dict) else None
        if actual_weight is None and expected_weight is not None:
            warnings.append({
                "category": "font_weight_readback_unavailable",
                "field": "font_weight",
                "severity": "info",
                "expected": expected_weight,
                "actual": None,
                "message": "Penpot readback did not expose fontWeight, so weight fidelity cannot be verified deterministically.",
                "autofixable": False,
            })
        elif expected_weight is not None and actual_weight is not None and _normalise_font_weight(expected_weight) != _normalise_font_weight(actual_weight):
            issues.append({
                "category": "font_weight_mismatch",
                "field": "font_weight",
                "severity": "medium",
                "expected": expected_weight,
                "actual": actual_weight,
                "message": "Penpot text weight differs from Stitch computed style; material symbol fidelity should preserve the source weight.",
                "autofixable": True,
            })

        expected_text = op_values.get("text") or expected.get("text")
        actual_text = rb_style.get("characters") if isinstance(rb_style, dict) else None
        if actual_text is None and isinstance(rb_style, dict):
            actual_text = rb_style.get("text")
        if expected_text is not None and actual_text is not None and str(expected_text) != str(actual_text):
            issues.append({
                "category": "text_content_mismatch",
                "field": "text",
                "severity": "high",
                "expected": expected_text,
                "actual": actual_text,
                "message": "Penpot text content differs from Stitch rendered text.",
                "autofixable": True,
            })

        expected_size = op_values.get("font_size") or expected.get("font_size")
        actual_size = rb_style.get("fontSize") if isinstance(rb_style, dict) else None
        if expected_size is not None and actual_size is not None and abs(_num(expected_size, 0) - _num(actual_size, 0)) > 0.2:
            issues.append({
                "category": "font_size_mismatch",
                "field": "font_size",
                "severity": "medium",
                "expected": expected_size,
                "actual": actual_size,
                "message": "Penpot font size differs from Stitch computed style.",
                "autofixable": True,
            })

        expected_line_ratio = op_values.get("penpot_line_height_ratio") or expected.get("penpot_line_height_ratio")
        if expected_line_ratio is None:
            raw_lh = op_values.get("line_height") or expected.get("line_height")
            if raw_lh is not None and _num(raw_lh, 0) <= 4:
                expected_line_ratio = raw_lh
        actual_line_ratio = rb_style.get("lineHeight") if isinstance(rb_style, dict) else None
        if expected_line_ratio is not None and actual_line_ratio is not None and abs(_num(expected_line_ratio, 0) - _num(actual_line_ratio, 0)) > 0.015:
            issues.append({
                "category": "line_height_mismatch",
                "field": "line_height",
                "severity": "medium",
                "expected": expected_line_ratio,
                "actual": actual_line_ratio,
                "message": "Penpot line-height differs from the Stitch computed line-height ratio.",
                "autofixable": True,
            })

        expected_lines = _num(op_values.get("expected_line_count") or op_values.get("source_line_count"), 0)
        expected_grow = str(op_values.get("penpot_grow_type") or "").lower()
        actual_grow = str(rb_style.get("growType") if isinstance(rb_style, dict) else "").lower()
        if expected_lines and expected_lines <= 1.15 and op_values.get("text_no_wrap"):
            if expected_grow == "auto-width" and actual_grow and actual_grow != "auto-width":
                issues.append({
                    "category": "single_line_text_wrap_risk",
                    "field": "growType",
                    "severity": "medium",
                    "expected": "auto-width",
                    "actual": rb_style.get("growType"),
                    "message": "Stitch rendered this text as one line; Penpot kept a fixed text box, so it may wrap visually.",
                    "autofixable": True,
                })
            else:
                # Verified no-wrap is not a drift warning. Keep exact quality
                # when source and Penpot values already match.
                pass

    high = sum(1 for issue in issues if issue.get("severity") == "high")
    medium = sum(1 for issue in issues if issue.get("severity") == "medium")
    low = sum(1 for issue in issues if issue.get("severity") == "low")
    score = max(0, 100 - high * 35 - medium * 18 - low * 8 - min(len(warnings), 6) * 2)
    if origin and origin != "rendered_playwright":
        score = min(score, 64)
    if not shape_ids:
        score = 0
    if not shape_ids:
        quality = "unmatched"
    elif origin and origin != "rendered_playwright":
        quality = "fallback"
    elif high or medium:
        quality = "drift"
    elif warnings:
        quality = "close"
    else:
        quality = "exact"
    metrics = {
        "profile": profile,
        "slot_projection": slot_projection,
        "score": score,
        "quality": quality,
        "issue_count": len(issues),
        "warning_count": len(warnings),
    }
    return issues, warnings, metrics


def _source_to_penpot_map_from_results(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    issue_counts: dict[str, int] = {}
    warning_counts: dict[str, int] = {}
    quality_counts: dict[str, int] = {}
    slot_kind_counts: dict[str, int] = {}
    source_fidelity_counts: dict[str, int] = {}
    deterministic_rule_counts: dict[str, int] = {}
    mapped = 0
    root_origin = _readback_root_origin(results)
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("op") in {"create_root", "finalize_import_job"}:
            continue
        trace = result.get("source_trace") if isinstance(result.get("source_trace"), dict) else None
        if not trace or not trace.get("source_ref"):
            continue
        source_snapshot = trace.get("source_snapshot") if isinstance(trace.get("source_snapshot"), dict) else {}
        source_fidelity = trace.get("source_fidelity") if isinstance(trace.get("source_fidelity"), dict) else {}
        if source_fidelity:
            key = str(source_fidelity.get("mode") or "unknown")
            source_fidelity_counts[key] = source_fidelity_counts.get(key, 0) + 1
        deterministic_transform = trace.get("deterministic_transform") if isinstance(trace.get("deterministic_transform"), dict) else {}
        if deterministic_transform:
            rule_key = str(deterministic_transform.get("rule_id") or "unknown")
            deterministic_rule_counts[rule_key] = deterministic_rule_counts.get(rule_key, 0) + 1
        op_values = trace.get("penpot_op_values") if isinstance(trace.get("penpot_op_values"), dict) else {}
        shape_ids = [str(x) for x in (result.get("created_shape_ids") or []) if x]
        readbacks = result.get("penpot_readback") if isinstance(result.get("penpot_readback"), list) else []
        readback_normalized = _normalise_readback(readbacks, root_origin)
        issues, warnings, metrics = _style_drift_diagnostics(source_snapshot, op_values, shape_ids, trace, readback_normalized)
        for issue in issues:
            key = str(issue.get("category") or "unknown")
            issue_counts[key] = issue_counts.get(key, 0) + 1
        for warning in warnings:
            key = str(warning.get("category") or "unknown")
            warning_counts[key] = warning_counts.get(key, 0) + 1
        quality = str(metrics.get("quality") or "unknown")
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
        slot_projection = metrics.get("slot_projection") if isinstance(metrics.get("slot_projection"), dict) else {}
        slot_kind = str(slot_projection.get("slot_kind") or "unknown")
        slot_kind_counts[slot_kind] = slot_kind_counts.get(slot_kind, 0) + 1
        mappings.append({
            "schema": "dvcp.source_to_penpot_mapping.v2",
            "source_ref": trace.get("source_ref"),
            "source_name": trace.get("source_name"),
            "source_snapshot": source_snapshot,
            "component_id": trace.get("component_id"),
            "component_type": trace.get("component_type"),
            "role": trace.get("role"),
            "kind": trace.get("kind"),
            "slot": trace.get("slot"),
            "op_index": result.get("op_index"),
            "op": result.get("op"),
            "penpot_shape_ids": shape_ids,
            "penpot_op_values": op_values,
            "penpot_readback": readbacks,
            "penpot_readback_normalized": readback_normalized,
            "coordinate_space": {
                "source": "root_relative",
                "penpot_readback": "absolute_canvas",
                "penpot_readback_normalized": "root_relative",
                "root_origin": root_origin,
            },
            "comparison_profile": metrics.get("profile"),
            "source_slot_projection": metrics.get("slot_projection"),
            "source_fidelity": trace.get("source_fidelity"),
            "deterministic_transform": trace.get("deterministic_transform"),
            "mapping_score": metrics.get("score"),
            "mapping_quality": metrics.get("quality"),
            "issues": issues,
            "warnings": warnings,
        })
        if shape_ids:
            mapped += 1
    real_issue_count = sum(issue_counts.values())
    warning_count = sum(warning_counts.values())
    report = {
        "schema": "dvcp.source_penpot_deterministic_report.v7",
        "mode": "source_map_no_llm",
        "cleanup": "v06.13.6_icon_only_no_label_fidelity",
        "mapping_count": len(mappings),
        "mapped_shape_count": mapped,
        "issue_count": real_issue_count,
        "warning_count": warning_count,
        "issue_counts": issue_counts,
        "warning_counts": warning_counts,
        "mapping_quality_counts": quality_counts,
        "slot_kind_counts": slot_kind_counts,
        "source_fidelity_counts": source_fidelity_counts,
        "deterministic_transform_rule_counts": deterministic_rule_counts,
        "coordinate_space": {
            "source": "root_relative",
            "penpot_readback": "absolute_canvas",
            "penpot_readback_normalized": "root_relative",
            "root_origin": root_origin,
        },
        "status": "passed" if real_issue_count == 0 else "issues_detected",
    }
    return mappings, report




def _is_material_symbol_op_values(op: dict[str, Any]) -> bool:
    family = str(op.get("font_family") or op.get("source_font_family") or "").lower()
    css = str(op.get("css_class") or "").lower()
    return bool(op.get("is_material_symbol") or "material symbols" in family or "material icons" in family or "material-symbol" in css)


def _normalise_text_fidelity_fields(op: dict[str, Any]) -> None:
    kind = str(op.get("kind") or "").lower()
    role = str(op.get("role") or "").lower()
    slot = str(op.get("slot") or "").lower()
    has_text = op.get("text") is not None or kind == "text" or slot in {"label", "content", "text_slot"} or role in {"label", "heading", "body_text", "link", "button_text", "placeholder"}
    if not has_text:
        return
    font_size = max(_num(op.get("font_size"), 14), 1)
    raw_lh = op.get("line_height")
    if raw_lh is None or raw_lh == "":
        source_px = font_size * 1.2
        ratio = 1.2
    else:
        lh = _num(raw_lh, 0)
        if lh > 4:
            source_px = lh
            ratio = source_px / font_size
        elif lh > 0:
            ratio = lh
            source_px = _num(op.get("source_line_height_px"), font_size * ratio)
        else:
            source_px = font_size * 1.2
            ratio = 1.2
    ratio = max(1.0, min(2.4, ratio))
    op.setdefault("source_line_height_px", round(source_px, 3))
    op["penpot_line_height_ratio"] = round(ratio, 3)
    # Penpot receives a ratio in line_height; source px is preserved separately.
    op["line_height"] = round(ratio, 3)
    if op.get("font_family") and not op.get("source_font_family"):
        op["source_font_family"] = op.get("font_family")

    bbox = op.get("bbox") if isinstance(op.get("bbox"), dict) else {}
    source_h = _num(bbox.get("height"), 0)
    source_line_count = source_h / max(source_px, 1) if source_h > 0 else 1.0
    if source_line_count <= 0:
        source_line_count = 1.0
    op["source_line_count"] = round(source_line_count, 3)
    op["expected_line_count"] = max(1, int(round(source_line_count))) if source_line_count > 1.15 else 1
    is_single_line = source_line_count <= 1.15
    if is_single_line:
        op["text_no_wrap"] = True
    is_material = _is_material_symbol_op_values(op) or kind == "icon"
    # v06.13.6: if Stitch/Chromium rendered it as one visual line, Penpot must
    # not wrap it. This applies to centered headings too. Icons remain fixed.
    if is_single_line and not is_material:
        op["penpot_grow_type"] = "auto-width"
    else:
        op.setdefault("penpot_grow_type", "fixed")

def _item_to_op(
    item: dict[str, Any],
    index: int,
    job_id: str,
    total: int,
    offset: dict[str, float],
    root_name: str = "ImportedStitchScreen",
    visual_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind = str(item.get("kind") or "card").lower()
    role = str(item.get("role") or "")
    name = _safe_name(item.get("name"), f"Element{index}")
    bbox = _bbox(item)
    base = {
        "job_id": job_id,
        "root_name": root_name,
        "op_index": index + 1,  # +1 because op 0 is root
        "op_total": total,
        "source_name": item.get("name"),
        "name": name,
        "role": role,
        "kind": kind,
        "bbox": bbox,
        "root_offset": offset,
        "z_index": _semantic_z(item, index),
        "source_order": index,
        "source_ref": str(item.get("source_ref") or f"queue_{index:03d}"),
        "source_snapshot": _source_snapshot_for_item(item, index),
        "source_fidelity": item.get("source_fidelity"),
        "deterministic_transform": item.get("deterministic_transform"),
    }
    base["source_trace"] = {
        "schema": "dvcp.source_trace.v1",
        "source_ref": base["source_ref"],
        "source_name": item.get("source_name") or item.get("name"),
        "source_snapshot": base["source_snapshot"],
        "penpot_op_values": _op_visual_values(base),
        "component_id": item.get("component_id"),
        "component_type": item.get("component_type"),
        "role": role,
        "kind": kind,
        "slot": item.get("slot"),
        "source_fidelity": item.get("source_fidelity"),
        "deterministic_transform": item.get("deterministic_transform"),
    }

    if visual_context:
        base["visual_context"] = visual_context

    if item.get("text") is not None:
        base["text"] = str(item.get("text") or "")[:500]
    # Preserve rendered/computed visual styles from the Playwright extractor.
    # The queue stays generic: each op is still tiny, but it can carry the
    # actual CSS-derived appearance for any Stitch design.
    for style_key in (
        "color",
        "text_color",
        "font_size",
        "font_weight",
        "font_family",
        "source_font_family",
        "line_height",
        "source_line_height_px",
        "penpot_line_height_ratio",
        "text_align",
        "radius",
        "fill",
        "fill_opacity",
        "stroke",
        "stroke_width",
        "opacity",
        "box_shadow",
        "input_type",
        "media_alt",
        "is_material_symbol",
        "material_symbol_name",
        "svg",
        "tag",
        "z_index",
        "layer_order",
        "component_id",
        "component_type",
        "attach_to",
        "slot",
        "source_ref",
        "source_snapshot",
        "source_trace",
        "source_fidelity",
        "deterministic_transform",
        "dom_path",
        "ghost",
        "text_no_wrap",
        "expected_line_count",
        "source_line_count",
        "penpot_grow_type",
        "allow_no_shape",
    ):
        if item.get(style_key) is not None:
            base[style_key] = item.get(style_key)

    _normalise_text_fidelity_fields(base)

    # Refresh op values after optional visual fields were copied.
    base["source_trace"]["penpot_op_values"] = _op_visual_values(base)

    if kind == "text":
        base["op"] = "create_text"
    elif kind == "input":
        base["op"] = "create_input"
        base.setdefault("radius", 12)
    elif kind == "button":
        base["op"] = "create_button"
        base.setdefault("radius", 12)
    elif kind == "svg":
        base["op"] = "create_svg"
    elif kind == "icon":
        # Icons are optional in the rendered extractor. When enabled, keep them
        # as editable text glyphs instead of polluting normal body text.
        base["op"] = "create_icon"
        base.setdefault("font_size", item.get("font_size") or 18)
    elif kind in {"card", "section", "navigation", "data_display", "table", "chart", "control", "toggle", "upload", "surface", "container", "form", "media"}:
        # Generic visual containers from rendered DOM become editable cards.
        # They are drawn before text by the queue's source order.
        base["op"] = "create_card"
        base.setdefault("radius", item.get("radius") if item.get("radius") is not None else 18)
    else:
        base["op"] = "create_card"
        base.setdefault("radius", item.get("radius") or 12)

    return base


def build_stitch_import_queue(spec: dict[str, Any]) -> dict[str, Any]:
    """Convert ExternalDesignSpec to a resumable Penpot import job."""
    screen_name = _safe_name(spec.get("screen_name"), "ImportedStitchScreen")
    offset = _root_offset(spec)
    # v06.13.6 strict image rule: actual HTML <img> nodes must never be
    # materialized in Penpot in this import path. The deterministic planner
    # already removes them; this queue filter is a defensive guard.
    raw_children = [
        child for child in (spec.get("children") or [])
        if isinstance(child, dict) and str(child.get("tag") or "").lower() != "img"
    ]
    assembled_children, assembly_summary = semantic_component_assembly(raw_children)
    children = _sorted_children_for_queue(assembled_children)
    # +2 because op 0 is root and the last op restacks/finalizes the job.
    total = len(children) + 2
    job_id = f"stitch_{screen_name}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    ops: list[dict[str, Any]] = [
        {
            "job_id": job_id,
            "root_name": screen_name,
            "op_index": 0,
            "op_total": total,
            "op": "create_root",
            "name": screen_name,
            "role": "screen_root",
            "kind": "screen_root",
            "bbox": {
                "x": offset["x"],
                "y": offset["y"],
                "width": max(_num(spec.get("width"), 390), 1),
                "height": max(_num(spec.get("height"), 860), 1),
            },
            "fill": (spec.get("tokens") or {}).get("color.background.canvas", "#F8FAFC"),
            "stroke": "#CBD5E1",
            "stroke_width": 1,
            "radius": 0,
            "root_offset": {"x": 0, "y": 0},
            "spec_summary": {
                "schema": spec.get("schema"),
                "source": spec.get("source"),
                "import_mode": spec.get("import_mode"),
                "screen_name": spec.get("screen_name"),
                "screen_title": spec.get("screen_title"),
                "screen_type": spec.get("screen_type"),
                "width": spec.get("width"),
                "height": spec.get("height"),
                "child_count": len(children),
                "component_assembly": assembly_summary,
                "source_trace": (spec.get("metadata") or {}).get("source_trace"),
                "metadata": _compact_metadata(spec.get("metadata")),
            },
        }
    ]

    tokens = spec.get("tokens") if isinstance(spec.get("tokens"), dict) else {}
    for index, child in enumerate(children):
        visual_context = _visual_context_for(child, assembled_children, tokens)
        ops.append(_item_to_op(child, index, job_id, total, offset, screen_name, visual_context=visual_context))

    ops.append(
        {
            "job_id": job_id,
            "root_name": screen_name,
            "op_index": len(ops),
            "op_total": total,
            "op": "finalize_import_job",
            "name": f"Finalize_{screen_name}",
            "role": "finalize",
            "kind": "finalize",
            "root_offset": offset,
            "expected_shape_count": len(children),
            "debug": {
                "children_in": len(raw_children),
                "children_queued": len(children),
                "component_assembly": assembly_summary,
            },
        }
    )

    return {
        "type": "stitch_import_queue",
        "job_id": job_id,
        "status": "prepared",
        "prepared_at": utc_now_iso(),
        "source": "stitch",
        "import_strategy": "queue_execute_code",
        "screen_name": spec.get("screen_name"),
        "screen_title": spec.get("screen_title"),
        "screen_type": spec.get("screen_type"),
        "width": spec.get("width"),
        "height": spec.get("height"),
        "cursor": 0,
        "total_ops": len(ops),
        "created_shape_ids": [],
        "results": [],
        "component_assembly_summary": assembly_summary,
        "ops": ops,
    }


def aggregate_stitch_import_queue_results(job: dict[str, Any]) -> dict[str, Any]:
    results = [item for item in (job.get("results") or []) if isinstance(item, dict)]
    checked = sum(int(item.get("checked_count") or 0) for item in results)
    applied = sum(int(item.get("applied_count") or 0) for item in results)
    failed = sum(int(item.get("failed_count") or 0) for item in results)
    created = sum(int(item.get("created_shape_count") or 0) for item in results)
    all_applied = bool(results) and len(results) == int(job.get("total_ops") or 0) and failed == 0
    first_error = next((item.get("error") for item in results if item.get("error")), None)

    visual_items = [item.get("visual_materialization") for item in results if isinstance(item.get("visual_materialization"), dict)]
    visual_method_counts: dict[str, int] = {}
    visually_materialized = 0
    visual_fallbacks = 0
    visual_unknown = 0
    visual_adjustment_counts: dict[str, int] = {}
    visual_intent_counts: dict[str, int] = {}
    contrast_checked = 0
    for visual in visual_items:
        method = str(visual.get("method") or "unknown")
        visual_method_counts[method] = visual_method_counts.get(method, 0) + 1
        intent = str(visual.get("visual_intent") or "unknown")
        visual_intent_counts[intent] = visual_intent_counts.get(intent, 0) + 1
        if visual.get("visually_materialized") is True:
            visually_materialized += 1
        elif visual.get("visually_materialized") is False:
            visual_unknown += 1
        else:
            visual_unknown += 1
        if visual.get("fallback_used"):
            visual_fallbacks += 1
        if isinstance(visual.get("contrast"), dict):
            contrast_checked += 1
        for adjustment in visual.get("adjustments") or []:
            key = str(adjustment or "unknown")
            visual_adjustment_counts[key] = visual_adjustment_counts.get(key, 0) + 1

    visual_summary = {
        "schema": "dvcp.visual_materialization_summary.v2",
        "checked_ops": len(visual_items),
        "visually_materialized_count": visually_materialized,
        "visual_unknown_count": visual_unknown,
        "fallback_count": visual_fallbacks,
        "contrast_checked_count": contrast_checked,
        "method_counts": visual_method_counts,
        "intent_counts": visual_intent_counts,
        "adjustment_counts": visual_adjustment_counts,
    }

    source_map, source_report = _source_to_penpot_map_from_results(results)

    return {
        "all_applied": all_applied,
        "action": "import_external_design_spec",
        "import_strategy": "queue_execute_code",
        "job_id": job.get("job_id"),
        "status": "completed" if all_applied else job.get("status", "paused"),
        "cursor": job.get("cursor", 0),
        "total_ops": job.get("total_ops", 0),
        "checked_count": checked,
        "applied_count": applied,
        "failed_count": failed,
        "created_shape_count": created,
        "created_shape_ids": job.get("created_shape_ids", []),
        "error": None if all_applied else (first_error or "queue_not_completed"),
        "results_preview": results[-10:],
        "visual_materialization_summary": visual_summary,
        "component_assembly_summary": job.get("component_assembly_summary"),
        "source_to_penpot_map": source_map,
        "source_trace_report": source_report,
    }
