"""Validate/sanitize LLM-guided Stitch ExternalDesignSpec plans."""
from __future__ import annotations

import json
import os
import re
from typing import Any

ALLOWED_KINDS = {"screen_root", "surface", "container", "card", "section", "navigation", "data_display", "table", "chart", "text", "heading", "input", "button", "control", "icon", "svg", "media"}
VISUAL_KEYS = (
    "tag", "text", "fill", "stroke", "stroke_width", "color", "text_color", "font_size", "font_weight",
    "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align", "radius", "opacity", "fill_opacity", "box_shadow",
    "input_type", "svg", "css_class", "id_attr", "dom_path", "source_ref", "source_snapshot",
    "z_index", "layer_order", "component_id",
    "component_type", "attach_to", "slot", "deterministic_transform", "ghost", "allow_no_shape",
)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = str(text or "").strip()
    if not text:
        return None
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else None
    except Exception:
        pass
    if text.startswith("```"):
        for part in text.split("```"):
            part = part.strip()
            part = part[4:].strip() if part.startswith("json") else part
            try:
                val = json.loads(part)
                return val if isinstance(val, dict) else None
            except Exception:
                pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            val = json.loads(text[start : end + 1])
            return val if isinstance(val, dict) else None
        except Exception:
            return None
    return None


