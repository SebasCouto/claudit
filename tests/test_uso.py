"""Pestaña Uso: metricas por dia/mes de TODAS las sesiones locales (estilo dashboard Claude)."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import _fixtures as fx
import claudit

AHORA = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc).timestamp()


def resp(dia, minuto, *bloques, mid=None, model="claude-opus-5-5"):
    e = fx.asistente(fx.usage(read=1000, write=0, inp=0, out=0), fx.ts(minuto, dia=dia), model=model,
                     bloques=list(bloques) or None)
    if mid:
        e["message"]["id"] = mid
    return e


def editado(tid, dia, minuto, lineas):
    patch = [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 2, "lines": lineas}]
    return fx.tool_result(tid, "ok", fx.ts(minuto, dia=dia),
                          extra={"filePath": "/x/a.py", "structuredPatch": patch})


def creado(tid, dia, minuto, contenido):
    return fx.tool_result(tid, "ok", fx.ts(minuto, dia=dia),
                          extra={"type": "create", "filePath": "/x/b.py", "content": contenido,
                                 "structuredPatch": []})


def rechazado(tid, dia, minuto):
    return fx.tool_result(tid, "The user doesn't want to proceed with this tool use. The tool use was rejected",
                          fx.ts(minuto, dia=dia), error=True)


SESION_A = [
    fx.prompt("hola", fx.ts(0, dia=10)),
    resp(10, 1, fx.tool_use("Edit", "e1"), fx.tool_use("Skill", "s1", {"skill": "flow"})),
    editado("e1", 10, 2, [" ctx", "+nueva", "+otra", "-vieja"]),
    resp(10, 3, fx.tool_use("Write", "w1")),
    creado("w1", 10, 4, "a\nb\nc\n"),
    resp(10, 5, fx.tool_use("Edit", "e2")),
    rechazado("e2", 10, 6),
    fx.prompt("<command-name>/claudit:claudit</command-name>", fx.ts(7, dia=10)),
    resp(10, 8, fx.tool_use("mcp__engram__mem_search", "m1"), fx.tool_use("mcp__engram__mem_save", "m2")),
]
SESION_B = [
    fx.prompt("otro dia", fx.ts(0, dia=11)),
    resp(11, 1, fx.tool_use("Bash", "b1"), model="claude-sonnet-5-5"),
]
SESION_VIEJA = [fx.prompt("octubre", "2026-10-15T12:00:00.000Z"),
                fx.asistente(fx.usage(read=10), "2026-10-15T12:01:00.000Z")]


class TestMetricasUso(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        raiz = Path(cls.tmp.name)
        for nombre, lineas in (("-p-a", SESION_A), ("-p-b", SESION_B), ("-p-c", SESION_VIEJA)):
            (raiz / nombre).mkdir()
            fx.escribir(raiz / nombre, lineas, "s.jsonl")
        cls.u = claudit.metricas_uso(raiz=raiz, ahora=AHORA)
        cls.nov = next(m for m in cls.u["meses"] if m["mes"] == "2026-11")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_meses_ordenados_y_paginables(self):
        self.assertEqual([m["mes"] for m in self.u["meses"]], ["2026-10", "2026-11"])

    def test_lineas_aceptadas_de_edit_y_write(self):
        self.assertEqual(self.nov["totales"]["lineas"], 2 + 3)

    def test_accept_rate_cuenta_rechazos_de_edicion(self):
        t = self.nov["totales"]
        self.assertEqual((t["aceptadas"], t["rechazadas"]), (2, 1))
        self.assertAlmostEqual(t["accept_rate"], 2 / 3)

    def test_actividad_por_dia(self):
        dias = {d["fecha"]: d for d in self.nov["dias"]}
        self.assertEqual(len(self.nov["dias"]), 30)
        self.assertEqual((dias["2026-11-10"]["sesiones"], dias["2026-11-10"]["prompts"]), (1, 2))
        self.assertEqual(dias["2026-11-10"]["lineas"], 5)
        self.assertEqual(dias["2026-11-11"]["sesiones"], 1)
        self.assertEqual(dias["2026-11-01"]["sesiones"], 0)

    def test_acciones_por_prompt(self):
        t = self.nov["totales"]
        self.assertEqual((t["tools"], t["prompts"]), (7, 3))
        self.assertAlmostEqual(t["acciones_por_prompt"], 7 / 3)

    def test_skills_por_tool_y_por_comando(self):
        self.assertEqual(dict(self.nov["skills"]), {"flow": 1, "claudit:claudit": 1})

    def test_conectores_lecturas_y_escrituras(self):
        c = {x["servidor"]: x for x in self.nov["conectores"]}
        self.assertEqual((c["engram"]["lecturas"], c["engram"]["escrituras"]), (1, 1))

    def test_adherencia_y_modelos(self):
        t = self.nov["totales"]
        self.assertEqual(t["dias_activos"], 2)
        self.assertEqual(t["dias_transcurridos"], 12)
        self.assertAlmostEqual(t["adherencia"], 2 / 12)
        self.assertEqual(dict(self.nov["modelos"]), {"claude-opus-5-5": 4, "claude-sonnet-5-5": 1})

    def test_sin_meses_no_dibuja_paginador(self):
        self.assertNotIn("uso-prev", claudit._uso_html({"meses": [], "hoy": "2026-11-12"}))

    def test_html_embebe_json_y_paginador(self):
        html = claudit._uso_html(self.u)
        self.assertIn('id="claudit-data-uso"', html)
        self.assertIn("uso-prev", html)
        self.assertIn("2026-11", html)


class TestUsoPorProyecto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raiz = Path(cls.tmp.name)
        for nombre, lineas in (("-p-a", SESION_A), ("-p-b", SESION_B), ("-p-c", SESION_VIEJA)):
            (cls.raiz / nombre).mkdir()
            fx.escribir(cls.raiz / nombre, lineas, "s.jsonl")
        cls.u = claudit.metricas_uso(raiz=cls.raiz, ahora=AHORA, proyecto="-p-a")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_solo_cuenta_el_proyecto_pedido(self):
        self.assertEqual([m["mes"] for m in self.u["meses"]], ["2026-11"])
        nov = self.u["meses"][0]
        self.assertEqual(nov["totales"]["sesiones"], 1)
        self.assertEqual(dict(nov["modelos"]), {"claude-opus-5-5": 4})

    def test_proyecto_inexistente_sin_meses(self):
        self.assertEqual(claudit.metricas_uso(raiz=self.raiz, ahora=AHORA, proyecto="-nope")["meses"], [])

    def test_html_trae_ambos_ambitos_y_selector(self):
        todos = claudit.metricas_uso(raiz=self.raiz, ahora=AHORA)
        html = claudit._uso_html(todos, self.u, "-p-a")
        self.assertIn('id="uso-ambito"', html)
        datos = json.loads(html.split('id="claudit-data-uso">')[1].split("</script>")[0])
        self.assertEqual(set(datos), {"todos", "proyecto", "nombre"})
        self.assertEqual(datos["nombre"], "-p-a")


if __name__ == "__main__":
    unittest.main()
