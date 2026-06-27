function toArray(value) {
  if (!value) return [];
  try {
    return Array.from(value);
  } catch (err) {
    return [];
  }
}

function asNumber(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  return null;
}

function asString(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value);
}

function safePlain(value, maxItems) {
  if (value === null || value === undefined) {
    return null;
  }

  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return value;
  }

  if (Array.isArray(value)) {
    return value.slice(0, maxItems || 8).map(function (item) {
      return safePlain(item, maxItems);
    });
  }

  try {
    var out = {};
    var keys = Object.keys(value).slice(0, maxItems || 12);

    for (var i = 0; i < keys.length; i++) {
      var key = keys[i];
      var item = value[key];

      if (typeof item !== "function") {
        out[key] = safePlain(item, maxItems);
      }
    }

    return out;
  } catch (err) {
    return String(value);
  }
}

function readText(shape) {
  if (!shape) return null;

  if (shape.characters !== undefined && shape.characters !== null) {
    return String(shape.characters);
  }

  if (shape.text !== undefined && shape.text !== null) {
    return String(shape.text);
  }

  if (shape.content !== undefined && shape.content !== null) {
    return String(shape.content);
  }

  return null;
}

function readChildren(shape) {
  if (!shape) return [];

  var candidates = [
    shape.children,
    shape.shapes,
    shape.items
  ];

  for (var i = 0; i < candidates.length; i++) {
    var arr = toArray(candidates[i]);
    if (arr.length > 0) {
      return arr;
    }
  }

  return [];
}

function serializeShape(shape, depth, path) {
  if (!shape || depth > 8) {
    return null;
  }

  var id = asString(shape.id);
  var type = asString(shape.type || shape.shapeType);
  var name = asString(shape.name);
  var label = name || type || id || "unnamed";
  var currentPath = path ? path + " / " + label : label;

  var children = [];
  var rawChildren = readChildren(shape);

  for (var i = 0; i < rawChildren.length; i++) {
    var child = serializeShape(rawChildren[i], depth + 1, currentPath);
    if (child) {
      children.push(child);
    }
  }

  return {
    id: id,
    name: name,
    type: type,
    path: currentPath,

    x: asNumber(shape.x),
    y: asNumber(shape.y),
    width: asNumber(shape.width),
    height: asNumber(shape.height),
    rotation: asNumber(shape.rotation),

    visible: shape.visible !== false && shape.hidden !== true,
    locked: shape.locked === true,

    text: readText(shape),

    fills: safePlain(shape.fills, 6),
    strokes: safePlain(shape.strokes, 6),
    opacity: asNumber(shape.opacity),

    fontFamily: shape.fontFamily ? String(shape.fontFamily) : null,
    fontSize: asNumber(shape.fontSize),
    fontWeight: shape.fontWeight ? String(shape.fontWeight) : null,
    lineHeight: shape.lineHeight ? safePlain(shape.lineHeight, 4) : null,

    componentId: shape.componentId ? String(shape.componentId) : null,
    componentName: shape.component && shape.component.name ? String(shape.component.name) : null,

    children: children
  };
}

var selection = toArray(penpot.selection);
var currentPage = penpot.currentPage || null;
var pageChildren = currentPage ? readChildren(currentPage) : [];
var roots = selection.length > 0 ? selection : pageChildren;

var result = {
  file: {
    id: penpot.currentFile && penpot.currentFile.id ? String(penpot.currentFile.id) : "",
    name: penpot.currentFile && penpot.currentFile.name ? String(penpot.currentFile.name) : ""
  },
  page: {
    id: currentPage && currentPage.id ? String(currentPage.id) : "",
    name: currentPage && currentPage.name ? String(currentPage.name) : ""
  },
  root_source: selection.length > 0 ? "selection" : "current_page_children",
  selection_count: selection.length,
  root_count: roots.length,
  roots: roots.map(function (shape) {
    return serializeShape(shape, 0, "");
  }).filter(Boolean)
};

return JSON.stringify(result);
