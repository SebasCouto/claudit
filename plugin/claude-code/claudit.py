#!/usr/bin/env python3
# claudit — audita el cache-read REAL de la sesion de Claude Code (no estimado).
# made by @SebasCouto.
#
# Lee el transcript .jsonl de la sesion (lo escribe Claude Code en ~/.claude/projects/)
# y saca de cada inferencia los tokens reales que reporto la API:
#   cache_read  -> prefijo cacheado, re-leido este turno (0.1x input)
#   cache_write -> prefijo nuevo escrito al cache (1.25x input, 5 min)
#   input       -> input fresco no cacheado (el prompt del usuario + tool results del turno)
#   output      -> la respuesta del modelo (lo mas caro)
#
# Como plugin de Claude Code:   /claudit          /claudit --detalle          /claudit --html
# Como CLI standalone:          python3 claudit.py [--detalle] [--html [ruta]] [<archivo.jsonl | uuid>]
#
# --html [ruta]  ademas del reporte de texto, escribe un reporte HTML self-contained
#                (charts en canvas, dark/light) en `ruta`, o en REPO/.claudit/report.html
#                si no se especifica. Crea REPO/.claudit/.gitignore con "*" para que el
#                reporte nunca quede en el working tree del repo del usuario.
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

# USD por millon de tokens (Opus 4.8; edita si cambia el pricing).
# cache_read = 0.1x input; cache_write = 1.25x input (cache 5 min). Si la sesion usa
# cache de 1 hora el write cuesta 2x ($10) — el write suele ser una fraccion chica del
# total (se paga una vez por contenido nuevo; los reads dominan), asi que la aprox alcanza.
PRECIO = {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25}

# Aproximacion chars -> tokens para las piezas del setup que solo tengo como texto
# (CLAUDE.md, skill_listing, hooks). Afecta el balance editable-vs-base, no las
# proporciones internas. Umbrales de la columna de palanca (*), en tok/turno.
CHARS_POR_TOKEN = 4
PALANCA_ALTA = 1000
PALANCA_MEDIA = 300
SECC_MINIMA = 200  # secciones de CLAUDE.md por debajo de esto se colapsan en 'resto'

# El repo cuya sesion medimos: $CLAUDE_PROJECT_DIR (lo inyecta Claude Code al
# correr como plugin) o, si no esta, el directorio actual. NO la carpeta del
# script — asi el plugin, aunque viva centralizado, mide el repo activo.
def proyecto_activo():
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).resolve() if env else Path.cwd().resolve()


REPO = proyecto_activo()
HOME = Path.home()


def dir_proyecto():
    codificado = re.sub(r"[^A-Za-z0-9]", "-", str(REPO))
    return HOME / ".claude" / "projects" / codificado


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
        filas.append({
            "read": u.get("cache_read_input_tokens", 0) or 0,
            "write": write_tokens(u),
            "input": u.get("input_tokens", 0) or 0,
            "output": u.get("output_tokens", 0) or 0,
            "ts": entry.get("timestamp") if isinstance(entry, dict) else None,
        })
    return filas


def costo(f):
    return (f["input"] * PRECIO["input"] + f["output"] * PRECIO["output"]
            + f["read"] * PRECIO["cache_read"] + f["write"] * PRECIO["cache_write"]) / 1_000_000


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
                n = (len(att.get("stdout", "") or "") + len(att.get("stderr", "") or "")
                     + len(att.get("content", "") or ""))
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


def secciones_md(path):
    """Corta un CLAUDE.md por headers (# y ##) y devuelve [(titulo, tokens_estimados)]."""
    if not path.is_file():
        return []
    txt = path.read_text("utf-8", errors="replace")
    partes = re.split(r"(?m)^(#{1,2} .+)$", txt)
    tramos = []
    if partes[0].strip():
        tramos.append(("(preambulo)", len(partes[0])))
    for i in range(1, len(partes), 2):
        h = partes[i].strip()
        cuerpo = partes[i + 1] if i + 1 < len(partes) else ""
        tramos.append((h.lstrip("#").strip(), len(h) + len(cuerpo)))
    return [(t, ch / CHARS_POR_TOKEN) for t, ch in tramos]


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


