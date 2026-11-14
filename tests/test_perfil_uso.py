"""Perfil de uso (peligroso / moderado / saludable) y tiempo hasta el bloqueo del cupo 5 h.

El % de la cuenta lo carga el usuario en el reporte (Account & Usage); claudit aporta lo
que SI ve en local: inicio/reset estimado del bloque de 5 h, ritmo de consumo reciente,
% de contexto y cuanto cupo le cuesta cada cache-write (lo que se re-lee despues)."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import _fixtures as fx
import claudit
from test_cupo import AHORA, limite

H = 3600


class TestBloque5h(unittest.TestCase):
    def test_un_bloque_nuevo_arranca_tras_vencer_el_anterior(self):
        self.assertEqual(claudit.bloque_5h([0, 1 * H, 6 * H], ahora=7 * H), (6 * H, 11 * H))

    def test_bloque_vencido_o_sin_actividad_es_none(self):
        self.assertIsNone(claudit.bloque_5h([0], ahora=6 * H))
        self.assertIsNone(claudit.bloque_5h([], ahora=6 * H))


class TestConsumoDelBloque(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        raiz = Path(tmp.name)
        a = raiz / "-proj-a"
        a.mkdir()
        fx.escribir(a, [fx.asistente(fx.usage(read=5000, inp=0, out=0), fx.ts(0, hora=8)),
                        fx.asistente(fx.usage(read=1000, inp=0, out=0), fx.ts(0, hora=13)),
                        fx.asistente(fx.usage(read=2000, inp=0, out=0), fx.ts(40, hora=14)),
                        limite(fx.ts(45, hora=14))], "s1.jsonl")
        self.b = claudit.consumo_local(raiz=raiz, ahora=AHORA)["bloque"]

    def test_inicio_y_reset_estimados_por_el_primer_uso_del_bloque(self):
        inicio = datetime(2026, 11, 10, 13, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual((self.b["inicio"], self.b["reset"]), (inicio, inicio + 5 * H))
        self.assertEqual(self.b["fuente"], "estimated")

    def test_consumo_del_bloque_y_reciente(self):
        p = claudit.precios("claude-opus-5-5")["cache_read"] / 1e6
        self.assertAlmostEqual(self.b["usd"], 3000 * p)  # la de las 8:00 es de otro bloque
        self.assertAlmostEqual(self.b["usd_reciente"], 2000 * p)
        self.assertEqual(self.b["min_reciente"], claudit.MIN_RITMO)
        self.assertEqual(self.b["limites"], 1)


class TestCostoInducidoDelWrite(unittest.TestCase):
    def test_el_write_se_cobra_en_cada_relectura_hasta_el_compact(self):
        f = lambda r, w, m: {"read": r, "write": w, "input": 0, "output": 0, "ts": fx.ts(m), "model": "opus"}
        filas = [f(0, 1000, 0), f(1000, 0, 1), f(1000, 0, 2), f(10, 0, 3)]  # la 4.a: compact
        p = claudit.precios("opus")
        items = claudit._items_write(filas)
        esperado = (1000 * p["cache_write_5m"] + 2 * 1000 * p["cache_read"]) / 1e6
        self.assertAlmostEqual(items[0]["costo_inducido"], esperado)
        self.assertAlmostEqual(items[3]["costo_inducido"], 0)

    def test_auto_compact_real_cae_a_la_mitad_justa(self):
        f = lambda r, w, m: {"read": r, "write": w, "input": 0, "output": 0, "ts": fx.ts(m), "model": "opus"}
        filas = [f(0, 1000, 0), f(165000, 0, 1), f(0, 84000, 2), f(84000, 0, 3)]  # 165k -> 84k (51%)
        items = claudit._items_write(filas)
        p = claudit.precios("opus")
        self.assertAlmostEqual(items[0]["costo_inducido"], (1000 * p["cache_write_5m"] + 1000 * p["cache_read"]) / 1e6)
        self.assertIn("auto-compact", items[2]["desc"])

    def test_cache_vencida_re_escrita_corta_las_relecturas_previas(self):
        f = lambda r, w, m: {"read": r, "write": w, "input": 0, "output": 0, "ts": fx.ts(m), "model": "opus"}
        filas = [f(0, 1000, 0), f(1000, 0, 1), f(0, 1000, 2), f(1000, 0, 3)]  # la 3.a re-escribe todo
        items = claudit._items_write(filas)
        p = claudit.precios("opus")
        self.assertAlmostEqual(items[0]["costo_inducido"], (1000 * p["cache_write_5m"] + 1000 * p["cache_read"]) / 1e6)
        self.assertIn("cache vencida", items[2]["desc"])

    def test_solo_cuenta_lo_consumido_dentro_del_bloque_actual(self):
        f = lambda r, w, m: {"read": r, "write": w, "input": 0, "output": 0, "ts": fx.ts(m), "model": "opus"}
        filas = [f(0, 1000, 0), f(1000, 0, 1), f(1000, 0, 2)]
        desde = claudit._ts_epoch(fx.ts(2))  # el bloque arranca en la 3.a inferencia
        items = claudit._items_write(filas, desde=desde)
        self.assertAlmostEqual(items[0]["costo_inducido"], 1000 * claudit.precios("opus")["cache_read"] / 1e6)


class TestNivelPerfil(unittest.TestCase):
    def test_cortes_de_contexto_y_cupo(self):
        ctx, cupo = claudit.CORTES_PERFIL["contexto"], claudit.CORTES_PERFIL["cupo"]
        self.assertEqual((ctx, cupo), ((70, 40), (100, 60)))
        self.assertEqual([claudit.nivel_perfil(v, ctx) for v in (70, 40, 39.9)],
                         ["peligroso", "moderado", "saludable"])
        self.assertEqual(claudit.nivel_perfil(270, cupo), "peligroso")


DATOS = {"inicio": 0, "reset": 5 * H, "usd": 3.0, "usd_reciente": 1.0, "min_reciente": 30,
         "limites": 0, "fuente": "estimated"}


class TestPerfilHtml(unittest.TestCase):
    def setUp(self):
        self.html = claudit._perfil_html(DATOS, ctx_pct=32.0, ahora=H)

    def test_campos_para_el_pct_de_la_cuenta_y_el_reset(self):
        self.assertIn('id="pf-pct"', self.html)
        self.assertIn('id="pf-reset"', self.html)
        self.assertIn("Account &amp; Usage", self.html)

    def test_perfil_inicial_por_contexto_y_politicas_visibles(self):
        self.assertIn('class="pf-badge pf-saludable"', self.html)
        tabla = self.html.split("pf-politicas")[1]
        for corte in ("100%", "60%", "70%", "40%"):
            self.assertIn(corte, tabla)

    def test_json_para_proyectar_en_el_navegador(self):
        datos = json.loads(self.html.split('id="claudit-data-perfil">')[1].split("</script>")[0])
        self.assertEqual(datos["cortes"], {"cupo": [100, 60], "contexto": [70, 40]})
        self.assertEqual((datos["ahora"], datos["reset"], datos["ctx_pct"]), (H, 5 * H, 32.0))
        self.assertEqual(datos["umbral_inferencia"], claudit.UMBRAL_INFERENCIA_PCT)

    def test_rate_limit_observado_es_peligroso_sin_cargar_nada(self):
        html = claudit._perfil_html(dict(DATOS, limites=1), ctx_pct=10.0, ahora=H)
        self.assertIn('class="pf-badge pf-peligroso"', html)

    def test_sin_bloque_activo_avisa(self):
        self.assertIn("sin bloque de 5 h activo", claudit._perfil_html(None, ctx_pct=10.0, ahora=H))


class TestTemplate(unittest.TestCase):
    def setUp(self):
        self.tpl = (fx.PLUGIN / "report.html").read_text("utf-8")

    def test_perfil_es_la_primera_seccion_de_uso_con_nav(self):
        uso = self.tpl.split('data-panel="uso"')[1]
        self.assertLess(uso.index('id="u-perfil"'), uso.index('id="u-sesion"'))
        self.assertIn("__CLAUDIT_PERFIL__", uso)
        nav = self.tpl.split("</aside>")[0]
        self.assertLess(nav.index('href="#u-perfil"'), nav.index('href="#u-sesion"'))

    def test_write_tiene_leyenda_de_peligrosa(self):
        write = self.tpl.split('id="s-write"')[1].split("</section>")[0]
        self.assertIn("var(--bad)", write)
        self.assertIn('id="pf-write-nota"', write)


if __name__ == "__main__":
    unittest.main()
