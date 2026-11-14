"""Vista /context: categorias, ventana, buffer de auto-compact y espacio libre."""
import unittest
from unittest import mock

import _fixtures as fx
import claudit


def vista(lineas, **kw):
    filas = claudit.leer_inferencias(lineas)
    setup = claudit.desglose_setup(lineas, filas)
    return claudit.vista_contexto(lineas, filas, setup, **kw)


def sesion(read_final=80_000, model="claude-opus-5-5"):
    agentes = {"type": "agent_listing_delta", "builtInTypes": [], "addedLines": ["- mi-worker: " + "w" * 300]}
    instr = {"type": "instructions", "files": [{"path": "/h/.claude/CLAUDE.md", "type": "User", "content": "c" * 3000}]}
    return [fx.forma(system_chars=6000, tools=[]), fx.adjunto(agentes), fx.adjunto(instr),
            fx.asistente(fx.usage(write=3100, inp=0, out=0), fx.ts(1), model=model),
            fx.prompt("hola", fx.ts(2)),
            fx.asistente(fx.usage(read=read_final, write=0, inp=0, out=0), fx.ts(3), model=model)]


class TestVentana(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def test_por_modelo(self):
        self.assertEqual(claudit.ventana_modelo("claude-opus-5-5"), 1_000_000)
        self.assertEqual(claudit.ventana_modelo("claude-sonnet-5-5"), 1_000_000)
        self.assertEqual(claudit.ventana_modelo("claude-haiku-4-5"), 200_000)
        self.assertEqual(claudit.ventana_modelo("claude-sonnet-4-5[1m]"), 1_000_000)
        self.assertEqual(claudit.ventana_modelo(None), 200_000)

    def test_flag_manda(self):
        v = vista(sesion(), ventana_flag=500_000)
        self.assertEqual((v["ventana"], v["ventana_fuente"]), (500_000, "flag"))


class TestVistaContexto(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def test_categorias_y_cierre(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            v = vista(sesion())
        self.assertEqual(v["modelo"], "claude-opus-5-5")
        self.assertEqual(v["usado"], 80_000)
        cats = {c["clave"]: c["tok"] for c in v["categorias"]}
        for k in ("sistema", "agentes", "memoria", "mensajes", "buffer", "libre"):
            self.assertIn(k, cats)
        self.assertEqual(cats["buffer"], claudit.AUTOCOMPACT_BUFFER)
        self.assertAlmostEqual(sum(cats.values()), v["ventana"])
        usados = sum(t for k, t in cats.items() if k not in ("buffer", "libre"))
        self.assertAlmostEqual(usados, 80_000)

    def test_listas_memoria_y_agentes(self):
        v = vista(sesion())
        self.assertEqual([m["label"] for m in v["memoria"]], ["User /h/.claude/CLAUDE.md"])
        self.assertEqual([a["label"] for a in v["agentes"]], ["mi-worker"])

    def test_tras_compact_mensajes_no_negativos(self):
        lineas = sesion(read_final=500)
        v = vista(lineas)
        cats = {c["clave"]: c["tok"] for c in v["categorias"]}
        self.assertGreaterEqual(cats["mensajes"], 0)

    def test_ventana_casi_llena_cierra_igual(self):
        v = vista(sesion(read_final=190_000, model="claude-haiku-4-5"))
        cats = {c["clave"]: c["tok"] for c in v["categorias"]}
        self.assertEqual(cats["libre"], 0)
        self.assertAlmostEqual(sum(cats.values()), 200_000)


class TestContextoHtml(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def test_seccion_con_barra_tabla_y_listas(self):
        html = claudit._contexto_html(vista(sesion()))
        for txt in ("Free space", "Autocompact buffer", "Messages", "Memory files",
                    "Custom agents", "mi-worker", "claude-opus-5-5", "ctx-seg"):
            self.assertIn(txt, html)

    def test_plantilla_tiene_placeholder(self):
        self.assertIn("__CLAUDIT_CONTEXTO__", claudit._HTML_TEMPLATE_PATH.read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