def desglose_setup(lineas, filas):
    """Abre el setup fijo (prefijo real de la 1a inferencia) en sus componentes.

    Medibles como texto: CLAUDE.md global (por seccion), CLAUDE.md proyecto (por
    seccion), skill_listing y hooks del transcript. Lo no medible (system base +
    tool-defs) queda como residuo = setup_real - suma_medibles. Los CLAUDE.md se
    leen del disco AHORA, asi que reflejan el estado actual (pudo cambiar).
    """
    total = filas[0]["read"] + filas[0]["write"] + filas[0]["input"]
    skills_ch = hook_ch = otros_ch = 0
    for e in lineas:
        if usage_de(e):  # llegamos a la 1a inferencia: lo previo es setup
            break
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        if t == "attachment":
            att = e.get("attachment") or {}
            cont = len(att.get("content", "") or "")
            salida = len(att.get("stdout", "") or "") + len(att.get("stderr", "") or "")
            if att.get("type") == "skill_listing":
                skills_ch += cont
            elif att.get("hookName"):
                hook_ch += cont + salida
            else:
                otros_ch += cont + salida
        elif t == "user":
            for b in blocks_de(e):
                if isinstance(b, dict) and b.get("type") == "text":
                    otros_ch += len(b.get("text", "") or "")

    g_secc = secciones_md(HOME / ".claude" / "CLAUDE.md")
    p_secc = secciones_md(REPO / "CLAUDE.md")
    g_tok = sum(k for _, k in g_secc)
    p_tok = sum(k for _, k in p_secc)
    ch = lambda n: n / CHARS_POR_TOKEN
    medible = g_tok + p_tok + ch(skills_ch) + ch(hook_ch) + ch(otros_ch)

    comps = [{"label": "base harness + tool-defs (no editable)", "tok": max(0.0, total - medible),
              "editable": False, "secc": None}]
    editables = [
        {"label": "CLAUDE.md global (~/.claude)", "tok": g_tok, "editable": True, "secc": secciones_top(g_secc)},
        {"label": "skill_listing (skills instaladas)", "tok": ch(skills_ch), "editable": True, "secc": None},
        {"label": "SessionStart hook (engram)", "tok": ch(hook_ch), "editable": True, "secc": None},
        {"label": "CLAUDE.md proyecto", "tok": p_tok, "editable": True, "secc": secciones_top(p_secc)},
    ]
    if otros_ch:
        editables.append({"label": "otros system-reminders", "tok": ch(otros_ch), "editable": True, "secc": None})
    comps += sorted(editables, key=lambda c: -c["tok"])
    return {"total": total, "componentes": comps}


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
    "Contenido nuevo escrito al cache (una vez por turno). Cuesta 1.25x input; "
    "se paga una sola vez, no domina el gasto.",
    "Lo que genera el modelo: thinking + texto + tool calls. El token mas caro (5x input).",
    "USD estimado = read(0.1x) + write(1.25x) + input(1x) + output(5x), a precios de "
    "Opus 4.8. Edita PRECIO en el script si cambia.",
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


def _items_write(filas):
    return [{
        "turno": i + 1, "value": f["write"], "hora": _hora_local(f.get("ts")),
        "desc": f"Turno {i + 1}: tokens nuevos escritos al cache ese turno.",
    } for i, f in enumerate(filas)]


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
    return "\n".join(filas)


def generar_html(jsonl, lineas, filas, destino):
    """Arma el reporte HTML self-contained y lo escribe en `destino` (Path).

    Crea el dir padre si hace falta. Si el dir padre se llama '.claudit', le
    escribe ademas un .gitignore con '*' para que el reporte nunca aparezca en
    el working tree del repo del usuario, sin tocar el .gitignore del usuario.
    """
    comp = composicion_prefijo(lineas, filas)
    setup = desglose_setup(lineas, filas)
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

    out = _HTML_TEMPLATE
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


_HTML_TEMPLATE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__CLAUDIT_TITULO__</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,__CLAUDIT_FAVICON__">
<style>
:root{
  --bg:hsl(220 15% 8%); --surface-1:hsl(220 14% 11%); --surface-2:hsl(220 14% 13%);
  --ink:hsl(210 20% 92%); --muted:hsl(215 12% 62%); --line:hsl(220 10% 20%);
  --primary:hsl(190 95% 55%); --primary-ink:hsl(220 30% 8%);
  --good:hsl(142 65% 48%); --mid:hsl(38 92% 55%); --bad:hsl(0 72% 60%); --neutral:hsl(215 12% 50%);
  --radius:14px;
  --font-sans:'Inter',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  --font-mono:'JetBrains Mono','Consolas',monospace;
}
*{box-sizing:border-box;}
body{margin:0;color:var(--ink);background:var(--bg);font-family:var(--font-sans);line-height:1.45;}
main{max-width:1180px;margin:0 auto;padding:32px 24px;}
h1{margin:0 0 8px;font-size:clamp(26px,4vw,40px);letter-spacing:-.03em;font-weight:700;}
h2{margin:0 0 12px;font-size:20px;font-weight:600;}
h3{margin:0 0 10px;font-size:15px;font-weight:600;}
.muted{color:var(--muted);font-size:13px;}
header{border:1px solid var(--line);background:linear-gradient(135deg,var(--surface-2),var(--surface-1));
  border-radius:20px;padding:26px 30px;}