def parse_llm_build_plan(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            else:
                parts.append(str(item))
        content = "\n".join(parts)
    return _extract_json_object(str(content or "")) or {"schema": "dvcp.external_design_spec.v1", "error": "llm_json_parse_failed"}


def _num(v: Any, default: float = 0.0) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("px", "").strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _rendered_import_icons_enabled() -> bool:
    # This is the single switch for icon inference. When Stitch rendered icon
    # extraction is enabled, rendered Material Symbol glyphs are the source of
    # truth and no heuristic icon layers should be invented. When it is off,
    # inference remains the fallback so older/non-icon extraction runs still
    # produce usable affordances.
    return _env_bool("STITCH_RENDERED_IMPORT_ICONS", False)


def _bbox(v: Any) -> dict[str, float] | None:
    if not isinstance(v, dict):
        return None
    w, h = _num(v.get("width")), _num(v.get("height"))
    if w <= 0 or h <= 0:
        return None
    return {"x": _num(v.get("x")), "y": _num(v.get("y")), "width": w, "height": h}


def _safe_name(value: Any, fallback: str) -> str:
    text = str(value or fallback).strip() or fallback
    text = re.sub(r"[^A-Za-z0-9_ÁÉÍÓÚÜÑáéíóúüñ -]+", "", text)
    text = re.sub(r"\s+", "", text)
    return text[:80] or fallback


def _iter_plan_children(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept both flat `children` and nested `components`/`layers` plans."""
    out: list[dict[str, Any]] = []

    def walk(node: Any, inherited_role: str = "") -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, inherited_role)
            return
        if not isinstance(node, dict):
            return

        has_bbox = isinstance(node.get("bbox"), dict)
        if has_bbox:
            item = dict(node)
            if inherited_role and not item.get("role"):
                item["role"] = inherited_role
            out.append(item)

        role = str(node.get("role") or node.get("type") or node.get("kind") or inherited_role or "")
        for key in ("children", "components", "layers", "items"):
            if isinstance(node.get(key), list):
                walk(node[key], role)

    if isinstance(plan.get("children"), list):
        walk(plan["children"])
    elif isinstance(plan.get("components"), list):
        walk(plan["components"])
    elif isinstance(plan.get("layers"), list):
        walk(plan["layers"])
    return out


SOURCE_EXPECTED_KEYS = (
    "fill", "stroke", "stroke_width", "color", "text_color", "font_size", "font_weight",
    "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align", "radius", "opacity", "fill_opacity",
    "box_shadow", "input_type",
)


def _bbox_area_value(b: dict[str, float] | None) -> float:
    if not b:
        return 0.0
    return max(_num(b.get("width"), 0), 0.0) * max(_num(b.get("height"), 0), 0.0)


def _bbox_iou(a: dict[str, float] | None, b: dict[str, float] | None) -> float:
    if not a or not b:
        return 0.0
    ax1, ay1 = _num(a.get("x"), 0), _num(a.get("y"), 0)
    ax2, ay2 = ax1 + _num(a.get("width"), 0), ay1 + _num(a.get("height"), 0)
    bx1, by1 = _num(b.get("x"), 0), _num(b.get("y"), 0)
    bx2, by2 = bx1 + _num(b.get("width"), 0), by1 + _num(b.get("height"), 0)
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(ix2 - ix1, 0.0), max(iy2 - iy1, 0.0)
    inter = iw * ih
    union = _bbox_area_value(a) + _bbox_area_value(b) - inter
    return inter / union if union > 0 else 0.0


def _bbox_center_distance(a: dict[str, float] | None, b: dict[str, float] | None) -> float:
    if not a or not b:
        return 1_000_000.0
    ax = _num(a.get("x"), 0) + _num(a.get("width"), 0) / 2
    ay = _num(a.get("y"), 0) + _num(a.get("height"), 0) / 2
    bx = _num(b.get("x"), 0) + _num(b.get("width"), 0) / 2
    by = _num(b.get("y"), 0) + _num(b.get("height"), 0) / 2
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _expected_from_child(child: dict[str, Any]) -> dict[str, Any]:
    expected: dict[str, Any] = {}
    bbox = _bbox(child.get("bbox")) or child.get("bbox") or {}
    expected["bbox"] = bbox
    if child.get("text") is not None:
        expected["text"] = str(child.get("text") or "")[:500]
    for key in SOURCE_EXPECTED_KEYS:
        value = child.get(key)
        if value is not None and value != "":
            expected[key] = value
    return expected


def _make_source_snapshot(child: dict[str, Any], source_ref: str, *, origin: str = "rendered_playwright") -> dict[str, Any]:
    existing = child.get("source_snapshot")
    if isinstance(existing, dict):
        snap = dict(existing)
        snap.setdefault("schema", "dvcp.source_element_snapshot.v1")
        snap.setdefault("source_ref", source_ref)
        snap.setdefault("origin", origin)
        snap.setdefault("name", child.get("name"))
        snap.setdefault("kind", child.get("kind"))
        snap.setdefault("role", child.get("role"))
        snap.setdefault("tag", child.get("tag"))
        snap.setdefault("dom_path", child.get("dom_path"))
        snap.setdefault("css_class", child.get("css_class"))
        snap.setdefault("id_attr", child.get("id_attr"))
        snap.setdefault("expected", _expected_from_child(child))
        return snap
    return {
        "schema": "dvcp.source_element_snapshot.v1",
        "source_ref": source_ref,
        "origin": origin,
        "name": child.get("name"),
        "kind": child.get("kind"),
        "role": child.get("role"),
        "tag": child.get("tag"),
        "dom_path": child.get("dom_path"),
        "css_class": child.get("css_class"),
        "id_attr": child.get("id_attr"),
        "expected": _expected_from_child(child),
    }


def _ensure_source_traces(children: list[dict[str, Any]], *, origin: str = "rendered_playwright") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, child in enumerate(children):
        item = dict(child)
        source_ref = str(item.get("source_ref") or f"rendered_{index:03d}")
        item["source_ref"] = source_ref
        item["source_snapshot"] = _make_source_snapshot(item, source_ref, origin=origin)
        out.append(item)
    return out


def _has_visual_paint(child: dict[str, Any]) -> bool:
    for key in ("fill", "stroke", "color", "text_color"):
        value = child.get(key)
        if value is None:
            continue
        text = str(value).strip().lower()
        if text and text not in {"none", "transparent", "rgba(0,0,0,0)", "rgba(0, 0, 0, 0)"}:
            return True
    return False


def _is_text_like_kind_or_role(kind: str, role: str) -> bool:
    return kind in {"text", "heading"} or role in {"label", "placeholder", "heading", "body_text", "link", "button_text", "content"}


def _is_visual_container_kind_or_role(kind: str, role: str) -> bool:
    return kind in {"surface", "container", "card", "section", "navigation", "form", "media"} or role in {"surface", "card", "section", "panel", "navigation", "container"}


def _match_source_child(child: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    cb = _bbox(child.get("bbox"))
    ctext = str(child.get("text") or "").strip().lower()
    ckind = str(child.get("kind") or "").lower()
    crole = str(child.get("role") or "").lower()
    candidates: list[tuple[float, int, dict[str, Any]]] = []
    for index, source in enumerate(sources):
        sb = _bbox(source.get("bbox"))
        if not sb:
            continue
        stext = str(source.get("text") or "").strip().lower()
        skind = str(source.get("kind") or "").lower()
        srole = str(source.get("role") or "").lower()
        sclass = str(source.get("css_class") or "").lower()
        iou = _bbox_iou(cb, sb)
        dist = _bbox_center_distance(cb, sb)

        # v06.6 source fidelity: generated visual wrappers must not steal an
        # arbitrary nearby text/header source. A planned badge/logo/link-layout
        # container is more honest as planned_unmatched unless it overlaps a
        # compatible rendered container strongly enough.
        child_is_container = _is_visual_container_kind_or_role(ckind, crole)
        source_is_text = _is_text_like_kind_or_role(skind, srole)
        source_is_container = _is_visual_container_kind_or_role(skind, srole) or skind in {"input", "button", "control"}
        if child_is_container and not ctext:
            if source_is_text and iou < 0.45:
                continue
            if not source_is_container and iou < 0.35:
                continue
            # Paintless layout containers are often synthesized slots. Only map
            # them to a source element when geometry is clearly the same.
            if not _has_visual_paint(child) and iou < 0.35:
                continue

        # Icons are frequently synthesized by the planner when the rendered
        # extractor cannot see Material Symbols as individual visual nodes. Do
        # not attach them to arbitrary nearby labels/surfaces; either match a
        # real icon-like source or mark them as planned_unmatched so the source
        # map is honest instead of misleading.
        if ckind == "icon" or "icon" in crole or crole == "media":
            icon_like_source = (
                skind in {"icon", "svg", "media"}
                or "material-symbol" in sclass
                or "icon" in srole
                or (ctext and stext and (ctext == stext or ctext in stext or stext in ctext))
            )
            if not icon_like_source:
                continue
        score = iou * 1000.0 - dist
        if ctext and stext:
            if ctext == stext:
                score += 450.0
            elif ctext in stext or stext in ctext:
                score += 180.0
            else:
                score -= 120.0
        if ckind and skind and ckind == skind:
            score += 120.0
        if crole and srole and crole == srole:
            score += 80.0
        # Strongly prefer plausible spatial matches. A text label from another
        # part of the UI can share role/kind, so distance still matters.
        if iou <= 0.02 and dist > 80 and not (ctext and ctext == stext):
            score -= 500.0
        candidates.append((score, -index, source))
    if not candidates:
        return None
    best = sorted(candidates, key=lambda x: (x[0], x[1]), reverse=True)[0]
    return best[2] if best[0] > -300 else None


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _is_direct_rendered_source(item: dict[str, Any]) -> bool:
    snap = item.get("source_snapshot") if isinstance(item.get("source_snapshot"), dict) else {}
    return str(snap.get("origin") or "") == "rendered_playwright"


def _source_expected(item: dict[str, Any]) -> dict[str, Any]:
    snap = item.get("source_snapshot") if isinstance(item.get("source_snapshot"), dict) else {}
    return snap.get("expected") if isinstance(snap.get("expected"), dict) else {}


def _copy_present(target: dict[str, Any], expected: dict[str, Any], keys: tuple[str, ...], *, overwrite: bool = True) -> None:
    for key in keys:
        value = expected.get(key)
        if value is None or value == "":
            continue
        if overwrite or target.get(key) is None or target.get(key) == "":
            target[key] = value


def _source_fidelity_kind(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "").lower()
    role = str(item.get("role") or "").lower()
    component_type = str(item.get("component_type") or "").lower()
    dt = item.get("deterministic_transform") if isinstance(item.get("deterministic_transform"), dict) else {}
    rule_id = str(dt.get("rule_id") or "").lower()
    snap = item.get("source_snapshot") if isinstance(item.get("source_snapshot"), dict) else {}
    skind = str(snap.get("kind") or "").lower()
    srole = str(snap.get("role") or "").lower()
    # Icon containers are visual surfaces, not icon glyphs. They must preserve
    # source.fill/radius (e.g. blue logo block) instead of projecting color as fill.
    if component_type == "icon_container" or rule_id == "icon.container_surface":
        return "surface"
    if kind in {"text", "heading"} or role in {"label", "placeholder", "heading", "body_text", "link", "content"}:
        if skind in {"button", "input", "textarea", "select", "control"} or srole in {"button_primary", "button_secondary", "input", "field", "control"}:
            return "text_slot"
        return "text"
    if kind == "icon" or "icon" in role or role == "media":
        return "icon"
    if kind in {"input", "textarea", "select"} or role in {"field", "input", "textbox"}:
        return "field"
    if kind == "button" or role in {"action", "button", "cta"}:
        return "action"
    if kind in {"control", "toggle", "checkbox", "radio"} or role in {"control", "checkbox", "radio", "switch"}:
        return "control"
    if _is_visual_container_kind_or_role(kind, role):
        return "surface"
    return "generic"


def _apply_source_fidelity(children: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prefer rendered/computed source values over LLM/token-normalized values.

    v06.4 exposed that many remaining visual drifts were introduced before
    Penpot: strokes/radii/footer height/action colors had been normalized by
    the LLM or tokens even though the Playwright source carried exact computed
    values. v06.13 applies a deterministic precedence rule:

        rendered source style > LLM planned value > token fallback > default

    Composite slots keep their own geometry/text while inheriting only the
    relevant text/icon style from the source parent.
    """
    out: list[dict[str, Any]] = []
    mode_counts: dict[str, int] = {}
    copied_counts: dict[str, int] = {}
    planned_count = 0
    for raw in children:
        item = dict(raw)
        if not _is_direct_rendered_source(item):
            planned_count += 1
            item.setdefault("source_fidelity", {
                "schema": "dvcp.source_fidelity.v1",
                "mode": "planned_source",
                "applied": False,
                "reason": "no_direct_rendered_source",
            })
            out.append(item)
            continue
        expected = _source_expected(item)
        if not expected:
            out.append(item)
            continue
        fidelity_kind = _source_fidelity_kind(item)
        before = dict(item)
        if fidelity_kind in {"surface", "field", "action", "control"}:
            # These are visual containers: exact source geometry/paint should be
            # authoritative. This preserves original borders/radii/sizes.
            _copy_present(item, expected, ("bbox", "fill", "stroke", "stroke_width", "radius", "opacity", "fill_opacity", "box_shadow"))
            if fidelity_kind == "action" and item.get("text") is not None:
                _copy_present(item, expected, ("color", "text_color", "font_size", "font_weight", "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align"))
                if expected.get("text") is not None and not item.get("text"):
                    item["text"] = expected.get("text")
        elif fidelity_kind == "text":
            _copy_present(item, expected, ("bbox", "text", "color", "text_color", "font_size", "font_weight", "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align", "opacity"))
        elif fidelity_kind == "text_slot":
            # Text slots inside inputs/buttons keep planned bbox/text (e.g.
            # 'Acceder' rather than 'Acceder arrow_forward') but should inherit
            # exact computed typography/color from the source parent.
            _copy_present(item, expected, ("color", "text_color", "font_size", "font_weight", "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align", "opacity"))
            source_text = _norm_text(expected.get("text"))
            item_text = _norm_text(item.get("text"))
            if source_text and item_text and item_text not in source_text:
                # Keep planned text when it is not a reliable substring.
                pass
        elif fidelity_kind == "icon":
            # v06.13: distinguish true Material Symbol glyphs from inferred icon
            # slots. True glyphs must preserve the source font family and
            # ligature text; inferred slots should not inherit parent typography.
            is_material_symbol = bool(
                item.get("is_material_symbol")
                or _font_family_is_material_symbol(item.get("font_family"))
                or _font_family_is_material_symbol(item.get("source_font_family"))
                or _font_family_is_material_symbol(expected.get("font_family"))
                or _font_family_is_material_symbol(expected.get("source_font_family"))
            )
            icon_color = (
                item.get("color")
                or item.get("text_color")
                or expected.get("color")
                or expected.get("text_color")
                or item.get("fill")
            )
            if icon_color:
                item["fill"] = icon_color
                item["color"] = icon_color
            _copy_present(item, expected, ("opacity",), overwrite=True)
            if is_material_symbol:
                _copy_present(item, expected, ("font_size", "font_weight", "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align"), overwrite=True)
                item["is_material_symbol"] = True
                item["material_symbol_name"] = item.get("material_symbol_name") or item.get("text") or expected.get("text")
                item.setdefault("penpot_line_height_ratio", 1)
                inherited_to_drop = ("stroke", "stroke_width", "radius", "box_shadow", "input_type")
            else:
                inherited_to_drop = (
                    "stroke", "stroke_width", "radius", "box_shadow", "input_type",
                    "font_size", "font_weight", "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align",
                )
            for inherited_key in inherited_to_drop:
                item.pop(inherited_key, None)
        else:
            _copy_present(item, expected, ("bbox", "fill", "stroke", "stroke_width", "color", "text_color", "radius", "opacity"))

        changed = [key for key in sorted(set(item) | set(before)) if item.get(key) != before.get(key) and key not in {"source_fidelity"}]
        mode_counts[fidelity_kind] = mode_counts.get(fidelity_kind, 0) + 1
        for key in changed:
            copied_counts[key] = copied_counts.get(key, 0) + 1
        item["source_fidelity"] = {
            "schema": "dvcp.source_fidelity.v1",
            "mode": "computed_source_authority",
            "version": "v06.13",
            "applied": bool(changed),
            "fidelity_kind": fidelity_kind,
            "copied_fields": changed,
            "precedence": "rendered_source_style > llm_spec_value > token_fallback > default",
        }
        out.append(item)
    return out, {
        "schema": "dvcp.source_fidelity_summary.v1",
        "version": "v06.13",
        "strategy": "computed_source_style_authority_icon_source_precedence_text_no_wrap",
        "target_count": len(children),
        "planned_source_count": planned_count,
        "mode_counts": mode_counts,
        "copied_field_counts": copied_counts,
    }


def _attach_source_traces(children: list[dict[str, Any]], sources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    traced_sources = _ensure_source_traces(sources)
    out: list[dict[str, Any]] = []
    matched = 0
    unmatched = 0
    for index, child in enumerate(children):
        item = dict(child)
        if item.get("source_snapshot") and item.get("source_ref"):
            out.append(item)
            matched += 1
            continue
        source = _match_source_child(item, traced_sources)
        if source is not None:
            source_ref = str(source.get("source_ref") or f"rendered_{index:03d}")
            item["source_ref"] = source_ref
            item["source_snapshot"] = _make_source_snapshot(source, source_ref)
            matched += 1
        else:
            # Still create a trace so every Penpot layer can be audited. The
            # origin makes clear that this was generated by the planning layer,
            # not a direct rendered DOM match.
            source_ref = f"planned_{index:03d}"
            item["source_ref"] = source_ref
            item["source_snapshot"] = _make_source_snapshot(item, source_ref, origin="llm_planned_unmatched")
            unmatched += 1
        out.append(item)
    return out, {
        "schema": "dvcp.source_trace_summary.v1",
        "source_count": len(traced_sources),
        "target_count": len(out),
        "matched_count": matched,
        "unmatched_count": unmatched,
        "strategy": "bbox_text_kind_role_nearest_source_match_v06_13_text_no_wrap_fidelity",
    }


def _looks_decorative(child: dict[str, Any], width: float, height: float) -> bool:
    kind = str(child.get("kind") or "").lower()
    role = str(child.get("role") or "").lower()
    bbox = _bbox(child.get("bbox"))
    if not bbox:
        return True
    cls = str(child.get("css_class") or "").lower()
    name = str(child.get("name") or "").lower()
    text = str(child.get("text") or "").strip().lower()
    if kind in {"surface", "container", "card"} or role == "surface":
        huge = bbox["width"] >= width * 0.75 and bbox["height"] >= height * 0.25
        outside = bbox["x"] < -20 or bbox["y"] < -20 or bbox["x"] + bbox["width"] > width + 40 or bbox["y"] + bbox["height"] > height + 40
        decorative_class = any(t in cls for t in ("blur", "pointer-events-none", "opacity-20", "absolute", "rounded-full"))
        decorative_name = name.startswith("absolute") or "blur" in name
        if huge and outside and (decorative_class or decorative_name):
            return True
    if kind == "text" and text in {"lock", "person", "arrow_forward", "shield_person", "visibility"}:
        return True
    return False


def _clean_child(child: dict[str, Any], width: float, height: float, index: int) -> dict[str, Any] | None:
    bbox = _bbox(child.get("bbox"))
    if bbox is None:
        return None
    kind = str(child.get("kind") or child.get("type") or child.get("role") or "surface").lower()
    role = str(child.get("role") or kind).lower()
    if kind not in ALLOWED_KINDS:
        kind = "surface"
    if kind == "heading":
        kind = "text"
        role = role if role != "heading" else "heading"
    if bbox["width"] > width * 1.2 or bbox["height"] > height * 1.2:
        return None

    base_for_decoration = dict(child)
    base_for_decoration["kind"] = kind
    base_for_decoration["role"] = role
    base_for_decoration["bbox"] = bbox
    if _looks_decorative(base_for_decoration, width, height):
        return None

    out: dict[str, Any] = {
        "name": _safe_name(child.get("name"), f"Layer{index}"),
        "kind": kind,
        "role": role,
        "bbox": bbox,
    }
    for key in VISUAL_KEYS:
        val = child.get(key)
        if val is not None and val != "":
            out[key] = str(val)[:500] if key == "text" else val
    return out


def sanitize_external_design_spec_for_import(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove obvious decorative DOM surfaces and normalize children before queueing."""
    if not isinstance(spec, dict):
        return spec, {"removed_count": 0, "input_count": 0, "output_count": 0}
    width = _num(spec.get("width"), 390)
    height = _num(spec.get("height"), 860)
    raw_children = [c for c in (spec.get("children") or []) if isinstance(c, dict)]
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, int, int, str]] = set()
    for i, child in enumerate(raw_children):
        clean = _clean_child(child, width, height, i)
        if not clean:
            continue
        b = clean["bbox"]
        sig = (
            str(clean.get("kind")),
            int(round(b["x"])), int(round(b["y"])), int(round(b["width"])), int(round(b["height"])),
            str(clean.get("text") or clean.get("name") or "")[:80],
        )
        if sig in seen:
            continue
        seen.add(sig)
        cleaned.append(clean)
    cleaned = _ensure_source_traces(cleaned)
    out = dict(spec)
    out["children"] = cleaned
    meta = dict(out.get("metadata") or {})
    meta["sanitized_for_import"] = True
    meta["sanitized_removed_count"] = len(raw_children) - len(cleaned)
    meta.setdefault("source_trace", {
        "schema": "dvcp.source_trace_summary.v1",
        "source_count": len(cleaned),
        "target_count": len(cleaned),
        "matched_count": len(cleaned),
        "unmatched_count": 0,
        "strategy": "direct_rendered_source_trace",
    })
    out["metadata"] = meta
    return out, {"input_count": len(raw_children), "output_count": len(cleaned), "removed_count": len(raw_children) - len(cleaned)}



# -----------------------------------------------------------------------------
# v06.13 deterministic Stitch -> Penpot transform
# -----------------------------------------------------------------------------
# Formal intent:
#   S = set of rendered Stitch source elements
#   P = set of Penpot import layers/ops
#   T : S -> Pow(P)
#
# This transform removes the LLM from structural planning. The LLM may still be
# used elsewhere when explicitly enabled, but the default import path can now use
# a deterministic mapping from rendered source nodes to Penpot layer slots.

ICON_TOKENS = {
    "lock", "lock_open", "person", "shield_person", "verified_user", "arrow_forward", "arrow_back",
    "home", "search", "settings", "menu", "close", "delete", "edit", "visibility", "visibility_off",
    "check", "add", "remove", "mail", "email", "key", "security", "account_circle", "dashboard",
    "notifications", "logout",
}


def _tw_spacing_value(css_class: Any, prefix: str, default: float) -> float:
    """Read common Tailwind spacing classes deterministically.

    Supports pl-10/pr-16/px-4 style values. We intentionally keep the parser
    small and generic; unknown classes fall back to the supplied default.
    """
    cls = str(css_class or "")
    matches = re.findall(rf"(?:^|\s){re.escape(prefix)}-([0-9]+(?:\.[0-9]+)?)(?:\s|$)", cls)
    if not matches:
        # px-* can be used as a fallback for side-specific padding.
        if prefix in {"pl", "pr"}:
            matches = re.findall(r"(?:^|\s)px-([0-9]+(?:\.[0-9]+)?)(?:\s|$)", cls)
        elif prefix in {"pt", "pb"}:
            matches = re.findall(r"(?:^|\s)py-([0-9]+(?:\.[0-9]+)?)(?:\s|$)", cls)
    if not matches:
        return float(default)
    try:
        # Tailwind spacing scale: n * 0.25rem. With a 16px root, n*4 px.
        return float(matches[-1]) * 4.0
    except Exception:
        return float(default)


def _det_source_ref(source: dict[str, Any], index: int) -> str:
    return str(source.get("source_ref") or f"rendered_{index:03d}")


def _det_source_snapshot(source: dict[str, Any], index: int) -> dict[str, Any]:
    source_ref = _det_source_ref(source, index)
    return _make_source_snapshot(source, source_ref, origin="rendered_playwright")


def _target_from_source(
    source: dict[str, Any],
    index: int,
    *,
    name_suffix: str,
    slot: str,
    kind: str | None = None,
    role: str | None = None,
    bbox: dict[str, Any] | None = None,
    text: str | None = None,
    extra: dict[str, Any] | None = None,
    rule_id: str = "identity",
) -> dict[str, Any]:
    source_ref = _det_source_ref(source, index)
    snap = _det_source_snapshot(source, index)
    expected = snap.get("expected") if isinstance(snap.get("expected"), dict) else {}
    target: dict[str, Any] = {
        "name": _safe_name(f"{source.get('name') or 'Source'}{name_suffix}", f"Target{index}{name_suffix}"),
        "kind": kind or str(source.get("kind") or "surface"),
        "role": role or str(source.get("role") or kind or source.get("kind") or "surface"),
        "bbox": bbox or expected.get("bbox") or source.get("bbox"),
        "source_ref": source_ref,
        "source_snapshot": snap,
        "source_name": source.get("name"),
        "slot": slot,
        "deterministic_transform": {
            "schema": "dvcp.deterministic_transform.v1",
            "version": "v06.13",
            "relation": "R ⊆ StitchRenderedElement × PenpotLayer",
            "function": "T : StitchRenderedElement -> Pow(PenpotLayer)",
            "source_domain": "StitchRenderedElement",
            "target_domain": "PenpotLayer",
            "source_ref": source_ref,
            "rule_id": rule_id,
            "slot": slot,
            "target_name": _safe_name(f"{source.get('name') or 'Source'}{name_suffix}", f"Target{index}{name_suffix}"),
        },
    }
    # Copy a conservative set of exact computed values. _apply_source_fidelity
    # will enforce final precedence again after the transform.
    for key in (
        "fill", "stroke", "stroke_width", "color", "text_color", "font_size", "font_weight",
        "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align", "radius", "opacity", "fill_opacity",
        "box_shadow", "input_type", "tag", "css_class", "dom_path", "id_attr",
    ):
        if source.get(key) is not None and source.get(key) != "":
            target[key] = source.get(key)
    if text is not None:
        target["text"] = text
    elif source.get("text") is not None and target["kind"] in {"text", "button"}:
        target["text"] = str(source.get("text") or "")[:500]
    if extra:
        target.update(extra)
    return target


def _text_parts_without_icon_tokens(text: Any) -> tuple[str, list[str]]:
    words = str(text or "").strip().split()
    icons: list[str] = []
    human: list[str] = []
    for word in words:
        token = word.strip().lower()
        if token in ICON_TOKENS:
            icons.append(token)
        else:
            human.append(word)
    return " ".join(human).strip(), icons


def _input_icon_for_source(source: dict[str, Any]) -> str | None:
    evidence = " ".join(str(source.get(k) or "") for k in ("name", "text", "id_attr", "input_type", "css_class", "dom_path")).lower()
    if any(t in evidence for t in ("password", "contraseña", "pass", "key")):
        return "lock"
    if any(t in evidence for t in ("email", "mail", "correo")):
        return "person"  # Keep visual compatibility with existing Stitch sample.
    if any(t in evidence for t in ("search", "buscar")):
        return "search"
    if any(t in evidence for t in ("user", "usuario", "name", "nombre", "account")):
        return "person"
    return None


def _input_text_bbox(source: dict[str, Any]) -> dict[str, float] | None:
    b = _bbox(source.get("bbox"))
    if not b:
        return None
    css = source.get("css_class")
    pad_l = _tw_spacing_value(css, "pl", 20)
    pad_r = _tw_spacing_value(css, "pr", 16)
    line = _num(source.get("line_height"), 20)
    # Leave room for an inferred leading icon if the CSS does not expose padding
    # but the source strongly suggests an icon slot.
    if _input_icon_for_source(source) and pad_l < 28:
        pad_l = 40
    return {
        "x": b["x"] + pad_l,
        "y": b["y"] + max((b["height"] - line) / 2, 0),
        "width": max(b["width"] - pad_l - pad_r, 8),
        "height": max(line, 8),
    }


def _button_text_bbox(source: dict[str, Any], text: str, has_icon: bool) -> dict[str, float] | None:
    b = _bbox(source.get("bbox"))
    if not b:
        return None
    font_size = _num(source.get("font_size"), 14)
    line = _num(source.get("line_height"), max(font_size + 4, 16))
    # Estimate label width deterministically; Penpot text will still render
    # the exact text but this keeps the slot centered in the source button.
    estimated_w = max(min(len(text) * font_size * 0.62, b["width"] - 24), 8)
    gap_shift = 10 if has_icon else 0
    return {
        "x": b["x"] + (b["width"] - estimated_w) / 2 - gap_shift,
        "y": b["y"] + max((b["height"] - line) / 2, 0),
        "width": estimated_w,
        "height": max(line, 8),
    }


def _icon_bbox_inside(source: dict[str, Any], *, side: str = "leading", size: float = 20) -> dict[str, float] | None:
    b = _bbox(source.get("bbox"))
    if not b:
        return None
    if side == "trailing":
        x = b["x"] + b["width"] - size - 18
    elif side == "center":
        x = b["x"] + (b["width"] - size) / 2
    else:
        x = b["x"] + 10
    return {
        "x": x,
        "y": b["y"] + max((b["height"] - size) / 2, 0),
        "width": size,
        "height": size,
    }

def _font_family_is_material_symbol(value: Any) -> bool:
    raw = str(value or "").lower()
    return "material symbols" in raw or "material icons" in raw


def _is_material_symbol_glyph_source(source: dict[str, Any]) -> bool:
    """True for the actual icon-font glyph node, not its layout wrapper."""
    kind = str(source.get("kind") or "").lower()
    role = str(source.get("role") or "").lower()
    tag = str(source.get("tag") or "").lower()
    text = str(source.get("text") or "").strip().lower()
    family = source.get("font_family") or source.get("source_font_family")
    css = str(source.get("css_class") or "").lower()
    return (
        (kind == "icon" or role == "icon" or "material-symbol" in css)
        and tag in {"span", "i", "em"}
        and bool(text)
        and (_font_family_is_material_symbol(family) or "material-symbol" in css)
    )


def _source_has_visible_paint(source: dict[str, Any]) -> bool:
    fill = str(source.get("fill") or "").strip().lower()
    stroke = str(source.get("stroke") or "").strip().lower()
    sw = _num(source.get("stroke_width"), 0)
    return (
        fill not in {"", "none", "transparent", "rgba(0, 0, 0, 0)", "rgba(0,0,0,0)"}
        or (stroke not in {"", "none", "transparent", "rgba(0, 0, 0, 0)", "rgba(0,0,0,0)"} and sw > 0)
    )


def _bbox_contains_center(container: dict[str, Any] | None, child: dict[str, Any] | None, *, pad: float = 2.0) -> bool:
    c = _bbox(container)
    b = _bbox(child)
    if not c or not b:
        return False
    cx = b["x"] + b["width"] / 2
    cy = b["y"] + b["height"] / 2
    return (
        cx >= c["x"] - pad and cx <= c["x"] + c["width"] + pad
        and cy >= c["y"] - pad and cy <= c["y"] + c["height"] + pad
    )


def _source_has_material_icon_inside(source: dict[str, Any], material_icon_sources: list[dict[str, Any]] | None, *, token: str | None = None) -> bool:
    """Detect rendered Material Symbol children so we do not infer duplicates."""
    if not material_icon_sources:
        return False
    src_ref = str(source.get("source_ref") or "")
    b = _bbox(source.get("bbox"))
    if not b:
        return False
    token_norm = str(token or "").strip().lower()
    for icon in material_icon_sources:
        if str(icon.get("source_ref") or "") == src_ref:
            continue
        if token_norm and str(icon.get("text") or "").strip().lower() != token_norm:
            continue
        if _bbox_contains_center(b, icon.get("bbox"), pad=4):
            return True
    return False


def _transform_one_source_to_penpot(source: dict[str, Any], index: int, material_icon_sources: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    kind = str(source.get("kind") or "").lower()
    role = str(source.get("role") or "").lower()
    tag = str(source.get("tag") or "").lower()
    text = str(source.get("text") or "").strip()
    rendered_icons_enabled = _rendered_import_icons_enabled()
    out: list[dict[str, Any]] = []

    # v06.13: Material Symbols are text ligatures, but semantically they are
    # icon glyphs. Handle them before generic span/text mapping so Penpot gets
    # a create_icon op with the Material Symbols font instead of normal body text.
    if _is_material_symbol_glyph_source(source):
        icon_target = _target_from_source(
            source,
            index,
            name_suffix="Text",
            slot="source_element",
            kind="icon",
            role="icon",
            text=text,
            extra={
                "component_type": "icon_glyph",
                "is_material_symbol": True,
                "material_symbol_name": text,
            },
            rule_id="icon.material_symbol_glyph",
        )
        out.append(icon_target)
        return out

    # Icon wrappers are not glyphs. If they have visible paint they become
    # surfaces, otherwise they are layout-only and intentionally produce no
    # Penpot layer. This avoids duplicated/overlaid symbols when
    # STITCH_RENDERED_IMPORT_ICONS=1 exposes the real glyph span separately.
    if kind == "icon" or role == "icon":
        if _source_has_visible_paint(source):
            surf = _target_from_source(
                source, index, name_suffix="Surface", slot="source_element",
                kind="surface", role="media", rule_id="icon.container_surface",
                extra={"component_type": "icon_container"},
            )
            surf.pop("text", None)
            out.append(surf)
        return out

    # Text nodes are direct 1:1 mappings.
    if kind in {"text", "heading"} or tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "label", "a", "li", "small"}:
        out.append(_target_from_source(source, index, name_suffix="Text", slot="source_element", kind="text", role=role or "content_block", text=text, rule_id="text.identity"))
        return out

    # Inputs decompose into a field surface plus deterministic internal slots.
    if kind in {"input", "textarea", "select"} or role in {"input", "field", "textbox"}:
        field = _target_from_source(source, index, name_suffix="Field", slot="container", kind="input", role="field", rule_id="field.container")
        field["component_type"] = "field"
        field["component_id"] = _safe_name(field.get("name"), f"Field{index}")
        out.append(field)

        icon = _input_icon_for_source(source)
        # If the rendered DOM already contains a Material Symbols glyph inside
        # this field, do not generate an inferred duplicate.
        if icon and not rendered_icons_enabled and not _source_has_material_icon_inside(source, material_icon_sources, token=icon):
            icon_target = _target_from_source(
                source,
                index,
                name_suffix="LeadingIcon",
                slot="leading_icon",
                kind="icon",
                role="media",
                bbox=_icon_bbox_inside(source, side="leading", size=20),
                text=icon,
                extra={"fill": source.get("color") or source.get("text_color") or "#43474F", "component_id": field["component_id"], "attach_to": field["component_id"], "component_type": "media"},
                rule_id="field.leading_icon.inferred",
            )
            out.append(icon_target)

        if text:
            text_target = _target_from_source(
                source,
                index,
                name_suffix="Text",
                slot="content",
                kind="text",
                role="content",
                bbox=_input_text_bbox(source),
                text=text,
                extra={"component_id": field["component_id"], "attach_to": field["component_id"], "component_type": "content_block"},
                rule_id="field.text_slot",
            )
            out.append(text_target)
        return out

    # Buttons/actions decompose only when they have a visible container. Small
    # text-only actions remain direct text/action layers.
    if kind == "button" or role in {"button", "button_primary", "button_secondary", "action", "cta"}:
        b = _bbox(source.get("bbox"))
        fill = str(source.get("fill") or "").strip().lower()
        has_container = bool(fill and fill not in {"transparent", "none", "rgba(0, 0, 0, 0)", "rgba(0,0,0,0)"}) or (b and b["height"] > 28)
        human_text, icon_tokens = _text_parts_without_icon_tokens(text)
        has_rendered_icon = any(
            _source_has_material_icon_inside(source, material_icon_sources, token=tok)
            for tok in icon_tokens
        )
        if not has_container:
            out.append(_target_from_source(source, index, name_suffix="ActionText", slot="source_element", kind="text", role="action", text=text, rule_id="action.text_only"))
            return out

        action = _target_from_source(source, index, name_suffix="Button", slot="container", kind="button", role="action", rule_id="action.container")
        # Container represents only the visual button surface; label/icon are deterministic child slots.
        action.pop("text", None)
        action["component_type"] = "action"
        action["component_id"] = _safe_name(action.get("name"), f"Action{index}")
        out.append(action)
        label = human_text or text
        if label:
            text_target = _target_from_source(
                source,
                index,
                name_suffix="Label",
                slot="label",
                kind="text",
                role="content",
                bbox=_button_text_bbox(source, label, bool(icon_tokens)),
                text=label,
                extra={"component_id": action["component_id"], "attach_to": action["component_id"], "component_type": "content_block"},
                rule_id="action.label_slot",
            )
            out.append(text_target)
        for pos, icon in enumerate([] if (rendered_icons_enabled or has_rendered_icon) else icon_tokens[:1]):
            icon_target = _target_from_source(
                source,
                index,
                name_suffix="TrailingIcon",
                slot="trailing_icon",
                kind="icon",
                role="media",
                bbox=_icon_bbox_inside(source, side="trailing", size=16),
                text=icon,
                extra={"fill": source.get("color") or source.get("text_color") or "#FFFFFF", "component_id": action["component_id"], "attach_to": action["component_id"], "component_type": "media"},
                rule_id="action.trailing_icon.from_text_token",
            )
            out.append(icon_target)
        return out

    # Controls are direct visual primitives. Labels are separate rendered source
    # text nodes and will map independently.
    if kind in {"control", "checkbox", "radio", "toggle"} or role in {"control", "checkbox", "radio", "switch"}:
        control = _target_from_source(source, index, name_suffix="Control", slot="source_element", kind="control", role="control", rule_id="control.identity")
        control["component_type"] = "control"
        control["component_id"] = _safe_name(control.get("name"), f"Control{index}")
        out.append(control)
        return out

    # Generic surfaces/nav/footer/header/media are direct container mappings.
    role_out = role or ("navigation" if tag in {"header", "nav"} else "surface")
    kind_out = "surface" if kind in {"surface", "container", "card", "section", "navigation", "form", "media"} else (kind or "surface")
    target = _target_from_source(source, index, name_suffix="Surface", slot="source_element", kind=kind_out, role=role_out, rule_id="surface.identity")
    if tag in {"header", "footer"}:
        target["role"] = tag
    out.append(target)
    return out


def _dedupe_transformed_targets(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, int, int, int, str]] = set()
    for child in children:
        b = _bbox(child.get("bbox")) or {"x": 0, "y": 0, "width": 0, "height": 0}
        sig = (
            str(child.get("source_ref") or ""),
            str(child.get("slot") or ""),
            int(round(b["x"])), int(round(b["y"])), int(round(b["width"])), int(round(b["height"])),
            str(child.get("text") or child.get("name") or "")[:60],
        )
        if sig in seen:
            continue
        seen.add(sig)
        out.append(child)
    return out


def build_external_design_spec_from_deterministic_transform(fallback_spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build ExternalDesignSpec using only deterministic rendered-source rules.

    This is the implementation of the relation/function discussed with the user:

        R ⊆ StitchRenderedElement × PenpotLayer
        T : StitchRenderedElement -> Pow(PenpotLayer)

    No LLM output is used to choose structure. Each rendered source element is
    transformed with a stable rule into one or more Penpot layers, and every
    target carries source_trace/source_snapshot evidence.
    """
    fallback_sanitized, sanitize_summary = sanitize_external_design_spec_for_import(fallback_spec)
    sources = _ensure_source_traces([c for c in (fallback_sanitized.get("children") or []) if isinstance(c, dict)])
    material_icon_sources = [src for src in sources if _is_material_symbol_glyph_source(src)]
    rendered_icons_enabled = _rendered_import_icons_enabled()
    transformed: list[dict[str, Any]] = []
    rule_counts: dict[str, int] = {}
    relation_pairs: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        targets = _transform_one_source_to_penpot(source, index, material_icon_sources)
        for target in targets:
            dt = target.get("deterministic_transform") if isinstance(target.get("deterministic_transform"), dict) else {}
            rule_id = str(dt.get("rule_id") or "unknown")
            rule_counts[rule_id] = rule_counts.get(rule_id, 0) + 1
            relation_pairs.append({
                "source_ref": target.get("source_ref"),
                "target_name": target.get("name"),
                "slot": target.get("slot"),
                "rule_id": rule_id,
            })
        transformed.extend(targets)
    transformed = _dedupe_transformed_targets(transformed)
    transformed, source_fidelity_summary = _apply_source_fidelity(transformed)

    transform_summary = {
        "schema": "dvcp.deterministic_transform_summary.v1",
        "version": "v06.13",
        "relation": "R ⊆ StitchRenderedElement × PenpotLayer",
        "function": "T : StitchRenderedElement -> Pow(PenpotLayer)",
        "source_domain": "StitchRenderedElement",
        "target_domain": "PenpotLayer",
        "source_count": len(sources),
        "target_count": len(transformed),
        "rule_counts": rule_counts,
        "material_symbol_source_count": len(material_icon_sources),
        "icon_inference": {
            "enabled": not rendered_icons_enabled,
            "mode": "fallback_when_STITCH_RENDERED_IMPORT_ICONS_is_0" if not rendered_icons_enabled else "disabled_by_STITCH_RENDERED_IMPORT_ICONS",
            "rendered_import_icons": rendered_icons_enabled,
        },
        "relation_pair_count": len(relation_pairs),
        "relation_pairs_preview": relation_pairs[:40],
    }
    source_trace_summary = {
        "schema": "dvcp.source_trace_summary.v1",
        "source_count": len(sources),
        "target_count": len(transformed),
        "matched_count": len(transformed),
        "unmatched_count": 0,
        "strategy": "deterministic_transform_T_v06_13_source_to_penpot",
        "deterministic_transform": transform_summary,
        "source_fidelity": source_fidelity_summary,
    }
    spec = dict(fallback_sanitized)
    spec.update({
        "schema": "dvcp.external_design_spec.v1",
        "source": "stitch_deterministic_transform",
        "import_mode": "existing_screen_html_deterministic_transform",
        "children": transformed,
    })
    meta = dict(spec.get("metadata") or {})
    meta.update({
        "llm_guided": False,
        "deterministic_transform": transform_summary,
        "source_trace": source_trace_summary,
        "source_fidelity": source_fidelity_summary,
        "layout_extraction_mode": meta.get("layout_extraction_mode") or "rendered_playwright",
        "sanitized_for_import": True,
    })
    spec["metadata"] = meta
    summary = {
        "used": False,
        "reason": "deterministic_transform_T_v06_13",
        "planner": "disabled_for_structure",
        "deterministic": True,
        "fallback_child_count": len(sources),
        "target_child_count": len(transformed),
        "fallback_sanitize": sanitize_summary,
        "source_trace": source_trace_summary,
        "source_fidelity": source_fidelity_summary,
        "deterministic_transform": transform_summary,
        "source": spec.get("source"),
    }
    return spec, summary

def build_external_design_spec_from_llm_plan(llm_plan: dict[str, Any], fallback_spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    width = _num(llm_plan.get("width") or (llm_plan.get("screen") or {}).get("width"), _num(fallback_spec.get("width"), 390))
    height = _num(llm_plan.get("height") or (llm_plan.get("screen") or {}).get("height"), _num(fallback_spec.get("height"), 860))

    raw_children = _iter_plan_children(llm_plan)
    children: list[dict[str, Any]] = []
    for index, item in enumerate(raw_children):
        clean = _clean_child(item, width, height, index)
        if clean:
            children.append(clean)

    fallback_sanitized, sanitize_summary = sanitize_external_design_spec_for_import(fallback_spec)
    fallback_children = [c for c in (fallback_sanitized.get("children") or []) if isinstance(c, dict)]
    children, source_trace_summary = _attach_source_traces(children, fallback_children)
    children, source_fidelity_summary = _apply_source_fidelity(children)
    source_trace_summary["source_fidelity"] = source_fidelity_summary

    minimum = max(6, min(len(fallback_children), 10))
    if len(children) < minimum:
        return fallback_sanitized, {
            "used": False,
            "reason": "llm_plan_invalid_or_too_sparse",
            "llm_child_count": len(children),
            "fallback_child_count": len(fallback_children),
            "fallback_sanitize": sanitize_summary,
            "llm_error": llm_plan.get("error"),
        }

    spec = dict(fallback_sanitized)
    spec.update(
        {
            "schema": "dvcp.external_design_spec.v1",
            "source": llm_plan.get("source") or "stitch_llm_vision_guided",
            "import_mode": llm_plan.get("import_mode") or "existing_screen_html_llm_vision_guided",
            "screen_name": llm_plan.get("screen_name") or (llm_plan.get("screen") or {}).get("name") or fallback_spec.get("screen_name"),
            "screen_title": llm_plan.get("screen_title") or (llm_plan.get("screen") or {}).get("title") or fallback_spec.get("screen_title"),
            "screen_type": llm_plan.get("screen_type") or (llm_plan.get("screen") or {}).get("type") or fallback_spec.get("screen_type"),
            "width": width,
            "height": height,
            "tokens": llm_plan.get("tokens") if isinstance(llm_plan.get("tokens"), dict) else (fallback_spec.get("tokens") or {}),
            "children": children,
        }
    )
    meta = dict(spec.get("metadata") or {})
    meta.update(
        {
            "llm_guided": True,
            "llm_child_count": len(children),
            "fallback_child_count": len(fallback_children),
            "layout_extraction_mode": meta.get("layout_extraction_mode") or "rendered_playwright",
            "sanitized_for_import": True,
            "source_trace": source_trace_summary,
            "source_fidelity": source_fidelity_summary,
        }
    )
    spec["metadata"] = meta
    return spec, {
        "used": True,
        "reason": "llm_plan_validated",
        "llm_child_count": len(children),
        "fallback_child_count": len(fallback_children),
        "fallback_sanitize": sanitize_summary,
        "source_trace": source_trace_summary,
        "source_fidelity": source_fidelity_summary,
        "source": spec.get("source"),
    }
