function asNumber(value, fallback) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (value === undefined || value === null) return fallback || 0;
  var parsed = Number(String(value).replace("px", ""));
  if (Number.isFinite(parsed)) return parsed;
  return fallback || 0;
}

function asString(value, fallback) {
  if (value === undefined || value === null) return fallback || "";
  return String(value);
}

function trySet(shape, prop, value) {
  if (!shape) return false;
  try { shape[prop] = value; return true; } catch (err) { return false; }
}

function setName(shape, name) {
  if (!shape || !name) return false;
  var value = String(name).slice(0, 120);
  var ok = false;
  ok = trySet(shape, "name", value) || ok;
  if (String(shape.name || "") !== value && typeof shape.rename === "function") {
    try { shape.rename(value); ok = true; } catch (err1) {}
  }
  if (String(shape.name || "") !== value && typeof shape.setName === "function") {
    try { shape.setName(value); ok = true; } catch (err2) {}
  }
  return ok;
}


function getShapeId(shape) {
  if (!shape) return "";
  var keys = ["id", "uuid", "shapeId", "shape_id", "$id"];
  for (var i = 0; i < keys.length; i++) {
    try {
      var value = shape[keys[i]];
      if (value !== undefined && value !== null && String(value).trim()) return String(value);
    } catch (err1) {}
  }
  var methods = ["getId", "getID", "getUuid", "getUUID"];
  for (var j = 0; j < methods.length; j++) {
    try {
      if (typeof shape[methods[j]] === "function") {
        var mvalue = shape[methods[j]]();
        if (mvalue !== undefined && mvalue !== null && String(mvalue).trim()) return String(mvalue);
      }
    } catch (err2) {}
  }
  return "";
}

function setPluginDataSafe(shape, key, value) {
  if (!shape) return false;
  var text = typeof value === "string" ? value : JSON.stringify(value);
  var ok = false;
  try {
    if (typeof shape.setPluginData === "function") {
      shape.setPluginData(key, text);
      ok = true;
    }
  } catch (err1) {}
  try {
    if (typeof shape.setSharedPluginData === "function") {
      shape.setSharedPluginData("dvcp", key, text);
      ok = true;
    }
  } catch (err2) {}
  return ok;
}

function getPluginDataSafe(shape, key) {
  if (!shape) return "";
  try {
    if (typeof shape.getPluginData === "function") {
      var v = shape.getPluginData(key);
      if (v !== undefined && v !== null && String(v).trim()) return String(v);
    }
  } catch (err1) {}
  try {
    if (typeof shape.getSharedPluginData === "function") {
      var sv = shape.getSharedPluginData("dvcp", key);
      if (sv !== undefined && sv !== null && String(sv).trim()) return String(sv);
    }
  } catch (err2) {}
  return "";
}

function readChildren(shape) {
  if (!shape) return [];
  var candidates = [shape.children, shape.shapes, shape.items];
  for (var i = 0; i < candidates.length; i++) {
    var c = candidates[i];
    if (c && typeof c.length === "number") {
      var out = [];
      for (var j = 0; j < c.length; j++) out.push(c[j]);
      return out;
    }
  }
  return [];
}

function walkShapes(root, out) {
  if (!root) return;
  out.push(root);
  var children = readChildren(root);
  for (var i = 0; i < children.length; i++) walkShapes(children[i], out);
}

function allPageShapes() {
  var out = [];

  // In the Penpot MCP/plugin runtime, currentPage.children is not always
  // enumerable, even when the shapes exist in the layer tree. penpotUtils
  // is the most reliable way to inspect the actual document tree.
  try {
    if (typeof penpotUtils !== "undefined" && penpotUtils && typeof penpotUtils.findShapes === "function" && penpot.root) {
      var found = penpotUtils.findShapes(function (shape) { return !!shape; }, penpot.root);
      if (found && typeof found.length === "number" && found.length > 0) {
        for (var f = 0; f < found.length; f++) out.push(found[f]);
        return out;
      }
    }
  } catch (errFind) {}

  try { if (penpot.root) walkShapes(penpot.root, out); } catch (errRoot) {}
  if (out.length === 0 && penpot.currentPage) walkShapes(penpot.currentPage, out);
  return out;
}

function findRootForJob(jobId, rootName) {
  if (!jobId) return null;
  var shapes = allPageShapes();
  var expectedRootName = String(rootName || "").trim();
  for (var i = 0; i < shapes.length; i++) {
    var shape = shapes[i];
    var source = getPluginDataSafe(shape, "source");
    var job = getPluginDataSafe(shape, "import_job");
    var role = getPluginDataSafe(shape, "semantic_role");
    if (source === "stitch" && job === String(jobId) && role === "screen_root") return shape;
  }
  // Generic fallback by root name because some plugin-data APIs are flaky in
  // MCP/plugin contexts. The name is supplied by the queue for any screen; it is
  // not tied to a specific Stitch template.
  if (expectedRootName) {
    for (var j = 0; j < shapes.length; j++) {
      var s = shapes[j];
      if (String(s.name || "") === expectedRootName) return s;
    }
  }
  return null;
}

function appendToRootIfPossible(root, shape) {
  if (!root || !shape || root === shape) return false;
  try {
    if (typeof root.appendChild === "function") {
      root.appendChild(shape);
      return true;
    }
  } catch (err1) {}
  try {
    if (root.children && typeof root.children.append === "function") {
      root.children.append(shape);
      return true;
    }
  } catch (err2) {}
  return false;
}

function setPositionAndSize(shape, bbox, offset) {
  if (!shape) return false;
  bbox = bbox || {};
  offset = offset || { x: 0, y: 0 };
  var x = asNumber(bbox.x, 0) + asNumber(offset.x, 0);
  var y = asNumber(bbox.y, 0) + asNumber(offset.y, 0);
  var w = Math.max(asNumber(bbox.width, 100), 1);
  var h = Math.max(asNumber(bbox.height, 40), 1);

  trySet(shape, "x", x);
  trySet(shape, "y", y);
  trySet(shape, "width", w);
  trySet(shape, "height", h);
  trySet(shape, "w", w);
  trySet(shape, "h", h);

  if (typeof shape.setPosition === "function") {
    try { shape.setPosition(x, y); } catch (err1) {}
  }
  if (typeof shape.setSize === "function") {
    try { shape.setSize(w, h); } catch (err2) {}
  }
  if (typeof shape.resize === "function") {
    try { shape.resize(w, h); } catch (err3) {}
  }
  return true;
}

function isTransparentColor(color) {
  var c = String(color || "").toLowerCase().trim();
  return !c || c === "transparent" || c === "none" || c === "rgba(0, 0, 0, 0)" || c === "rgba(0,0,0,0)";
}


