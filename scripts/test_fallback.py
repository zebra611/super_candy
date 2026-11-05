# -*- coding: utf-8 -*-
"""兜底话术细分 + 转人工 的离线自测。

跑法：python3 scripts/test_fallback.py

这里锁两类行为，都是"改代码时最容易悄悄破坏、线上又最难发现"的：

    1. 六种失败各有各的说法。退回一句"麻烦您再说一遍"，
       用户重复三遍听同一句，体验比挂断还差。
    2. 转人工不能被"投诉"误触发。投诉要致歉承接后继续解决问题，
       一句"帮您转人工"就把电话挂掉，等于把投诉变成挂断。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_loader  # noqa: E402
from modules import dialogue  # noqa: E402

REASONS = ("asr", "error", "timeout", "business", "tool", "writeback")


class FallbackTextTest(unittest.TestCase):
    """兜底话术：六种原因必须说六句不同的话。"""

    def test_all_reasons_have_text(self):
        for r in REASONS:
            text = dialogue.fallback_for(r)
            self.assertTrue(text and text.strip(), "%s 没有兜底话术" % r)

    def test_reasons_are_distinct(self):
        seen = {}
        for r in REASONS:
            text = dialogue.fallback_for(r)
            self.assertNotIn(text, seen,
                             "%s 和 %s 说了同一句话：%s" % (r, seen.get(text), text))
            seen[text] = r

    def test_unknown_reason_falls_back_to_error(self):
        self.assertEqual(dialogue.fallback_for("error"), dialogue.fallback_for("不存在的原因"))

    def test_key_mapping(self):
        self.assertEqual("reply_asr", dialogue.fallback_key("asr"))
        self.assertEqual("reply_fail", dialogue.fallback_key("writeback"))
        self.assertEqual("reply", dialogue.fallback_key("error"))
        # 未知原因也必须有键，不能抛 KeyError
        self.assertEqual("reply", dialogue.fallback_key("xxx"))

    def test_writeback_uses_existing_config(self):
        """回写失败那句话早就写在 stages.WRITEBACK 里，不能因为新机制就丢。"""
        want = config_loader.stage_config("WRITEBACK").get("reply_fail")
        self.assertTrue(want, "配置里没有 WRITEBACK.reply_fail")
        self.assertEqual(want, dialogue.fallback_for("writeback"))

    def test_config_overrides_default(self):
        """配置里写了的，必须盖过内置默认——这是 4 号改话术的唯一入口。"""
        want = config_loader.fallback_text("reply_timeout")
        if not want:
            self.skipTest("当前配置没写 fallback.reply_timeout，跳过覆盖测试")
        self.assertEqual(want, dialogue.fallback_for("timeout"))

    def test_legacy_fallback_text(self):
        """app.py:525 仍在调 fallback_text('reply_fail')，不能失效。"""
        self.assertEqual(dialogue.fallback_for("writeback"),
                         dialogue.fallback_text("reply_fail"))
        # 不认识的键返回空串而不是抛异常：调用方要能判断"没配"
        self.assertEqual("", dialogue.fallback_text("一个不存在的键"))

    def test_safe_fallback_does_not_advance(self):
        s = dialogue.new_session("fb")
        s["stage"] = "OFFER"
        out = dialogue.safe_fallback(s)
        self.assertEqual("error", out["intent"])
        self.assertEqual("OFFER", out["stage"], "兜底不该改变当前阶段")
        self.assertEqual("OFFER", out["next_stage"], "兜底不该推进状态机")

    def test_safe_fallback_by_reason(self):
        s = dialogue.new_session("fb2")
        self.assertEqual(dialogue.fallback_for("timeout"),
                         dialogue.safe_fallback(s, "timeout")["reply_text"])


class HandoffTest(unittest.TestCase):
    """转人工：要能被明确要求触发，不能被拒绝/投诉误触发。"""

    def _flags(self, text):
        return dialogue._classify(text)

    def test_positive(self):
        for text in ["帮我转人工", "我要找人工客服", "转客服", "叫你们领导来"]:
            self.assertTrue(self._flags(text)["handoff"], "「%s」应当识别为要求转人工" % text)

    def test_negated_not_handoff(self):
        """"我不需要转人工"里的"转人工"是拒绝，不是要求。"""
        self.assertFalse(self._flags("我不需要转人工")["handoff"])

    def test_complaint_is_not_handoff(self):
        """投诉是情绪，不是转接请求。误判会把投诉变成挂断。"""
        self.assertFalse(self._flags("我要投诉你们")["handoff"])
        self.assertTrue(self._flags("我要投诉你们")["angry"], "投诉应当被识别为情绪")

    def test_respond_ends_call_without_writeback(self):
        s = dialogue.new_session("ho")
        out = dialogue.respond(s, "别说了，帮我转人工")
        self.assertEqual("handoff", out["intent"])
        self.assertEqual("DONE", out["next_stage"])
        self.assertEqual("handoff", out.get("end_reason"))
        self.assertIsNone(out.get("tool_call"), "转人工不该替用户下业务结论")
        self.assertTrue(out["reply_text"].strip())

    def test_no_writeback_after_handoff(self):
        """转人工之后用户再说"同意"，也不能补一张工单——结论该由人工给。"""
        s = dialogue.new_session("ho2")
        dialogue.respond(s, "帮我转人工")
        out = dialogue.respond(s, "同意")
        self.assertIsNone(out.get("tool_call"))
        self.assertFalse(s.get("closed"), "转人工后不应标记为闭环")

    def test_not_triggered_in_done(self):
        """已经挂断的会话里再提转人工，不再重复收尾。"""
        s = dialogue.new_session("ho3")
        dialogue.respond(s, "帮我转人工")
        out = dialogue.respond(s, "帮我转人工")
        self.assertNotEqual("handoff", out.get("end_reason"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
