"""Fuente de cada skill del skill_listing (proveedor, de donde sale, desde cuando se ve y con
que version de Claude Code), con filtros por proveedor y descarga en Excel."""
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import _fixtures as fx
import claudit


def skill(base, nombre, front=""):
    d = base / nombre
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {nombre}\ndescription: x\n{front}---\n\n# {nombre}\n", "utf-8")


class TestFuenteSkill(unittest.TestCase):
    def setUp(self):
        self.home = fx.aislar_home(self)
        self.glob = self.home / ".claude" / "skills"
        skill(self.glob, "flow")
        skill(self.glob, "sdd-apply", 'license: MIT\nmetadata:\n  author: gentleman-programming\n  version: "3.0"\n')
        skill(self.home / "repo" / ".claude" / "skills", "deploy")

    def test_anthropic_desde_claude_ai_y_desde_claude_code(self):
        self.assertEqual(claudit.fuente_skill("anthropic-skills:docx")[:2], ("anthropic", "claude.ai (anthropic-skills)"))
        self.assertEqual(claudit.fuente_skill("dataviz")[:2], ("anthropic", "Claude Code (incluida)"))

    def test_plugin_por_prefijo(self):
        self.assertEqual(claudit.fuente_skill("claudit:claudit")[:2], ("Plugin claudit", "plugin claudit"))

    def test_personal_global_sin_autor_con_etiqueta_por_defecto(self):
        self.assertEqual(claudit.fuente_skill("flow"), ("Personales globales", "~/.claude/skills", ""))

    def test_etiqueta_personal_configurable(self):
        (self.home / ".claude" / "claudit.json").write_text('{"etiqueta_personales": "Aegyros"}', "utf-8")
        self.assertEqual(claudit.fuente_skill("flow")[0], "Aegyros")

    def test_autor_y_version_del_frontmatter(self):
        self.assertEqual(claudit.fuente_skill("sdd-apply"), ("gentleman-programming", "~/.claude/skills", "3.0"))

    def test_skill_del_proyecto(self):
        self.assertEqual(claudit.fuente_skill("deploy")[:2], ("Proyecto", ".claude/skills del proyecto"))


def listado(names, cuando, version):
    e = fx.adjunto({"type": "skill_listing", "content": "", "names": names}, cuando)
    e["version"] = version
    return e


class TestHistorial(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as d:
            raiz = Path(d)
            (raiz / "-p").mkdir()
            fx.escribir(raiz / "-p", [listado(["flow", "dataviz"], fx.ts(0, dia=12), "2.1.320")], "b.jsonl")
            fx.escribir(raiz / "-p", [listado(["flow"], fx.ts(0, dia=3), "2.1.301"),
                                      fx.asistente(fx.usage(read=1), fx.ts(1, dia=3))], "a.jsonl")
            self.listados = claudit.escanear_listados(raiz)

    def test_primera_vez_vista_y_version_de_claude_code(self):
        h = claudit.historial_skills(self.listados)
        self.assertEqual(h["flow"], {"desde": "2026-11-03", "cc": "2.1.301"})
        self.assertEqual(h["dataviz"], {"desde": "2026-11-12", "cc": "2.1.320"})

    def test_versiones_de_claude_code_en_orden(self):
        self.assertEqual(claudit.versiones_cc(self.listados),
                         [("2.1.301", "2026-11-03"), ("2.1.320", "2026-11-12")])


def _filas():
    with mock.patch.object(claudit, "HOME", Path("/no/existe")):
        crudas = [(f"s{i:02d}", 100.0) for i in range(14)] + [("anthropic-skills:pdf", 50.0)]
        filas = claudit.filas_skills(crudas, {"s00": {"desde": "2026-09-30", "cc": "2.1.282"}})
    for f in filas[:14]:
        f["proveedor"] = "Personales globales"
    return filas


class TestSkillsHtml(unittest.TestCase):
    def setUp(self):
        self.html = claudit._skills_html(_filas(), [("2.1.282", "2026-09-30"), ("2.1.301", "2026-10-18")])

    def test_cada_skill_lleva_proveedor_fuente_y_fecha(self):
        self.assertIn('data-prov="anthropic"', self.html)
        self.assertIn('data-prov="Personales globales"', self.html)
        self.assertIn("claude.ai (anthropic-skills)", self.html)
        self.assertIn("2026-09-30", self.html)

    def test_filtros_por_proveedor_con_conteo(self):
        filtros = self.html.split('class="sk-filtros"')[1].split("</div>")[0]
        self.assertIn('aria-pressed="true">Todas (15)', filtros)
        self.assertIn(">Personales globales (14)", filtros)
        self.assertIn(">anthropic (1)", filtros)

    def test_boton_excel_y_datos_para_exportar(self):
        self.assertIn('id="sk-xlsx"', self.html)
        datos = json.loads(self.html.split('id="claudit-data-skills">')[1].split("</script>")[0])
        self.assertEqual(datos[0], {"skill": "anthropic-skills:pdf", "proveedor": "anthropic",
                                    "fuente": "claude.ai (anthropic-skills)", "tok": 50,
                                    "desde": "", "cc": "", "version": ""})
        self.assertEqual(len(datos), 15)

    def test_paginador_propio_no_lo_pisa_el_de_tips(self):
        self.assertIn('<div class="sk-pag"', self.html)
        self.assertNotIn("tip-pag", self.html)

    def test_versiones_de_claude_code(self):
        self.assertIn("2.1.301", self.html.split("sk-versiones")[1])

    def test_sin_skills_avisa(self):
        self.assertIn("sin skill_listing", claudit._skills_html([], []))


class TestTemplate(unittest.TestCase):
    def setUp(self):
        self.tpl = (fx.PLUGIN / "report.html").read_text("utf-8")

    def test_skills_es_la_ultima_seccion_de_uso_con_nav(self):
        uso = self.tpl.split('data-panel="uso"')[1].split('class="tab-panel"')[0]
        self.assertLess(uso.index('id="u-mes"'), uso.index('id="u-skills"'))
        self.assertIn("__CLAUDIT_SKILLS__", uso)
        nav = self.tpl.split("</aside>")[0]
        self.assertLess(nav.index('href="#u-mes"'), nav.index('data-tab="uso" href="#u-skills"'))

    def test_filtra_pagina_y_exporta(self):
        self.assertIn("function claudinXlsx", self.tpl)
        self.assertIn("function claudinSkills", self.tpl)


if __name__ == "__main__":
    unittest.main()
