#!/usr/bin/env python3
# claudit — audita el cache-read REAL de la sesion de Claude Code (no estimado).
# made by @SebasCouto.
#
# Lee el transcript .jsonl de la sesion (lo escribe Claude Code en ~/.claude/projects/)
# y saca de cada inferencia los tokens reales que reporto la API:
#   cache_read  -> prefijo cacheado, re-leido este turno (0.1x input)
#   cache_write -> prefijo nuevo escrito al cache (1.25x input TTL 5 min, 2x TTL 1 h)
#   input       -> input fresco no cacheado (el prompt del usuario + tool results del turno)
#   output      -> la respuesta del modelo (lo mas caro)
#
# Como plugin de Claude Code:   /claudit          /claudit --detalle          /claudit --html
# Como CLI standalone:          python3 claudit.py [--detalle] [--html [ruta]] [<archivo.jsonl | uuid>]
#
# --html [ruta]  ademas del reporte de texto, escribe un reporte HTML self-contained
#                (charts en canvas, dark/light) en `ruta`, o -sin ruta- en la subcarpeta
#                de la sesion: REPO/.claudit/<slot>_<timestamp>__<id8>/report-<ts>.html
#                (ring de 11 slots; ver carpeta_sesion). Crea REPO/.claudit/.gitignore
#                con "*" para que nada quede en el working tree del repo del usuario.
#
# El hook SessionStart lo dispara al INICIAR la sesion para dar un panorama del setup
# fijo desde el arranque. La sesion que arranca todavia no tiene inferencias, pero el
# setup fijo es identico entre sesiones, asi que se calibra contra la ultima inferencia
# disponible (una sesion previa cualquiera del proyecto: Claude Code escribe el .jsonl
# de CADA sesion, haya o no plugin). Unico borde sin panorama: la 1a sesion de la vida
# en un proyecto nuevo (cero transcripts previos) -> aparece en la 1a corrida a mano.
#
# Resuelve QUE proyecto medir por $CLAUDE_PROJECT_DIR (cuando corre como plugin)
# o, si no esta seteado, por el directorio actual (cwd). Asi mide el repo activo,
# no donde vive el script — funciona instalado en cualquier repo.
#
# El resumen desglosa, indentado bajo el cache-read acumulado, QUE compone ese
# prefijo re-leido: setup fijo (system+tool-defs+CLAUDE.md+skills+hooks), el
# prompt del usuario, resultados de herramientas (por tipo: Read/Bash/...) y las
# respuestas del modelo.
# El setup fijo se abre a su vez por componente y, dentro de cada CLAUDE.md, por
# seccion (header), con una columna de palanca = tokens/turno editables que
# ahorrarias al recortar esa pieza:
#     ***          ahorras >= 1000 tok en CADA turno si lo recortas
#     **           ahorras >= 300 tok/turno
#     *            ahorras < 300 tok/turno, pero es editable
#     (sin marca)  base del harness/tool-defs: no editable, no lo tocas
# El total del cache-read es REAL (API); el reparto interno se estima del contenido
# del transcript (calibrado contra el prefijo real) y se declara como tal.
#
# Corre igual en macOS/Linux/Windows. Motor unico, sin dependencias externas.
import base64
import calendar
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# USD por millon de tokens (input, output) por familia de modelo; edita si cambia el
# pricing. Se matchea por substring del id (`message.model`), en orden. Lo que no
# matchea (None, "<synthetic>") se cotiza como opus.
PRECIOS_MODELO = (("fable", 10.0, 50.0), ("opus", 5.0, 25.0),
                  ("sonnet", 3.0, 15.0), ("haiku", 1.0, 5.0))
# Multiplicadores sobre input: cache_read 0.1x; cache_write 1.25x con TTL 5 min y
# 2x con TTL 1 h (desglose en usage.cache_creation; sin desglose se asume 5 min).
MULT_CACHE = {"cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0}


_TTL_TXT = {"5m": "5 min", "1h": "1 h"}


def precios(model):
    """USD/MTok del modelo: input, output, cache_read, cache_write_5m, cache_write_1h."""
    m = (model or "").lower()
    entrada, salida = next(((i, o) for fam, i, o in PRECIOS_MODELO if fam in m),
                           PRECIOS_MODELO[1][1:])
    p = {"input": entrada, "output": salida}
    p.update({k: entrada * x for k, x in MULT_CACHE.items()})
    return p

# Conversion chars -> tokens para las piezas del setup que solo se tienen como texto.
# Se CALIBRA por sesion: chars medibles del setup / tokens reales de la 1a inferencia,
# acotado a [CPT_MIN, CPT_MAX] (texto con mucho codigo/JSON tokeniza mas denso que la
# prosa). Sin api-request-shape no hay con que calibrar y se usa CHARS_POR_TOKEN.
# --chars-por-token N lo fija a mano. Umbrales de la columna de palanca (*), en tok/turno.
CHARS_POR_TOKEN = 3.0
CPT_MIN, CPT_MAX = 2.0, 4.0
PALANCA_ALTA = 1000
PALANCA_MEDIA = 300
SECC_MINIMA = 200  # secciones de CLAUDE.md por debajo de esto se colapsan en 'resto'
SKILLS_TOP = 12  # skills del listado que se muestran una por una; el resto se agrupa
RING_SLOTS = 11    # subcarpetas .claudit/0_ .. .claudit/10_; la sesion N reusa el slot N % 11
                   # (la sesion 11 pisa la 0, la 12 pisa la 1, ...). El timestamp del nombre
                   # manda para "cual es la ultima"; el numero es el handle visual del slot.

# El repo cuya sesion medimos: $CLAUDE_PROJECT_DIR (lo inyecta Claude Code al
# correr como plugin) o, si no esta, el directorio actual. NO la carpeta del
# script — asi el plugin, aunque viva centralizado, mide el repo activo.
def proyecto_activo():
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).resolve() if env else Path.cwd().resolve()


REPO = proyecto_activo()
HOME = Path.home()