.header-top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:8px;}
.status{display:inline-flex;padding:6px 14px;border-radius:999px;background:var(--primary);color:var(--primary-ink);
  font:700 15px/1.1 var(--font-mono);letter-spacing:.02em;}
.actions{display:flex;gap:10px;margin-top:16px;}
.btn{cursor:pointer;border:1px solid var(--line);background:var(--surface-2);color:var(--ink);
  border-radius:999px;padding:8px 16px;font:600 13px var(--font-sans);}
.btn:hover{background:var(--surface-1);}
.brand{display:flex;align-items:center;gap:14px;}
.metric-boxes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:22px;}
@media (max-width:640px){.metric-boxes{grid-template-columns:repeat(2,minmax(0,1fr));}}
@media (max-width:400px){.metric-boxes{grid-template-columns:1fr;}}
.metric{border:1px solid var(--line);background:var(--surface-2);border-radius:16px;padding:18px 16px;min-width:0;
  display:flex;flex-direction:column;align-items:center;text-align:center;gap:5px;}
.metric strong{font-size:clamp(22px,3vw,34px);line-height:1.1;white-space:nowrap;max-width:100%;}
.metric>span{color:var(--muted);font-size:13px;}
.card{border:1px solid var(--line);background:var(--surface-1);border-radius:var(--radius);padding:24px 28px;margin-top:20px;}
.chart-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;}
.chart-pct{text-align:right;line-height:1.1;}
.chart-pct strong{display:block;font-size:34px;font-weight:700;color:var(--primary);}
.chart-pct span{font-size:12px;color:var(--muted);}
.chart-wrap{overflow-x:auto;}
canvas{width:100%;display:block;}
.tips-intro{margin:0 0 14px;color:var(--muted);font-size:14px;line-height:1.55;}
.tips-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px;}
.tips-list li{border-bottom:1px solid var(--line);padding-bottom:10px;font-size:14px;}
.tips-list li:last-child{border-bottom:none;padding-bottom:0;}
.tip-marca{display:inline-block;min-width:28px;color:var(--bad);font-weight:700;font-family:var(--font-mono);}
details{border:1px solid var(--line);background:var(--surface-1);border-radius:var(--radius);padding:18px 24px;margin-top:20px;}
summary{cursor:pointer;font-weight:600;}
details p{color:var(--muted);font-size:13px;line-height:1.6;margin:10px 0 0;}
footer{margin-top:28px;text-align:center;color:var(--muted);font-size:12px;}
.metric[data-tip]{cursor:help;}
#claudit-tip{position:fixed;z-index:50;max-width:300px;padding:10px 12px;background:var(--surface-2);
  border:1px solid var(--line);border-radius:10px;color:var(--ink);font:13px/1.4 var(--font-sans);
  box-shadow:0 8px 28px rgba(0,0,0,.45);pointer-events:none;opacity:0;transition:opacity .12s;}
