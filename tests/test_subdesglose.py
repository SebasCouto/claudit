"""Un nivel mas de desglose en el setup: cada hook por secciones, skill_listing por skill,
y la Prioridad de mejora mostrando ese detalle."""
import unittest
from pathlib import Path

import _fixtures as fx
import claudit
from test_hooks import hook


def setup(*adjuntos):
    lineas = [fx.adjunto(a) for a in adjuntos] + [fx.asistente(fx.usage(read=0, write=40000, inp=10), fx.ts(1))]
    return claudit.desglose_setup(lineas, claudit.leer_inferencias(lineas))


def comp(s, prefijo):
    return next(c for c in s["componentes"] if c["label"].startswith(prefijo))


def listado(**skills):
    return {"type": "skill_listing", "content": "\n".join(f"- {n}: {'d' * k}" for n, k in skills.items())}


class TestHookPorSecciones(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def test_hook_con_headers_se_abre_en_secciones(self):
        txt = "## Protocolo\n" + "a" * 3000 + "\n## Memoria previa\n" + "b" * 6000
        c = comp(setup(hook("SessionStart:startup", txt)), "hook ")
        self.assertEqual([t for t, _ in c["secc"]], ["Memoria previa", "Protocolo"])

    def test_hook_sin_headers_no_tiene_secciones(self):
        c = comp(setup(hook("SessionStart:startup", "x" * 3000)), "hook ")
        self.assertFalse(c["secc"])


class TestSkillsPorSkill(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def test_una_fila_por_skill_ordenada_por_peso(self):
        c = comp(setup(listado(alpha=900, **{"claudit:claudit": 3000})), "skill_listing")
        self.assertEqual([t for t, _ in c["secc"]], ["claudit:claudit", "alpha"])
        self.assertAlmostEqual(sum(k for _, k in c["secc"]), c["tok"], delta=1)

    def test_las_menores_se_agrupan_en_resto(self):
        muchas = {f"s{i:02d}": 1000 + i for i in range(claudit.SKILLS_TOP + 5)}
        c = comp(setup(listado(**muchas)), "skill_listing")
        self.assertEqual(len(c["secc"]), claudit.SKILLS_TOP + 1)
        self.assertIn("resto (5 skills)", [t for t, _ in c["secc"]])


class TestTipsConDetalle(unittest.TestCase):
    def test_tip_lista_el_sub_desglose(self):
        s = {"componentes": [{"label": "skill_listing (skills instaladas)", "tok": 7000, "editable": True,
                              "secc": [("flow", 900.0), ("resto (3 skills)", 300.0)], "tipo": "skills"}]}
        html = claudit._tips_html(s)
        self.assertIn("tip-sub", html)
        self.assertIn("flow", html)
        self.assertIn("resto (3 skills)", html)


class TestLeyendaCacheWrite(unittest.TestCase):
    def test_leyenda_con_ambos_colores(self):
        tpl = (Path(claudit.__file__).parent / "report.html").read_text("utf-8")
        sec = tpl.split("Cache-write — por inferencia")[1].split("</section>")[0]
        self.assertIn('class="legend"', sec)
        self.assertIn("var(--primary)", sec)
        self.assertIn("var(--mid)", sec)


class TestWritePorInferencia(unittest.TestCase):
    """Cada barra del cache-write es una INFERENCIA; un turno = azul + sus hermanas."""

    FILAS = [{"write": 40000, "ts": fx.ts(0)}, {"write": 900, "ts": fx.ts(0, segundo=5)}]

    def test_items_numerados_por_inferencia(self):
        a, b = claudit._items_write(self.FILAS)
        self.assertEqual((a["inferencia"], b["inferencia"]), (1, 2))
        self.assertNotIn("turno", a)
        self.assertTrue(a["desc"].startswith("Inferencia 1"))
        self.assertIn("hermana", b["desc"])

    def test_aria_habla_de_inferencias(self):
        aria = claudit._aria_write(claudit._items_write(self.FILAS))
        self.assertIn("inferencia 1:", aria)
        self.assertNotIn("turno", aria)

    def test_template_y_tooltip_dicen_inferencia(self):
        tpl = (Path(claudit.__file__).parent / "report.html").read_text("utf-8")
        sec = tpl.split("Cache-write — por inferencia")[1].split("</section>")[0]
        self.assertIn("Una barra por inferencia", sec)
        self.assertNotIn("'Turno '", tpl)
        self.assertIn("'Inferencia '", tpl)



if __name__ == "__main__":
    unittest.main()
