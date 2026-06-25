# Documentación del agente Penpot + LangGraph + MCP

## Objetivo general

Estamos construyendo un agente en LangGraph conectado a Penpot mediante MCP. El objetivo es permitir workflows de diseño, validación y corrección automática de pantallas UI para que estén listas para handoff frontend.

La arquitectura busca separar responsabilidades:

```text
Builder  -> diseña o modifica en Penpot
Validator -> inspecciona y valida, solo lectura
Fixer -> convierte el reporte del validador en instrucciones para que el builder corrija
```

---

# Stack actual

## Frameworks y librerías

```text
LangGraph
LangChain
langchain-mistralai
langchain-mcp-adapters
httpx
typing-extensions
python-dotenv
```

## Modelo

Se está usando Mistral:

```python
ChatMistralAI(
    model="mistral-large-latest",
    temperature=0,
    max_retries=2,
    rate_limiter=shared_rate_limiter,
)
```

El modelo puede configurarse con:

```env
MISTRAL_MODEL=mistral-large-latest
MISTRAL_REQUESTS_PER_SECOND=0.6
MISTRAL_TOKENS_PER_MINUTE=20000
TOKEN_WINDOW_SECONDS=60
```

## MCP

Se usa `MultiServerMCPClient` conectado a Penpot:

```python
MultiServerMCPClient(
    {
        "penpot": {
            "transport": "http",
            "url": os.getenv("PENPOT_MCP_KEY"),
        }
    }
)
```

La variable `PENPOT_MCP_KEY` contiene la URL del servidor MCP de Penpot.

---

# Estructura del proyecto

```text
.
├── langgraph.json
├── pyproject.toml
├── src
│   └── agent
│       ├── graph.py
│       ├── __init__.py
│       └── utils
│           ├── __init__.py
│           ├── llm_control.py
│           ├── agents
│           │   ├── __init__.py
│           │   └── design_validator.py
│           ├── skills
│           │   └── design_validator.md
│           └── tool_policies
│               ├── penpot_read_tools.md
│               └── penpot_write_tools.md
```

---

# Configuración del paquete

En `pyproject.toml` se recomendó usar discovery automático:

```toml
[tool.setuptools.packages.find]
where = ["src"]
include = ["agent*"]

[tool.setuptools.package-data]
"agent" = [
    "utils/skills/*.md",
    "utils/tool_policies/*.md",
    "py.typed",
]
```

Si `py.typed` no existe, crear:

```bash
touch src/agent/py.typed
```

Import correcto del validador desde `graph.py`:

```python
from agent.utils.agents.design_validator import validator_graph
```

---

# Arquitectura general del grafo principal

El grafo principal está en:

```text
src/agent/graph.py
```

Tiene estos nodos conceptuales:

```text
prepare_input
llm_call
tool_node
run_validator
fix_design
```

## Responsabilidades

### `prepare_input`

Normaliza la acción recibida, prepara el estado inicial y decide si debe arrancar por builder o validator.

### `llm_call`

Es el nodo del builder. Llama al modelo con herramientas de Penpot habilitadas. Puede crear o modificar diseño usando tools de escritura como `execute_code`.

### `tool_node`

Ejecuta las tools pedidas por el builder.

### `run_validator`

Ejecuta el subgrafo del validador read-only.

### `fix_design`

No ejecuta tools directamente. Convierte el `validation_report` en un prompt concreto para que el builder corrija el diseño.

---

# Acciones disponibles

Se definieron cinco acciones principales:

```text
build
validate_only
build_and_validate
validate_and_fix
build_validate_and_fix
```

## `build`

Solo diseña o modifica en Penpot.

```json
{
  "action": "build",
  "changeme": "Crea una pantalla de login móvil."
}
```

Flujo:

```text
START -> prepare_input -> llm_call -> tool_node -> llm_call -> END
```

---

## `validate_only`

Solo valida el diseño actual. No modifica nada.

```json
{
  "action": "validate_only",
  "changeme": "Valida el diseño actual de Penpot para handoff frontend."
}
```

Flujo:

```text
START -> prepare_input -> run_validator -> END
```

---

## `build_and_validate`

Diseña o modifica y luego valida. No corrige automáticamente.

```json
{
  "action": "build_and_validate",
  "changeme": "Crea una pantalla de login móvil lista para handoff frontend."
}
```

Flujo:

```text
START -> prepare_input -> llm_call -> tool_node -> llm_call -> run_validator -> END
```

