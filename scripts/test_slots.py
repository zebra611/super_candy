# -*- coding: utf-8 -*-
"""槽位驱动追问（T2）的离线自测。

跑法：python3 scripts/test_slots.py

锁的是"追问"这件事的四条底线，全部来自方案里 T2 的可执行验收：

    1. 缺尾号就问尾号，缺姓名就问姓名——不是永远念同一句；
    2. 同一个槽位最多问两次，两次都对不上就收尾转专员，不追着问第三遍；
    3. 身份没核上绝不往下走（不透露订单、不进方案、不回写）；
    4. 用户改口要覆盖旧值，不能新旧两个值同时在册。

第 4 条在 C03/C04 两条种子用例里已有，但那些用例只验"没有越界"，
不验"到底问了什么"。这里补上"问了什么"。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import dialogue  # noqa: E402


def _run(turns, sid):
    """跑一串用户话，返回 (session, [每轮输出])。"""
    s = dialogue.new_session(sid)
    outs = [dialogue.respond(s, t) for t in turns]
    return s, outs


class AskLogicTest(unittest.TestCase):
    """问什么、问几次。"""

    def test_first_question_mentions_tail(self):
        """M01 验收：第一句追问必须明确提到"尾号"。"""
        _, outs = _run(["是我"], "slot-first")
        self.assertIn("尾号", outs[0]["reply_text"],
                      "第一句核验追问没说尾号：%r" % outs[0]["reply_text"])

    def test_asks_alternate_and_never_repeat(self):
        """四个追问句必须互不相同——重复同一句是这轮改造要消灭的问题。"""
        _, outs = _run(["是我", "呃", "嗯", "呃"], "slot-alternate")
        asks = [o["reply_text"] for o in outs[1:]]
        self.assertEqual(len(asks), len(set(asks)), "有重复追问：%r" % asks)
        tail_hits = sum(1 for a in asks if "尾号" in a or "后四位" in a or "最后四位" in a)
        name_hits = sum(1 for a in asks if "姓名" in a or "称呼" in a)
        self.assertGreater(tail_hits, 0, "三轮里一次都没问尾号：%r" % asks)
        self.assertGreater(name_hits, 0, "三轮里一次都没问姓名：%r" % asks)

    def test_slot_asked_at_most_twice(self):
        s, _ = _run(["是我", "呃", "嗯", "呃", "嗯"], "slot-cap")
        for slot, n in s["slot_asks"].items():
            self.assertLessEqual(n, dialogue.SLOT_ASK_MAX,
                                 "槽位 %s 问了 %d 次，超过上限 %d" % (slot, n, dialogue.SLOT_ASK_MAX))

    def test_exhaust_then_finish(self):
        """两个槽位都问满还核不上 → 收尾（end_reason=identity_failed），不是无限追问。"""
        s, outs = _run(["是我", "呃", "嗯", "呃", "嗯", "呃"], "slot-exhaust")
        self.assertEqual("DONE", s["stage"])
        self.assertEqual("identity_failed", outs[4].get("end_reason"))
        self.assertEqual(0, s.get("writeback_version", 0), "身份没核上不能回写工单")

    def test_wrong_tail_asks_name_not_tail_again(self):
        """报了尾号但对不上，下一句该问姓名——再问一遍尾号等于没听见用户说话。"""
        _, outs = _run(["订单尾号1234"], "slot-wrong-tail")
        self.assertNotIn("尾号", outs[0]["reply_text"],
                         "刚报过尾号又追问尾号：%r" % outs[0]["reply_text"])
        self.assertIn("姓名", outs[0]["reply_text"])


class NoAdvanceTest(unittest.TestCase):
    """身份没确认，绝不往下走。"""

    def test_never_reach_offer_or_writeback(self):
        s, outs = _run(["是我", "我忘了尾号了", "真不记得", "呃", "嗯"], "slot-noadvance")
        stages = [o["next_stage"] for o in outs]
        self.assertNotIn("OFFER", stages, "身份未确认却进了方案沟通：%r" % stages)
        self.assertNotIn("WRITEBACK", stages, "身份未确认却进了回写：%r" % stages)
        self.assertNotIn("NEED", stages, "身份未确认却进了需求确认：%r" % stages)
        self.assertFalse(s["slots"]["identity_confirmed"])

    def test_no_personal_info_before_identity(self):
        """核验没通过时，回复里不能出现订单金额、地址这类个人信息。"""
        _, outs = _run(["是我", "我忘了尾号了", "真不记得"], "slot-privacy")
        joined = " ".join(o["reply_text"] for o in outs)
        for leak in ("订单号", "金额", "地址", "收件人"):
            self.assertNotIn(leak, joined, "核验未通过就提到了 %s：%r" % (leak, joined))


class SlotValueTest(unittest.TestCase):
    """槽位取值：覆盖、不叠加、核上就停。"""

    def test_tail_confirms(self):
        s, _ = _run(["是我，尾号8866"], "slot-tail-ok")
        self.assertTrue(s["slots"]["identity_confirmed"])
        self.assertEqual("NEED", s["stage"])
        self.assertEqual("tail", s["slots"].get("identity_via"))

    def test_name_confirms(self):
        s, _ = _run(["我是张伟"], "slot-name-ok")
        self.assertTrue(s["slots"]["identity_confirmed"])
        self.assertEqual("name", s["slots"].get("identity_via"))

    def test_correction_overwrites(self):
        """C03：先说姓李，再说姓张，最后说全名——只保留最后那个。"""
        s, _ = _run(["我姓李", "不对，我姓张", "对，张伟"], "slot-correct")
        self.assertEqual("张伟", s["identity_slots"]["name"])
        self.assertNotIn("李", s["identity_slots"]["name"])
        self.assertTrue(s["slots"]["identity_confirmed"])

    def test_both_slots_given_one_turn(self):
        """N05：姓名与尾号一次说完，不该再问任何一句。"""
        s, outs = _run(["我是张伟，尾号8866"], "slot-both")
        self.assertEqual("NEED", s["stage"])
        self.assertEqual(0, sum(s["slot_asks"].values()), "信息齐了还在追问：%r" % s["slot_asks"])

    def test_name_extraction_rejects_noise(self):
        """抽姓名不能把"真不记得""是我"当成人名。"""
        for text in ("真不记得", "是我", "我忘了尾号了", "不记得了", "他不在"):
            self.assertEqual("", dialogue._extract_name(text),
                             "「%s」被误抽成姓名：%r" % (text, dialogue._extract_name(text)))

    def test_name_extraction_positive(self):
        for text, want in (("我是张伟", "张伟"), ("我叫张三", "张三"),
                           ("我姓王", "王"), ("对，张伟", "张伟"),
                           ("张先生你好", "张")):
            self.assertEqual(want, dialogue._extract_name(text), "「%s」抽取失败" % text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
