# -*- coding: utf-8 -*-
"""11-12 六张截图回归：开场白语法 / 过期日期 / 分步结尾 / 补齐范围 / 误判收线 / 回声复述。

纪律：全部走真跑路径（app.handle_turn）或真实生效配置，不用手工 flags 造假；
每个正向断言都配一条反向对照，防止"词表放宽了什么都拦"式的假绿。
"""
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader
from modules import dialogue as D

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CONFIGS = ("config/scene.json", "config/scenes/pop_reject.json")


def _load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return json.load(f)


def _all_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            for x in _all_strings(v):
                yield x
    elif isinstance(obj, list):
        for v in obj:
            for x in _all_strings(v):
                yield x


def _run_chain():
    """真跑一通：开场 → 怎么了 → 确认公司 → 核身 → 四连追问。返回 (开场白, 档案, 轮次结果)。"""
    prev = config_loader.scene_path()
    config_loader.set_scene("pop_reject")
    try:
        import app as A
        sid = A.start_call()["session_id"]
        sess = A._SESSIONS[sid]
        tgt = sess.get("outbound_target") or {}
        opening = D._fill_outbound(
            config_loader.stage_config("GREETING")["reply"], tgt)
        turns = [
            "怎么了",
            "是的",
            "尾号%s" % (tgt.get("phone_tail") or "0086"),
            "这几步都要弄吗",
            "补齐到哪",
            "还有疑问",
            "你到底啥意思",
            "同意不需要啥",
        ]
        outs = [(t, A.handle_turn(sid, user_text=t)) for t in turns]
        A.end_call(sid)
        return opening, tgt, outs
    finally:
        config_loader.set_scene(prev)


class TestScripts(unittest.TestCase):
    """配置层：两份场景包必须同步，且绝不播出会过期的绝对日期。"""

    def test_two_scene_packs_in_sync(self):
        a, b = _load(CONFIGS[0]), _load(CONFIGS[1])
        keys = ("reply", "reply_confirm_company", "reply_lang_barrier",
                "reply_protest_next", "reply_before_confirm")
        for k in keys:
            self.assertEqual(a["stages"]["GREETING"].get(k),
                             b["stages"]["GREETING"].get(k), k)
        for stage in ("NEED", "OFFER", "CONFIRM"):
            for k in ("reply_help_steps", "reply_scope", "reply_scope_2",
                      "reply_scope_3", "reply_doubt", "reply_doubt_2",
                      "reply_doubt_3", "reply_application_detail_rich",
                      "reply_application_detail_rich_2"):
                self.assertEqual(a["stages"][stage].get(k),
                                 b["stages"][stage].get(k), stage + "." + k)

    def test_no_absolute_date_anywhere_in_live_scripts(self):
        # 档案里的 recheck_deadline 是 2026-09/10 的固定日期，到 11 月已过期。
        # 用户 11-12 要求只说相对口径「一周内」，不许播具体时间。
        for rel in CONFIGS:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                raw = f.read()
            self.assertEqual([], DATE_RE.findall(raw), rel + " 仍含绝对日期")
            d = json.loads(raw)
            for stage in ("NEED", "OFFER", "CONFIRM"):
                st = d["stages"][stage]
                for k, v in st.items():
                    if isinstance(v, str) and k.startswith("reply_"):
                        self.assertNotIn("{deadline}", v, rel + " " + stage + "." + k)

    def test_relative_deadline_and_step_ending(self):
        for rel in CONFIGS:
            d = _load(rel)
            for stage in ("NEED", "OFFER", "CONFIRM"):
                t = d["stages"][stage]["reply_help_steps"]
                self.assertIn("建议一周内完成", t)
                self.assertIn("您对哪一步还不清楚？", t)
                self.assertNotIn("您想先弄哪一样", t, "按顺序操作却问先弄哪一样")
            self.assertTrue(d["stages"]["CONFIRM"]["reply_ambiguous_hold"])
            self.assertNotIn("同意", d["stages"]["CONFIRM"]["reply_ambiguous_hold"])

    def test_opening_no_role_preset_and_no_full_legal_name(self):
        prev = config_loader.scene_path()
        config_loader.set_scene("pop_reject")
        try:
            import modules.tools as T
            tgt = (T.next_outbound_target().get("application") or {})
            line = D._fill_outbound(config_loader.stage_config("GREETING")["reply"], tgt)
            self.assertIn("请问您是", line)       # 赛题②环硬校验
            self.assertIn("这边吗", line)          # 主宾：您（人）≠ 店（机构）
            self.assertNotIn(tgt.get("contact_role") or "___", line)
            self.assertNotIn(tgt.get("company_name") or "___", line)
            self.assertEqual([], DATE_RE.findall(line))
        finally:
            config_loader.set_scene(prev)


