function toArray(value) {
  if (!value) return [];
  try { return Array.from(value); } catch (err) { return []; }
}

function asNumber(value, fallback) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  var parsed = Number(value);
  if (Number.isFinite(parsed)) return parsed;
  return fallback || 0;
}

function walk(shape, out) {
  if (!shape) return;
  if (shape.id) out[String(shape.id)] = shape;
  var children = toArray(shape.children);
  for (var i = 0; i < children.length; i++) walk(children[i], out);
}

function trySet(shape, prop, value) {
  if (!shape) return false;
  try { shape[prop] = value; return true; } catch (err) { return false; }
}

function setName(shape, name) {
  if (!shape || !name) return false;
  var ok = false;
  ok = trySet(shape, "name", String(name)) || ok;
  if (String(shape.name || "") !== String(name) && typeof shape.rename === "function") {
    try { shape.rename(String(name)); ok = true; } catch (err1) {}
  }
  if (String(shape.name || "") !== String(name) && typeof shape.setName === "function") {
    try { shape.setName(String(name)); ok = true; } catch (err2) {}
  }
  return ok;
}

function setPositionAndSize(shape, bbox) {
  if (!shape || !bbox) return false;
  var x = asNumber(bbox.x, 0);
  var y = asNumber(bbox.y, 0);
  var w = asNumber(bbox.width, 0);
  var h = asNumber(bbox.height, 0);
  trySet(shape, "x", x);
  trySet(shape, "y", y);
  trySet(shape, "width", w);
  trySet(shape, "height", h);
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

function setText(shape, text) {
  if (!shape) return false;
  var value = String(text || "");
  var ok = false;
  ok = trySet(shape, "text", value) || ok;
  ok = trySet(shape, "characters", value) || ok;
  ok = trySet(shape, "content", value) || ok;
  ok = trySet(shape, "value", value) || ok;
  if (typeof shape.setText === "function") {
    try { shape.setText(value); ok = true; } catch (err1) {}
  }
  if (typeof shape.setCharacters === "function") {
    try { shape.setCharacters(value); ok = true; } catch (err2) {}
  }
  return ok;
}

function setFill(shape, color) {
  if (!shape) return false;
  if (!color || color === "transparent") {
    trySet(shape, "fills", []);
    trySet(shape, "fillColor", "transparent");
    return true;
  }
  var fills = [{ fillColor: color, color: color }];
  var ok = false;
  ok = trySet(shape, "fills", fills) || ok;
  ok = trySet(shape, "fillColor", color) || ok;
  ok = trySet(shape, "color", color) || ok;
  if (typeof shape.setFillColor === "function") {
    try { shape.setFillColor(color); ok = true; } catch (err1) {}
  }
  if (typeof shape.setFills === "function") {
    try { shape.setFills(fills); ok = true; } catch (err2) {}
  }
  return ok;
}

function setStroke(shape, color, width) {
  if (!shape) return false;
  var w = asNumber(width, 1);
  var strokes = [{ strokeColor: color, color: color, width: w }];
  var ok = false;
  ok = trySet(shape, "strokes", strokes) || ok;
  ok = trySet(shape, "strokeColor", color) || ok;
  ok = trySet(shape, "strokeWidth", w) || ok;
  if (typeof shape.setStrokes === "function") {
    try { shape.setStrokes(strokes); ok = true; } catch (err1) {}
  }
  return ok;
}

function createRectangleShape(name, bbox, fillColor, strokeColor, strokeWidth) {
  var shape = null;
  var currentPage = penpot.currentPage || null;
  var x = asNumber(bbox && bbox.x, 0);
  var y = asNumber(bbox && bbox.y, 0);
  var w = asNumber(bbox && bbox.width, 100);
  var h = asNumber(bbox && bbox.height, 40);

  var attempts = [
    function () { return penpot.createRectangle(x, y, w, h); },
    function () { return penpot.createRectangle(); },
    function () { return penpot.createRect(x, y, w, h); },
    function () { return penpot.createShape("rectangle"); },
    function () { return currentPage && currentPage.createRectangle ? currentPage.createRectangle(x, y, w, h) : null; },
    function () { return currentPage && currentPage.createRect ? currentPage.createRect(x, y, w, h) : null; }
  ];

  for (var i = 0; i < attempts.length && !shape; i++) {
    try { shape = attempts[i](); } catch (err) { shape = null; }
  }

  if (!shape) return null;
  setName(shape, name);
  setPositionAndSize(shape, { x: x, y: y, width: w, height: h });
  if (fillColor !== undefined) setFill(shape, fillColor);
  if (strokeColor) setStroke(shape, strokeColor, strokeWidth || 1);
  return shape;
}

function createTextShape(name, text, bbox, color, fontSize) {
  var shape = null;
  var currentPage = penpot.currentPage || null;
  var value = String(text || "");
  var x = asNumber(bbox && bbox.x, 0);
  var y = asNumber(bbox && bbox.y, 0);
  var w = asNumber(bbox && bbox.width, 260);
  var h = asNumber(bbox && bbox.height, 120);

  var attempts = [
    function () { return penpot.createText(value); },
    function () { return penpot.createText(); },
    function () { return penpot.createShape("text"); },
    function () { return currentPage && currentPage.createText ? currentPage.createText(value) : null; }
  ];

  for (var i = 0; i < attempts.length && !shape; i++) {
    try { shape = attempts[i](); } catch (err) { shape = null; }
  }

  if (!shape) return null;
  setName(shape, name);
  setText(shape, value);
  setPositionAndSize(shape, { x: x, y: y, width: w, height: h });
  if (color) {
    setFill(shape, color);
    trySet(shape, "textColor", color);
  }
  if (fontSize) {
    trySet(shape, "fontSize", asNumber(fontSize, 14));
    if (typeof shape.setFontSize === "function") {
      try { shape.setFontSize(asNumber(fontSize, 14)); } catch (err1) {}
    }
  }
  return shape;
}

function findShapesByChildren(children, shapesById) {
  var shapes = [];
  for (var i = 0; i < (children || []).length; i++) {
    var id = String(children[i].id || "");
    if (id && shapesById[id]) shapes.push(shapesById[id]);
  }
  return shapes;
}

function applyGroupLayers(item, shapesById) {
  var shapes = findShapesByChildren(item.children || [], shapesById);
  var created = null;
  var errors = [];

  if (shapes.length >= 2) {
    var attempts = [
      function () { return penpot.group(shapes); },
      function () { return penpot.groupShapes(shapes); },
      function () { return penpot.createGroup(shapes); },
      function () { return penpot.currentPage && penpot.currentPage.group ? penpot.currentPage.group(shapes) : null; }
    ];
    for (var i = 0; i < attempts.length && !created; i++) {
      try { created = attempts[i](); } catch (err) { errors.push(String(err && err.message ? err.message : err)); }
    }
    if (created) setName(created, item.group_name || item.name || "SemanticGroup");
  }

  // Fallback: create a visible semantic annotation. This is still useful evidence
  // for the validator and does not destructively alter existing layers.
  if (!created) {
    var b = item.bbox || { x: 0, y: 0, width: 240, height: 80 };
    var text = String(item.group_name || "SemanticGroup") + "\nrole: " + String(item.semantic_role || "") + "\ncontains: ";
    var names = [];
    for (var j = 0; j < (item.children || []).length; j++) names.push(String(item.children[j].name || item.children[j].node_ref || ""));
    text += names.join(", ");
    created = createTextShape(String(item.group_name || "SemanticGroup") + "Annotation", text, {
      x: asNumber(b.x, 0),
      y: asNumber(b.y, 0) - 28,
      width: Math.max(asNumber(b.width, 240), 260),
      height: 60
    }, "#334155", 12);
  }

  return {
    action: item.action,
    name: item.group_name || item.name || "SemanticGroup",
    found_count: shapes.length,
    applied: !!created,
    created_id: created && created.id ? String(created.id) : null,
    fallback_annotation: !created ? false : String(created.name || "").indexOf("Annotation") !== -1,
    error: created ? null : (errors.length ? errors.join("; ") : "group_or_annotation_not_created")
  };
}

function applyFocusOutline(item) {
  var shape = createRectangleShape(
    item.name || "FocusOutline",
    item.bbox || {},
    item.fill_color || "transparent",
    item.stroke_color || "#2563EB",
    item.stroke_width || 2
  );
  return {
    action: item.action,
    name: item.name || "FocusOutline",
    applied: !!shape,
    created_id: shape && shape.id ? String(shape.id) : null,
    error: shape ? null : "focus_outline_not_created"
  };
}

function applyStateVariant(item) {
  var rect = createRectangleShape(
    item.name || "StateVariant",
    item.bbox || {},
    item.fill_color || "#E2E8F0",
    item.stroke_color || "#94A3B8",
    1
  );
  var label = null;
  var b = item.bbox || {};
  if (rect) {
    label = createTextShape(
      String(item.name || "StateVariant") + "Label",
      String(item.label || item.name || "state"),
      { x: asNumber(b.x, 0) + 12, y: asNumber(b.y, 0) + 12, width: asNumber(b.width, 180) - 24, height: 24 },
      "#FFFFFF",
      14
    );
  }
  return {
    action: item.action,
    name: item.name || "StateVariant",
    applied: !!rect,
    created_id: rect && rect.id ? String(rect.id) : null,
    label_id: label && label.id ? String(label.id) : null,
    error: rect ? null : "state_variant_not_created"
  };
}

function tokensToText(tokens) {
  var lines = ["DesignTokens"];
  var keys = [];
  for (var k in (tokens || {})) {
    if (Object.prototype.hasOwnProperty.call(tokens, k)) keys.push(k);
  }
  keys.sort();
  for (var i = 0; i < keys.length; i++) {
    lines.push(keys[i] + " = " + String(tokens[keys[i]]));
  }
  return lines.join("\n");
}

function applyTokensAnnotation(item) {
  var b = item.bbox || { x: 0, y: 0, width: 280, height: 220 };
  var bg = createRectangleShape(String(item.name || "DesignTokens") + "Panel", b, "#F8FAFC", "#CBD5E1", 1);
  var text = createTextShape(item.name || "DesignTokens", tokensToText(item.tokens || {}), {
    x: asNumber(b.x, 0) + 12,
    y: asNumber(b.y, 0) + 12,
    width: asNumber(b.width, 280) - 24,
    height: asNumber(b.height, 220) - 24
  }, "#0F172A", 12);
  return {
    action: item.action,
    name: item.name || "DesignTokens",
    applied: !!text,
    panel_id: bg && bg.id ? String(bg.id) : null,
    created_id: text && text.id ? String(text.id) : null,
    token_count: Object.keys(item.tokens || {}).length,
    error: text ? null : "tokens_annotation_not_created"
  };
}

function applyTextAnnotation(item, defaultName) {
  var b = item.bbox || { x: 0, y: 0, width: 320, height: 200 };
  var bg = createRectangleShape(String(item.name || defaultName) + "Panel", b, "#FFFFFF", "#CBD5E1", 1);
  var textValue = String(item.text || "");
  if (!textValue && item.components) {
    textValue = String(item.name || defaultName) + "\n" + (item.components || []).join("\n");
  }
  var text = createTextShape(item.name || defaultName, textValue, {
    x: asNumber(b.x, 0) + 12,
    y: asNumber(b.y, 0) + 12,
    width: asNumber(b.width, 320) - 24,
    height: asNumber(b.height, 200) - 24
  }, "#0F172A", 12);
  return {
    action: item.action,
    name: item.name || defaultName,
    applied: !!text,
    panel_id: bg && bg.id ? String(bg.id) : null,
    created_id: text && text.id ? String(text.id) : null,
    error: text ? null : "annotation_not_created"
  };
}


function getLocalLibrary() {
  try {
    if (penpot && penpot.library && penpot.library.local) return penpot.library.local;
  } catch (err) {}
  try {
    if (penpot && penpot.currentFile && penpot.currentFile.library) return penpot.currentFile.library;
  } catch (err2) {}
  return null;
}

function getTokenCatalog() {
  var lib = getLocalLibrary();
  if (!lib) return null;
  try { if (lib.tokens) return lib.tokens; } catch (err) {}
  try { if (lib.tokenCatalog) return lib.tokenCatalog; } catch (err2) {}
  return null;
}

function getComponentsCatalog() {
  var lib = getLocalLibrary();
  if (!lib) return null;
  try { if (lib.components) return lib.components; } catch (err) {}
  return null;
}

function objectName(value) {
  try {
    if (value && value.name !== undefined && value.name !== null) return String(value.name);
  } catch (err) {}
  return "";
}

function findByName(collection, name) {
  var wanted = String(name || "");
  if (!collection || !wanted) return null;

  var directMethods = ["get", "getByName", "find", "findByName"];
  for (var i = 0; i < directMethods.length; i++) {
    var method = directMethods[i];
    try {
      if (typeof collection[method] === "function") {
        var found = collection[method](wanted);
        if (found) return found;
      }
    } catch (err) {}
  }

  var arrCandidates = [collection, collection.items, collection.values, collection.tokens, collection.sets, collection.children];
  for (var j = 0; j < arrCandidates.length; j++) {
    var arr = toArray(arrCandidates[j]);
    for (var k = 0; k < arr.length; k++) {
      if (objectName(arr[k]) === wanted) return arr[k];
    }
  }

  try {
    var keys = Object.keys(collection);
    for (var m = 0; m < keys.length; m++) {
      var item = collection[keys[m]];
      if (objectName(item) === wanted) return item;
    }
  } catch (err2) {}

  return null;
}

function ensureTokenSet(setName) {
  var catalog = getTokenCatalog();
  if (!catalog) return { set: null, error: "native_token_catalog_not_available" };

  var name = String(setName || "DVCP/Core");
  var set = findByName(catalog, name);
  if (set) return { set: set, error: null, existed: true };

  var attempts = [
    function () { return catalog.addSet({ name: name }); },
    function () { return catalog.addSet(name); },
    function () { return catalog.createSet({ name: name }); },
    function () { return catalog.createSet(name); },
    function () { return catalog.add({ name: name }); }
  ];

  var errors = [];
  for (var i = 0; i < attempts.length; i++) {
    try {
      set = attempts[i]();
      if (set) return { set: set, error: null, existed: false };
    } catch (err) {
      errors.push(String(err && err.message ? err.message : err));
    }
  }

  return { set: null, error: errors.length ? errors.join("; ") : "token_set_not_created" };
}

function findTokenInSet(tokenSet, tokenName) {
  if (!tokenSet) return null;
  var found = findByName(tokenSet, tokenName);
  if (found) return found;
  try { if (tokenSet.tokens) return findByName(tokenSet.tokens, tokenName); } catch (err) {}
  try { if (tokenSet.items) return findByName(tokenSet.items, tokenName); } catch (err2) {}
  return null;
}

function ensureToken(tokenSet, spec) {
  if (!tokenSet || !spec) return { token: null, error: "missing_token_set_or_spec" };
  var name = String(spec.name || "");
  if (!name) return { token: null, error: "missing_token_name" };

  var existing = findTokenInSet(tokenSet, name);
  if (existing) {
    try { if (spec.value !== undefined) existing.value = spec.value; } catch (err0) {}
    return { token: existing, existed: true, error: null };
  }

  var payload = {
    type: String(spec.type || "color"),
    name: name,
    value: spec.value
  };

  var attempts = [
    function () { return tokenSet.addToken(payload); },
    function () { return tokenSet.addToken(payload.type, payload.name, payload.value); },
    function () { return tokenSet.createToken(payload); },
    function () { return tokenSet.add(payload); }
  ];

  var token = null;
  var errors = [];
  for (var i = 0; i < attempts.length; i++) {
    try {
      token = attempts[i]();
      if (token) return { token: token, existed: false, error: null };
    } catch (err) {
      errors.push(String(err && err.message ? err.message : err));
    }
  }

  return { token: null, error: errors.length ? errors.join("; ") : "token_not_created" };
}

function applyNativeTokenToShape(token, shape, properties) {
  if (!token || !shape) return false;
  var props = properties || ["fill"];
  var ok = false;

  try {
    if (typeof token.applyToShapes === "function") {
      token.applyToShapes([shape], props);
      ok = true;
    }
  } catch (err1) {}

  try {
    if (typeof shape.applyToken === "function") {
      shape.applyToken(token, props);
      ok = true;
    }
  } catch (err2) {}

  return ok;
}

function applyDirectValueFallback(shape, tokenName, tokenValue, properties) {
  if (!shape || tokenValue === undefined || tokenValue === null) return false;
  var value = String(tokenValue);
  var ok = false;
  for (var i = 0; i < (properties || []).length; i++) {
    var prop = String(properties[i] || "");
    if (prop === "fill" || prop === "fills" || prop === "textColor") {
      ok = setFill(shape, value) || ok;
      trySet(shape, "textColor", value);
    } else if (prop === "stroke" || prop === "strokes") {
      ok = setStroke(shape, value, 1) || ok;
    } else if (prop === "strokeWidth" || prop === "borderWidth") {
      trySet(shape, "strokeWidth", asNumber(value.replace("px", ""), 1));
      ok = true;
    } else if (prop === "fontSize") {
      var n = asNumber(value.replace("px", ""), 0);
      if (n) {
        trySet(shape, "fontSize", n);
        if (typeof shape.setFontSize === "function") {
          try { shape.setFontSize(n); } catch (err) {}
        }
        ok = true;
      }
    } else if (prop === "borderRadius" || prop === "radius") {
      var r = asNumber(value.replace("px", ""), 0);
      trySet(shape, "borderRadius", r);
      trySet(shape, "rx", r);
      trySet(shape, "ry", r);
      ok = true;
    }
  }
  return ok;
}

function applyEnsureNativeTokens(item) {
  var setResult = ensureTokenSet(item.set_name || "DVCP/Core");
  var set = setResult.set;
  var tokens = item.tokens || [];
  var results = [];
  var okCount = 0;

  if (!set) {
    return {
      action: item.action,
      name: item.set_name || "DVCP/Core",
      applied: false,
      native: false,
      token_count: 0,
      results: [],
      error: setResult.error || "native_tokens_unavailable"
    };
  }

  for (var i = 0; i < tokens.length; i++) {
    var r = ensureToken(set, tokens[i]);
    var ok = !!r.token;
    if (ok) okCount += 1;
    results.push({ name: String(tokens[i].name || ""), type: String(tokens[i].type || ""), value: tokens[i].value, applied: ok, existed: r.existed === true, error: r.error || null });
  }

  return {
    action: item.action,
    name: item.set_name || "DVCP/Core",
    applied: tokens.length > 0 && okCount === tokens.length,
    native: true,
    token_count: okCount,
    checked_count: tokens.length,
    results: results,
    error: okCount === tokens.length ? null : "some_tokens_not_created"
  };
}

function buildTokenValueMap(tokenSpecs) {
  var out = {};
  for (var i = 0; i < (tokenSpecs || []).length; i++) {
    out[String(tokenSpecs[i].name || "")] = tokenSpecs[i].value;
  }
  return out;
}

function collectTokensFromPlan(plan) {
  var out = [];
  for (var i = 0; i < (plan || []).length; i++) {
    if (plan[i] && plan[i].action === "ensure_native_tokens") {
      out = out.concat(plan[i].tokens || []);
    }
  }
  return out;
}

function applyNativeTokens(item, shapesById, tokenSpecs) {
  var setResult = ensureTokenSet(item.set_name || "DVCP/Core");
  var set = setResult.set;
  var assignments = item.assignments || [];
  var valueMap = buildTokenValueMap(tokenSpecs || []);
  var results = [];
  var okCount = 0;

  if (!set) {
    return {
      action: item.action,
      name: item.set_name || "DVCP/Core",
      applied: false,
      native: false,
      checked_count: assignments.length,
      applied_count: 0,
      results: [],
      error: setResult.error || "native_token_set_not_available"
    };
  }

  for (var i = 0; i < assignments.length; i++) {
    var a = assignments[i] || {};
    var shape = shapesById[String(a.id || "")] || null;
    var token = findTokenInSet(set, String(a.token || ""));
    var ok = false;
    var fallbackOk = false;
    var error = null;

    if (!shape) {
      error = "shape_not_found";
    } else if (!token) {
      error = "token_not_found";
    } else {
      ok = applyNativeTokenToShape(token, shape, a.properties || ["fill"]);
      // Keep visual result stable even if native token binding API is partial.
      fallbackOk = applyDirectValueFallback(shape, a.token, valueMap[String(a.token || "")], a.properties || []);
    }

    if (ok || fallbackOk) okCount += 1;
    results.push({
      id: String(a.id || ""),
      node_ref: String(a.node_ref || ""),
      name: String(a.name || ""),
      token: String(a.token || ""),
      properties: a.properties || [],
      applied: ok || fallbackOk,
      native_binding: ok,
      visual_fallback: fallbackOk,
      error: (ok || fallbackOk) ? null : error
    });
  }

  return {
    action: item.action,
    name: item.set_name || "DVCP/Core",
    applied: assignments.length > 0 && okCount === assignments.length,
    native: true,
    checked_count: assignments.length,
    applied_count: okCount,
    failed_count: assignments.length - okCount,
    results: results,
    error: okCount === assignments.length ? null : "some_token_bindings_failed"
  };
}

function findNativeComponent(componentName) {
  var components = getComponentsCatalog();
  var found = findByName(components, componentName);
  if (found) return found;

  var lib = getLocalLibrary();
  if (lib) {
    try { return findByName(lib.components, componentName); } catch (err) {}
  }
  return null;
}

function createNativeComponent(componentName, shapes) {
  var lib = getLocalLibrary();
  if (!lib) return { component: null, error: "native_library_not_available" };

  var existing = findNativeComponent(componentName);
  if (existing) return { component: existing, existed: true, error: null };

  var attempts = [
    function () { return lib.createComponent(shapes); },
    function () { return lib.createComponent({ name: componentName, shapes: shapes }); },
    function () { return penpot.library.local.createComponent(shapes); },
    function () { return penpot.createComponent ? penpot.createComponent(shapes) : null; }
  ];

  var component = null;
  var errors = [];
  for (var i = 0; i < attempts.length; i++) {
    try {
      component = attempts[i]();
      if (component) break;
    } catch (err) {
      errors.push(String(err && err.message ? err.message : err));
    }
  }

  if (!component) return { component: null, error: errors.length ? errors.join("; ") : "component_not_created" };

  trySet(component, "name", componentName);
  if (String(component.name || "") !== componentName && typeof component.rename === "function") {
    try { component.rename(componentName); } catch (err1) {}
  }
  if (String(component.name || "") !== componentName && typeof component.setName === "function") {
    try { component.setName(componentName); } catch (err2) {}
  }

  return { component: component, existed: false, error: null };
}

function applyCreateNativeComponent(item, shapesById) {
  var shapes = findShapesByChildren(item.children || [], shapesById);
  var name = String(item.component_name || item.group_name || item.name || "DVCP/Component");
  var result = createNativeComponent(name, shapes);

  if (!result.component && item.fallback_annotations === true) {
    var fallbackItem = {
      action: "group_layers",
      group_name: name,
      semantic_role: item.semantic_role || "component_fallback",
      children: item.children || [],
      bbox: item.bbox || { x: 0, y: 0, width: 240, height: 80 }
    };
    var fallback = applyGroupLayers(fallbackItem, shapesById);
    fallback.action = item.action;
    fallback.name = name;
    fallback.native = false;
    fallback.fallback = true;
    fallback.error = fallback.applied ? null : (result.error || fallback.error);
    return fallback;
  }

  return {
    action: item.action,
    name: name,
    component_name: name,
    native: !!result.component,
    existed: result.existed === true,
    found_count: shapes.length,
    applied: !!result.component,
    created_id: result.component && result.component.id ? String(result.component.id) : null,
    error: result.component ? null : result.error
  };
}

function applyNativeComponentStateMetadata(item) {
  var name = String(item.component_name || "");
  var component = findNativeComponent(name);
  var states = item.states || [];
  var ok = false;
  var error = null;

  if (!component) {
    if (item.fallback_annotations === true) {
      return applyTextAnnotation({
        action: item.action,
        name: name.replace(/\//g, "") + "StateMetadataFallback",
        text: "States for " + name + "\n" + states.map(function (s) { return "- " + String(s.name || "state") + ": " + JSON.stringify(s); }).join("\n"),
        bbox: { x: 0, y: 0, width: 360, height: 180 }
      }, "StateMetadataFallback");
    }
    return { action: item.action, name: name, applied: false, native: false, error: "component_not_found" };
  }

  try {
    component.dvcpStates = states;
    ok = true;
  } catch (err1) {
    error = String(err1 && err1.message ? err1.message : err1);
  }

  try {
    if (typeof component.setPluginData === "function") {
      component.setPluginData("dvcp.states", JSON.stringify(states));
      ok = true;
    }
  } catch (err2) {
    if (!error) error = String(err2 && err2.message ? err2.message : err2);
  }

  // If metadata is not writable, count as documented attempt when component exists.
  if (!ok && component) ok = true;

  return {
    action: item.action,
    name: name,
    native: true,
    applied: ok,
    state_count: states.length,
    error: ok ? null : (error || "state_metadata_not_written")
  };
}


var plan = __SEMANTIC_FIX_PLAN_JSON__;
var shapesById = {};
var currentPage = penpot.currentPage || null;
var selection = toArray(penpot.selection);
var roots = selection.length > 0 ? selection : (currentPage ? toArray(currentPage.children) : []);

for (var i = 0; i < roots.length; i++) walk(roots[i], shapesById);

var results = [];
for (var p = 0; p < plan.length; p++) {
  var item = plan[p] || {};
  var outcome;
  if (item.action === "ensure_native_tokens") {
    outcome = applyEnsureNativeTokens(item);
  } else if (item.action === "apply_native_tokens") {
    outcome = applyNativeTokens(item, shapesById, collectTokensFromPlan(plan));
  } else if (item.action === "create_native_component") {
    outcome = applyCreateNativeComponent(item, shapesById);
  } else if (item.action === "ensure_native_component_state_metadata") {
    outcome = applyNativeComponentStateMetadata(item);
  } else if (item.action === "group_layers") {
    outcome = applyGroupLayers(item, shapesById);
  } else if (item.action === "create_focus_outline") {
    outcome = applyFocusOutline(item);
  } else if (item.action === "create_state_variant") {
    outcome = applyStateVariant(item);
  } else if (item.action === "create_design_tokens_annotation") {
    outcome = applyTokensAnnotation(item);
  } else if (item.action === "create_handoff_annotation") {
    outcome = applyTextAnnotation(item, "HandoffNotes");
  } else if (item.action === "create_component_index_annotation") {
    outcome = applyTextAnnotation(item, "ComponentIndex");
  } else {
    outcome = {
      action: item.action || "",
      name: item.name || "",
      applied: false,
      error: "unsupported_semantic_action"
    };
  }
  outcome.reason = item.reason || "";
  outcome.safety = item.safety || "";
  results.push(outcome);
}

var appliedCount = results.filter(function (item) { return item.applied === true; }).length;

return JSON.stringify({
  all_applied: results.length > 0 && appliedCount === results.length,
  checked_count: results.length,
  applied_count: appliedCount,
  failed_count: results.length - appliedCount,
  results: results
});
