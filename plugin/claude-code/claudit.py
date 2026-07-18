#!/usr/bin/env python3
# claudit — audita el cache-read REAL de tu sesion de Claude Code (no estimado).
# made by @SebasCouto.
#
# Lee el transcript .jsonl de la sesion (lo escribe Claude Code en ~/.claude/projects/)
# y saca de cada inferencia los tokens reales que reporto la API:
#   cache_read  -> prefijo cacheado, re-leido este turno (0.1x input)
#   cache_write -> prefijo nuevo escrito al cache (1.25x input, 5 min)
#   input       -> input fresco no cacheado (tu prompt + tool results del turno)
#   output      -> mi respuesta (lo mas caro)
#
# Como plugin de Claude Code:   /claudit          /claudit --detalle
# Como CLI standalone:          python3 claudit.py [--detalle] [<archivo.jsonl | uuid>]
#
# Resuelve QUE proyecto medir por $CLAUDE_PROJECT_DIR (cuando corre como plugin)
# o, si no esta seteado, por el directorio actual (cwd). Asi mide el repo donde
# estas parado, no donde vive el script — funciona instalado en cualquier repo.
#
# El resumen desglosa, indentado bajo el cache-read acumulado, QUE compone ese
# prefijo re-leido: setup fijo (system+tool-defs+CLAUDE.md+skills+hooks), tus
# prompts, resultados de herramientas (por tipo: Read/Bash/...) y mis respuestas.
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
import json
import os
import re
import sys
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
# script — asi el plugin, aunque viva centralizado, mide el repo donde estas.
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
        })
    return filas


def costo(f):
    return (f["input"] * PRECIO["input"] + f["output"] * PRECIO["output"]
            + f["read"] * PRECIO["cache_read"] + f["write"] * PRECIO["cache_write"]) / 1_000_000


def usd(x):
    return f"${x:,.4f}"


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
    """Chars de un bloque de mi respuesta (text / thinking / tool_use)."""
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
    transcript, pero la API ya lo cacheo ahi. El resto (tus prompts, resultados de
    herramientas por tipo, mis respuestas) se estima por chars del contenido
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
    """Deja las secciones >= SECC_MINIMA (desc) y colapsa el resto en una fila."""
    grandes = sorted([(t, k) for t, k in secc if k >= SECC_MINIMA], key=lambda x: -x[1])
    chicas = [(t, k) for t, k in secc if k < SECC_MINIMA]
    if chicas:
        grandes.append((f"resto ({len(chicas)} secciones menores)", sum(k for _, k in chicas)))
    return grandes


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
    print("            (*) palanca = tok/turno editables que ahorras al recortar: ***>=1000 **>=300 *<300; sin marca = base no editable")


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
        linea("tus prompts", comp["prompts"], 8)
    tools_tot = sum(comp["tools"].values())
    if tools_tot:
        linea("tool results (lecturas/comandos inline)", tools_tot, 8)
        for name, tk in sorted(comp["tools"].items(), key=lambda kv: -kv[1]):
            linea(name, tk, 12)
    if comp["output"]:
        linea("mis respuestas (thinking+texto+tool calls)", comp["output"], 8)


def main():
    args = sys.argv[1:]
    detalle = "--detalle" in args
    resto = [a for a in args if a != "--detalle"]
    jsonl = resolver_jsonl(resto[0] if resto else None)
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
    print("Cuanto mas escribis en el hilo (archivos inline, prompts largos, sin")
    print("skills/distill/sub-agentes), mas grande el prefijo -> mas cache-read en")
    print("CADA turno siguiente. El cache-read acumulado es el proxy del gasto.")


if __name__ == "__main__":
    main()