#claudit-tip.on{opacity:1;}
#claudit-tip strong{display:block;font-size:13px;margin-bottom:4px;}
#claudit-tip span{color:var(--muted);}
.setup-tree{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px;min-width:520px;}
.setup-tree th{text-align:right;color:var(--muted);font-weight:600;font-size:12px;padding:7px 12px;border-bottom:1px solid var(--line);white-space:nowrap;}
.setup-tree th:first-child{text-align:left;}
.setup-tree td{padding:6px 12px;border-bottom:1px solid var(--line);text-align:right;font-family:var(--font-mono);white-space:nowrap;}
.setup-tree td:first-child{text-align:left;font-family:var(--font-sans);white-space:normal;}
.setup-tree .r-comp td{font-weight:600;}
.setup-tree .r-comp.r-base td:first-child{color:var(--muted);font-weight:600;}
.setup-tree .r-secc td{color:var(--muted);border-bottom:none;}
.setup-tree .r-secc td:first-child{padding-left:30px;font-weight:400;}
.setup-tree .r-total td{font-weight:700;border-top:2px solid var(--line);border-bottom:none;padding-top:10px;}
.setup-tree .r-sub td{color:var(--muted);border-bottom:none;}
.setup-tree .p-mark{color:var(--bad);font-weight:700;}
.setup-tree .p-none{color:var(--muted);}
</style>
</head>
<body>
<div id="claudit-tip" role="tooltip" aria-hidden="true"></div>
<main>
<header>
  <div class="header-top">
    <div class="brand">
      <svg width="44" height="44" viewBox="0 0 512 512" aria-hidden="true"><rect x="60" y="300" width="82" height="150" rx="20" fill="#1fd5f9" opacity=".42"/><rect x="176" y="222" width="82" height="228" rx="20" fill="#1fd5f9" opacity=".62"/><rect x="292" y="140" width="82" height="310" rx="20" fill="#1fd5f9" opacity=".82"/><rect x="408" y="60" width="82" height="390" rx="20" fill="#1fd5f9"/></svg>
      <h1>claudit</h1>
    </div>
    <span class="status">__CLAUDIT_BADGE__</span>
  </div>
  <p class="muted">__CLAUDIT_SUBTITULO__</p>
  <div class="metric-boxes">
__CLAUDIT_KPIS__
  </div>
</header>

<section class="card">
  <div class="chart-head">
    <h2>Cache-read — composicion del prefijo</h2>
    <div class="chart-pct"><strong>__CLAUDIT_READ_PCT__%</strong><span>del total de tokens · lo que se reenvia cada turno</span></div>
  </div>
  <p class="muted">Cada barra es un componente del prefijo que se re-lee ENTERO en cada turno. % = componente / prefijo; tok = ese % aplicado al cache-read acumulado REAL de la sesion.</p>
  <div class="chart-wrap">
    <canvas id="claudit-chart-comp" height="260" role="img" aria-label="__CLAUDIT_ARIA_COMP__"></canvas>
  </div>
  <script type="application/json" id="claudit-data-comp">__CLAUDIT_COMP_JSON__</script>
</section>

<section class="card">
  <div class="chart-head">
    <h2>Setup fijo — desglose completo</h2>
    <div class="chart-pct"><strong>__CLAUDIT_SETUP_TOTAL__</strong><span>tok/turno · piso fijo re-leido cada turno</span></div>
  </div>
  <p class="muted">Cada fila es un componente del piso fijo (la barra "setup fijo" de arriba, abierta). % = componente / setup fijo. Palanca = tok/turno editables que ahorras si lo recortas: <span class="p-mark">***</span> &ge;1000 · <span class="p-mark">**</span> &ge;300 · <span class="p-mark">*</span> &lt;300 · <span class="p-none">—</span> base del harness, no editable. Los CLAUDE.md se leen del disco AHORA (reflejan el estado actual).</p>
  <div class="chart-wrap">
    <table class="setup-tree">
      <thead><tr><th>Componente</th><th>tok/turno</th><th>% setup</th><th>palanca</th></tr></thead>
      <tbody>
__CLAUDIT_SETUP_ROWS__
      </tbody>
    </table>
  </div>
</section>

<section class="card">
  <div class="chart-head">
    <h2>Cache-write — por inferencia</h2>
    <div class="chart-pct"><strong>__CLAUDIT_WRITE_PCT__%</strong><span>del total de tokens</span></div>
  </div>
  <p class="muted">Una barra por turno. Altura = tokens nuevos escritos al cache ese turno (el contenido que entro y se cacheo). El pico grande esta al arranque (se cachea el setup); despues se escribe algo cada turno segun cuanto contenido nuevo entro. Aparecen picos EXTRA cuando el cache se re-crea: tras un /compact, tras un hueco de inactividad &gt;5 min (el cache expira, TTL) o al entrar contenido nuevo grande.</p>
  <div class="chart-wrap">
    <canvas id="claudit-chart-write" height="220" role="img" aria-label="__CLAUDIT_ARIA_WRITE__"></canvas>
  </div>
  <script type="application/json" id="claudit-data-write">__CLAUDIT_WRITE_JSON__</script>
</section>

