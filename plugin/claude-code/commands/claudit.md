---
description: Audita el cache-read REAL de tu sesión de Claude Code — dónde se van los tokens y qué recortar para gastar menos.
argument-hint: "[--detalle] [<archivo.jsonl | uuid>]"
allowed-tools: ["Bash"]
---

# /claudit — auditoría de cache-read de la sesión

Ejecutá el script de claudit y mostrame su salida **tal cual**, dentro de un bloque
de código, sin resumirla, reordenarla ni reinterpretarla. Es un reporte de números
reales: cualquier edición lo falsea.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/claudit.py" $ARGUMENTS
```

Notas:
- `$ARGUMENTS` reenvía lo que el usuario haya pasado: `--detalle` (una fila por
  inferencia) y/o un `<archivo.jsonl | uuid>` para auditar una sesión puntual.
- El script resuelve solo qué proyecto medir: usa `$CLAUDE_PROJECT_DIR` o, si no
  está, el directorio actual. No hace falta pasarle la ruta del transcript.
- Después de mostrar la salida verbatim, y solo si aparecen componentes con palanca
  `***`, agregá UNA línea señalando los 2–3 de mayor ahorro por turno. Nada más.
