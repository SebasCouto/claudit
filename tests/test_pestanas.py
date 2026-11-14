"""Dashboard en pestañas: Sesión / Contexto / Uso dentro del mismo HTML."""
import re
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


class TestNavLateral(unittest.TestCase):
    """Nav lateral izquierdo: un grupo por vista, un link por seccion."""

    def setUp(self):
        tpl = (fx.PLUGIN / "report.html").read_text("utf-8")
        self.nav = tpl.split('<aside class="sidenav"')[1].split("</aside>")[0]
        self.cuerpo = tpl.split("</aside>")[1]
        self.panel = lambda n: self.cuerpo.split(f'data-panel="{n}"')[1].split('class="tab-panel"')[0]

    def test_cada_seccion_tiene_link_y_cae_en_su_vista(self):
        links = re.findall(r'data-tab="(\w+)" href="#([\w-]+)"', self.nav)
        self.assertGreaterEqual(len(links), 10)
        for vista, ancla in links:
            if ancla != vista:
                self.assertIn(f'id="{ancla}"', self.panel(vista), ancla)

    def test_ninguna_seccion_queda_fuera_del_nav(self):
        for ancla in re.findall(r'<(?:section|details) class="card[^"]*" id="([\w-]+)"', self.cuerpo):
            self.assertIn(f'href="#{ancla}"', self.nav, ancla)

    def test_toggle_movil_accesible(self):
        self.assertIn('aria-controls="sidenav"', self.cuerpo.split('data-panel="sesion"')[0])


if __name__ == "__main__":
    unittest.main()
