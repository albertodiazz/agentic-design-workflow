# Penpot Design Workflow

## Objetivo

Agente LangGraph conectado a Penpot mediante MCP para crear, validar y corregir interfaces UI.

El sistema separa tres responsabilidades:

```text
Builder   -> crea o modifica diseño en Penpot
Validator -> valida visual y estructuralmente el diseño actual
Fixer     -> convierte el reporte del validator en instrucciones seguras
Verifier  -> confirma si el auto-fix aplicado quedó reflejado en Penpot
```

## Stack

```text
LangGraph
LangChain
Mistral Vision
Penpot MCP
Python
```

## Acciones

```text
build
validate_only
build_and_validate
validate_and_fix
build_validate_and_fix
```

## Flujo de validación

```text
run_validator
-> validator_visual_call
-> export_shape
-> execute_code read-only
-> Mistral Vision
-> validation_report
```

El validator es **stateless**: no depende de runs anteriores, flags de fix ni planes previos. Siempre valida el diseño actual.

Combina:

```text
PNG exportado desde Penpot
+
estructura real de capas vía execute_code read-only
+
DESIGN_CONTEXT_JSON
```

El contexto permite mapear regiones visuales a capas reales usando:

```text
node_ref
id
name
type
path
bbox
```

## validate_only

```text
validate_only
-> run_validator
-> END
```

Devuelve:

```text
validation_report
passed
score
status
```

## validate_and_fix

```text
validate_and_fix
-> run_validator
-> fix_design
-> builder aplica auto_fix_plan
-> verify_auto_fix_plan_applied
-> END
```

`run_validator` produce los datos. El router decide pasar a `fix_design` solo si:

```text
passed=false
hay auto_fix_plan seguro
fix_iterations < max_fix_iterations
```

Después del fix **no se ejecuta otra validación visual completa** automáticamente.

## Auto-fix

El auto-fix actual solo aplica renombrado semántico seguro de capas.

Permitido:

```text
rename_layer
```

No permitido automáticamente:

```text
layout
colores
tamaños
textos visibles
componentes
tokens
accesibilidad avanzada
estados interactivos
```

El `auto_fix_plan` final debe ser determinístico y seguro. El LLM puede sugerir, pero Python normaliza referencias y genera el plan aplicable.

## Verificación post-fix

`verify_auto_fix_plan_applied` no usa LLM.

Solo confirma si los cambios del plan aplicado quedaron en Penpot:

```text
- lee last_auto_fix_plan
- ejecuta execute_code read-only
- busca cada shape por id
- compara actual_name contra expected_name
- devuelve una bandera con timestamp
```

Ejemplo:

```json
{
  "auto_fix_verified": true,
  "auto_fix_event": {
    "type": "auto_fix_verification",
    "status": "applied",
    "verified_at": "2026-06-26T18:42:10Z",
    "fix_iteration": 1,
    "checked_count": 8,
    "applied_count": 8,
    "failed_count": 0
  }
}
```

Esta verificación no modifica:

```text
validation_report
passed
score
status
```

La validación global sigue perteneciendo solo al validator.

## Mistral Vision

La llamada visual se hace desde `MistralVisionRunnable`.

El payload real incluye:

```text
VISUAL_VALIDATOR_PROMPT
+
changeme
+
DESIGN_CONTEXT_JSON compacto
+
image_url data:image/png;base64,...
```

Se confirmó que el payload real funciona contra Mistral, pero puede tardar más de 60s en prompts grandes. Por eso se usa timeout explícito y control de tokens.

Variables recomendadas:

```bash
MISTRAL_VISION_TIMEOUT_MS=180000
MISTRAL_VISION_MAX_TOKENS=6000
MISTRAL_VISION_ESTIMATED_COMPLETION_TOKENS=6000
MISTRAL_VISION_EXTRA_ESTIMATED_TOKENS=2500
```

Para limitar contexto estructural:

```bash
PENPOT_VALIDATOR_MAX_CONTEXT_NODES=80
PENPOT_VALIDATOR_CONTEXT_CHARS=9000
```

Para debug de export/payload:

```bash
PENPOT_VALIDATOR_DEBUG_EXPORT=1
PENPOT_DEBUG_OUT=/tmp/penpot_debug
```

Archivos esperados:

```text
/tmp/penpot_debug/validator_export.png
/tmp/penpot_debug/validator_export.png.data_url.txt
/tmp/penpot_debug/validator_visual_prompt.txt
/tmp/penpot_debug/validator_design_context.json
/tmp/penpot_debug/validator_payload_debug.json
```

## Prueba directa de Mistral

Probar el payload real desde terminal:

```bash
curl -v \
  --connect-timeout 10 \
  --max-time 180 \
  -w "\nHTTP=%{http_code} STARTTRANSFER=%{time_starttransfer}s TOTAL=%{time_total}s SIZE_UPLOAD=%{size_upload}\n" \
  https://api.mistral.ai/v1/chat/completions \
  -H "Authorization: Bearer $MISTRAL_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/penpot_debug/validator_payload_debug.json
```

Interpretación rápida:

```text
HTTP 200 y tarda >60s -> subir timeout del cliente
HTTP 400              -> payload mal formado
HTTP 401/403          -> API key/permisos
HTTP 429              -> rate limit
ReadTimeout           -> timeout o payload demasiado pesado
```

## Scope recomendado

Para diseños pesados, validar por selección o frame específico:

```bash
PENPOT_VALIDATOR_SHAPE_ID=selection
```

Ajustes rápidos:

```text
Pantalla chica:
  nodes 40-80
  max_tokens 3000
  timeout 120s

Pantalla mediana:
  nodes 80
  max_tokens 6000
  timeout 180s

Pantalla pesada:
  validar por frame/selección
  nodes 40-80
  timeout 180-240s
```

## Estado actual

```text
1. validate_only básico ✅
2. export_shape PNG desde Penpot ✅
3. lectura estructural con execute_code read-only ✅
4. imagen + DESIGN_CONTEXT_JSON hacia Mistral Vision ✅
5. mapeo visual a capas reales con node_ref ✅
6. generación de auto_fix_plan seguro ✅
7. validate_and_fix ejecuta renombrado en Penpot ✅
8. verify_auto_fix_plan_applied con timestamp ✅
9. timeout explícito para Mistral Vision ✅
```

## Regla de arquitectura

```text
Validator = evalúa el diseño actual
Fixer     = aplica un subconjunto seguro del reporte
Verifier  = confirma si ese subconjunto se aplicó
```

No mezclar esas responsabilidades.
