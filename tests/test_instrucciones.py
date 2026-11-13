"""Instrucciones (CLAUDE.md, MEMORY.md, rules) medidas desde el transcript, no del disco."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _fixtures as fx
import claudit


def instrucciones(*files):
    return fx.adjunto({"type": "instructions", "files": [
        {"path": p, "type": t, "content": c} for p, t, c in files]})


def comps(lineas):
    return claudit.desglose_setup(lineas, claudit.leer_inferencias(lineas))["componentes"]


class TestAbreviarRuta(unittest.TestCase):
    def test_mac(self):
        self.assertEqual(claudit.abreviar_ruta("/Users/ana/.claude/CLAUDE.md", "/Users/ana"), "~/.claude/CLAUDE.md")

    def test_windows(self):
        self.assertEqual(claudit.abreviar_ruta(r"C:\Users\Ana\.claude\CLAUDE.md", r"C:\Users\ana"),
                         "~/.claude/CLAUDE.md")

    def test_fuera_del_home(self):
        self.assertEqual(claudit.abreviar_ruta("/opt/x/CLAUDE.md", "/Users/ana"), "/opt/x/CLAUDE.md")

    def test_prefijo_parecido_no_es_home(self):
        self.assertEqual(claudit.abreviar_ruta("/Users/anabel/CLAUDE.md", "/Users/ana"), "/Users/anabel/CLAUDE.md")


class TestInstruccionesDelTranscript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        (home / ".claude").mkdir()
        (home / ".claude" / "CLAUDE.md").write_text("# DISCO\n" + "z" * 9000, encoding="utf-8")
        self.p = [mock.patch.object(claudit, "HOME", home), mock.patch.object(claudit, "REPO", home / "repo")]
        for x in self.p:
            x.start()
        self.home = home

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.tmp.cleanup()

    def test_un_componente_por_archivo(self):
        g = str(self.home / ".claude" / "CLAUDE.md")
        m = str(self.home / ".claude" / "projects" / "x" / "memory" / "MEMORY.md")
        lineas = [instrucciones((g, "User", "# Reglas\n" + "a" * 2000), (m, "AutoMem", "- item\n" * 50)),
                  fx.asistente(fx.usage(write=30000), fx.ts(1))]
        cs = {c["label"]: c for c in comps(lineas)}
        self.assertIn("User ~/.claude/CLAUDE.md", cs)
        self.assertIn("AutoMem ~/.claude/projects/x/memory/MEMORY.md", cs)
        c = cs["User ~/.claude/CLAUDE.md"]
        self.assertAlmostEqual(c["tok"], (len("# Reglas\n") + 2000) / claudit.CHARS_POR_TOKEN)
        self.assertEqual(c["tipo"], "memoria")
        self.assertFalse(any("DISCO" in t for t, _ in c["secc"] or []))

    def test_sin_adjunto_cae_al_disco(self):
        cs = comps([fx.asistente(fx.usage(write=30000), fx.ts(1))])
        globales = [c for c in cs if c["label"].endswith("~/.claude/CLAUDE.md")]
        self.assertEqual(len(globales), 1)
        self.assertTrue(globales[0]["estimado"])
        self.assertTrue(any("DISCO" in t for t, _ in globales[0]["secc"]))


if __name__ == "__main__":
    unittest.main()
