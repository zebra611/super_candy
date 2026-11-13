# -*- coding: utf-8 -*-
"""清单发完之后会不会"打转"、收线之后会不会"接着说"（11-13 截图）。

用户原话：

    前面还很正常，后面就开始牛头不对马嘴了。
    "不用再等我复述"这句话也很奇怪。
    而且闭环的时候甚至没有说问候语并说明客服电话，而且通话还能继续下去，很怪。

三件事对应三条断言：

    ① 清单发过之后，客户连说几句"对"，不能再原地念同一句清单提示——
       旧实现每句"对"都回到同一分支，阶段永远停在 NEED，通话说不到头；
    ② 收线句必须带热线（用户 11-12 就要求过，WRITEBACK 那条漏了）；
    ③ 收线之后再说话，只回一句"已经结束 + 热线"，不再走业务逻辑；
       但"等等，我说错了"要放行——那是改口，工单得改。

另加两条同一批发现的：礼貌"麻烦您…"不许被判成骂人（"烦"在情绪词表里），
以及话术里不许出现"不用再等我复述"这种把客户没说过的动作安在他头上的句子。

跑法：python -m scripts.test_checklist_close
"""

import os
import sys
import unittest

os.environ.setdefault("APP_SCENE", "pop_reject")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import app                                                   # noqa: E402
from modules import dialogue, tools                          # noqa: E402

HOTLINE = "4006180092"
#: 从开头走到"清单已发"的最短路径：确认公司 → 核身 → 说"材料"（触发发清单）。
TO_CHECKLIST = ("是的", "0086", "材料")


def _session():
    tools._reset_for_test()
    started = app.start_call(lock_target=True)
    sid = started["session_id"]
    sess = app._SESSIONS[sid]
    for t in TO_CHECKLIST:
        dialogue.respond(sess, t)
    return sid, sess


class ChecklistDoesNotSpin(unittest.TestCase):
    """"对 / 对 / 对"不能换来三遍同一句话。"""

    def setUp(self):
        self.sid, self.sess = _session()

    def tearDown(self):
        app.end_call(self.sid)

    def test_repeats_are_not_verbatim(self):
        outs = [dialogue.respond(self.sess, "对") for _ in range(3)]
        texts = [o.get("reply_text") or "" for o in outs]
        for i in range(1, len(texts)):
            self.assertNotEqual(texts[i - 1], texts[i],
                                "第 %d 句和第 %d 句逐字相同：%s" % (i, i + 1, texts[i]))

    def test_closes_instead_of_looping(self):
        """最迟第三句"对"必须收线，不能一直停在 NEED。"""
        for i in range(3):
            dialogue.respond(self.sess, "对")
        self.assertEqual("DONE", self.sess.get("stage"),
                         "连说三句对还没收线，阶段停在 %r" % self.sess.get("stage"))

    def test_stage_moves_forward(self):
        dialogue.respond(self.sess, "对")
        self.assertIn(self.sess.get("stage"), ("OFFER", "CONFIRM", "WRITEBACK", "DONE"),
                      "说了一句对，阶段还在 %r——原地打转" % self.sess.get("stage"))


class ClosingHasHotline(unittest.TestCase):
    """收线必须有热线，而且要在"再见"之前。"""

    def tearDown(self):
        if getattr(self, "sid", None):
            app.end_call(self.sid)

    def _closing_text(self, turns):
        self.sid, self.sess = _session()
        last = ""
        for t in turns:
            last = dialogue.respond(self.sess, t).get("reply_text") or ""
        return last

    def test_checklist_closing_has_hotline(self):
        """清单路径的收线句（WRITEBACK 的 reply_checklist_done）曾经没有热线。"""
        text = self._closing_text(["对", "对", "对"])
        self.assertIn(HOTLINE, text, "清单路径收线没给热线：%s" % text)
        self.assertIn("再见", text)
        self.assertLess(text.index(HOTLINE), text.index("再见"),
                        "热线要念在再见之前：%s" % text)

    def test_plain_closing_has_hotline(self):
        text = self._closing_text(["对", "对", "对", "清楚了清楚了"])
        self.assertIn(HOTLINE, text, text)


