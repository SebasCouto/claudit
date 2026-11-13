"""Medicion de cada tipo de adjunto del setup + system prompt y tool-defs reales."""
import json
import unittest

import _fixtures as fx
import claudit


def comps(lineas):
    return claudit.desglose_setup(lineas, claudit.leer_inferencias(lineas),
                                  cpt_flag=claudit.CHARS_POR_TOKEN)["componentes"]


def por_tipo(cs):
    out = {}
    for c in cs:
        out[c["tipo"]] = out.get(c["tipo"], 0) + c["tok"]
    return out


class TestCharsAdjunto(unittest.TestCase):
    def test_deltas(self):
        self.assertEqual(claudit.chars_adjunto({"type": "deferred_tools_delta", "addedLines": ["ab", "cde"]})[0], 6)
        self.assertEqual(claudit.chars_adjunto({"type": "mcp_instructions_delta", "addedBlocks": ["x" * 10]})[0], 10)

    def test_output_style(self):
        n, tipo, est = claudit.chars_adjunto({"type": "output_style_instructions",
                                              "style": {"name": "S", "prompt": "p" * 500}})
        self.assertEqual((n, tipo, est), (500, "sistema", False))

    def test_session_context_y_environment(self):
        n, _, est = claudit.chars_adjunto({"type": "session_context", "context": {"a": "x" * 30, "b": "y" * 20}})
        self.assertEqual((n, est), (50, False))
        snap = {"platform": "darwin", "shell": "zsh"}
        n, _, _ = claudit.chars_adjunto({"type": "environment", "snapshot": snap})
        self.assertEqual(n, len(json.dumps(snap, ensure_ascii=False)))

    def test_prompt_snapshot_excluido(self):
        self.assertEqual(claudit.chars_adjunto({"type": "prompt_snapshot", "systemPrompt": ["x" * 9000]})[0], 0)

    def test_desconocido_estimado(self):
        att = {"type": "algo_nuevo", "dato": "z" * 40}
        n, tipo, est = claudit.chars_adjunto(att)
        self.assertEqual(n, len(json.dumps(att, ensure_ascii=False)))
        self.assertTrue(est)
        self.assertEqual(tipo, "otros")


class TestDesgloseConForma(unittest.TestCase):
    def setUp(self):
        fx.aislar_home(self)

    def test_system_prompt_y_tools_medidos(self):
        tools = [{"name": "Bash", "description": "x" * 1000, "input_schema": {}},
                 {"name": "mcp__srv__leer", "description": "y" * 400, "input_schema": {}}]
        lineas = [fx.forma(system_chars=4000, tools=tools), fx.asistente(fx.usage(write=3000), fx.ts(1))]
        t = por_tipo(comps(lineas))
        k = claudit.CHARS_POR_TOKEN
        self.assertAlmostEqual(t["sistema"], 4000 / k)
        self.assertAlmostEqual(t["tools"], len(json.dumps(tools[0], ensure_ascii=False)) / k)
        self.assertAlmostEqual(t["mcp_tools"], len(json.dumps(tools[1], ensure_ascii=False)) / k)

    def test_forma_posterior_a_la_1a_inferencia_no_cuenta(self):
        lineas = [fx.asistente(fx.usage(write=3000), fx.ts(1)), fx.forma(system_chars=4000)]
        labels = [c["label"] for c in comps(lineas)]
        self.assertNotIn("system prompt", labels)
        self.assertIn("base harness + tool-defs (no editable)", labels)

    def test_agentes_custom_vs_builtin(self):
        att = {"type": "agent_listing_delta", "builtInTypes": ["Explore"],
               "addedLines": ["- Explore: " + "e" * 300, "- mi-worker: " + "w" * 100]}
        cs = comps([fx.adjunto(att), fx.asistente(fx.usage(write=3000), fx.ts(1))])
        agentes = [c for c in cs if c["tipo"] == "agentes"]
        self.assertEqual([c["label"] for c in agentes], ["agente mi-worker"])
        self.assertIn("tools", por_tipo(cs))

    def test_mcp_instrucciones_y_snapshot(self):
        lineas = [fx.adjunto({"type": "mcp_instructions_delta", "addedBlocks": ["m" * 800]}),
                  fx.adjunto({"type": "prompt_snapshot", "systemPrompt": ["x" * 90000]}),
                  fx.asistente(fx.usage(write=3000), fx.ts(1))]
        cs = comps(lineas)
        self.assertAlmostEqual(por_tipo(cs)["mcp"], 800 / claudit.CHARS_POR_TOKEN)
        self.assertFalse(any("prompt_snapshot" in c["label"] for c in cs))


if __name__ == "__main__":
    unittest.main()