---

## `validate_and_fix`

Valida un diseño existente y, si falla, intenta corregirlo automáticamente.

```json
{
  "action": "validate_and_fix",
  "changeme": "Valida el diseño actual y corrige problemas críticos.",
  "max_fix_iterations": 2
}
```

Flujo:

```text
START
  -> prepare_input
  -> run_validator
  -> si passed=false: fix_design
  -> llm_call
  -> tool_node
  -> llm_call
  -> run_validator
  -> END si passed=true o si llega a max_fix_iterations
```

---

## `build_validate_and_fix`

Diseña, valida y corrige automáticamente si hace falta.

```json
{
  "action": "build_validate_and_fix",
  "changeme": "Crea una pantalla de registro móvil lista para handoff frontend.",
  "max_fix_iterations": 2
}
```

Flujo:

```text
START
  -> prepare_input
  -> llm_call
  -> tool_node
  -> llm_call
  -> run_validator
  -> si passed=false: fix_design
  -> llm_call
  -> tool_node
  -> llm_call
  -> run_validator
  -> END
```

---

# Diferencia entre validator y fixer

## Validator

El validator es read-only.

No debe modificar Penpot.

Debe inspeccionar la pantalla actual y devolver un JSON con:

```json
{
  "passed": false,
  "score": 0,
  "status": "not_ready",
  "summary": "",
  "checks": {},
  "issues": [],
  "required_fixes": [],
  "suggested_structure": "",
  "developer_notes": [],
  "can_be_sent_to_development": false
}
```

## Fixer

El fixer no modifica directamente.

El fixer toma el `validation_report` y genera un prompt para que el builder corrija usando herramientas de escritura.

Ejemplo de prompt generado por el fixer:

```text
Corrige el diseño actual de Penpot usando el siguiente reporte de validación.
Prioriza problemas critical y high.
No borres elementos existentes salvo que el reporte lo indique.
Mantén la intención visual original.
```

---

# Control de tokens y requests

Se decidió eliminar los nodos explícitos:

```text
token_gate
token_accounting
```

Ahora el control vive dentro de un wrapper central:

```text
src/agent/utils/llm_control.py
```

## Regla de arquitectura

Ningún nodo debe llamar directamente:

```python
llm.ainvoke(...)
llm_with_tools.ainvoke(...)
```

Todos los nodos que llaman al modelo deben usar:

```python
metered_ainvoke(...)
```

## Qué hace `metered_ainvoke`

```text
1. Estima tokens antes de llamar al modelo.
2. Espera si se alcanzó el límite por ventana.
3. Ejecuta la llamada real al LLM.
4. Extrae usage_metadata o response_metadata.token_usage.
5. Registra tokens consumidos.
6. Devuelve ai_message y métricas.
```

## Métricas guardadas en estado

```text
input_tokens
output_tokens
total_tokens
token_window_used
token_gate_waited
```

---

# Estado del grafo principal

El input permite:

```python
class InputState(TypedDict, total=False):
    changeme: str
    action: Action
    max_fix_iterations: int
```

El output permite:

```python
class OutputState(TypedDict, total=False):
    response: str | None

    validation_report: dict[str, Any] | str | None
    passed: bool | None
    score: int | None
    status: str | None

    fix_iterations: int
    max_fix_iterations: int

    input_tokens: int
    output_tokens: int
    total_tokens: int
    token_window_used: int
    token_gate_waited: bool
```

El estado interno incluye además:

```python
skip_validation
messages
```

---

# Condiciones de parada del loop

El sistema puede detenerse por varias condiciones.

## Builder loop

El builder se detiene cuando el último `AIMessage` ya no tiene `tool_calls`.

```text
llm_call -> tool_node -> llm_call -> ...
```

Para evitar loops infinitos, se recomendó agregar:

```text
max_tool_iterations
tool_iterations
```

Todavía está pendiente implementarlo en el builder principal.

## Validator loop

El validator puede entrar en:

```text
validator_llm_call -> validator_tool_node -> validator_llm_call -> ...
```

Se recomendó limitarlo con:

```python
MAX_VALIDATOR_TOOL_ITERATIONS = 2
```

Para debug, se quiere que después de 2 iteraciones el validator genere un reporte final.

## Fix loop

El loop de corrección automática se detiene cuando:

```text
passed == true
```

o cuando:

```text
fix_iterations >= max_fix_iterations
```

Default recomendado:

```json
{
  "max_fix_iterations": 2
}
```

---

# `passed`

Actualmente, para debug, `passed` debe quedarse simple:

```python
def validation_passed(state: OverallState) -> bool:
    return bool(state.get("passed", False))
```

`passed` no lo calcula LangGraph automáticamente. Sale del JSON generado por el validator.

El flujo es:

```text
validator_llm_call genera JSON
parse_json_report lo convierte a dict
extract_output_from_report toma report["passed"]
run_validator lo pasa al grafo principal
route_after_validator decide si termina o corrige
```

Más adelante se quiere endurecer con una función más estricta:

```python
def validation_passed(state: OverallState) -> bool:
    report = state.get("validation_report")

    if not isinstance(report, dict):
        return False

    passed = bool(report.get("passed", False))
    score = int(report.get("score", 0) or 0)
    status = report.get("status")
    can_be_sent = bool(report.get("can_be_sent_to_development", False))

    raw_issues = report.get("issues", [])
    issues = raw_issues if isinstance(raw_issues, list) else []

    blocking_issues = [
        issue
        for issue in issues
        if isinstance(issue, dict)
        and issue.get("severity") in {"critical", "high"}
    ]

    return (
        passed
        and can_be_sent
        and status == "ready"
        and score >= 85
        and len(blocking_issues) == 0
    )
```

Pero todavía no debe activarse hasta que el validator genere JSON final correctamente.

---

# Problema actual detectado

El validator no estaba terminando porque seguía generando `tool_calls`.

Además, se observó un tool call mal formado:

```json
{
  "type": "tool_call",
  "id": "Ml6rREM4M",
  "name": "tools ... inspeccionaré la estructura ...",
  "args": {
    "type": "function",
    "function": {
      "name": "export_shape",
      "parameters": {}
    }
  }
}
```

Esto es incorrecto.

El tool call correcto debería tener un nombre real de tool, por ejemplo:

```text
export_shape
```

y argumentos concretos, por ejemplo:

```json
{
  "shapeId": "page",
  "format": "svg",
  "mode": "shape"
}
```

El problema es que el modelo está confundiendo:

```text
definir una tool
```

con:

```text
usar una tool existente
```

Por eso se necesita simplificar temporalmente la skill del validator.

---

# Skill temporal de debug para validator

Se decidió usar una skill menos estricta para debug.

Objetivos de esta skill:

```text
- Que use máximo 2 rondas de tools.
- Que no intente definir herramientas.
- Que use herramientas existentes con argumentos concretos.
- Que si no puede inspeccionar bien, genere JSON igual.
- Que use passed de forma simple.
- Que priorice terminar con un reporte.
```

Archivo:

```text
src/agent/utils/skills/design_validator.md
```

Contenido temporal recomendado:

````markdown
# Skill: Design Validator Debug para Penpot

## Rol

Eres un agente validador de diseño UI conectado a Penpot mediante MCP.

Tu tarea es inspeccionar la pantalla actual y devolver un reporte JSON válido sobre su preparación para handoff frontend.

Este modo es de debug. Prioriza generar un reporte útil y terminar la ejecución.

## Política de herramientas

Eres un agente read-only.

Puedes usar herramientas de lectura o inspección disponibles, por ejemplo:

```text
high_level_overview
penpot_api_info
export_shape
````

No puedes modificar Penpot.

No uses herramientas que creen, editen, borren, renombren, muevan o actualicen elementos.

Herramientas prohibidas:

```text
execute_code
create
edit
update
delete
remove
rename
move
import
write
set
```

## Protocolo de herramientas

Las herramientas ya están definidas por el sistema. No debes definir herramientas nuevas ni devolver schemas de funciones.

Cuando necesites usar una herramienta, usa una herramienta existente con argumentos concretos.

No devuelvas estructuras como:

```json
{
  "type": "function",
  "function": {
    "name": "export_shape",
    "parameters": {}
  }
}
```

Eso es incorrecto.

Ejemplos de argumentos correctos:

Para `penpot_api_info`:

```json
{
  "type": "Page"
}
```

Para `export_shape`:

```json
{
  "shapeId": "page",
  "format": "svg",
  "mode": "shape"
}
```

Para exportar la selección actual:

```json
{
  "shapeId": "selection",
  "format": "svg",
  "mode": "shape"
}
```

Si una herramienta falla o no sabes qué herramienta usar, no sigas intentando indefinidamente. Genera el reporte final con la información disponible.

## Límite de inspección

Haz como máximo 2 rondas de herramientas.

Después de obtener información suficiente o si ya intentaste inspeccionar, genera el reporte final.

No busques perfección. Este modo es para debug.

## Qué debes evaluar

Evalúa de forma general:

```text
- existencia de una pantalla o frame principal
- claridad de nombres de capas
- estructura para handoff frontend
- uso aparente de componentes
- consistencia visual
- layout y espaciado
- accesibilidad básica
```

Si no puedes verificar un punto, usa `status: "unknown"` en ese check.

## Criterio simple para passed

En este modo de debug, `passed` puede ser `true` si:

```text
- la pantalla parece entendible para desarrollo
- no detectas problemas graves evidentes
- el score es 70 o superior
```

Usa `passed: false` si:

```text
- no puedes inspeccionar el diseño
- hay problemas graves de estructura
- no hay frame/pantalla clara
- la información disponible es insuficiente
- el score es menor a 70
```

No seas demasiado estricto en este modo.

## Score

Calcula un score de 0 a 100 aproximado.

Guía:

```text
90-100: listo o casi listo
70-89: usable para desarrollo con ajustes menores
50-69: necesita correcciones importantes
0-49: no listo o no se pudo validar
```

## Status global

Usa uno de estos:

```text
ready
needs_minor_fixes
needs_major_fixes
not_ready
```

Reglas simples:

```text
ready: score >= 85 y sin problemas graves
needs_minor_fixes: score entre 70 y 84
needs_major_fixes: score entre 50 y 69
not_ready: score menor a 50 o información insuficiente
```

## Formato obligatorio de salida

Debes responder siempre únicamente con JSON válido.

No agregues Markdown.
No agregues explicación fuera del JSON.
No uses bloques de código.

Usa exactamente esta estructura:

{
"passed": false,
"score": 0,
"status": "not_ready",
"summary": "",
"checks": {
"screen_structure": {
"status": "unknown",
"score": 0,
"notes": []
},
"layer_naming": {
"status": "unknown",
"score": 0,
"notes": []
},
"componentization": {
"status": "unknown",
"score": 0,
"notes": []
},
"layout_spacing": {
"status": "unknown",
"score": 0,
"notes": []
},
"accessibility": {
"status": "unknown",
"score": 0,
"notes": []
},
"frontend_handoff": {
"status": "unknown",
"score": 0,
"notes": []
}
},
"issues": [],
"required_fixes": [],
"suggested_structure": "",
"developer_notes": [],
"can_be_sent_to_development": false
}

## Valores permitidos para checks

Cada check puede usar:

```text
pass
warning
fail
unknown
```

## Valores permitidos para issues

Cada issue debe usar una severidad:

```text
low
medium
high
critical
```

## Instrucción final

Tu prioridad es terminar con un JSON válido.

Si tienes información incompleta, no sigas pidiendo herramientas muchas veces. Devuelve un reporte con:

```json
{
  "passed": false,
  "status": "not_ready",
  "can_be_sent_to_development": false
}
```

y marca los checks que no pudiste verificar como `unknown`.

````

---

# Política de tools

Hay dos archivos:

```text
penpot_read_tools.md
penpot_write_tools.md
````

## `penpot_read_tools.md`

Para validator.

Debe permitir tools de lectura como:

```markdown
## allow

- high_level_overview
- penpot_api_info
- export_shape

## allow_keywords

- overview
- info
- export
- inspect
- read
- get
- list
```

## `penpot_write_tools.md`

Para bloquearlas en validator y permitirlas en builder.

Ejemplo:

```markdown
## deny

- execute_code

## deny_keywords

- create
- edit
- update
- delete
- remove
- rename
- move
- import
- write
- set
```

El builder interpreta esas write tools como permitidas, pero el validator las interpreta como prohibidas.

---

# Debug en LangGraph Studio

Para ver qué devuelve el validador:

## Dentro del subgrafo validator

Buscar el último:

```text
validator_llm_call
```

Luego abrir pestaña:

```text
Output
```

Si todavía aparece algo como:

```json
[
  {
    "type": "tool_call",
    "name": "..."
  }
]
```

entonces aún no es el reporte final.

El reporte final debe verse como:

```json
{
  "validation_report": {
    "passed": false,
    "score": 65,
    "status": "needs_major_fixes"
  },
  "passed": false,
  "score": 65,
  "status": "needs_major_fixes"
}
```