function escXml(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function safePaint(color, fallback) {
  var c = String(color === undefined || color === null ? "" : color).trim();
  if (isTransparentColor(c)) return fallback === undefined ? "none" : String(fallback);
  return c;
}

function safeOpacity(value, fallback) {
  var o = asNumber(value, fallback === undefined ? 1 : fallback);
  if (o < 0) o = 0;
  if (o > 1) o = 1;
  return o;
}

function valueOrDefault(value, fallback) {
  return value === undefined ? fallback : value;
}

function bboxLocal(op) {
  var b = (op && op.bbox) || {};
  return {
    x: asNumber(b.x, 0),
    y: asNumber(b.y, 0),
    width: Math.max(asNumber(b.width, 100), 1),
    height: Math.max(asNumber(b.height, 40), 1)
  };
}

function localTextAnchor(align) {
  var a = normalizeTextAlign(align || "left");
  if (a === "center") return "middle";
  if (a === "right") return "end";
  return "start";
}

function localTextX(width, align, pad) {
  var a = normalizeTextAlign(align || "left");
  if (a === "center") return width / 2;
  if (a === "right") return Math.max(width - (pad || 0), 0);
  return pad || 0;
}

function splitTextLines(value) {
  var text = String(value === undefined || value === null ? "" : value).replace(/\s+/g, " ").trim();
  if (!text) return [""];
  // Keep the fallback generic: preserve explicit short text, and wrap only very
  // long prose so a visual text layer does not become one unreadable line.
  var words = text.split(" ");
  if (words.length <= 6 || text.length <= 42) return [text];
  var lines = [];
  var current = "";
  for (var i = 0; i < words.length; i++) {
    var next = current ? current + " " + words[i] : words[i];
    if (next.length > 42 && current) {
      lines.push(current);
      current = words[i];
    } else {
      current = next;
    }
  }
  if (current) lines.push(current);
  return lines.slice(0, 8);
}

function svgTextFragment(name, text, box, style) {
  style = style || {};
  var w = Math.max(asNumber(box && box.width, 100), 1);
  var h = Math.max(asNumber(box && box.height, 24), 1);
  var x0 = asNumber(box && box.x, 0);
  var y0 = asNumber(box && box.y, 0);
  var fontSize = Math.max(asNumber(style.font_size, 14), 1);
  var lineHeight = Math.max(asNumber(style.line_height, Math.round(fontSize * 1.25)), fontSize);
  var align = normalizeTextAlign(style.text_align || "left");
  var anchor = localTextAnchor(align);
  var pad = asNumber(style.pad, 0);
  var tx = x0 + localTextX(w, align, pad);
  var firstY = y0 + Math.min(Math.max(fontSize, 10), h);
  var fill = safePaint(style.color || style.text_color, "#0F172A");
  var opacity = safeOpacity(style.opacity, 1);
  var weight = escXml(style.font_weight || "400");
  var family = escXml(normalizeFontFamily(style.font_family) || "Arial, sans-serif");
  var lines = splitTextLines(text);
  var spans = [];
  for (var i = 0; i < lines.length; i++) {
    var dy = i === 0 ? 0 : lineHeight;
    spans.push('<tspan x="' + tx + '" dy="' + dy + '">' + escXml(lines[i]) + '</tspan>');
  }
  return '<text data-name="' + escXml(name || "Text") + '" x="' + tx + '" y="' + firstY + '" fill="' + escXml(fill) + '" opacity="' + opacity + '" font-family="' + family + '" font-size="' + fontSize + '" font-weight="' + weight + '" text-anchor="' + anchor + '">' + spans.join("") + '</text>';
}

function svgRectFragment(name, box, fill, stroke, strokeWidth, radius, opacity) {
  var w = Math.max(asNumber(box && box.width, 100), 1);
  var h = Math.max(asNumber(box && box.height, 40), 1);
  var x = asNumber(box && box.x, 0);
  var y = asNumber(box && box.y, 0);
  var rx = Math.max(asNumber(radius, 0), 0);
  var f = safePaint(fill, "none");
  var s = safePaint(stroke, "none");
  var sw = s === "none" ? 0 : Math.max(asNumber(strokeWidth, 1), 0);
  var o = safeOpacity(opacity, 1);
  return '<rect data-name="' + escXml(name || "Rect") + '" x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="' + rx + '" fill="' + escXml(f) + '" stroke="' + escXml(s) + '" stroke-width="' + sw + '" opacity="' + o + '" />';
}

function buildLocalSvg(width, height, parts) {
  var w = Math.max(asNumber(width, 100), 1);
  var h = Math.max(asNumber(height, 40), 1);
  return '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">' + parts.join("\n") + '</svg>';
}

function visualSvgForOp(op) {
  op = op || {};
  var b = bboxLocal(op);
  var local = { x: 0, y: 0, width: b.width, height: b.height };
  var kind = String(op.kind || "").toLowerCase();
  var role = String(op.role || "").toLowerCase();
  var opName = String(op.name || kind || op.op || "Layer");
  var parts = [];
  var stroke = op.stroke;
  var strokeWidth = op.stroke_width;
  var radius = op.radius;
  var fill = op.fill;
  var opacity = op.opacity;

  if (op.op === "create_text") {
    parts.push(svgTextFragment(opName, op.text || op.name || "", local, textStyleFromOp(op)));
    return buildLocalSvg(b.width, b.height, parts);
  }

  if (op.op === "create_icon") {
    var w = b.width, h = b.height;
    var roleText = String((op.role || "") + " " + (op.component_type || "") + " " + (op.name || "")).toLowerCase();
    var fillLooksSet = !isTransparentColor(op.fill) && String(op.fill || "").trim() && String(op.fill || "none").toLowerCase() !== "none";
    var hasBackground = fillLooksSet && (roleText.indexOf("hero") >= 0 || roleText.indexOf("avatar") >= 0 || roleText.indexOf("badge") >= 0 || (w >= 36 && h >= 36 && !op.color));
    var iconColor = op.color || (hasBackground ? "#FFFFFF" : (fillLooksSet ? op.fill : "#475569"));
    if (hasBackground) {
      parts.push(svgRectFragment(opName + "_Bg", local, op.fill, op.stroke || "none", op.stroke_width || 0, op.radius === undefined ? Math.min(w, h) / 4 : op.radius, opacity));
    }
    parts.push(svgTextFragment(opName + "_Glyph", iconTextFromOp(op), local, {
      color: iconColor,
      font_size: Math.max(asNumber(op.font_size, Math.min(w, h)), 10),
      font_weight: normalizeFontWeight(op.font_weight || op.weight || "400"),
      font_family: "Arial, sans-serif",
      line_height: Math.max(asNumber(op.font_size, Math.min(w, h)), 10) + 2,
      text_align: "center",
      opacity: opacity === undefined ? 1 : opacity
    }));
    return buildLocalSvg(b.width, b.height, parts);
  }

  if (op.op === "create_button") {
    var primary = role.indexOf("primary") >= 0;
    var secondaryTextOnly = !primary && isTransparentColor(fill) && (!stroke || asNumber(strokeWidth, 0) <= 0 || isTransparentColor(stroke));
    if (!secondaryTextOnly) {
      parts.push(svgRectFragment(opName, local, fill || (primary ? "#2563EB" : "#FFFFFF"), stroke || (primary ? "none" : "#CBD5E1"), strokeWidth === undefined ? (primary ? 0 : 1) : strokeWidth, radius === undefined ? 12 : radius, opacity));
    }
    if (op.text) {
      parts.push(svgTextFragment(opName + "_Text", op.text, { x: 8, y: Math.max((b.height - 22) / 2, 4), width: Math.max(b.width - 16, 1), height: Math.max(Math.min(b.height, 26), 12) }, {
        color: op.color || (primary ? "#FFFFFF" : "#0F172A"),
        font_size: op.font_size || 15,
        font_weight: normalizeFontWeight(op.font_weight || op.weight || "600"),
        font_family: op.font_family,
        line_height: op.line_height || 20,
        text_align: op.text_align || "center",
        opacity: opacity === undefined ? 1 : opacity
      }));
    }
    return buildLocalSvg(b.width, b.height, parts);
  }

  if (op.op === "create_input") {
    parts.push(svgRectFragment(opName, local, fill || "#FFFFFF", stroke || "#CBD5E1", strokeWidth === undefined ? 1 : strokeWidth, radius === undefined ? 12 : radius, opacity));
    if (op.text) {
      parts.push(svgTextFragment(opName + "_Text", op.text, { x: 12, y: Math.max((b.height - 20) / 2, 4), width: Math.max(b.width - 24, 1), height: Math.max(Math.min(b.height, 24), 12) }, {
        color: op.color || "#64748B",
        font_size: Math.max(asNumber(op.font_size, 14), 10),
        font_weight: normalizeFontWeight(op.font_weight || op.weight || "400"),
        font_family: op.font_family,
        line_height: op.line_height || 20,
        text_align: op.text_align || "left",
        opacity: opacity === undefined ? 1 : opacity
      }));
    }
    return buildLocalSvg(b.width, b.height, parts);
  }

  if (op.op === "create_card" || op.op === "create_rect") {
    var defaultRadius = op.op === "create_card" ? 18 : 0;
    parts.push(svgRectFragment(opName, local, fill || "#FFFFFF", stroke || "none", strokeWidth || 0, radius === undefined ? defaultRadius : radius, opacity));
    if (op.text) {
      parts.push(svgTextFragment(opName + "_Text", op.text, { x: 12, y: 12, width: Math.max(b.width - 24, 1), height: Math.max(b.height - 24, 1) }, {
        color: op.color || "#0F172A",
        font_size: op.font_size || 14,
        font_weight: normalizeFontWeight(op.font_weight || op.weight || "400"),
        font_family: op.font_family,
        line_height: op.line_height || 20,
        text_align: op.text_align || "left",
        opacity: opacity === undefined ? 1 : opacity
      }));
    }
    return buildLocalSvg(b.width, b.height, parts);
  }

  return null;
}

function compactOpVisualValues(op) {
  op = op || {};
  var out = { bbox: op.bbox || {} };
  var keys = [
    "text", "fill", "stroke", "stroke_width", "color", "text_color", "font_size",
    "font_weight", "font_family", "source_font_family", "line_height", "source_line_height_px", "penpot_line_height_ratio", "text_align", "radius", "opacity",
    "fill_opacity", "box_shadow", "input_type", "media_alt", "text_no_wrap", "expected_line_count", "source_line_count", "penpot_grow_type"
  ];
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i];
    if (op[k] !== undefined && op[k] !== null && String(op[k]) !== "") out[k] = op[k];
  }
  return out;
}

function compactShapeReadback(shape) {
  if (!shape) return {};
  var out = { id: getShapeId(shape), name: String(shape.name || "") };
  var props = ["x", "y", "width", "height", "w", "h", "fills", "strokes", "opacity", "fontSize", "fontFamily", "fontWeight", "lineHeight", "growType", "text", "characters"];
  for (var i = 0; i < props.length; i++) {
    try {
      var v = shape[props[i]];
      if (v !== undefined && v !== null && typeof v !== "function") out[props[i]] = v;
    } catch (err) {}
  }
  var pd = ["dvcp_grow_type", "text_no_wrap", "expected_line_count", "source_line_count", "media_alt"];
  for (var pi = 0; pi < pd.length; pi++) {
    var pv = getPluginDataSafe(shape, pd[pi]);
    if (pv !== undefined && pv !== null && String(pv) !== "") out[pd[pi]] = pv;
  }
  return out;
}

