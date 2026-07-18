# Changelog

Todos los cambios notables de **claudit** se documentan en este archivo.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el proyecto sigue [Versionado Semántico](https://semver.org/lang/es/). La versión
del plugin vive en `plugin/claude-code/.claude-plugin/plugin.json` y se espeja en
`.claude-plugin/marketplace.json`; un GitHub Action la incrementa automáticamente
(ver **Versionado** más abajo).

## [Unreleased]

_Sin cambios pendientes._

## [1.0.1] - 2026-07-18

Primera versión publicada de claudit como plugin de Claude Code.

### Added

- **Plugin de Claude Code instalable vía marketplace.** Estructura espejada de Engram:
  `.claude-plugin/marketplace.json` en la raíz + el plugin en `plugin/claude-code/`.
  Instalación: `/plugin marketplace add SebasCouto/claudit` + `/plugin install claudit@claudit`
  (o `claude plugin ...` desde la terminal).
- **Slash command `/claudit:claudit`** que corre el reporte on-demand y muestra su
  salida verbatim. Acepta `--detalle` (una fila por inferencia) y un
  `<archivo.jsonl | uuid>` para auditar una sesión puntual.
- **Motor `claudit.py`, cero dependencias**, que lee el transcript `.jsonl` de la
  sesión y reporta, con números reales de la API:
  - Totales de `cache-read` / `cache-write` / `input` / `output` y costo estimado en USD.
  - **Rampa** de crecimiento del cache-read (primera → última inferencia, el `Nx`).
  - **Composición del prefijo** re-leído cada turno: setup fijo, tus prompts, tool
    results (por tipo: `Read`, `Bash`, …) y las respuestas del modelo.
  - Desglose del setup fijo por componente (CLAUDE.md global/proyecto por sección,
    `skill_listing`, hooks) con **columna de palanca (`*`)** = ahorro potencial por
    turno si se recortara cada pieza.
- **Resolución de proyecto portable:** el script mide el repo donde estás parado
  (`$CLAUDE_PROJECT_DIR` cuando corre como plugin, o el `cwd`), no la carpeta donde
  vive el script — así funciona instalado centralizado en cualquier repo.
- **CLI standalone** (sin Claude Code): `python3 plugin/claude-code/claudit.py`.
- **Versionado automático** vía GitHub Action (`.github/workflows/version-bump.yml`):
  cuando cambios que tocan `plugin/claude-code/` llegan a `main`, sube el patch en los
  dos manifests. Motor único del bump: `scripts/bump_version.py` (también sirve para
  `minor`/`major` a mano).
- **README** con About, captura hero del output, instalación por dos vías y el tip de
  *reload window* (los slash-commands de plugin se registran al arrancar la sesión).
- **LICENSE MIT** y `.gitignore`.

### Changed

- El título del reporte muestra la marca **claudit** y el proyecto medido pasó a la
  línea de sesión: `Sesion: <uuid> (<proyecto>)`.

### Fixed

- `marketplace.json` adecuado al schema actual de Claude Code (2.1.92): sin `$schema`
  ni `description` en la raíz; la descripción del marketplace va en `metadata.description`.
  Validado con `claude plugin validate`.

### Alcance y notas

- **Claude-only, a propósito.** claudit está acoplado al transcript de Claude Code y
  al modelo de caching de Anthropic (`cache_read` 0.1x + `cache_write` 1.25x). No
  funciona con Codex u otros harnesses (otra ubicación, otro schema de `usage`, sin
  `cache_write`). Sí funciona con cualquier modelo de Claude editando el dict `PRECIO`.
- **claudit da visibilidad, no recetas.** Muestra qué consume tokens y cuánto; qué
  hacer con esa información es decisión del usuario. Es un medidor, no un optimizador.
- **Nota de versionado:** durante el desarrollo inicial la versión saltó de forma no
  intencional (residuo de staging de una prueba del bump); la primera versión
  efectivamente publicada es la 1.0.1. No hubo releases previos consumibles.
