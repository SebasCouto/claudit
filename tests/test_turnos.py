"""Turnos por prompt: inferencias y tool_use que genera cada prompt (1.er turno incluido)."""
import unittest

import _fixtures as fx
import claudit


def respuesta(minuto, *tools, mid=None):
    bloques = [fx.tool_use(n, f"t{minuto}{i}") for i, n in enumerate(tools)] or None
    e = fx.asistente(fx.usage(read=1000), fx.ts(minuto), bloques=bloques)
    if mid:
        e["message"]["id"] = mid
    return e


SESION = [
    fx.prompt("hola", fx.ts(0)),
    respuesta(1, "ToolSearch", "ToolSearch"),
    fx.tool_result("t10", "ok", fx.ts(2)),
    respuesta(3, "Bash"),
    fx.tool_result("t30", "ok", fx.ts(4)),
    respuesta(5),
    fx.prompt("segundo", fx.ts(6)),
    respuesta(7, "Read"),
]


class TestTurnos(unittest.TestCase):
    def test_tool_result_no_abre_turno(self):
        t = claudit.turnos(SESION)
        self.assertEqual(len(t), 2)
        self.assertEqual(t[0]["inferencias"], 3)
        self.assertEqual(t[0]["tools"], {"ToolSearch": 2, "Bash": 1})
        self.assertEqual(t[1]["tools"], {"Read": 1})

    def test_mismo_message_id_es_una_inferencia(self):
        lineas = [fx.prompt("hola", fx.ts(0)), respuesta(1, "Bash", mid="m1"), respuesta(1, "Read", mid="m1")]
        t = claudit.turnos(lineas)
        self.assertEqual(t[0]["inferencias"], 1)
        self.assertEqual(t[0]["tools"], {"Bash": 1, "Read": 1})

    def test_meta_no_es_prompt(self):
        meta = fx.prompt("<local-command-caveat>", fx.ts(2))
        meta["isMeta"] = True
        t = claudit.turnos([fx.prompt("hola", fx.ts(0)), respuesta(1), meta, respuesta(3)])
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["inferencias"], 2)

    def test_linea_primer_turno(self):
        txt = claudit.linea_primer_turno(claudit.turnos(SESION))
        self.assertIn("1.er turno: 3 inferencias", txt)
        self.assertIn("ToolSearch x2", txt)


if __name__ == "__main__":
    unittest.main()