function buildSourceTraceFromOp(op) {
  op = op || {};
  var trace = op.source_trace && typeof op.source_trace === "object" ? Object.assign({}, op.source_trace) : {};
  trace.schema = trace.schema || "dvcp.source_trace.v1";
  trace.source_ref = trace.source_ref || op.source_ref || "";
  trace.source_name = trace.source_name || op.source_name || op.name || "";
  trace.source_snapshot = trace.source_snapshot || op.source_snapshot || null;
  trace.penpot_op_values = compactOpVisualValues(op);
  trace.component_id = trace.component_id || op.component_id || "";
  trace.component_type = trace.component_type || op.component_type || "";
  trace.role = trace.role || op.role || "";
  trace.kind = trace.kind || op.kind || "";
  trace.slot = trace.slot || op.slot || "";
  trace.source_fidelity = trace.source_fidelity || op.source_fidelity || null;
  trace.deterministic_transform = trace.deterministic_transform || op.deterministic_transform || null;
  return trace;
}


function createVisualMaterializedShape(op, offset) {
  op = op || {};
  offset = offset || { x: 0, y: 0 };
  var svg = visualSvgForOp(op);
  var info = {
    schema: "dvcp.visual_materialization.v1",
    version: "v06.13.6",
    method: "svg_fallback",
    visually_materialized: false,
    fallback_used: false,
    expected_text: !!op.text,
    expected_fill: !isTransparentColor(op.fill),
    error: null
  };
  if (!svg || typeof penpot.createShapeFromSvg !== "function") {
    info.method = "native_fallback";
    info.fallback_used = true;
    info.error = svg ? "createShapeFromSvg_unavailable" : "visual_svg_not_supported_for_op";
    return { shape: null, info: info };
  }
  var shape = null;
  try { shape = penpot.createShapeFromSvg(svg); } catch (err1) { shape = null; info.error = String(err1 && err1.message ? err1.message : err1).slice(0, 200); }
  if (!shape) {
    info.method = "native_fallback";
    info.fallback_used = true;
    if (!info.error) info.error = "svg_shape_not_created";
    return { shape: null, info: info };
  }
  setName(shape, op.name || op.kind || op.op || "Layer");
  setPositionAndSize(shape, op.bbox || {}, offset);
  setOpacity(shape, op.opacity === undefined ? 1 : op.opacity);
  info.visually_materialized = true;
  info.svg_length = svg.length;
  return { shape: shape, info: info };
}

function setFill(shape, color, opacity) {
  if (!shape) return false;
  var c = color === undefined || color === null ? "" : String(color);
  if (isTransparentColor(c)) {
    trySet(shape, "fills", []);
    return true;
  }
  var o = asNumber(opacity, 1);
  if (o < 0) o = 0;
  if (o > 1) o = 1;
  var fillVariants = [
    [{ fillColor: c, fillOpacity: o }],
    [{ color: c, opacity: o }],
    [{ type: "solid", color: c, opacity: o }],
    [{ type: "solid", fillColor: c, fillOpacity: o }]
  ];
  var ok = false;
  for (var vi = 0; vi < fillVariants.length; vi++) {
    try { shape.fills = fillVariants[vi]; ok = true; break; } catch (err0) {}
  }
  ok = trySet(shape, "fill", c) || ok;
  ok = trySet(shape, "fillColor", c) || ok;
  ok = trySet(shape, "fillOpacity", o) || ok;
  ok = trySet(shape, "backgroundColor", c) || ok;
  ok = trySet(shape, "backgroundOpacity", o) || ok;
  ok = trySet(shape, "color", c) || ok;
  if (typeof shape.setFillColor === "function") {
    try { shape.setFillColor(c); ok = true; } catch (err1) {}
  }
  if (typeof shape.setFills === "function") {
    for (var vf = 0; vf < fillVariants.length; vf++) {
      try { shape.setFills(fillVariants[vf]); ok = true; break; } catch (err2) {}
    }
  }
  return ok;
}

function setStroke(shape, color, width) {
  if (!shape || isTransparentColor(color)) {
    if (shape) trySet(shape, "strokes", []);
    return false;
  }
  var c = String(color);
  var w = asNumber(width, 1);
  if (w <= 0) {
    trySet(shape, "strokes", []);
    return true;
  }
  var strokes = [{ strokeColor: c, strokeOpacity: 1, strokeWidth: w }];
  var ok = false;
  ok = trySet(shape, "strokes", strokes) || ok;
  ok = trySet(shape, "stroke", c) || ok;
  ok = trySet(shape, "strokeColor", c) || ok;
  ok = trySet(shape, "strokeOpacity", 1) || ok;
  ok = trySet(shape, "strokeWidth", w) || ok;
  if (typeof shape.setStrokes === "function") {
    try { shape.setStrokes(strokes); ok = true; } catch (err1) {}
  }
  return ok;
}

function setRadius(shape, radius) {
  if (!shape || radius === undefined || radius === null) return false;
  var r = Math.max(asNumber(radius, 0), 0);
  trySet(shape, "borderRadius", r);
  trySet(shape, "borderRadiusTopLeft", r);
  trySet(shape, "borderRadiusTopRight", r);
  trySet(shape, "borderRadiusBottomRight", r);
  trySet(shape, "borderRadiusBottomLeft", r);
  return true;
}

function setOpacity(shape, opacity) {
  if (!shape || opacity === undefined || opacity === null) return false;
  var o = asNumber(opacity, 1);
  if (o < 0) o = 0;
  if (o > 1) o = 1;
  return trySet(shape, "opacity", o);
}


function parseCssBoxShadow(value) {
  var raw = String(value || "").trim();
  if (!raw || raw === "none") return null;
  var color = "#000000";
  var opacity = 0.18;
  var rgba = raw.match(/rgba?\(([^\)]+)\)/i);
  if (rgba) {
    var parts = rgba[1].split(',').map(function (p) { return String(p).trim(); });
    var r = Math.max(0, Math.min(255, Math.round(asNumber(parts[0], 0))));
    var g = Math.max(0, Math.min(255, Math.round(asNumber(parts[1], 0))));
    var b = Math.max(0, Math.min(255, Math.round(asNumber(parts[2], 0))));
    opacity = parts.length > 3 ? safeOpacity(parts[3], opacity) : 1;
    color = "#" + [r, g, b].map(function (n) { return ("0" + n.toString(16)).slice(-2); }).join("").toUpperCase();
    raw = raw.replace(rgba[0], " ");
  }
  var nums = raw.match(/-?\d*\.?\d+px/g) || raw.match(/-?\d*\.?\d+/g) || [];
  var x = nums.length > 0 ? asNumber(String(nums[0]).replace('px',''), 0) : 0;
  var y = nums.length > 1 ? asNumber(String(nums[1]).replace('px',''), 2) : 2;
  var blur = nums.length > 2 ? asNumber(String(nums[2]).replace('px',''), 8) : 8;
  var spread = nums.length > 3 ? asNumber(String(nums[3]).replace('px',''), 0) : 0;
  return { color: color, opacity: opacity, x: x, y: y, blur: blur, spread: spread };
}

function setBoxShadow(shape, boxShadow) {
  if (!shape || !boxShadow) return false;
  var parsed = parseCssBoxShadow(boxShadow);
  if (!parsed) return false;
  var variants = [
    [{ color: parsed.color, opacity: parsed.opacity, offsetX: parsed.x, offsetY: parsed.y, blur: parsed.blur, spread: parsed.spread }],
    [{ shadowColor: parsed.color, shadowOpacity: parsed.opacity, shadowOffsetX: parsed.x, shadowOffsetY: parsed.y, shadowBlur: parsed.blur, shadowSpread: parsed.spread }],
    [{ type: "drop-shadow", color: parsed.color, opacity: parsed.opacity, x: parsed.x, y: parsed.y, blur: parsed.blur, spread: parsed.spread }]
  ];
  var ok = false;
  for (var i = 0; i < variants.length; i++) {
    ok = trySet(shape, "shadows", variants[i]) || ok;
    ok = trySet(shape, "shadow", variants[i][0]) || ok;
    if (ok) break;
  }
  return ok;
}

function normalizeTextAlign(value) {
  var v = String(value || "left").toLowerCase();
  if (v === "start") return "left";
  if (v === "end") return "right";
  if (v === "middle") return "center";
  return v;
}

function normalizeFontFamily(value) {
  var v = String(value || "").trim();
  if (!v) return "";
  var lower = v.toLowerCase();
  // v06.13: Material Symbols are icon-font ligatures. Do not strip the
  // family, otherwise Penpot renders words such as "person" or
  // "arrow_forward" as normal text.
  if (lower.indexOf("material symbols") >= 0) return "Material Symbols Outlined";
  if (lower.indexOf("material icons") >= 0) return "Material Icons";
  if (lower.indexOf("system-ui") >= 0 || lower.indexOf("sans-serif") >= 0) return "";
  // v06.13: try to preserve the source family. If Penpot cannot resolve it, the
  // readback/report will record the fallback explicitly instead of hiding it.
  return v;
}


