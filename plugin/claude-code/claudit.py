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
import html
import json
import os
import re
import shutil
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
    """Lista ordenada de dicts con los 4 contadores por inferencia (assistant con usage)."""
    filas = []
    for entry in lineas:
        u = usage_de(entry)
        if not u:
            continue
        w5m, w1h = write_ttl(u)
        msg = entry.get("message") if isinstance(entry, dict) else None
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
    return [(t, ch / cpt) for t, ch in tramos]


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
            else:
                n, tipo, est = chars_adjunto(att)
                sumar(tipo, _ETIQUETA_ADJUNTO.get(at, at or "adjunto"), n, est)
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

    def comp(tipo, label, tk, secc=None, estimado=False):
        comps.append({"label": label, "tok": tk, "editable": CATEGORIAS[tipo][1], "secc": secc,
                      "tipo": tipo, "estimado": estimado})

    for path, (tipo, txt) in memoria.items():
        secc = secciones_texto(txt, ratio)
        comp("memoria", f"{tipo} {abreviar_ruta(path)}", sum(k for _, k in secc), secciones_top(secc))
    for tipo, path, txt in disco:
        secc = secciones_texto(txt, ratio)
        comp("memoria", f"{tipo} {abreviar_ruta(path)}", sum(k for _, k in secc),
             secciones_top(secc), estimado=True)
    for (tipo, label), (n, est) in acum.items():
        comp(tipo, label, ch(n), estimado=est)

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


def modelo_actual(filas):
    reales = [f.get("model") for f in filas if f.get("model") and f.get("model") != "<synthetic>"]
    return reales[-1] if reales else None


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


def _ts_epoch(ts):
    # ISO 8601 UTC ("2026-07-18T17:33:46.748Z") -> epoch en segundos. None si no parsea.
    if not (isinstance(ts, str) and len(ts) >= 19 and ts[10:11] == "T"):
        return None
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _items_write(filas):
    items, prev = [], None
    for i, f in enumerate(filas):
        ep = _ts_epoch(f.get("ts"))
        sibling = prev is not None and ep is not None and (ep - prev) < _UMBRAL_HERMANA_S
        items.append({
            "turno": i + 1, "value": f["write"], "hora": _hora_local(f.get("ts")),
            "sibling": sibling,
            "desc": f"Turno {i + 1}: tokens nuevos escritos al cache ese turno.",
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
    partes = [f'turno {it["turno"]}: {tok(it["value"])} tok' for it in items]
    return (f"Cache-write por inferencia a lo largo de {len(items)} turnos, "
            f"pico de {tok(pico['value'])} tok en el turno {pico['turno']}. Detalle: "
            + ", ".join(partes) + ".")


def _tips_html(setup):
    """Top componentes editables del setup fijo, ordenados por tok/turno desc."""
    editables = sorted([c for c in setup["componentes"] if c["editable"]], key=lambda c: -c["tok"])[:5]
    if not editables:
        return '<li class="muted">No hay componentes editables medibles en esta sesion.</li>'
    filas = []
    for c in editables:
        marca = palanca(c["tok"], True)
        marca_html = f'<span class="tip-marca">{_esc(marca)}</span>' if marca else '<span class="tip-marca muted">—</span>'
        filas.append(
            f'<li>{marca_html} <strong>{_esc(c["label"])}</strong> '
            f'<span class="muted">— {_tok0(c["tok"])} tok/turno</span></li>'
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
    items_write = _items_write(filas)

    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.parent.name == ".claudit":
        (destino.parent / ".gitignore").write_text("*\n", encoding="utf-8")

    out = _HTML_TEMPLATE_PATH.read_text("utf-8")
    out = out.replace("__CLAUDIT_TTL__", _TTL_TXT[ttl_dominante(filas)])
    out = out.replace("__CLAUDIT_TITULO__", _esc(f"claudit — {jsonl.stem[:8]}"))
    out = out.replace(
        "__CLAUDIT_SUBTITULO__",
        _esc(f"Sesion {jsonl.stem[:8]} · Proyecto {REPO.name} · {len(filas)} inferencias"),
    )
    out = out.replace("__CLAUDIT_BADGE__", _esc(usd(total_usd)))
    out = out.replace("__CLAUDIT_KPIS__", _kpi_cards_html(acum, total_usd, crec, len(filas)))
    out = out.replace("__CLAUDIT_TIPS__", _tips_html(setup))
    out = out.replace("__CLAUDIT_SETUP_TOTAL__", _tok0(setup["total"]))
    out = out.replace("__CLAUDIT_SETUP_ROWS__", _setup_tree_html(setup))
    out = out.replace("__CLAUDIT_CONTEXTO__", _contexto_html(vista_contexto(lineas, filas, setup, ventana_flag)))
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