class AfterDone(unittest.TestCase):
    """收线之后还说话：不再走业务，但改口要放行。"""

    def setUp(self):
        self.sid, self.sess = _session()
        for _ in range(3):
            dialogue.respond(self.sess, "对")
        assert self.sess.get("stage") == "DONE", self.sess.get("stage")

    def tearDown(self):
        app.end_call(self.sid)

    def test_says_ended_and_keeps_done(self):
        r = dialogue.respond(self.sess, "对")
        text = r.get("reply_text") or ""
        self.assertEqual("done_again", r.get("intent"), text)
        self.assertEqual("DONE", self.sess.get("stage"))
        self.assertIn("结束", text, "没说清电话已经结束：%s" % text)
        self.assertIn(HOTLINE, text, "结束语没给热线：%s" % text)
        self.assertLess(len(text), 80, "结束语不该这么长：%s" % text)

    def test_no_business_content_after_done(self):
        r = dialogue.respond(self.sess, "那我再问个事")
        text = r.get("reply_text") or ""
        self.assertNotIn("驳回", text, "收线后还在讲业务：%s" % text)
        self.assertNotIn("材料", text, text)

    def test_correction_still_allowed(self):
        r = dialogue.respond(self.sess, "等等，我说错了，我不是本人")
        self.assertNotEqual("done_again", r.get("intent"),
                            "改口被当成闲聊挡掉了：%s" % r.get("reply_text"))


class NoNameAtSentenceStart(unittest.TestCase):
    """出话路径上不许以客户姓名开头——措辞层很爱加这个称呼。

    11-13 线上连续见到三种写法：`马涛，…` / `马涛先生，…` / `马涛您好，…`。
    agent_free 内部剥过一道，但措辞层把整句重写之后又会加回来，
    所以必须在 `_reply` 出口再剥一次。
    """

    def setUp(self):
        tools._reset_for_test()
        self.sid = app.start_call(lock_target=True)["session_id"]
        self.sess = app._SESSIONS[self.sid]

    def tearDown(self):
        app.end_call(self.sid)

    def test_salutation_forms_stripped(self):
        for text in ("马涛，这通电话就先到这里。",
                     "马涛先生，这通电话就先到这里。",
                     "马涛您好，这通电话就先到这里。",
                     "马经理，麻烦您报一下后四位。"):
            out = dialogue._reply(self.sess, text, next_stage="DONE",
                                  intent="done_again")
            self.assertNotIn("马涛", out["reply_text"], out["reply_text"])
            self.assertNotIn("马经理", out["reply_text"], out["reply_text"])

    def test_echo_of_reported_name_kept(self):
        """客户自己报了名字，句中的复述要留着（那是承接他给的值）。"""
        out = dialogue._reply(self.sess, "对，是您本人，马涛。",
                              next_stage="DONE", intent="done_again")
        self.assertIn("马涛", out["reply_text"], out["reply_text"])


class PoliteIsNotAngry(unittest.TestCase):
    """「麻烦您…」是客气话，不是骂人。"""

    def test_polite_request_not_angry(self):
        for t in ("麻烦把材料清单发到我商家后台",
                  "麻烦您把清单发我",
                  "麻烦帮我看下要补什么"):
            flags = dialogue.classify(t)
            self.assertFalse(flags.get("angry"), "%r 被判成骂人了" % t)

    def test_product_words_not_angry(self):
        for t in ("我卖滚筒洗衣机的", "我卖垃圾袋的"):
            self.assertFalse(dialogue.classify(t).get("angry"), t)

    def test_real_complaint_still_angry(self):
        for t in ("我就是要投诉你们", "你们这是骗子", "别烦我"):
            self.assertTrue(dialogue.classify(t).get("angry"), t)


class NoWeirdPhrase(unittest.TestCase):
    """「不用再等我复述」这句不成立——客户没让客服复述过。"""

    def test_no_odd_phrase_in_any_reply(self):
        self.sid, self.sess = _session()
        try:
            texts = []
            for t in ("对", "对", "对"):
                texts.append(dialogue.respond(self.sess, t).get("reply_text") or "")
        finally:
            app.end_call(self.sid)
        for t in texts:
            self.assertNotIn("等我复述", t, t)


if __name__ == "__main__":
    unittest.main(verbosity=1)
