"""Constructores de transcripts sinteticos (anonimizados) para los tests de claudit.

Cada helper devuelve una entrada del .jsonl con la forma que escribe Claude Code.
Ningun fixture contiene datos reales: rutas, nombres y textos son inventados.
"""
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "plugin" / "claude-code"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))


def ts(minuto, segundo=0, hora=12, dia=10):
    return f"2026-11-{dia:02d}T{hora:02d}:{minuto:02d}:{segundo:02d}.000Z"


def usage(read=0, write=0, inp=1, out=10, w1h=None, w5m=None):
    u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
         "cache_creation_input_tokens": write}
    if w1h is not None or w5m is not None:
        u["cache_creation"] = {"ephemeral_1h_input_tokens": w1h or 0,
                               "ephemeral_5m_input_tokens": w5m or 0}
    return u


def asistente(u, cuando, model="claude-opus-5-5", bloques=None):
    return {"type": "assistant", "timestamp": cuando,
            "message": {"model": model, "usage": u, "content": bloques or [{"type": "text", "text": "ok"}]}}


def tool_use(name, tid, inp=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp or {}}


def prompt(texto, cuando):
    return {"type": "user", "timestamp": cuando, "message": {"role": "user", "content": texto}}


def tool_result(tid, texto, cuando, error=False, extra=None):
    e = {"type": "user", "timestamp": cuando,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": tid, "content": texto, "is_error": error}]}}
    if extra:
        e["toolUseResult"] = extra
    return e


def adjunto(att, cuando=None):
    return {"type": "attachment", "timestamp": cuando or ts(0), "attachment": att}


def forma(system_chars=4000, tools=None):
    """api-request-shape con system prompt y tool-defs de tamano conocido."""
    tools = tools or [{"name": "Bash", "description": "x" * 2000, "input_schema": {}}]
    return {"type": "api-request-shape", "timestamp": ts(0),
            "shape": {"system": [{"type": "text", "text": "s" * system_chars}], "tools": tools}}


def api_request(effort="high", model="claude-opus-5-5"):
    return {"type": "api-request", "timestamp": ts(0),
            "params": {"model": model, "output_config": {"effort": effort}}}


def escribir(tmp, lineas, nombre="sesion-aaaaaaaa.jsonl"):
    p = Path(tmp) / nombre
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lineas) + "\n", encoding="utf-8")
    return p
