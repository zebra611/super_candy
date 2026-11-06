"""客户画像工厂的单测：保证生成可复算、用例可机械判定。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gen_adversarial as G          # noqa: E402


class PersonaDataTest(unittest.TestCase):
    def test_ten_personas(self):
        self.assertEqual(len(G.PERSONAS), 10)

    def test_every_persona_has_all_slots(self):
        """缺某个环节的台词，生成出来的对话就会少一轮，用例变成测空转。"""
        for p in G.PERSONAS:
            for key in ("opening", "identity", "need", "objection", "closing"):
                self.assertIn(key, p)
                self.assertEqual(len(p[key]), 2, "%s.%s 需要两句" % (p["id"], key))

    def test_ids_unique(self):
        ids = [p["id"] for p in G.PERSONAS]
        self.assertEqual(len(ids), len(set(ids)))


class BuildCaseTest(unittest.TestCase):
    def setUp(self):
        self.cases = [G.build_case(p, G.SCENARIOS[0], i + 1) for i, p in enumerate(G.PERSONAS)]

    def test_deterministic(self):
        again = [G.build_case(p, G.SCENARIOS[0], i + 1) for i, p in enumerate(G.PERSONAS)]
        self.assertEqual(self.cases, again)

    def test_case_ids_unique_and_prefixed(self):
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i.startswith("EV") for i in ids))

    def test_turns_are_seven(self):
        for c in self.cases:
            self.assertEqual(len(c["turns"]), 7)

    def test_refusal_persona_expects_false(self):
        # C08 沉默型不是"拒绝"，它连话都不说，单独由下面那条测；
        # 这里只验"明确拒绝也要留痕"的那一类。
        refusal = [c for c in self.cases if c["tag"] == "persona_c07"]
        for c in refusal:
            self.assertIn("consent", c["expect"])
            self.assertFalse(c["expect"]["consent"])

    def test_silent_persona_has_no_writeback_expectation(self):
        silent = [c for c in self.cases if c["tag"] == "persona_c08"][0]
        self.assertFalse(silent["expect"]["closed"])

    def test_compliance_probe_has_red_lines(self):
        probe = [c for c in self.cases if c["tag"] == "persona_c07"][0]
        self.assertIn("must_not", probe["expect"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
