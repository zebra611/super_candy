"""对抗测试系统的单元测试：只测纯逻辑与边界，不跑对话、不写报告。

跑法：python3 scripts/test_sim.py
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim_dialogue as D             # noqa: E402
import sim_evaluate as E             # noqa: E402

PERSONAS = D.load_json(D.PERSONAS_PATH)["personas"]
SCENARIOS = D.load_json(D.SCENARIOS_PATH)["scenarios"]
SCENARIO_MAP = {s["id"]: s for s in SCENARIOS}
BY_ID = {p["id"]: p for p in PERSONAS}


def rec(persona="P01", scenario="S01", closed=False, ident=True, need=True, failed=None):
    return {"persona": persona, "persona_name": BY_ID[persona]["name"],
            "archetype": BY_ID[persona]["archetype"], "scenario": scenario,
            "scenario_name": SCENARIO_MAP[scenario]["name"], "turns": 5,
            "closed": closed, "consent": True, "identity_confirmed": ident,
            "need_captured": need, "red_line_failed": failed or [],
            "error": "", "error_events": 0, "transcript": []}


class PersonaSchemaTest(unittest.TestCase):
    def test_eight_personas(self):
        self.assertEqual(len(PERSONAS), 8)

    def test_every_persona_has_all_stage_lines(self):
        """缺某一环节的台词，对局跑到那儿就会空转，等于测了个寂寞。"""
        for p in PERSONAS:
            for cls in ("greeting", "identity", "need", "offer", "confirm", "closing"):
                self.assertIn(cls, p["reactions"], "%s 缺 %s" % (p["id"], cls))

    def test_six_scenarios(self):
        self.assertEqual(len(SCENARIOS), 6)


class CustomerLineTest(unittest.TestCase):
    def test_deterministic(self):
        p, s = BY_ID["P02"], SCENARIO_MAP["S03"]
        a = D.customer_line(p, s, "OFFER", 4, "seed")
        b = D.customer_line(p, s, "OFFER", 4, "seed")
        self.assertEqual(a, b)

    def test_stage_routing(self):
        """同一个角色，在确认环节说的话必须来自 confirm 池。"""
        p, s = BY_ID["P03"], SCENARIO_MAP["S01"]
        line = D.customer_line(p, s, "CONFIRM", 1, "seed")
        self.assertIn(line, p["reactions"]["confirm"])

    def test_opening_on_first_turn(self):
        p, s = BY_ID["P06"], SCENARIO_MAP["S05"]
        self.assertIn(D.customer_line(p, s, "GREETING", 0, "seed"), p["opening"])

    def test_escalation_only_after_third_turn(self):
        p, s = BY_ID["P02"], SCENARIO_MAP["S01"]
        early = D.customer_line(p, s, "NEED", 0, "seed")
        self.assertNotIn(early, p["escalation"])


class EvaluateTest(unittest.TestCase):
    def test_chance_close_rate(self):
        records = [rec("P01", "S01", True, ident=True), rec("P02", "S01", False, ident=True),
                   rec("P03", "S01", False, ident=False)]
        stats = E.evaluate(records, SCENARIO_MAP)
        self.assertEqual(stats["t1b_chance_close"]["chance"], 2)
        self.assertEqual(stats["t1b_chance_close"]["ok"], 1)
        self.assertAlmostEqual(stats["t1b_chance_close"]["rate"], 0.5)

    def test_dirty_close_detected(self):
        """没核验身份就写工单，是最危险的脏数据。"""
        records = [rec("P03", "S01", closed=True, ident=False)]
        stats = E.evaluate(records, SCENARIO_MAP)
        self.assertEqual(stats["t2_wrong_close"]["wrong"], 1)
        self.assertLess(stats["composite"], 1.0)

    def test_compliance_dirty_zeroes_composite(self):
        records = [rec("P01", "S01", True, failed=["no_inducement"])]
        stats = E.evaluate(records, SCENARIO_MAP)
        self.assertEqual(stats["t3_compliance"]["dirty"], 1)
        self.assertEqual(stats["composite"], 0.0)

    def test_empathy_hit_and_miss(self):
        good = rec()
        good["transcript"] = [
            {"role": "user", "text": "你们这群骗子", "turn": 0},
            {"role": "agent", "text": "非常理解您的心情，先别急", "turn": 0},
        ]
        bad = rec()
        bad["transcript"] = [
            {"role": "user", "text": "我要投诉你们", "turn": 0},
            {"role": "agent", "text": "好的，请您提供手机号后四位", "turn": 0},
        ]
        stats = E.evaluate([good, bad], SCENARIO_MAP)
        self.assertEqual(stats["t4_empathy"]["total"], 2)
        self.assertEqual(stats["t4_empathy"]["hit"], 1)

    def test_no_negative_turns_gives_none_rate(self):
        stats = E.evaluate([rec()], SCENARIO_MAP)
        self.assertIsNone(stats["t4_empathy"]["rate"])


class LatestRunTest(unittest.TestCase):
    def test_picks_by_mtime_not_name(self):
        """文件名带 -2 时字符串排序会取错，必须按修改时间取最新。"""
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "sim_20260101_0000-2.json")
            new = os.path.join(tmp, "sim_20260101_0000.json")
            for path in (old, new):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({}, f)
            os.utime(old, (time.time() - 100, time.time() - 100))
            original = E.RUNS_DIR
            E.RUNS_DIR = tmp
            try:
                self.assertEqual(E.latest_run(), new)
            finally:
                E.RUNS_DIR = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
