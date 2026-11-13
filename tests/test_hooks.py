"""Hooks: se mide lo que ve el modelo (content; stdout solo si no hay content),
una vez, con la etiqueta real del hook."""
import unittest

import _fixtures as fx
import claudit


def hook(nombre, content=None, stdout=None, command="", tipo="hook_success", **extra):
    att = {"type": tipo, "hookName": nombre, "hookEvent": nombre.split(":")[0], "command": command,
           "stderr": "", "exitCode": 0}
    if content is not None:
        att["content"] = content
    if stdout is not None:
        att["stdout"] = stdout
    att.update(extra)
    return att


class TestCharsHook(unittest.TestCase):
    def test_content_y_stdout_iguales_cuentan_una_vez(self):
        self.assertEqual(claudit.chars_hook(hook("SessionStart:startup", "x" * 800, "x" * 801)), 800)

    def test_sin_content_usa_stdout(self):
        self.assertEqual(claudit.chars_hook(hook("SessionStart:startup", stdout="y" * 300)), 300)

    def test_async_sin_respuesta_no_entra_al_contexto(self):
        att = hook("SessionStart:startup", tipo="async_hook_response", stderr="boom", response={})
        self.assertEqual(claudit.chars_hook(att), 0)


class TestEtiquetaHook(unittest.TestCase):
    def test_usa_hookname_y_comando(self):
        lab = claudit.etiqueta_hook(hook("SessionStart:startup", "x", command="Loading engram memory..."))
        self.assertIn("SessionStart:startup", lab)
        self.assertIn("Loading engram memory", lab)
        self.assertNotIn("(engram)", lab)

    def test_prefiere_plugin_id(self):
        lab = claudit.etiqueta_hook(hook("SessionStart:startup", "x", command="python3 x.py", pluginId="demo@demo"))
        self.assertIn("demo@demo", lab)


class TestDesgloseHooks(unittest.TestCase):
    def test_un_componente_por_hook_sin_duplicar(self):
        lineas = [
            fx.adjunto(hook("SessionStart:startup", "a" * 4000, "a" * 4001, command="Cargando memoria")),
            fx.adjunto(hook("UserPromptSubmit", "b" * 400, tipo="hook_system_message")),
            fx.asistente(fx.usage(read=0, write=20000, inp=10), fx.ts(1)),
        ]
        comps = claudit.desglose_setup(lineas, claudit.leer_inferencias(lineas))["componentes"]
        hooks = {c["label"]: c["tok"] for c in comps if c["label"].startswith("hook ")}
        self.assertEqual(len(hooks), 2)
        tok_startup = next(v for k, v in hooks.items() if "SessionStart:startup" in k)
        self.assertAlmostEqual(tok_startup, 4000 / claudit.CHARS_POR_TOKEN)


if __name__ == "__main__":
    unittest.main()
