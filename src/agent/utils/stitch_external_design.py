"""Rendered Stitch HTML -> DVCP ExternalDesignSpec.

This importer is intentionally generic. It does not hardcode login screens or
specific Stitch templates. It renders the downloaded Stitch HTML in Chromium,
measures visible DOM nodes with `getBoundingClientRect`, reads computed styles,
and converts those measurements into DVCP ExternalDesignSpec children.

Why this exists:
- The static HTML parser could only read text order, so Tailwind/flex/grid
  layouts were flattened into a vertical list.
- Material Symbols icon names such as `lock`, `person`, `arrow_forward` were
  interpreted as normal text.
- The queue importer worked, but it applied a poor visual spec.

Runtime behavior:
- Preferred path: Playwright/Chromium rendered extraction.
- Fallback path: conservative static parser if Playwright is unavailable.

Environment flags:
- STITCH_RENDERED_IMPORT=1         Enable rendered extractor. Default: 1
- STITCH_RENDERED_TIMEOUT_MS=25000 Playwright timeout. Default: 25000
- STITCH_RENDERED_IMPORT_ICONS=0   Import material icon glyph text. Default: 0
- STITCH_STATIC_FALLBACK=1         Allow fallback if rendering fails. Default: 1
- STITCH_LAYOUT_MAX_ELEMENTS=0     Optional safety cap after extraction. 0=all
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
from collections import Counter
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, Optional


# -----------------------------------------------------------------------------
# Generic semantic hints. These are category hints, not screen/template rules.
# -----------------------------------------------------------------------------

FIELD_HINTS = (
    "name", "nombre", "email", "correo", "password", "contraseña", "sku", "barcode", "código", "codigo",
    "price", "precio", "stock", "category", "categoría", "categoria", "supplier", "proveedor", "search",
    "buscar", "phone", "teléfono", "telefono", "address", "dirección", "direccion", "date", "fecha",
    "quantity", "cantidad", "description", "descripción", "descripcion", "usuario", "user", "account",
)
BUTTON_PRIMARY_HINTS = (
    "save", "guardar", "continue", "continuar", "submit", "enviar", "add", "agregar", "create", "crear",
    "update", "actualizar", "login", "entrar", "acceder", "checkout", "comprar", "confirm", "confirmar",
    "sign in", "iniciar", "start", "next", "siguiente",
)
BUTTON_HINTS = BUTTON_PRIMARY_HINTS + (
    "cancel", "cancelar", "delete", "eliminar", "edit", "editar", "back", "volver", "close", "cerrar",
    "mostrar", "show", "hide", "ocultar",
)
TABLE_HINTS = ("table", "tabla", "row", "column", "columna", "list", "lista")
CHART_HINTS = ("chart", "graph", "gráfica", "grafica", "kpi", "metric", "métrica", "metrica", "analytics")
MEDIA_HINTS = ("image", "imagen", "photo", "foto", "avatar", "illustration", "ilustración", "ilustracion", "upload", "subir")
NAV_HINTS = ("nav", "navigation", "sidebar", "menu", "tab", "breadcrumb")

MATERIAL_SYMBOL_TEXTS = {
    "lock", "lock_open", "person", "shield_person", "verified_user", "arrow_forward", "arrow_back", "home",
    "search", "settings", "menu", "close", "delete", "edit", "visibility", "visibility_off", "check", "add",
    "remove", "mail", "email", "key", "security", "account_circle", "dashboard", "notifications", "logout",
}

SKIP_TAGS = {"script", "style", "meta", "link", "noscript", "template"}
TEXT_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "label", "a", "li", "small", "strong", "em", "div"}
EMPTY_UI_TAGS = {"input", "select", "textarea", "img"}


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    try:
        value = int(str(raw).strip()) if raw is not None else default
    except Exception:
        value = default
    if minimum is not None:
        value = max(value, minimum)
    return value


def clean_text(value: str, limit: int = 300) -> str:
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def is_resource_ref(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if lowered.startswith(("http://", "https://", "data:", "blob:", "javascript:")):
        return True
    if re.fullmatch(r"projects/[^\s]+", text):
        return True
    if re.fullmatch(r"assets/[^\s]+", text):
        return True
    if re.fullmatch(r"[a-f0-9]{24,}", text):
        return True
    if text.startswith("{") or text.startswith("["):
        return True
    return False


def meaningful_text(value: str) -> bool:
    text = clean_text(value)
    if not text or is_resource_ref(text):
        return False
    if len(text) < 1 or len(text) > 300:
        return False
    if re.fullmatch(r"[#{};:,._/\-\d\s]+", text):
        return False
    return True


def is_material_icon_text(value: str) -> bool:
    text = clean_text(value, 80).lower()
    return text in MATERIAL_SYMBOL_TEXTS or bool(re.fullmatch(r"[a-z]+(?:_[a-z]+)+", text))


def rendered_node_uses_material_icon_font_or_class(node: dict[str, Any]) -> bool:
    """Return true only when the rendered node itself is an icon-font node.

    Some real labels are words that also happen to be Material Symbol ligature
    tokens, for example Home or Settings in a bottom navigation.  Those must
    remain text when their source font/class is normal.  The icon-token filter
    should only remove nodes that actually came from Material Symbols/Icons.
    """
    css = str(node.get("css_class") or "").lower()
    family = str(node.get("font_family") or node.get("source_font_family") or "").lower()
    kind = str(node.get("kind") or "").lower()
    role = str(node.get("role") or "").lower()
    return (
        bool(node.get("is_icon"))
        or kind == "icon"
        or role == "icon"
        or "material-symbol" in css
        or "material-icons" in css
        or "material symbols" in family
        or "material icons" in family
    )


def slug_words(value: str, fallback: str = "Item") -> str:
    words = re.findall(r"[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ]+", value or "")
    words = [w for w in words if w.lower() not in {"the", "and", "for", "con", "para", "de", "la", "el", "un", "una"}]
    if not words:
        return fallback
    return "".join(word[:1].upper() + word[1:] for word in words[:5])[:64]


def normalize_name(value: str, fallback: str = "Item") -> str:
    raw = slug_words(value, fallback=fallback) or fallback
    # Penpot names with some punctuation/emoji can get awkward in layers.
    raw = re.sub(r"[^A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_\-]", "", raw)
    return raw or fallback


def safe_number(value: Any, fallback: float = 0.0) -> float:
    try:
        if value is None:
            return fallback
        return float(str(value).replace("px", "").strip())
    except Exception:
        return fallback


def safe_int(value: Any, fallback: int) -> int:
    try:
        return int(round(float(str(value).replace("px", "").strip())))
    except Exception:
        return fallback


def normalize_screen_size(width: Any, height: Any, device_type: str = "") -> tuple[int, int]:
    w = safe_int(width, 390)
    h = safe_int(height, 900)
    device = (device_type or "").lower()
    # Stitch often stores mobile screens at 2x. Render at logical CSS pixels.
    if device == "mobile" and w > 480:
        w = max(320, int(round(w / 2)))
        h = max(560, int(round(h / 2)))
    if w <= 0:
        w = 390
    if h <= 0:
        h = 900
    return w, h


def rgba_alpha(color: str) -> float:
    color = (color or "").strip().lower()
    if color in {"transparent", "rgba(0, 0, 0, 0)", "rgba(0,0,0,0)", "none", ""}:
        return 0.0
    m = re.match(r"rgba\([^,]+,[^,]+,[^,]+,\s*([0-9.]+)\s*\)", color)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return 1.0
    return 1.0


def css_color_to_hex(value: str, fallback: str | None = None) -> str | None:
    value = (value or "").strip()
    if not value:
        return fallback
    if value.startswith("#"):
        return value
    m = re.match(r"rgba?\(([^)]+)\)", value)
    if not m:
        return fallback if value in {"transparent", "none"} else value
    parts = [p.strip() for p in m.group(1).split(",")]
    if len(parts) < 3:
        return fallback
    try:
        r = max(0, min(255, int(float(parts[0]))))
        g = max(0, min(255, int(float(parts[1]))))
        b = max(0, min(255, int(float(parts[2]))))
        if len(parts) >= 4 and float(parts[3]) <= 0.01:
            return fallback
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        return fallback


def infer_screen_type(elements: list[dict[str, Any]], title: str = "") -> str:
    text = " ".join([title] + [str(e.get("text", "")) for e in elements]).lower()
    roles = {str(e.get("role", "")) for e in elements}
    if any(k in text for k in ["inventario", "inventory", "sku", "barcode", "stock"]):
        return "inventory_form"
    if any(k in text for k in ["login", "password", "contraseña", "mfa", "auth", "empresa", "sesión", "session"]):
        return "auth_screen"
    if any(k in text for k in ["dashboard", "metric", "analytics", "chart", "kpi"]) or "data_viz" in roles:
        return "dashboard"
    if any(k in text for k in ["cart", "checkout", "price", "catalog", "product", "producto"]):
        return "ecommerce"
    if any(k in text for k in ["settings", "configuración", "profile", "perfil"]):
        return "settings_profile"
    if "table" in roles:
        return "table_list"
    if "input" in roles:
        return "form"
    return "generic_interface"


def infer_role_from_text(text: str, tag: str = "", attrs: Optional[dict[str, str]] = None) -> str:
    attrs = attrs or {}
    hay = " ".join([text or "", tag or "", " ".join(f"{k} {v}" for k, v in attrs.items())]).lower()
    tag = (tag or "").lower()
    input_type = attrs.get("type", "").lower()
    role_attr = attrs.get("role", "").lower()
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return "heading"
    if tag == "nav" or role_attr == "navigation" or any(k in hay for k in NAV_HINTS):
        return "navigation"
    if tag == "button" or role_attr == "button" or any(k in hay for k in BUTTON_HINTS):
        return "button_primary" if any(k in hay for k in BUTTON_PRIMARY_HINTS) else "button_secondary"
    if tag in {"input", "select", "textarea", "label"} or input_type or any(k in hay for k in FIELD_HINTS):
        if input_type in {"checkbox", "radio", "range"} or "toggle" in hay or "switch" in hay:
            return "control"
        return "input"
    if any(k in hay for k in TABLE_HINTS):
        return "table"
    if any(k in hay for k in CHART_HINTS):
        return "data_viz"
    if tag == "img" or any(k in hay for k in MEDIA_HINTS):
        return "media_upload" if any(k in hay for k in ("upload", "subir")) else "media"
    return "body_text"


# -----------------------------------------------------------------------------
# Static fallback parser
# -----------------------------------------------------------------------------

class _StaticHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict[str, Any]] = []
        self.elements: list[dict[str, Any]] = []
        self.title_text = ""
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attrs = {k: (v or "") for k, v in attrs_list}
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        item = {"tag": tag, "attrs": attrs, "text": ""}
        self.stack.append(item)
        if tag in EMPTY_UI_TAGS:
            text = attrs.get("placeholder") or attrs.get("aria-label") or attrs.get("alt") or attrs.get("title") or attrs.get("name") or attrs.get("id") or tag
            self._append_element(tag, attrs, text)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth = max(self._skip_depth - 1, 0)
            return
        if self._skip_depth or not self.stack:
            return
        item = self.stack.pop()
        text = clean_text(item.get("text", ""))
        item_tag = item.get("tag", "")
        if item_tag == "title" and text:
            self.title_text = text
        if item_tag in TEXT_TAGS and meaningful_text(text):
            self._append_element(item_tag, item.get("attrs", {}), text)
        if self.stack and text:
            self.stack[-1]["text"] = (self.stack[-1].get("text", "") + " " + text).strip()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = clean_text(data, limit=500)
        if text and self.stack:
            self.stack[-1]["text"] = (self.stack[-1].get("text", "") + " " + text).strip()

    def _append_element(self, tag: str, attrs: dict[str, str], text: str) -> None:
        text = clean_text(text)
        if not meaningful_text(text):
            return
        if is_material_icon_text(text) and not _env_bool("STITCH_RENDERED_IMPORT_ICONS", False):
            return
        role = infer_role_from_text(text, tag, attrs)
        self.elements.append({"tag": tag, "attrs": attrs, "text": text, "role": role})


def parse_html_static(html_text: str) -> tuple[list[dict[str, Any]], str]:
    parser = _StaticHTMLParser()
    try:
        parser.feed(html_text or "")
    except Exception:
        pass
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for el in parser.elements:
        text = clean_text(str(el.get("text", "")))
        if not meaningful_text(text):
            continue
        if is_material_icon_text(text) and not _env_bool("STITCH_RENDERED_IMPORT_ICONS", False):
            continue
        role = str(el.get("role") or infer_role_from_text(text, str(el.get("tag", "")), el.get("attrs") or {}))
        key = (role, text.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "tag": str(el.get("tag", "")), "attrs": el.get("attrs") or {}, "role": role})
    return out, parser.title_text


def kind_from_role(role: str) -> str:
    role = role or ""
    if role in {"heading", "body_text", "text"}:
        return "text"
    if role == "input":
        return "input"
    if role.startswith("button"):
        return "button"
    if role == "control":
        return "control"
    if role == "media_upload":
        return "upload"
    if role == "media":
        return "media"
    if role == "data_viz":
        return "chart"
    if role == "table":
        return "table"
    if role == "navigation":
        return "surface"
    return "text"


def layout_elements_static(elements: list[dict[str, Any]], *, width: int, height: int, title: str) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    margin = 24 if width <= 480 else 48
    y = 36
    usable_w = max(260, width - margin * 2)

    title_text = title if meaningful_text(title) else "Imported Stitch Screen"
    children.append(
        {
            "name": "ScreenTitle",
            "role": "heading",
            "kind": "text",
            "bbox": {"x": margin, "y": y, "width": usable_w, "height": 42},
            "text": title_text,
            "font_size": 28 if width <= 480 else 32,
            "color": "#0F172A",
        }
    )
    y += 66
    body = []
    for el in elements:
        text = clean_text(str(el.get("text", "")))
        if not meaningful_text(text) or text.lower() == title_text.lower():
            continue
        body.append({**el, "text": text})

    for index, el in enumerate(body):
        role = str(el.get("role") or "body_text")
        kind = kind_from_role(role)
        text = str(el.get("text") or "")
        name = normalize_name(text, fallback=f"Element{index + 1}")
        if kind == "text":
            is_heading = role == "heading"
            h = 34 if is_heading else 26
            children.append({"name": name, "role": role, "kind": "text", "bbox": {"x": margin, "y": y, "width": usable_w, "height": h}, "text": text, "font_size": 22 if is_heading else 14, "color": "#0F172A" if is_heading else "#475569"})
            y += h + (16 if is_heading else 10)
        elif kind == "input":
            children.append({"name": name + "Input", "role": role, "kind": "input", "bbox": {"x": margin, "y": y, "width": usable_w, "height": 48}, "text": text, "radius": 12})
            y += 64
        elif kind == "button":
            children.append({"name": name + "Button", "role": role, "kind": "button", "bbox": {"x": margin, "y": y, "width": usable_w, "height": 48}, "text": text, "radius": 12})
            y += 64
        else:
            children.append({"name": name, "role": role, "kind": "surface", "bbox": {"x": margin, "y": y, "width": usable_w, "height": 92}, "text": text, "radius": 18})
            y += 112

    return children


# -----------------------------------------------------------------------------
# Rendered extraction via Playwright, isolated in a worker thread.
# -----------------------------------------------------------------------------

_RENDER_JS = r"""
(options) => {
  const importIcons = !!options.importIcons;
  const viewportW = window.innerWidth || options.viewportWidth || 390;
  const viewportH = window.innerHeight || options.viewportHeight || 900;
  const iconTexts = new Set(options.iconTexts || []);
  const textTags = new Set(['h1','h2','h3','h4','h5','h6','p','span','label','a','li','small','strong','em']);
  const containerTags = new Set(['div','section','main','header','footer','nav','article','form','aside']);
  const inputTags = new Set(['input','textarea','select']);

  function clean(s) {
    return String(s || '').replace(/\s+/g, ' ').trim();
  }

  function domPath(el) {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === Node.ELEMENT_NODE && cur !== document.body && parts.length < 8) {
      const tag = cur.tagName.toLowerCase();
      let idx = 1;
      let sib = cur.previousElementSibling;
      while (sib) {
        if (sib.tagName && sib.tagName.toLowerCase() === tag) idx += 1;
        sib = sib.previousElementSibling;
      }
      const id = cur.id ? '#' + String(cur.id).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 48) : '';
      parts.unshift(tag + id + ':nth-of-type(' + idx + ')');
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  function num(v, fallback) {
    const n = Number(String(v || '').replace('px', ''));
    return Number.isFinite(n) ? n : (fallback || 0);
  }

  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return {
      x: Math.round((r.left + window.scrollX) * 100) / 100,
      y: Math.round((r.top + window.scrollY) * 100) / 100,
      width: Math.round(r.width * 100) / 100,
      height: Math.round(r.height * 100) / 100,
      right: Math.round((r.right + window.scrollX) * 100) / 100,
      bottom: Math.round((r.bottom + window.scrollY) * 100) / 100,
      area: Math.round(r.width * r.height * 100) / 100,
    };
  }

  function colorToHex(c, fallback) {
    c = String(c || '').trim();
    if (!c || c === 'transparent') return fallback || null;
    if (c.startsWith('#')) return c;
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return c;
    const parts = m[1].split(',').map(v => v.trim());
    if (parts.length < 3) return fallback || null;
    const a = parts.length >= 4 ? Number(parts[3]) : 1;
    if (Number.isFinite(a) && a <= 0.01) return fallback || null;
    const r = Math.max(0, Math.min(255, Math.round(Number(parts[0]))));
    const g = Math.max(0, Math.min(255, Math.round(Number(parts[1]))));
    const b = Math.max(0, Math.min(255, Math.round(Number(parts[2]))));
    if (![r,g,b].every(Number.isFinite)) return fallback || null;
    return '#' + [r,g,b].map(n => n.toString(16).padStart(2, '0')).join('').toUpperCase();
  }

  function alphaOf(c) {
    c = String(c || '').trim().toLowerCase();
    if (!c || c === 'transparent' || c === 'rgba(0, 0, 0, 0)' || c === 'rgba(0,0,0,0)') return 0;
    const m = c.match(/rgba\([^,]+,[^,]+,[^,]+,\s*([0-9.]+)\s*\)/);
    if (m) {
      const a = Number(m[1]);
      return Number.isFinite(a) ? a : 1;
    }
    return 1;
  }

  function isVisible(el, cs, r) {
    if (!r || r.width < 1 || r.height < 1) return false;
    if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || 1) < 0.02) return false;
    if (r.x > viewportW + 200 || r.right < -200 || r.y > Math.max(document.documentElement.scrollHeight, viewportH) + 200) return false;
    return true;
  }

  function hasIconClass(el) {
    const cls = String(el.className || '');
    return /material-symbols|material-icons|lucide|\bicon\b/i.test(cls);
  }

  function isIconText(text) {
    const t = clean(text).toLowerCase();
    return iconTexts.has(t) || /^[a-z]+(?:_[a-z]+)+$/.test(t);
  }

  function hasVisibleTextChild(el) {
    for (const child of Array.from(el.children || [])) {
      const t = clean(child.innerText || child.textContent || '');
      if (t && !hasIconClass(child)) return true;
    }
    return false;
  }

  function directText(el) {
    let out = '';
    for (const node of Array.from(el.childNodes || [])) {
      if (node.nodeType === Node.TEXT_NODE) out += ' ' + node.textContent;
    }
    return clean(out);
  }

  function ancestorMatches(el, selector) {
    try { return !!el.closest(selector); } catch (e) { return false; }
  }

  function isVisualBox(tag, cs, r) {
    if (!containerTags.has(tag)) return false;
    if (tag === 'body' || tag === 'html') return false;
    const bgA = alphaOf(cs.backgroundColor);
    const borderW = num(cs.borderTopWidth, 0) + num(cs.borderRightWidth, 0) + num(cs.borderBottomWidth, 0) + num(cs.borderLeftWidth, 0);
    const hasShadow = cs.boxShadow && cs.boxShadow !== 'none';
    const radius = num(cs.borderTopLeftRadius, 0) + num(cs.borderTopRightRadius, 0) + num(cs.borderBottomRightRadius, 0) + num(cs.borderBottomLeftRadius, 0);
    if (r.width > viewportW * 0.98 && r.height > viewportH * 0.98 && bgA <= 0.01 && borderW <= 0 && !hasShadow) return false;
    return bgA > 0.01 || borderW > 0 || hasShadow || radius > 8;
  }

  function stylePayload(el, cs) {
    const borderWidth = Math.max(num(cs.borderTopWidth, 0), num(cs.borderRightWidth, 0), num(cs.borderBottomWidth, 0), num(cs.borderLeftWidth, 0));
    const radius = Math.max(num(cs.borderTopLeftRadius, 0), num(cs.borderTopRightRadius, 0), num(cs.borderBottomRightRadius, 0), num(cs.borderBottomLeftRadius, 0));
    const fontSize = num(cs.fontSize, 14);
    const rawLineHeight = String(cs.lineHeight || '').toLowerCase();
    let sourceLineHeightPx = num(cs.lineHeight, 0);
    if (!sourceLineHeightPx || rawLineHeight === 'normal') sourceLineHeightPx = fontSize * 1.2;
    const penpotLineHeightRatio = Math.max(1, Math.min(2.4, sourceLineHeightPx / Math.max(fontSize, 1)));
    const sourceFontFamily = String(cs.fontFamily || '').split(',')[0].replace(/["']/g, '').trim();
    return {
      color: colorToHex(cs.color, '#0F172A'),
      fill: colorToHex(cs.backgroundColor, null),
      stroke: borderWidth > 0 ? colorToHex(cs.borderTopColor, '#CBD5E1') : null,
      stroke_width: borderWidth || 0,
      radius: radius || 0,
      font_size: fontSize,
      font_weight: String(cs.fontWeight || '400'),
      font_family: sourceFontFamily,
      source_font_family: sourceFontFamily,
      source_line_height_px: Math.round(sourceLineHeightPx * 100) / 100,
      penpot_line_height_ratio: Math.round(penpotLineHeightRatio * 1000) / 1000,
      line_height: Math.round(sourceLineHeightPx * 100) / 100,
      text_align: cs.textAlign || 'left',
      opacity: Number(cs.opacity || 1),
      box_shadow: cs.boxShadow && cs.boxShadow !== 'none' ? cs.boxShadow : null,
    };
  }

  function pushNode(nodes, el, kind, role, text, extra) {
    const cs = getComputedStyle(el);
    const r = rectOf(el);
    if (!isVisible(el, cs, r)) return;
    const style = stylePayload(el, cs);
    const tag = el.tagName.toLowerCase();
    const attrs = {};
    for (const attr of Array.from(el.attributes || [])) {
      if (['class','id','type','role','aria-label','placeholder','href','alt','title','name'].includes(attr.name)) {
        attrs[attr.name] = String(attr.value || '').slice(0, 250);
      }
    }
    nodes.push(Object.assign({
      tag,
      attrs,
      kind,
      role,
      text: clean(text || ''),
      bbox: { x: r.x, y: r.y, width: r.width, height: r.height },
      dom_depth: el.parents ? 0 : 0,
      area: r.area,
      css_class: String(el.className || '').slice(0, 250),
      id_attr: String(el.id || '').slice(0, 120),
      dom_path: domPath(el),
      source_seq: nodes.length,
    }, style, extra || {}));
  }

  const nodes = [];
  const ignoredImages = [];
  const all = Array.from(document.body ? document.body.querySelectorAll('*') : document.querySelectorAll('*'));

  for (const el of all) {
    const tag = el.tagName.toLowerCase();
    if (['script','style','meta','link','template','noscript','path'].includes(tag)) continue;
    const cs = getComputedStyle(el);
    const r = rectOf(el);
    if (!isVisible(el, cs, r)) continue;

    const fullText = clean(el.innerText || el.textContent || '');
    const dText = directText(el);
    const fontFamilyLower = String(cs.fontFamily || '').toLowerCase();
    const iconFontLike = fontFamilyLower.includes('material symbols') || fontFamilyLower.includes('material icons');
    const iconLike = hasIconClass(el) || (iconFontLike && isIconText(fullText));

    if (iconLike) {
      if (importIcons && fullText) pushNode(nodes, el, 'icon', 'icon', fullText, { is_icon: true });
      continue;
    }

    if (tag === 'svg') {
      const outer = el.outerHTML || '';
      pushNode(nodes, el, 'svg', 'media', '', { svg: outer.slice(0, 20000) });
      continue;
    }

    if (tag === 'img') {
      // v06.13.4+ strict media rule: images are currently out of scope for the
      // Penpot import. Do not create fallback surfaces or placeholders; the
      // design stays deterministic by explicitly ignoring <img> elements.
      ignoredImages.push({
        tag: 'img',
        bbox: { x: r.x, y: r.y, width: r.width, height: r.height },
        alt: String(el.getAttribute('alt') || el.getAttribute('data-alt') || '').slice(0, 250),
        css_class: String(el.className || '').slice(0, 250),
        dom_path: domPath(el),
        reason: 'strict_img_ignored_not_imported_to_penpot'
      });
      continue;
    }

    if (inputTags.has(tag)) {
      const type = (el.getAttribute('type') || tag).toLowerCase();
      const txt = el.getAttribute('placeholder') || el.getAttribute('aria-label') || el.getAttribute('value') || el.getAttribute('name') || type;
      const role = ['checkbox','radio','range','switch'].includes(type) ? 'control' : 'input';
      const kind = role === 'control' ? 'control' : 'input';
      pushNode(nodes, el, kind, role, txt, { input_type: type });
      continue;
    }

    if (tag === 'button' || el.getAttribute('role') === 'button') {
      const txt = fullText || el.getAttribute('aria-label') || el.getAttribute('title') || 'Button';
      const lowered = txt.toLowerCase();
      const primary = ['save','guardar','continue','continuar','submit','enviar','add','agregar','create','crear','update','actualizar','login','entrar','acceder','checkout','comprar','confirm','confirmar','sign in','iniciar','next','siguiente'].some(k => lowered.includes(k));
      pushNode(nodes, el, 'button', primary ? 'button_primary' : 'button_secondary', txt, {});
      continue;
    }

    if (tag === 'a' && fullText && !ancestorMatches(el, 'button')) {
      pushNode(nodes, el, 'text', 'body_text', fullText, { href_present: !!el.getAttribute('href') });
      continue;
    }

    if (textTags.has(tag) && fullText && !ancestorMatches(el, 'input, textarea, select')) {
      const insideButton = ancestorMatches(el, 'button, [role="button"]');
      const hasTextChild = hasVisibleTextChild(el);
      let txt = dText || fullText;
      // Exact source-to-layer fidelity: do not materialize text on a parent
      // element when its displayed string is only inherited from a textual
      // descendant. Example: <label><input/><span>Recordar sesión</span></label>
      // must produce the span text layer only, not a duplicated label layer.
      // v06.13.4: text leaves inside buttons are valid source elements. This
      // lets bottom-nav/button layouts map the real <span> label instead of a
      // projected label from button.textContent.
      if (hasTextChild && !dText) continue;
      if (hasTextChild && tag !== 'label' && !/^h[1-6]$/.test(tag)) continue;
      if (iconFontLike && isIconText(txt)) continue;
      let role = /^h[1-6]$/.test(tag) ? 'heading' : 'body_text';
      if (tag === 'label') role = 'label';
      if (insideButton) role = 'button_text';
      pushNode(nodes, el, 'text', role, txt, { direct_text: dText, text_from_descendants: hasTextChild && !dText, inside_button: insideButton });
      continue;
    }

    if (isVisualBox(tag, cs, r)) {
      let role = tag === 'nav' ? 'navigation' : (tag === 'form' ? 'form' : 'surface');
      let kind = tag === 'nav' ? 'surface' : (tag === 'form' ? 'surface' : 'surface');
      pushNode(nodes, el, kind, role, '', {});
    }
  }

  const doc = document.documentElement;
  const body = document.body;
  const documentHeight = Math.max(
    doc ? doc.scrollHeight : 0,
    body ? body.scrollHeight : 0,
    viewportH
  );
  const documentWidth = Math.max(
    doc ? doc.scrollWidth : 0,
    body ? body.scrollWidth : 0,
    viewportW
  );

  return {
    title: document.title || '',
    viewport: { width: viewportW, height: viewportH },
    document: { width: documentWidth, height: documentHeight },
    nodes,
    ignored_img_count: ignoredImages.length,
    ignored_img_preview: ignoredImages.slice(0, 20)
  };
}
"""


def _render_html_playwright_worker(html_text: str, viewport_width: int, viewport_height: int, timeout_ms: int, import_icons: bool) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright  # type: ignore

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        try:
            page = browser.new_page(
                viewport={"width": viewport_width, "height": viewport_height},
                device_scale_factor=1,
                is_mobile=viewport_width <= 520,
            )
            page.set_content(html_text, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
            except Exception:
                # Tailwind/fonts/CDNs can keep network busy. The DOM is enough.
                pass
            try:
                page.wait_for_timeout(600)
            except Exception:
                pass
            return page.evaluate(
                _RENDER_JS,
                {
                    "viewportWidth": viewport_width,
                    "viewportHeight": viewport_height,
                    "importIcons": import_icons,
                    "iconTexts": sorted(MATERIAL_SYMBOL_TEXTS),
                },
            )
        finally:
            browser.close()


def render_html_with_playwright(html_text: str, *, viewport_width: int, viewport_height: int) -> dict[str, Any]:
    timeout_ms = _env_int("STITCH_RENDERED_TIMEOUT_MS", 25000, minimum=5000)
    import_icons = _env_bool("STITCH_RENDERED_IMPORT_ICONS", False)
    # Run in a dedicated thread so this synchronous function can be called from
    # LangGraph async nodes without Playwright's sync API complaining about an
    # existing asyncio loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_render_html_playwright_worker, html_text, viewport_width, viewport_height, timeout_ms, import_icons)
        return future.result(timeout=(timeout_ms / 1000.0) + 10)


# -----------------------------------------------------------------------------
# Post-processing rendered nodes into ExternalDesignSpec children
# -----------------------------------------------------------------------------

def _node_name(node: dict[str, Any], fallback: str) -> str:
    text = clean_text(str(node.get("text") or ""), 60)
    if text:
        return normalize_name(text, fallback=fallback)
    id_attr = clean_text(str(node.get("id_attr") or ""), 60)
    if id_attr:
        return normalize_name(id_attr, fallback=fallback)
    cls = clean_text(str(node.get("css_class") or ""), 60)
    if cls:
        return normalize_name(cls, fallback=fallback)
    return fallback


def _bbox_valid(bbox: dict[str, Any]) -> bool:
    return safe_number(bbox.get("width"), 0) >= 1 and safe_number(bbox.get("height"), 0) >= 1


def _bbox_key(bbox: dict[str, Any], precision: int = 1) -> tuple[int, int, int, int]:
    factor = 10 ** precision
    return (
        int(round(safe_number(bbox.get("x"), 0) * factor)),
        int(round(safe_number(bbox.get("y"), 0) * factor)),
        int(round(safe_number(bbox.get("width"), 0) * factor)),
        int(round(safe_number(bbox.get("height"), 0) * factor)),
    )


def _intersects_or_contains(parent: dict[str, Any], child: dict[str, Any]) -> bool:
    px, py, pw, ph = [safe_number(parent.get(k), 0) for k in ("x", "y", "width", "height")]
    cx, cy, cw, ch = [safe_number(child.get(k), 0) for k in ("x", "y", "width", "height")]
    return cx >= px - 1 and cy >= py - 1 and cx + cw <= px + pw + 1 and cy + ch <= py + ph + 1


def _dedupe_rendered_nodes(nodes: list[dict[str, Any]], viewport_width: int, viewport_height: int) -> list[dict[str, Any]]:
    # Remove duplicate texts and redundant surfaces. Keep generic behavior.
    cleaned: list[dict[str, Any]] = []
    seen_text: set[tuple[str, tuple[int, int, int, int]]] = set()
    seen_box: set[tuple[str, tuple[int, int, int, int]]] = set()

    for node in nodes:
        bbox = node.get("bbox") if isinstance(node.get("bbox"), dict) else {}
        if not _bbox_valid(bbox):
            continue
        kind = str(node.get("kind") or "").lower()
        text = clean_text(str(node.get("text") or ""))
        if text and is_resource_ref(text):
            continue
        # v06.13.6: do not drop normal-font labels whose visible text is also
        # a Material Symbol token (e.g. "Home", "Settings"). Only filter
        # icon-like text when the node actually uses an icon font/class.
        if kind != "icon" and text and is_material_icon_text(text) and rendered_node_uses_material_icon_font_or_class(node):
            continue
        if kind == "text" and not meaningful_text(text):
            continue
        # Avoid creating a full-page duplicate surface; root op already covers it.
        area = safe_number(bbox.get("width"), 0) * safe_number(bbox.get("height"), 0)
        if kind == "surface" and area > viewport_width * viewport_height * 0.92:
            fill = css_color_to_hex(str(node.get("fill") or ""), None)
            stroke = css_color_to_hex(str(node.get("stroke") or ""), None)
            if not stroke and fill in {None, "#FFFFFF", "#F8FAFC", "#F7F9FB"}:
                continue
        key_box = _bbox_key(bbox)
        if kind == "text":
            key = (text.lower(), key_box)
            if key in seen_text:
                continue
            seen_text.add(key)
        else:
            key = (kind, key_box)
            if key in seen_box:
                continue
            seen_box.add(key)
        cleaned.append(node)

    return cleaned


SOURCE_SNAPSHOT_STYLE_KEYS = (
    "fill", "stroke", "stroke_width", "color", "text_color", "font_size", "font_weight",
    "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align", "radius", "opacity", "fill_opacity",
    "box_shadow", "input_type",
)


def _is_media_child(child: dict[str, Any]) -> bool:
    kind = str(child.get("kind") or "").lower()
    role = str(child.get("role") or "").lower()
    tag = str(child.get("tag") or "").lower()
    return kind in {"media", "image", "video", "avatar"} or role in {"media", "image", "avatar"} or tag in {"img", "picture", "video", "canvas"}


def _compact_source_expected(child: dict[str, Any]) -> dict[str, Any]:
    expected: dict[str, Any] = {"bbox": child.get("bbox") or {}}
    for key in SOURCE_SNAPSHOT_STYLE_KEYS:
        value = child.get(key)
        if value is not None and value != "":
            expected[key] = value
    if child.get("media_alt") is not None:
        expected["media_alt"] = str(child.get("media_alt") or "")[:500]
    if child.get("text") is not None and not _is_media_child(child):
        expected["text"] = str(child.get("text") or "")[:500]
    elif child.get("text") is not None and _is_media_child(child) and expected.get("media_alt") is None:
        expected["media_alt"] = str(child.get("text") or "")[:500]
    return expected


def _source_snapshot_for_rendered_node(node: dict[str, Any], child: dict[str, Any], source_ref: str) -> dict[str, Any]:
    attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
    return {
        "schema": "dvcp.source_element_snapshot.v1",
        "source_ref": source_ref,
        "origin": "rendered_playwright",
        "name": child.get("name"),
        "kind": child.get("kind"),
        "role": child.get("role"),
        "tag": child.get("tag") or node.get("tag"),
        "dom_path": node.get("dom_path"),
        "css_class": node.get("css_class"),
        "id_attr": node.get("id_attr"),
        "attrs": {k: attrs.get(k) for k in sorted(attrs) if attrs.get(k) not in (None, "")},
        "expected": _compact_source_expected(child),
    }


def rendered_nodes_to_children(rendered: dict[str, Any], *, width: int, height: int, title: str) -> list[dict[str, Any]]:
    raw_nodes = rendered.get("nodes") if isinstance(rendered, dict) else []
    raw_nodes = [n for n in raw_nodes if isinstance(n, dict)]
    doc = rendered.get("document") if isinstance(rendered.get("document"), dict) else {}
    viewport = rendered.get("viewport") if isinstance(rendered.get("viewport"), dict) else {}
    viewport_width = safe_int(viewport.get("width"), width)
    viewport_height = safe_int(viewport.get("height"), height)

    nodes = _dedupe_rendered_nodes(raw_nodes, viewport_width, viewport_height)

    # Use stable generic drawing order: surfaces first, then controls/media, then text.
    def draw_order(node: dict[str, Any]) -> tuple[int, float, float]:
        kind = str(node.get("kind") or "").lower()
        bbox = node.get("bbox") if isinstance(node.get("bbox"), dict) else {}
        area = safe_number(bbox.get("width"), 0) * safe_number(bbox.get("height"), 0)
        y = safe_number(bbox.get("y"), 0)
        if kind == "surface":
            return (0, -area, y)
        if kind in {"media", "svg", "input", "control", "button"}:
            return (1, y, area)
        return (2, y, area)

    nodes.sort(key=draw_order)

    max_elements = _env_int("STITCH_LAYOUT_MAX_ELEMENTS", 0, minimum=0)
    if max_elements > 0:
        nodes = nodes[:max_elements]

    children: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    for index, node in enumerate(nodes):
        kind = str(node.get("kind") or "text").lower()
        role = str(node.get("role") or "body_text")
        text = clean_text(str(node.get("text") or ""), 500)
        bbox = node.get("bbox") if isinstance(node.get("bbox"), dict) else {}
        if not _bbox_valid(bbox):
            continue
        # Clamp extremely wide nodes to document width, but keep actual layout.
        bbox = {
            "x": round(safe_number(bbox.get("x"), 0), 2),
            "y": round(safe_number(bbox.get("y"), 0), 2),
            "width": round(max(safe_number(bbox.get("width"), 1), 1), 2),
            "height": round(max(safe_number(bbox.get("height"), 1), 1), 2),
        }

        base_name = _node_name(node, fallback=f"{kind.title()}Element")
        counters[base_name] += 1
        name = base_name if counters[base_name] == 1 else f"{base_name}{counters[base_name]}"

        child: dict[str, Any] = {
            "name": name,
            "role": role,
            "kind": kind,
            "bbox": bbox,
            "tag": node.get("tag"),
        }
        for meta_key in ["css_class", "id_attr", "dom_path"]:
            value = node.get(meta_key)
            if value is not None and value != "":
                child[meta_key] = str(value)[:500]
        media_alt = clean_text(str(node.get("media_alt") or node.get("alt") or ""), 500)
        if text and kind != "media":
            child["text"] = text
        elif kind == "media" and text and not media_alt:
            child["media_alt"] = text
        if media_alt:
            child["media_alt"] = media_alt
        if node.get("svg"):
            child["svg"] = node.get("svg")
        for key in [
            "color", "fill", "stroke", "stroke_width", "radius", "font_size", "font_weight", "font_family",
            "line_height", "source_line_height_px", "penpot_line_height_ratio", "source_font_family", "text_align", "opacity", "box_shadow", "input_type", "is_icon",
        ]:
            value = node.get(key)
            if value is not None and value != "":
                child[key] = value
        # Avoid transparent fills on text nodes.
        if kind == "text":
            child.pop("fill", None)
            child.pop("stroke", None)
            child.pop("stroke_width", None)
        source_ref = f"rendered_{index:03d}"
        child["source_ref"] = source_ref
        child["source_snapshot"] = _source_snapshot_for_rendered_node(node, child, source_ref)
        # Keep surfaces editable but not noisy.
        if kind == "surface" and not child.get("fill") and not child.get("stroke") and not child.get("box_shadow"):
            continue
        children.append(child)

    if not children:
        children = layout_elements_static([], width=width, height=height, title=title)

    return children


# -----------------------------------------------------------------------------
# Tokens / payload helpers
# -----------------------------------------------------------------------------

def walk_values(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_values(child)
    elif isinstance(value, list):
        for item in value:
            yield from walk_values(item)


def extract_tokens(stitch_payload: Dict[str, Any], children: list[dict[str, Any]] | None = None) -> dict[str, str]:
    screen = stitch_payload.get("screen") or {}
    project_theme = None
    for item in walk_values(stitch_payload.get("listed_projects")):
        if isinstance(item, dict) and isinstance(item.get("designTheme"), dict):
            project_theme = item.get("designTheme")
            break
    theme = project_theme or {}
    named = theme.get("namedColors") if isinstance(theme, dict) else None
    named = named if isinstance(named, dict) else {}

    def color(*keys: str, fallback: str) -> str:
        for key in keys:
            value = named.get(key)
            if isinstance(value, str) and value.startswith("#"):
                return value
        return fallback

    child_colors = Counter()
    child_fills = Counter()
    for child in children or []:
        c = css_color_to_hex(str(child.get("color") or ""), None)
        f = css_color_to_hex(str(child.get("fill") or ""), None)
        if c:
            child_colors[c] += 1
        if f:
            child_fills[f] += 1

    common_text = child_colors.most_common(1)[0][0] if child_colors else "#0F172A"
    common_surface = child_fills.most_common(1)[0][0] if child_fills else "#FFFFFF"

    return {
        "color.background.canvas": color("background", "surface", fallback="#F8FAFC"),
        "color.surface.default": color("surface_container_lowest", "surface", fallback=common_surface or "#FFFFFF"),
        "color.text.default": color("on_surface", "on_background", fallback=common_text or "#0F172A"),
        "color.text.muted": color("on_surface_variant", "secondary", fallback="#64748B"),
        "color.border.default": color("outline_variant", "outline", fallback="#E2E8F0"),
        "color.action.primary.default": color("primary_container", "primary", fallback="#2563EB"),
        "color.action.primary.hover": color("primary", "primary_container", fallback="#1D4ED8"),
        "color.focus.ring": color("tertiary_container", "tertiary", fallback="#60A5FA"),
        "spacing.16": "16px",
        "spacing.24": "24px",
        "radius.card": "18px",
        "radius.input": "12px",
        "radius.button": "12px",
    }


def screen_from_payload(stitch_payload: Dict[str, Any]) -> dict[str, Any]:
    screen = stitch_payload.get("screen")
    if isinstance(screen, dict):
        return screen
    result = stitch_payload.get("result") or {}
    normalized = result.get("normalized") if isinstance(result, dict) else None
    if isinstance(normalized, dict):
        return normalized
    if isinstance(normalized, str):
        try:
            parsed = json.loads(normalized)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _metadata_without_heavy_debug(metadata: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in metadata.items():
        if key in {"html_preview", "payload_preview", "download_url", "html"}:
            continue
        out[key] = value
    return out


def build_external_design_spec_from_stitch(stitch_payload: Dict[str, Any], selection_hint: str = "") -> Dict[str, Any]:
    screen = screen_from_payload(stitch_payload)
    downloads = stitch_payload.get("downloads") or {}
    html_download = downloads.get("html") if isinstance(downloads, dict) else None
    html_download = html_download if isinstance(html_download, dict) else {}
    html_text = str(html_download.get("text") or "")

    if not html_text or html_download.get("ok") is not True:
        raise ValueError(
            json.dumps(
                {
                    "error": "stitch_html_download_missing_or_failed",
                    "message": "get_screen worked, but htmlCode.downloadUrl was not downloaded; refusing to build UI from Stitch metadata.",
                    "html_download": {k: v for k, v in html_download.items() if k != "text"},
                    "screen_keys": sorted(screen.keys()),
                },
                ensure_ascii=False,
            )
        )

    document_title = ""
    rendered: dict[str, Any] | None = None
    rendered_error: str | None = None
    extraction_mode = "static_fallback"

    screen_title_seed = clean_text(str(screen.get("title") or stitch_payload.get("screen_name") or "Imported Stitch Screen"), limit=80)
    width, height = normalize_screen_size(screen.get("width"), screen.get("height"), str(screen.get("deviceType") or ""))

    if _env_bool("STITCH_RENDERED_IMPORT", True):
        try:
            rendered = render_html_with_playwright(html_text, viewport_width=width, viewport_height=height)
            document_title = clean_text(str(rendered.get("title") or ""), 80)
            extraction_mode = "rendered_playwright"
        except Exception as exc:
            rendered_error = repr(exc)
            if not _env_bool("STITCH_STATIC_FALLBACK", True):
                raise

    if rendered is not None:
        screen_title = clean_text(str(screen.get("title") or document_title or stitch_payload.get("screen_name") or "Imported Stitch Screen"), limit=80)
        children = rendered_nodes_to_children(rendered, width=width, height=height, title=screen_title)
        doc = rendered.get("document") if isinstance(rendered.get("document"), dict) else {}
        content_bottom = max([safe_number((child.get("bbox") or {}).get("y"), 0) + safe_number((child.get("bbox") or {}).get("height"), 0) for child in children] + [height])
        # Use the rendered document height when it is reasonable. This avoids the old 1736px
        # flattened list when the logical screen is around 900px.
        doc_height = safe_number(doc.get("height"), height)
        height = int(round(max(height, min(max(content_bottom + 24, doc_height), max(doc_height, content_bottom + 24)))))
    else:
        elements, document_title = parse_html_static(html_text)
        screen_title = clean_text(str(screen.get("title") or document_title or stitch_payload.get("screen_name") or "Imported Stitch Screen"), limit=80)
        children = layout_elements_static(elements, width=width, height=height, title=screen_title)
        content_bottom = max([safe_number((child.get("bbox") or {}).get("y"), 0) + safe_number((child.get("bbox") or {}).get("height"), 0) for child in children] + [height])
        height = int(round(max(height, content_bottom + 36)))

    screen_type = infer_screen_type(children, screen_title)
    screen_name = normalize_name(screen_title, fallback="ImportedStitchScreen")
    tokens = extract_tokens(stitch_payload, children)

    metadata = {
        "stitch_mode": stitch_payload.get("mode"),
        "stitch_project_id": stitch_payload.get("project_id"),
        "stitch_project_name": stitch_payload.get("project_name"),
        "stitch_screen_id": stitch_payload.get("screen_id"),
        "stitch_screen_name": stitch_payload.get("screen_name"),
        "stitch_screen_resource": screen.get("name"),
        "selection_hint": selection_hint,
        "device_type": screen.get("deviceType"),
        "html_file": ((screen.get("structuredContent") or {}).get("htmlCode") or {}).get("name") if isinstance(screen.get("structuredContent"), dict) else None,
        "html_download_url_present": bool(((screen.get("structuredContent") or {}).get("htmlCode") or {}).get("downloadUrl")) if isinstance(screen.get("structuredContent"), dict) else False,
        "screenshot_file": ((screen.get("structuredContent") or {}).get("screenshot") or {}).get("name") if isinstance(screen.get("structuredContent"), dict) else None,
        "screenshot_download_url_present": bool(((screen.get("structuredContent") or {}).get("screenshot") or {}).get("downloadUrl")) if isinstance(screen.get("structuredContent"), dict) else False,
        "html_bytes_read": html_download.get("bytes_read"),
        "html_content_type": html_download.get("content_type"),
        "layout_extraction_mode": extraction_mode,
        "rendered_error": rendered_error,
        "rendered_element_count": len(children),
        "rendered_ignored_img_count": safe_int(rendered.get("ignored_img_count"), 0) if isinstance(rendered, dict) else 0,
        "rendered_ignored_img_preview": rendered.get("ignored_img_preview") if isinstance(rendered, dict) else [],
        "rendered_viewport": rendered.get("viewport") if isinstance(rendered, dict) else None,
        "rendered_document": rendered.get("document") if isinstance(rendered, dict) else None,
        "imported_icons": _env_bool("STITCH_RENDERED_IMPORT_ICONS", False),
    }

    return {
        "schema": "dvcp.external_design_spec.v1",
        "source": "stitch",
        "import_mode": "existing_screen_html_rendered" if extraction_mode == "rendered_playwright" else "existing_screen_html_static_fallback",
        "screen_name": screen_name,
        "screen_title": screen_title,
        "screen_type": screen_type,
        "width": width,
        "height": height,
        "tokens": tokens,
        "children": children,
        "metadata": _metadata_without_heavy_debug(metadata),
    }