function normalizeFontWeight(value) {
  if (value === undefined || value === null || value === "") return "400";
  var raw = String(value).trim().toLowerCase();
  var named = {
    thin: 100, hairline: 100, extralight: 200, "extra-light": 200, ultralight: 200,
    light: 300, regular: 400, normal: 400, book: 400, medium: 500,
    semibold: 600, "semi-bold": 600, demibold: 600, "demi-bold": 600,
    bold: 700, extrabold: 800, "extra-bold": 800, ultrabold: 800, black: 900, heavy: 900
  };
  var numeric = Number(raw.replace(/[^0-9.]/g, ""));
  if (!Number.isFinite(numeric) || numeric <= 0) numeric = named[raw] || 400;
  numeric = Math.round(numeric / 100) * 100;
  if (numeric < 100) numeric = 100;
  if (numeric > 900) numeric = 900;
  return String(numeric);
}

function fontStyleNameFromWeight(value) {
  var w = Number(normalizeFontWeight(value));
  if (w >= 900) return "Black";
  if (w >= 800) return "Extra Bold";
  if (w >= 700) return "Bold";
  if (w >= 600) return "Semi Bold";
  if (w >= 500) return "Medium";
  if (w <= 300) return "Light";
  return "Regular";
}

function applyDynamicFontWeight(shape, weight) {
  if (!shape) return false;
  var normalized = normalizeFontWeight(weight);
  var numeric = Number(normalized);
  var styleName = fontStyleNameFromWeight(normalized);
  var ok = false;
  // Penpot builds differ: some honor numeric fontWeight, others switch by style.
  // We never force "normal" here because that resets Inter 700 back to 400.
  var variants = [numeric, normalized, styleName];
  var props = ["fontWeight", "font-weight", "weight", "fontVariantWeight"];
  for (var pi = 0; pi < props.length; pi++) {
    for (var vi = 0; vi < variants.length; vi++) {
      ok = trySet(shape, props[pi], variants[vi]) || ok;
    }
  }
  var styleProps = ["fontStyle", "font-style", "fontVariant", "fontVariantName", "fontFace", "fontStyleName"];
  for (var si = 0; si < styleProps.length; si++) {
    ok = trySet(shape, styleProps[si], styleName) || ok;
  }
  if (typeof shape.setFontWeight === "function") {
    try { shape.setFontWeight(numeric); ok = true; } catch (err1) {
      try { shape.setFontWeight(normalized); ok = true; } catch (err2) {}
    }
  }
  if (typeof shape.setFontStyle === "function") {
    try { shape.setFontStyle(styleName); ok = true; } catch (err3) {}
  }
  if (typeof shape.setFontVariant === "function") {
    try { shape.setFontVariant(styleName); ok = true; } catch (err4) {}
  }
  setPluginDataSafe(shape, "dvcp_font_weight", normalized);
  setPluginDataSafe(shape, "dvcp_font_style", styleName);
  return ok;
}

function sourceLineHeightPxFromOp(op, fontSize) {
  op = op || {};
  var sourcePx = asNumber(op.source_line_height_px, 0);
  if (sourcePx > 0) return sourcePx;
  var raw = asNumber(op.line_height, 0);
  if (raw > 4) return raw;
  if (raw > 0) return raw * Math.max(fontSize || 14, 1);
  return Math.max(fontSize || 14, 1) * 1.2;
}

function penpotLineHeightRatioFromOp(op, fontSize) {
  op = op || {};
  var ratio = asNumber(op.penpot_line_height_ratio, 0);
  if (ratio > 0 && ratio <= 4) return Math.max(1, Math.min(2.4, ratio));
  var raw = asNumber(op.line_height, 0);
  if (raw > 0 && raw <= 4) return Math.max(1, Math.min(2.4, raw));
  var sourcePx = sourceLineHeightPxFromOp(op, fontSize);
  return Math.max(1, Math.min(2.4, sourcePx / Math.max(fontSize || 14, 1)));
}

function setTextFill(shape, color, opacity) {
  if (!shape) return false;
  var c = color === undefined || color === null ? "#0F172A" : String(color);
  if (isTransparentColor(c)) c = "#0F172A";
  var o = asNumber(opacity, 1);
  if (o < 0) o = 0;
  if (o > 1) o = 1;
  var fillVariants = [
    [{ fillColor: c, fillOpacity: o }],
    [{ color: c, opacity: o }],
    [{ type: "solid", color: c, opacity: o }],
    [{ type: "solid", fillColor: c, fillOpacity: o }]
  ];
  var ok = false;
  for (var vi = 0; vi < fillVariants.length; vi++) {
    try { shape.fills = fillVariants[vi]; ok = true; break; } catch (err0) {}
  }
  ok = trySet(shape, "fillColor", c) || ok;
  ok = trySet(shape, "fontColor", c) || ok;
  ok = trySet(shape, "textColor", c) || ok;
  ok = trySet(shape, "textFill", c) || ok;
  ok = trySet(shape, "color", c) || ok;
  ok = trySet(shape, "opacity", o) || ok;
  if (typeof shape.setFills === "function") {
    for (var vf = 0; vf < fillVariants.length; vf++) {
      try { shape.setFills(fillVariants[vf]); ok = true; break; } catch (err1) {}
    }
  }
  if (typeof shape.setFillColor === "function") {
    try { shape.setFillColor(c); ok = true; } catch (err2) {}
  }
  if (typeof shape.setTextColor === "function") {
    try { shape.setTextColor(c); ok = true; } catch (err3) {}
  }
  return ok;
}

function setLayerIndexData(shape, op, localRank) {
  if (!shape) return;
  var z = asNumber(op && op.z_index, 50000) + asNumber(localRank, 0);
  setPluginDataSafe(shape, "z_index", String(z));
  setPluginDataSafe(shape, "source_order", String(op && op.source_order !== undefined ? op.source_order : op && op.op_index !== undefined ? op.op_index : 0));
}

function setText(shape, value) {
  if (!shape) return false;
  var text = String(value || "").slice(0, 1000);
  var ok = false;
  ok = trySet(shape, "text", text) || ok;
  ok = trySet(shape, "characters", text) || ok;
  ok = trySet(shape, "content", text) || ok;
  ok = trySet(shape, "value", text) || ok;
  ok = trySet(shape, "plainText", text) || ok;
  if (typeof shape.setText === "function") {
    try { shape.setText(text); ok = true; } catch (err1) {}
  }
  if (typeof shape.setCharacters === "function") {
    try { shape.setCharacters(text); ok = true; } catch (err2) {}
  }
  return ok;
}

function textNoWrapFromOp(op) {
  op = op || {};
  if (op.text_no_wrap === true || String(op.text_no_wrap).toLowerCase() === "true") return true;
  if (op.no_wrap === true || String(op.no_wrap).toLowerCase() === "true") return true;
  var expectedLineCount = asNumber(op.expected_line_count || op.source_line_count, 0);
  return expectedLineCount > 0 && expectedLineCount <= 1.15;
}

function textGrowTypeFromOp(op) {
  op = op || {};
  var explicit = String(op.penpot_grow_type || op.growType || op.grow_type || "").trim();
  if (explicit) return explicit;
  var material = isMaterialSymbolOp(op);
  // v06.13.6: Stitch rendered line count is the source of truth. Any
  // non-icon text that Chromium rendered as one visual line must stay no-wrap
  // in Penpot, including centered headings and CTA labels. Material Symbols
  // stay fixed because they are glyph icons, not editable prose labels.
  if (!material && textNoWrapFromOp(op)) return "auto-width";
  return "fixed";
}

function setTextStyle(shape, op) {
  if (!shape) return false;
  var ok = false;
  op = op || {};
  var color = op.color || op.text_color || "#0F172A";
  var fontSize = Math.max(asNumber(op.font_size, 14), 1);
  var fontWeight = normalizeFontWeight(op.font_weight || op.weight || "400");
  var align = normalizeTextAlign(op.text_align || "left");
  var sourceLineHeightPx = sourceLineHeightPxFromOp(op, fontSize);
  var lineHeightRatio = penpotLineHeightRatioFromOp(op, fontSize);
  var growType = textGrowTypeFromOp(op);

  // Text layers in Penpot MCP are sensitive to a few properties. Set the same
  // semantic value through several known API/property names so the layer is
  // visible both in canvas and export_shape.
  ok = trySet(shape, "growType", growType) || ok;
  ok = trySet(shape, "verticalAlign", "top") || ok;
  ok = trySet(shape, "fontSize", String(fontSize)) || ok;
  ok = trySet(shape, "font-size", String(fontSize)) || ok;
  var normalizedFamily = normalizeFontFamily(op.font_family);
  if (normalizedFamily) ok = trySet(shape, "fontFamily", normalizedFamily) || ok;
  ok = applyDynamicFontWeight(shape, fontWeight) || ok;
  ok = trySet(shape, "textAlign", align) || ok;
  ok = trySet(shape, "align", align) || ok;
  ok = trySet(shape, "lineHeight", String(lineHeightRatio)) || ok;
  ok = trySet(shape, "line-height", String(lineHeightRatio)) || ok;
  ok = trySet(shape, "sourceLineHeightPx", String(sourceLineHeightPx)) || ok;
  setTextFill(shape, color, op.opacity === undefined ? 1 : op.opacity);

  if (typeof shape.setFontSize === "function") {
    try { shape.setFontSize(String(fontSize)); ok = true; } catch (err1) {
      try { shape.setFontSize(fontSize); ok = true; } catch (err2) {}
    }
  }
  if (typeof shape.setFontFamily === "function" && normalizedFamily) {
    try { shape.setFontFamily(normalizedFamily); ok = true; } catch (err3) {}
  }
  // Apply weight and grow type after family/size/text mutations. Some Penpot
  // runtimes reset the selected style to Regular or fixed box after family is
  // applied.
  ok = applyDynamicFontWeight(shape, fontWeight) || ok;
  ok = trySet(shape, "growType", growType) || ok;
  return ok;
}


