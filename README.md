# claudit

> Audit the **real** cache-read cost of your Claude Code sessions — see where your tokens go and what to trim.

`claudit` (Claude + audit) es un plugin de Claude Code que mide el **cache-read REAL**
de tu sesión activa —no estimado— leyendo el transcript `.jsonl` que Claude Code
escribe en `~/.claude/projects/`. Te dice, con números de la propia API, cuánto se
re-lee en CADA turno, cómo se compone ese prefijo y **qué pieza recortar para gastar
menos tokens**.

## About

Cada inferencia de Claude Code re-lee el prefijo **entero** de tu contexto como
`cache-read`. Ese re-leído es el que **domina el gasto** de una sesión larga: no es
tu último prompt, es todo lo que se arrastra turno a turno (system, tool-defs,
CLAUDE.md, skills, hooks, tus prompts, resultados de herramientas y mis respuestas).

`claudit` abre esa caja negra:

- **Total REAL** de cache-read / cache-write / input / output, tomado del `usage`
  que reporta la API en cada inferencia (no una estimación).
- **Rampa**: cómo crece el cache-read de la primera a la última inferencia (el `Nx`).
- **Composición del prefijo**: qué parte del re-leído es setup fijo, tus prompts,
  tool results (por tipo: `Read`, `Bash`, …) y mis respuestas.
- **Columna de palanca (`*`)**: cuántos tokens/turno editables ahorrarías si
  recortás cada pieza — `***` ≥ 1000 tok/turno, `**` ≥ 300, `*` < 300, sin marca =
  base del harness (no editable).

La lente en una frase: **cuanto más escribís en el hilo (archivos inline, prompts
largos, sin skills/distill/sub-agentes), más grande el prefijo → más cache-read en
cada turno siguiente.** claudit te muestra exactamente dónde.

## Instalación

**Dentro de Claude Code** (en el input de Claude Code, no en la terminal):

```
/plugin marketplace add SebasCouto/claudit
/plugin install claudit@claudit
```

**O desde la terminal**, con el binario `claude`:

```bash
claude plugin marketplace add SebasCouto/claudit
claude plugin install claudit@claudit
```

Una vez instalado, el comando `/claudit:claudit` queda disponible en **todos** tus
repos — tipeás `/` y aparece en el autocomplete:

![El comando /claudit:claudit en el autocomplete de Claude Code](assets/slash-command.png)

> **¿No aparece el comando recién instalado?** Los slash-commands de un plugin se
> registran al **arrancar** la sesión. Recargá: en VSCode `Cmd+Shift+P → Reload
> Window`; en la CLI, salí y reabrí `claude`. Después, tipeando `/` en el input
> tenés que ver `claudit`.

## Uso

```
/claudit:claudit             # resumen + composición del cache-read del repo actual
/claudit:claudit --detalle   # + una fila por inferencia (la rampa completa)
```

> El comando queda con namespace `plugin:comando` (por eso `claudit:claudit`).
> Tipeá `/claudit` y el autocomplete lo completa.

El plugin resuelve solo qué proyecto medir: usa `$CLAUDE_PROJECT_DIR` (el repo donde
estás), así que aunque el plugin viva centralizado, mide **tu** sesión actual.

### CLI standalone (sin Claude Code)

Es un único archivo Python, cero dependencias. Corré desde la raíz del repo que
quieras auditar:

```bash
python3 plugin/claude-code/claudit.py             # sesión más reciente del cwd
python3 plugin/claude-code/claudit.py --detalle
python3 plugin/claude-code/claudit.py <uuid|archivo.jsonl>   # una sesión puntual
```

## Alcance

- **Claude-only, a propósito.** claudit está acoplado al formato de transcript de
  Claude Code y al modelo de caching de Anthropic (cache-read 0.1x + cache-write
  1.25x). No funciona con Codex u otros harnesses: usan otra ubicación, otro schema
  de `usage` y otro modelo de caché (sin `cache_write`). El nombre lo posee.
- **Cualquier modelo de Claude.** Opus, Sonnet, Haiku, Fable — mismo formato. Si
  cambia el pricing, editás un solo dict (`PRECIO`) arriba del script.
- **Total real, reparto estimado.** El total de cache-read es de la API (real); el
  reparto interno del prefijo se estima del contenido del transcript, calibrado
  contra el prefijo real, y se declara como tal en la salida.

## Desarrollo

La versión del plugin vive en dos manifests que deben coincidir:
`plugin/claude-code/.claude-plugin/plugin.json` y el entry del plugin en
`.claude-plugin/marketplace.json`. [scripts/bump_version.py](scripts/bump_version.py)
es la fuente única del bump y sube **ambos manifests en sync**.

**Automático, vía CI (ante cada PR).** Cuando un PR se mergea a `main` y tocó
`plugin/claude-code/`, el workflow
[.github/workflows/version-bump.yml](.github/workflows/version-bump.yml) sube el patch
en los dos manifests y lo commitea de vuelta a `main` — así `claude plugin update`
siempre ve la versión nueva. **No requiere setup de los contribuidores.** (Si protegés
`main` con required-PR, dale al bot permiso de push o usá un PAT.)

**Bump manual** de `minor` / `major` cuando corresponda:

```bash
python3 scripts/bump_version.py minor   # o: major
```

## Licencia

[MIT](LICENSE) · made by [@SebasCouto](https://github.com/SebasCouto)
