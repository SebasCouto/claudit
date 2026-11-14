"""Prioridad de mejora legible: titulos repetidos fusionados, skills completas paginadas
en orden alfabetico, y secciones agrupadas por concepto (Memoria, SDD, ...)."""
import unittest

import _fixtures as fx
import claudit


class TestTitulosRepetidos(unittest.TestCase):
    def test_mismo_header_se_fusiona_sumando(self):
        txt = "## Instructions\n" + "a" * 400 + "\n## Otra\n" + "b" * 400 + "\n## Instructions\n" + "c" * 400
        secc = dict(claudit.secciones_texto(txt, 1))
        self.assertEqual(list(secc), ["Instructions", "Otra"])
        self.assertEqual(secc["Instructions"], len("## Instructions\n" + "a" * 400 + "\n") + len("## Instructions\n" + "c" * 400))


class TestSkillsCompletas(unittest.TestCase):
    TXT = "- zeta: " + "z" * 90 + "\n- alfa: " + "a" * 10 + "\n- Medio: " + "m" * 50 + "\n"

    def test_todas_en_orden_alfabetico_sin_resto(self):
        self.assertEqual([n for n, _ in claudit.todas_skills(self.TXT, 1)], ["alfa", "Medio", "zeta"])

    def test_tip_pagina_las_skills(self):
        skills = [(f"s{i:02d}", 100.0) for i in range(30)]
        c = {"label": "skill_listing (skills instaladas)", "tok": 3000, "editable": True, "tipo": "skills",
             "secc": skills[:12], "crudas": skills}
        html = claudit._tips_html({"componentes": [c]})
        self.assertIn('class="tip-pag"', html)
        self.assertIn("s29", html)
        self.assertNotIn("resto (", html)
        self.assertEqual(html.count('data-pag="'), 30)
        self.assertIn('data-pag="3"', html)


class TestAgruparConceptos(unittest.TestCase):
    SECC = [("SDD Workflow (Spec-Driven Development)", 2177), ("Modelo por etapa (SDD) — contrato", 1529),
            ("Engram Persistent Memory — Protocol", 1446), ("Memoria de proyecto (cross-repo)", 319),
            ("Cómo trabajo", 3791), ("Tono", 50)]

    def setUp(self):
        self.g = claudit.agrupar_conceptos(self.SECC)
        self.por = {n["titulo"]: n for n in self.g}

    def test_sdd_y_memoria_agrupan_a_sus_hijos(self):
        self.assertEqual([t for t, _ in self.por["SDD"]["hijos"]],
                         ["SDD Workflow (Spec-Driven Development)", "Modelo por etapa (SDD) — contrato"])
        self.assertEqual(self.por["Memoria persistente"]["tok"], 1446 + 319)

    def test_sin_concepto_queda_suelto_y_chicas_en_resto(self):
        self.assertEqual(self.por["Cómo trabajo"]["hijos"], [])
        self.assertIn("resto (1 secciones menores)", self.por)

    def test_vocabulario_generico_sirve_para_otro_setup(self):
        g = claudit.agrupar_conceptos([("Memory bank rules", 600), ("Session memory", 500), ("Deploy", 900)])
        self.assertEqual({n["titulo"] for n in g}, {"Memoria persistente", "Deploy"})

    def test_ordenado_por_peso_y_total_conservado(self):
        toks = [n["tok"] for n in self.g]
        self.assertEqual(toks, sorted(toks, reverse=True))
        self.assertAlmostEqual(sum(toks), sum(k for _, k in self.SECC))

    def test_concepto_de_un_solo_item_no_se_envuelve(self):
        g = claudit.agrupar_conceptos([("Engram Persistent Memory", 900), ("Otra cosa", 800)])
        self.assertEqual([n["titulo"] for n in g], ["Engram Persistent Memory", "Otra cosa"])

    def test_tip_muestra_grupos_con_sus_hijos(self):
        c = {"label": "User ~/.claude/CLAUDE.md", "tok": 9312, "editable": True, "tipo": "memoria",
             "secc": self.SECC, "crudas": self.SECC}
        html = claudit._tips_html({"componentes": [c]})
        self.assertIn('class="tip-grupo"', html)
        self.assertIn("Memoria persistente", html)
        self.assertIn('class="tip-hijo"', html)


class TestComponentesTraenCrudas(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def test_skill_listing_trae_todas(self):
        muchas = "\n".join(f"- s{i:02d}: " + "d" * 900 for i in range(claudit.SKILLS_TOP + 5))
        lineas = [fx.adjunto({"type": "skill_listing", "content": muchas}),
                  fx.asistente(fx.usage(read=0, write=40000, inp=10), fx.ts(1))]
        s = claudit.desglose_setup(lineas, claudit.leer_inferencias(lineas))
        c = next(c for c in s["componentes"] if c["label"].startswith("skill_listing"))
        self.assertEqual(len(c["crudas"]), claudit.SKILLS_TOP + 5)


if __name__ == "__main__":
    unittest.main()
