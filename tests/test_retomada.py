"""Sesion retomada con cache vencida: hueco > TTL seguido de un re-write grande."""
import unittest

import _fixtures as fx
import claudit


def filas(*inferencias):
    """inferencias: (hora, minuto, read, write, w1h?)"""
    lineas = []
    for hora, minuto, read, write, w1h in inferencias:
        u = fx.usage(read=read, write=write, inp=0, out=0, w1h=w1h, w5m=0 if w1h is not None else None)
        lineas.append(fx.asistente(u, fx.ts(minuto, hora=hora)))
    return claudit.leer_inferencias(lineas)


class TestRetomadas(unittest.TestCase):
    def test_hueco_largo_con_rewrite_grande_avisa_con_costo(self):
        fs = filas((10, 0, 0, 150_000, 150_000), (10, 5, 150_000, 1_000, 1_000),
                   (12, 0, 0, 151_000, 151_000))
        r = claudit.retomadas_cache_vencida(fs)
        self.assertEqual(len(r), 1)
        ev = r[0]
        self.assertEqual(ev["indice"], 2)
        self.assertEqual(ev["rewrite"], 151_000)
        self.assertGreater(ev["hueco_s"], 3600)
        self.assertGreater(ev["costo"], 0)
        self.assertTrue(ev["hora"])

    def test_hueco_corto_no_avisa(self):
        fs = filas((10, 0, 0, 150_000, 150_000), (10, 30, 0, 150_000, 150_000))
        self.assertEqual(claudit.retomadas_cache_vencida(fs), [])

    def test_hueco_largo_con_write_chico_no_avisa(self):
        fs = filas((10, 0, 0, 150_000, 150_000), (12, 0, 148_000, 2_000, 2_000))
        self.assertEqual(claudit.retomadas_cache_vencida(fs), [])

    def test_ttl_5m_usa_umbral_de_5_minutos(self):
        fs = filas((10, 0, 0, 80_000, None), (10, 10, 0, 80_000, None))
        self.assertEqual(len(claudit.retomadas_cache_vencida(fs)), 1)


if __name__ == "__main__":
    unittest.main()