## En el grafo principal

Buscar:

```text
run_validator -> Output
```

Ahí debe aparecer el resultado limpio del subgrafo:

```json
{
  "validation_report": {},
  "passed": false,
  "score": 0,
  "status": "not_ready"
}
```

---

# Logs recomendados para debug

En `design_validator.py`:

```python
print(
    "VALIDATOR AI TOOL CALLS:",
    json.dumps(
        getattr(ai_message, "tool_calls", []),
        ensure_ascii=False,
        default=str,
        indent=2,
    ),
)
```

Cuando no haya tool calls:

```python
print("VALIDATOR FINAL RAW:", ai_message.content)
```

En `graph.py`, dentro de `run_validator`:

```python
print(
    "VALIDATOR GRAPH RESULT:",
    json.dumps(validation_result, ensure_ascii=False, default=str, indent=2),
)
```

---

# Próximos pasos recomendados

## Paso 1

Reemplazar temporalmente `design_validator.md` por la skill debug.

## Paso 2

Configurar:

```python
MAX_VALIDATOR_TOOL_ITERATIONS = 2
```

## Paso 3

Mantener temporalmente:

```python
def validation_passed(state: OverallState) -> bool:
    return bool(state.get("passed", False))
```

## Paso 4

Ejecutar:

```json
{
  "action": "validate_only",
  "changeme": "Valida el diseño actual de Penpot para handoff frontend."
}
```

## Paso 5

Ver en LangGraph Studio:

```text
último validator_llm_call -> Output
```

o:

```text
run_validator -> Output
```

## Paso 6

Confirmar si el validator ya devuelve JSON final.

## Paso 7

Cuando ya funcione, endurecer gradualmente:

```text
- Skill más estricta.
- Passing criteria formal.
- validation_passed determinístico.
- max_tool_iterations para builder.
- force_final_report para validator si llega al máximo de tools.
```

---

# Decisiones importantes tomadas

## Se elimina `create_agent`

Antes el código usaba:

```python
create_agent(model=llm, tools=tools, system_prompt=...)
```

Se decidió reemplazarlo por grafo explícito con:

```text
llm_call
tool_node
should_continue
```

Motivo:

```text
más control de loops
más control de tokens
mejor debugging en LangGraph Studio
mejor separación entre builder, validator y fixer
```

## El validator es subgrafo

El validator vive en:

```text
src/agent/utils/agents/design_validator.py
```

y se invoca desde el nodo `run_validator` del grafo principal.

## El validator lee su skill desde Markdown

Archivo:

```text
src/agent/utils/skills/design_validator.md
```

Esto permite iterar comportamiento sin tocar tanto código.

## Las policies de tools viven en Markdown

Archivos:

```text
src/agent/utils/tool_policies/penpot_read_tools.md
src/agent/utils/tool_policies/penpot_write_tools.md
```

---

# Errores tratados durante el desarrollo

## Error 429 de Mistral

Se agregó manejo de:

```python
HTTPStatusError
```

Si status code es `429`, el nodo devuelve un `AIMessage` sin `tool_calls` y `skip_validation=True`.

## Error 400 de Mistral

Error:

```text
Expected last role User or Tool but got assistant
```

Causa:

```text
se estaba llamando a Mistral con historial terminando en AIMessage
```

Solución:

```text
prepare_input agrega HumanMessage
llm_call valida que el último mensaje no sea AIMessage
tool_node agrega ToolMessage antes de volver a llamar al modelo
```

## Problemas de import

Se recomendó importar así:

```python
from agent.utils.agents.design_validator import validator_graph
```

y configurar `pyproject.toml` con:

```toml
[tool.setuptools.packages.find]
where = ["src"]
include = ["agent*"]
```

---

# Estado actual

No logro recibir imganes ya sea en svg o base64 lo quesea para poder ocuparlo con mi llm, por el momento tengo el siguiente script en `./scripts/test_penpot_export_shape.py` que es con el que estoy debugeando la imagen. 

---

# Resumen ejecutivo

El sistema final deseado será:

```text
Usuario elige action
  -> build / validate / fix workflow
  -> builder modifica Penpot cuando corresponde
  -> validator inspecciona read-only
  -> fixer convierte reporte en instrucciones
  -> builder corrige
  -> validator confirma
  -> termina por passed=true o max_fix_iterations
```

La prioridad inmediata es estabilizar el validator para que siempre produzca un JSON final usable.