function createBoardRoot(name, bbox, fill, stroke, strokeWidth, radius, offset, opacity) {
  var shape = null;
  var attempts = [
    function () { return penpot.createBoard ? penpot.createBoard() : null; },
    function () { return penpot.currentPage && penpot.currentPage.createBoard ? penpot.currentPage.createBoard() : null; }
  ];
  for (var i = 0; i < attempts.length && !shape; i++) {
    try { shape = attempts[i](); } catch (err) { shape = null; }
  }
  if (!shape) {
    // Fallback for older MCP builds: use a rectangle, but export will only include children if they can be grouped/appended.
    return createRectangle(name, bbox, fill, stroke, strokeWidth, radius, offset, opacity);
  }
  setName(shape, name || "ImportedScreen");
  setPositionAndSize(shape, bbox || {}, offset || {x:0, y:0});
  setFill(shape, valueOrDefault(fill, "#FFFFFF"), opacity === undefined ? 1 : opacity);
  setStroke(shape, stroke || null, strokeWidth || 0);
  if (radius !== undefined && radius !== null) setRadius(shape, radius);
  setOpacity(shape, opacity);
  return shape;
}

function createRectangle(name, bbox, fill, stroke, strokeWidth, radius, offset, opacity) {
  var shape = null;
  bbox = bbox || {};
  offset = offset || { x: 0, y: 0 };
  var x = asNumber(bbox.x, 0) + asNumber(offset.x, 0);
  var y = asNumber(bbox.y, 0) + asNumber(offset.y, 0);
  var w = Math.max(asNumber(bbox.width, 100), 1);
  var h = Math.max(asNumber(bbox.height, 40), 1);
  var attempts = [
    function () { return penpot.createRectangle ? penpot.createRectangle(x, y, w, h) : null; },
    function () { return penpot.createRect ? penpot.createRect(x, y, w, h) : null; },
    function () { return penpot.createShape ? penpot.createShape("rectangle") : null; },
    function () { return penpot.createRectangle ? penpot.createRectangle() : null; },
    function () { return penpot.currentPage && penpot.currentPage.createRectangle ? penpot.currentPage.createRectangle(x, y, w, h) : null; }
  ];
  for (var i = 0; i < attempts.length && !shape; i++) {
    try { shape = attempts[i](); } catch (err) { shape = null; }
  }
  if (!shape) return null;
  setName(shape, name || "Rectangle");
  setPositionAndSize(shape, bbox || {}, offset || {x:0, y:0});
  setFill(shape, valueOrDefault(fill, "#FFFFFF"), opacity === undefined ? 1 : opacity);
  setStroke(shape, stroke || null, strokeWidth || 0);
  if (radius !== undefined && radius !== null) setRadius(shape, radius);
  setOpacity(shape, opacity);
  return shape;
}

function createText(name, text, bbox, color, fontSize, offset, style) {
  var shape = null;
  var value = String(text || name || "Text").slice(0, 1000);
  var attempts = [
    function () { return penpot.createText(value); },
    function () { return penpot.createText(); },
    function () { return penpot.createShape ? penpot.createShape("text") : null; },
    function () { return penpot.currentPage && penpot.currentPage.createText ? penpot.currentPage.createText(value) : null; }
  ];
  for (var i = 0; i < attempts.length && !shape; i++) {
    try { shape = attempts[i](); } catch (err) { shape = null; }
  }
  if (!shape) return null;
  setName(shape, name || "Text");
  setText(shape, value);
  style = style || {};
  trySet(shape, "growType", textGrowTypeFromOp(style));
  style.color = color || style.color || "#0F172A";
  style.font_size = fontSize || style.font_size || 14;
  if (style.source_line_height_px === undefined && style.line_height !== undefined && asNumber(style.line_height, 0) > 4) style.source_line_height_px = style.line_height;
  if (style.penpot_line_height_ratio === undefined) style.penpot_line_height_ratio = penpotLineHeightRatioFromOp(style, style.font_size);
  // Apply text style before and after positioning. Some Penpot builds move the
  // glyph baseline when line-height/growType changes; the second position write
  // keeps the selectable box and rendered glyph in the same coordinate space.
  setTextStyle(shape, style);
  setPositionAndSize(shape, bbox || {}, offset || {x:0, y:0});
  setTextStyle(shape, style);
  setPositionAndSize(shape, bbox || {}, offset || {x:0, y:0});
  return shape;
}


function isMaterialSymbolOp(op) {
  op = op || {};
  var fam = String(op.font_family || op.source_font_family || "").toLowerCase();
  var cls = String(op.css_class || "").toLowerCase();
  return !!op.is_material_symbol || fam.indexOf("material symbols") >= 0 || fam.indexOf("material icons") >= 0 || cls.indexOf("material-symbol") >= 0;
}

function materialSymbolTextFromOp(op) {
  op = op || {};
  var raw = String(op.material_symbol_name || op.text || "").trim();
  if (raw) return raw;
  return iconTextFromOp(Object.assign({}, op, { is_material_symbol: false, font_family: "" }));
}

function iconTextFromOp(op) {
  op = op || {};
  var raw = String(op.text || "").trim();
  var nameRole = String((op.name || "") + " " + (op.role || "") + " " + (op.slot || "")).toLowerCase();
  if (!raw) {
    if (nameRole.indexOf("arrow") >= 0 || nameRole.indexOf("next") >= 0) raw = "arrow_forward";
    else if (nameRole.indexOf("shield") >= 0 || nameRole.indexOf("secure") >= 0 || nameRole.indexOf("security") >= 0) raw = "shield";
    else if (nameRole.indexOf("lock") >= 0 || nameRole.indexOf("password") >= 0) raw = "lock";
    else if (nameRole.indexOf("person") >= 0 || nameRole.indexOf("user") >= 0 || nameRole.indexOf("email") >= 0) raw = "person";
    else if (nameRole.indexOf("search") >= 0) raw = "search";
    else if (nameRole.indexOf("menu") >= 0) raw = "menu";
    else if (nameRole.indexOf("close") >= 0) raw = "close";
    else raw = "dot";
  }
  var r = String(raw).toLowerCase();
  if (isMaterialSymbolOp(op)) return raw;
  // When the source does not expose an icon-font glyph, use a Unicode fallback.
  // Real rendered Material Symbols take the raw ligature path above.
  if (r === "arrow_forward" || r === "arrow" || r === "chevron_right") return "→";
  if (r.indexOf("shield") >= 0) return "◇";
  if (r.indexOf("lock") >= 0) return "▣";
  if (r.indexOf("person") >= 0 || r.indexOf("user") >= 0) return "○";
  if (r.indexOf("search") >= 0) return "⌕";
  if (r.indexOf("menu") >= 0) return "☰";
  if (r.indexOf("close") >= 0) return "×";
  if (r === "dot" || r === "•") return "•";
  return raw;
}

