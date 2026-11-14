"""Ventana de 1M sin auto-compact acotado: alerta, fix y curva de contexto."""
import json
import unittest
from unittest import mock

import _fixtures as fx
import claudit

VAR = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"


def filas_con(contextos, model="claude-opus-5-5"):
    lineas = [fx.asistente(fx.usage(read=c, write=0, inp=0, out=0), fx.ts(i), model=model)
              for i, c in enumerate(contextos)]
    return claudit.leer_inferencias(lineas)


class TestConfigAutocompact(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def test_env_manda(self):
        with mock.patch.dict("os.environ", {VAR: "200000"}, clear=True):
            self.assertEqual(claudit.autocompact_config(), (200_000, "env"))

    def test_settings_json(self):
        cfg = claudit.HOME / ".claude" / "settings.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"env": {VAR: "300000"}}), "utf-8")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(claudit.autocompact_config(), (300_000, "settings.json"))

    def test_sin_config(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(claudit.autocompact_config(), (None, None))


class TestAlertaVentana(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def test_contexto_alto_sin_variable_alerta_con_fix(self):
        filas = filas_con([50_000, 180_000, 229_000], model="claude-haiku-4-5")
        with mock.patch.dict("os.environ", {}, clear=True):
            a = claudit.analisis_ventana(filas)
        self.assertEqual(a["max"], 229_000)
        self.assertTrue(a["alerta"])
        self.assertIn(VAR, a["fix"])
        self.assertEqual(a["turnos_sobre"], 2)
        self.assertGreater(a["costo_sobre"], 0)

    def test_modelo_1m_alerta_aunque_contexto_bajo(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            a = claudit.analisis_ventana(filas_con([40_000, 60_000]))
        self.assertTrue(a["alerta"])
        self.assertEqual(a["umbral"], 1_000_000 - claudit.AUTOCOMPACT_BUFFER)

    def test_con_variable_sin_alerta_y_umbral_configurado(self):
        with mock.patch.dict("os.environ", {VAR: "200000"}, clear=True):
            a = claudit.analisis_ventana(filas_con([50_000, 229_000]))
        self.assertFalse(a["alerta"])
        self.assertEqual(a["umbral"], 200_000 - claudit.AUTOCOMPACT_BUFFER)

    def test_modelo_200k_contexto_bajo_sin_alerta(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            a = claudit.analisis_ventana(filas_con([40_000], model="claude-haiku-4-5"))
        self.assertFalse(a["alerta"])

    def test_curva_html_tiene_umbral(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            a = claudit.analisis_ventana(filas_con([50_000, 229_000]))
        html = claudit._curva_contexto_html(a)
        self.assertIn("<svg", html)
        self.assertIn("ctx-umbral", html)
        self.assertIn("polyline", html)


class TestVistaUsaConfig(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def test_ventana_de_la_variable(self):
        filas = filas_con([80_000])
        setup = {"componentes": [], "total": 0}
        with mock.patch.dict("os.environ", {VAR: "200000"}, clear=True):
            v = claudit.vista_contexto([], filas, setup)
        self.assertEqual((v["ventana"], v["ventana_fuente"]), (200_000, VAR))


if __name__ == "__main__":
    unittest.main()