<section class="card">
  <h2>Prioridad de mejora</h2>
  <p class="tips-intro">claudit muestra las mayores palancas por ahorro potencial por turno. Recortar, reordenar o no tocar nada es una decisión a criterio del usuario — esto es visibilidad, no una receta.</p>
  <ul class="tips-list">
__CLAUDIT_TIPS__
  </ul>
</section>

<details>
  <summary>Metodologia</summary>
  <p>El TOTAL de cache-read, cache-write, input y output de la sesion es el dato real que reporta la API de Anthropic (usage de cada inferencia del transcript) — no es una estimacion. El REPARTO interno del cache-read entre setup fijo, prompts, tool results y respuestas se estima por proporcion de caracteres del contenido del transcript, calibrado contra el prefijo real de la ultima inferencia. El total nunca se estima; el desglose interno si, y se declara como tal.</p>
</details>

<footer><span>claudit · Licencia MIT — software libre, sin garantia</span><br><span class="foot-by">@by SebasCouto</span></footer>
</main>

<script>
function claudinTok(name){return getComputedStyle(document.documentElement).getPropertyValue('--'+name).trim();}

function claudinEscHtml(s){
  var d=document.createElement('div'); d.textContent=(s==null?'':String(s)); return d.innerHTML;
}

function claudinShowTip(x, y, titulo, desc){
  var el=document.getElementById('claudit-tip'); if(!el || !desc) return;
  el.innerHTML='<strong>'+claudinEscHtml(titulo)+'</strong><span>'+claudinEscHtml(desc)+'</span>';
  el.classList.add('on'); el.setAttribute('aria-hidden','false');
  var w=el.offsetWidth, h=el.offsetHeight;
  var left=x+14, top=y-h-14;                 // por defecto ARRIBA del cursor: no tapa la barra ni desborda la card hacia abajo
  if(left+w>window.innerWidth) left=x-w-14;
  if(left<0) left=4;
  if(top<4) top=y+14;                        // si no entra arriba, abajo
  if(top+h>window.innerHeight) top=Math.max(4, window.innerHeight-h-4);
  el.style.left=left+'px'; el.style.top=top+'px';
}

function claudinHideTip(){
  var el=document.getElementById('claudit-tip'); if(!el) return;
  el.classList.remove('on'); el.setAttribute('aria-hidden','true');
}

function claudinWireMetrics(){
  document.querySelectorAll('.metric[data-tip]').forEach(function(elm){
    elm.addEventListener('mousemove', function(e){
      var lbl=elm.querySelector('span');
      claudinShowTip(e.clientX, e.clientY, lbl?lbl.textContent:'', elm.getAttribute('data-tip'));
    });
    elm.addEventListener('mouseout', function(){ claudinHideTip(); });
  });
}

function claudinDrawBarsH(canvasId, dataId){
  var el=document.getElementById(canvasId); if(!el) return;
  var items; try{items=JSON.parse(document.getElementById(dataId).textContent);}catch(e){return;}
  if(!items.length) return;
  var dpr=window.devicePixelRatio||1, W=el.clientWidth||600, rowH=34, H=items.length*rowH+16;
  el.style.height=H+'px'; el.width=W*dpr; el.height=H*dpr;
  var c=el.getContext('2d'); c.setTransform(dpr,0,0,dpr,0,0); c.clearRect(0,0,W,H);
  var max=Math.max.apply(null,items.map(function(i){return i.value;}))||1;
  var bh=Math.min(24,rowH-10);
  var rows=[];
  items.forEach(function(it,k){
    var y=8+k*rowH;
    c.fillStyle=claudinTok('ink'); c.font='13px '+claudinTok('font-sans'); c.textAlign='left'; c.textBaseline='middle';
    var lab=it.label;                                  // la barra arranca en x=120: truncar el label si no entra
    if(c.measureText(lab).width>108){ while(lab.length>1 && c.measureText(lab+'…').width>108){lab=lab.slice(0,-1);} lab+='…'; }
    c.fillText(lab,0,y+bh/2);
    var w=Math.max(2,(W-300)*(it.value/max));
    c.fillStyle=claudinTok(it.color||'primary'); c.fillRect(120,y,w,bh);
    var lbl=Math.round(it.pct*100)+'%  '+it.tokLabel+' tok';
    c.font='12px '+claudinTok('font-sans'); c.textBaseline='middle';
    if(124+w+c.measureText(lbl).width>W){            // no entra afuera -> adentro, a la derecha de la barra
      c.textAlign='right'; c.fillStyle=claudinTok('bg'); c.fillText(lbl,116+w,y+bh/2); c.textAlign='left';
    }else{
      c.fillStyle=claudinTok('muted'); c.fillText(lbl,124+w,y+bh/2);
    }
    rows.push({y0:k*rowH, y1:(k+1)*rowH, label:it.label.trim(), desc:it.desc||''});
  });
  el._rows=rows;
  if(!el._tipWired){
    el._tipWired=true;
    el.addEventListener('mousemove', function(e){
      var rs=el._rows||[], y=e.offsetY, hit=null;
      for(var i=0;i<rs.length;i++){ if(y>=rs[i].y0 && y<rs[i].y1){ hit=rs[i]; break; } }
      if(hit && hit.desc){ claudinShowTip(e.clientX, e.clientY, hit.label, hit.desc); } else { claudinHideTip(); }
    });
    el.addEventListener('mouseleave', function(){ claudinHideTip(); });
  }
}

