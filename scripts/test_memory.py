# -*- coding: utf-8 -*-
"""长会话记忆（T4）的离线自测。

跑法：python3 scripts/test_memory.py

锁四件事：

    1. **第 2 轮说的话，第 18 轮还记得**。这是考察点"长会话记忆"最直接的证据，
       也是旧实现最明显的缺口（slots 的 need 会被后续轮次覆盖）。
    2. **身份没核验不许复述**。记忆里有姓名和订单号，在对面的身份还没确认时
       念出来就是泄漏个人信息——这条比"记得住"更重要。
    3. **记忆不能变成合规漏洞**。记忆会进 prompt、也可能被复述，
       一条带禁语的记忆等于把违规词存进了上下文。
    4. **记忆不带来可感知的延迟**。T4 要求轮次增长的增量 ≤50ms。
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_loader  # noqa: E402
from modules import dialogue  # noqa: E402

# 20 轮剧本：第 2 轮说诉求，中间十几轮在问同一个问题（停在 OFFER 不推进），
# 第 18 轮问"我刚才说的什么"。
FILLER = "这个权益要花钱吗"


def _long_call(sid="mem-long"):
    s = dialogue.new_session(sid)
    dialogue.respond(s, "是我，8866")
    dialogue.respond(s, "我买的那个包装被压坏了")     # 第 2 轮：诉求
    for _ in range(15):
        dialogue.respond(s, FILLER)
    return s


class RecallTest(unittest.TestCase):
    """记不记得住。"""

    def test_recall_at_turn_18(self):
        s = _long_call()
        self.assertGreaterEqual(s["turn"], 17, "剧本没跑到 18 轮")
        out = dialogue.respond(s, "你还记得我刚才说的问题吗")
        self.assertEqual("recall", out["intent"], "第 18 轮没走记忆回溯")
        self.assertIn("包装", out["reply_text"], "回溯的内容不对：%r" % out["reply_text"])
        self.assertEqual(18, s["turn"])

    def test_recall_does_not_advance(self):
        """回溯是回答问题，不该改变流程。"""
        s = _long_call("mem-noadv")
        before = s["stage"]
        out = dialogue.respond(s, "我刚才说的是什么来着")
        self.assertEqual(before, out["next_stage"], "回溯改变了阶段：%s -> %s" % (before, out["next_stage"]))

    def test_recall_requires_identity(self):
        """身份没核验不许复述——这是隐私红线。"""
        s = dialogue.new_session("mem-privacy")
        dialogue.respond(s, "是我")                     # 停在 IDENTITY
        self.assertFalse(s["slots"]["identity_confirmed"])
        out = dialogue.respond(s, "你还记得我刚才说的问题吗")
        self.assertNotEqual("recall", out["intent"], "身份未核验却复述了记忆")
        self.assertNotIn("包装", out["reply_text"])

    def test_first_need_not_overwritten(self):
        """诉求只记第一次：后面改口或"不需要"都不能覆盖它。"""
        s = _long_call("mem-first")
        s2 = dialogue.respond(s, "其实我更关心运费")      # 第二个诉求
        self.assertNotEqual("recall", s2["intent"])
        facts = s["memory"]["facts"]
        self.assertEqual("我买的那个包装被压坏了", facts["need"])
        # 再问一次，仍然是第一句
        out = dialogue.respond(s, "我刚才说的什么")
        self.assertIn("包装", out["reply_text"])


class MemoryHygieneTest(unittest.TestCase):
    """记忆的卫生：长度、禁语、结构。"""

    def test_recent_is_capped(self):
        s = _long_call("mem-cap")
        self.assertEqual(dialogue.MEMORY_RECENT, len(s["memory"]["recent"]),
                         "最近轮次没有被裁剪")

    def test_forbidden_words_filtered(self):
        """用户自己说了禁语，记忆里不能原样留着。"""
        s = dialogue.new_session("mem-forbidden")
        bad = config_loader.forbidden_words()[0]
        dialogue.respond(s, "是我，8866")
        dialogue.respond(s, "你能%s吗" % bad)
        dump = repr(s["memory"])
        self.assertNotIn(bad, dump, "禁语进了记忆：%r" % dump)

    def test_forbidden_words_not_in_brief(self):
        s = dialogue.new_session("mem-forbidden2")
        bad = config_loader.forbidden_words()[0]
        dialogue.respond(s, "是我，8866")
        dialogue.respond(s, "你们%s" % bad)
        self.assertNotIn(bad, dialogue.memory_brief(s))

    def test_brief_shape(self):
        s = _long_call("mem-brief")
        brief = dialogue.memory_brief(s)
        self.assertIn("诉求：", brief)
        self.assertIn("客户：", brief)
        self.assertNotIn("{", brief, "brief 不该是 JSON——模型会照着格式输出结构化内容")

    def test_empty_brief_is_empty_string(self):
        s = dialogue.new_session("mem-empty")
        self.assertEqual("", dialogue.memory_brief(s))


class MemoryCostTest(unittest.TestCase):
    """记忆的代价：不能带来可感知延迟。"""

    def test_increment_under_budget(self):
        s = _long_call("mem-cost")
        n = 2000
        t0 = time.perf_counter()
        for i in range(n):
            dialogue._remember(s, "这个权益要花钱吗", {"stage": "OFFER", "intent": "ask"})
            dialogue.memory_brief(s)
        per_call = (time.perf_counter() - t0) / n * 1000.0
        print("\n[记忆开销] 每轮 %.3f ms（预算 50ms）" % per_call)
        self.assertLess(per_call, 50.0, "记忆每轮耗时 %.3f ms，超过 50ms 预算" % per_call)
        # 更贴近实际的判断：它应该是微秒级的，50ms 是给整条链路留的余量
        self.assertLess(per_call, 5.0, "记忆每轮 %.3f ms，已经不是一个可忽略的开销了" % per_call)

    def test_growth_is_bounded(self):
        """跑满 60 轮，记忆大小不随轮次线性增长。"""
        s = dialogue.new_session("mem-growth")
        for _ in range(60):
            dialogue._remember(s, "很长的一句话" * 3, {"stage": "OFFER", "intent": "ask"})
        self.assertLessEqual(len(s["memory"]["recent"]), dialogue.MEMORY_RECENT)
        facts = s["memory"]["facts"]
        self.assertLessEqual(len(facts), 5, "事实表无界增长：%r" % list(facts))


if __name__ == "__main__":
    unittest.main(verbosity=1)
