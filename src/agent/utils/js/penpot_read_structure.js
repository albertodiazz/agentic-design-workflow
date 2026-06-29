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


function normalizeEvidenceName(value) {
  return String(value || "")
    .replace(/\s*\/\s*/g, "/")
    .replace(/[_\s-]+/g, "")
    .toLowerCase();
}

function readPluginData(item, key) {
  try {
    if (item && typeof item.getPluginData === "function") {
      var value = item.getPluginData(key);
      if (value !== undefined && value !== null && String(value) !== "") return String(value);
    }
  } catch (err) {}
  return "";
}

function parseJsonOrNull(value) {
  try {
    if (!value) return null;
    return JSON.parse(String(value));
  } catch (err) {
    return null;
  }
}

function deriveComponentEvidenceFields(entry) {
  var candidateNames = [entry.full_name, entry.path, entry.name, entry.plugin_full_name].filter(Boolean);
  var joined = candidateNames.join(" /");
  var normalized = normalizeEvidenceName(joined);
  var semanticRole = normalizeEvidenceName(entry.semantic_role || entry.plugin_semantic_role || "");

  entry.normalized_name = normalizeEvidenceName(entry.full_name || entry.path || entry.name);
  entry.evidence_aliases = candidateNames;
  entry.is_text_input = /textinput/.test(normalized) || /textinput/.test(semanticRole) || /input/.test(semanticRole);
  entry.is_button = /button/.test(normalized) || /button/.test(semanticRole);
  entry.is_focus_state = /focus/.test(normalized) || /focus/.test(semanticRole);
  entry.is_hover_state = /hover/.test(normalized) || /hover/.test(semanticRole);
  entry.is_disabled_state = /disabled/.test(normalized) || /disabled/.test(semanticRole);

  return entry;
}

function readLibraryItems(collection, maxItems) {
  var out = [];
  var candidates = [collection, collection && collection.items, collection && collection.values, collection && collection.components, collection && collection.tokens, collection && collection.sets];
  for (var c = 0; c < candidates.length && out.length === 0; c++) {
    var arr = toArray(candidates[c]);
    for (var i = 0; i < arr.length && out.length < (maxItems || 80); i++) {
      var item = arr[i];
      if (!item) continue;

      var pluginFullName = readPluginData(item, "dvcp.full_name");
      var pluginSemanticRole = readPluginData(item, "dvcp.semantic_role");
      var pluginStatesSerialized = readPluginData(item, "dvcp.states");
      var pluginStates = parseJsonOrNull(pluginStatesSerialized);

      var entry = {
        id: item.id ? String(item.id) : "",
        name: item.name ? String(item.name) : "",
        path: item.path ? String(item.path) : "",
        type: item.type ? String(item.type) : "",
        value: item.value !== undefined ? safePlain(item.value, 6) : null,
        plugin_full_name: pluginFullName,
        plugin_semantic_role: pluginSemanticRole,
        full_name: pluginFullName || (item.fullName ? String(item.fullName) : "") || (item.path ? String(item.path) : "") || (item.name ? String(item.name) : ""),
        semantic_role: pluginSemanticRole || (item.dvcpSemanticRole ? String(item.dvcpSemanticRole) : ""),
        dvcpStates: item.dvcpStates !== undefined ? safePlain(item.dvcpStates, 12) : null,
        dvcpStatesSerialized: pluginStatesSerialized,
        dvcpStatesParsed: pluginStates ? safePlain(pluginStates, 20) : null
      };

      deriveComponentEvidenceFields(entry);
      if (entry.name || entry.id || entry.type || entry.full_name) out.push(entry);
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
  summary.component_full_names = summary.components.map(function (component) { return String(component.full_name || component.path || component.name || ""); }).filter(Boolean);
  summary.component_semantic_roles = summary.components.map(function (component) { return String(component.semantic_role || ""); }).filter(Boolean);

  var allComponentText = summary.components.map(function (component) {
    return [component.full_name, component.path, component.name, component.semantic_role, component.dvcpStatesSerialized].join(" /");
  }).join(" /");
  var normalizedComponents = normalizeEvidenceName(allComponentText);
  var normalizedTokens = normalizeEvidenceName(summary.token_names.join(" /"));

  function hasAll(parts, text) {
    for (var i = 0; i < parts.length; i++) {
      if (text.indexOf(normalizeEvidenceName(parts[i])) < 0) return false;
    }
    return true;
  }

  summary.interactive_state_evidence = {
    has_email_input: hasAll(["textinput", "email"], normalizedComponents) || hasAll(["email", "input"], normalizedComponents),
    has_password_input: hasAll(["textinput", "password"], normalizedComponents) || hasAll(["password", "input"], normalizedComponents),
    has_primary_button: hasAll(["button", "primary"], normalizedComponents) || normalizedComponents.indexOf("primary") >= 0,
    has_email_focus: hasAll(["email", "focus"], normalizedComponents),
    has_password_focus: hasAll(["password", "focus"], normalizedComponents),
    has_button_focus: hasAll(["button", "focus"], normalizedComponents) || hasAll(["primary", "focus"], normalizedComponents),
    has_button_hover: hasAll(["button", "hover"], normalizedComponents) || hasAll(["primary", "hover"], normalizedComponents),
    has_button_disabled: hasAll(["button", "disabled"], normalizedComponents) || hasAll(["primary", "disabled"], normalizedComponents),
    has_focus_tokens: normalizedTokens.indexOf("color.focus.ring") >= 0 || normalizedTokens.indexOf("colorfocusring") >= 0,
    has_interactive_color_tokens: (normalizedTokens.indexOf("hover") >= 0 && normalizedTokens.indexOf("disabled") >= 0),
    has_spacing_tokens: normalizedTokens.indexOf("spacing.form.gap") >= 0 || normalizedTokens.indexOf("spacingformgap") >= 0,
    normalized_component_text: normalizedComponents.slice(0, 2000),
    normalized_token_text: normalizedTokens.slice(0, 2000)
  };
  summary.interactive_state_evidence.all_focus_states = summary.interactive_state_evidence.has_email_focus && summary.interactive_state_evidence.has_password_focus && summary.interactive_state_evidence.has_button_focus;
  summary.interactive_state_evidence.all_button_states = summary.interactive_state_evidence.has_button_hover && summary.interactive_state_evidence.has_button_disabled && summary.interactive_state_evidence.has_button_focus;
  summary.interactive_state_evidence.interactive_tokens_complete = summary.interactive_state_evidence.has_focus_tokens && summary.interactive_state_evidence.has_interactive_color_tokens;

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