function claudinDrawBarsV(canvasId, dataId){
  var el=document.getElementById(canvasId); if(!el) return;
  var items; try{items=JSON.parse(document.getElementById(dataId).textContent);}catch(e){return;}
  if(!items.length) return;
  var dpr=window.devicePixelRatio||1, W=el.clientWidth||600, H=220;
  el.style.height=H+'px'; el.width=W*dpr; el.height=H*dpr;
  var c=el.getContext('2d'); c.setTransform(dpr,0,0,dpr,0,0); c.clearRect(0,0,W,H);
  var max=Math.max.apply(null,items.map(function(i){return i.value;}))||1;
  var padBottom=26, padTop=10, plotH=H-padBottom-padTop, slot=W/items.length, bw=Math.max(2,slot-4);
  var cols=[];
  items.forEach(function(it,k){
    var h=plotH*(it.value/max), x=k*slot+2;
    c.fillStyle=claudinTok('primary'); c.fillRect(x,padTop+plotH-h,bw,Math.max(1,h));
    cols.push({x0:k*slot, x1:(k+1)*slot, turno:it.turno, hora:it.hora||'', desc:it.desc||''});
  });
  c.strokeStyle=claudinTok('line'); c.beginPath(); c.moveTo(0,padTop+plotH+0.5); c.lineTo(W,padTop+plotH+0.5); c.stroke();
  c.fillStyle=claudinTok('muted'); c.font='11px '+claudinTok('font-sans'); c.textBaseline='top';
  var step=Math.max(1,Math.round(items.length/10)), n=items.length;
  for(var k=0;k<n;k++){
    var isFirst=(k===0), isLast=(k===n-1);
    if(!(isFirst||isLast||(k%step===0 && (n-1-k)>=step*0.55))) continue;
    var lx=k*slot+2+bw/2;                              // primero a la izq, ultimo a la der (no se sale del canvas)
    if(isFirst){ c.textAlign='left'; lx=0; } else if(isLast){ c.textAlign='right'; lx=W; } else { c.textAlign='center'; }
    c.fillText(String(items[k].turno), lx, padTop+plotH+6);
  }
  el._cols=cols;
  if(!el._tipWired){
    el._tipWired=true;
    el.addEventListener('mousemove', function(e){
      var cs=el._cols||[], x=e.offsetX, hit=null;
      for(var i=0;i<cs.length;i++){ if(x>=cs[i].x0 && x<cs[i].x1){ hit=cs[i]; break; } }
      if(hit && hit.desc){ claudinShowTip(e.clientX, e.clientY, 'Turno '+hit.turno+(hit.hora?' · '+hit.hora:''), hit.desc); } else { claudinHideTip(); }
    });
    el.addEventListener('mouseleave', function(){ claudinHideTip(); });
  }
}

