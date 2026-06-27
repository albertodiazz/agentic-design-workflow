# Estado actual del flujo DVCP + Penpot Auto-Fix

## Objetivo

Construir un flujo de validación y corrección para diseños en Penpot usando tres responsabilidades separadas:

* **Validator**: inspecciona el diseño y produce un reporte. No modifica Penpot.
* **Fixer / Planner**: transforma el reporte en planes de corrección seguros.
* **Builder / Executor**: aplica cambios sobre Penpot usando MCP `execute_code` y scripts JS.

El sistema busca mantener una frontera clara entre lectura, diagnóstico, planeación y escritura.

---

## Estado del pipeline

### `validate_only`

```text
run_validator
-> END
```

Sirve para medir el estado actual del diseño. No debe aplicar cambios.

### `validate_and_fix`

```text
run_validator
-> deterministic_rename_phase, si hay auto_fix_plan de rename
-> verify_rename_phase
-> deterministic_canvas_fix_plan, si canvas auto-fix está habilitado
-> apply_canvas_fix_plan
-> END
```

Después de `validate_and_fix`, se debe correr otro `validate_only` para medir si el score visual subió.

---

## Lo que ya funciona

### 1. DVCP compacto

El validator ya usa un protocolo compacto:

```text
DesignSnapshot compacto
-> Mistral Vision produce ValidationDelta compacto
-> Python expande a ValidationReport completo
```

Esto evita que el modelo repita nodos completos y permite trabajar con referencias como `n_003`, `n_004`, etc.

### 2. Rename determinístico

Los cambios de nombre ya no dependen del builder LLM. Python genera un `auto_fix_plan` determinístico para acciones `rename_layer`, y un script JS las aplica por `id`.

Ejemplo aplicado:

```text
EmailInputLabel      -> EmailInputLabelText
PasswordInputLabel   -> PasswordInputLabelText
```

### 3. Parser de resultados anidados

`execute_code` puede regresar resultados anidados:

```text
raw
-> list[0].text
-> wrapper.result
-> JSON real
```

El parser ya soporta este formato y puede extraer correctamente `all_applied`, `checked_count`, `applied_count` y `failed_count`.

### 4. Canvas fix plan determinístico

El canvas auto-fix ya no manda instrucciones vagas al builder. Ahora genera un `canvas_fix_plan` explícito y lo aplica con JS.

Acciones implementadas:

```text
set_position
set_min_font_size
set_text_color
set_fill_color
set_stroke
```

El último plan fuerte aplicó correctamente todas sus acciones:

```text
checked_count: 21
applied_count: 21
failed_count: 0
```

---

## Estado visual actual

El diseño sí cambió visualmente. El validator ya detecta nuevas posiciones como:

```text
LoginTitleText:          y = 340
EmailInputBackground:    y = 393
PasswordInputBackground: y = 457
LoginButtonBackground:   y = 521
```

Sin embargo, el score sigue igual:

```text
score: 65
status: needs_major_fixes
passed: false
```

Los principales fallos restantes son:

```text
layout_spacing
componentization
accessibility
frontend_handoff
```

---

## Diagnóstico actual

La infraestructura ya funciona:

```text
DVCP compacto: OK
validator read-only: OK
rename determinístico: OK
parser execute_code: OK
canvas_fix_plan: OK
JS apply canvas: OK
Penpot update: OK
validate_only posterior: OK
```

El problema actual ya no es técnico de aplicación, sino semántico:

```text
El canvas_fix_plan modifica geometría y estilos,
pero el validator penaliza estructura y semántica de diseño.
```

Ejemplos de problemas que no se resuelven solo con posición/color:

```text
- Inputs no están componentizados.
- Labels no están asociados estructuralmente a inputs.
- No hay estados hover/focus/disabled.
- No hay documentación de handoff.
- No hay evidencia estructural de accesibilidad.
```

---

## Siguiente fase pendiente

Se debe diseñar una fase nueva:

```text
semantic_canvas_fix_plan
```

Esta fase debe planear y aplicar cambios estructurales, no solo visuales.

Acciones candidatas:

```text
group_layers
create_component
create_focus_outline
create_state_variant
add_handoff_annotation
rename_layer_semantic
```

Estructura deseada:

```text
LoginContainer
├── LoginCardBackground
├── LoginTitleText
├── EmailInputGroup
│   ├── EmailInputLabelText
│   └── EmailInputBackground
├── PasswordInputGroup
│   ├── PasswordInputLabelText
│   └── PasswordInputBackground
└── LoginButtonGroup
    ├── LoginButtonBackground
    └── LoginButtonText
```

Además, podrían agregarse capas auxiliares o variantes:

```text
EmailInputFocusOutline
PasswordInputFocusOutline
LoginButtonHoverState
LoginButtonDisabledState
```

---

## Variables relevantes

```bash
PENPOT_ENABLE_CANVAS_AUTO_FIX=1
PENPOT_CANVAS_AUTO_FIX_MIN_CONFIDENCE=0.8
PENPOT_RENAME_AUTO_FIX_MIN_CONFIDENCE=0.8
```

---

## Archivos principales

```text
src/agent/graph.py
src/agent/utils/agents/design_validator.py
src/agent/utils/fixer_prompt.py
src/agent/utils/skills/validator.md
src/agent/utils/skills/fixer.md
src/agent/utils/json/auto_fix_constraints.json
src/agent/utils/json/schemas/validator_delta_contract.json
src/agent/utils/json/schemas/validator_report_contract.json
src/agent/utils/js/penpot_read_structure.js
src/agent/utils/js/penpot_verify_rename_plan.js
src/agent/utils/js/penpot_apply_rename_plan.js
src/agent/utils/js/penpot_apply_canvas_fix_plan.js
```

---

## Próximo paso recomendado

Antes de implementar más JS, planear formalmente la fase semántica:

```text
1. Definir qué significa componentizar en Penpot.
2. Definir si se crearán grupos, componentes reales o solo capas semánticas.
3. Definir cómo representar focus/hover/disabled.
4. Definir qué puede verificar el validator después.
5. Definir límites de seguridad para evitar cambios destructivos.
```