def codificar_ruta(ruta):
    """Nombre de carpeta que Claude Code usa en ~/.claude/projects/ para un repo:
    todo lo no alfanumerico pasa a '-'. En Windows 'C:\\Users\\x' -> 'C--Users-x'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(ruta))


def dir_proyecto():
    return HOME / ".claude" / "projects" / codificar_ruta(REPO)


def resolver_jsonl(arg):
    """Devuelve el .jsonl a leer: el argumento (path o uuid), o el mas reciente del repo."""
    proj = dir_proyecto()
    if arg:
        p = Path(arg)
        if p.is_file():
            return p
        cand = proj / f"{arg}.jsonl"
        if cand.is_file():
            return cand
        sys.exit(f"No encuentro la sesion: {arg}")
    if not proj.is_dir():
        sys.exit(f"No existe el directorio de proyecto: {proj}")
    sesiones = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not sesiones:
        sys.exit(f"No hay transcripts .jsonl en {proj}")
    return sesiones[-1]


def _tiene_inferencias(jsonl):
    """True si el transcript tiene al menos una inferencia con usage."""
    try:
        return bool(leer_inferencias(parsear_lineas(jsonl)))
    except OSError:
        return False


def resolver_medible(arg):
    """jsonl a MEDIR: el arg explicito, o el mas reciente CON inferencias.

    En un SessionStart la sesion que arranca todavia no tiene inferencias; el
    setup fijo es identico entre sesiones, asi que se calibra contra la ultima
    inferencia disponible (la sesion previa). Apenas la sesion actual produce su
    primera inferencia, /claudit a mano ya la mide a ella.
    """
    if arg:
        return resolver_jsonl(arg)
    proj = dir_proyecto()
    if not proj.is_dir():
        sys.exit(f"No existe el directorio de proyecto: {proj}")
    for s in sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if _tiene_inferencias(s):
            return s
    sys.exit("Ninguna sesion del proyecto tiene inferencias con usage todavia.")


def sesion_actual():
    """jsonl de la sesion en curso: la mas reciente por mtime, aunque este vacia.
    Sirve para NOMBRAR la subcarpeta; en SessionStart es la que recien arranca."""
    proj = dir_proyecto()
    sesiones = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if proj.is_dir() else []
    return sesiones[-1] if sesiones else None


def carpeta_sesion(id_sesion):
    """Devuelve (creandola si hace falta) la subcarpeta de .claudit para la sesion.

    Ring de RING_SLOTS slots (.claudit/0_ .. .claudit/10_): una sesion nueva toma
    el slot (contador % RING_SLOTS) y PISA la carpeta que ocupara ese slot ~11
    sesiones atras. Si la sesion ya tiene carpeta, la reusa: todos los reports de
    esa sesion se acumulan adentro. El nombre lleva slot, timestamp local y los 8
    primeros del session-id. Contador persistido en .claudit/.ring.
    """
    base = REPO / ".claudit"
    base.mkdir(parents=True, exist_ok=True)
    (base / ".gitignore").write_text("*\n", encoding="utf-8")
    for suelto in base.glob("report-*.html"):  # residuos del esquema plano viejo
        suelto.unlink()
    id8 = id_sesion[:8]

    existente = next((d for d in base.glob(f"*__{id8}") if d.is_dir()), None)
    if existente:
        return existente

    ring = base / ".ring"
    try:
        ordinal = int(json.loads(ring.read_text("utf-8")).get("next", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        ordinal = 0
    slot = ordinal % RING_SLOTS
    ring.write_text(json.dumps({"next": ordinal + 1}), encoding="utf-8")

    for viejo in base.glob(f"{slot}_*"):  # el '_' tras el numero evita que 1_* pise 10_*
        if viejo.is_dir():
            shutil.rmtree(viejo, ignore_errors=True)

    carpeta = base / f"{slot}_{time.strftime('%Y-%m-%d_%H-%M-%S')}__{id8}"
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def usage_de(entry):
    msg = entry.get("message") if isinstance(entry, dict) else None
    u = msg.get("usage") if isinstance(msg, dict) else None
    if u is None and isinstance(entry, dict):
        u = entry.get("usage")
    return u if isinstance(u, dict) else None


def write_tokens(u):
    if u.get("cache_creation_input_tokens") is not None:
        return u["cache_creation_input_tokens"] or 0
    cc = u.get("cache_creation")
    if isinstance(cc, dict):
        return sum(v for v in cc.values() if isinstance(v, (int, float)))
    return 0


def write_ttl(u):
    """(tokens 5m, tokens 1h) del cache-write. Sin desglose todo cuenta como 5m."""
    total = write_tokens(u)
    cc = u.get("cache_creation")
    if not isinstance(cc, dict):
        return total, 0
    w1h = cc.get("ephemeral_1h_input_tokens") or 0
    w5m = cc.get("ephemeral_5m_input_tokens") or 0
    if w1h + w5m == 0:
        return total, 0
    resto = total - w1h if total >= w1h else w5m
    return resto, w1h


def parsear_lineas(jsonl):
    """Lista de entradas JSON del transcript, en orden y saltando lineas invalidas."""
    lineas = []
    for linea in jsonl.read_text("utf-8-sig", errors="replace").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        try:
            lineas.append(json.loads(linea))
        except json.JSONDecodeError:
            continue
    return lineas


def leer_inferencias(lineas):
    """Lista ordenada de dicts con los 4 contadores por inferencia (assistant con usage).
    Claude Code escribe UNA LINEA POR BLOQUE de la misma respuesta (thinking, text,
    tool_use...) y repite el mismo usage en cada una: se cuenta una vez por message.id."""
    filas, vistos = [], set()
    for entry in lineas:
        u = usage_de(entry)
        if not u:
            continue
        msg = entry.get("message") if isinstance(entry, dict) else None
        mid = msg.get("id") if isinstance(msg, dict) else None
        if mid:
            if mid in vistos:
                continue
            vistos.add(mid)
        w5m, w1h = write_ttl(u)
        filas.append({
            "read": u.get("cache_read_input_tokens", 0) or 0,
            "write": write_tokens(u),
            "w5m": w5m,
            "w1h": w1h,
            "input": u.get("input_tokens", 0) or 0,
            "output": u.get("output_tokens", 0) or 0,
            "ts": entry.get("timestamp") if isinstance(entry, dict) else None,
            "model": msg.get("model") if isinstance(msg, dict) else None,
        })
    return filas


def costo_partes(f):
    """USD de una inferencia por tipo de token, al precio de su modelo y TTL."""
    p = precios(f.get("model"))
    w5m, w1h = f.get("w5m", f["write"]), f.get("w1h", 0)
    return {"read": f["read"] * p["cache_read"] / 1e6,
            "write": (w5m * p["cache_write_5m"] + w1h * p["cache_write_1h"]) / 1e6,
            "input": f["input"] * p["input"] / 1e6,
            "output": f["output"] * p["output"] / 1e6}


def costo(f):
    return sum(costo_partes(f).values())


def costo_por_tipo(filas):
    tot = {"read": 0.0, "write": 0.0, "input": 0.0, "output": 0.0}
    for f in filas:
        for k, v in costo_partes(f).items():
            tot[k] += v
    return tot


def ttl_dominante(filas):
    """'1h' si la mayoria de los tokens escritos al cache fueron con TTL 1 h, si no '5m'."""
    w1h = sum(f.get("w1h", 0) for f in filas)
    w5m = sum(f.get("w5m", f["write"]) for f in filas)
    return "1h" if w1h > w5m else "5m"


def usd(x):
    # 2 decimales para montos >= $1 ($167.24); 4 para los chicos ($0.0050) donde importa.
    return f"${x:,.2f}" if abs(x) >= 1 else f"${x:,.4f}"


def tok(n):
    return f"{n:,}"


def fila_str(label, f):
    return (f"{label.ljust(22)}{tok(f['read']).rjust(13)}{tok(f['write']).rjust(13)}"
            f"{tok(f['input']).rjust(11)}{tok(f['output']).rjust(11)}")


def blocks_de(entry):
    msg = entry.get("message") if isinstance(entry, dict) else None
    c = msg.get("content") if isinstance(msg, dict) else None
    return c if isinstance(c, list) else []


def es_prompt(entry):
    """Mensaje humano real: type user, no meta/compact, y sin bloques tool_result."""
    if not isinstance(entry, dict) or entry.get("type") != "user":
        return False
    if entry.get("isMeta") or entry.get("isCompactSummary"):
        return False
    msg = entry.get("message")
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return bool(c.strip())
    return isinstance(c, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def _turnos_detalle(lineas):
    """Fuente unica del loop: un dict por prompt humano con sus inferencias en orden
    ({"read", "tools": [nombres]}), agrupadas por message.id (una respuesta = N lineas)."""
    out, por_id = [], {}
    for entry in lineas:
        if es_prompt(entry):
            out.append({"ts": entry.get("timestamp"), "infs": []})
            por_id = {}
            continue
        u = usage_de(entry)
        if not out or not u:
            continue
        mid = (entry.get("message") or {}).get("id") or id(entry)
        inf = por_id.get(mid)
        if inf is None:
            inf = por_id[mid] = {"read": u.get("cache_read_input_tokens", 0) or 0, "tools": []}
            out[-1]["infs"].append(inf)
        inf["tools"] += [b.get("name") or "?" for b in blocks_de(entry)
                         if isinstance(b, dict) and b.get("type") == "tool_use"]
    return out


def turnos(lineas):
    """Un dict por prompt humano: inferencias que genero y tool_use por nombre."""
    out = []
    for t in _turnos_detalle(lineas):
        tools = {}
        for i in t["infs"]:
            for n in i["tools"]:
                tools[n] = tools.get(n, 0) + 1
        out.append({"ts": t["ts"], "inferencias": len(t["infs"]), "tools": tools})
    return out


TOOLS_SUBAGENTE = {"Task", "Agent"}
# Umbrales de las palancas del loop (fuente unica: aca, no en el HTML).
LOOP_MIN_CON_TOOLS = 5        # debajo de esto el % de paralelismo es ruido
LOOP_PARALELO_BAJO = 0.20     # < 20% de inferencias con tools lanzan 2+ a la vez
LOOP_CADENA_LARGA = 5         # 5+ inferencias seguidas de UNA sola tool en un turno
LOOP_TURNO_LARGO = 10         # turno con 10+ inferencias sin delegar
LOOP_HERMANAS_ALTO = 0.50     # > 50% del cache-read se va en hermanas


def loop_agentico(lineas):
    """Como se encadena el trabajo dentro de cada turno. None si no hay inferencias."""
    det = [t for t in _turnos_detalle(lineas) if t["infs"]]
    if not det:
        return None
    infs = [i for t in det for i in t["infs"]]
    por_turno = [len(t["infs"]) for t in det]
    con_tools = [i for i in infs if i["tools"]]
    paralelas = sum(1 for i in con_tools if len(i["tools"]) >= 2)
    cadena, cadena_turno = 0, None
    for n, t in enumerate(det, 1):
        run = 0
        for i in t["infs"]:
            run = run + 1 if len(i["tools"]) == 1 else 0
            if run > cadena:
                cadena, cadena_turno = run, n
    mx = max(por_turno)
    return {"turnos": len(det), "inferencias": len(infs), "prom": len(infs) / len(det),
            "max": mx, "max_turno": por_turno.index(mx) + 1,
            "con_tools": len(con_tools), "paralelas": paralelas,
            "pct_paralelo": paralelas / len(con_tools) if con_tools else 0.0,
            "cadena_max": cadena, "cadena_turno": cadena_turno,
            "read_total": sum(i["read"] for i in infs),
            "read_hermanas": sum(i["read"] for t in det for i in t["infs"][1:]),
            "subagentes": sum(1 for i in infs for n in i["tools"] if n in TOOLS_SUBAGENTE)}


def palancas_loop(m):
    """Hallazgos accionables del loop, con numeros reales. Lista vacia = loop sano."""
    if not m:
        return []
    out = []
    if m["con_tools"] >= LOOP_MIN_CON_TOOLS and m["pct_paralelo"] < LOOP_PARALELO_BAJO:
        out.append(f"Solo {m['pct_paralelo']:.0%} de las inferencias con tools lanzan 2+ en paralelo: "
                   "agrupar lecturas independientes en una misma inferencia ahorra un re-read del cache por tool.")
    if m["cadena_max"] >= LOOP_CADENA_LARGA:
        out.append(f"En el turno {m['cadena_turno']}: cadena de {m['cadena_max']} inferencias seguidas de una sola tool; "
                   "cada eslabon re-lee todo el prefijo cacheado.")
    pct_h = m["read_hermanas"] / m["read_total"] if m["read_total"] else 0
    if m["subagentes"] == 0 and m["max"] >= LOOP_TURNO_LARGO and pct_h > LOOP_HERMANAS_ALTO:
        out.append(f"{_tok0(m['read_hermanas'])} tok de cache-read ({pct_h:.0%}) en inferencias hermanas y 0 sub-agentes "
                   f"(turno {m['max_turno']}: {m['max']} inferencias): la exploracion larga en un sub-agente no re-lee tu contexto.")
    return out


def linea_loop(m):
    if not m:
        return "Loop agentico: sin turnos"
    return (f"Loop agentico: {m['prom']:.1f} inferencias/turno (max {m['max']} en turno {m['max_turno']}), "
            f"{m['pct_paralelo']:.0%} paralelas, cadena max {m['cadena_max']}, {m['subagentes']} sub-agentes")


def linea_primer_turno(ts_):
    if not ts_:
        return "1.er turno: sin prompts"
    t = ts_[0]
    tools = ", ".join(f"{n} x{c}" for n, c in sorted(t["tools"].items(), key=lambda kv: -kv[1])) or "ninguna"
    return f"1.er turno: {t['inferencias']} inferencias, tools: {tools}"


def chars_texto(x):
    """Chars de texto de un content de tool_result: str suelto o lista de bloques."""
    if isinstance(x, str):
        return len(x)
    if isinstance(x, list):
        return sum(len(b.get("text", "") or "") for b in x if isinstance(b, dict))
    return 0


def chars_hook(att):
    """Chars que el hook agrega al contexto: `content` es lo que ve el modelo; `stdout`
    suele ser la misma salida (+1 char) y se usa solo si no hay `content`. stderr y los
    async sin respuesta no entran al prompt."""
    cont = att.get("content")
    if isinstance(cont, str) and cont:
        return len(cont)
    if att.get("type") == "async_hook_response":
        resp = att.get("response")
        return len(json.dumps(resp, ensure_ascii=False)) if resp else 0
    return len(att.get("stdout", "") or "")


def etiqueta_hook(att):
    """'hook <evento> · <origen>': origen = plugin que lo declara o, si no, el
    statusMessage/comando que reporta Claude Code."""
    origen = att.get("pluginId") or (att.get("command") or "").strip()
    origen = origen if len(origen) <= 40 else origen[:39] + "…"
    nombre = att.get("hookName") or att.get("hookEvent") or "?"
    return f"hook {nombre}" + (f" · {origen}" if origen else "")


_TIPOS_HOOK = ("hook_success", "async_hook_response", "hook_system_message")
_MARCA_DEGRADADO = re.compile(r"^\s*(warning|error)\b", re.I)
CAIDA_HOOK = 0.8


def salud_hooks(lineas):
    """Hooks del arranque (antes de la 1.a inferencia): tamano y si vienen degradados.
    Degradado = stderr no vacio, exitCode != 0, o salida cuya PRIMERA linea arranca con
    warning/error (un 'error' en medio de memoria sana no cuenta)."""
    out = []
    for entry in lineas:
        if usage_de(entry):
            break
        att = entry.get("attachment") if isinstance(entry, dict) else None
        if not isinstance(att, dict) or att.get("type") not in _TIPOS_HOOK:
            continue
        texto = att.get("content") if isinstance(att.get("content"), str) else (att.get("stdout") or "")
        primera = next((l for l in str(texto).splitlines() if l.strip()), "")
        motivos, fallas = [], []
        err = str(att.get("stderr") or "").strip()
        if err:
            motivos.append("stderr: " + err.splitlines()[0][:60])
            fallas.append({"corto": "stderr", "detalle": err.splitlines()[0][:300],
                           "que": "El hook escribio en stderr al arrancar: algo fallo o advirtio, "
                                  "y lo que deberia inyectar al contexto puede faltar."})
        code = att.get("exitCode")
        if code not in (None, 0):
            motivos.append(f"exitCode {code}")
            fallas.append({"corto": f"exit {code}", "detalle": f"exitCode {code}",
                           "que": f"Termino con codigo {code} (distinto de 0): Claude Code lo da por fallido."})
        if _MARCA_DEGRADADO.match(primera):
            motivos.append(primera.strip()[:60])
            fallas.append({"corto": "aviso", "detalle": primera.strip()[:300],
                           "que": "La salida arranca con warning/error: el hook inyecta un aviso "
                                  "en vez de (o ademas de) el contenido esperado."})
        out.append({"label": etiqueta_hook(att), "chars": chars_hook(att),
                    "degradado": bool(motivos), "motivo": "; ".join(motivos), "fallas": fallas})
    return out


def comparar_hooks(actual, previa):
    """Hooks que inyectan > 80% menos que en la sesion previa del proyecto."""
    antes = {}
    for h in previa:
        antes[h["label"]] = antes.get(h["label"], 0) + h["chars"]
    avisos = []
    for h in actual:
        prev = antes.get(h["label"], 0)
        if prev and h["chars"] < (1 - CAIDA_HOOK) * prev:
            avisos.append({"label": h["label"], "antes": prev, "ahora": h["chars"]})
    return avisos


def sesion_previa(jsonl):
    """El transcript anterior (por mtime) del mismo proyecto que tenga inferencias."""
    try:
        mt = jsonl.stat().st_mtime
        cands = [p for p in jsonl.parent.glob("*.jsonl") if p != jsonl and p.stat().st_mtime < mt]
    except OSError:
        return None
    for p in sorted(cands, key=lambda p: -p.stat().st_mtime):
        if _tiene_inferencias(p):
            return p
    return None


def hooks_con_previa(jsonl, lineas):
    actual = salud_hooks(lineas)
    prev = sesion_previa(jsonl)
    return actual, comparar_hooks(actual, salud_hooks(parsear_lineas(prev)) if prev else [])


def imprimir_salud_hooks(actual, avisos):
    if not actual:
        return
    print("\nSalud de hooks (arranque):")
    for h in actual:
        estado = f"DEGRADADO ({h['motivo']})" if h["degradado"] else "ok"
        print(f"  {h['label'][:56].ljust(56)}{tok(h['chars']).rjust(9)} chars  {estado}")
    for a in avisos:
        print(f"  AVISO: {a['label']} inyecta mucho menos que antes ({tok(a['antes'])} -> {tok(a['ahora'])} chars)")


def chars_salida(b):
    """Chars de un bloque de la respuesta del modelo (text / thinking / tool_use)."""
    bt = b.get("type")
    if bt == "text":
        return len(b.get("text", "") or "")
    if bt == "thinking":
        return len(b.get("thinking", "") or "")
    if bt == "tool_use":
        return len(json.dumps(b.get("input", {}), ensure_ascii=False)) + len(b.get("name", "") or "")
    return 0


def composicion_prefijo(lineas, filas):
    """Estima como se compone, en tokens, el prefijo que se re-lee cada turno.

    El 'setup fijo' (system + tool-defs + CLAUDE.md + skills + hooks iniciales) se
    mide con el prefijo REAL de la 1a inferencia — no vive como texto en el
    transcript, pero la API ya lo cacheo ahi. El resto (el prompt del usuario,
    resultados de herramientas por tipo, las respuestas del modelo) se estima por chars del contenido
    POSTERIOR y se calibra para que setup + resto == prefijo real de la ultima
    inferencia. Total real (API); reparto interno estimado del contenido.
    """
    setup = filas[0]["read"] + filas[0]["write"] + filas[0]["input"]
    prefijo = filas[-1]["read"] + filas[-1]["write"] + filas[-1]["input"]

    id2name = {}
    for e in lineas:
        if isinstance(e, dict) and e.get("type") == "assistant":
            for b in blocks_de(e):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    id2name[b.get("id")] = b.get("name") or "otro"

    prompts = 0
    output = 0
    tools = {}
    vista_primera = False
    for e in lineas:
        if not isinstance(e, dict):
            continue
        if usage_de(e):  # inferencia: su salida se re-lee luego -> cuenta como output
            for b in blocks_de(e):
                if isinstance(b, dict):
                    output += chars_salida(b)
            vista_primera = True
            continue
        if not vista_primera:
            continue  # todo lo previo a la 1a inferencia ya esta en 'setup' (numero real)
        t = e.get("type")
        if t == "user":
            for b in blocks_de(e):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    prompts += len(b.get("text", "") or "")
                elif b.get("type") == "tool_result":
                    name = id2name.get(b.get("tool_use_id"), "otro")
                    tools[name] = tools.get(name, 0) + chars_texto(b.get("content"))
        elif t == "attachment":
            att = e.get("attachment")
            if not isinstance(att, dict):
                continue
            if att.get("toolUseID") or att.get("hookName"):
                name = id2name.get(att.get("toolUseID")) or ("hooks" if att.get("hookName") else "otro")
                n = chars_hook(att) if att.get("hookName") else (
                    len(att.get("stdout", "") or "") + len(att.get("content", "") or ""))
                if n:
                    tools[name] = tools.get(name, 0) + n

    chars_total = prompts + output + sum(tools.values())
    resto = max(0, prefijo - setup)
    ratio = (resto / chars_total) if chars_total else 0.0
    return {
        "prefijo": prefijo,
        "setup": float(setup),
        "prompts": prompts * ratio,
        "output": output * ratio,
        "tools": {k: v * ratio for k, v in tools.items()},
    }


def abreviar_ruta(ruta, home=None):
    """Ruta con el home como '~' y '/' como separador (igual en Windows y macOS).
    En Windows el home se compara sin distinguir mayusculas."""
    norm = lambda x: str(x).replace("\\", "/").rstrip("/")
    r, h = norm(ruta), norm(home if home is not None else HOME)
    if h and r.lower().startswith(h.lower()) and (len(r) == len(h) or r[len(h)] == "/"):
        return "~" + r[len(h):]
    return r


def secciones_md(path, cpt=CHARS_POR_TOKEN):
    """Corta un CLAUDE.md del disco por headers (# y ##): [(titulo, tokens_estimados)]."""
    if not path.is_file():
        return []
    return secciones_texto(path.read_text("utf-8", errors="replace"), cpt)


def secciones_texto(txt, cpt=CHARS_POR_TOKEN):
    """Corta un markdown por headers (# y ##) y devuelve [(titulo, tokens_estimados)]."""
    partes = re.split(r"(?m)^(#{1,2} .+)$", txt)
    tramos = []
    if partes[0].strip():
        tramos.append(("(preambulo)", len(partes[0])))
    for i in range(1, len(partes), 2):
        h = partes[i].strip()
        cuerpo = partes[i + 1] if i + 1 < len(partes) else ""
        tramos.append((h.lstrip("#").strip(), len(h) + len(cuerpo)))
    fusion = {}  # mismo header repetido (p. ej. un hook que inyecta 2 veces) = una fila con el total
    for t, ch in tramos:
        fusion[t] = fusion.get(t, 0) + ch
    return [(t, ch / cpt) for t, ch in fusion.items()]


def todas_skills(txt, cpt=CHARS_POR_TOKEN):
    """Cada skill del skill_listing ('- nombre: descripcion') como (nombre, tokens), en orden alfabetico."""
    items = []
    for parte in re.split(r"(?m)^(?=- )", txt):
        if parte.strip():
            nombre = parte[2:].split(": ", 1)[0].strip() if parte.startswith("- ") else "(preambulo)"
            items.append((nombre, len(parte) / cpt))
    return sorted(items, key=lambda x: x[0].casefold())


ETIQUETA_PERSONALES = "Personales globales"


def etiqueta_personales():
    """Nombre de las skills personales globales (sin `author`); configurable en ~/.claude/claudit.json."""
    try:
        cfg = json.loads((HOME / ".claude" / "claudit.json").read_text("utf-8"))
        return str(cfg.get("etiqueta_personales") or ETIQUETA_PERSONALES)
    except (OSError, ValueError, AttributeError):
        return ETIQUETA_PERSONALES


def _frontmatter(path):
    """(author, version) del frontmatter de un SKILL.md; '' si no estan."""
    try:
        txt = path.read_text("utf-8", errors="replace")
    except OSError:
        return "", ""
    partes = txt.split("---", 2)
    front = partes[1] if txt.startswith("---") and len(partes) > 2 else ""
    campo = lambda k: (re.search(rf"(?m)^\s*{k}:\s*[\"']?([^\"'\n]+?)[\"']?\s*$", front) or [None, ""])[1]
    return campo("author"), campo("version")


def fuente_skill(nombre):
    """(proveedor, fuente, version_skill) de una skill del skill_listing."""
    if nombre.startswith("anthropic-skills:"):
        return "anthropic", "claude.ai (anthropic-skills)", ""
    if ":" in nombre:
        plugin = nombre.split(":", 1)[0]
        return f"Plugin {plugin}", f"plugin {plugin}", ""
    for base, fuente in ((HOME / ".claude" / "skills", "~/.claude/skills"),
                         (REPO / ".claude" / "skills", ".claude/skills del proyecto")):
        md = base / nombre / "SKILL.md"
        if md.is_file():
            autor, version = _frontmatter(md)
            prov = "Proyecto" if fuente.endswith("proyecto") else autor or etiqueta_personales()
            return prov, fuente, version
    return "anthropic", "Claude Code (incluida)", ""


def escanear_listados(raiz=None):
    """[(ts, version_cc, names)] de cada skill_listing de los transcripts locales, en orden.
    Es un piso, no un nacimiento: Claude Code borra los transcripts viejos (cleanupPeriodDays)."""
    raiz = raiz or HOME / ".claude" / "projects"
    out = []
    for p in Path(raiz).rglob("*.jsonl"):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                for linea in fh:
                    if '"skill_listing"' not in linea:
                        continue
                    try:
                        e = json.loads(linea)
                    except ValueError:
                        continue
                    att, ts = e.get("attachment") or {}, e.get("timestamp") or ""
                    if att.get("type") == "skill_listing" and ts:
                        out.append((ts, e.get("version") or "", att.get("names") or []))
        except OSError:
            continue
    return sorted(out, key=lambda x: x[0])


def historial_skills(listados):
    """{skill: {desde, cc}}: primera vez que aparece en un skill_listing y la version de Claude Code."""
    primera = {}
    for ts, version, names in listados:
        for n in names:
            primera.setdefault(n, {"desde": ts[:10], "cc": version})
    return primera


def versiones_cc(listados):
    """[(version, primer dia visto)] de Claude Code, en el orden en que aparecieron."""
    vistas = {}
    for ts, version, _ in listados:
        if version:
            vistas.setdefault(version, ts[:10])
    return list(vistas.items())


# Plugins cuya version propia vive en un binario aparte. Lista CERRADA a proposito: ejecutar
# binarios elegidos por nombre de plugin seria abrir la puerta a correr cualquier cosa.
BINARIOS_VERSION = {"engram": ["engram", "version"]}


def _version_binario(cmd):
    """Version (x.y.z) que imprime `cmd`; '' si el binario no esta o no responde."""
    if not shutil.which(cmd[0]):
        return ""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\d+\.\d+\.\d+", out or "")
    return m.group(0) if m else ""


def _clave_version(v):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v)]


def _componente(nombre, version, eventos, fuente):
    eventos = sorted(eventos)
    return {"nombre": nombre, "version": version, "desde": eventos[0][0], "actualizada": eventos[-1][0],
            "fuente": fuente, "eventos": eventos}


def _plugins_versiones():
    """Un componente por plugin de installed_plugins.json (los que haya en ESTA maquina)."""
    try:
        data = json.loads((HOME / ".claude" / "plugins" / "installed_plugins.json").read_text("utf-8"))
        plugins = data.get("plugins") or {}
    except (OSError, ValueError, AttributeError):
        return []
    comps = []
    for clave in sorted(plugins):
        inst = [i for i in plugins[clave] or [] if isinstance(i, dict) and i.get("installedAt")]
        if not inst:
            continue
        i = max(inst, key=lambda x: x.get("lastUpdated") or x["installedAt"])
        nombre, v = clave.split("@", 1)[0], str(i.get("version") or "")
        eventos = [(i["installedAt"][:10], f"instalado {v}")]
        if (i.get("lastUpdated") or "")[:10] > eventos[0][0]:
            eventos.append((i["lastUpdated"][:10], f"actualizado {v}"))
        binario = _version_binario(BINARIOS_VERSION[nombre]) if nombre in BINARIOS_VERSION else ""
        version = f"{binario} · plugin {v}" if binario else v
        comps.append(_componente(nombre, version, eventos, "installed_plugins.json"))
    return comps


def _autores_versiones():
    """Un componente por autor de ~/.claude/skills: version declarada por skill y dias de instalacion."""
    grupos = {}
    for md in sorted((HOME / ".claude" / "skills").glob("*/SKILL.md")):
        try:
            dia = datetime.fromtimestamp(md.stat().st_mtime).date().isoformat()
        except OSError:
            continue
        autor, version = _frontmatter(md)
        g = grupos.setdefault(autor or etiqueta_personales(), {"versiones": set(), "dias": {}})
        if version:
            g["versiones"].add(version)
        g["dias"][dia] = g["dias"].get(dia, 0) + 1
    comps = []
    for autor in sorted(grupos, key=str.casefold):
        g = grupos[autor]
        vs = sorted(g["versiones"], key=_clave_version)
        version = (vs[0] if len(vs) == 1 else "por skill: " + ", ".join(vs)) if vs else "sin versión"
        eventos = [(d, f"{n} skill{'s' if n > 1 else ''} instalada{'s' if n > 1 else ''}/actualizada{'s' if n > 1 else ''}")
                   for d, n in g["dias"].items()]
        comps.append(_componente(autor, version, eventos, "~/.claude/skills (fecha de los archivos)"))
    return comps


