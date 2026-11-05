# -*- coding: utf-8 -*-
"""意图置信度分级 的离线自测（T1）。

跑法：python3 scripts/test_intent.py

锁三件事，都是"改词表时最容易悄悄破坏、而评测集又照不出来"的：

    1. **含糊输入必须被判成含糊**。"你说是就是吧""嗯……""随便"这类话，
       判成同意会回写一个用户没给过的结论，判成诉求会让客服自说自话。
    2. **明确输入不能被误伤**。"同意""不需要""我要退款"不能被含糊规则吃掉——
       宁可漏判含糊，也不能把明确诉求当成没听懂反复追问。
    3. **含糊不推进阶段**。追问只解决"没听清"，不解决"没同意"；
       阶段一动，后面所有判断都建立在一个没有的前提上。

末尾会打一行歧义样本准确率，直接可以贴进 docs/20 的"意图识别准确率"。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import dialogue  # noqa: E402

# ---------------------------------------------------------------------------
# 10 条歧义样本：全部来自真实外呼里"听起来像回应、其实没信息"的话。
# 期望值三列：intent（压平后的意图名）/ vague（是否含糊）/ 说明
# ---------------------------------------------------------------------------
AMBIGUOUS = [
    ("你说是就是吧", "unclear", True, "把决定推回给客服，等于没表态"),
    ("嗯……", "unclear", True, "拖长音的嗯，是犹豫不是同意"),
    ("随便吧", "unclear", True, "敷衍，最容易被误判成同意的一种"),
    ("我也不太清楚", "unclear", True, "包含式含糊，整句匹配对不上"),
    ("啊？", "ask", True, "带问号但只是没听清，不是具体问题"),
    ("你定吧", "unclear", True, "把选择权推走"),
    ("可以考虑一下", "unclear", True, "外呼里最典型的软拒绝，绝不能当同意"),
    ("行吧行吧", "unclear", True, "连说两遍是不耐烦，不是同意"),
    ("不要了不要了", "no", False, "重复的拒绝仍是明确拒绝"),
    ("嗯，那就这样吧", "yes", False, "带具体内容的接受，算明确同意"),
]

# 明确输入：这些不能被含糊规则吃掉
CLEAR = [
    ("同意", "yes"),
    ("不需要", "no"),
    ("我要退款", "unknown"),          # 没有关键词，但是有效诉求，必须能继续推进
    ("用着还行", "unknown"),
    ("你们天天打电话烦不烦", "angry"),
    ("我要转人工", "handoff"),
    ("是我，尾号8866", "info"),
]


def _flags(text):
    return dialogue.classify(text)


class AmbiguousTest(unittest.TestCase):
    """歧义样本：判对率必须 ≥8/10。"""

    def test_ambiguous_samples(self):
        ok = 0
        detail = []
        for text, want_intent, want_vague, why in AMBIGUOUS:
            f = _flags(text)
            got_intent = dialogue._intent_name(f)
            got_vague = bool(f["vague"])
            hit = (got_intent == want_intent) and (got_vague == want_vague)
            ok += 1 if hit else 0
            detail.append("%s %-14r 期望(%s/%s) 实际(%s/%s)"
                          % ("✓" if hit else "✗", text, want_intent, want_vague,
                             got_intent, got_vague))
        rate = ok / float(len(AMBIGUOUS))
        print("\n[歧义样本] %d/%d 判对，准确率 %.0f%%" % (ok, len(AMBIGUOUS), rate * 100))
        for line in detail:
            print("   " + line)
        self.assertGreaterEqual(rate, 0.8, "歧义判对率低于 80%%：\n" + "\n".join(detail))

    def test_vague_has_low_conf(self):
        """含糊的置信度必须显著低于明确输入，否则阈值没有意义。"""
        for text, _, want_vague, _ in AMBIGUOUS:
            if not want_vague:
                continue
            conf = _flags(text)["conf"]
            self.assertLess(conf, dialogue.YES_MIN_CONF,
                            "「%s」被判成含糊，但 conf=%.2f 高于同意阈值" % (text, conf))

    def test_evidence_is_traceable(self):
        """每个置信度都要能说出"从哪来"。没有证据来源的分数，事后无法复现。"""
        allowed = {"exact", "contains", "digit", "weak", "vague", "filler", "none",
                   "deny", "hangup", "handoff", "angry", "no", "yes", "ask", "info"}
        for text, _, _, _ in AMBIGUOUS:
            ev = _flags(text)["evidence"]
            self.assertIn(ev, allowed, "「%s」的 evidence 值不在白名单：%r" % (text, ev))


class ClearInputTest(unittest.TestCase):
    """明确输入不能被含糊规则误伤。"""

    def test_clear_intents(self):
        for text, want in CLEAR:
            got = dialogue._intent_name(_flags(text))
            self.assertEqual(want, got, "「%s」应判为 %s，实际 %s" % (text, want, got))

    def test_clear_never_vague(self):
        for text, _ in CLEAR:
            self.assertFalse(_flags(text)["vague"], "「%s」是明确输入，不该判成含糊" % text)

    def test_bare_filler_is_not_consent(self):
        """单个"嗯"不是同意——这是 docs/02 的硬规矩。"""
        for text in ("嗯", "嗯嗯", "哦", "啊"):
            self.assertFalse(_flags(text)["yes"], "「%s」不能判成同意" % text)

    def test_weak_yes_below_threshold(self):
        """弱肯定要给低分，但仍然是 yes（阶段推进要靠它，落库要靠阈值拦）。"""
        for text in ("好吧", "应该可以", "差不多"):
            f = _flags(text)
            self.assertTrue(f["yes"], "「%s」应识别为肯定" % text)
            self.assertLess(f["conf"], dialogue.YES_MIN_CONF,
                            "「%s」是弱肯定，置信度不能过阈值" % text)

    def test_negation_wins(self):
        """含否定词的句子不能被判成同意——"不要"里含"要"是经典坑。"""
        for text in ("不要了", "不需要", "不用了", "不行吧"):
            self.assertFalse(_flags(text)["yes"], "「%s」不能判成同意" % text)


class StageBehaviorTest(unittest.TestCase):
    """含糊不推进阶段：这是 T1 的行为验收，不只是分类器验收。"""

    def _to_need(self):
        s = dialogue.new_session("t-intent-need")
        out = dialogue.respond(s, "是我，尾号8866")
        self.assertEqual("NEED", s["stage"], "身份核验后应进入 NEED，实际 %s" % s["stage"])
        return s, out

    def _to_offer(self):
        s, _ = self._to_need()
        dialogue.respond(s, "用着还行")
        self.assertEqual("OFFER", s["stage"], "表达诉求后应进入 OFFER，实际 %s" % s["stage"])
        return s

    def _to_confirm(self):
        s = self._to_offer()
        dialogue.respond(s, "行，你说这个权益吧")
        self.assertEqual("CONFIRM", s["stage"], "讲完方案应进入 CONFIRM，实际 %s" % s["stage"])
        return s

    def test_need_vague_does_not_advance(self):
        s, _ = self._to_need()
        out = dialogue.respond(s, "你说是就是吧")
        self.assertEqual("NEED", s["stage"], "含糊输入不得推进到 OFFER")
        self.assertEqual("unclear", out["intent"])
        self.assertEqual("NEED", out["next_stage"])
        self.assertFalse(s["slots"].get("need"), "含糊输入不能被记成诉求：%r" % s["slots"].get("need"))

    def test_offer_vague_does_not_advance(self):
        s = self._to_offer()
        out = dialogue.respond(s, "随便吧")
        self.assertEqual("OFFER", s["stage"], "含糊输入不得推进到 CONFIRM")
        self.assertEqual("unclear", out["intent"])

    def test_event_carries_conf(self):
        """每一轮回复都要带 intent_conf——这是 T17 出数的唯一来源。"""
        for text in ("同意", "嗯", "你说是就是吧"):
            s = dialogue.new_session("t-intent-conf")
            out = dialogue.respond(s, text)
            self.assertIn("intent_conf", out, "「%s」这一轮的事件没有 intent_conf" % text)
            self.assertGreaterEqual(out["intent_conf"], 0.0)
            self.assertLessEqual(out["intent_conf"], 1.0)

    def test_clarify_then_handoff(self):
        """追问有上限：问到第 3 次就转人工，不把用户晾在同一句话上。"""
        s, _ = self._to_need()
        texts = []
        for _ in range(dialogue.CLARIFY_MAX):
            out = dialogue.respond(s, "嗯……")
            texts.append(out["reply_text"])
            self.assertEqual("NEED", s["stage"])
        self.assertEqual(len(set(texts)), len(texts), "两次追问必须是不同的话：%r" % texts)
        out = dialogue.respond(s, "嗯……")
        self.assertEqual("DONE", s["stage"], "追问超限应转人工收尾")
        self.assertEqual("handoff_unclear", out.get("end_reason"))
        self.assertFalse(s.get("writeback_version"), "含糊追问到转人工，不能顺手回写一张工单")

    def test_weak_yes_not_written_back(self):
        """CONFIRM 阶段"好吧"不算同意：先追问，不给业务侧一个错结论。"""
        s = self._to_confirm()
        out = dialogue.respond(s, "好吧")
        self.assertEqual("CONFIRM", s["stage"], "弱肯定不得直接回写")
        self.assertFalse(s.get("writeback_version"), "弱肯定不得触发回写")
        self.assertIsNone(out.get("tool_call"))
        # 追问之后明确同意，仍要能落库
        dialogue.respond(s, "同意")
        self.assertTrue(s.get("writeback_version"), "明确同意后必须回写")

    def test_accept_still_closes(self):
        """不能因为加了闸门，把正常同意也拦掉——闭环是基础门槛。"""
        s = self._to_confirm()
        out = dialogue.respond(s, "同意")
        self.assertEqual("DONE", s["stage"])
        self.assertTrue(s.get("writeback_version"))
        self.assertEqual(True, s["slots"]["consent"])
        self.assertEqual("create_ticket", (out.get("tool_call") or {}).get("name"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
