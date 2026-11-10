"""职业画像库的校验测试：这批数据是下一轮对抗训练的输入，格式错了整批都跑不起来。"""

import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "eval", "personas_profession.json")
EXISTING = ("eval/personas_adversarial.json", "eval/personas_temperament.json")

REQUIRED = {"id", "label", "goal", "split", "knows", "willing", "traits",
            "style", "catchphrases", "expect"}
TRAITS = {"hearing", "patience", "trust0", "trust_gate", "verbosity"}


def load():
    with open(PATH, encoding="utf-8") as f:
        data = json.load(f)
    # sim_eval 的 load_personas() 要求 {"personas": [...]} 结构
    return data["personas"]


class ProfessionPersonaTest(unittest.TestCase):
    def setUp(self):
        self.items = load()

    def test_twelve_professions(self):
        self.assertEqual(len(self.items), 12)

    def test_required_fields(self):
        for p in self.items:
            self.assertFalse(REQUIRED - set(p), "%s 缺字段：%s" % (p["id"], REQUIRED - set(p)))

    def test_traits_complete(self):
        for p in self.items:
            self.assertEqual(set(p["traits"]), TRAITS, p["id"])

    def test_ids_unique_and_not_colliding(self):
        ids = [p["id"] for p in self.items]
        self.assertEqual(len(ids), len(set(ids)))
        for rel in EXISTING:
            other = os.path.join(ROOT, rel)
            if not os.path.exists(other):
                continue
            with open(other, encoding="utf-8") as f:
                raw = json.load(f)
            others = raw if isinstance(raw, list) else raw.get("personas") or []
            clash = {p["id"] for p in others} & set(ids)
            self.assertFalse(clash, "与 %s 的 id 撞车：%s" % (rel, clash))

    def test_goal_and_split_domain(self):
        for p in self.items:
            self.assertIn(p["goal"], ("confirm", "refuse", "ask"))
            self.assertIn(p["split"], ("train", "holdout"))

    def test_expect_shape(self):
        for p in self.items:
            self.assertIn("closed", p["expect"])
            self.assertIsInstance(p["expect"]["closed"], bool)

    def test_each_has_catchphrases(self):
        """每类职业至少要有一句口头禅，否则跑起来和通用画像没区别。"""
        for p in self.items:
            self.assertGreaterEqual(len(p["catchphrases"]), 3, p["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
