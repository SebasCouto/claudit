"""Uso de la sesion por turno: los graficos del dashboard de Claude (autonomia, actividad,
lineas, profundidad, adherencia, skills y conectores) calculados turno a turno."""
import json
import unittest

import _fixtures as fx
import claudit

RECHAZO = "The user doesn't want to proceed with this tool use."


def resp(minuto, *tools):
    """Una inferencia; cada tool es (nombre, id) o (nombre, id, input)."""
    bloques = [fx.tool_use(*t) for t in tools] or None
    return fx.asistente(fx.usage(read=1000), fx.ts(minuto), bloques=bloques)


PATCH = {"structuredPatch": [{"lines": ["+a", "+b", " c", "+d", "-e"]}]}

# Turno 1: Read+Grep en paralelo, Edit aceptado (3 lineas) y respuesta final.
# Turno 2 (slash command /flow): Skill dataviz, 2 MCP (lectura + escritura), Agent, Write rechazado.
# Turno 3: prompt sin respuesta (interrumpido) -> no cuenta como turno.
SESION = [
    fx.prompt("hola", fx.ts(0)),
    resp(1, ("Read", "r1"), ("Grep", "g1")),
    resp(2, ("Edit", "e1")),
    fx.tool_result("e1", "ok", fx.ts(2, 5), extra=PATCH),
    resp(3),
    fx.prompt("<command-name>/flow</command-name>", fx.ts(10)),
    resp(11, ("Skill", "s1", {"skill": "dataviz"})),
    resp(12, ("mcp__engram__mem_search", "m1")),
    resp(13, ("mcp__engram__mem_save", "m2")),
    resp(14, ("Agent", "a1")),
    resp(15, ("Write", "w1")),
    fx.tool_result("w1", RECHAZO, fx.ts(15, 5), error=True),
    fx.prompt("otra", fx.ts(20)),
]


class TestUsoSesion(unittest.TestCase):
    def setUp(self):
        self.u = claudit.uso_sesion(SESION)

    def test_serie_por_turno(self):
        t = self.u["turnos"]
        self.assertEqual([x["turno"] for x in t], [1, 2])
        self.assertEqual([(x["inferencias"], x["tools"]) for x in t], [(3, 3), (5, 5)])
        self.assertEqual([(x["nativas"], x["mcp"], x["subagentes"]) for x in t], [(3, 0, 0), (2, 2, 1)])
        self.assertEqual([x["lineas"] for x in t], [3, 0])

    def test_kpis(self):
        k = self.u["kpis"]
        self.assertAlmostEqual(k["acciones_por_prompt"], 4.0)
        self.assertEqual((k["lineas"], k["aceptadas"], k["rechazadas"]), (3, 1, 1))
        self.assertAlmostEqual(k["accept_rate"], 0.5)
        self.assertIsNone(k["delta"])  # con < 4 turnos no hay mitades comparables

    def test_delta_segunda_mitad_vs_primera(self):
        s = []
        for n, k in enumerate((1, 1, 3, 3)):
            s += [fx.prompt(f"p{n}", fx.ts(n * 10)),
                  resp(n * 10 + 1, *[("Read", f"r{n}-{i}") for i in range(k)])]
        self.assertAlmostEqual(claudit.uso_sesion(s)["kpis"]["delta"], 2.0)

    def test_profundidad_de_turnos(self):
        self.assertEqual(self.u["profundidad"],
                         [["Todos los turnos", 2], ["Con tools", 2], ["5+ tools", 1], ["20+ tools", 0]])

    def test_adherencia_por_familia(self):
        adh = {f: pct for f, _, pct in self.u["adherencia"]}
        self.assertEqual(adh, {"Edicion": 1.0, "Lectura": 0.5, "Bash": 0.0, "MCP": 0.5})

    def test_skills_y_conectores_reusan_los_clasificadores(self):
        self.assertEqual(self.u["skills"], [["dataviz", 1], ["flow", 1]])
        self.assertEqual(self.u["conectores"], [{"servidor": "engram", "lecturas": 1, "escrituras": 1}])

    def test_sin_turnos_es_none(self):
        self.assertIsNone(claudit.uso_sesion([resp(1, ("Read", "x"))]))


class TestUsoSesionHtml(unittest.TestCase):
    def setUp(self):
        self.html = claudit._sesion_uso_html(claudit.uso_sesion(SESION))

    def test_kpis_y_json_para_los_graficos(self):
        self.assertIn("Acciones por prompt", self.html)
        self.assertIn("Lineas aceptadas", self.html)
        self.assertIn("50.0%", self.html)
        datos = self.html.split('id="claudit-data-sesion-uso">')[1].split("</script>")[0]
        self.assertEqual(len(json.loads(datos)["turnos"]), 2)

    def test_barras_de_profundidad_y_adherencia(self):
        self.assertIn('class="us-barras"', self.html)
        self.assertIn('class="us-adh"', self.html)
        self.assertIn("50% de los turnos", self.html)

    def test_tablas_skills_y_conectores(self):
        self.assertIn("dataviz", self.html)
        self.assertIn("engram", self.html)

    def test_sin_datos_avisa(self):
        self.assertIn("sin turnos", claudit._sesion_uso_html(None))

    def test_template_en_uso_con_link_en_el_nav(self):
        tpl = (fx.PLUGIN / "report.html").read_text("utf-8")
        uso = tpl.split('data-panel="uso"')[1]
        self.assertIn('id="u-sesion"', uso)
        self.assertIn("__CLAUDIT_SESION_USO__", uso)
        nav = tpl.split("</aside>")[0]
        self.assertIn('<a class="sn-item" data-tab="uso" href="#u-sesion">', nav)


if __name__ == "__main__":
    unittest.main()