def componentes_versiones(listados):
    """Claude Code + plugins + autores de skills, cada uno con su version propia (la que declara
    quien lo publica) y las fechas en que se vio en ESTA maquina. Todo se detecta en runtime."""
    comps = []
    vs = versiones_cc(listados)
    if vs:
        comps.append(_componente("Claude Code", vs[-1][0], [(d, v) for v, d in vs],
                                 "transcripts (primer dia de cada version)"))
    return comps + _plugins_versiones() + _autores_versiones()


def filas_skills(crudas, historial):
    """Una fila por skill (orden de `crudas`) con proveedor, fuente, tok, desde y version."""
    filas = []
    for nombre, tok in crudas:
        prov, fuente, version = fuente_skill(nombre)
        h = historial.get(nombre) or {}
        filas.append({"skill": nombre, "proveedor": prov, "fuente": fuente, "tok": round(tok),
                      "desde": h.get("desde", ""), "cc": h.get("cc", ""), "version": version})
    return filas


def secciones_skills(txt, cpt=CHARS_POR_TOKEN, top=None):
    """Las `top` skills mas pesadas una por una y el resto agregado al final."""
    top = SKILLS_TOP if top is None else top
    items = sorted(todas_skills(txt, cpt), key=lambda x: -x[1])
    if len(items) > top:
        resto = items[top:]
        items = items[:top] + [(f"resto ({len(resto)} skills)", sum(k for _, k in resto))]
    return items


def secc_hook(txt, cpt=CHARS_POR_TOKEN):
    """Secciones (# y ##) de la salida de un hook; None si no tiene headers."""
    secc = secciones_texto(txt, cpt)
    return secciones_top(secc) if len(secc) > 1 else None


def secciones_top(secc):
    """Secciones >= SECC_MINIMA + el 'resto' agregado, TODO ordenado por tokens
    desc (= % setup desc). Como la palanca deriva de los tokens (***>=1000, **>=300,
    *<300), ese mismo orden deja las palancas de *** a * — segundo orden gratis.
    El 'resto' entra al sort: cae en su posicion real por peso, no pegado al final."""
    grandes = [(t, k) for t, k in secc if k >= SECC_MINIMA]
    chicas = [(t, k) for t, k in secc if k < SECC_MINIMA]
    if chicas:
        grandes.append((f"resto ({len(chicas)} secciones menores)", sum(k for _, k in chicas)))
    return sorted(grandes, key=lambda x: -x[1])


# Conceptos para agrupar secciones en Prioridad de mejora. Vocabulario GENERICO y
# bilingue (no los titulos de un setup puntual): matchea por subcadena en el titulo,
# gana el primer concepto. Heuristica: lo que no matchea queda suelto, nunca se pierde.
CONCEPTOS = [
    ("SDD", ("sdd", "spec-driven", "openspec")),
    ("Memoria persistente", ("memori", "memory", "engram", "recall", "recordar")),
    ("Agentes y modelos", ("agent", "orquest", "orchestr", "model", "delegat", "sub-agente")),
    ("Skills", ("skill",)),
    ("Sesión y contexto", ("sesión", "sesion", "session", "context", "compact")),
    ("Estilo y tono", ("tone", "tono", "personality", "personalidad", "language", "idioma",
                       "style", "estilo", "speech")),
]


def _concepto(titulo):
    t = titulo.casefold()
    return next((n for n, claves in CONCEPTOS if any(c in t for c in claves)), None)


def agrupar_conceptos(secc):
    """[{titulo, tok, hijos}] por peso desc: un nodo por concepto con 2+ secciones;
    el resto suelto, y las sueltas < SECC_MINIMA juntas en 'resto'. Total conservado."""
    grupos = {}
    for t, k in secc:
        grupos.setdefault(_concepto(t) or ("", t), []).append((t, k))
    nodos, chicas = [], []
    for clave, hijos in grupos.items():
        if isinstance(clave, str) and len(hijos) > 1:
            nodos.append({"titulo": clave, "tok": sum(k for _, k in hijos),
                          "hijos": sorted(hijos, key=lambda x: -x[1])})
            continue
        for t, k in hijos:
            (nodos.append({"titulo": t, "tok": k, "hijos": []}) if k >= SECC_MINIMA else chicas.append(k))
    if chicas:
        nodos.append({"titulo": f"resto ({len(chicas)} secciones menores)", "tok": sum(chicas), "hijos": []})
    return sorted(nodos, key=lambda n: -n["tok"])


def palanca(tok_val, editable):
    """Columna (*): magnitud del ahorro/turno si se recorta. Base no editable -> ''."""
    if not editable:
        return ""
    if tok_val >= PALANCA_ALTA:
        return "***"
    if tok_val >= PALANCA_MEDIA:
        return "**"
    return "*"


# Categoria de cada componente del setup, alineada con /context de Claude Code.
# editable = se puede recortar desde la config del usuario (columna de palanca).
CATEGORIAS = {
    "sistema": ("System prompt", False),
    "tools": ("System tools", False),
    "mcp_tools": ("MCP tools", True),
    "mcp": ("MCP server instructions", True),
    "agentes": ("Custom agents", True),
    "memoria": ("Memory files", True),
    "skills": ("Skills", True),
    "hook": ("Hooks", True),
    "otros": ("Otros adjuntos", False),
    "resto": ("Resto no medido", False),
}

_TXT_ADJUNTO = {  # tipo -> funcion que devuelve el texto que el adjunto aporta al prompt
    "skill_listing": lambda a: a.get("content") or "",
    "deferred_tools_delta": lambda a: "\n".join(a.get("addedLines") or []),
    "mcp_instructions_delta": lambda a: "\n".join(b if isinstance(b, str) else json.dumps(b, ensure_ascii=False)
                                                 for b in a.get("addedBlocks") or []),
    "output_style_instructions": lambda a: (a.get("style") or {}).get("prompt") or "",
    "session_context": lambda a: "".join(str(v) for v in (a.get("context") or {}).values()),
    "environment": lambda a: json.dumps(a.get("snapshot") or {}, ensure_ascii=False),
    "model": lambda a: a.get("text") or "",
    "date": lambda a: str(a.get("date") or ""),
    "output_style": lambda a: str(a.get("style") or ""),
    "prompt_snapshot": lambda a: "",  # copia del system prompt: ya se mide desde api-request-shape
}
_ETIQUETA_ADJUNTO = {"skill_listing": "skill_listing (skills instaladas)",
                     "deferred_tools_delta": "deferred tools (listado de nombres)",
                     "mcp_instructions_delta": "instrucciones de servidores MCP",
                     "output_style_instructions": "output style (instrucciones)"}
_TIPO_ADJUNTO = {"skill_listing": "skills", "deferred_tools_delta": "tools",
                 "mcp_instructions_delta": "mcp", "output_style_instructions": "sistema"}


def chars_adjunto(att):
    """(chars, categoria, estimado) que un adjunto aporta al contexto. Tipos sin
    extractor conocido se miden como su JSON entero y quedan marcados estimados."""
    t = att.get("type")
    if att.get("hookName"):
        return chars_hook(att), "hook", False
    if t in _TXT_ADJUNTO:
        return len(_TXT_ADJUNTO[t](att)), _TIPO_ADJUNTO.get(t, "otros"), False
    return len(json.dumps(att, ensure_ascii=False)), "otros", True


def _agentes(att):
    """Lineas del listado de agentes -> ([(nombre, chars)] custom, chars built-in)."""
    builtin = set(att.get("builtInTypes") or []) | set(att.get("builtInPluginTypes") or [])
    custom, base = [], 0
    for linea in att.get("addedLines") or []:
        nombre = linea.lstrip("- ").split(":", 1)[0].strip()
        if nombre in builtin:
            base += len(linea)
        else:
            custom.append((nombre, len(linea)))
    return custom, base


def calibrar_cpt(chars, tokens, cpt_flag=None, con_forma=True):
    """(ratio chars/tok, fuente): 'flag' si se paso a mano; 'calibrado' contra los
    tokens reales (solo si se midieron system prompt + tool-defs); si no 'default'."""
    if cpt_flag:
        return float(cpt_flag), "flag"
    if con_forma and chars > 0 and tokens > 0:
        return min(CPT_MAX, max(CPT_MIN, chars / tokens)), "calibrado"
    return CHARS_POR_TOKEN, "default"


def desglose_setup(lineas, filas, cpt_flag=None):
    """Abre el setup fijo (prefijo real de la 1a inferencia) en sus componentes.

    Todo se mide como texto desde el transcript: system prompt y tool-defs
    (`api-request-shape`), archivos de instrucciones tal como los cargo la sesion
    (`instructions`), skills, agentes, MCP, hooks y demas adjuntos. Lo que no se
    pudo medir queda como 'resto' = setup_real - suma_medida. Solo si el transcript
    no trae `instructions` se leen los CLAUDE.md del disco (estado ACTUAL, estimado).
    """
    total = filas[0]["read"] + filas[0]["write"] + filas[0]["input"]
    acum = {}  # (tipo, label) -> [chars, estimado, secc]
    textos = {}  # (tipo, label) -> texto, para abrir hooks y skill_listing un nivel mas
    memoria = {}  # path -> (tipo, texto), en orden de carga
    forma_vista = False

    def sumar(tipo, label, n, estimado=False):
        if n <= 0:
            return
        c = acum.setdefault((tipo, label), [0, False])
        c[0] += n
        c[1] = c[1] or estimado

    for e in lineas:
        if usage_de(e):  # llegamos a la 1a inferencia: lo previo es setup
            break
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        if t == "api-request-shape" and not forma_vista:
            forma_vista = True
            sh = e.get("shape") or {}
            sumar("sistema", "system prompt", sum(len(b.get("text") or "") for b in sh.get("system") or []
                                                  if isinstance(b, dict)))
            for tool in sh.get("tools") or []:
                n = len(json.dumps(tool, ensure_ascii=False))
                if str(tool.get("name", "")).startswith("mcp__"):
                    srv = tool["name"].split("__")[1] if tool["name"].count("__") >= 2 else "mcp"
                    sumar("mcp_tools", f"MCP tool-defs · {srv}", n)
                else:
                    sumar("tools", "tool-defs (herramientas cargadas)", n)
        elif t == "attachment":
            att = e.get("attachment") or {}
            at = att.get("type")
            if at == "instructions":
                for fi in att.get("files") or []:
                    if isinstance(fi, dict) and fi.get("path"):
                        memoria[fi["path"]] = (fi.get("type") or "Memoria", fi.get("content") or "")
            elif at == "agent_listing_delta":
                custom, base = _agentes(att)
                sumar("tools", "agentes built-in (listado)", base)
                for nombre, n in custom:
                    sumar("agentes", f"agente {nombre}", n)
            elif att.get("hookName"):
                sumar("hook", etiqueta_hook(att), chars_hook(att))
                if isinstance(att.get("content"), str):
                    textos[("hook", etiqueta_hook(att))] = textos.get(("hook", etiqueta_hook(att)), "") + att["content"]
            else:
                n, tipo, est = chars_adjunto(att)
                sumar(tipo, _ETIQUETA_ADJUNTO.get(at, at or "adjunto"), n, est)
                if at == "skill_listing":
                    textos[(tipo, _ETIQUETA_ADJUNTO[at])] = textos.get((tipo, _ETIQUETA_ADJUNTO[at]), "") + _TXT_ADJUNTO[at](att)
        elif t == "user":
            for b in blocks_de(e):
                if isinstance(b, dict) and b.get("type") == "text":
                    sumar("otros", "system-reminders del primer mensaje", len(b.get("text", "") or ""))

    disco = []
    if not memoria:  # transcript viejo sin adjunto `instructions`: estado actual del disco
        for tipo, path in (("User", HOME / ".claude" / "CLAUDE.md"), ("Project", REPO / "CLAUDE.md")):
            if path.is_file():
                disco.append((tipo, path, path.read_text("utf-8", errors="replace")))
    chars = (sum(n for n, _ in acum.values()) + sum(len(txt) for _, txt in memoria.values())
             + sum(len(txt) for _, _, txt in disco))
    ratio, fuente = calibrar_cpt(chars, total, cpt_flag, forma_vista)
    ch = lambda n: n / ratio

    comps = []

    def comp(tipo, label, tk, secc=None, estimado=False, crudas=None):
        # secc = vista compacta (top + resto) del arbol; crudas = todas las secciones, para los tips.
        comps.append({"label": label, "tok": tk, "editable": CATEGORIAS[tipo][1], "secc": secc,
                      "tipo": tipo, "estimado": estimado, "crudas": crudas})

    for path, (tipo, txt) in memoria.items():
        secc = secciones_texto(txt, ratio)
        comp("memoria", f"{tipo} {abreviar_ruta(path)}", sum(k for _, k in secc), secciones_top(secc), crudas=secc)
    for tipo, path, txt in disco:
        secc = secciones_texto(txt, ratio)
        comp("memoria", f"{tipo} {abreviar_ruta(path)}", sum(k for _, k in secc),
             secciones_top(secc), estimado=True, crudas=secc)
    for (tipo, label), (n, est) in acum.items():
        txt = textos.get((tipo, label))
        secc = crudas = None
        if txt and tipo == "hook":
            secc = secc_hook(txt, ratio)
            crudas = secciones_texto(txt, ratio) if secc else None
        elif txt and tipo == "skills":
            secc, crudas = secciones_skills(txt, ratio), todas_skills(txt, ratio)
        comp(tipo, label, ch(n), secc, estimado=est, crudas=crudas)

    medido = sum(c["tok"] for c in comps)
    resto = total - medido
    orden = list(CATEGORIAS)
    # base no editable primero (en orden de /context), despues lo editable por peso desc
    comps.sort(key=lambda c: (c["editable"], orden.index(c["tipo"]) if not c["editable"] else 0, -c["tok"]))
    if forma_vista:
        if abs(resto) >= 1:
            comps.append({"label": "resto no medido (diferencia chars->tok)", "tok": resto, "editable": False,
                          "secc": None, "tipo": "resto", "estimado": True})
    else:  # sin api-request-shape (transcript viejo): system + tool-defs quedan como residuo
        comps.insert(0, {"label": "base harness + tool-defs (no editable)", "tok": max(0.0, resto),
                         "editable": False, "secc": None, "tipo": "sistema", "estimado": True})
    return {"total": total, "componentes": comps, "ratio": ratio, "ratio_fuente": fuente}


# ---------------------------------------------------------------------------
# Vista /context: el contexto ACTUAL (ultima inferencia) por categoria, como el panel
# /context de Claude Code. Las categorias fijas salen del desglose del setup; Messages
# es el resto del contexto usado. La ventana se estima por modelo (o --ventana N).
# ---------------------------------------------------------------------------
AUTOCOMPACT_BUFFER = 33_000
VENTANA_BASE, VENTANA_1M = 200_000, 1_000_000
_CATS_FIJAS = ("sistema", "tools", "mcp_tools", "mcp", "agentes", "memoria", "skills")
_NOMBRE_CAT_EXTRA = {"mensajes": "Messages", "buffer": "Autocompact buffer", "libre": "Free space"}


def ventana_modelo(model):
    """Ventana de contexto estimada: 1M para la familia 5 (opus/sonnet/fable) o ids
    con [1m]; 200k para el resto. Es una estimacion: --ventana N la fija."""
    m = (model or "").lower()
    if "[1m]" in m or re.search(r"(opus|sonnet|fable)-5", m):
        return VENTANA_1M
    return VENTANA_BASE


VAR_AUTOCOMPACT = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
VENTANA_RECOMENDADA = 200_000


