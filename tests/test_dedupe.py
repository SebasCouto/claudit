"""Claude Code escribe una linea por bloque de la MISMA respuesta, repitiendo usage:
cada message.id es UNA inferencia (sin esto los totales se inflaban ~2.4x)."""
import unittest

import _fixtures as fx
import claudit


def linea(mid, read, minuto, bloque):
    e = fx.asistente(fx.usage(read=read, write=10, inp=1, out=5), fx.ts(minuto), bloques=[bloque])
    e["message"]["id"] = mid
    return e


class TestDedupe(unittest.TestCase):
    def test_mismo_message_id_cuenta_una_vez(self):
        lineas = [linea("m1", 1000, 1, {"type": "thinking", "thinking": "x"}),
                  linea("m1", 1000, 1, {"type": "text", "text": "y"}),
                  linea("m1", 1000, 1, fx.tool_use("Bash", "t1")),
                  fx.tool_result("t1", "ok", fx.ts(2)),
                  linea("m2", 2000, 3, {"type": "text", "text": "z"})]
        filas = claudit.leer_inferencias(lineas)
        self.assertEqual([f["read"] for f in filas], [1000, 2000])

    def test_sin_id_cada_linea_es_una_inferencia(self):
        lineas = [fx.asistente(fx.usage(read=1), fx.ts(1)), fx.asistente(fx.usage(read=1), fx.ts(2))]
        self.assertEqual(len(claudit.leer_inferencias(lineas)), 2)


if __name__ == "__main__":
    unittest.main()
