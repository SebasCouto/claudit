"""Precio del cache-write segun TTL (5m = 1.25x, 1h = 2x) y precio por modelo."""
import unittest

import _fixtures as fx
import claudit


def fila(u, model="claude-opus-5-5"):
    return claudit.leer_inferencias([fx.asistente(u, fx.ts(0), model=model)])[0]


class TestPrecioModelo(unittest.TestCase):
    def test_tabla_por_familia(self):
        self.assertEqual(claudit.precios("claude-opus-5-5")["input"], 5.0)
        self.assertEqual(claudit.precios("claude-sonnet-5-5")["output"], 15.0)
        self.assertEqual(claudit.precios("claude-haiku-5-5")["input"], 1.0)
        self.assertEqual(claudit.precios("claude-fable-5-2")["output"], 50.0)

    def test_multiplicadores_de_cache(self):
        p = claudit.precios("claude-sonnet-5-5")
        self.assertAlmostEqual(p["cache_read"], 0.30)
        self.assertAlmostEqual(p["cache_write_5m"], 3.75)
        self.assertAlmostEqual(p["cache_write_1h"], 6.0)

    def test_modelo_desconocido_usa_opus(self):
        self.assertEqual(claudit.precios(None)["input"], 5.0)
        self.assertEqual(claudit.precios("<synthetic>")["input"], 5.0)


class TestCostoWrite(unittest.TestCase):
    def _solo_write(self, **kw):
        return fila(fx.usage(write=1_000_000, inp=0, out=0, **kw))

    def test_write_5m(self):
        self.assertAlmostEqual(claudit.costo(self._solo_write(w5m=1_000_000)), 6.25)

    def test_write_1h(self):
        self.assertAlmostEqual(claudit.costo(self._solo_write(w1h=1_000_000)), 10.0)

    def test_write_mixto(self):
        self.assertAlmostEqual(claudit.costo(self._solo_write(w1h=500_000, w5m=500_000)), 8.125)

    def test_sin_desglose_asume_5m(self):
        f = self._solo_write()
        self.assertEqual((f["w5m"], f["w1h"]), (1_000_000, 0))
        self.assertAlmostEqual(claudit.costo(f), 6.25)

    def test_costo_usa_el_modelo_de_la_fila(self):
        f = fila(fx.usage(read=0, write=0, inp=1_000_000, out=0), model="claude-haiku-5-5")
        self.assertAlmostEqual(claudit.costo(f), 1.0)


class TestTtlDominante(unittest.TestCase):
    def test_dominante(self):
        f1 = fila(fx.usage(write=100, w1h=100))
        f5 = fila(fx.usage(write=10, w5m=10))
        self.assertEqual(claudit.ttl_dominante([f1, f5]), "1h")
        self.assertEqual(claudit.ttl_dominante([f5]), "5m")


class TestCostoPorTipo(unittest.TestCase):
    def test_suma_igual_al_total(self):
        filas = [fila(fx.usage(read=1000, write=500, inp=10, out=100, w1h=500)),
                 fila(fx.usage(read=2000, write=50, inp=5, out=20), model="claude-sonnet-5-5")]
        por = claudit.costo_por_tipo(filas)
        self.assertAlmostEqual(sum(por.values()), sum(claudit.costo(f) for f in filas))


if __name__ == "__main__":
    unittest.main()
