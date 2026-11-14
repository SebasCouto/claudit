"""Seccion Loop agentico: como se encadenan las inferencias dentro de cada turno
(paralelismo de tools, cadenas secuenciales, cache-read de hermanas, sub-agentes)."""
import unittest

import _fixtures as fx
import claudit


def resp(minuto, *tools, read=1000, mid=None):
    """Una inferencia; varias tools => un bloque tool_use por tool (misma respuesta)."""
    bloques = [fx.tool_use(n, f"t{minuto}-{k}") for k, n in enumerate(tools)] or None
    e = fx.asistente(fx.usage(read=read), fx.ts(minuto), bloques=bloques)
    if mid:
        e["message"]["id"] = mid
    return e


# Turno 1: 4 inferencias -> Read, Read, Read (cadena 3) y respuesta final.
# Turno 2: 2 inferencias -> una paralela (Grep+Glob) y una con Agent.
SESION = [
    fx.prompt("explorá", fx.ts(0)),
    resp(1, "Read", read=10_000),
    resp(2, "Read", read=11_000),
    resp(3, "Read", read=12_000),
    resp(4, read=13_000),
    fx.prompt("ahora delegá", fx.ts(10)),
    resp(11, "Grep", "Glob", read=20_000),
    resp(12, "Agent", read=21_000),
]


class TestLoopAgentico(unittest.TestCase):
    def setUp(self):
        self.m = claudit.loop_agentico(SESION)

    def test_inferencias_por_turno(self):
        self.assertEqual((self.m["turnos"], self.m["inferencias"]), (2, 6))
        self.assertAlmostEqual(self.m["prom"], 3.0)
        self.assertEqual((self.m["max"], self.m["max_turno"]), (4, 1))

    def test_paralelismo_sobre_inferencias_con_tools(self):
        self.assertEqual((self.m["con_tools"], self.m["paralelas"]), (5, 1))
        self.assertAlmostEqual(self.m["pct_paralelo"], 1 / 5)

    def test_cadena_secuencial_mas_larga(self):
        self.assertEqual((self.m["cadena_max"], self.m["cadena_turno"]), (3, 1))

    def test_cache_read_de_hermanas_excluye_la_primera_de_cada_turno(self):
        self.assertEqual(self.m["read_total"], 87_000)
        self.assertEqual(self.m["read_hermanas"], 11_000 + 12_000 + 13_000 + 21_000)

    def test_cuenta_sub_agentes(self):
        self.assertEqual(self.m["subagentes"], 1)

    def test_bloques_de_la_misma_respuesta_son_una_inferencia(self):
        partida = [fx.prompt("x", fx.ts(0)), resp(1, "Read", mid="m1"), resp(1, "Grep", mid="m1")]
        m = claudit.loop_agentico(partida)
        self.assertEqual((m["inferencias"], m["paralelas"]), (1, 1))

    def test_sin_prompts_no_hay_loop(self):
        self.assertIsNone(claudit.loop_agentico([resp(1, "Read")]))

    def test_turnos_sigue_igual(self):
        t = claudit.turnos(SESION)
        self.assertEqual([x["inferencias"] for x in t], [4, 2])
        self.assertEqual(t[1]["tools"], {"Grep": 1, "Glob": 1, "Agent": 1})


class TestPalancasLoop(unittest.TestCase):
    def test_sesion_secuencial_dispara_las_tres(self):
        cadena = [fx.prompt("x", fx.ts(0))] + [resp(i, "Read", read=50_000) for i in range(1, 13)]
        textos = " ".join(claudit.palancas_loop(claudit.loop_agentico(cadena)))
        self.assertIn("paralelo", textos)
        self.assertIn("turno 1", textos)
        self.assertIn("sub-agente", textos)

    def test_sesion_sana_sin_palancas(self):
        sana = [fx.prompt("x", fx.ts(0)), resp(1, "Read", "Grep"), resp(2)]
        self.assertEqual(claudit.palancas_loop(claudit.loop_agentico(sana)), [])


class TestLoopHtml(unittest.TestCase):
    def test_html_con_metricas_y_palancas(self):
        html = claudit._loop_html(claudit.loop_agentico(SESION))
        self.assertIn('class="metric-boxes"', html)
        self.assertIn("20%", html)
        self.assertIn("inferencias / turno", html)

    def test_sin_datos_avisa(self):
        self.assertIn("sin turnos", claudit._loop_html(None))

    def test_template_tiene_seccion_y_link(self):
        tpl = (fx.PLUGIN / "report.html").read_text("utf-8")
        sesion = tpl.split('data-panel="sesion"')[1].split('class="tab-panel"')[0]
        self.assertIn('id="s-loop"', sesion)
        self.assertIn("__CLAUDIT_LOOP__", sesion)
        self.assertIn('href="#s-loop"', tpl.split("</aside>")[0])

    def test_linea_terminal(self):
        self.assertIn("Loop agentico", claudit.linea_loop(claudit.loop_agentico(SESION)))


if __name__ == "__main__":
    unittest.main()
