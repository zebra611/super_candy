# -*- coding: utf-8 -*-
"""11-12 三张截图回归：清单直接发后台 / 清楚了就收线 / 中英互切。

纪律：全部走真跑路径（app.handle_turn）或真实工具 dispatch；
每组正向断言配反向对照，防止"词表放宽了什么都拦"式的假绿。
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader
from modules import dialogue as D, i18n, tools

_ZH = re.compile(r"[\u4e00-\u9fff]")


def _run(turns, scene="pop_reject"):
    """真跑一通，返回 (session_id, 档案, [(话术, 回复)], 工单)。"""
    prev = config_loader.scene_path()
    config_loader.set_scene(scene)
    try:
        import app as A
        sid = A.start_call()["session_id"]
        tgt = A._SESSIONS[sid].get("outbound_target") or {}
        outs = [(t, A.handle_turn(sid, user_text=t)) for t in turns]
        ticket = tools.ticket_for_session(sid)
        A.end_call(sid)
        return sid, tgt, outs, ticket
    finally:
        config_loader.set_scene(prev)


def _is_zh(s):
    return bool(_ZH.search(s or ""))


class TestChecklistTool(unittest.TestCase):
    """「直接把清单发到后台」必须是真实工具动作，不是一句口头承诺。"""

    def test_send_is_real_and_idempotent(self):
        aid = "QZ-TEST-CHECKLIST-A"
        tc = {"name": "send_checklist", "args": {
            "session_id": "s_test_a", "application_id": aid,
            "materials": ["品牌授权书", "逐级授权链路证明"]}}
        r1 = tools.dispatch(dict(tc))
        self.assertTrue(r1.get("ok"), r1)
        self.assertTrue(r1.get("checklist_id"))
        self.assertEqual(["品牌授权书", "逐级授权链路证明"], r1["materials"])
        self.assertFalse(r1["already_sent"])
        r2 = tools.dispatch(dict(tc))
        self.assertTrue(r2["already_sent"], "重复调用必须幂等")
        self.assertEqual(r1["checklist_id"], r2["checklist_id"])
        self.assertEqual(r1["checklist_id"], tools.checklist_for_application(aid))

    def test_missing_args_is_rejected(self):
        r = tools.dispatch({"name": "send_checklist",
                            "args": {"session_id": "s_x", "materials": ["营业执照"]}})
        self.assertEqual("INVALID_ARG", r.get("error"))

    def test_empty_materials_is_rejected(self):
        r = tools.dispatch({"name": "send_checklist",
                            "args": {"session_id": "s_x", "application_id": "QZ-X",
                                     "materials": []}})
        self.assertEqual("INVALID_ARG", r.get("error"))


class TestChecklistChain(unittest.TestCase):
    """19:42 / 19:46 两张长链：问材料、说清楚了，都不该再被复述/追问。"""

    @classmethod
    def setUpClass(cls):
        cls.sid, cls.tgt, cls.outs, cls.ticket = _run(
            ["是的", "尾号0086", "不太清楚", "材料", "知道了", "需要",
             "清楚了清楚了"])
        cls.by = {t: o for t, o in cls.outs}

    def test_material_choice_answers_only_materials(self):
        o = self.by["材料"]
        t = o["reply_text"]
        self.assertEqual("materials_only", o["intent"])
        for m in (self.tgt.get("required_materials") or []):
            self.assertIn(m, t)
        self.assertIn("清单我已经发到后台", t)
        self.assertNotIn("没有通过审核", t)
        self.assertNotIn("原因是", t)
        self.assertNotIn("这一步您清楚怎么补吗", t)

    def test_known_sends_checklist_instead_of_asking(self):
        """19:42：客户说「知道了」，不再问要不要发清单，直接发。"""
        o = self.by["知道了"]
        self.assertEqual("checklist_sent", o["intent"])
        self.assertIn("已经发到您商家后台", o["reply_text"])
        self.assertIn("直接翻后台清单", o["reply_text"])
        self.assertNotIn("需要我把清单发到后台吗", o["reply_text"])

    def test_need_after_checklist_is_short_ack(self):
        """客户接着说「需要」：正面确认，不整段复述。"""
        o = self.by["需要"]
        self.assertIn("清单", o["reply_text"])
        self.assertNotIn("我跟您复述一下", o["reply_text"])
        self.assertLess(len(o["reply_text"]), 60)

    def test_done_ack_closes_immediately(self):
        o = self.by["清楚了清楚了"]
        self.assertEqual("DONE", o["next_stage"])
        self.assertEqual("consent_checklist", o["intent"])
        self.assertIn("清单也在您商家后台", o["reply_text"])
        self.assertNotIn("稍后会有短信同步", o["reply_text"])
        self.assertNotIn("我跟您复述一下", o["reply_text"])

    def test_checklist_really_sent_and_recorded_in_ticket(self):
        self.assertTrue(tools.checklist_for_application(self.tgt["application_id"]))
        self.assertIsNotNone(self.ticket, "收线轮必须真的回写工单")
        content = self.ticket.get("content") or {}
        self.assertTrue(content.get("checklist_sent"))
        self.assertTrue(content.get("checklist_id"))

    def test_negative_no_ack_before_facts(self):
        s = D.new_session("t-no-ack")
        s["slots"]["application"] = {"application_id": "QZ-N",
                                     "required_materials": ["营业执照"]}
        self.assertFalse(D._is_done_ack(s, "清楚了", {}), "事实没讲过不许直接收线")
        self.assertFalse(D._is_done_ack(s, "不清楚", {"vague": True}), "否定不能当肯定")
        s["facts_spoken"] = True
        self.assertFalse(D._is_done_ack(s, "好的，清楚了", {}),
                         "平静的清楚了要留给四段式④推进，不许抢先收线")
        self.assertTrue(D._is_done_ack(s, "清楚了清楚了", {}))
        self.assertFalse(D._is_done_ack(D.new_session("t-empty"), "清楚了", {}),
                         "无档案场景不许触发")

    def test_no_restatement_after_clear(self):
        """19:46 长链：客户连说清楚了，不应再出现第四次复述。"""
        _, _, outs, _ = _run(["是的", "尾号0086", "什么授权", "清楚了清楚了",
                              "知道了！！！", "没有了！"])
        texts = [o.get("reply_text") or "" for _, o in outs]
        o = outs[2][1]
        self.assertEqual("what_auth", o["intent"])
        self.assertIn("授权", o["reply_text"])
        self.assertIn("清单", o["reply_text"])
        closed = outs[3][1]
        self.assertEqual("DONE", closed["next_stage"])
        for t in texts[3:]:
            self.assertNotIn("我跟您复述一下", t)
            self.assertNotIn("还有不", t)


class TestLanguageSwitch(unittest.TestCase):
    """19:48 截图：说 yes 必须有反应，且中英能互切。"""

    def test_detect_and_intent_map(self):
        # 11-13：短回答（yes / ok）不再切语言，完整英文句子才切。
        # 但"短回答要能听懂"这件事没变——to_zh 的映射照旧，见下面的断言。
        self.assertEqual("zh", i18n.detect("yes"))
        self.assertEqual("en", i18n.detect("I want to know why it was rejected"))
        self.assertEqual("zh", i18n.detect("怎么了"))
        self.assertEqual("是的", i18n.to_zh("yes"))
        self.assertEqual("是的，是我", i18n.to_zh("i am"))
        self.assertEqual("不是", i18n.to_zh("no"))
        self.assertEqual("稍等", i18n.to_zh("hold on hold on"))
        self.assertEqual("材料", i18n.to_zh("what materials"))
        self.assertTrue(i18n.wants_chinese("i can't understand english"))
        self.assertFalse(i18n.wants_chinese("yes"))
        self.assertTrue(i18n.asks_english("can you speak english"))

    def test_yes_is_not_ignored_and_reply_is_chinese(self):
        """11-12 提的是"说 yes 必须有反应"，11-13 补了"别一听 yes 就切英文"。

        两件事都要：yes 要被当成"是的"接住并推进到核身，回复仍然是中文。
        """
        _, _, outs, _ = _run(["yes"])
        o = outs[0][1]
        self.assertEqual("IDENTITY", o["next_stage"], "yes 必须确认公司并进核身")
        self.assertTrue(_is_zh(o["reply_text"]), "一句 yes 就把回复切成英文了")
        self.assertIn("后四位", o["reply_text"])

    def test_english_chain_stays_english(self):
        """切过去之后一直是英文：中间夹一句 ok 也不能切回来。

        语种切换的入口从"短回答"换成了"完整英文句子"，所以这条链的起点也换了——
        用 yes/ok 起头的话，现在压根不会切到英文（那正是 11-13 要的效果）。
        """
        _, _, outs, _ = _run(["I want to know why my application was rejected",
                              "ok",
                              "can you explain the materials again",
                              "hold on hold on"])
        for t, o in outs:
            text = o.get("reply_text") or ""
            # 英文话术里**主体名保留中文**（安食），所以不能拿"有没有汉字"当判据。
            # 要守的是"没有整句中文"。
            words = re.findall(r"[A-Za-z]{2,}", text)
            self.assertGreaterEqual(len(words), 3,
                                    "说的是 %r，回复不像英文：%s" % (t, text))
            for zh in ("请问您是", "我这边", "咱们", "好的，", "您这笔"):
                self.assertNotIn(zh, text, "说的是 %r，冒出中文句子：%s" % (t, text))

    def test_ask_english_then_back_to_chinese(self):
        _, _, outs, _ = _run(["can you speak english",
                              "i can't understand english", "怎么了"])
        # 公司名/材料名是后台专有名词，允许保留中文；这里只守"整句是英语"。
        self.assertIn("English", outs[0][1]["reply_text"])
        for zh in ("请问您是", "我这边", "咱们", "好的，"):
            self.assertNotIn(zh, outs[0][1]["reply_text"])
        self.assertTrue(_is_zh(outs[1][1]["reply_text"]), "明确要求切回中文")
        self.assertIn("改用中文", outs[1][1]["reply_text"])
        self.assertTrue(_is_zh(outs[2][1]["reply_text"]), "中文继续就保持中文")

    def test_chinese_input_never_switches_to_english(self):
        _, _, outs, _ = _run(["是的", "尾号0086"])
        for t, o in outs:
            self.assertTrue(_is_zh(o.get("reply_text") or ""), t)

    def test_no_foreign_lang_rejection_in_english_mode(self):
        """英文模式不能再说「我这边暂时只能用普通话沟通」。"""
        _, _, outs, _ = _run(["yes", "what do you mean"])
        for t, o in outs:
            self.assertNotIn("只能用普通话", o.get("reply_text") or "")


if __name__ == "__main__":
    unittest.main()
