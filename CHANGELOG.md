# Changelog

Todos los cambios notables de **claudit** se documentan en este archivo.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el proyecto sigue [Versionado Semántico](https://semver.org/lang/es/). La versión
del plugin vive en `plugin/claude-code/.claude-plugin/plugin.json` y se espeja en
`.claude-plugin/marketplace.json`; un GitHub Action la incrementa automáticamente
(ver **Versionado** en el README).

## [1.1.0] - 2026-07-20

### Added

- Auto-limpieza del cache: en cada corrida, claudit borra las versiones **huérfanas**
  que Claude Code deja en `~/.claude/plugins/cache/claudit/claudit/<version>/` al
  actualizar, en vez de esperar los 7 días del período de gracia. Preserva la versión
  **instalada** (según `installed_plugins.json`) y la que corre la **sesión viva** —
  borrar esa última rompería la sesión y en Windows falla por archivo en uso. Cross-OS
  vía `shutil` (sin detectar el OS ni shellear `rm`); silenciosa ante cualquier fallo y
  no borra nada si no puede confirmar qué preservar.

## [1.0.1] - 2026-07-19

### Fixed

- El hook que auto-genera el reporte HTML pasa del evento `Stop` (dispara al final de
  **cada turno**, dejando un HTML por respuesta y re-parseando el transcript entero
  cada vez) a `SessionEnd` (**una sola vez, al cerrar la sesión**). Evita la
  duplicación de reportes y el trabajo redundante turno a turno.

## [1.0.0] - 2026-07-18

Primera versión pública de **claudit** — un plugin de Claude Code que mide el
**cache-read REAL** de la sesión activa (no estimado), leyendo el transcript `.jsonl`
que Claude Code escribe en `~/.claude/projects/`.

### Added

- **Plugin de Claude Code instalable vía marketplace.** `.claude-plugin/marketplace.json`
  en la raíz + el plugin en `plugin/claude-code/`. Instalación:
  `/plugin marketplace add SebasCouto/claudit` + `/plugin install claudit@claudit`
  (o `claude plugin ...` desde la terminal).
- **Slash command `/claudit:claudit`** que corre el reporte on-demand y muestra su
  salida verbatim. Acepta `--detalle` (una fila por inferencia) y un
  `<archivo.jsonl | uuid>` para auditar una sesión puntual. **Siempre** genera además
  el reporte HTML.
- **Motor `claudit.py`, cero dependencias**, que lee el transcript `.jsonl` de la
  sesión y reporta, con números reales de la API:
  - Totales de `cache-read` / `cache-write` / `input` / `output` y costo estimado en USD.
  - **Rampa** de crecimiento del cache-read (primera → última inferencia, el `Nx`).
  - **Composición del prefijo** re-leído cada turno: setup fijo, prompt (input), tool
    results (por tipo: `Read`, `Bash`, …) y las respuestas del modelo.
  - Desglose del setup fijo por componente (CLAUDE.md global/proyecto por sección,
    `skill_listing`, hooks) con **columna de palanca (`*`)** = ahorro potencial por
    turno si se recortara cada pieza.
- **Reporte HTML (`claudit --html`)**: self-contained (dark-only, cero dependencias,
  charts dibujados en `<canvas>`) con la **composición del cache-read**, el
  **cache-write por inferencia** (con hora UTC en el tooltip), KPI cards, **tooltips
  explicativos** por barra y por KPI, y una sección **Prioridad de mejora**. Cada
  gráfico muestra su **% del total de tokens** de la sesión. Cada reporte se escribe con
  su propio nombre `report-<fecha>_<hora>.html`.
- **Salida a un directorio auto-ignorante** `.claudit/` (con su propio `.gitignore` = `*`):
  el reporte nunca aparece en el working tree del repo del usuario, sin tocar su `.gitignore`.
- **Generación automática del HTML al cerrar la sesión** vía Stop hook
  (`plugin/claude-code/hooks/hooks.json`).
- **Alerta de nueva versión** antes de correr la auditoría (chequeo diario cacheado,
  opt-out con `CLAUDIT_NO_UPDATE_CHECK`).
- **Resolución de proyecto portable:** el script mide el repo donde estás parado
  (`$CLAUDE_PROJECT_DIR` cuando corre como plugin, o el `cwd`), no la carpeta donde
  vive el script — así funciona instalado centralizado en cualquier repo.
- **CLI standalone** (sin Claude Code): `python3 plugin/claude-code/claudit.py`.
- **Versionado automático** vía GitHub Action (`.github/workflows/version-bump.yml`):
  cuando cambios que tocan `plugin/claude-code/` llegan a `main`, sube el patch en los
  dos manifests. Motor único del bump: `scripts/bump_version.py` (también sirve para
  `minor`/`major` a mano).
- **Identidad visual**: favicon (barras cyan) embebido en el reporte + `assets/favicon.svg`,
  banner de portada, hero de terminal y capturas por funnel en el README.
- **README** con About (por qué el modelo es *stateless* y re-lee todo el prefijo),
  instalación por dos vías, sección de actualización y el tip de *reload window*.
- **LICENSE MIT** y `.gitignore`.

### Notas de alcance

- **Claude-only, a propósito.** claudit está acoplado al transcript de Claude Code y
  al modelo de caching de Anthropic (`cache_read` 0.1x + `cache_write` 1.25x). No
  funciona con Codex u otros harnesses (otra ubicación, otro schema de `usage`, sin
  `cache_write`). Sí funciona con cualquier modelo de Claude editando el dict `PRECIO`.
- **claudit da visibilidad, no recetas.** Muestra qué consume tokens y cuánto; qué
  hacer con esa información es decisión del usuario. Es un medidor, no un optimizador.
- **Total real, reparto estimado.** El total de cache-read es de la API (real); el
  reparto interno del prefijo se estima del contenido del transcript y se declara como tal.
