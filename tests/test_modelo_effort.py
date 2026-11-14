"""Encabezado: modelo (ultimo y si cambio) y effort (transcript > settings.json > unknown)."""
import json
import unittest

import _fixtures as fx
import claudit


def info(lineas):
    return claudit.modelo_y_effort(lineas, claudit.leer_inferencias(lineas))


def inf(model, minuto=1):
    return fx.asistente(fx.usage(read=100), fx.ts(minuto), model=model)


class TestModeloEffort(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def test_effort_del_transcript(self):
        m = info([fx.api_request(effort="max"), inf("claude-opus-5-5")])
        self.assertEqual((m["effort"], m["effort_fuente"]), ("max", "transcript"))
        self.assertTrue(m["effort_alto"])

    def test_effort_de_settings(self):
        cfg = claudit.HOME / ".claude" / "settings.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"effortLevel": "xhigh"}), "utf-8")
        m = info([inf("claude-opus-5-5")])
        self.assertEqual((m["effort"], m["effort_fuente"]), ("xhigh", "settings.json"))

    def test_sin_effort_unknown(self):
        m = info([inf("claude-opus-5-5")])
        self.assertEqual(m["effort"], "unknown")
        self.assertFalse(m["effort_alto"])

    def test_cambio_de_modelo(self):
        m = info([inf("claude-sonnet-5-5", 1), inf("<synthetic>", 2), inf("claude-opus-5-5", 3)])
        self.assertEqual(m["modelo"], "claude-opus-5-5")
        self.assertEqual(m["modelos"], ["claude-sonnet-5-5", "claude-opus-5-5"])
        self.assertIn("claude-opus-5-5", claudit.linea_modelo(m))
        self.assertIn("cambio", claudit.linea_modelo(m))

    def test_sin_cambio(self):
        m = info([inf("claude-opus-5-5", 1), inf("claude-opus-5-5", 2)])
        self.assertNotIn("cambio", claudit.linea_modelo(m))


if __name__ == "__main__":
    unittest.main()
