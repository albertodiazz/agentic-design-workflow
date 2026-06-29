### `Validator Domain`

Lee el diseño, lo valida y produce un ValidationReport.
No modifica Penpot.
No crea capas.
No ejecuta fixes.

### `Fixer Domain`

Recibe el ValidationReport y genera FixPlans.
No modifica Penpot directamente.
No valida score.
No crea capas por sí mismo.

### `Executor / Builder Domain`

Recibe FixPlans y aplica cambios en Penpot.
Actualmente solo modifica capas existentes.
En futuras fases podrá crear grupos, componentes, variantes o anotaciones si el protocolo lo permite.

### `Canvas Fix Domain`

Corrige geometría y estilos visuales sobre known_targets:
posición, color, tipografía, stroke, fill.

_________

Semantic Fix Domain:
Pendiente de diseñar.
Debería encargarse de agrupación, componentización, tokens, estados interactivos, handoff y accesibilidad estructural.
