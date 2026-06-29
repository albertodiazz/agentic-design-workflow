function toArray(value) {
  if (!value) return [];
  try {
    return Array.from(value);
  } catch (err) {
    return [];
  }
}

function asNumber(value, fallback) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  var parsed = Number(value);
  if (Number.isFinite(parsed)) return parsed;
  return fallback || 0;
}

function walk(shape, out) {
  if (!shape) return;

  if (shape.id) {
    out[String(shape.id)] = shape;
  }

  var children = toArray(shape.children);
  for (var i = 0; i < children.length; i++) {
    walk(children[i], out);
  }
}

function trySet(shape, prop, value) {
  try {
    shape[prop] = value;
    return true;
  } catch (err) {
    return false;
  }
}

function readNum(shape, prop) {
  return asNumber(shape && shape[prop], null);
}

function applyPosition(shape, item) {
  var target = item.target || {};
  var x = asNumber(target.x, readNum(shape, "x") || 0);
  var y = asNumber(target.y, readNum(shape, "y") || 0);
  var errors = [];

  if (!trySet(shape, "x", x)) errors.push("x_assignment_failed");
  if (!trySet(shape, "y", y)) errors.push("y_assignment_failed");

  if ((readNum(shape, "x") !== x || readNum(shape, "y") !== y) && typeof shape.setPosition === "function") {
    try {
      shape.setPosition(x, y);
      errors = [];
    } catch (err1) {
      errors.push(String(err1 && err1.message ? err1.message : err1));
    }
  }

  return {
    applied: readNum(shape, "x") === x && readNum(shape, "y") === y,
    actual: { x: readNum(shape, "x"), y: readNum(shape, "y") },
    expected: { x: x, y: y },
    error: errors.length ? errors.join("; ") : null
  };
}

function applyFontSize(shape, item) {
  var minSize = asNumber(item.min_font_size, 0);
  var before = readNum(shape, "fontSize");
  var target = Math.max(before || 0, minSize);
  var errors = [];

  if (!target) {
    return {
      applied: false,
      actual: { fontSize: before },
      expected: { fontSize: minSize },
      error: "font_size_not_readable"
    };
  }

  if (!trySet(shape, "fontSize", target)) errors.push("fontSize_assignment_failed");

  if (readNum(shape, "fontSize") !== target && typeof shape.setFontSize === "function") {
    try {
      shape.setFontSize(target);
      errors = [];
    } catch (err1) {
      errors.push(String(err1 && err1.message ? err1.message : err1));
    }
  }

  return {
    applied: readNum(shape, "fontSize") >= minSize,
    actual: { fontSize: readNum(shape, "fontSize") },
    expected: { min_font_size: minSize },
    error: errors.length ? errors.join("; ") : null
  };
}

function applyStroke(shape, item) {
  var color = String(item.stroke_color || "#475569");
  var width = asNumber(item.stroke_width, 1);
  var errors = [];

  var strokes = [{ strokeColor: color, color: color, width: width }];

  if (!trySet(shape, "strokes", strokes)) errors.push("strokes_assignment_failed");
  if (shape.strokes === undefined || shape.strokes === null) {
    trySet(shape, "strokeColor", color);
    trySet(shape, "strokeWidth", width);
  }

  var hasStroke = false;
  try {
    hasStroke = !!shape.strokes || !!shape.strokeColor || !!shape.strokeWidth;
  } catch (err) {
    hasStroke = false;
  }

  return {
    applied: hasStroke,
    actual: { has_stroke: hasStroke },
    expected: { stroke_color: color, stroke_width: width },
    error: hasStroke ? null : (errors.length ? errors.join("; ") : "stroke_not_applied")
  };
}

var plan = __CANVAS_FIX_PLAN_JSON__;
var shapesById = {};

var currentPage = penpot.currentPage || null;
var selection = toArray(penpot.selection);
var roots = selection.length > 0
  ? selection
  : (currentPage ? toArray(currentPage.children) : []);

for (var i = 0; i < roots.length; i++) {
  walk(roots[i], shapesById);
}

var results = plan.map(function (item) {
  var id = String(item.id || "");
  var shape = shapesById[id] || null;

  if (!shape) {
    return {
      action: item.action || "",
      node_ref: item.node_ref || "",
      id: id,
      name: item.name || "",
      found: false,
      applied: false,
      error: "shape_not_found"
    };
  }

  var outcome;
  if (item.action === "set_position") {
    outcome = applyPosition(shape, item);
  } else if (item.action === "set_min_font_size") {
    outcome = applyFontSize(shape, item);
  } else if (item.action === "set_stroke") {
    outcome = applyStroke(shape, item);
  } else {
    outcome = {
      applied: false,
      actual: null,
      expected: null,
      error: "unsupported_action"
    };
  }

  return {
    action: item.action || "",
    node_ref: item.node_ref || "",
    id: id,
    name: item.name || (shape.name ? String(shape.name) : ""),
    found: true,
    applied: outcome.applied === true,
    actual: outcome.actual,
    expected: outcome.expected,
    reason: item.reason || "",
    error: outcome.error
  };
});

var appliedCount = results.filter(function (item) {
  return item.applied === true;
}).length;

return JSON.stringify({
  all_applied: results.length > 0 && appliedCount === results.length,
  checked_count: results.length,
  applied_count: appliedCount,
  failed_count: results.length - appliedCount,
  results: results
});