function createIconShapes(op, offset) {
  op = op || {};
  var created = [];
  var b = op.bbox || {};
  var w = Math.max(asNumber(b.width, 20), 1);
  var h = Math.max(asNumber(b.height, 20), 1);
  var x = asNumber(b.x, 0);
  var y = asNumber(b.y, 0);
  var roleText = String((op.role || "") + " " + (op.component_type || "") + " " + (op.name || "")).toLowerCase();
  var fillLooksSet = !isTransparentColor(op.fill) && String(op.fill || "").trim() && String(op.fill || "none").toLowerCase() !== "none";
  // For small input/header icons, `fill` means glyph color. For large hero/avatar
  // icons, it may be the background tile color. This keeps the rule generic.
  var hasBackground = fillLooksSet && (roleText.indexOf("hero") >= 0 || roleText.indexOf("avatar") >= 0 || roleText.indexOf("badge") >= 0 || (w >= 36 && h >= 36 && !op.color));
  var iconColor = op.color || (hasBackground ? "#FFFFFF" : (fillLooksSet ? op.fill : "#475569"));
  var fontSize = Math.max(asNumber(op.font_size, Math.min(w, h)), 10);

  if (hasBackground) {
    var bg = createRectangle(
      String(op.name || "Icon") + "_Bg",
      b,
      op.fill,
      op.stroke || null,
      op.stroke_width || 0,
      op.radius === undefined ? Math.min(w, h) / 4 : op.radius,
      offset,
      op.opacity
    );
    if (bg) created.push(bg);
  }

  var glyphBox = {
    x: x + Math.max((w - fontSize) / 2, 0),
    y: y + Math.max((h - fontSize) / 2, 0),
    width: Math.max(fontSize + 2, w),
    height: Math.max(fontSize + 2, h)
  };
  if (!hasBackground) {
    glyphBox = b;
  }
  var materialSymbol = isMaterialSymbolOp(op);
  var style = textStyleFromOp(Object.assign({}, op, {
    color: iconColor,
    font_size: fontSize,
    font_weight: normalizeFontWeight(op.font_weight || op.weight || "400"),
    font_family: materialSymbol ? (op.font_family || op.source_font_family || "Material Symbols Outlined") : "",
    source_font_family: materialSymbol ? (op.source_font_family || op.font_family || "Material Symbols Outlined") : (op.source_font_family || op.font_family || ""),
    text_align: op.text_align || "center",
    // Exact fidelity: Material Symbols still use the browser-computed line-height.
    // Do not force 1; Stitch may compute values such as 40px / 36px = 1.111.
    line_height: op.line_height !== undefined ? op.line_height : (materialSymbol ? null : fontSize + 2),
    source_line_height_px: op.source_line_height_px !== undefined ? op.source_line_height_px : (materialSymbol ? null : fontSize + 2),
    penpot_line_height_ratio: op.penpot_line_height_ratio !== undefined ? op.penpot_line_height_ratio : null,
    opacity: op.opacity === undefined ? 1 : op.opacity
  }));
  var glyphText = materialSymbol ? materialSymbolTextFromOp(op) : iconTextFromOp(op);
  var glyph = createText(String(op.name || "Icon") + "_Glyph", glyphText, glyphBox, iconColor, fontSize, offset, style);
  if (glyph && materialSymbol) {
    setPluginDataSafe(glyph, "is_material_symbol", "true");
    setPluginDataSafe(glyph, "material_symbol_name", glyphText);
    // Re-apply the symbol font after text/weight/position writes; Penpot may
    // reset the family when the glyph characters are assigned.
    trySet(glyph, "fontFamily", normalizeFontFamily(style.font_family));
    if (typeof glyph.setFontFamily === "function") {
      try { glyph.setFontFamily(normalizeFontFamily(style.font_family)); } catch (errMs) {}
    }
  }
  if (glyph) created.push(glyph);
  return created;
}


function nativeVisualInfo(op, method, created, error) {
  op = op || {};
  created = created || [];
  var expectedFill = !isTransparentColor(op.fill);
  var expectedText = !!op.text;
  return {
    schema: "dvcp.visual_materialization.v1",
    version: "v06.13.6",
    method: method || "native_first",
    materialization_kind: "native_penpot_shapes",
    visually_materialized: created.length > 0,
    fallback_used: false,
    expected_text: expectedText,
    expected_fill: expectedFill,
    native_created_shape_count: created.length,
    native_shape_ids: created.map(function (shape) { return getShapeId(shape); }),
    error: error || (created.length > 0 ? null : "native_created_no_shapes")
  };
}

function shouldEmbedTextInComposite(op) {
  if (!op || !op.text) return false;
  var slot = String(op.slot || "").toLowerCase();
  if (slot === "container" || slot === "source_element") return false;
  return true;
}

function createNativeMaterializedShapes(op, offset) {
  op = op || {};
  offset = offset || { x: 0, y: 0 };
  var created = [];
  var error = null;
  try {
    if (op.op === "create_rect") {
      var rect = createRectangle(op.name, op.bbox, valueOrDefault(op.fill, "#FFFFFF"), op.stroke || null, op.stroke_width || 0, op.radius || 0, offset, op.opacity);
      if (rect) { setBoxShadow(rect, op.box_shadow); created.push(rect); }
    } else if (op.op === "create_text") {
      var text = createText(op.name, op.text || op.name || "Text", op.bbox, op.color || op.text_color || "#0F172A", op.font_size || 14, offset, textStyleFromOp(op));
      if (text) created.push(text);
    } else if (op.op === "create_icon") {
      var icons = createIconShapes(op, offset);
      for (var ii = 0; ii < icons.length; ii++) created.push(icons[ii]);
    } else if (op.op === "create_input") {
      var inputRect = createRectangle(
        op.name || "Input",
        op.bbox || {},
        valueOrDefault(op.fill, "#FFFFFF"),
        op.stroke || "#CBD5E1",
        op.stroke_width === undefined ? 1 : op.stroke_width,
        op.radius === undefined ? 12 : op.radius,
        offset,
        op.opacity
      );
      if (inputRect) { setBoxShadow(inputRect, op.box_shadow); created.push(inputRect); }
      if (shouldEmbedTextInComposite(op)) {
        var ib = op.bbox || {};
        var inputText = createText(
          String(op.name || "Input") + "_Text",
          op.text || "",
          {
            x: asNumber(ib.x, 0) + 12,
            y: asNumber(ib.y, 0) + Math.max((asNumber(ib.height, 44) - 20) / 2, 4),
            width: Math.max(asNumber(ib.width, 240) - 24, 40),
            height: Math.max(Math.min(asNumber(ib.height, 44), 24), 12)
          },
          op.color || "#64748B",
          Math.max(asNumber(op.font_size, 14), 10),
          offset,
          textStyleFromOp(op)
        );
        if (inputText) created.push(inputText);
      }
    } else if (op.op === "create_button") {
      var bb = op.bbox || {};
      var primary = String(op.role || "").indexOf("primary") >= 0;
      var secondaryTextOnly = !primary && isTransparentColor(op.fill) && (!op.stroke || asNumber(op.stroke_width, 0) <= 0 || isTransparentColor(op.stroke));
      var buttonFill = valueOrDefault(op.fill, primary ? "#2563EB" : "#FFFFFF");
      var buttonTextColor = op.color || (primary ? "#FFFFFF" : "#0F172A");
      if (!secondaryTextOnly) {
        var buttonRect = createRectangle(
          op.name || "Button",
          bb,
          buttonFill,
          op.stroke || (primary ? null : "#CBD5E1"),
          op.stroke_width === undefined ? (primary ? 0 : 1) : op.stroke_width,
          op.radius === undefined ? 12 : op.radius,
          offset,
          op.opacity
        );
        if (buttonRect) { setBoxShadow(buttonRect, op.box_shadow); created.push(buttonRect); }
      }
      if (shouldEmbedTextInComposite(op)) {
        var textBox = secondaryTextOnly ? bb : {
          x: asNumber(bb.x, 0) + 12,
          y: asNumber(bb.y, 0) + Math.max((asNumber(bb.height, 48) - 22) / 2, 4),
          width: Math.max(asNumber(bb.width, 120) - 24, 40),
          height: Math.max(Math.min(asNumber(bb.height, 48), 26), 12)
        };
        var buttonText = createText(
          String(op.name || "Button") + "_Text",
          op.text || "",
          textBox,
          buttonTextColor,
          op.font_size || 15,
          offset,
          textStyleFromOp(Object.assign({}, op, { color: buttonTextColor, text_align: op.text_align || "center" }))
        );
        if (buttonText) created.push(buttonText);
      }
    } else if (op.op === "create_card") {
      var card = createRectangle(
        op.name || "Card",
        op.bbox || {},
        valueOrDefault(op.fill, null),
        op.stroke || null,
        op.stroke_width || 0,
        op.radius || 0,
        offset,
        op.opacity
      );
      if (card) { setBoxShadow(card, op.box_shadow); created.push(card); }
      if (shouldEmbedTextInComposite(op)) {
        var cb = op.bbox || {};
        var cardText = createText(
          String(op.name || "Card") + "_Text",
          op.text,
          {
            x: asNumber(cb.x, 0) + 12,
            y: asNumber(cb.y, 0) + 12,
            width: Math.max(asNumber(cb.width, 240) - 24, 80),
            height: Math.max(asNumber(cb.height, 80) - 24, 24)
          },
          op.color || "#0F172A",
          op.font_size || 14,
          offset,
          textStyleFromOp(op)
        );
        if (cardText) created.push(cardText);
      }
    }
  } catch (err) {
    error = String(err && err.message ? err.message : err).slice(0, 240);
  }
  return { shapes: created, info: nativeVisualInfo(op, "native_first", created, error) };
}

function createSvg(name, svg, bbox, offset) {
  var shape = null;
  try { shape = penpot.createShapeFromSvg(String(svg || "")); } catch (err) { shape = null; }
  if (!shape) return null;
  setName(shape, name || "SVG");
  setPositionAndSize(shape, bbox || {}, offset || {x:0, y:0});
  return shape;
}

function textStyleFromOp(op) {
  return {
    color: op.color || op.text_color || "#0F172A",
    font_size: op.font_size || 14,
    font_weight: normalizeFontWeight(op.font_weight || op.weight || "400"),
    font_family: op.font_family || null,
    source_font_family: op.source_font_family || op.font_family || null,
    line_height: op.line_height || null,
    source_line_height_px: op.source_line_height_px || null,
    penpot_line_height_ratio: op.penpot_line_height_ratio || null,
    text_align: op.text_align || "left",
    opacity: op.opacity === undefined ? 1 : op.opacity,
    text_no_wrap: op.text_no_wrap,
    expected_line_count: op.expected_line_count,
    source_line_count: op.source_line_count,
    penpot_grow_type: op.penpot_grow_type
  };
}


