"""京东人物库的验收测试：广度、唯一性、可复算、字段齐备。"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "eval"))

import persona_factory_jd as F          # noqa: E402

PATH = os.path.join(ROOT, "eval", "personas_jd.json")


def load():
    with open(PATH, encoding="utf-8") as f:
        return json.load(f)["personas"]


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.items = load()

    def test_size(self):
        self.assertGreaterEqual(len(self.items), 300)

    def test_professions_hundreds(self):
        n = len({i["profession"] for i in self.items})
        self.assertGreaterEqual(n, 150, "职业只有 %d 种" % n)

    def test_tone_variety(self):
        labels = len({i["tone"]["label"] for i in self.items})
        combos = len({(i["tone"]["label"], i["tone"]["level"]) for i in self.items})
        self.assertGreaterEqual(labels, 55, "语气标签只有 %d" % labels)
        self.assertGreaterEqual(combos, 150, "语气组合只有 %d" % combos)

    def test_attitudes(self):
        self.assertGreaterEqual(len({i["attitude"] for i in self.items}), 25)

    def test_languages_and_dialects(self):
        self.assertGreaterEqual(len({i["language"]["code"] for i in self.items}), 18)
        names = {i["language"]["name"] for i in self.items}
        self.assertIn("粤语", names)
        self.assertIn("四川话", names)

    def test_scenarios(self):
        self.assertEqual(len({i["scenario"]["id"] for i in self.items}), 10)


class ShapeTest(unittest.TestCase):
    def setUp(self):
        self.items = load()

    def test_required_fields(self):
        need = {"id", "scenario", "profession", "tone", "attitude", "language",
                "utterances", "expect", "tags"}
        for p in self.items:
            self.assertFalse(need - set(p), p["id"])

    def test_reply_guidance_present(self):
        """没有'得体回复要点'，人物库就只能挑错，说不出该怎么答。"""
        for p in self.items:
            self.assertTrue(p["scenario"]["reply_guide"].strip(), p["id"])
            self.assertTrue(p["scenario"]["need"].strip(), p["id"])

    def test_utterances_use_language_markers(self):
        for p in self.items:
            self.assertEqual(len(p["utterances"]), 4, p["id"])
            joined = " ".join(p["utterances"])
            marker = p["language"]["markers"][0]
            self.assertIn(marker, joined, "%s 没用上本语言的标志词" % p["id"])

    def test_ids_unique(self):
        ids = [p["id"] for p in self.items]
        self.assertEqual(len(ids), len(set(ids)))


class DeterminismTest(unittest.TestCase):
    def test_rebuild_is_identical(self):
        """同输入必须同输出，否则回归没有基准。"""
        self.assertEqual(F.build(50), F.build(50))


if __name__ == "__main__":
    unittest.main(verbosity=2)
