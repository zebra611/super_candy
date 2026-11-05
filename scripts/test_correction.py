# -*- coding: utf-8 -*-
"""改口与指代消解（T3）的离线自测。

跑法：python3 scripts/test_correction.py

锁四件事：

    1. **一句话里说两个值，信最后一个**。"尾号1234，不对是4321" 是改口最常见的
       语序，旧代码取第一个，会把错的号记下来。
    2. **改口是覆盖，不是叠加**。槽位里只能有一个值，轨迹里要能看到变化过程。
    3. **改口要改在同一张工单上**。版本递增、工单号不变，业务侧只有一条结论。
    4. **只有指代没有内容的输入不能当诉求**。"就是那个"记进 need 字段，
       回写出去就是一个"那个"，等于没写。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import dialogue  # noqa: E402


def _run(turns, sid):
    s = dialogue.new_session(sid)
    outs = [dialogue.respond(s, t) for t in turns]
    return s, outs


class SingleUtteranceTest(unittest.TestCase):
    """同一句里多个值时取哪个。"""

    def test_last_value_wins(self):
        f = dialogue.classify("尾号1234，不对是4321")
        self.assertEqual("4321", f["digits"], "同句两个尾号应取最后一个（改口语序）")

    def test_single_value_unchanged(self):
        self.assertEqual("8866", dialogue.classify("是我，尾号8866")["digits"])

    def test_full_phone_not_mistaken_for_tail(self):
        """8 位连写不能截出中间 4 位当尾号——那是凭空造数据。"""
        self.assertEqual("", dialogue.classify("我号码12345678")["digits"])

    def test_tail_with_punctuation(self):
        self.assertEqual("8866", dialogue.classify("尾号8866。")["digits"])


class IdentityCorrectionTest(unittest.TestCase):
    """身份槽位的改口。"""

    def test_c03_trail(self):
        """C03：李 → 张 → 张伟，最终只留张伟，轨迹里有两次改口。"""
        s, _ = _run(["我姓李", "不对，我姓张", "对，张伟"], "corr-c03")
        self.assertEqual("张伟", s["identity_slots"]["name"], "最终值必须是最后说的那个")
        pairs = [(c["from"], c["to"]) for c in s["corrections"] if c["slot"] == "name"]
        self.assertIn(("李", "张"), pairs, "漏了 李→张 这次改口：%r" % pairs)
        self.assertIn(("张", "张伟"), pairs, "漏了 张→张伟 这次改口：%r" % pairs)

    def test_c03_no_accumulation(self):
        """槽位里只能有一个值——不能变成"李张"或同时记两个姓名。

        注意：查不到的值会被清空（这是刻意的，避免拿错值反复打接口），
        所以这里查的是**提交历史**与"值里没有拼接"两件事。
        """
        s, _ = _run(["我姓李", "不对，我姓张"], "corr-c03b")
        self.assertEqual(["李", "张"], s["slot_history"]["name"], "提交历史不对")
        for v in s["slot_history"]["name"]:
            self.assertIn(v, ("李", "张"), "槽位值被拼接了：%r" % v)
        self.assertLessEqual(len(s["identity_slots"]["name"]), 1,
                            "槽位里存了多个字符：%r" % s["identity_slots"]["name"])

    def test_c04_tail_overwritten(self):
        """C04：报错尾号后自行更正，旧值不得残留。"""
        s, _ = _run(["订单尾号1234", "啊不对，是4321"], "corr-c04")
        self.assertNotEqual("1234", s["identity_slots"].get("tail"), "旧尾号还在槽位里")
        tails = s["slot_history"]["tail"]
        self.assertEqual(["1234", "4321"], tails, "提交历史不对：%r" % tails)
        pairs = [(c["from"], c["to"]) for c in s["corrections"] if c["slot"] == "tail"]
        self.assertIn(("1234", "4321"), pairs, "没有记下尾号改口：%r" % pairs)

    def test_correction_has_turn(self):
        """轨迹要带轮次，否则没法回放到"哪一句改的口"。"""
        s, _ = _run(["我姓李", "不对，我姓张"], "corr-turn")
        turns = [c["turn"] for c in s["corrections"]]
        self.assertTrue(all(t > 0 for t in turns), "轮次没记：%r" % turns)

    def test_correction_synced_into_slots(self):
        """轨迹也要进 slots，因为 /api/turn 只回传 slots。"""
        s, _ = _run(["我姓李", "不对，我姓张"], "corr-sync")
        self.assertEqual(s["corrections"], s["slots"]["corrections"])


class ConsentCorrectionTest(unittest.TestCase):
    """业务结论的改口：同工单、新版本。"""

    def _closed_then_changed(self, sid):
        return _run(["是我，8866", "用着还行", "可以", "同意", "等等我说错了，我不要了"], sid)

    def test_consent_correction_trail(self):
        s, _ = self._closed_then_changed("corr-consent")
        pairs = [(c["from"], c["to"]) for c in s["corrections"] if c["slot"] == "consent"]
        self.assertIn((True, False), pairs, "consent 的改口没记：%r" % pairs)

    def test_same_ticket_new_version(self):
        s, outs = self._closed_then_changed("corr-ticket")
        first = outs[3]["tool_call"]["args"]
        second = outs[4]["tool_call"]["args"]
        self.assertEqual(1, first["version"])
        self.assertEqual(2, second["version"], "改口应推进版本号")
        self.assertEqual(first["args_key"] if "args_key" in first else first["session_id"],
                         second["session_id"], "工单主键必须同一个 session")

    def test_writeback_payload_carries_trail(self):
        _, outs = self._closed_then_changed("corr-payload")
        content = outs[4]["tool_call"]["args"]["content"]
        self.assertIn("corrections", content, "改口后的回写没带轨迹")
        self.assertTrue(any(c["slot"] == "consent" for c in content["corrections"]))

    def test_clean_writeback_has_no_trail(self):
        """没改过口的工单，payload 与以前逐字节一致——不给下游添字段。"""
        _, outs = _run(["是我，8866", "用着还行", "可以", "同意"], "corr-clean")
        content = outs[3]["tool_call"]["args"]["content"]
        self.assertNotIn("corrections", content)


class ReferenceTest(unittest.TestCase):
    """指代：只有指代没有内容 → 追问；带名词 → 照常处理。"""

    def _to_need(self, sid):
        s, _ = _run(["是我，8866"], sid)
        return s

    def test_reference_only_not_recorded_as_need(self):
        s = self._to_need("ref-need")
        out = dialogue.respond(s, "就是那个")
        self.assertEqual("NEED", s["stage"], "只有指代不能推进")
        self.assertFalse(s["slots"].get("need"), "把'那个'记成诉求了：%r" % s["slots"].get("need"))
        self.assertEqual("unclear", out["intent"])

    def test_reference_with_noun_advances(self):
        s = self._to_need("ref-noun")
        dialogue.respond(s, "你刚说的那个权益")
        self.assertEqual("OFFER", s["stage"], "带名词的指代是有效诉求")

    def test_plain_question_not_treated_as_reference(self):
        s = self._to_need("ref-question")
        dialogue.respond(s, "这个订单到哪了")
        self.assertEqual("NEED", s["stage"], "提问应留在 NEED 回答，不是追问")


if __name__ == "__main__":
    unittest.main(verbosity=1)
