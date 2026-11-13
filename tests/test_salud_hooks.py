"""Salud de hooks del arranque: tamano, degradado y caida vs la sesion previa."""
import os
import tempfile
import time
import unittest
from pathlib import Path

import _fixtures as fx
import claudit


def hook(content, stderr="", nombre="SessionStart:startup", command="Loading engram memory...", **extra):
    att = {"type": "hook_success", "hookName": nombre, "hookEvent": "SessionStart",
           "command": command, "content": content, "stderr": stderr, "exitCode": 0}
    att.update(extra)
    return att


def arranque(*hooks):
    return [fx.adjunto(h) for h in hooks] + [fx.asistente(fx.usage(write=1000), fx.ts(1)),
                                             fx.adjunto(hook("tarde"), fx.ts(2))]


class TestSaludHooks(unittest.TestCase):
    def test_solo_hooks_del_arranque_con_tamano(self):
        s = claudit.salud_hooks(arranque(hook("m" * 11_000)))
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["chars"], 11_000)
        self.assertFalse(s[0]["degradado"])

    def test_stderr_o_warning_marca_degradado(self):
        s = claudit.salud_hooks(arranque(
            hook("warning: Engram server ownership mismatch"),
            hook("ok " * 100, stderr="boom", command="otro")))
        self.assertTrue(all(h["degradado"] for h in s))
        self.assertIn("warning", s[0]["motivo"])
        self.assertIn("stderr", s[1]["motivo"])

    def test_error_en_medio_del_contenido_no_degrada(self):
        s = claudit.salud_hooks(arranque(hook("memoria sana\nbugfix: error de parseo resuelto")))
        self.assertFalse(s[0]["degradado"])

    def test_exit_code_no_cero_degrada(self):
        s = claudit.salud_hooks(arranque(hook("x", exitCode=1)))
        self.assertTrue(s[0]["degradado"])


class TestComparacion(unittest.TestCase):
    def test_caida_mayor_80_avisa(self):
        act = claudit.salud_hooks(arranque(hook("w" * 364)))
        prev = claudit.salud_hooks(arranque(hook("m" * 11_000)))
        avisos = claudit.comparar_hooks(act, prev)
        self.assertEqual(len(avisos), 1)
        self.assertEqual((avisos[0]["antes"], avisos[0]["ahora"]), (11_000, 364))

    def test_caida_chica_no_avisa(self):
        act = claudit.salud_hooks(arranque(hook("m" * 9_000)))
        prev = claudit.salud_hooks(arranque(hook("m" * 11_000)))
        self.assertEqual(claudit.comparar_hooks(act, prev), [])

    def test_texto_y_html_marcan_degradado(self):
        act = claudit.salud_hooks(arranque(hook("warning: mismatch")))
        html = claudit._salud_hooks_html(act, [])
        self.assertIn("degradado", html)


class TestSesionPrevia(unittest.TestCase):
    def test_toma_la_anterior_con_inferencias(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            vieja = fx.escribir(d, arranque(hook("m" * 100)), "a.jsonl")
            vacia = d / "b.jsonl"
            vacia.write_text("", "utf-8")
            actual = fx.escribir(d, arranque(hook("m" * 100)), "c.jsonl")
            ahora = time.time()
            for i, p in enumerate((vieja, vacia, actual)):
                os.utime(p, (ahora + i, ahora + i))
            self.assertEqual(claudit.sesion_previa(actual), vieja)
            self.assertIsNone(claudit.sesion_previa(vieja))


if __name__ == "__main__":
    unittest.main()
