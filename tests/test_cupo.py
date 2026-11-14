"""Consumo local en la ventana de 5 h sumando TODOS los proyectos + eventos de limite."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import _fixtures as fx
import claudit

AHORA = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc).timestamp()


def limite(cuando):
    e = fx.asistente(fx.usage(read=0, write=0, inp=0, out=0), cuando, model="<synthetic>",
                     bloques=[{"type": "text", "text": "You've hit your session limit · resets 12:40pm"}])
    e.update({"error": "rate_limit", "isApiErrorMessage": True})
    return e


class TestConsumoLocal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raiz = Path(self.tmp.name)
        a, b = raiz / "-proj-a", raiz / "-proj-b"
        a.mkdir()
        b.mkdir()
        fx.escribir(a, [fx.asistente(fx.usage(read=1000, write=0, inp=0, out=0), fx.ts(0, hora=13)),
                        fx.asistente(fx.usage(read=5000, write=0, inp=0, out=0), fx.ts(0, hora=8))], "s1.jsonl")
        fx.escribir(b, [fx.asistente(fx.usage(read=2000, write=0, inp=0, out=0), fx.ts(30, hora=14)),
                        limite(fx.ts(40, hora=14))], "s2.jsonl")
        self.c = claudit.consumo_local(raiz=raiz, ahora=AHORA)

    def test_suma_todos_los_proyectos_dentro_de_la_ventana(self):
        self.assertEqual(self.c["tokens"]["valor"], 3000)
        self.assertEqual(self.c["sesiones"]["valor"], 2)
        self.assertEqual(self.c["proyectos"]["valor"], 2)
        self.assertEqual(self.c["tokens"]["fuente"], "calculated")
        self.assertEqual(self.c["costo"]["fuente"], "estimated")

    def test_eventos_de_limite(self):
        ev = self.c["limites"]["valor"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(self.c["limites"]["fuente"], "observed")

    def test_sin_dato_de_cuenta_unknown_sin_advertencia(self):
        self.assertEqual(self.c["cupo_cuenta"], {"valor": None, "fuente": "unknown"})
        txt = claudit.lineas_cupo(self.c)
        self.assertTrue(any("unknown" in l for l in txt))
        self.assertFalse(any("ADVERTENCIA" in l for l in txt))

    def test_html(self):
        html = claudit._cupo_html(self.c)
        for t in ("calculated", "estimated", "observed", "unknown"):
            self.assertIn(t, html)


if __name__ == "__main__":
    unittest.main()
