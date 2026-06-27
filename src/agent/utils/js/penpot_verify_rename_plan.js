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
    out[String(shape.id)] = {
      id: String(shape.id),
      name: shape.name ? String(shape.name) : "",
      type: shape.type ? String(shape.type) : ""
    };
  }

  var children = toArray(shape.children);
  for (var i = 0; i < children.length; i++) {
    walk(children[i], out);
  }
}

var expected = __EXPECTED_PLAN_JSON__;
var shapesById = {};

var currentPage = penpot.currentPage || null;
var selection = toArray(penpot.selection);
var roots = selection.length > 0
  ? selection
  : (currentPage ? toArray(currentPage.children) : []);

for (var i = 0; i < roots.length; i++) {
  walk(roots[i], shapesById);
}

var results = expected.map(function (item) {
  var id = String(item.id || "");
  var expectedName = String(item.new_name || "");
  var actual = shapesById[id] || null;

  return {
    action: item.action || "",
    node_ref: item.node_ref || "",
    id: id,
    expected_name: expectedName,
    actual_name: actual ? actual.name : null,
    actual_type: actual ? actual.type : null,
    found: actual !== null,
    applied: actual !== null && actual.name === expectedName
  };
});

var appliedCount = results.filter(function (item) {
  return item.applied === true;
}).length;

var allApplied = results.length > 0 && appliedCount === results.length;

return JSON.stringify({
  all_applied: allApplied,
  checked_count: results.length,
  applied_count: appliedCount,
  failed_count: results.length - appliedCount,
  results: results
});
