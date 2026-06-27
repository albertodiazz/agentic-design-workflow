function toArray(value) {
  if (!value) return [];
  try {
    return Array.from(value);
  } catch (err) {
    return [];
  }
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

function applyName(shape, newName) {
  var before = shape && shape.name ? String(shape.name) : "";
  var error = null;

  try {
    shape.name = newName;
  } catch (err1) {
    error = String(err1 && err1.message ? err1.message : err1);
  }

  if (String(shape.name || "") !== newName && typeof shape.rename === "function") {
    try {
      shape.rename(newName);
      error = null;
    } catch (err2) {
      error = String(err2 && err2.message ? err2.message : err2);
    }
  }

  if (String(shape.name || "") !== newName && typeof shape.setName === "function") {
    try {
      shape.setName(newName);
      error = null;
    } catch (err3) {
      error = String(err3 && err3.message ? err3.message : err3);
    }
  }

  var after = shape && shape.name ? String(shape.name) : "";

  return {
    before_name: before,
    actual_name: after,
    applied: after === newName,
    error: after === newName ? null : error
  };
}

var plan = __RENAME_PLAN_JSON__;
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
  var expectedName = String(item.new_name || "");
  var shape = shapesById[id] || null;

  if (!shape) {
    return {
      action: item.action || "",
      node_ref: item.node_ref || "",
      id: id,
      expected_name: expectedName,
      before_name: null,
      actual_name: null,
      found: false,
      applied: false,
      error: "shape_not_found"
    };
  }

  var applyResult = applyName(shape, expectedName);

  return {
    action: item.action || "",
    node_ref: item.node_ref || "",
    id: id,
    expected_name: expectedName,
    before_name: applyResult.before_name,
    actual_name: applyResult.actual_name,
    actual_type: shape.type ? String(shape.type) : "",
    found: true,
    applied: applyResult.applied,
    error: applyResult.error
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