function shapeKindRank(shape) {
  var role = String(getPluginDataSafe(shape, "semantic_role") || "").toLowerCase();
  var kind = String(getPluginDataSafe(shape, "kind") || shape.type || "").toLowerCase();
  var z = asNumber(getPluginDataSafe(shape, "z_index"), NaN);
  if (Number.isFinite(z)) return z;
  if (role === "screen_root") return 0;
  if (kind.indexOf("rectangle") >= 0 || kind === "surface" || kind === "card" || role.indexOf("surface") >= 0 || role.indexOf("card") >= 0) return 10000;
  if (role.indexOf("input") >= 0 || role.indexOf("checkbox") >= 0 || role.indexOf("control") >= 0) return 30000;
  if (role.indexOf("button") >= 0) return 40000;
  if (role.indexOf("icon") >= 0) return 70000;
  if (kind.indexOf("text") >= 0 || role.indexOf("text") >= 0 || role === "label" || role === "link" || role === "placeholder" || role === "heading") return 90000;
  return 50000;
}

function importedShapesForJob(jobId) {
  var shapes = allPageShapes();
  var out = [];
  for (var i = 0; i < shapes.length; i++) {
    var s = shapes[i];
    if (getPluginDataSafe(s, "source") === "stitch" && getPluginDataSafe(s, "import_job") === String(jobId)) {
      out.push(s);
    }
  }
  return out;
}

function finalizeImportJob(op) {
  var jobId = op.job_id || "";
  var root = findRootForJob(jobId, op.root_name || op.name);
  var shapes = importedShapesForJob(jobId);
  var restacked = 0;
  var textTouched = 0;
  var errors = [];
  var componentCounts = {};
  var attachmentCounts = {};
  if (!root) {
    return {
      all_applied: false,
      action: "dvcp_finalize_import_job",
      import_strategy: "queue_execute_code",
      job_id: jobId,
      op_index: op.op_index,
      op_total: op.op_total,
      op: op.op,
      name: op.name || null,
      checked_count: 1,
      applied_count: 0,
      failed_count: 1,
      created_shape_count: 0,
      created_shape_ids: [],
      root_container_id: null,
      appended_to_root: false,
      error: "root_not_found_for_finalize",
      scanned_shape_count: shapes.length,
      __dvcp_result_marker: "penpot_apply_import_op"
    };
  }

  shapes.sort(function (a, b) {
    var za = shapeKindRank(a), zb = shapeKindRank(b);
    if (za !== zb) return za - zb;
    var ay = asNumber(a.y, 0), by = asNumber(b.y, 0);
    if (ay !== by) return ay - by;
    return asNumber(a.x, 0) - asNumber(b.x, 0);
  });

  for (var i = 0; i < shapes.length; i++) {
    var shape = shapes[i];
    if (shape === root) continue;
    try {
      if (appendToRootIfPossible(root, shape)) restacked++;
    } catch (err1) { errors.push(String(err1 && err1.message ? err1.message : err1).slice(0, 160)); }
    var ctype = getPluginDataSafe(shape, "component_type") || "unknown";
    componentCounts[ctype] = (componentCounts[ctype] || 0) + 1;
    var slot = getPluginDataSafe(shape, "slot") || "";
    if (slot) attachmentCounts[slot] = (attachmentCounts[slot] || 0) + 1;
    // Re-assert text visibility after all surfaces have been appended.
    var type = String(shape.type || "").toLowerCase();
    var kind = String(getPluginDataSafe(shape, "kind") || "").toLowerCase();
    var role = String(getPluginDataSafe(shape, "semantic_role") || "").toLowerCase();
    if (type.indexOf("text") >= 0 || kind === "text" || kind === "icon" || role.indexOf("text") >= 0 || role === "label" || role === "link" || role === "placeholder" || role.indexOf("icon") >= 0) {
      var color = getPluginDataSafe(shape, "dvcp_color") || shape.color || shape.textColor || "#0F172A";
      var storedWeight = getPluginDataSafe(shape, "dvcp_font_weight") || shape.fontWeight || "400";
      setTextFill(shape, color, shape.opacity === undefined ? 1 : shape.opacity);
      applyDynamicFontWeight(shape, storedWeight);
      var storedGrow = getPluginDataSafe(shape, "dvcp_grow_type") || shape.growType || "fixed";
      trySet(shape, "growType", storedGrow);
      textTouched++;
    }
  }

  return {
    all_applied: true,
    action: "dvcp_finalize_import_job",
    import_strategy: "queue_execute_code",
    job_id: jobId,
    op_index: op.op_index,
    op_total: op.op_total,
    op: op.op,
    name: op.name || null,
    checked_count: 1,
    applied_count: 1,
    failed_count: 0,
    created_shape_count: 0,
    created_shape_ids: [],
    root_container_id: getShapeId(root),
    appended_to_root: true,
    error: null,
    scanned_shape_count: shapes.length,
    restacked_count: restacked,
    text_touched_count: textTouched,
    component_type_counts: componentCounts,
    attachment_slot_counts: attachmentCounts,
    errors: errors.slice(0, 10),
    __dvcp_result_marker: "penpot_apply_import_op"
  };
}

