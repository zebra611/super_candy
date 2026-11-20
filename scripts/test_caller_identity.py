# -*- coding: utf-8 -*-
"""P4-1 负向测试：质疑来电身份/号码来源，必须正面答官方口径，不许答非所问。

钉住 docs/60 挂账 #4 / #5：
    「你哪来的我号码，怎么信你」以前落到 _stage_identity 的 ask 分支，
    被当成"问驳回原因"或直接"记下了等专员"——答非所问。
    多问题（身份质疑 + 业务问）以前也只答一半。

本测试：
    1. IDENTITY / NEED 阶段，号码来源质疑 → 回复含"官方/预留/核实热线"口径；
    2. 不落"记下了""等专员"这类含糊话、不答成驳回原因；
    3. 多问题（质疑 + 问驳回原因）→ 先正面答身份，并承诺核身后答业务；
    4. 守卫不推进阶段、不消耗身份追问额度（纯回答，不改流程）；
    5. 连问两轮不逐字复读。

运行：python scripts/test_caller_identity.py
"""

import os
import sys
import unittest

os.environ.setdefault("APP_SCENE", "pop_reject")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from modules import dialogue, tools  # noqa: E402


def _rec():
    hit = tools.query_application("QZ202611060001") or {}
    return hit.get("application")


def _session(stage="IDENTITY"):
    rec = _rec()
    s = dialogue.new_session("s-caller-" + stage)
    s["outbound_target"] = rec
    s["company_confirmed"] = True
    s["target_locked"] = True
    s["slots"]["application"] = rec
    s["stage"] = stage
    return s


def _reply_text(out):
    return out.get("reply_text") or ""


class CallerIdentityTest(unittest.TestCase):
    def setUp(self):
        try:
            tools._reset_for_test()
        except Exception:
            pass

    def _assert_official(self, text):
        # 官方口径三要素：官方身份、号码来源、可核实路径
        self.assertTrue(any(w in text for w in ("官方", "京东")),
                        "回复应正面确认官方身份：%s" % text)
        self.assertTrue(any(w in text for w in ("预留", "入驻时", "订单")),
                        "回复应说明号码来源：%s" % text)
        self.assertIn("4006180092", text, "回复应给可核实的官方热线")

    def test_identity_stage_source_question(self):
        """IDENTITY：'你哪来的我号码' → 官方口径，不含糊、不答业务。"""
        s = _session("IDENTITY")
        out = dialogue.respond(s, "你哪来的我号码？")
        text = _reply_text(out)
        self._assert_official(text)
        self.assertNotIn("记下了", text)
        self.assertNotIn("专员会", text)
        self.assertEqual(s["stage"], "IDENTITY", "守卫不应推进阶段")
        self.assertEqual(out.get("intent"), "caller_identity")

    def test_need_stage_legit_question(self):
        """NEED：'我怎么信你' → 官方口径。"""
        s = _session("NEED")
        out = dialogue.respond(s, "我怎么信你是京东的？")
        self._assert_official(_reply_text(out))
        self.assertEqual(out.get("intent"), "caller_identity")

    def test_source_question_variants(self):
        """多种口语问法都要打中。"""
        variants = [
            "你怎么知道我电话号码的",
            "你从哪儿弄到我这号的",
            "这电话哪来的",
            "你们怎么联系到我的",
        ]
        for v in variants:
            with self.subTest(v=v):
                s = _session("IDENTITY")
                out = dialogue.respond(s, v)
                self.assertEqual(out.get("intent"), "caller_identity",
                                 "问法 %s 应被识别为来电身份质疑" % v)

    def test_multiquestion_answers_identity_and_promises_business(self):
        """多问题（质疑 + 问驳回原因）：先答身份，再答应核身后答业务。"""
        s = _session("IDENTITY")
        out = dialogue.respond(s, "你哪来的我号码？我怎么信你，还有到底为什么驳回？")
        text = _reply_text(out)
        self._assert_official(text)
        # 承诺核身后讲业务（P4-1 的多问题分诊：先答身份，业务不丢）
        self.assertTrue(any(w in text for w in ("驳回原因", "要补的材料")),
                        "多问题里应答应核身后讲业务：%s" % text)
        self.assertTrue(any(w in text for w in ("核对完", "核对上", "报一下")),
                        "应回到核身流程：%s" % text)

    def test_guard_does_not_consume_identity_budget(self):
        """质疑来电不消耗身份追问额度——客户只是谨慎，不是核身失败。"""
        s = _session("IDENTITY")
        before_ask = int(s.get("identity_ask_n") or 0)
        before_result = int(s.get("identity_result_ask_n") or 0)
        dialogue.respond(s, "你哪来的号码？")
        self.assertEqual(int(s.get("identity_ask_n") or 0), before_ask)
        self.assertEqual(int(s.get("identity_result_ask_n") or 0), before_result)

    def test_two_rounds_not_verbatim_repeat(self):
        """连问两轮官方口径，两句话不能逐字相同。"""
        s = _session("IDENTITY")
        a = _reply_text(dialogue.respond(s, "你哪来的我号码？"))
        b = _reply_text(dialogue.respond(s, "你哪来的我号码？"))
        self.assertNotEqual(a, b, "连续两轮官方口径不应逐字复读")

    def test_real_ask_still_goes_to_original_branch(self):
        """不是身份质疑的普通提问仍走原分支——防止守卫写宽把正常问题吞掉。"""
        s = _session("IDENTITY")
        out = dialogue.respond(s, "为什么驳回？")
        self.assertNotEqual(out.get("intent"), "caller_identity",
                            "问驳回原因不该被来电身份守卫截走")


if __name__ == "__main__":
    unittest.main(verbosity=2)
