"""Comportamiento base de claudit que no debe romperse (contrato previo a 1.5)."""
import unittest

import _fixtures as fx  # noqa: F401  (agrega el plugin al sys.path)
import claudit


class TestCodificarRuta(unittest.TestCase):
    def test_ruta_mac(self):
        self.assertEqual(claudit.codificar_ruta("/Users/ana/Repos/demo"), "-Users-ana-Repos-demo")

    def test_ruta_windows(self):
        self.assertEqual(claudit.codificar_ruta(r"C:\Users\ana\Repos\demo"), "C--Users-ana-Repos-demo")


class TestParsearArgs(unittest.TestCase):
    def test_html_sin_ruta_no_se_come_el_jsonl(self):
        det, html, ruta, forzar, pos = claudit.parsear_args(["--html", "sesion.jsonl"])
        self.assertTrue(html)
        self.assertIsNone(ruta)
        self.assertEqual(pos, ["sesion.jsonl"])

    def test_html_con_ruta(self):
        _, _, ruta, _, _ = claudit.parsear_args(["--html", "out/r.html"])
        self.assertEqual(ruta, "out/r.html")

    def test_force_y_detalle(self):
        det, _, _, forzar, _ = claudit.parsear_args(["--detalle", "--force"])
        self.assertTrue(det)
        self.assertTrue(forzar)


class TestPalanca(unittest.TestCase):
    def test_umbrales(self):
        self.assertEqual(claudit.palanca(1500, True), "***")
        self.assertEqual(claudit.palanca(400, True), "**")
        self.assertEqual(claudit.palanca(10, True), "*")
        self.assertEqual(claudit.palanca(99999, False), "")


class TestLeerInferencias(unittest.TestCase):
    def test_solo_entradas_con_usage(self):
        lineas = [fx.prompt("hola", fx.ts(0)), fx.asistente(fx.usage(read=100, write=50), fx.ts(1))]
        filas = claudit.leer_inferencias(lineas)
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["read"], 100)
        self.assertEqual(filas[0]["write"], 50)


if __name__ == "__main__":
    unittest.main()
