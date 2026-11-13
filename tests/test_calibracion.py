"""Calibracion chars->tok del setup contra los tokens reales de la 1a inferencia."""
import unittest

import _fixtures as fx
import claudit


def setup(lineas, **kw):
    return claudit.desglose_setup(lineas, claudit.leer_inferencias(lineas), **kw)


class TestCalibracion(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def _sesion(self, sys_chars, tokens_reales):
        return [fx.forma(system_chars=sys_chars, tools=[]), fx.asistente(fx.usage(write=tokens_reales, inp=0), fx.ts(1))]

    def test_ratio_calibrado_y_suma_cierra(self):
        s = setup(self._sesion(9000, 3000))
        self.assertEqual(s["ratio_fuente"], "calibrado")
        self.assertAlmostEqual(s["ratio"], 3.0)
        medido = sum(c["tok"] for c in s["componentes"] if c["tipo"] != "resto")
        self.assertLessEqual(abs(medido - s["total"]) / s["total"], 0.10)

    def test_ratio_acotado_a_2_y_4(self):
        self.assertEqual(setup(self._sesion(50000, 1000))["ratio"], 4.0)
        self.assertEqual(setup(self._sesion(100, 1000))["ratio"], 2.0)

    def test_sin_forma_usa_default(self):
        s = setup([fx.asistente(fx.usage(write=3000), fx.ts(1))])
        self.assertEqual((s["ratio"], s["ratio_fuente"]), (3.0, "default"))

    def test_flag_manda(self):
        s = setup(self._sesion(9000, 3000), cpt_flag=3.5)
        self.assertEqual((s["ratio"], s["ratio_fuente"]), (3.5, "flag"))


class TestExtraerOpciones(unittest.TestCase):
    def test_chars_por_token(self):
        op, resto = claudit.extraer_opciones(["--html", "--chars-por-token", "3.2", "x.jsonl"])
        self.assertEqual(op["chars_por_token"], 3.2)
        self.assertEqual(resto, ["--html", "x.jsonl"])

    def test_valor_invalido_falla(self):
        with self.assertRaises(SystemExit):
            claudit.extraer_opciones(["--chars-por-token", "cero"])
        with self.assertRaises(SystemExit):
            claudit.extraer_opciones(["--chars-por-token", "0"])


if __name__ == "__main__":
    unittest.main()
