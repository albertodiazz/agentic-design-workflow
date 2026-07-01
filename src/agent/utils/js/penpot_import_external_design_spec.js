// DVCP Stitch import — SVG fast path.
// Imports the complete ExternalDesignSpec in one Penpot Plugin API call using
// penpot.createShapeFromSvg(svgString). This avoids per-layer createRectangle /
// createText calls that can exceed the 30s MCP execute_code timeout.

function asNumber(value, fallback) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  var parsed = Number(String(value || "").replace("px", ""));
  if (Number.isFinite(parsed)) return parsed;
  return fallback || 0;
}

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function safeName(value) {
  return String(value || "ImportedStitchScreen")
    .replace(/[^a-zA-Z0-9_\- ]+/g, "")
    .slice(0, 80) || "ImportedStitchScreen";
}

function colorForItem(item, fallback) {
  if (item && item.color) return String(item.color);
  if (item && item.fill) return String(item.fill);
  return fallback;
}

function rectSvg(item, opts) {
  opts = opts || {};
  var b = item.bbox || {};
  var x = asNumber(b.x, 0);
  var y = asNumber(b.y, 0);
  var w = Math.max(asNumber(b.width, 100), 1);
  var h = Math.max(asNumber(b.height, 40), 1);
  var rx = asNumber(item.radius, opts.radius || 0);
  var fill = opts.fill || item.fill || "#FFFFFF";
  var stroke = opts.stroke === undefined ? (item.stroke || "none") : opts.stroke;
  var sw = stroke && stroke !== "none" ? asNumber(opts.strokeWidth || item.stroke_width, 1) : 0;
  return '<rect data-name="' + esc(item.name || "Rect") + '" x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="' + rx + '" fill="' + esc(fill) + '" stroke="' + esc(stroke || "none") + '" stroke-width="' + sw + '" />';
}

function textSvg(item, opts) {
  opts = opts || {};
  var b = item.bbox || {};
  var x = asNumber(b.x, 0);
  var y = asNumber(b.y, 0);
  var fontSize = asNumber(item.font_size, opts.fontSize || 14);
  var fill = colorForItem(item, opts.fill || "#0F172A");
  var text = String(item.text || item.name || "").slice(0, 260);
  var baselineY = y + Math.max(fontSize, 12) + 2;
  return '<text data-name="' + esc(item.name || "Text") + '" x="' + x + '" y="' + baselineY + '" fill="' + esc(fill) + '" font-family="Inter, Arial, sans-serif" font-size="' + fontSize + '" font-weight="' + (opts.fontWeight || 400) + '">' + esc(text) + '</text>';
}

function inputSvg(item) {
  var b = item.bbox || {};
  var x = asNumber(b.x, 0);
  var y = asNumber(b.y, 0);
  var w = Math.max(asNumber(b.width, 100), 1);
  var h = Math.max(asNumber(b.height, 40), 1);
  var label = {
    name: String(item.name || "Input") + "Text",
    text: item.text || "Input",
    font_size: 14,
    color: "#64748B",
    bbox: { x: x + 16, y: y + Math.max((h - 22) / 2, 8), width: Math.max(w - 32, 40), height: 22 }
  };
  return rectSvg(item, { fill: "#FFFFFF", stroke: "#CBD5E1", strokeWidth: 1, radius: item.radius || 12 }) + textSvg(label);
}

function buttonSvg(item) {
  var role = String(item.role || "button");
  var primary = role.indexOf("primary") !== -1;
  var b = item.bbox || {};
  var x = asNumber(b.x, 0);
  var y = asNumber(b.y, 0);
  var w = Math.max(asNumber(b.width, 100), 1);
  var h = Math.max(asNumber(b.height, 40), 1);
  var fill = primary ? "#2563EB" : "#FFFFFF";
  var stroke = primary ? "none" : "#CBD5E1";
  var label = {
    name: String(item.name || "Button") + "Text",
    text: item.text || "Button",
    font_size: 15,
    color: primary ? "#FFFFFF" : "#0F172A",
    bbox: { x: x + 16, y: y + Math.max((h - 22) / 2, 8), width: Math.max(w - 32, 40), height: 22 }
  };
  return rectSvg(item, { fill: fill, stroke: stroke, strokeWidth: 1, radius: item.radius || 12 }) + textSvg(label, { fontWeight: 600 });
}

function cardSvg(item) {
  var svg = rectSvg(item, { fill: item.fill || "#FFFFFF", stroke: item.stroke || "#E2E8F0", strokeWidth: 1, radius: item.radius || 18 });
  if (item.text) {
    var b = item.bbox || {};
    svg += textSvg({
      name: String(item.name || "Card") + "Text",
      text: item.text,
      font_size: item.font_size || 14,
      color: item.color || "#0F172A",
      bbox: {
        x: asNumber(b.x, 0) + 16,
        y: asNumber(b.y, 0) + 16,
        width: Math.max(asNumber(b.width, 100) - 32, 40),
        height: Math.max(asNumber(b.height, 40) - 32, 24)
      }
    });
  }
  return svg;
}