class TestEchoGuard(unittest.TestCase):
    """「怎么了，得先确认下…」这种拿客户原话开头的回声必须剥掉。"""

    def test_strip_echo(self):
        self.assertEqual(
            "得先确认下您是XX吗？",
            D._strip_user_echo("怎么了，得先确认下您是XX吗？", "怎么了"))

    def test_mid_sentence_mention_is_kept(self):
        s = "您刚问怎么了，我说明一下"
        self.assertEqual(s, D._strip_user_echo(s, "怎么了"))

    def test_too_short_rest_is_kept(self):
        self.assertEqual("怎么了", D._strip_user_echo("怎么了", "怎么了"))

    def test_reply_path_strips_echo(self):
        s = D.new_session("echo")
        s["last_user_text"] = "怎么了"
        out = D._reply(s, "怎么了，得先确认下您是XX吗？", intent="ask")
        self.assertFalse(out["reply_text"].startswith("怎么了"))


class TestScopeGates(unittest.TestCase):
    """反向对照：场景闸 / 表态闸不能拆，质疑不能被当成拒绝。"""

    def _session(self, with_app=True):
        s = D.new_session("scope")
        if with_app:
            s["slots"]["application"] = {
                "brand_name": "暖时光", "category_l1": "家用电器",
                "reject_reason": "授权链路不完整",
                "required_materials": ["品牌授权书", "逐级授权链路证明"],
                "recheck_deadline": "2026-09-09",
            }
        return s

    def test_no_application_scene_is_not_intercepted(self):
        self.assertIsNone(D._is_scope_question(
            self._session(False), "补齐到哪", {"ask": True}))

    def test_explicit_refusal_is_not_intercepted(self):
        self.assertIsNone(D._is_scope_question(
            self._session(), "不需要，别打了", {"no": True}))

    def test_scope_and_doubt_are_distinct(self):
        s = self._session()
        self.assertEqual("scope", D._is_scope_question(s, "补齐到哪", {"ask": True}))
        self.assertEqual("scope", D._is_scope_question(s, "这几步都要弄吗", {"ask": True}))
        self.assertEqual("doubt", D._is_scope_question(s, "还有疑问", {}))
        self.assertEqual("doubt", D._is_scope_question(s, "你到底啥意思", {}))

    def test_doubt_beats_no(self):
        # 「同意不需要啥」里带"不需要"，classify 判 no；客户其实是在问"你说的啥"。
        self.assertEqual("doubt", D._is_scope_question(
            self._session(), "同意不需要啥", {"no": True}))

    def test_ordinary_question_is_not_intercepted(self):
        self.assertIsNone(D._is_scope_question(
            self._session(), "这个收费吗", {"ask": True}))


class TestRealConversationChain(unittest.TestCase):
    """11-12 截图整条链，真跑 app.handle_turn。"""

    @classmethod
    def setUpClass(cls):
        cls.opening, cls.tgt, cls.outs = _run_chain()
        cls.by_text = {t: o for t, o in cls.outs}

    def test_opening(self):
        self.assertIn("请问您是", self.opening)
        self.assertIn("这边吗", self.opening)
        self.assertNotIn("品牌授权经办人", self.opening)

    def test_no_echo_in_greeting_answer(self):
        r = self.by_text["怎么了"]["reply_text"]
        self.assertFalse(r.startswith("怎么了"))

    def test_all_replies_have_no_absolute_date(self):
        for t, o in self.outs:
            self.assertEqual([], DATE_RE.findall(o.get("reply_text") or ""), t)

    def test_first_scope_question_gets_direct_answer(self):
        o = self.by_text["这几步都要弄吗"]
        self.assertIn("都要走完", o["reply_text"])
        self.assertEqual("NEED", o["next_stage"])

    def test_second_scope_question_gives_range_not_confirm(self):
        o = self.by_text["补齐到哪"]
        t = o["reply_text"]
        self.assertIn("都要备齐", t)
        self.assertIn("建议一周内完成", t)
        self.assertIn("您对哪一步还不清楚", t)
        self.assertNotIn("您看这样解释清楚了吗", t)
        self.assertNotEqual("CONFIRM", o["next_stage"])

    def test_doubt_is_answered_not_treated_as_ambiguity(self):
        o = self.by_text["还有疑问"]
        t = o["reply_text"]
        self.assertNotIn("同意", t)
        self.assertNotIn("不需要", t)
        self.assertNotIn("回我一句", t)
        self.assertNotEqual("DONE", o["next_stage"])

    def test_consecutive_doubt_does_not_repeat_verbatim(self):
        a = self.by_text["还有疑问"]["reply_text"]
        b = self.by_text["你到底啥意思"]["reply_text"]
        self.assertNotEqual(a, b, "连问两轮又逐字复读")

    def test_question_is_never_recorded_as_declined(self):
        o = self.by_text["同意不需要啥"]
        self.assertNotEqual("declined", o["intent"])
        self.assertNotEqual("DONE", o["next_stage"])


if __name__ == "__main__":
    unittest.main()
