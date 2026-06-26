# Penpot Design Workflow

## Objetivo

Este proyecto implementa un agente con LangGraph conectado a Penpot mediante MCP para crear, validar y corregir interfaces UI.

El workflow busca separar tres responsabilidades principales:

```text
Builder   -> crea o modifica diseño en Penpot
Validator -> valida visual y estructuralmente el diseño
Fixer     -> convierte el reporte del validator en instrucciones seguras para el builder
```

## Stack

```text
LangGraph
LangChain
Mistral
Penpot MCP
Python
```

## Acciones principales

```text
build
validate_only
build_and_validate
validate_and_fix
build_validate_and_fix
```

## Workflow actual

El flujo principal de validación funciona así:

```text
prepare_input
-> run_validator
-> validator_visual_call
-> export_shape
-> execute_code read-only
-> Mistral Vision
-> validation_report
```

El validator ya no depende solo de la imagen PNG. Ahora combina:

```text
Imagen exportada desde Penpot
+
Estructura real de capas obtenida con execute_code read-only
+
DESIGN_CONTEXT_JSON
```

Esto permite mapear regiones visuales a capas reales de Penpot usando:

```text
node_ref
id
name
type
path
bbox
```

Ejemplo:

```json
{
  "visual_region": "login_button_background",
  "matched_layer": {
    "node_ref": "n_007",
    "id": "...",
    "name": "Rectangle",
    "type": "rectangle",
    "path": "LoginContainer / Rectangle[4]",
    "bbox": {
      "x": 724,
      "y": 560,
      "width": 240,
      "height": 45
    }
  }
}
```

## Auto-fix actual

El validator genera un `auto_fix_plan` para correcciones seguras y determinísticas.

Por ahora el auto-fix solo corrige renombrado semántico de capas. No modifica layout, colores, tamaños, textos visibles, componentes ni tokens.

Ejemplo de correcciones aplicadas:

```text
Rectangle -> LoginCardBackground
Text -> LoginTitleText
Rectangle -> EmailInputBackground
Text -> EmailInputLabel
Rectangle -> PasswordInputBackground
Text -> PasswordInputLabel
Rectangle -> LoginButtonBackground
Text -> LoginButtonText
```

## Checkpoint actual

Estado del proyecto:

```text
1. validate_only básico ✅
2. export_shape PNG desde Penpot ✅
3. lectura estructural con execute_code read-only ✅
4. imagen + DESIGN_CONTEXT_JSON hacia Mistral Vision ✅
5. mapeo visual a capas reales con node_ref ✅
6. generación de auto_fix_plan para renombrado seguro ✅
7. validate_and_fix ejecuta renombrado en Penpot ✅
8. verificación post-fix estable ⬅️ pendiente
```

Ya se comprobó que:

```text
- El validator obtiene estructura real de Penpot.
- El validator puede asociar elementos visuales con capas reales.
- El auto_fix_plan genera acciones seguras.
- El builder aplica los renombres correctamente en Penpot.
```

## Problema actual

Después de aplicar el fix, el grafo vuelve a ejecutar una validación visual completa con Mistral Vision.

Esto puede fallar por timeout:

```text
mistral_network / ReadTimeout
```

El problema no está en el renombrado ni en Penpot. El problema está en que la revalidación post-fix depende de una segunda llamada visual pesada a Mistral.

## Siguiente paso

Separar la validación visual completa de la verificación estructural post-fix.

Nuevo flujo recomendado:

```text
validate_and_fix
-> run_validator
-> fix_design
-> builder aplica auto_fix_plan
-> verify_auto_fix_plan_applied
-> END
```

La verificación post-fix debe ser determinística:

```text
- usar execute_code
- leer capas actuales de Penpot
- comparar id contra new_name esperado
- devolver all_applied true/false
```

Esto evita depender de Mistral para comprobar si el renombrado fue aplicado.

## Próxima implementación

Agregar un nuevo nodo:

```text
verify_auto_fix_plan_applied
```

Este nodo debe:

```text
1. Leer last_auto_fix_plan.
2. Ejecutar execute_code en modo lectura.
3. Buscar cada shape por id.
4. Comparar actual_name contra expected_name.
5. Devolver auto_fix_verified y auto_fix_verification.
```

Resultado esperado:

```json
{
  "auto_fix_verified": true,
  "auto_fix_verification": {
    "all_applied": true,
    "checked_count": 8,
    "applied_count": 8,
    "failed_count": 0
  }
}
```

## Estado estable buscado

El workflow debe quedar así:

```text
Validación visual para detectar problemas
-> auto_fix_plan seguro
-> builder aplica cambios
-> verificación estructural determinística
-> validación visual completa solo cuando sea necesario
```

Esto permitirá que el sistema sea más confiable, rápido y menos dependiente de llamadas repetidas a Mistral Vision.