function itemToSvg(item) {
  item = item || {};
  var kind = String(item.kind || "text").toLowerCase();
  if (kind === "text") return textSvg(item, { fontWeight: String(item.role || "").indexOf("heading") !== -1 ? 700 : 400 });
  if (kind === "input") return inputSvg(item);
  if (kind === "button") return buttonSvg(item);
  if (kind === "card" || kind === "surface" || kind === "table" || kind === "upload" || kind === "chart") return cardSvg(item);
  return cardSvg(item);
}

function buildSvg(spec) {
  var width = Math.max(asNumber(spec.width, 390), 1);
  var height = Math.max(asNumber(spec.height, 860), 1);
  var children = Array.isArray(spec.children) ? spec.children : [];
  var parts = [];
  parts.push('<svg xmlns="http://www.w3.org/2000/svg" width="' + width + '" height="' + height + '" viewBox="0 0 ' + width + ' ' + height + '">');
  parts.push('<rect data-name="CanvasBackground" x="0" y="0" width="' + width + '" height="' + height + '" fill="' + esc((spec.tokens && spec.tokens["color.background.canvas"]) || "#F8FAFC") + '"/>');
  for (var i = 0; i < children.length; i++) {
    parts.push('<g data-name="' + esc(children[i].name || ('Item' + i)) + '" data-role="' + esc(children[i].role || '') + '" data-kind="' + esc(children[i].kind || '') + '">');
    parts.push(itemToSvg(children[i]));
    parts.push('</g>');
  }
  parts.push('</svg>');
  return parts.join('\n');
}

function trySet(shape, prop, value) {
  try { if (shape) shape[prop] = value; return true; } catch (err) { return false; }
}

(function () {
  try {
    var spec = __EXTERNAL_DESIGN_SPEC_JSON__;
    var svg = buildSvg(spec || {});
    var group = null;

    if (typeof penpot.createShapeFromSvg !== "function") {
      return JSON.stringify({
        all_applied: false,
        action: "import_external_design_spec",
        import_strategy: "svg_fast_path",
        error: "createShapeFromSvg_not_available",
        checked_count: 1,
        applied_count: 0,
        failed_count: 1,
        created_shape_count: 0,
        __dvcp_result_marker: "penpot_import_external_design_spec"
      });
    }

    group = penpot.createShapeFromSvg(svg);
    if (!group) {
      return JSON.stringify({
        all_applied: false,
        action: "import_external_design_spec",
        import_strategy: "svg_fast_path",
        error: "svg_group_not_created",
        checked_count: 1,
        applied_count: 0,
        failed_count: 1,
        created_shape_count: 0,
        svg_length: svg.length,
        __dvcp_result_marker: "penpot_import_external_design_spec"
      });
    }

    var name = safeName(spec.screen_name || spec.screen_title || "ImportedStitchScreen");
    trySet(group, "name", name);
    trySet(group, "x", asNumber(spec.canvas_x, 120));
    trySet(group, "y", asNumber(spec.canvas_y, 80));

    try {
      if (typeof group.setPluginData === "function") {
        group.setPluginData("dvcp.source", "stitch");
        group.setPluginData("dvcp.import_strategy", "svg_fast_path");
        group.setPluginData("dvcp.screen_type", String(spec.screen_type || "unknown"));
        group.setPluginData("dvcp.child_count", String((spec.children || []).length));
      }
    } catch (errMeta) {}

    try { penpot.selection = [group]; } catch (errSel) {}

    return JSON.stringify({
      all_applied: true,
      action: "import_external_design_spec",
      source: String(spec.source || "stitch"),
      import_mode: String(spec.import_mode || "existing_screen_html"),
      import_strategy: "svg_fast_path",
      screen_name: name,
      screen_type: String(spec.screen_type || "unknown"),
      checked_count: (spec.children || []).length,
      applied_count: (spec.children || []).length,
      failed_count: 0,
      created_shape_count: 1,
      svg_length: svg.length,
      root_id: group && group.id ? String(group.id) : null,
      error: null,
      __dvcp_result_marker: "penpot_import_external_design_spec"
    });
  } catch (err) {
    return JSON.stringify({
      all_applied: false,
      action: "import_external_design_spec",
      import_strategy: "svg_fast_path",
      error: "js_exception",
      message: err && err.message ? String(err.message) : String(err),
      stack: err && err.stack ? String(err.stack).slice(0, 1200) : null,
      checked_count: 0,
      applied_count: 0,
      failed_count: 1,
      created_shape_count: 0,
      __dvcp_result_marker: "penpot_import_external_design_spec"
    });
  }
})();
