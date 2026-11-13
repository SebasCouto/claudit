"""Salida en UTF-8 en todos los OS (Windows usa cp1252 por defecto en consolas/pipes)."""
import io
import sys
import unittest

import _fixtures as fx  # noqa: F401
import claudit


class TestSalidaUtf8(unittest.TestCase):
    def test_reconfigura_stdout_y_stderr_a_utf8(self):
        viejos = sys.stdout, sys.stderr
        try:
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
            sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
            claudit.salida_utf8()
            self.assertEqual(sys.stdout.encoding.lower(), "utf-8")
            self.assertEqual(sys.stderr.encoding.lower(), "utf-8")
            print("─ ✓ · ≈ é")  # no debe lanzar UnicodeEncodeError
        finally:
            sys.stdout, sys.stderr = viejos

    def test_stream_sin_reconfigure_no_rompe(self):
        viejo = sys.stdout
        try:
            sys.stdout = io.StringIO()
            claudit.salida_utf8()
        finally:
            sys.stdout = viejo


if __name__ == "__main__":
    unittest.main()
