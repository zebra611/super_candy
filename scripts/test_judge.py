"""归因器与逐句反馈的判据一致性（11-13）。

为什么专门建一个文件：这两处判据曾经各写一份词表，注释里还写着"两边词表
必须一致"，实际上 feedback 收「尾号」、attribution 只收「后四位」。同一句

    尾号 0086，对上了。那这几样材料您清楚怎么准备吗？

在面板里算缺陷、在归因里算正常。自进化据此报了 5 条 `slot_loss`，全是假阳——
优化器差点被指去改一个不存在的毛病，而客服其实只是**复述确认**客户刚给的值。

这里守三件事：
  1. 确认句式不算"又要一遍"（两边都不算）；
  2. 真索要句两边都算（不能为了消假阳把真问题一起放过）；
  3. 客户整通不给信息时停在核身不算客服无进展；给过信息仍停着才要报。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import attribution, feedback  # noqa: E402


def _both(said, action="answer_tail"):
    """同一句话，两边各判一次，返回 (feedback 判 slot_loss, attribution 判 slot_loss)。"""
    turns = [{"user": {"text": "尾号0086", "action": action},
              "agent": {"reply_text": said, "stage": "CONFIRM"}}]
    fb = "slot_loss" in feedback.judge_turn(turns, 0)["codes"]
    at = any(d["code"] == attribution.AGENT_SLOT_LOSS
             for d in attribution.analyse_turns(turns))
    return fb, at


class TestAsksAgainIsOneJudge(unittest.TestCase):
    """一句话在两个面板里不能有两种结论。"""

    def test_confirm_echo_is_not_reask(self):
        """复述客户刚给的值 + 说"对上了"，是在确认，不是在要第二遍。"""
        for said in (
            "尾号 0086，对上了。那这几样材料您清楚怎么准备吗？",
            "尾号 0086，对上了。材料这块，您还有哪点不清楚？",
            "核对上了，感谢配合。暖时光品牌、家用电器类目没有通过审核。",
        ):
            fb, at = _both(said)
            self.assertFalse(fb, "feedback 把确认当成了索要：%s" % said)
            self.assertFalse(at, "attribution 把确认当成了索要：%s" % said)

    def test_real_reask_counts_on_both_sides(self):
        """真有"要"的动作时必须两边都判——否则假阳没了、假阴来了。"""
        for said in (
            "麻烦您再报一下后四位。",
            "需要您预留手机号的后四位，说四个数字就行。",
            "我这边没有核对上，麻烦您再确认一次，报一下申请时预留手机号的后四位。",
            "后四位是多少？",
            "请报一下尾号。",
        ):
            fb, at = _both(said)
            self.assertTrue(fb, "feedback 漏了真索要：%s" % said)
            self.assertTrue(at, "attribution 漏了真索要：%s" % said)

    def test_feedback_has_no_second_word_list(self):
        """feedback 不许再自己维护一份词表——引用同一个函数才算一致。

        这条是静态断言：行为测不出"两份词表碰巧都对"，只能扫源码。
        """
        import inspect
        src = inspect.getsource(feedback._asks_again)
        self.assertIn("asks_same_fact", src)
        self.assertNotIn("_RE_ASK", inspect.getsource(feedback),
                         "feedback 里又建了一份索要词表")


def _sess(acts, replies=None):
    """构造一段"全程停在核身"的会话。

    客服每句刻意不同：同一句连说四轮会被 `agent_loop` 判复读（那是真缺陷，
    不能拿来当对照），这里要测的只是"阶段没推进"这一条。
    """
    pool = replies or ["麻烦您报一下后四位。",
                       "那您说下申请单编号后六位也行。",
                       "或者报一下联系人姓名，我这边也能核对。",
                       "您方便的时候查一下当初留的号码。"]
    return {
        "turns": [{"user": {"text": "嗯", "action": a},
                   "agent": {"reply_text": pool[i % len(pool)], "stage": "IDENTITY"}}
                  for i, a in enumerate(acts)],
        "closed": False,
    }


class TestNoProgressNeedsCooperation(unittest.TestCase):
    """停在同一阶段，要分清是"客服问不明白"还是"客户就是不给"。"""

    def test_all_refuse_is_by_design(self):
        """客户 8 轮一直拒绝/记不住：客服给替代路径并收尾，不算客服无进展。"""
        s = _sess(["yes_company", "refuse_info", "refuse_info", "cant_remember",
                   "ack", "cant_remember", "cant_remember", "cant_remember"])
        d = attribution.analyse_session(s, {"goal": "refuse", "expect": {"closed": False}})
        codes = [x["code"] for x in d["defects"]]
        self.assertNotIn(attribution.AGENT_NO_PROGRESS, codes,
                         "客户整通没给信息，却被算成客服无进展：%r" % codes)
        self.assertNotEqual(d["fault"], "agent", "责任判成了客服：%r" % d["fault"])

    def test_gave_fact_but_stuck_is_agent(self):
        """客户报了尾号、客服还在原地打转——这条必须照旧报出来。

        豁免只该放过"客户不给"，不能连带把"客服接不住"也放过。
        """
        turns = [{"user": {"text": "嗯", "action": a},
                  "agent": {"reply_text": "好的，我记下了。", "stage": "IDENTITY"}}
                 for a in ("yes_company", "answer_tail", "answer_tail", "ask_why")]
        d = attribution.analyse_session({"turns": turns, "closed": False},
                                        {"goal": "confirm", "expect": {"closed": True}})
        codes = [x["code"] for x in d["defects"]]
        self.assertIn(attribution.AGENT_NO_PROGRESS, codes,
                      "客户给过信息、客服仍在原地，却漏报了：%r" % codes)
        self.assertEqual(d["fault"], "agent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
