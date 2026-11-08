"""官方 QA 接进对话的离线自测（不联网、不调模型）。

为什么单独一个文件：`verify.py` 的证据验的是"整条链路没坏"，
而这里要验的是一组**只在特定输入下才走到的边界**——
占位答案不播、同一条不重复播、单通限额、提问不算表态。
这些路径平时跑一遍通话都碰不到，散在 verify 里会让证据序号越来越难读。

和 `test_tools.py` 的分工：
    test_tools.py 验的是**适配器**（lookup_qa 能不能查到、命中门槛对不对）；
    这个文件验的是**对话层**（查到了之后，这句话该不该说、说到哪一步为止）。

运行：
    python3 scripts/test_qa_dialogue.py
    python3 scripts/test_qa_dialogue.py -v      # 看每条用例名

约束：
    - 纯标准库 unittest，不引 pytest（项目零依赖）；
    - 默认场景（POP 资审驳回）跑，不另设 APP_SCENE——QA 命中与场景无关；
    - 不读密钥、不发请求，跑批不产生费用。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import dialogue                                     # noqa: E402

# 官方 27 条里能逐字播的几条（answer 不含 XX/【】），用作正向样本。
Q_HF003 = "在职证明有哪些"                  # QA-HF-003
Q_HF004 = "店铺名怎么更改"                  # QA-HF-004
Q_HF006 = "什么情况下需要提供银行流水"      # QA-HF-006
Q_HF007 = "发票必须要盖开店公司公章吗"      # QA-HF-007
Q_HF008 = "商标持有人要和品牌库持有人一样吗"  # QA-HF-008
Q_HF009 = "从专柜拿货算是追溯到品牌持有人吗"  # QA-HF-009
# 答案里带未填占位的（"在商标状态变为XX后重新提交"）。
Q_TS003 = "我在哪可以查询到这个商标状态"     # QA-TS-003
# 谁都不该答的闲聊。
Q_OFFTOPIC = "今天天气不错"


def _session(stage="OFFER", **slots):
    """造一个已经核过身份、停在指定阶段的会话。"""
    s = dialogue.new_session("qa-%s" % stage)
    s["stage"] = stage
    s["slots"]["identity_confirmed"] = True
    s["slots"].update(slots)
    return s


def _app():
    """一条最小可用的申请单档案（NEED 阶段要用）。"""
    return {"application_id": "A2026001",
            "application_desc": "某某旗舰店 品牌入驻",
            "reason": "商标状态不符合要求",
            "materials": "注册商标证、授权书",
            "close_mode": "电话", "call_status": "待外呼"}


class OfficialQaInDialogue(unittest.TestCase):
    """官方原话在对话里的行为。"""

    def test_offer_answers_with_official_wording(self):
        """OFFER 阶段问官方问法 → 逐字播官方答案，阶段不动。"""
        s = _session("OFFER")
        out = dialogue.respond(s, Q_HF003)
        self.assertEqual(out["intent"], "qa")
        self.assertEqual(out["next_stage"], "OFFER", "答问题不该推进流程")
        self.assertIn("企微认证", out["reply_text"])
        self.assertEqual(out["qa"]["id"], "QA-HF-003")
        self.assertEqual(out["qa"]["source"], "高频QA")

    def test_official_answer_is_not_rewritten(self):
        """官方原话必须逐字播——措辞层开启时也不许改。"""
        s = _session("OFFER")
        with mock.patch.object(dialogue.nlg, "say",
                               side_effect=lambda sess, text, stage: "改写后的句子"):
            out = dialogue.respond(s, Q_HF004)
        self.assertNotIn("改写后的句子", out["reply_text"],
                         "官方原话被措辞层改写了，等于我们转述官方口径")
        self.assertIn("京麦后台", out["reply_text"])

    def test_need_prefers_question_over_application_detail(self):
        """NEED 阶段：问的是具体问题，就别念整段驳回详情。"""
        s = _session("NEED", application=_app())
        out = dialogue.respond(s, Q_HF007)
        self.assertEqual(out["qa"]["id"], "QA-HF-007")
        self.assertIn("公章", out["reply_text"])
        self.assertNotIn("驳回原因是", out["reply_text"],
                         "用户问 A，不能拿 B 的回答糊过去")

    def test_confirm_question_is_not_counted_as_ambiguous(self):
        """CONFIRM 阶段提问：先答题 + 重新要答复，且不计入含糊次数。"""
        s = _session("CONFIRM")
        out = dialogue.respond(s, Q_HF008)
        self.assertEqual(out["intent"], "qa")
        self.assertEqual(s["confirm_asks"], 0,
                         "提问不是含糊，不该消耗追问次数")
        self.assertEqual(out["next_stage"], "CONFIRM")
        self.assertIn("清楚了吗", out["reply_text"], "答完必须把确认问题放回去")

    def test_done_still_offers_hotline(self):
        """终态提问：先答，热线兜底不能丢。"""
        s = _session("DONE")
        out = dialogue.respond(s, Q_HF003)
        self.assertEqual(out["qa"]["id"], "QA-HF-003")
        self.assertIn("再见", out["reply_text"])
        self.assertIn("4006180092", out["reply_text"])

    def test_greeting_keeps_company_confirmation(self):
        """还没确认公司时提问：答了问题也要把"您是XX公司吗"问回去。"""
        s = _session("GREETING")
        s["outbound_target"] = {"company_name": "北京某某科技有限公司",
                                "company_short_name": "某某科技",
                                "contact_role": "负责人"}
        out = dialogue.respond(s, Q_HF003)
        self.assertIn("企微认证", out["reply_text"])
        self.assertIn("北京某某科技有限公司", out["reply_text"],
                      "赛题四段式②'确认公司'不能因为答了问题就省掉")
        self.assertFalse(s["company_confirmed"])


class OfficialQaGuards(unittest.TestCase):
    """不该播的时候，一条都不能播。"""

    def test_placeholder_answer_is_not_read(self):
        """答案里带 XX 的官方原话不播，且不能把 XX 念出去。"""
        s = _session("OFFER")
        out = dialogue.respond(s, Q_TS003)
        self.assertNotIn("XX", out["reply_text"])
        self.assertNotIn("【", out["reply_text"])
        self.assertIsNone(out.get("qa"))
        reasons = [(b.get("via"), b.get("reason"), b.get("id"))
                   for b in s["answer_blocked"]]
        self.assertIn(("qa", "placeholder", "QA-TS-003"), reasons,
                      "挡住了但要留下记录，否则没法解释'为什么这句没答'")

    def test_kb_template_with_slots_is_not_read(self):
        """kb 里带【】槽位的模板不许原样念（TTS 会念成左右括号）。"""
        s = _session("OFFER")
        out = dialogue.respond(s, "你们能和品牌方确认吗")
        self.assertNotIn("【", out["reply_text"])
        self.assertNotIn("】", out["reply_text"])
        self.assertTrue(any(b.get("via") == "kb" for b in s["answer_blocked"]))

    def test_unrelated_question_is_not_invented(self):
        """闲聊不命中：宁可说"确认不到"，也不许套一条官方答案。"""
        s = _session("OFFER")
        out = dialogue.respond(s, Q_OFFTOPIC)
        self.assertIsNone(out.get("qa"))
        for frag in ("企微认证", "京麦后台", "公章", "银行流水"):
            self.assertNotIn(frag, out["reply_text"])

    def test_same_answer_is_not_repeated(self):
        """同一个问题连问两次：第二次不再念同一句话。

        注意这里判的是**跨来源**去重：`kb_pop.json` 里有几条条目和官方 QA
        逐字相同（当时照原文抄进去的），只按 qid 去重的话，第二次会从 kb 那边
        把同一句话原样再念一遍——用户听到的是一字不差的复读，去重等于没做。
        """
        s = _session("OFFER")
        first = dialogue.respond(s, Q_HF004)
        second = dialogue.respond(s, Q_HF004)
        self.assertEqual(first["qa"]["id"], "QA-HF-004")
        self.assertIn("咨询单", first["reply_text"])
        self.assertIsNone(second.get("qa"), "官方原话不该播第二遍")
        self.assertNotIn("咨询单客服创建", second["reply_text"],
                         "同一句话（哪怕来自 kb）也不该逐字复读")
        self.assertEqual(s["qa_answered"].count("QA-HF-004"), 1)
        self.assertTrue(any(b.get("reason") == "repeated" for b in s["answer_blocked"]))

    def test_single_call_quota(self):
        """单通电话最多播 4 条官方原话，第 5 条起退回原路径。"""
        s = _session("OFFER")
        for q in (Q_HF003, Q_HF004, Q_HF006, Q_HF007):
            self.assertEqual(dialogue.respond(s, q)["intent"], "qa")
        out = dialogue.respond(s, Q_HF008)
        self.assertIsNone(out.get("qa"))
        self.assertEqual(s["qa_count"], dialogue.QA_MAX_ANSWERS)

    def test_adapter_failure_degrades_to_old_path(self):
        """查询接口抛异常时不许把整通电话带崩，只能退回原路径。"""
        s = _session("OFFER")
        with mock.patch.object(dialogue.tools, "lookup_qa",
                               side_effect=RuntimeError("upstream down")):
            out = dialogue.respond(s, Q_HF003)
        self.assertTrue(out["reply_text"].strip())
        self.assertIsNone(out.get("qa"))


class QaHelpers(unittest.TestCase):
    """两个纯函数的边界，单独钉一下。"""

    def test_qa_lookup_returns_none_on_empty(self):
        self.assertIsNone(dialogue._qa_lookup(_session("OFFER"), ""))
        self.assertIsNone(dialogue._qa_lookup(_session("OFFER"), "   "))

    def test_unreadable_marks(self):
        self.assertTrue(dialogue._unreadable("变为XX后重新提交"))
        self.assertTrue(dialogue._unreadable("您申请的【】品牌"))
        self.assertFalse(dialogue._unreadable("可在单据中授权书位置上传"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
