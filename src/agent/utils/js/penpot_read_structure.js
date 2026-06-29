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


function readLibraryItems(collection, maxItems) {
  var out = [];
  var candidates = [collection, collection && collection.items, collection && collection.values, collection && collection.components, collection && collection.tokens, collection && collection.sets];
  for (var c = 0; c < candidates.length && out.length === 0; c++) {
    var arr = toArray(candidates[c]);
    for (var i = 0; i < arr.length && out.length < (maxItems || 80); i++) {
      var item = arr[i];
      if (!item) continue;
      var entry = {
        id: item.id ? String(item.id) : "",
        name: item.name ? String(item.name) : "",
        path: item.path ? String(item.path) : "",
        type: item.type ? String(item.type) : "",
        value: item.value !== undefined ? safePlain(item.value, 6) : null,
        dvcpStates: item.dvcpStates !== undefined ? safePlain(item.dvcpStates, 12) : null
      };
      try {
        if (typeof item.getPluginData === "function") {
          var states = item.getPluginData("dvcp.states");
          if (states) entry.dvcpStatesSerialized = String(states);
        }
      } catch (errState) {}
      if (entry.name || entry.id || entry.type) out.push(entry);
    }
  }
  return out;
}

function readTokenSets(catalog, maxSets, maxTokens) {
  var sets = [];
  if (!catalog) return sets;

  var candidates = [catalog.sets, catalog.items, catalog.values, catalog];
  for (var c = 0; c < candidates.length && sets.length === 0; c++) {
    var arr = toArray(candidates[c]);
    for (var i = 0; i < arr.length && sets.length < (maxSets || 20); i++) {
      var set = arr[i];
      if (!set) continue;
      var tokens = readLibraryItems(set.tokens || set.items || set.values || set, maxTokens || 80);
      var name = set.name ? String(set.name) : "";
      if (name || tokens.length) {
        sets.push({
          id: set.id ? String(set.id) : "",
          name: name,
          token_count: tokens.length,
          tokens: tokens
        });
      }
    }
  }

  return sets;
}

function readLibrarySummary() {
  var library = null;
  try {
    if (penpot.library && penpot.library.local) library = penpot.library.local;
  } catch (err) {}

  var summary = {
    available: !!library,
    token_sets: [],
    components: [],
    colors: [],
    typographies: []
  };

  if (!library) return summary;

  try { summary.token_sets = readTokenSets(library.tokens, 20, 80); } catch (err1) { summary.token_error = String(err1 && err1.message ? err1.message : err1); }
  try { summary.components = readLibraryItems(library.components, 80); } catch (err2) { summary.components_error = String(err2 && err2.message ? err2.message : err2); }
  try { summary.colors = readLibraryItems(library.colors, 80); } catch (err3) { summary.colors_error = String(err3 && err3.message ? err3.message : err3); }
  try { summary.typographies = readLibraryItems(library.typographies, 80); } catch (err4) { summary.typographies_error = String(err4 && err4.message ? err4.message : err4); }

  summary.token_set_names = summary.token_sets.map(function (set) { return String(set.name || ""); }).filter(Boolean);
  summary.token_names = [];
  for (var tsi = 0; tsi < summary.token_sets.length; tsi++) {
    var tsTokens = summary.token_sets[tsi].tokens || [];
    for (var ti = 0; ti < tsTokens.length; ti++) {
      if (tsTokens[ti] && tsTokens[ti].name) summary.token_names.push(String(tsTokens[ti].name));
    }
  }
  summary.component_names = summary.components.map(function (component) { return String(component.name || ""); }).filter(Boolean);

  return summary;
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
  library: readLibrarySummary(),
  roots: roots.map(function (shape) {
    return serializeShape(shape, 0, "");
  }).filter(Boolean)
};

return JSON.stringify(result);
