# DVCP — Design Validation Communication Protocol

## Versión

```text
DVCP/0.1
```

DVCP significa **Design Validation Communication Protocol**. Es el protocolo interno usado para comunicar el estado de un diseño, sus problemas y sus planes de corrección entre el validator, el planner/fixer y los ejecutores de Penpot.

---

## Propósito

DVCP separa cuatro operaciones que no deben mezclarse:

```text
1. Lectura del diseño
2. Validación visual / estructural
3. Planeación de correcciones
4. Ejecución de cambios
```

La separación evita que el modelo de visión modifique el canvas directamente y permite verificar los cambios por `id`, `node_ref` y acciones explícitas.

---

## Roles del sistema

### User

Solicita una acción:

```json
{
  "action": "validate_only",
  "changeme": "Valida el diseño actual",
  "max_fix_iterations": 1
}
```

O:

```json
{
  "action": "validate_and_fix",
  "changeme": "Valida y corrige el diseño actual",
  "max_fix_iterations": 1
}
```

### Graph

Orquesta el flujo. Decide si solo valida o si también debe entrar a fases de corrección.

### Penpot MCP

Fuente de lectura y mecanismo de escritura. Se usa principalmente mediante:

```text
high_level_overview
execute_code
```

### Validator

Responsabilidad estricta:

```text
Penpot state -> ValidationReport
```

No puede modificar el canvas.

### Vision model

Recibe un `DesignSnapshot` compacto y produce un `ValidationDelta` compacto. No debe generar scripts ni ejecutar cambios.

### Fixer / Planner

Transforma problemas del `ValidationReport` en planes explícitos:

```text
ValidationReport -> RenameFixPlan
ValidationReport -> CanvasFixPlan
```

### Executor

Aplica planes en Penpot mediante JS controlado.

---

## Modelo formal del dominio

Sea:

```text
U = conjunto finito de nodos/capas del canvas
R = conjunto de regiones visuales inferidas
C = conjunto de categorías de problemas
S = conjunto de severidades
```

Funciones de metadatos:

```text
id: U -> ID
name: U -> String
type: U -> Type
path: U -> Path
bbox: U -> Box
parent: U ⇀ U
```

Relaciones estructurales:

```text
contains ⊆ U × U
near_to ⊆ U × U
aligned_with ⊆ U × U
same_group_as ⊆ U × U
```

Mapeo visual:

```text
maps_to ⊆ R × U × Confidence
```

Known targets:

```text
KnownTargets = { n ∈ U | ∃r ∈ R : maps_to(r,n,c) ∧ c ≥ threshold }
```

Problemas:

```text
Issue ⊆ Category × Severity × P(U)
```

Candidatos corregibles:

```text
FixCandidates = ⋃ { A ⊆ U | ∃c∈C, ∃s∈S : (c,s,A) ∈ Issue }
```

Objetivos seguros de canvas:

```text
SafeCanvasTargets = FixCandidates ∩ KnownTargets
ForbiddenTargets = U - SafeCanvasTargets
```

---

## Tipos de mensajes

### 1. DesignSnapshot

Resumen compacto del diseño leído desde Penpot.

Contiene:

```json
{
  "nodes": [
    {
      "ref": "n_003",
      "id": "...",
      "name": "EmailInputBackground",
      "type": "rectangle",
      "bbox": { "x": 724, "y": 393, "width": 240, "height": 40 },
      "path": "LoginContainer / EmailInputBackground"
    }
  ],
  "context": {
    "file": "Test",
    "page": "Page 1",
    "root_source": "selection"
  }
}
```

### 2. ValidationDelta

Salida compacta del modelo de visión.

Debe referirse a nodos usando `node_ref`, no duplicar nodos completos.

```json
{
  "score": 65,
  "status": "needs_major_fixes",
  "checks": {
    "layout_spacing": { "status": "fail", "score": 40 }
  },
  "issues": [
    {
      "category": "layout_spacing",
      "severity": "high",
      "affected_refs": ["n_002", "n_003", "n_005", "n_007"],
      "recommendation": "Normalizar espaciado vertical."
    }
  ],
  "visual_structure_map": [
    {
      "visual_region": "email_input_background",
      "node_ref": "n_003",
      "inferred_role": "input_background",
      "confidence": 0.98
    }
  ]
}
```

### 3. ValidationReport

Reporte expandido por Python.

```text
ValidationDelta + DesignSnapshot -> ValidationReport
```

Contiene nodos completos en `affected_layers`, `visual_structure_map`, `required_fixes`, `manual_fixes`, etc.

