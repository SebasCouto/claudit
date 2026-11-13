"""Dashboard en pestañas: Sesión / Contexto / Uso dentro del mismo HTML."""
import unittest

import _fixtures as fx


class TestPestanas(unittest.TestCase):
    def setUp(self):
        self.tpl = (fx.PLUGIN / "report.html").read_text("utf-8")

    def test_tres_pestanas_con_su_panel(self):
        for n in ("sesion", "contexto", "uso"):
            self.assertIn(f'data-tab="{n}"', self.tpl)
            self.assertIn(f'data-panel="{n}"', self.tpl)

    def test_cada_seccion_en_su_pestana(self):
        panel = lambda n: self.tpl.split(f'data-panel="{n}"')[1].split('class="tab-panel"')[0]
        self.assertIn("__CLAUDIT_CONTEXTO__", panel("contexto"))
        self.assertIn("__CLAUDIT_CUPO__", panel("uso"))
        self.assertIn("__CLAUDIT_USO__", panel("uso"))
        self.assertIn("__CLAUDIT_SETUP_ROWS__", panel("sesion"))


if __name__ == "__main__":
    unittest.main()