function claudinDrawAll(){
  claudinDrawBarsH('claudit-chart-comp','claudit-data-comp');
  claudinDrawBarsV('claudit-chart-write','claudit-data-write');
}
window.addEventListener('resize',claudinDrawAll);
claudinDrawAll();
claudinWireMetrics();
</script>
</body>
</html>
"""


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


def _chequear_update():
    """Si hay una version de claudit mas nueva publicada, devuelve el banner de alerta (o None).

    Opt-out con CLAUDIT_NO_UPDATE_CHECK (cualquier valor) -> None sin tocar la red.
    Cache diario en ~/.claude/plugins/.claudit-update-check.json: si tiene menos de
    24h, reusa la version remota cacheada (no vuelve a pegarle a la red). Si no,
    hace fetch a marketplace.json (timeout 3s) y reescribe el cache. Cualquier error
    (offline, repo privado -> 404, timeout, JSON invalido) -> None en silencio;
    nunca rompe ni demora el reporte.
    """
    if os.environ.get("CLAUDIT_NO_UPDATE_CHECK"):
        return None
    instalada = _version_instalada()
    if not instalada:
        return None

    remota = None
    try:
        if _UPDATE_CACHE.is_file():
            cache = json.loads(_UPDATE_CACHE.read_text("utf-8"))
            if time.time() - cache.get("ts", 0) < _UPDATE_TTL_SEG:
                remota = cache.get("remote")
    except Exception:
        remota = None

    if remota is None:
        # El cache no tenia una entrada reciente -> intentamos el fetch una vez.
        # Escribimos el cache pase lo que pase (exito o fallo, p.ej. 404 de repo
        # privado) para que el chequeo no le pegue a la red mas de 1 vez/dia ni
        # siquiera cuando esta fallando de forma persistente.
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
            _UPDATE_CACHE.write_text(
                json.dumps({"ts": time.time(), "remote": remota}), encoding="utf-8"
            )
        except Exception:
            pass

    if not remota:
        return None
    try:
        es_mas_nueva = (tuple(int(x) for x in remota.split("."))
                        > tuple(int(x) for x in instalada.split(".")))
    except Exception:
        return None
    if not es_mas_nueva:
        return None

    return (
        "+-----------------------------------------------------------------+\n"
        f"|  !! HAY UNA NUEVA VERSION DE CLAUDIT: {remota} (instalada: {instalada})\n"
        "|  Antes de confiar en estos numeros, actualizar y reiniciar:\n"
        "|    claude plugin marketplace update claudit\n"
        "|    claude plugin update claudit@claudit\n"
        "|  (para desactivar este chequeo: CLAUDIT_NO_UPDATE_CHECK=1)\n"
        "+-----------------------------------------------------------------+"
    )


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
    posicionales = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--detalle":
            detalle = True
        elif a == "--html":
            quiere_html = True
            if i + 1 < len(args) and args[i + 1].lower().endswith(".html"):
                html_ruta = args[i + 1]
                i += 1
        else:
            posicionales.append(a)
        i += 1
    return detalle, quiere_html, html_ruta, posicionales


def main():
    _limpiar_versiones_huerfanas()
    alerta_update = _chequear_update()
    if alerta_update:
        print(alerta_update)

    detalle, quiere_html, html_ruta_arg, posicionales = parsear_args(sys.argv[1:])
    jsonl = resolver_jsonl(posicionales[0] if posicionales else None)
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
    print("\nAcumulado en la sesion:")
    print(f"  cache-read : {tok(acum['read']).rjust(13)} tok   ~ {usd(acum['read'] * PRECIO['cache_read'] / 1e6)}")
    imprimir_composicion(composicion_prefijo(lineas, filas), desglose_setup(lineas, filas), acum["read"])
    print(f"  cache-write: {tok(acum['write']).rjust(13)} tok   ~ {usd(acum['write'] * PRECIO['cache_write'] / 1e6)}")
    print(f"  input      : {tok(acum['input']).rjust(13)} tok   ~ {usd(acum['input'] * PRECIO['input'] / 1e6)}")
    print(f"  output     : {tok(acum['output']).rjust(13)} tok   ~ {usd(acum['output'] * PRECIO['output'] / 1e6)}")
    print(f"  TOTAL estimado                        ~ {usd(total_usd)}")
    print("=" * ancho)
    print("Lente: cada inferencia re-lee el prefijo ENTERO como cache-read.")
    print("Cuanto mas se escribe en el hilo (archivos inline, prompts largos, sin")
    print("skills/distill/sub-agentes), mas grande el prefijo -> mas cache-read en")
    print("CADA turno siguiente. El cache-read acumulado es el proxy del gasto.")

    if quiere_html:
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")   # cada reporte, su propio archivo con fecha+hora
        destino = Path(html_ruta_arg).resolve() if html_ruta_arg else (REPO / ".claudit" / f"report-{stamp}.html")
        generar_html(jsonl, lineas, filas, destino)
        print(f"Reporte HTML: {destino}")


if __name__ == "__main__":
    main()