def leer_settings():
    """~/.claude/settings.json del usuario (dict vacio si falta o esta roto)."""
    try:
        d = json.loads((HOME / ".claude" / "settings.json").read_text("utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def autocompact_config():
    """(ventana, fuente) de CLAUDE_CODE_AUTO_COMPACT_WINDOW: env > settings.json env."""
    for fuente, val in (("env", os.environ.get(VAR_AUTOCOMPACT)),
                        ("settings.json", (leer_settings().get("env") or {}).get(VAR_AUTOCOMPACT))):
        try:
            n = int(str(val).strip()) if val is not None else 0
        except ValueError:
            n = 0
        if n > 0:
            return n, fuente
    return None, None


def analisis_ventana(filas):
    """Contexto por inferencia (read+write+input), su maximo y la alerta de la mejora 8:
    modelo de 1M o contexto > 200k SIN la variable de auto-compact -> fix + costo de los
    turnos por encima de 200k - buffer (donde el auto-compact recomendado ya habria saltado)."""
    modelo = modelo_actual(filas)
    ctx = [f["read"] + f["write"] + f["input"] for f in filas]
    maximo = max(ctx, default=0)
    config, fuente = autocompact_config()
    ventana = config or ventana_modelo(modelo)
    corte = VENTANA_RECOMENDADA - AUTOCOMPACT_BUFFER
    sobre = [f for f, c in zip(filas, ctx) if c > corte]
    alerta = config is None and (maximo > VENTANA_RECOMENDADA or ventana_modelo(modelo) >= VENTANA_1M)
    fix = json.dumps({"env": {VAR_AUTOCOMPACT: str(VENTANA_RECOMENDADA)}}, indent=2)
    return {"modelo": modelo, "contextos": ctx, "max": maximo, "config": config, "fuente": fuente,
            "ventana": ventana, "umbral": ventana - AUTOCOMPACT_BUFFER, "alerta": alerta, "fix": fix,
            "corte": corte, "turnos_sobre": len(sobre), "costo_sobre": sum(costo(f) for f in sobre)}


def imprimir_alerta_ventana(a):
    print(f"\nContexto maximo de la sesion: {tok(a['max'])} tok · auto-compact a ~{tok(a['umbral'])}"
          + (f" ({VAR_AUTOCOMPACT}={a['config']} via {a['fuente']})" if a["config"] else " (default del modelo)"))
    if not a["alerta"]:
        return
    print(f"  ALERTA: sin {VAR_AUTOCOMPACT}, el auto-compact salta recien a ~{tok(a['umbral'])}.")
    print(f"  {a['turnos_sobre']} turnos por encima de {tok(a['corte'])} costaron ~ {usd(a['costo_sobre'])}.")
    print("  Fix en ~/.claude/settings.json:")
    for linea in a["fix"].splitlines():
        print(f"    {linea}")


_TTL_S = {"5m": 300, "1h": 3600}
_RECO_RETOMADA = ("/compact antes de la pausa, o sesion nueva si los pendientes "
                  "estan en Engram/HANDOFF")


def retomadas_cache_vencida(filas):
    """Huecos entre inferencias > TTL dominante seguidos de un write >= 50% del
    contexto: la cache vencio y el prefijo entero se re-escribio (mejora 9)."""
    ttl = _TTL_S[ttl_dominante(filas)]
    eventos = []
    for i in range(1, len(filas)):
        t0, t1 = _ts_epoch(filas[i - 1]["ts"]), _ts_epoch(filas[i]["ts"])
        f = filas[i]
        ctx = f["read"] + f["write"] + f["input"]
        if t0 is None or t1 is None or t1 - t0 <= ttl or not ctx or f["write"] < 0.5 * ctx:
            continue
        eventos.append({"indice": i, "hora": _hora_local(f["ts"]), "hueco_s": t1 - t0,
                        "rewrite": f["write"], "costo": costo_partes(f)["write"]})
    return eventos


def _duracion(seg):
    m = int(seg // 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def imprimir_retomadas(eventos):
    if not eventos:
        return
    print("\nSesion retomada con cache vencida:")
    for ev in eventos:
        print(f"  {ev['hora']} · pausa {_duracion(ev['hueco_s'])} · re-write {tok(ev['rewrite'])} tok"
              f" ~ {usd(ev['costo'])}")
    print(f"  Recomendacion: {_RECO_RETOMADA}.")


VENTANA_CUPO_H = 5
# Minutos recientes con los que se mide el ritmo de consumo del cupo.
MIN_RITMO = 30
# Perfil de uso: (peligroso desde, moderado desde). Cupo = % proyectado al reset;
# contexto = % de la ventana usado en la ultima inferencia (70 = regla de compact).
CORTES_PERFIL = {"cupo": (100, 60), "contexto": (70, 40)}
# Un cache-write es peligroso si por si solo (escritura + sus relecturas) costo >= este % del cupo.
UMBRAL_INFERENCIA_PCT = 1


def nivel_perfil(valor, cortes):
    peligro, moderado = cortes
    return "peligroso" if valor >= peligro else "moderado" if valor >= moderado else "saludable"


def _es_limite(entry):
    """Evento de limite de uso: assistant sintetico con error rate_limit (formato real
    de Claude Code: isApiErrorMessage + error 'rate_limit')."""
    return (isinstance(entry, dict) and entry.get("isApiErrorMessage")
            and str(entry.get("error") or "").lower() in ("rate_limit", "usage_limit_reached"))


def consumo_local(raiz=None, ahora=None, horas=VENTANA_CUPO_H):
    """Uso de TODAS las sesiones locales (todos los proyectos, sub-agentes incluidos)
    en la ventana de `horas`. Cada campo lleva su procedencia; el cupo de la cuenta no
    se expone por una via soportada -> unknown (sin endpoints privados ni credenciales)."""
    raiz = raiz or HOME / ".claude" / "projects"
    ahora = ahora if ahora is not None else time.time()
    desde = ahora - horas * 3600
    previo = ahora - 2 * horas * 3600  # para ubicar el inicio del bloque en curso
    toks = usd_tot = 0.0
    sesiones, proyectos, limites = set(), set(), []
    eventos, t_limites = [], []
    for jsonl in (raiz.rglob("*.jsonl") if raiz.is_dir() else []):
        try:
            if jsonl.stat().st_mtime < previo:
                continue
            lineas = parsear_lineas(jsonl)
        except OSError:
            continue
        activa = False
        for f in leer_inferencias(lineas):
            t = _ts_epoch(f["ts"])
            if t is not None and previo <= t <= ahora:
                eventos.append((t, costo(f)))
            if t is not None and desde <= t <= ahora:
                toks += f["read"] + f["write"] + f["input"] + f["output"]
                usd_tot += costo(f)
                activa = True
        for e in lineas:
            t = _ts_epoch(e.get("timestamp")) if _es_limite(e) else None
            if t is not None and desde <= t <= ahora:
                limites.append(_hora_local(e["timestamp"]))
                t_limites.append(t)
        if activa:
            rel = jsonl.relative_to(raiz).parts
            proyectos.add(rel[0])
            sesiones.add((rel[0], rel[1].split(".")[0]))  # sub-agentes cuentan en su sesion
    return {"horas": horas,
            "tokens": {"valor": int(toks), "fuente": "calculated"},
            "costo": {"valor": usd_tot, "fuente": "estimated"},
            "sesiones": {"valor": len(sesiones), "fuente": "observed"},
            "proyectos": {"valor": len(proyectos), "fuente": "observed"},
            "limites": {"valor": limites, "fuente": "observed"},
            "cupo_cuenta": {"valor": None, "fuente": "unknown"},
            "bloque": _consumo_bloque(eventos, t_limites, ahora, horas)}


def bloque_5h(epochs, ahora, horas=VENTANA_CUPO_H):
    """(inicio, reset) del bloque de cupo en curso, o None si no hay uno activo.
    Un bloque arranca con el primer uso tras vencer el anterior y dura `horas`."""
    inicio = None
    for t in sorted(epochs):
        if t > ahora:
            break
        if inicio is None or t >= inicio + horas * 3600:
            inicio = t
    if inicio is None or ahora >= inicio + horas * 3600:
        return None
    return inicio, inicio + horas * 3600


def _consumo_bloque(eventos, t_limites, ahora, horas):
    """USD del bloque en curso y de los ultimos MIN_RITMO min: con el % que el usuario
    lee en Account & Usage, el reporte escala USD -> % de cupo (ritmo y bloqueo)."""
    b = bloque_5h([t for t, _ in eventos], ahora, horas)
    if not b:
        return None
    inicio, reset = b
    minutos = max(1.0, min(MIN_RITMO, (ahora - inicio) / 60))
    return {"inicio": inicio, "reset": reset,
            "usd": sum(u for t, u in eventos if t >= inicio),
            "usd_reciente": sum(u for t, u in eventos if t >= ahora - minutos * 60),
            "min_reciente": minutos,
            "limites": sum(1 for t in t_limites if t >= inicio),
            "fuente": "estimated"}


def lineas_cupo(c):
    lim = c["limites"]["valor"]
    out = [f"Cupo · consumo local ultimas {c['horas']} h (todos los proyectos):",
           f"  tokens    {tok(c['tokens']['valor']).rjust(14)}  [{c['tokens']['fuente']}]",
           f"  costo     {usd(c['costo']['valor']).rjust(14)}  [{c['costo']['fuente']}]",
           f"  sesiones  {str(c['sesiones']['valor']).rjust(14)}  [{c['sesiones']['fuente']}]"
           f" · proyectos {c['proyectos']['valor']}",
           f"  limites   {str(len(lim)).rjust(14)}  [{c['limites']['fuente']}]" + (f" · {', '.join(lim[:5])}" if lim else ""),
           f"  cupo de la cuenta: unknown (sin dato de cuenta por una via soportada)"]
    return out


USO_MESES = 6
_TOOLS_EDICION = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_RE_COMANDO = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
# Comandos nativos de Claude Code: no son skills, no se cuentan como tales.
_COMANDOS_NATIVOS = frozenset((
    "add-dir agents bug clear compact config context cost doctor effort exit export fast help hooks ide "
    "init login logout mcp memory model output-style permissions plugin plugins pr-comments privacy-settings "
    "release-notes resume rewind status statusline terminal-setup theme todos upgrade usage vim").split())
# Heuristica lectura/escritura de un conector MCP por los verbos del nombre de la tool.
_VERBOS_LECTURA = frozenset("get list read search query fetch find context current stats view describe "
                            "lookup timeline export guide".split())


def _skills_de_prompt(e):
    """Slash commands del prompt que no son comandos nativos (fuente unica: Uso y sesion)."""
    c = (e.get("message") or {}).get("content")
    return [cmd for cmd in _RE_COMANDO.findall(c if isinstance(c, str) else "") if cmd not in _COMANDOS_NATIVOS]


def _clasificar_tool(b):
    """("skill", nombre) | ("conector", servidor, "lecturas"|"escrituras") | None."""
    nombre = b.get("name") or "?"
    if nombre == "Skill" and isinstance(b.get("input"), dict) and b["input"].get("skill"):
        return ("skill", b["input"]["skill"])
    if nombre.startswith("mcp__") and nombre.count("__") >= 2:
        _, srv, tool_ = nombre.split("__", 2)
        lectura = _VERBOS_LECTURA & set(re.split(r"[_\-]", tool_.lower()))
        return ("conector", srv, "lecturas" if lectura else "escrituras")
    return None


def _contar_clase(clase, skills, conectores):
    if clase and clase[0] == "skill":
        skills[clase[1]] = skills.get(clase[1], 0) + 1
    elif clase:
        c = conectores.setdefault(clase[1], {"lecturas": 0, "escrituras": 0})
        c[clase[2]] += 1


def _texto_resultado(b):
    c = b.get("content")
    return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)


def _rechazado(entry, b):
    r = entry.get("toolUseResult")
    return (isinstance(r, str) and "rejected" in r.lower()) or (
        bool(b.get("is_error")) and "doesn't want to proceed" in _texto_resultado(b))


def _lineas_aceptadas(r):
    """'+' del structuredPatch de un Edit; en un Write de archivo nuevo, sus lineas."""
    if not isinstance(r, dict):
        return 0
    patch = r.get("structuredPatch") or []
    n = sum(1 for h in patch if isinstance(h, dict) for l in h.get("lines") or []
            if isinstance(l, str) and l.startswith("+"))
    if not patch and r.get("type") == "create" and isinstance(r.get("content"), str):
        n = len(r["content"].splitlines())
    return n


def _fecha_local(ts):
    t = _ts_epoch(ts)
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d") if t is not None else None


def _mes_vacio():
    return {"dias": {}, "skills": {}, "conectores": {}, "modelos": {}, "aceptadas": 0, "rechazadas": 0}


def _dia_vacio():
    return {"sesiones": set(), "prompts": 0, "lineas": 0, "tools": 0, "inferencias": 0, "tokens": 0, "costo": 0.0}


def _acumular_uso(meses, lineas, sesion, sidechain):
    """Suma un transcript a `meses` (mes -> acumulador). Los prompts de sub-agentes
    no son prompts humanos: sus tools, lineas y tokens si cuentan (trabajo delegado)."""
    def dia(fecha):
        m = meses.setdefault(fecha[:7], _mes_vacio())
        return m, m["dias"].setdefault(fecha, _dia_vacio())

    for f in leer_inferencias(lineas):
        fecha = _fecha_local(f["ts"])
        if not fecha:
            continue
        m, d = dia(fecha)
        d["inferencias"] += 1
        d["tokens"] += f["read"] + f["write"] + f["input"] + f["output"]
        d["costo"] += costo(f)
        d["sesiones"].add(sesion)
        if f.get("model") and f["model"] != "<synthetic>":
            m["modelos"][f["model"]] = m["modelos"].get(f["model"], 0) + 1
    nombres = {}
    for e in lineas:
        fecha = _fecha_local(e.get("timestamp")) if isinstance(e, dict) else None
        if not fecha:
            continue
        m, d = dia(fecha)
        if es_prompt(e) and not sidechain:
            d["prompts"] += 1
            d["sesiones"].add(sesion)
            for cmd in _skills_de_prompt(e):
                m["skills"][cmd] = m["skills"].get(cmd, 0) + 1
        for b in blocks_de(e):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and e.get("type") == "assistant":
                nombre = b.get("name") or "?"
                if b.get("id") in nombres:
                    continue  # misma respuesta repetida en otra linea
                nombres[b.get("id")] = nombre
                d["tools"] += 1
                _contar_clase(_clasificar_tool(b), m["skills"], m["conectores"])
            elif b.get("type") == "tool_result" and nombres.get(b.get("tool_use_id")) in _TOOLS_EDICION:
                if _rechazado(e, b):
                    m["rechazadas"] += 1
                elif isinstance(e.get("toolUseResult"), dict):
                    m["aceptadas"] += 1
                    d["lineas"] += _lineas_aceptadas(e["toolUseResult"])


FAMILIAS_TOOL = (("Edicion", frozenset(_TOOLS_EDICION)),
                 ("Lectura", frozenset(("Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "NotebookRead"))),
                 ("Bash", frozenset(("Bash",))),
                 ("MCP", None))  # None = cualquier mcp__*
PROFUNDIDAD_TOOLS = (5, 20)


def _familia(nombre):
    for fam, nombres in FAMILIAS_TOOL:
        if (nombre.startswith("mcp__") if nombres is None else nombre in nombres):
            return fam
    return None


def uso_sesion(lineas):
    """Uso de ESTA sesion turno a turno (los graficos del dashboard de Claude con
    eje = turno). Misma numeracion que el loop: solo prompts con inferencias. None si no hay."""
    det = _turnos_detalle(lineas)
    filas = [{"turno": 0, "ts": t["ts"], "inferencias": len(t["infs"]), "tools": 0, "nativas": 0,
              "mcp": 0, "subagentes": 0, "lineas": 0, "familias": set()} for t in det]
    skills, conectores, acept, rech, nombres, k = {}, {}, 0, 0, {}, -1
    for e in lineas:
        if es_prompt(e):
            k += 1
            for cmd in _skills_de_prompt(e):
                skills[cmd] = skills.get(cmd, 0) + 1
            continue
        if k < 0 or not isinstance(e, dict):
            continue
        f = filas[k]
        for b in blocks_de(e):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and e.get("type") == "assistant" and b.get("id") not in nombres:
                n = nombres[b.get("id")] = b.get("name") or "?"
                f["tools"] += 1
                f["mcp" if n.startswith("mcp__") else "subagentes" if n in TOOLS_SUBAGENTE else "nativas"] += 1
                if _familia(n):
                    f["familias"].add(_familia(n))
                _contar_clase(_clasificar_tool(b), skills, conectores)
            elif b.get("type") == "tool_result" and nombres.get(b.get("tool_use_id")) in _TOOLS_EDICION:
                if _rechazado(e, b):
                    rech += 1
                elif isinstance(e.get("toolUseResult"), dict):
                    acept += 1
                    f["lineas"] += _lineas_aceptadas(e["toolUseResult"])
    filas = [f for f in filas if f["inferencias"]]
    if not filas:
        return None
    for n, f in enumerate(filas, 1):
        f["turno"] = n
    tools = [f["tools"] for f in filas]
    mitad = len(tools) // 2
    a, b = (sum(tools[:mitad]) / mitad, sum(tools[-mitad:]) / mitad) if mitad >= 2 else (0, 0)
    decididas = acept + rech
    n_t = len(filas)
    return {"turnos": [{k_: v for k_, v in f.items() if k_ != "familias"} for f in filas],
            "kpis": {"acciones_por_prompt": sum(tools) / n_t, "delta": (b / a - 1) if a else None,
                     "lineas": sum(f["lineas"] for f in filas), "aceptadas": acept, "rechazadas": rech,
                     "accept_rate": acept / decididas if decididas else None,
                     "inferencias": sum(f["inferencias"] for f in filas), "tools": sum(tools)},
            "profundidad": [["Todos los turnos", n_t], ["Con tools", sum(1 for x in tools if x)]]
                           + [[f"{p}+ tools", sum(1 for x in tools if x >= p)] for p in PROFUNDIDAD_TOOLS],
            "adherencia": [[fam, c, c / n_t] for fam, _ in FAMILIAS_TOOL
                           for c in [sum(1 for f in filas if fam in f["familias"])]],
            "skills": [[s, c] for s, c in sorted(skills.items(), key=lambda x: (-x[1], x[0]))],
            "conectores": sorted(({"servidor": s, **v} for s, v in conectores.items()),
                                 key=lambda x: -(x["lecturas"] + x["escrituras"]))}


def _cerrar_mes(mes, m, hoy):
    """Acumulador de un mes -> dict serializable con un dia por fecha calendario."""
    anio, num = int(mes[:4]), int(mes[5:])
    sig = datetime(anio + num // 12, num % 12 + 1, 1)
    n_dias = (sig - datetime(anio, num, 1)).days
    dias = []
    for i in range(1, n_dias + 1):
        fecha = f"{mes}-{i:02d}"
        d = m["dias"].get(fecha, _dia_vacio())
        dias.append({"fecha": fecha, "sesiones": len(d["sesiones"]), "prompts": d["prompts"],
                     "lineas": d["lineas"], "tools": d["tools"], "inferencias": d["inferencias"],
                     "tokens": d["tokens"], "costo": round(d["costo"], 4)})
    transcurridos = n_dias if hoy[:7] > mes else int(hoy[8:10]) if hoy[:7] == mes else 0
    activos = [d for d in dias if d["sesiones"]]
    ult7 = [d for d in dias if d["sesiones"] and d["fecha"] <= hoy and
            (datetime.strptime(hoy, "%Y-%m-%d") - datetime.strptime(d["fecha"], "%Y-%m-%d")).days < 7]
    suma = {k: sum(d[k] for d in dias) for k in ("lineas", "prompts", "tools", "inferencias", "tokens", "costo")}
    decididas = m["aceptadas"] + m["rechazadas"]
    sesiones = set().union(*(d["sesiones"] for d in m["dias"].values())) if m["dias"] else set()
    return {"mes": mes, "dias": dias,
            "totales": dict(suma, costo=round(suma["costo"], 4), sesiones=len(sesiones),
                            aceptadas=m["aceptadas"], rechazadas=m["rechazadas"],
                            accept_rate=m["aceptadas"] / decididas if decididas else None,
                            acciones_por_prompt=suma["tools"] / suma["prompts"] if suma["prompts"] else None,
                            dias_activos=len(activos), dias_activos_7d=len(ult7),
                            dias_transcurridos=transcurridos,
                            adherencia=len(activos) / transcurridos if transcurridos else None),
            "skills": sorted(m["skills"].items(), key=lambda x: (-x[1], x[0])),
            "conectores": sorted(({"servidor": s, **v} for s, v in m["conectores"].items()),
                                 key=lambda x: -(x["lecturas"] + x["escrituras"])),
            "modelos": sorted(m["modelos"].items(), key=lambda x: -x[1])}


def metricas_uso(raiz=None, ahora=None, meses=USO_MESES, proyecto=None):
    """Metricas de uso por dia y mes de TODAS las sesiones locales (todos los proyectos,
    sub-agentes incluidos), al estilo del dashboard de Claude pero de un solo usuario.
    Todo sale del transcript local: lineas aceptadas, accept rate, tools por prompt,
    skills, conectores MCP, dias activos y costo estimado. Con `proyecto` (carpeta bajo
    ~/.claude/projects) mide solo las sesiones de ese proyecto."""
    raiz = raiz or HOME / ".claude" / "projects"
    base = raiz / proyecto if proyecto else raiz
    ahora = ahora if ahora is not None else time.time()
    hoy = datetime.fromtimestamp(ahora)
    a, mm = hoy.year, hoy.month - (meses - 1)
    while mm < 1:
        a, mm = a - 1, mm + 12
    desde = f"{a:04d}-{mm:02d}"
    corte = datetime(a, mm, 1).timestamp()
    acc = {}
    for jsonl in (base.rglob("*.jsonl") if base.is_dir() else []):
        try:
            if jsonl.stat().st_mtime < corte:
                continue
            lineas = parsear_lineas(jsonl)
        except OSError:
            continue
        rel = jsonl.relative_to(raiz).parts
        _acumular_uso(acc, lineas, (rel[0], rel[1].split(".")[0]), sidechain=len(rel) > 2)
    hoy_s = hoy.strftime("%Y-%m-%d")
    claves = sorted(k for k in acc if desde <= k <= hoy_s[:7])
    if claves and hoy_s[:7] not in claves:
        claves.append(hoy_s[:7])
    return {"meses": [_cerrar_mes(k, acc.get(k, _mes_vacio()), hoy_s) for k in claves],
            "hoy": hoy_s, "fuente": "observed"}


def modelo_actual(filas):
    reales = [f.get("model") for f in filas if f.get("model") and f.get("model") != "<synthetic>"]
    return reales[-1] if reales else None


_EFFORT_ALTO = ("xhigh", "max")


def modelo_y_effort(lineas, filas):
    """Modelo (ultimo + lista en orden si cambio) y effort: el observado en el ultimo
    api-request del transcript; si no hay, effortLevel de settings.json; si no, unknown."""
    modelos = []
    for f in filas:
        m = f.get("model")
        if m and m != "<synthetic>" and (not modelos or modelos[-1] != m):
            modelos.append(m)
    effort, fuente = None, None
    for entry in lineas:
        if isinstance(entry, dict) and entry.get("type") == "api-request":
            e = ((entry.get("params") or {}).get("output_config") or {}).get("effort")
            if isinstance(e, str) and e:
                effort, fuente = e, "transcript"
    if not effort:
        e = leer_settings().get("effortLevel")
        effort, fuente = (e, "settings.json") if isinstance(e, str) and e else ("unknown", None)
    return {"modelo": modelos[-1] if modelos else None, "modelos": modelos,
            "effort": effort, "effort_fuente": fuente, "effort_alto": effort in _EFFORT_ALTO}


def linea_modelo(m):
    txt = f"Modelo: {m['modelo'] or 'desconocido'}"
    if len(m["modelos"]) > 1:
        txt += f" (cambio en la sesion: {' -> '.join(m['modelos'])})"
    txt += f" · effort: {m['effort']}" + (f" ({m['effort_fuente']})" if m["effort_fuente"] else "")
    if m["effort_alto"]:
        txt += " · effort alto: mas tokens de razonamiento por turno"
    return txt


def vista_contexto(lineas, filas, setup, ventana_flag=None):
    """Contexto de la ultima inferencia por categoria + listas de memoria y agentes."""
    modelo = modelo_actual(filas)
    config, _ = autocompact_config()
    if ventana_flag:
        ventana, fuente = ventana_flag, "flag"
    elif config:
        ventana, fuente = config, VAR_AUTOCOMPACT
    else:
        ventana, fuente = ventana_modelo(modelo), "estimada por modelo"
    ult = filas[-1]
    usado = ult["read"] + ult["write"] + ult["input"] + ult["output"]
    fijo = {k: 0.0 for k in _CATS_FIJAS}
    for c in setup["componentes"]:
        if c["tipo"] in fijo:
            fijo[c["tipo"]] += max(0.0, c["tok"])
    cats = [{"clave": k, "nombre": CATEGORIAS[k][0], "tok": v} for k, v in fijo.items() if v >= 0.5]
    mensajes = max(0.0, usado - sum(fijo.values()))
    # Con la ventana casi llena el buffer se recorta a lo que queda: las categorias
    # siempre cierran contra la ventana (igual que la barra de /context).
    buffer = min(AUTOCOMPACT_BUFFER, max(0.0, ventana - usado))
    libre = max(0.0, ventana - usado - buffer)
    cats += [{"clave": k, "nombre": _NOMBRE_CAT_EXTRA[k], "tok": v}
             for k, v in (("mensajes", mensajes), ("buffer", buffer), ("libre", libre))]
    lista = lambda tipo, quitar="": sorted(
        ({"label": c["label"][len(quitar):] if c["label"].startswith(quitar) else c["label"], "tok": c["tok"]}
         for c in setup["componentes"] if c["tipo"] == tipo), key=lambda x: -x["tok"])
    return {"modelo": modelo, "ventana": ventana, "ventana_fuente": fuente, "usado": usado,
            "pct": 100 * usado / ventana if ventana else 0, "categorias": cats,
            "memoria": lista("memoria"), "agentes": lista("agentes", "agente ")}


def imprimir_contexto(v):
    """Panel tipo /context en texto: barra, categorias y listas."""
    ancho = 50
    print(f"\nContexto actual (como /context) · {v['modelo'] or 'modelo desconocido'}")
    print(f"  {tok(round(v['usado']))} / {tok(v['ventana'])} tok ({v['pct']:.0f}%) · ventana {v['ventana_fuente']}")
    simbolos = {"buffer": "▒", "libre": "░"}
    barra = ""
    for c in v["categorias"]:
        barra += simbolos.get(c["clave"], "█") * round(ancho * c["tok"] / v["ventana"])
    print(f"  [{barra[:ancho].ljust(ancho, '░')}]")
    for c in v["categorias"]:
        print(f"  {c['nombre'].ljust(26)}{tok(round(c['tok'])).rjust(11)} tok {100 * c['tok'] / v['ventana']:5.1f}%")
    for titulo, items in (("Memory files", v["memoria"]), ("Custom agents", v["agentes"])):
        if items:
            print(f"  {titulo}:")
            for it in items:
                print(f"    {it['label'][:58].ljust(58)}{tok(round(it['tok'])).rjust(9)}")


_FUENTE_CPT = {"calibrado": "calibrado contra la 1a inferencia", "default": "default, sin api-request-shape",
               "flag": "fijado con --chars-por-token"}


def imprimir_setup(setup):
    """Sub-arbol del setup fijo: cada componente/seccion en tok/turno, % del setup y palanca (*)."""
    total = setup["total"]
    if total <= 0 or not setup["componentes"]:
        return

    def fila(indent, label, tok_val, editable):
        txt = " " * indent + label
        print(f"{txt[:56].ljust(56)}{tok(round(tok_val)).rjust(9)} "
              f"{100 * tok_val / total:3.0f}%  {palanca(tok_val, editable)}".rstrip())

    print(f"            se re-lee cada turno (fijo): {tok(round(total))} tok; composicion en tok/turno y % del setup:")
    editable_tot = 0.0
    for c in setup["componentes"]:
        fila(14, c["label"], c["tok"], c["editable"])
        if c["editable"]:
            editable_tot += c["tok"]
        for titulo, tk_val in c["secc"] or []:
            fila(18, titulo, tk_val, c["editable"])
    print(f"            {'-' * 51}")
    fila(14, "TOTAL setup fijo (100%)", total, False)
    fila(14, "de eso, editable (con palanca *)", editable_tot, False)
    print(f"            chars->tok = {setup['ratio']:.2f} ({_FUENTE_CPT[setup['ratio_fuente']]})")
    print("            (*) palanca = tok/turno editables que se ahorran al recortar: ***>=1000 **>=300 *<300; sin marca = base no editable")


def imprimir_composicion(comp, setup, acum_read):
    """Reparte el cache-read acumulado (real) segun la composicion estimada del prefijo."""
    pref = comp["prefijo"]
    if pref <= 0:
        return

    def linea(label, tok_prefijo, indent):
        prop = tok_prefijo / pref
        print(f"{' ' * indent}{label.ljust(54 - indent)}"
              f"{tok(round(prop * acum_read)).rjust(11)} tok {prop * 100:4.0f}%")

    print("      de eso, por composicion del prefijo (se re-lee ENTERO cada turno; reparto estimado):")
    linea("setup fijo (system+tool-defs+CLAUDE.md+skills)", comp["setup"], 8)
    imprimir_setup(setup)
    if comp["prompts"]:
        linea("prompt (input)", comp["prompts"], 8)
    tools_tot = sum(comp["tools"].values())
    if tools_tot:
        linea("tool results (lecturas/comandos inline)", tools_tot, 8)
        for name, tk in sorted(comp["tools"].items(), key=lambda kv: -kv[1]):
            linea(name, tk, 12)
    if comp["output"]:
        linea("prompt (output) (thinking+texto+tool calls)", comp["output"], 8)


# ============================================================================
# --html: reporte HTML self-contained (house-style de evidence-report/qa-deliverable).
# Cero red, cero CDN, cero deps: charts en <canvas> dibujados a mano, tema
# dark/light con toggle persistido, data embebida como JSON inline. Reusa
# exactamente las mismas funciones que el reporte de texto (composicion_prefijo,
# desglose_setup, costo, palanca) — ningun numero se recalcula distinto.
# ============================================================================
HTML_THEME_KEY = "claudit-theme"

# Favicon inline: barras ascendentes cyan, la identidad visual de claudit. Se
# embebe como data-URI base64 en el <head> (self-contained, CSP-safe: sin red).
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    '<rect width="512" height="512" rx="112" fill="#0d1117"/>'
    '<rect x="104" y="300" width="60" height="108" rx="16" fill="#1fd5f9" opacity=".45"/>'
    '<rect x="192" y="240" width="60" height="168" rx="16" fill="#1fd5f9" opacity=".62"/>'
    '<rect x="280" y="172" width="60" height="236" rx="16" fill="#1fd5f9" opacity=".8"/>'
    '<rect x="368" y="104" width="60" height="304" rx="16" fill="#1fd5f9"/>'
    "</svg>"
)
_FAVICON_B64 = base64.b64encode(_FAVICON_SVG.encode()).decode()


def _esc(s):
    return html.escape(str(s), quote=True)


def _tok0(x):
    return tok(round(x))


def _kpi_cards_html(acum, total_usd, crec, n_inferencias):
    def card(valor, label, desc):
        return (f'<div class="metric" data-tip="{_esc(desc)}"><strong>{_esc(valor)}</strong>'
                f'<span>{_esc(label)}</span></div>')

    valores = [
        (_tok0(acum["read"]), "cache-read total (tok)"),
        (_tok0(acum["write"]), "cache-write total (tok)"),
        (_tok0(acum["output"]), "output total (tok)"),
        (usd(total_usd), "costo total estimado"),
        ((f"{crec:.1f}x" if crec else "—"), "crecimiento cache-read"),
        (str(n_inferencias), "inferencias"),
    ]
    return "\n".join(card(v, l, d) for (v, l), d in zip(valores, _DESC_KPI))


# Textos pedagogicos de los tooltips: que ES cada metrica/componente (no numeros nuevos).
_DESC_KPI = [
    "Prefijo cacheado que se re-lee en CADA inferencia, sumado en toda la sesion. "
    "Cuesta 0.1x un token de input, pero es lo que MAS gasta porque se re-lee siempre.",
    "Contenido nuevo escrito al cache (una vez por turno). Cuesta 1.25x input con TTL "
    "5 min o 2x con TTL 1 h; "
    "se paga una sola vez, no domina el gasto.",
    "Lo que genera el modelo: thinking + texto + tool calls. El token mas caro (5x input).",
    "USD estimado = read(0.1x) + write(1.25x TTL 5 min / 2x TTL 1 h) + input(1x) + "
    "output, al precio del modelo de cada inferencia. Edita PRECIOS_MODELO si cambia.",
    "Cuanto crecio el cache-read de la 1ra a la ultima inferencia. 10x = al final el "
    "prefijo re-leido es 10 veces mas grande.",
    "Llamadas al modelo en la sesion. Una respuesta tuya puede disparar varias (cada "
    "tool call es una inferencia).",
]

_DESC_COMP = {
    "setup fijo": "System prompt + definiciones de tools + CLAUDE.md + skills + hooks "
                  "que Claude Code auto-carga al arrancar. Piso fijo que se re-lee entero cada turno.",
    "prompt (input)": "El texto que escribe el usuario. Casi siempre la porcion mas chica del prefijo.",
    "tool results (total)": "Salida de las herramientas (archivos leidos, comandos, edits, "
                             "busquedas), acumulada en el hilo y re-leida cada turno.",
    "prompt (output)": "El thinking + texto + tool calls que genera el modelo. Todo eso pasa a ser "
                        "parte del prefijo re-leido en los turnos siguientes.",
}

_DESC_COMP_HIJOS = {
    "Read": "Contenido de archivos leidos con Read, acumulado en la sesion.",
    "Bash": "Salida de comandos Bash, acumulada.",
    "Edit": "Diffs de ediciones con Edit, acumulados.",
    "Write": "Contenido de archivos escritos con Write, acumulado.",
}


def _desc_comp_item(label):
    """Desc pedagogica de una barra de composicion, mapeada por label."""
    stripped = label.strip()
    if stripped in _DESC_COMP:
        return _DESC_COMP[stripped]
    if stripped.startswith("· "):
        nombre = stripped[2:].strip()
        if nombre in _DESC_COMP_HIJOS:
            return _DESC_COMP_HIJOS[nombre]
        return f"Salida de la tool {nombre}, acumulada en la sesion."
    return ""


def _items_composicion(comp, acum_read):
    """Barras horizontales: cada componente del prefijo, con su % y los tokens
    reales de cache-read que le corresponden (comp/prefijo * acum_read)."""
    pref = comp["prefijo"] or 1
    items = []

    def add(label, val, color):
        if val <= 0:
            return
        pct = val / pref
        items.append({
            "label": label, "value": round(val, 2), "pct": round(pct, 4),
            "tokLabel": _tok0(pct * acum_read), "color": color,
            "desc": _desc_comp_item(label),
        })

    add("setup fijo", comp["setup"], "neutral")
    add("prompt (input)", comp["prompts"], "primary")
    tools_tot = sum(comp["tools"].values())
    add("tool results (total)", tools_tot, "mid")
    for name, v in sorted(comp["tools"].items(), key=lambda kv: -kv[1]):
        add(f"  · {name}", v, "mid")
    add("prompt (output)", comp["output"], "bad")
    return items


def _hora_local(ts):
    # ts en ISO 8601 UTC (con Z): "2026-07-18T17:33:46.748Z". Se convierte a la hora
    # LOCAL de la maquina que genera el reporte, con el offset explicito. En una PC
    # en Buenos Aires da "14:33:46 (UTC-3)"; en otro huso, el suyo. Sin hardcodear ciudad.
    if not (isinstance(ts, str) and len(ts) >= 19 and ts[10:11] == "T"):
        return ""
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        loc = dt.astimezone()  # huso local de la maquina
        off = loc.utcoffset()
        total = int(off.total_seconds() // 60) if off else 0
        signo = "+" if total >= 0 else "-"
        hh, mm = divmod(abs(total), 60)
        etiqueta = f"UTC{signo}{hh}" + (f":{mm:02d}" if mm else "")
        return loc.strftime("%H:%M:%S") + f" ({etiqueta})"
    except ValueError:
        return ts[11:19] + " UTC"


# Gap con la inferencia previa por debajo de este umbral => misma tanda de
# tool-calls encadenados (hermana): comparten el cache re-escrito, no son un
# evento de write nuevo. Se colorean distinto para no leer 1 evento como N.
_UMBRAL_HERMANA_S = 30
# Caida del contexto entre dos inferencias que delata un /compact (queda < 70%).
COMPACT_CAIDA = 0.7


def _ts_epoch(ts):
    # ISO 8601 UTC ("2026-07-18T17:33:46.748Z") -> epoch en segundos. None si no parsea.
    if not (isinstance(ts, str) and len(ts) >= 19 and ts[10:11] == "T"):
        return None
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _ctx_tok(f):
    return f.get("read", 0) + f.get("write", 0) + f.get("input", 0)


def _es_compact(antes, ahora):
    return antes.get("read", 0) > 0 and _ctx_tok(ahora) < COMPACT_CAIDA * _ctx_tok(antes)


def _es_reescritura(antes, ahora):
    """Cache vencida (TTL): el prefijo entero vuelve a escribirse sin achicarse."""
    return antes.get("read", 0) > 0 and ahora.get("write", 0) >= 0.5 * _ctx_tok(ahora)


def _costos_inducidos(filas, desde=None):
    """USD que cada cache-write termina costando: escribirlo + re-leerlo en cada
    inferencia posterior hasta que el prefijo se reinicia (/compact: el contexto cae
    mas de un 30%; el auto-compact real deja ~50%). Es lo que esa inferencia le resto al cupo. Con `desde`
    (inicio del bloque de 5 h) solo cuenta lo consumido dentro del bloque."""
    en = [desde is None or (_ts_epoch(f.get("ts")) or 0) >= desde for f in filas]
    n = len(filas)
    fin = [n] * n  # primera inferencia que ya no re-lee lo escrito en i
    for j in range(n - 1, 0, -1):
        compact = _es_compact(filas[j - 1], filas[j]) or _es_reescritura(filas[j - 1], filas[j])
        fin[j - 1] = j if compact else fin[j]
    lectura = [precios(f.get("model"))["cache_read"] / 1e6 if en[j] else 0.0 for j, f in enumerate(filas)]
    acum = [0.0] * (n + 1)
    for j in range(n - 1, -1, -1):
        acum[j] = acum[j + 1] + lectura[j]
    escribir = lambda f: f.get("w5m", f.get("write", 0)) * precios(f.get("model"))["cache_write_5m"] / 1e6 \
        + f.get("w1h", 0) * precios(f.get("model"))["cache_write_1h"] / 1e6
    return [escribir(f) * en[i] + f.get("write", 0) * (acum[i + 1] - acum[fin[i]])
            for i, f in enumerate(filas)]


def _items_write(filas, desde=None):
    items, prev = [], None
    inducidos = _costos_inducidos(filas, desde)
    for i, f in enumerate(filas):
        ep = _ts_epoch(f.get("ts"))
        sibling = prev is not None and ep is not None and (ep - prev) < _UMBRAL_HERMANA_S
        rol = ("re-escritura tras un auto-compact: el resumen + el setup vuelven a entrar al cache"
               if i and _es_compact(filas[i - 1], f) else
               "cache vencida re-escrita: el prefijo entero vuelve a entrar al cache"
               if i and _es_reescritura(filas[i - 1], f) else
               "hermana: tool-call encadenado del mismo turno" if sibling
               else "arranque de turno (tras tu prompt o una pausa)")
        items.append({
            "inferencia": i + 1, "value": f["write"], "hora": _hora_local(f.get("ts")),
            "sibling": sibling, "costo_inducido": inducidos[i],
            "desc": f"Inferencia {i + 1} · {rol}. Tokens nuevos escritos al cache en esta llamada a la API.",
        })
        if ep is not None:
            prev = ep
    return items


def _aria_composicion(items):
    partes = [f'{it["label"].strip()} {round(it["pct"] * 100)}% ({it["tokLabel"]} tok)' for it in items]
    return "Composicion del cache-read por componente del prefijo: " + "; ".join(partes) + "."


def _aria_write(items):
    if not items:
        return "Cache-write por inferencia: sin datos."
    pico = max(items, key=lambda it: it["value"])
    partes = [f'inferencia {it["inferencia"]}: {tok(it["value"])} tok' for it in items]
    return (f"Cache-write a lo largo de {len(items)} inferencias, "
            f"pico de {tok(pico['value'])} tok en la inferencia {pico['inferencia']}. Detalle: "
            + ", ".join(partes) + ".")


def _loop_html(m):
    """KPIs del loop agentico + palancas con numeros reales (o 'loop sano')."""
    if not m:
        return '<p class="muted">Loop agentico: sin turnos con inferencias en esta sesion.</p>'
    cards = [
        (f"{m['prom']:.1f}", "inferencias / turno",
         f"Promedio de llamadas a la API por prompt tuyo. El turno mas largo fue el {m['max_turno']} con {m['max']}."),
        (f"{m['pct_paralelo']:.0%}", "inferencias en paralelo",
         f"{m['paralelas']} de {m['con_tools']} inferencias con tools lanzaron 2+ tools a la vez. "
         "Cada inferencia re-lee todo el cache: menos inferencias = menos cache-read."),
        (str(m["cadena_max"]), "cadena secuencial max",
         "Inferencias seguidas de UNA sola tool dentro de un mismo turno (lectura exploratoria encadenada)."
         + (f" Ocurrio en el turno {m['cadena_turno']}." if m["cadena_turno"] else "")),
        (_tok0(m["read_hermanas"]), "cache-read en hermanas (tok)",
         "Cache-read de las inferencias posteriores a la primera de cada turno: el costo de encadenar tool-calls."),
        (f"{m['read_hermanas'] / m['read_total']:.0%}" if m["read_total"] else "—", "del cache-read total",
         "Que parte del cache-read de la sesion se fue en inferencias hermanas."),
        (str(m["subagentes"]), "sub-agentes lanzados",
         "Tool calls Agent/Task. Un sub-agente explora en su propio contexto y no re-lee el tuyo en cada paso."),
    ]
    html = '<div class="metric-boxes">' + "".join(
        f'<div class="metric" data-tip="{_esc(d)}"><strong>{_esc(v)}</strong><span>{_esc(l)}</span></div>'
        for v, l, d in cards) + "</div>"
    pal = palancas_loop(m)
    if pal:
        html += '<ul class="tips-list">' + "".join(f"<li>{_esc(p)}</li>" for p in pal) + "</ul>"
    else:
        html += '<p class="muted">Loop sano: sin cadenas largas ni turnos que pidan delegar.</p>'
    return html


SKILLS_POR_PAGINA = 12


def _skills_html(filas, componentes):
    """Pestana Skills: cada skill con proveedor/fuente/desde, filtros por proveedor, paginador,
    descarga en Excel y las versiones de Claude Code vistas."""
    if not filas:
        return '<p class="muted">sin skill_listing en esta sesion.</p>'
    conteo = {}
    for f in filas:
        conteo[f["proveedor"]] = conteo.get(f["proveedor"], 0) + 1
    boton = lambda prov, txt, on=False: (f'<button type="button" data-prov="{_esc(prov)}" '
                                          f'aria-pressed="{"true" if on else "false"}">{_esc(txt)}</button>')
    filtros = ('<div class="sk-filtros" role="group" aria-label="Filtrar skills por proveedor">'
               + boton("", f"Todas ({len(filas)})", True)
               + "".join(boton(p, f"{p} ({n})") for p, n in sorted(conteo.items(), key=lambda x: (-x[1], x[0])))
               + '</div>')
    fila = lambda f: (f'<tr data-prov="{_esc(f["proveedor"])}"><td>{_esc(f["skill"])}</td>'
                      f'<td>{_esc(f["proveedor"])}</td><td>{_esc(f["fuente"])}</td>'
                      f'<td class="num">{_tok0(f["tok"])}</td><td>{_esc(f["desde"] or "—")}</td>'
                      f'<td>{_esc(f["cc"] or "—")}</td><td>{_esc(f["version"] or "—")}</td></tr>')
    tabla = ('<div class="sk-tabla"><table class="setup-tree sk-lista"><thead><tr><th>Skill</th><th>Proveedor</th>'
             '<th>Fuente</th><th class="num">tok/turno</th><th>Vista desde</th><th>Claude Code</th>'
             '<th>Version skill</th></tr></thead><tbody>' + "".join(fila(f) for f in filas) + '</tbody></table></div>')
    nav = (f'<div class="sk-pag" data-por="{SKILLS_POR_PAGINA}">'
           '<button type="button" data-dir="-1" aria-label="Pagina anterior">&lsaquo;</button>'
           '<span aria-live="polite">1 / 1</span>'
           '<button type="button" data-dir="1" aria-label="Pagina siguiente">&rsaquo;</button>'
           f'<span class="muted sk-total">{len(filas)} skills · A–Z</span>'
           '<button type="button" class="sk-xlsx" id="sk-xlsx">Descargar Excel</button></div>')
    datos = sorted(filas, key=lambda f: (f["proveedor"].casefold(), f["skill"].casefold()))
    js = json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")
    return (filtros + tabla + nav
            + f'<script type="application/json" id="claudit-data-skills">{js}</script>'
            + _versiones_html(componentes))


MESES_CORTOS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
VS_COLORES = 8        # slots categoricos validados (--vs-1..8); mas alla, gris: nunca se recicla un color
CAL_MAX_MESES = 12


def _color_vs(i):
    return f"var(--vs-{i + 1})" if i < VS_COLORES else "var(--neutral)"


def _calendario_html(comps):
    """Un mini-mes por mes entre el primer y el ultimo evento; cada dia con eventos lleva un punto
    del color de cada componente y el detalle en tooltip (hover y foco)."""
    por_dia = {}
    for i, c in enumerate(comps):
        for dia, txt in c["eventos"]:
            por_dia.setdefault(dia, []).append((i, f'{c["nombre"]} {txt}'))
    if not por_dia:
        return ""
    y1, m1 = map(int, min(por_dia)[:7].split("-"))
    y2, m2 = map(int, max(por_dia)[:7].split("-"))
    meses = [(y, m) for y in range(y1, y2 + 1) for m in range(1, 13) if (y1, m1) <= (y, m) <= (y2, m2)]
    cal = calendar.Calendar(firstweekday=0)
    out = []
    for y, m in meses[-CAL_MAX_MESES:]:
        filas = []
        for semana in cal.monthdayscalendar(y, m):
            celdas = []
            for d in semana:
                dia = f"{y}-{m:02d}-{d:02d}"
                ev = por_dia.get(dia) if d else None
                if not d:
                    celdas.append("<td></td>")
                elif not ev:
                    celdas.append(f"<td>{d}</td>")
                else:
                    tip = _esc(" · ".join(t for _, t in ev))
                    puntos = "".join(f'<i class="cal-dot" style="--c:{_color_vs(i)}"></i>' for i, _ in ev)
                    celdas.append(f'<td class="cal-ev" tabindex="0" data-tip-title="{dia}" data-tip="{tip}" '
                                  f'aria-label="{dia}: {tip}">{d}<span class="cal-dots">{puntos}</span></td>')
            filas.append("<tr>" + "".join(celdas) + "</tr>")
        out.append(f'<table class="cal-mes"><caption>{MESES_CORTOS[m - 1]} {y}</caption><thead><tr>'
                   + "".join(f'<th scope="col">{x}</th>' for x in "LMMJVSD")
                   + "</tr></thead><tbody>" + "".join(filas) + "</tbody></table>")
    leyenda = "".join(f'<li><i class="cal-dot" style="--c:{_color_vs(i)}"></i>{_esc(c["nombre"])}</li>'
                      for i, c in enumerate(comps))
    return (f'<div class="vs-cal"><h4>Calendario de versiones en esta máquina</h4>'
            f'<ul class="cal-leyenda">{leyenda}</ul><div class="cal-meses">{"".join(out)}</div></div>')


def _versiones_html(comps):
    """Versiones vistas: version propia de cada componente (la que declara quien lo publica) y desde
    cuando corre en ESTA maquina, con calendario mes a mes."""
    if not comps:
        return ""
    fila = lambda i, c: (f'<tr><td><i class="vs-sw" style="--c:{_color_vs(i)}"></i>{_esc(c["nombre"])}'
                         f'<small class="muted">{_esc(c["fuente"])}</small></td><td>{_esc(c["version"])}</td>'
                         f'<td>{_esc(c["desde"])}</td><td>{_esc(c["actualizada"])}</td></tr>')
    tabla = ('<table class="setup-tree vs-tabla"><thead><tr><th>Componente</th><th>Versión</th>'
             '<th>En esta máquina desde</th><th>Última actualización</th></tr></thead><tbody>'
             + "".join(fila(i, c) for i, c in enumerate(comps)) + "</tbody></table>")
    return ('<h3>Versiones vistas</h3><p class="muted">La versión es la que declara cada componente '
            '(igual en todas las máquinas); las fechas son de <strong>esta</strong> máquina: transcripts '
            'para Claude Code, <code>installed_plugins.json</code> para plugins y fecha de los archivos para '
            'las skills de <code>~/.claude/skills</code>, agrupadas por <code>author</code>. "sin versión" = '
            'el componente no declara una.</p>'
            f'<div class="vs-grid"><div class="vs-col">{tabla}</div>{_calendario_html(comps)}</div>')


def _skills_del_setup_html(setup):
    """Seccion Skills: las skills del skill_listing de la sesion + versiones vistas en esta maquina."""
    crudas = next((c.get("crudas") or [] for c in setup["componentes"] if c.get("tipo") == "skills"), [])
    listados = escanear_listados()
    return _skills_html(filas_skills(crudas, historial_skills(listados)), componentes_versiones(listados))


def _tip_sub(c):
    """Sub-desglose de un tip: skills completas y paginadas (A-Z); secciones agrupadas por concepto."""
    fila = lambda t, k, cls="": (f'<li{cls}><span>{_esc(t)}</span><code>{_tok0(k)} tok</code></li>')
    crudas = c.get("crudas")
    if c.get("tipo") == "skills" and crudas:
        items = "".join(f'<li data-pag="{i // SKILLS_POR_PAGINA + 1}"><span>{_esc(t)}</span>'
                        f'<code>{_tok0(k)} tok</code></li>' for i, (t, k) in enumerate(crudas))
        paginas = -(-len(crudas) // SKILLS_POR_PAGINA)
        nav = ("" if paginas < 2 else
               f'<li class="tip-pag" data-paginas="{paginas}">'
               '<button type="button" data-dir="-1" aria-label="Pagina anterior">&lsaquo;</button>'
               f'<span aria-live="polite">1 / {paginas}</span>'
               '<button type="button" data-dir="1" aria-label="Pagina siguiente">&rsaquo;</button>'
               f'<span class="muted">{len(crudas)} skills · A–Z</span></li>')
        return items + nav
    if crudas:
        out = []
        for n in agrupar_conceptos(crudas):
            if n["hijos"]:
                out.append(fila(n["titulo"], n["tok"], ' class="tip-grupo"'))
                out += [fila(t, k, ' class="tip-hijo"') for t, k in n["hijos"]]
            else:
                out.append(fila(n["titulo"], n["tok"]))
        return "".join(out)
    return "".join(fila(t, k) for t, k in c.get("secc") or [])


def _tips_html(setup):
    """Top componentes editables del setup fijo, ordenados por tok/turno desc."""
    editables = sorted([c for c in setup["componentes"] if c["editable"]], key=lambda c: -c["tok"])[:5]
    if not editables:
        return '<li class="muted">No hay componentes editables medibles en esta sesion.</li>'
    filas = []
    for c in editables:
        marca = palanca(c["tok"], True)
        marca_html = f'<span class="tip-marca">{_esc(marca)}</span>' if marca else '<span class="tip-marca muted">—</span>'
        sub = _tip_sub(c)
        filas.append(
            f'<li>{marca_html} <strong>{_esc(c["label"])}</strong> '
            f'<span class="muted">— {_tok0(c["tok"])} tok/turno</span>'
            + (f'<ul class="tip-sub">{sub}</ul>' if sub else "") + '</li>'
        )
    return "\n".join(filas)


def _setup_tree_html(setup):
    """Arbol COMPLETO del setup fijo para el HTML: cada componente + cada seccion
    del CLAUDE.md, con tok/turno, % del setup y palanca. Espeja imprimir_setup
    (misma data de desglose_setup: fuente unica, terminal y HTML no divergen)."""
    total = setup["total"]
    if total <= 0 or not setup["componentes"]:
        return '<tr><td colspan="4" class="muted">Sin datos de setup fijo en esta sesion.</td></tr>'

    def marca(tok_val, editable):
        m = palanca(tok_val, editable)
        return f'<span class="p-mark">{m}</span>' if m else '<span class="p-none">—</span>'

    def pct(tok_val):
        return f"{round(100 * tok_val / total)}%"

    filas, editable_tot = [], 0.0
    for c in setup["componentes"]:
        if c["editable"]:
            editable_tot += c["tok"]
        cls = "r-comp" if c["editable"] else "r-comp r-base"
        filas.append(
            f'<tr class="{cls}"><td>{_esc(c["label"])}</td>'
            f'<td>{_tok0(c["tok"])}</td><td>{pct(c["tok"])}</td>'
            f'<td>{marca(c["tok"], c["editable"])}</td></tr>'
        )
        for titulo, tk_val in c["secc"] or []:
            filas.append(
                f'<tr class="r-secc"><td>{_esc(titulo)}</td>'
                f'<td>{_tok0(tk_val)}</td><td>{pct(tk_val)}</td>'
                f'<td>{marca(tk_val, c["editable"])}</td></tr>'
            )
    filas.append(
        f'<tr class="r-total"><td>TOTAL setup fijo</td>'
        f'<td>{_tok0(total)}</td><td>100%</td><td></td></tr>'
    )
    filas.append(
        f'<tr class="r-sub"><td>de eso, editable (con palanca)</td>'
        f'<td>{_tok0(editable_tot)}</td><td>{pct(editable_tot)}</td><td></td></tr>'
    )
    if setup.get("ratio"):
        filas.append(
            f'<tr class="r-sub"><td colspan="4" class="muted">chars&rarr;tok = {setup["ratio"]:.2f} '
            f'({_esc(_FUENTE_CPT[setup["ratio_fuente"]])})</td></tr>'
        )
    return "\n".join(filas)


def _contexto_html(v):
    """Seccion tipo /context: barra apilada por categoria, tabla y listas de
    memoria/agentes. Misma data que imprimir_contexto (vista_contexto)."""
    ventana = v["ventana"] or 1

    def pct(t):
        return 100 * t / ventana

    segs = "".join(
        f'<span class="ctx-seg k-{c["clave"]}" style="width:{pct(c["tok"]):.3f}%" '
        f'title="{_esc(c["nombre"])}: {_tok0(c["tok"])} tok ({pct(c["tok"]):.1f}%)"></span>'
        for c in v["categorias"] if c["tok"] > 0)
    filas = "".join(
        f'<tr><td><span class="ctx-dot k-{c["clave"]}"></span>{_esc(c["nombre"])}</td>'
        f'<td>{_tok0(c["tok"])}</td><td>{pct(c["tok"]):.1f}%</td></tr>'
        for c in v["categorias"])
    listas = ""
    for titulo, items in (("Memory files", v["memoria"]), ("Custom agents", v["agentes"])):
        if items:
            lis = "".join(f'<li><span>{_esc(it["label"])}</span><code>{_tok0(it["tok"])}</code></li>'
                          for it in items)
            listas += f'<div class="ctx-list"><h3>{titulo}</h3><ul>{lis}</ul></div>'
    return (
        f'<p class="ctx-head"><strong>{_esc(v["modelo"] or "modelo desconocido")}</strong> · '
        f'{_tok0(v["usado"])} / {_tok0(v["ventana"])} tokens ({v["pct"]:.0f}%) '
        f'<span class="muted">· ventana {_esc(v["ventana_fuente"])}</span></p>'
        f'<div class="ctx-bar" role="img" aria-label="Contexto usado {v["pct"]:.0f}%">{segs}</div>'
        f'<div class="ctx-grid"><table class="setup-tree ctx-table"><thead><tr><th>Categoria</th>'
        f'<th>tok</th><th>% ventana</th></tr></thead><tbody>{filas}</tbody></table>'
        f'<div>{listas}</div></div>'
    )


def _curva_contexto_html(a):
    """Curva de contexto por inferencia (SVG inline) con la linea del umbral de
    auto-compact y la del corte recomendado (200k - buffer)."""
    ctx = a["contextos"]
    if not ctx:
        return '<p class="muted">Sin inferencias.</p>'
    w, h, pad = 1000, 240, 18
    # Escala al contexto real; si el umbral queda muy arriba (1M) se fija al borde
    # superior con la leyenda "fuera de escala" para que la curva siga legible.
    tope = max(a["max"], a["corte"]) * 1.1
    if a["umbral"] <= tope * 1.6:
        tope = max(tope, a["umbral"] * 1.05)
    x = lambda i: pad + (w - 2 * pad) * (i / max(1, len(ctx) - 1))
    y = lambda v: h - pad - (h - 2 * pad) * min(v, tope) / tope
    pts = " ".join(f"{x(i):.1f},{y(c):.1f}" for i, c in enumerate(ctx))
    fuera = " (fuera de escala ↑)" if a["umbral"] > tope else ""
    lineas = ""
    for cls, val, txt in (("ctx-umbral", a["umbral"], f"auto-compact ~{_tok0(a['umbral'])}{fuera}"),
                          ("ctx-corte", a["corte"], f"recomendado {_tok0(a['corte'])}")):
        if not (cls == "ctx-corte" and val == a["umbral"]):
            lineas += (f'<line class="{cls}" x1="0" x2="{w}" y1="{y(val):.1f}" y2="{y(val):.1f}"/>'
                       f'<text class="{cls}" x="{w - 8}" y="{y(val) - 6:.1f}" text-anchor="end">{txt}</text>')
    alerta = ""
    if a["alerta"]:
        alerta = (f'<div class="ctx-alerta"><strong>Sin {VAR_AUTOCOMPACT}:</strong> el auto-compact salta recien a '
                  f'~{_tok0(a["umbral"])}. {a["turnos_sobre"]} turnos por encima de {_tok0(a["corte"])} costaron '
                  f'~{_esc(usd(a["costo_sobre"]))}. Fix en <code>~/.claude/settings.json</code>:'
                  f'<pre>{_esc(a["fix"])}</pre></div>')
    return (f'{alerta}<svg class="ctx-curva" viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="Contexto por inferencia, maximo {_tok0(a["max"])}">{lineas}'
            f'<polyline points="{pts}"/></svg>'
            f'<p class="muted">{len(ctx)} inferencias · maximo {_tok0(a["max"])} tok</p>')


def _retomadas_html(eventos):
    if not eventos:
        return ""
    lis = "".join(f'<li><span>{_esc(ev["hora"])} · pausa {_duracion(ev["hueco_s"])} · re-write '
                  f'{_tok0(ev["rewrite"])} tok</span><code>{_esc(usd(ev["costo"]))}</code></li>'
                  for ev in eventos)
    return (f'<div class="ctx-list"><h3>Retomada con cache vencida</h3><ul>{lis}</ul>'
            f'<p class="muted">Recomendacion: {_esc(_RECO_RETOMADA)}.</p></div>')


_SVG_WARN = ('<svg class="h-warn" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2 21h20L12 3z"/>'
             '<path d="M12 10v5M12 18v.01"/></svg>')


def _estado_hook_html(h):
    """Ok verde, o nOk rojo con icono de aviso + motivos cortos; el detalle completo y
    que significa van en el tooltip del boton '?' (hover o foco)."""
    if not h["degradado"]:
        return '<span class="h-ok">Ok</span>'
    fallas = h.get("fallas") or [{"corto": m.split(":")[0][:20], "detalle": m, "que": ""}
                                 for m in (h.get("motivo") or "degradado").split("; ")]
    cortos = " · ".join(f'<span class="h-corto">{_esc(f["corto"])}</span>' for f in fallas)
    tip = " ".join(f'{f["detalle"]} — {f["que"]}'.rstrip(" —") for f in fallas)
    return (f'<span class="h-nok">{_SVG_WARN}nOk</span> {cortos} '
            f'<button type="button" class="h-ayuda" aria-label="Que significa: {_esc(tip)}" '
            f'data-tip-title="{_esc(h["label"])}" data-tip="{_esc(tip)}">?</button>')


def _primer_turno_html(ts_):
    """1.er turno como lista: una viñeta por tool (mas usada primero); MCP abreviado."""
    if not ts_:
        return '<p class="muted">1.er turno: sin prompts</p>'
    t = ts_[0]
    tools = sorted(t["tools"].items(), key=lambda kv: (-kv[1], kv[0]))
    def nombre(n):
        if n.startswith("mcp__") and n.count("__") >= 2:
            _, srv, tool_ = n.split("__", 2)
            return f'<code>{_esc(tool_)}</code> <span class="muted">MCP {_esc(srv)}</span>'
        return f"<code>{_esc(n)}</code>"
    items = "".join(f'<li title="{_esc(n)}">{nombre(n)}<b>×{c}</b></li>' for n, c in tools)
    return (f'<div class="h-turno"><p><strong>1.er turno</strong> · {t["inferencias"]} inferencias · '
            f'{sum(t["tools"].values())} tool calls</p>'
            + (f'<ul class="h-tools">{items}</ul>' if items else '<p class="muted">sin tools</p>') + '</div>')


def _salud_hooks_html(actual, avisos):
    if not actual:
        return '<p class="muted">Sin hooks en el arranque.</p>'
    filas = "".join(
        f'<tr><td>{_esc(h["label"])}</td><td>{_tok0(h["chars"])}</td>'
        f'<td class="h-estado">{_estado_hook_html(h)}</td></tr>'
        for h in actual)
    av = "".join(f'<div class="ctx-alerta">{_esc(a["label"])} inyecta mucho menos que en la sesion previa: '
                 f'{_tok0(a["antes"])} &rarr; {_tok0(a["ahora"])} chars.</div>' for a in avisos)
    return (f'{av}<table class="setup-tree"><thead><tr><th>Hook</th><th>chars</th><th>estado</th></tr></thead>'
            f'<tbody>{filas}</tbody></table>')


def _cupo_html(c):
    lim = c["limites"]["valor"]
    filas = [("Tokens", _tok0(c["tokens"]["valor"]), c["tokens"]["fuente"]),
             ("Costo", _esc(usd(c["costo"]["valor"])), c["costo"]["fuente"]),
             ("Sesiones activas", f'{c["sesiones"]["valor"]} · {c["proyectos"]["valor"]} proyectos', c["sesiones"]["fuente"]),
             ("Eventos de limite", f'{len(lim)}' + (f' · {_esc(", ".join(lim[:5]))}' if lim else ""), c["limites"]["fuente"]),
             ("Cupo de la cuenta", "—", c["cupo_cuenta"]["fuente"])]
    trs = "".join(f'<tr><td>{n}</td><td>{v}</td><td><span class="prov prov-{f}">{f}</span></td></tr>'
                  for n, v, f in filas)
    return (f'<table class="setup-tree"><thead><tr><th>Ultimas {c["horas"]} h</th><th>valor</th>'
            f'<th>procedencia</th></tr></thead><tbody>{trs}</tbody></table>')


_NOMBRE_PERFIL = {"peligroso": "Peligroso", "moderado": "Moderado", "saludable": "Saludable"}
_ICONO_PERFIL = {"peligroso": "\u26a0", "moderado": "\u25d0", "saludable": "\u2713"}


def _perfil_html(b, ctx_pct, ahora):
    """Perfil de uso. Sin el % de la cuenta, el perfil sale del contexto (y de un
    rate_limit observado); el JS lo recalcula al cargar el % de Account & Usage."""
    nivel = nivel_perfil(ctx_pct, CORTES_PERFIL["contexto"])
    motivo = f"Contexto al {ctx_pct:.0f}% de la ventana."
    if b and b["limites"]:
        nivel, motivo = "peligroso", f"Hubo {b['limites']} evento(s) de limite en este bloque de 5 h. " + motivo
    if not b:
        motivo += " (sin bloque de 5 h activo: no hay consumo reciente para proyectar)."
    reset = datetime.fromtimestamp(b["reset"]).strftime("%H:%M") if b else ""
    (cp, cm), (xp, xm) = CORTES_PERFIL["cupo"], CORTES_PERFIL["contexto"]
    politicas = "".join(
        f'<tr><td><span class="pf-badge pf-{n}">{_ICONO_PERFIL[n]} {_NOMBRE_PERFIL[n]}</span></td><td>{c}</td><td>{x}</td></tr>'
        for n, c, x in (("peligroso", f"&ge; {cp}% (bloqueo antes del reset) o un rate_limit", f"&ge; {xp}%"),
                        ("moderado", f"{cm}% a {cp}%", f"{xm}% a {xp}%"),
                        ("saludable", f"&lt; {cm}%", f"&lt; {xm}%")))
    datos = dict(b or {}, ahora=ahora, ctx_pct=ctx_pct, umbral_inferencia=UMBRAL_INFERENCIA_PCT,
                 cortes={k: list(v) for k, v in CORTES_PERFIL.items()})
    cajas = "".join(f'<div class="metric"><strong id="{i}">{v}</strong><span>{n}</span></div>'
                    for i, v, n in (("pf-k-ctx", f"{ctx_pct:.0f}%", "Contexto de la sesion"),
                                    ("pf-k-proy", "—", "Cupo proyectado al reset"),
                                    ("pf-k-agota", "—", "Se agota a las"),
                                    ("pf-k-bloqueo", "—", "Tiempo bloqueado hasta el reset")))
    return (f'<div class="pf-head"><span class="pf-badge pf-{nivel}" id="pf-badge">{_ICONO_PERFIL[nivel]} {_NOMBRE_PERFIL[nivel]}</span>'
            f'<p class="muted" id="pf-motivo">{_esc(motivo)}</p></div>'
            f'<div class="pf-form"><label>% usado del cupo 5 h <small>(el de Account &amp; Usage)</small>'
            f'<input type="number" id="pf-pct" min="0" max="100" step="1" inputmode="decimal" placeholder="ej. 35"></label>'
            f'<label>Se resetea a las <small>(estimado por el primer uso del bloque; editable)</small>'
            f'<input type="time" id="pf-reset" value="{reset}"></label></div>'
            f'<div class="pf-meter" aria-hidden="true"><i id="pf-fill"></i></div>'
            f'<p class="pf-meter-txt" id="pf-meter-txt">Cargar el % para verlo como en Account &amp; Usage.</p>'
            f'<div class="metric-boxes us-kpis">{cajas}</div>'
            f'<table class="setup-tree pf-politicas"><thead><tr><th>Perfil</th><th>Cupo proyectado al reset</th>'
            f'<th>Contexto</th></tr></thead><tbody>{politicas}</tbody></table>'
            f'<script type="application/json" id="claudit-data-perfil">{json.dumps(datos)}</script>')


def _uso_html(u, up=None, nombre=None):
    """Contenedores de la pestaña Uso + los meses como JSON; el JS del template dibuja
    el mes elegido con el paginador ‹ › y cambia de ambito (todos los proyectos / este
    proyecto) con el selector, sin recalcular: los dos ambitos van embebidos."""
    if not u["meses"]:
        return f'<p class="muted">Sin sesiones locales en los ultimos {USO_MESES} meses.</p>'
    up = up or {"meses": []}
    datos = json.dumps({"todos": u, "proyecto": up, "nombre": nombre or ""},
                       ensure_ascii=False).replace("</", "<\\/")
    selector = ('<label class="uso-ambito">Ambito <select id="uso-ambito">'
                '<option value="todos">Todos los proyectos</option>'
                f'<option value="proyecto"{"" if up["meses"] else " disabled"}>'
                f'Este proyecto · {_esc(nombre)}</option></select></label>' if nombre else "")
    return (f'<div class="uso-pager">{selector}<button class="btn uso-prev" type="button" aria-label="Mes anterior">‹</button>'
            '<strong id="uso-mes" aria-live="polite"></strong>'
            '<button class="btn uso-next" type="button" aria-label="Mes siguiente">›</button></div>'
            '<div class="metric-boxes" id="uso-kpis"></div>'
            '<div class="uso-grid" id="uso-charts"></div>'
            '<div class="uso-grid"><div id="uso-skills"></div><div id="uso-conectores"></div></div>'
            '<div id="uso-modelos"></div>'
            f'<script type="application/json" id="claudit-data-uso">{datos}</script>')


def _tabla_uso_html(titulo, cols, filas):
    if not filas:
        return f'<div class="uso-chart"><h3>{_esc(titulo)}</h3><p class="muted">Sin usos en esta sesion.</p></div>'
    th = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    tr = "".join("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in f) + "</tr>" for f in filas)
    return (f'<div class="uso-chart chart-wrap"><h3>{_esc(titulo)}</h3><table class="setup-tree">'
            f"<thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>")


def _sesion_uso_html(u):
    """KPIs, barras y tablas en HTML; las curvas por turno las dibuja el JS con el JSON."""
    if not u:
        return '<p class="muted">Esta sesion todavia no tiene turnos con inferencias (sin turnos).</p>'
    k = u["kpis"]
    pct = lambda x: "—" if x is None else f"{x * 100:.1f}%"
    delta = ("sin mitades comparables (menos de 4 turnos)" if k["delta"] is None
             else f"{k['delta']:+.0%} en la 2.a mitad de la sesion vs la 1.a")
    kpis = [("Acciones por prompt", f"{k['acciones_por_prompt']:.1f}",
             f"{k['tools']} tool calls / {len(u['turnos'])} turnos · {delta}. Mas alto = mas trabajo autonomo por pedido."),
            ("Lineas aceptadas", f"{k['lineas']:,}", "Lineas agregadas por Edit/Write aplicados en esta sesion."),
            ("Accept rate", pct(k["accept_rate"]), f"{k['aceptadas']} ediciones aplicadas · {k['rechazadas']} rechazadas."),
            ("Turnos", f"{len(u['turnos'])}", f"{k['inferencias']} inferencias en total.")]
    cajas = "".join(f'<div class="metric" data-tip="{_esc(t)}"><strong>{_esc(v)}</strong><span>{_esc(n)}</span></div>'
                    for n, v, t in kpis)
    tope = max(c for _, c in u["profundidad"]) or 1
    barras = "".join(f'<div class="us-bar" data-tip="{_esc(f"{c} de {u["profundidad"][0][1]} turnos")}">'
                     f'<b>{c}</b><i style="height:{c / tope * 100:.0f}%"></i><span>{_esc(n)}</span></div>'
                     for n, c in u["profundidad"])
    adh = "".join(f'<div class="us-adh-fila"><span class="us-adh-lbl">{_esc(f)}</span>'
                  f'<span class="us-adh-pct">{p:.0%} de los turnos</span>'
                  f'<div class="us-adh-track" title="{c} turnos"><i style="width:{p * 100:.0f}%"></i></div></div>'
                  for f, c, p in u["adherencia"])
    datos = json.dumps({"turnos": u["turnos"]}, ensure_ascii=False).replace("</", "<\\/")
    return (f'<div class="metric-boxes us-kpis">{cajas}</div>'
            '<div class="uso-grid" id="us-charts"></div>'
            '<div class="uso-grid">'
            f'<div class="uso-chart"><h3>Profundidad de los turnos<span>tools por turno</span></h3><div class="us-barras">{barras}</div></div>'
            f'<div class="uso-chart"><h3>Adherencia por familia<span>% de turnos que la usaron</span></h3><div class="us-adh">{adh}</div></div>'
            '</div><div class="uso-grid">'
            + _tabla_uso_html("Skills", ["Skill", "Usos"], u["skills"])
            + _tabla_uso_html("Conectores (MCP)", ["Conector", "Lecturas", "Escrituras"],
                              [[c["servidor"], c["lecturas"], c["escrituras"]] for c in u["conectores"]])
            + f'</div><script type="application/json" id="claudit-data-sesion-uso">{datos}</script>')


def generar_html(jsonl, lineas, filas, destino, cpt_flag=None, ventana_flag=None):
    """Arma el reporte HTML self-contained y lo escribe en `destino` (Path).

    Crea el dir padre si hace falta. Si el dir padre se llama '.claudit', le
    escribe ademas un .gitignore con '*' para que el reporte nunca aparezca en
    el working tree del repo del usuario, sin tocar el .gitignore del usuario.
    """
    comp = composicion_prefijo(lineas, filas)
    setup = desglose_setup(lineas, filas, cpt_flag)
    acum = {k: sum(f[k] for f in filas) for k in ("read", "write", "input", "output")}
    total_usd = sum(costo(f) for f in filas)
    crec = filas[-1]["read"] / filas[0]["read"] if filas[0]["read"] else 0

    total_tok = acum["read"] + acum["write"] + acum["input"] + acum["output"]
    read_pct = round(100 * acum["read"] / total_tok) if total_tok else 0
    write_pct = round(100 * acum["write"] / total_tok) if total_tok else 0

    items_comp = _items_composicion(comp, acum["read"])
    cupo = consumo_local()
    items_write = _items_write(filas, (cupo["bloque"] or {}).get("inicio"))

    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.parent.name == ".claudit":
        (destino.parent / ".gitignore").write_text("*\n", encoding="utf-8")

    out = _HTML_TEMPLATE_PATH.read_text("utf-8")
    out = out.replace("__CLAUDIT_TTL__", _TTL_TXT[ttl_dominante(filas)])
    out = out.replace("__CLAUDIT_TITULO__", _esc(f"claudit — {jsonl.stem[:8]}"))
    out = out.replace(
        "__CLAUDIT_SUBTITULO__",
        _esc(f"Sesion {jsonl.stem[:8]} · Proyecto {REPO.name} · {len(filas)} inferencias · "
             + linea_modelo(modelo_y_effort(lineas, filas))),
    )
    out = out.replace("__CLAUDIT_BADGE__", _esc(usd(total_usd)))
    out = out.replace("__CLAUDIT_KPIS__", _kpi_cards_html(acum, total_usd, crec, len(filas)))
    out = out.replace("__CLAUDIT_TIPS__", _tips_html(setup))
    out = out.replace("__CLAUDIT_SETUP_TOTAL__", _tok0(setup["total"]))
    out = out.replace("__CLAUDIT_SETUP_ROWS__", _setup_tree_html(setup))
    ctx = vista_contexto(lineas, filas, setup, ventana_flag)
    out = out.replace("__CLAUDIT_CONTEXTO__", _contexto_html(ctx))
    out = out.replace("__CLAUDIT_CUPO__", _cupo_html(cupo))
    out = out.replace("__CLAUDIT_SKILLS__", _skills_del_setup_html(setup))
    out = out.replace("__CLAUDIT_PERFIL__", _perfil_html(cupo["bloque"], ctx["pct"], time.time()))
    proy = jsonl.parent.name
    nombre = REPO.name if jsonl.parent == dir_proyecto() else proy
    out = out.replace("__CLAUDIT_USO__", _uso_html(metricas_uso(), metricas_uso(proyecto=proy), nombre))
    out = out.replace("__CLAUDIT_HOOKS__", _salud_hooks_html(*hooks_con_previa(jsonl, lineas))
                      + _primer_turno_html(turnos(lineas)))
    out = out.replace("__CLAUDIT_LOOP__", _loop_html(loop_agentico(lineas)))
    out = out.replace("__CLAUDIT_SESION_USO__", _sesion_uso_html(uso_sesion(lineas)))
    out = out.replace("__CLAUDIT_CURVA__", _curva_contexto_html(analisis_ventana(filas))
                      + _retomadas_html(retomadas_cache_vencida(filas)))
    out = out.replace("__CLAUDIT_ARIA_COMP__", _esc(_aria_composicion(items_comp)))
    out = out.replace("__CLAUDIT_ARIA_WRITE__", _esc(_aria_write(items_write)))
    out = out.replace("__CLAUDIT_COMP_JSON__", json.dumps(items_comp, ensure_ascii=False))
    out = out.replace("__CLAUDIT_WRITE_JSON__", json.dumps(items_write, ensure_ascii=False))
    out = out.replace("__CLAUDIT_THEME_KEY__", HTML_THEME_KEY)
    out = out.replace("__CLAUDIT_FAVICON__", _FAVICON_B64)
    out = out.replace("__CLAUDIT_READ_PCT__", str(read_pct))
    out = out.replace("__CLAUDIT_WRITE_PCT__", str(write_pct))

    destino.write_text(out, encoding="utf-8")
    return destino


# Plantilla del reporte HTML: archivo aparte (report.html, junto al script) para
# poder editarla como HTML. Placeholders __CLAUDIT_*__ los llena generar_html.
_HTML_TEMPLATE_PATH = Path(__file__).resolve().parent / "report.html"


# ============================================================================
# Chequeo de nueva version: el slash command siempre pasa --html, asi que este
# chequeo corre en CADA invocacion. Debe ser silencioso ante cualquier fallo
# (offline, repo privado -> 404, timeout) y pegarle a la red como maximo 1 vez
# por dia (cache local) para no demorar el reporte.
# ============================================================================
_UPDATE_CACHE = HOME / ".claude" / "plugins" / ".claudit-update-check.json"
_UPDATE_URL = "https://raw.githubusercontent.com/SebasCouto/claudit/main/.claude-plugin/marketplace.json"
_UPDATE_TTL_SEG = 24 * 60 * 60

# Cache de versiones que Claude Code deja en disco al instalar/actualizar el plugin
# (una carpeta por version: .../cache/claudit/claudit/<version>/). Fuente de verdad de
# que version esta instalada: installed_plugins.json.
_CACHE_VERSIONS_DIR = HOME / ".claude" / "plugins" / "cache" / "claudit" / "claudit"
_INSTALLED_PLUGINS = HOME / ".claude" / "plugins" / "installed_plugins.json"


def _version_instalada():
    """Version del plugin instalado, leida de .claude-plugin/plugin.json junto al script.

    None si el archivo no existe o el JSON es invalido/incompleto.
    """
    try:
        p = Path(__file__).resolve().parent / ".claude-plugin" / "plugin.json"
        return json.loads(p.read_text("utf-8")).get("version")
    except Exception:
        return None


def _versiones_a_preservar():
    """Versiones de claudit que NO hay que borrar del cache: la que corre esta sesion
    viva (el script en ejecucion) y la instalada segun installed_plugins.json (todos
    los scopes). Borrar la que corre rompe la sesion viva, y en Windows tira
    PermissionError (no se puede borrar un archivo en uso)."""
    preservar = set()
    corriendo = _version_instalada()
    if corriendo:
        preservar.add(corriendo)
    try:
        data = json.loads(_INSTALLED_PLUGINS.read_text("utf-8"))
        for entry in data.get("plugins", {}).get("claudit@claudit", []):
            ver = entry.get("version")
            if ver:
                preservar.add(ver)
    except Exception:
        pass
    return preservar


def _limpiar_versiones_huerfanas():
    """Borra del cache las carpetas de versiones huerfanas de claudit, preservando la
    instalada y la que corre esta sesion. Cross-OS por construccion (shutil.rmtree +
    pathlib andan igual en Windows/macOS/Linux — no hace falta detectar el OS ni
    shellear un `rm`). Silencioso ante cualquier fallo: nunca rompe ni demora el
    reporte. Claude Code las limpia solo a los 7 dias; esto lo hace ya, en cada
    corrida, asi no se acumulan versiones viejas apenas reiniciaste a la nueva.

    Si no hay nada confirmado que preservar (no se pudo leer la version instalada ni
    la que corre) NO borra nada, para no barrer de mas por un estado ambiguo."""
    try:
        if not _CACHE_VERSIONS_DIR.is_dir():
            return  # no hay cache instalado (dev, otro layout, OS no reconocido) -> skip
        preservar = _versiones_a_preservar()
        if not preservar:
            return
        for hijo in _CACHE_VERSIONS_DIR.iterdir():
            if hijo.is_dir() and hijo.name not in preservar:
                shutil.rmtree(hijo, ignore_errors=True)
    except Exception:
        pass


def _version_remota():
    """Version publicada en marketplace.json de `main`, o None (opt-out/offline/error).

    Opt-out con CLAUDIT_NO_UPDATE_CHECK (cualquier valor). Cache diario en
    ~/.claude/plugins/.claudit-update-check.json: si tiene menos de 24h reusa la
    version cacheada; si no, hace fetch (timeout 3s) y reescribe el cache pase lo
    que pase (exito o fallo) para pegarle a la red como maximo 1 vez/dia. Cualquier
    error (offline, 404, timeout, JSON invalido) -> None en silencio.
    """
    if os.environ.get("CLAUDIT_NO_UPDATE_CHECK"):
        return None
    try:
        if _UPDATE_CACHE.is_file():
            cache = json.loads(_UPDATE_CACHE.read_text("utf-8"))
            if time.time() - cache.get("ts", 0) < _UPDATE_TTL_SEG:
                return cache.get("remote")
    except Exception:
        pass

    remota = None
    try:
        with urllib.request.urlopen(_UPDATE_URL, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        plugins = data.get("plugins") or []
        claudit_plugin = next(
            (p for p in plugins if isinstance(p, dict) and p.get("name") == "claudit"),
            plugins[0] if plugins and isinstance(plugins[0], dict) else {},
        )
        remota = claudit_plugin.get("version")
    except Exception:
        remota = None
    try:
        _UPDATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _UPDATE_CACHE.write_text(json.dumps({"ts": time.time(), "remote": remota}), encoding="utf-8")
    except Exception:
        pass
    return remota


def _hay_update():
    """(remota, instalada) si hay una version publicada MAS NUEVA que la instalada; si no, None.
    Silencioso ante cualquier dato faltante o version no comparable."""
    instalada = _version_instalada()
    remota = _version_remota()
    if not instalada or not remota:
        return None
    try:
        if tuple(int(x) for x in remota.split(".")) > tuple(int(x) for x in instalada.split(".")):
            return (remota, instalada)
    except Exception:
        return None
    return None


def _chequear_update():
    """Banner de alerta si hay version mas nueva (o None). Informativo; no bloquea."""
    upd = _hay_update()
    if not upd:
        return None
    remota, instalada = upd
    return (
        "+-----------------------------------------------------------------+\n"
        f"|  !! HAY UNA NUEVA VERSION DE CLAUDIT: {remota} (instalada: {instalada})\n"
        "|  Antes de confiar en estos numeros, actualizar y reiniciar:\n"
        "|    claude plugin marketplace update claudit\n"
        "|    claude plugin update claudit@claudit\n"
        "|  (para desactivar este chequeo: CLAUDIT_NO_UPDATE_CHECK=1)\n"
        "+-----------------------------------------------------------------+"
    )


def extraer_opciones(args):
    """Saca las opciones con valor numerico (--chars-por-token N, --ventana N) de args.
    Devuelve (opciones, resto); un valor invalido corta con error."""
    opciones = {"chars_por_token": None, "ventana": None}
    conv = {"--chars-por-token": ("chars_por_token", float), "--ventana": ("ventana", int)}
    resto, i = [], 0
    while i < len(args):
        if args[i] in conv:
            clave, tipo = conv[args[i]]
            try:
                valor = tipo(args[i + 1])
                if valor <= 0:
                    raise ValueError
            except (IndexError, ValueError):
                sys.exit(f"{args[i]} requiere un numero positivo")
            opciones[clave] = valor
            i += 2
            continue
        resto.append(args[i])
        i += 1
    return opciones, resto


def parsear_args(args):
    """Separa --detalle, --html [ruta] y el argumento posicional (archivo/uuid).

    --html es opcional-con-valor: el token siguiente se toma como ruta destino
    SOLO si termina en '.html' (si no, es ambiguo con el posicional del
    transcript — p.ej. `--html sesion.jsonl` no debe robarse el .jsonl como
    ruta). Sin ruta reconocible, --html usa el default (REPO/.claudit/report.html).
    --html repetido (el slash command siempre lo pasa, y el usuario puede pasarlo
    tambien) es tolerado: quiere_html es idempotente y un "--html" nunca matchea
    el sufijo '.html' (termina en 'tml' sin punto), asi que un --html duplicado
    jamas se come el token siguiente como ruta por error.
    """
    detalle = False
    quiere_html = False
    html_ruta = None
    forzar = False
    posicionales = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--detalle":
            detalle = True
        elif a == "--force":
            # Saltea el gate de version: genera el reporte con la version instalada
            # aunque haya una mas nueva. Lo usa el hook (panorama siempre) y la rama
            # "No" del slash command.
            forzar = True
        elif a == "--html":
            quiere_html = True
            if i + 1 < len(args) and args[i + 1].lower().endswith(".html"):
                html_ruta = args[i + 1]
                i += 1
        else:
            posicionales.append(a)
        i += 1
    return detalle, quiere_html, html_ruta, forzar, posicionales


def salida_utf8():
    """Fuerza UTF-8 en stdout/stderr: en Windows la consola y los pipes usan cp1252 y
    los caracteres del reporte (─ ✓ · ≈) lanzan UnicodeEncodeError."""
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")


def main():
    salida_utf8()
    _limpiar_versiones_huerfanas()
    opciones, args = extraer_opciones(sys.argv[1:])
    detalle, quiere_html, html_ruta_arg, forzar, posicionales = parsear_args(args)

    # Gate de version: si hay una version mas nueva publicada, el reporte queda
    # BLOQUEADO hasta actualizar. --force lo saltea (rama "No" del slash command y
    # el hook de arranque, que pasa --force para dar el panorama siempre). El slash
    # command detecta la marca UPDATE_REQUIRED y ofrece actualizar (Yes) o --force (No).
    upd = None if forzar else _hay_update()
    if upd:
        remota, instalada = upd
        print("UPDATE_REQUIRED")
        print(f"claudit {instalada} instalada · {remota} disponible.")
        print("El reporte esta BLOQUEADO hasta actualizar. Actualiza y reinicia Claude,")
        print("o genera con la version actual pasando --force.")
        return
    banner = _chequear_update() if forzar else None  # con --force el aviso es informativo, no bloquea
    if banner:
        print(banner)

    jsonl = resolver_medible(posicionales[0] if posicionales else None)
    lineas = parsear_lineas(jsonl)
    filas = leer_inferencias(lineas)
    if not filas:
        sys.exit("La sesion no tiene inferencias con usage todavia.")

    ancho = 22 + 13 + 13 + 11 + 11
    print("claudit — cache-read REAL de la sesion")
    print("=" * ancho)
    print(f"Sesion: {jsonl.stem[:8]} ({REPO.name})   Inferencias: {len(filas)}")
    print(linea_modelo(modelo_y_effort(lineas, filas)))
    print("-" * ancho)
    print(f"{''.ljust(22)}{'cache-read'.rjust(13)}{'cache-write'.rjust(13)}{'input'.rjust(11)}{'output'.rjust(11)}")
    print("-" * ancho)
    print(fila_str("Primera inferencia", filas[0]))
    print(fila_str("Ultima inferencia", filas[-1]))
    if detalle:
        print("-" * ancho)
        for i, f in enumerate(filas, 1):
            print(fila_str(f"  #{i}", f))
    print("-" * ancho)

    crec = filas[-1]["read"] / filas[0]["read"] if filas[0]["read"] else 0
    print(f"Crecimiento cache-read/inferencia: {tok(filas[0]['read'])} -> {tok(filas[-1]['read'])}"
          + (f"  ({crec:.1f}x)" if crec else "  (1a inferencia = establecimiento del cache)"))

    acum = {k: sum(f[k] for f in filas) for k in ("read", "write", "input", "output")}
    total_usd = sum(costo(f) for f in filas)
    por_tipo = costo_por_tipo(filas)
    print("\nAcumulado en la sesion:")
    print(f"  cache-read : {tok(acum['read']).rjust(13)} tok   ~ {usd(por_tipo['read'])}")
    setup = desglose_setup(lineas, filas, opciones["chars_por_token"])
    imprimir_composicion(composicion_prefijo(lineas, filas), setup, acum["read"])
    print(f"  cache-write: {tok(acum['write']).rjust(13)} tok   ~ {usd(por_tipo['write'])}  (TTL {_TTL_TXT[ttl_dominante(filas)]})")
    print(f"  input      : {tok(acum['input']).rjust(13)} tok   ~ {usd(por_tipo['input'])}")
    print(f"  output     : {tok(acum['output']).rjust(13)} tok   ~ {usd(por_tipo['output'])}")
    print(f"  TOTAL estimado                        ~ {usd(total_usd)}")
    print("=" * ancho)
    print("Lente: cada inferencia re-lee el prefijo ENTERO como cache-read.")
    print("Cuanto mas se escribe en el hilo (archivos inline, prompts largos, sin")
    print("skills/distill/sub-agentes), mas grande el prefijo -> mas cache-read en")
    print("CADA turno siguiente. El cache-read acumulado es el proxy del gasto.")
    imprimir_contexto(vista_contexto(lineas, filas, setup, opciones["ventana"]))
    imprimir_alerta_ventana(analisis_ventana(filas))
    imprimir_retomadas(retomadas_cache_vencida(filas))
    imprimir_salud_hooks(*hooks_con_previa(jsonl, lineas))
    print("\n" + linea_primer_turno(turnos(lineas)))
    print(linea_loop(loop_agentico(lineas)))
    print()
    for linea in lineas_cupo(consumo_local()):
        print(linea)

    if quiere_html:
        if html_ruta_arg:
            destino = Path(html_ruta_arg).resolve()
        else:
            # Nombre de la subcarpeta = sesion actual (en SessionStart, la que arranca);
            # si se paso un uuid explicito, la subcarpeta es la sesion medida.
            id_carpeta = jsonl.stem if posicionales else (sesion_actual() or jsonl).stem
            stamp = time.strftime("%Y-%m-%d_%H-%M-%S")   # cada report, su propio archivo con fecha+hora
            destino = carpeta_sesion(id_carpeta) / f"report-{stamp}.html"
        generar_html(jsonl, lineas, filas, destino, opciones["chars_por_token"], opciones["ventana"])
        print(f"Reporte HTML: {destino}")


if __name__ == "__main__":
    main()
