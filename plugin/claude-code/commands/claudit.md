---
description: Audita el cache-read REAL de tu sesión de Claude Code — dónde se van los tokens y qué recortar para gastar menos.
argument-hint: "[--detalle] [<archivo.jsonl | uuid>]"
allowed-tools: ["Bash", "AskUserQuestion"]
---

# /claudit — auditoría de cache-read de la sesión

Ejecutá el script y mostrame su salida **tal cual**, dentro de un bloque de código,
sin resumirla, reordenarla ni reinterpretarla. Es un reporte de números reales:
cualquier edición lo falsea.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/claudit.py" --html $ARGUMENTS
```

## Gate de versión (obligá a actualizar)

Si la **primera línea** de la salida es exactamente `UPDATE_REQUIRED`, el reporte
está **bloqueado** porque hay una versión más nueva publicada. En ese caso NO muestres
el bloque anterior como reporte; en su lugar:

1. Preguntá con `AskUserQuestion` (una sola pregunta, opciones **Yes** / **No**):
   *"Hay una versión nueva de claudit (ver líneas del script). ¿Actualizar ahora?"*
2. Según la respuesta:
   - **Yes** → ejecutá la actualización y NO generes el reporte:
     ```bash
     claude plugin marketplace update claudit && claude plugin update claudit@claudit
     ```
     Si termina OK, respondé **solo**: `Instalación exitosa, por favor reiniciá la sesión en Claude.`
     (Los hooks y el script nuevos toman con el reinicio; no intentes generar el reporte ahora.)
     Si el update falla, mostrá el error tal cual y ofrecé reintentar o correr con la versión actual.
   - **No** → generá el reporte con la versión actual, saltando el gate:
     ```bash
     python3 "${CLAUDE_PLUGIN_ROOT}/claudit.py" --html --force $ARGUMENTS
     ```
     y mostrá su salida verbatim (ver notas de abajo).

Si la primera línea NO es `UPDATE_REQUIRED`, esa salida ya es el reporte: mostrala directo.

## Notas del reporte

- **Siempre** se genera además el reporte **HTML**. Mostrá el texto verbatim y decime
  la ruta del HTML (la línea `Reporte HTML: ...`).
- `$ARGUMENTS` reenvía lo que el usuario haya pasado: `--detalle` (una fila por
  inferencia) y/o un `<archivo.jsonl | uuid>` para auditar una sesión puntual.
- El script resuelve solo qué proyecto medir: usa `$CLAUDE_PROJECT_DIR` o, si no
  está, el directorio actual. No hace falta pasarle la ruta del transcript.
- Después de mostrar la salida verbatim, y solo si aparecen componentes con palanca
  `***`, agregá UNA línea señalando los 2–3 de mayor ahorro por turno. Nada más.
