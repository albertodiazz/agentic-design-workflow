# DVCP Penpot Validator

Sistema de validación y corrección de diseños UI en Penpot usando MCP, LangGraph y Mistral Vision.

## Objetivo

Validar que una pantalla diseñada en Penpot sea entendible para desarrollo frontend, manteniendo separación estricta entre lectura, planeación y escritura sobre el canvas.

```text
Validator -> lee y evalúa
Fixer/Planner -> genera planes de corrección
Executor/Builder -> aplica cambios en Penpot vía MCP
```

## Arquitectura

```text
LangGraph
├── validate_only
├── validate_and_fix
└── validate_and_polish

Penpot MCP
├── high_level_overview
├── execute_code
├── export_shape
└── import_image

Mistral Vision
└── evalúa imagen + DESIGN_CONTEXT_JSON
```

## Acciones principales

### `validate_only`

Solo valida. No modifica Penpot.

```json
{
  "action": "validate_only",
  "changeme": "Valida el diseño actualizado",
  "max_fix_iterations": 1
}
```

### `validate_and_fix`

Valida y corrige problemas mayores de estructura, naming, layout, color, tipografía y agrupación.

```json
{
  "action": "validate_and_fix",
  "changeme": "Valida y corrige el diseño actual",
  "max_fix_iterations": 1
}
```

### `validate_and_polish`

Valida y pule detalles menores aunque el diseño ya pase. Usa tokens, assets y estados interactivos nativos de Penpot.

```json
{
  "action": "validate_and_polish",
  "changeme": "Valida y pule el diseño actual",
  "max_fix_iterations": 1
}
```

## Dominios de corrección

```text
Canvas Fix Domain
├── posición
├── tamaño de fuente
├── color de texto
├── fill
└── stroke

Semantic Fix Domain
├── grupos semánticos
├── componentes nativos
├── assets reutilizables
└── metadata DVCP

Token Domain
├── colores
├── spacing
├── tipografía
├── border width
└── radius

Interactive States Domain
├── focus
├── hover
├── disabled
└── state metadata
```

## Tokens nativos

Token set principal:

```text
DVCP/Core
```

Tokens base:

```text
color.action.primary.default
color.action.primary.hover
color.action.primary.disabled
color.focus.ring
color.text.default
color.text.inverse
color.text.disabled
color.border.input
color.surface.input
spacing.12
spacing.16
spacing.24
spacing.32
spacing.form.gap
spacing.input.padding.x
typography.heading.size
typography.label.size
typography.button.size
typography.heading.weight
typography.label.weight
typography.button.weight
border.input.width
border.focus.width
radius.input
radius.button
```

## Assets/componentes nativos

```text
TextInput / Email
TextInput / Password
Button / Primary
TextInput / Email / Focus
TextInput / Password / Focus
Button / Primary / Hover
Button / Primary / Focus
Button / Primary / Disabled
```

## Evidencia nativa

El reader expone evidencia compacta en:

```text
DESIGN_CONTEXT_JSON.native_library
DESIGN_CONTEXT_JSON.native_library.interactive_state_evidence
```

Campos esperados:

```json
{
  "tokens_complete": true,
  "states_complete": true,
  "focus_complete": true,
  "button_states_complete": true
}
```

Si esta evidencia está completa, el validator elimina recomendaciones obsoletas de `required_fixes` y `manual_fixes`.

## Contrato DVCP

El canvas se modela como un universo finito:

```text
U = {n_000, n_001, ..., n_k}
```

El LLM no transporta capas completas. Solo devuelve referencias:

```json
{
  "region": "login_button_text",
  "role": "button_text",
  "ref": "n_014",
  "confidence": 0.98
}
```

Python expande las referencias a objetos completos, genera planes y ejecuta acciones seguras.

## Invariantes

```text
1. Validator no escribe en Penpot.
2. Fixer/Planner no ejecuta cambios.
3. Executor solo aplica planes validados.
4. Canvas fixes solo operan sobre known targets.
5. Tokens/assets nativos tienen prioridad sobre anotaciones visibles.
6. Las anotaciones visibles son fallback/debug, no flujo principal.
7. validate_only nunca modifica el archivo.
8. validate_and_polish se activa por acción, no por bandera.
```

## Archivos principales

```text
src/agent/graph.py
src/agent/utils/agents/design_validator.py
src/agent/utils/skills/validator.md
src/agent/utils/js/penpot_read_structure.js
src/agent/utils/js/penpot_apply_canvas_fix_plan.js
src/agent/utils/js/penpot_apply_semantic_fix_plan.js
```

## Estado actual

Último estado validado:

```text
score: 88
passed: true
status: ready
issues: []
required_fixes: []
manual_fixes: []
can_be_sent_to_development: true
```

## Workflow recomendado

```text
1. Seleccionar LoginContainer en Penpot.
2. Ejecutar validate_only.
3. Si score < 70, ejecutar validate_and_fix.
4. Si score >= 70 pero hay detalles menores, ejecutar validate_and_polish.
5. Ejecutar validate_only final.
6. Revisar que issues, required_fixes y manual_fixes estén vacíos.
```

## Resultado esperado

```text
Canvas limpio
Tokens nativos completos
Assets/componentes reutilizables
Estados interactivos documentados
Reporte listo para desarrollo frontend
```