### 4. RenameFixPlan

Plan determinístico para nombres de capa.

```json
[
  {
    "action": "rename_layer",
    "node_ref": "n_004",
    "id": "...",
    "current_name": "EmailInputLabel",
    "new_name": "EmailInputLabelText",
    "confidence": 0.95,
    "safety": "safe_auto_fix"
  }
]
```

### 5. CanvasFixPlan

Plan determinístico para geometría y estilo.

```json
[
  {
    "action": "set_position",
    "node_ref": "n_003",
    "id": "...",
    "target": { "x": 724, "y": 393 },
    "safety": "safe_canvas_fix_known_target"
  },
  {
    "action": "set_fill_color",
    "node_ref": "n_003",
    "id": "...",
    "fill_color": "#FFFFFF",
    "safety": "safe_canvas_fix_known_target"
  }
]
```

### 6. ToolResult

Resultado de `execute_code`.

Puede venir en forma directa o anidada.

Formato directo:

```json
{
  "all_applied": true,
  "checked_count": 21,
  "applied_count": 21,
  "failed_count": 0,
  "results": []
}
```

Formato anidado observado:

```text
raw
-> JSON array
   -> item[0].text
      -> JSON object
         -> result
            -> JSON string con el resultado real
```

El parser debe aplicar unwrap recursivo hasta obtener el objeto real.

---

## Máquina de estados

### validate_only

```text
START
-> run_validator
-> END
```

### validate_and_fix

```text
START
-> run_validator
-> if rename plan exists: apply_rename_plan
-> verify_rename_plan
-> if canvas auto-fix enabled: build_canvas_fix_plan
-> apply_canvas_fix_plan
-> END
```

El canvas auto-fix puede ejecutarse si:

```text
PENPOT_ENABLE_CANVAS_AUTO_FIX = 1
∧ KnownTargets ≠ ∅
∧ confidence >= threshold
∧ rename_phase ∈ {verified, not_needed}
```

---

## Invariantes de seguridad

### Invariante 1: validator read-only

```text
Validator nunca ejecuta cambios sobre Penpot.
```

### Invariante 2: cambios solo sobre known targets

```text
∀ action ∈ CanvasFixPlan:
  action.target ∈ KnownTargets
```

### Invariante 3: umbral mínimo de confianza

```text
target ∈ KnownTargets ⇔ confidence(target) ≥ 0.8
```

### Invariante 4: rename antes de canvas

```text
canvas_phase ≠ ∅ ⇒ rename_status ∈ {verified, not_needed}
```

### Invariante 5: canvas aplicado no equivale a diseño aprobado

```text
canvas_apply_result.all_applied = true
⇏ validation_report.passed = true
```

Después de aplicar canvas, siempre se requiere una nueva corrida:

```text
validate_only
```

---

## Acciones implementadas en DVCP/0.1

### Rename

```text
rename_layer
```

### Canvas visual

```text
set_position
set_min_font_size
set_text_color
set_fill_color
set_stroke
```

---

## Estados de ejecución

### Rename

```text
not_needed
verified
error
```

### Canvas

```text
not_needed
applied_unverified
error
```

### Verificación

```text
all_applied: boolean | null
checked_count: number
applied_count: number
failed_count: number
```

`all_applied = null` es válido para canvas cuando la verificación estructural no puede determinar si el diseño final es mejor. En ese caso se requiere `validate_only`.

---

## Limitación actual de DVCP/0.1

DVCP/0.1 corrige geometría y estilos, pero no corrige todavía estructura semántica profunda.

No implementado aún:

```text
group_layers
create_component
create_focus_outline
create_state_variant
add_handoff_annotation
semantic_label_input_association
```

Por eso un plan puede aplicarse al 100% y aun así el score puede quedarse igual si los fallos restantes pertenecen a componentización, accesibilidad semántica o handoff.

---

## Próxima versión propuesta: DVCP/0.2

DVCP/0.2 debería agregar una fase formal:

```text
semantic_canvas_fix_plan
```

Acciones candidatas:

```text
group_layers
create_component
create_focus_outline
create_state_variant
add_handoff_annotation
rename_layer_semantic
```

Nuevas invariantes necesarias:

```text
1. No destruir jerarquía existente sin snapshot previo.
2. Todo grupo creado debe tener miembros explícitos por id.
3. Todo componente creado debe ser verificable por nombre, tipo y children.
4. Todo estado interactivo debe quedar representado visualmente o documentado.
5. Toda asociación label/input debe poder validarse por proximidad, grupo o metadato.
```