function applyImportOp(op) {
  op = op || {};
  var offset = op.root_offset || { x: 0, y: 0 };
  var created = [];
  var result = {
    all_applied: false,
    action: "dvcp_apply_import_op",
    import_strategy: "queue_execute_code",
    job_id: op.job_id || null,
    op_index: op.op_index,
    op_total: op.op_total,
    op: op.op,
    name: op.name || null,
    checked_count: 1,
    applied_count: 0,
    failed_count: 1,
    created_shape_count: 0,
    created_shape_ids: [],
    root_container_id: null,
    appended_to_root: false,
    error: null,
    source_trace: buildSourceTraceFromOp(op),
    penpot_readback: [],
    __dvcp_result_marker: "penpot_apply_import_op"
  };

  if (!penpot.currentPage) {
    result.error = "no_current_page";
    return result;
  }

  if (op.op === "finalize_import_job") {
    return finalizeImportJob(op);
  }

  var nativeFirstOps = {
    create_rect: true,
    create_text: true,
    create_icon: true,
    create_input: true,
    create_button: true,
    create_card: true
  };
  if (nativeFirstOps[op.op]) {
    var nativeCreated = createNativeMaterializedShapes(op, offset);
    result.visual_materialization = nativeCreated.info;
    for (var ni = 0; ni < nativeCreated.shapes.length; ni++) created.push(nativeCreated.shapes[ni]);
    if (created.length === 0) {
      var visualCreated = createVisualMaterializedShape(op, offset);
      if (visualCreated.info) {
        visualCreated.info.method = "svg_fallback_after_native_failure";
        visualCreated.info.fallback_used = true;
        visualCreated.info.native_error = nativeCreated.info ? nativeCreated.info.error : null;
      }
      result.visual_materialization = visualCreated.info;
      if (visualCreated.shape) created.push(visualCreated.shape);
    }
  }

  if (created.length === 0) {
  if (op.op === "create_root") {
    var root = createBoardRoot(
      op.name || "ImportedScreen",
      op.bbox || { x: 0, y: 0, width: 390, height: 860 },
      op.fill || "#F8FAFC",
      op.stroke || "#CBD5E1",
      op.stroke_width || 1,
      op.radius || 0,
      { x: 0, y: 0 },
      op.opacity
    );
    if (root) {
      setPluginDataSafe(root, "source", "stitch");
      setPluginDataSafe(root, "semantic_role", "screen_root");
      setPluginDataSafe(root, "import_job", op.job_id || "");
      setPluginDataSafe(root, "external_design_summary", op.spec_summary || {});
      setPluginDataSafe(root, "root_name", op.root_name || op.name || "");
      created.push(root);
    }
  } else if (op.op === "create_rect") {
    var rect = createRectangle(op.name, op.bbox, op.fill || "#FFFFFF", op.stroke || null, op.stroke_width || 0, op.radius || 0, offset, op.opacity);
    if (rect) created.push(rect);
  } else if (op.op === "create_text") {
    var text = createText(op.name, op.text || op.name || "Text", op.bbox, op.color || "#0F172A", op.font_size || 14, offset, textStyleFromOp(op));
    if (text) created.push(text);
  } else if (op.op === "create_icon") {
    var icons = createIconShapes(op, offset);
    for (var ii = 0; ii < icons.length; ii++) created.push(icons[ii]);
  } else if (op.op === "create_input") {
    var b = op.bbox || {};
    var inputRect = createRectangle(
      op.name || "Input",
      b,
      op.fill || "#FFFFFF",
      op.stroke || "#CBD5E1",
      op.stroke_width === undefined ? 1 : op.stroke_width,
      op.radius === undefined ? 12 : op.radius,
      offset,
      op.opacity
    );
    if (inputRect) created.push(inputRect);
    var inputText = createText(
      String(op.name || "Input") + "_Text",
      op.text || "",
      {
        x: asNumber(b.x, 0) + 12,
        y: asNumber(b.y, 0) + Math.max((asNumber(b.height, 44) - 20) / 2, 4),
        width: Math.max(asNumber(b.width, 240) - 24, 40),
        height: Math.max(Math.min(asNumber(b.height, 44), 24), 12)
      },
      op.color || "#64748B",
      Math.max(asNumber(op.font_size, 14), 10),
      offset,
      textStyleFromOp(op)
    );
    if (inputText && op.text) created.push(inputText);
  } else if (op.op === "create_button") {
    var bb = op.bbox || {};
    var primary = String(op.role || "").indexOf("primary") >= 0;
    var secondaryTextOnly = !primary && isTransparentColor(op.fill) && (!op.stroke || asNumber(op.stroke_width, 0) <= 0 || isTransparentColor(op.stroke));
    var buttonFill = op.fill || (primary ? "#2563EB" : "#FFFFFF");
    var buttonTextColor = op.color || (primary ? "#FFFFFF" : "#0F172A");
    if (!secondaryTextOnly) {
      var buttonRect = createRectangle(
        op.name || "Button",
        bb,
        buttonFill,
        op.stroke || (primary ? null : "#CBD5E1"),
        op.stroke_width === undefined ? (primary ? 0 : 1) : op.stroke_width,
        op.radius === undefined ? 12 : op.radius,
        offset,
        op.opacity
      );
      if (buttonRect) created.push(buttonRect);
    } else if (!op.text && (op.allow_no_shape || op.ghost)) {
      // Pure ghost action/hitbox. Do not create a rectangle because selected
      // invisible hitboxes made the imported screen look like a cyan wireframe.
      // The semantic action is represented by its attached text/icon layer.
    }
    var textBox = secondaryTextOnly ? bb : {
      x: asNumber(bb.x, 0) + 12,
      y: asNumber(bb.y, 0) + Math.max((asNumber(bb.height, 48) - 22) / 2, 4),
      width: Math.max(asNumber(bb.width, 120) - 24, 40),
      height: Math.max(Math.min(asNumber(bb.height, 48), 26), 12)
    };
    var buttonText = createText(
      String(op.name || "Button") + "_Text",
      op.text || "",
      textBox,
      buttonTextColor,
      op.font_size || 15,
      offset,
      textStyleFromOp(Object.assign({}, op, { color: buttonTextColor, text_align: op.text_align || "center" }))
    );
    if (buttonText && op.text) created.push(buttonText);
  } else if (op.op === "create_card") {
    var card = createRectangle(
      op.name || "Card",
      op.bbox,
      op.fill || "#FFFFFF",
      op.stroke || null,
      op.stroke_width || 0,
      op.radius || 0,
      offset,
      op.opacity
    );
    if (card) created.push(card);
    if (op.text) {
      var cb = op.bbox || {};
      var cardText = createText(
        String(op.name || "Card") + "_Text",
        op.text,
        {
          x: asNumber(cb.x, 0) + 12,
          y: asNumber(cb.y, 0) + 12,
          width: Math.max(asNumber(cb.width, 240) - 24, 80),
          height: Math.max(asNumber(cb.height, 80) - 24, 24)
        },
        op.color || "#0F172A",
        op.font_size || 14,
        offset,
        textStyleFromOp(op)
      );
      if (cardText) created.push(cardText);
    }
  } else if (op.op === "create_svg") {
    var svgShape = createSvg(op.name || "SVG", op.svg, op.bbox, offset);
    if (svgShape) created.push(svgShape);
  } else {
    result.error = "unknown_op";
    return result;
  }
  }

  if (!result.visual_materialization && op.op !== "create_root" && op.op !== "finalize_import_job") {
    result.visual_materialization = {
      schema: "dvcp.visual_materialization.v1",
      version: "v06.13.6",
      method: "native_fallback_legacy_path",
      visually_materialized: created.length > 0,
      fallback_used: true,
      expected_text: !!op.text,
      expected_fill: !isTransparentColor(op.fill),
      error: created.length > 0 ? null : "native_created_no_shapes"
    };
  }

  var rootForJob = null;
  if (op.op !== "create_root" && op.job_id) {
    rootForJob = findRootForJob(op.job_id, op.root_name);
  }

  for (var ai = 0; ai < created.length; ai++) {
    if (rootForJob) appendToRootIfPossible(rootForJob, created[ai]);
  }

  for (var i = 0; i < created.length; i++) {
    var shape = created[i];
    setPluginDataSafe(shape, "source", "stitch");
    setPluginDataSafe(shape, "import_job", op.job_id || "");
    setPluginDataSafe(shape, "semantic_role", op.role || op.kind || op.op || "unknown");
    setPluginDataSafe(shape, "kind", op.kind || op.op || "unknown");
    setPluginDataSafe(shape, "source_name", op.source_name || op.name || "");
    setPluginDataSafe(shape, "tag", op.tag || "");
    if (op.component_id) setPluginDataSafe(shape, "component_id", op.component_id || "");
    if (op.component_type) setPluginDataSafe(shape, "component_type", op.component_type || "");
    if (op.attach_to) setPluginDataSafe(shape, "attach_to", op.attach_to || "");
    if (op.slot) setPluginDataSafe(shape, "slot", op.slot || "");
    if (op.source_ref) setPluginDataSafe(shape, "source_ref", op.source_ref || "");
    if (op.source_snapshot) setPluginDataSafe(shape, "source_snapshot", op.source_snapshot);
    if (op.source_trace) setPluginDataSafe(shape, "source_trace", op.source_trace);
    setPluginDataSafe(shape, "expected_visual", compactOpVisualValues(op));
    setLayerIndexData(shape, op, i);
    if (op.color || op.text_color) setPluginDataSafe(shape, "dvcp_color", op.color || op.text_color || "");
    if (op.font_weight || op.weight) setPluginDataSafe(shape, "dvcp_font_weight", normalizeFontWeight(op.font_weight || op.weight || "400"));
    if (op.font_family || op.source_font_family) setPluginDataSafe(shape, "source_font_family", op.source_font_family || op.font_family || "");
    if (op.source_line_height_px !== undefined) setPluginDataSafe(shape, "source_line_height_px", String(op.source_line_height_px));
    if (op.penpot_line_height_ratio !== undefined) setPluginDataSafe(shape, "penpot_line_height_ratio", String(op.penpot_line_height_ratio));
    if (op.text_no_wrap !== undefined) setPluginDataSafe(shape, "text_no_wrap", String(op.text_no_wrap));
    if (op.media_alt !== undefined) setPluginDataSafe(shape, "media_alt", String(op.media_alt || ""));
    if (op.expected_line_count !== undefined) setPluginDataSafe(shape, "expected_line_count", String(op.expected_line_count));
    if (op.source_line_count !== undefined) setPluginDataSafe(shape, "source_line_count", String(op.source_line_count));
    var growTypeForShape = (String(shape.type || "").toLowerCase().indexOf("text") >= 0 || String(op.kind || "").toLowerCase() === "text" || String(op.kind || "").toLowerCase() === "icon") ? textGrowTypeFromOp(op) : "";
    if (growTypeForShape) setPluginDataSafe(shape, "dvcp_grow_type", growTypeForShape);
    if (op.text) setPluginDataSafe(shape, "dvcp_text", String(op.text).slice(0, 300));
    setPluginDataSafe(shape, "visual_materialization", "v06_13_6_icon_only_no_label_fidelity");
    if (result.visual_materialization) setPluginDataSafe(shape, "visual_materialization_detail", result.visual_materialization);
    result.created_shape_ids.push(getShapeId(shape));
    result.penpot_readback.push(compactShapeReadback(shape));
  }
  if (result.source_trace) {
    result.source_trace.penpot_shape_ids = result.created_shape_ids.slice();
    result.source_trace.penpot_readback = result.penpot_readback.slice();
  }

  if (rootForJob) {
    result.root_container_id = getShapeId(rootForJob);
    result.appended_to_root = true;
  }

  var allowedNoShape = !!op.allow_no_shape;
  result.applied_count = (created.length > 0 || allowedNoShape) ? 1 : 0;
  result.failed_count = (created.length > 0 || allowedNoShape) ? 0 : 1;
  result.created_shape_count = created.length;
  result.all_applied = created.length > 0 || allowedNoShape;
  result.error = result.all_applied ? null : "op_created_no_shapes";
  if (allowedNoShape && created.length === 0) result.no_shape_allowed = true;

  return result;
}

try {
  var IMPORT_OP = __IMPORT_OP_JSON__;
  return applyImportOp(IMPORT_OP);
} catch (err) {
  return {
    all_applied: false,
    action: "dvcp_apply_import_op",
    import_strategy: "queue_execute_code",
    checked_count: 1,
    applied_count: 0,
    failed_count: 1,
    created_shape_count: 0,
    error: "js_exception",
    message: err && err.message ? String(err.message) : String(err),
    stack: err && err.stack ? String(err.stack).slice(0, 1200) : null,
    __dvcp_result_marker: "penpot_apply_import_op"
  };
}
