"""Versiones vistas: Claude Code, plugins instalados y autores de ~/.claude/skills, cada uno con su
version propia, detectados en la maquina donde corre claudit; mas un calendario mes a mes."""
import json
import os
import unittest
from datetime import datetime
from unittest import mock

import _fixtures as fx
import claudit
from test_fuente_skills import skill


def _epoch(dia):
    return datetime.fromisoformat(dia + "T12:00:00").timestamp()


class TestComponentes(unittest.TestCase):
    def setUp(self):
        home = fx.aislar_home(self)
        plugins = home / ".claude" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
            "claudit@claudit": [{"version": "1.4.0", "installedAt": "2026-07-18T18:01:10.862Z",
                                 "lastUpdated": "2026-07-20T14:01:27.228Z"}],
            "engram@engram": [{"version": "0.1.0", "installedAt": "2026-05-07T14:11:39.702Z",
                               "lastUpdated": "2026-05-07T14:11:39.702Z"}],
            "otro@tercero": [{"version": "2.0.0", "installedAt": "2026-08-01T10:00:00.000Z"}]}}), "utf-8")
        (home / ".claude" / "claudit.json").write_text('{"etiqueta_personales": "aegyros"}', "utf-8")
        base = home / ".claude" / "skills"
        gentle = 'metadata:\n  author: gentleman-programming\n  version: "{}"\n'
        for nombre, front, dia in (("sdd-apply", gentle.format("3.0"), "2026-05-06"),
                                   ("branch-pr", gentle.format("1.0"), "2026-05-07"),
                                   ("flow", "", "2026-07-15"), ("distill", "", "2026-10-01")):
            skill(base, nombre, front)
            os.utime(base / nombre / "SKILL.md", (_epoch(dia), _epoch(dia)))
        listados = [(fx.ts(0, dia=3), "2.1.301", []), (fx.ts(0, dia=12), "2.1.320", [])]
        with mock.patch.object(claudit, "_version_binario", lambda cmd: "1.19.0" if cmd[0] == "engram" else ""):
            self.comps = {c["nombre"]: c for c in claudit.componentes_versiones(listados)}
            self.orden = list(self.comps)

    def test_detecta_lo_instalado_en_esta_maquina_en_orden_estable(self):
        self.assertEqual(self.orden, ["Claude Code", "claudit", "engram", "otro",
                                      "aegyros", "gentleman-programming"])

    def test_claude_code_desde_transcripts(self):
        cc = self.comps["Claude Code"]
        self.assertEqual((cc["version"], cc["desde"], cc["actualizada"]), ("2.1.320", "2026-11-03", "2026-11-12"))
        self.assertEqual(cc["eventos"], [("2026-11-03", "2.1.301"), ("2026-11-12", "2.1.320")])

    def test_plugin_con_instalacion_y_actualizacion(self):
        c = self.comps["claudit"]
        self.assertEqual((c["version"], c["desde"], c["actualizada"]), ("1.4.0", "2026-07-18", "2026-07-20"))
        self.assertEqual(c["eventos"], [("2026-07-18", "instalado 1.4.0"), ("2026-07-20", "actualizado 1.4.0")])

    def test_version_propia_del_binario_si_esta_permitido(self):
        self.assertEqual(self.comps["engram"]["version"], "1.19.0 · plugin 0.1.0")
        self.assertEqual(self.comps["otro"]["version"], "2.0.0")

    def test_autores_de_skills_con_version_por_skill_o_sin_version(self):
        g, a = self.comps["gentleman-programming"], self.comps["aegyros"]
        self.assertEqual(g["version"], "por skill: 1.0, 3.0")
        self.assertEqual((g["desde"], g["actualizada"]), ("2026-05-06", "2026-05-07"))
        self.assertEqual(a["version"], "sin versión")
        self.assertEqual(a["eventos"], [("2026-07-15", "1 skill instalada/actualizada"),
                                        ("2026-10-01", "1 skill instalada/actualizada")])


class TestVersionBinario(unittest.TestCase):
    def test_sin_binario_no_ejecuta_nada(self):
        with mock.patch.object(claudit.shutil, "which", return_value=None), \
                mock.patch.object(claudit.subprocess, "run") as run:
            self.assertEqual(claudit._version_binario(["engram", "version"]), "")
            run.assert_not_called()

    def test_extrae_la_version_de_la_salida(self):
        out = mock.Mock(stdout="engram 1.19.0\n")
        with mock.patch.object(claudit.shutil, "which", return_value="/bin/engram"), \
                mock.patch.object(claudit.subprocess, "run", return_value=out):
            self.assertEqual(claudit._version_binario(["engram", "version"]), "1.19.0")


def comp(nombre, eventos, version="1.0"):
    return {"nombre": nombre, "version": version, "desde": eventos[0][0], "actualizada": eventos[-1][0],
            "fuente": "x", "eventos": eventos}


class TestVersionesHtml(unittest.TestCase):
    def setUp(self):
        self.comps = [comp("Claude Code", [("2026-09-30", "2.1.282"), ("2026-11-13", "2.1.323")], "2.1.323"),
                      comp("engram", [("2026-11-13", "instalado 0.1.0")])]
        self.html = claudit._versiones_html(self.comps)

    def test_tabla_con_color_y_version_propia(self):
        tabla = self.html.split('class="setup-tree vs-tabla"')[1].split("</table>")[0]
        self.assertIn("var(--vs-1)", tabla)
        self.assertIn("var(--vs-2)", tabla)
        self.assertIn("2.1.323", tabla)
        self.assertIn("En esta máquina desde", tabla)  # la fecha es local; la version, del publicador

    def test_calendario_dice_que_es_de_esta_maquina(self):
        self.assertIn("en esta máquina", self.html.split('class="vs-cal"')[1])

    def test_calendario_mes_a_mes_del_primer_al_ultimo_evento(self):
        self.assertEqual(self.html.count('class="cal-mes"'), 3)  # sep, oct, nov
        self.assertIn(">sep 2026<", self.html)
        self.assertIn(">nov 2026<", self.html)

    def test_dia_con_eventos_lleva_un_punto_por_componente_y_tooltip(self):
        dia = self.html.split('data-tip-title="2026-11-13"')[1].split("</td>")[0]
        self.assertIn("Claude Code 2.1.323 · engram instalado 0.1.0", dia)
        self.assertEqual(dia.count('class="cal-dot"'), 2)
        self.assertIn('tabindex="0" data-tip-title="2026-11-13"', self.html)

    def test_mas_de_ocho_componentes_no_reciclan_color(self):
        muchos = [comp(f"c{i}", [("2026-11-01", "v")]) for i in range(9)]
        self.assertIn("var(--neutral)", claudit._versiones_html(muchos))
        self.assertNotIn("var(--vs-9)", claudit._versiones_html(muchos))

    def test_sin_componentes_no_dibuja_nada(self):
        self.assertEqual(claudit._versiones_html([]), "")


class TestTemplate(unittest.TestCase):
    def test_tokens_de_color_y_tooltip_del_calendario(self):
        tpl = (fx.PLUGIN / "report.html").read_text("utf-8")
        for i in range(1, 9):
            self.assertIn(f"--vs-{i}:", tpl)
        self.assertIn(".cal-ev[data-tip]", tpl)


if __name__ == "__main__":
    unittest.main()
