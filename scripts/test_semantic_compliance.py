"""L4 语义级合规的验收测试：证明句式模板层不是多余的。

跑法：python3 scripts/test_semantic_compliance.py

为什么这组测试重要：
    L4 的验收标准是"至少 5 条换说法样本能被判违规"。如果探针本身能被字面词表
    抓到，就说明不了模板层有价值；如果抓不到，又说明模板没起作用。
    所以这里同时卡两个数：字面层必须 0 命中，模板层必须 ≥5。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import eval as E          # noqa: E402

PROBES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "eval", "probes_semantic.json")


def _replies(text):
    return [{"stage": "", "next": "", "text": text}]


def load_probes():
    with open(PROBES, "r", encoding="utf-8") as f:
        return json.load(f)["probes"]


class ProbeSetTest(unittest.TestCase):
    def test_literal_layer_misses_everything(self):
        """字面词表必须一条都抓不到——否则证明不了模板层的价值。"""
        for p in load_probes():
            got = E._check_categories(_replies(p["text"]))
            self.assertFalse(got, "%s 被字面词表抓到了：%s" % (p["id"], got))

    def test_template_layer_catches_at_least_half(self):
        """L4 验收线：至少 5 条换说法样本被判违规。"""
        hit = 0
        for p in load_probes():
            got = E._check_templates(_replies(p["text"]))
            if got:
                hit += 1
                self.assertIn(p["expect_category"], got,
                              "%s 命中了但类别不对：%s" % (p["id"], got))
        self.assertGreaterEqual(hit, 5, "模板层只抓到 %d 条，低于验收线 5" % hit)

    def test_negation_is_exempted(self):
        """否定语境不是承诺：这条错了，合规护栏会把最守规矩的话判成违规。"""
        for text in ("我不能给您保证有效", "不是最后一天，您别急", "这个没法保证回本"):
            self.assertFalse(E._check_templates(_replies(text)), "误伤：%s" % text)

    def test_normal_talk_has_no_false_positive(self):
        """正常话术不能被误判——合规误报比漏报更伤。"""
        normal = ("这份权益是免费的，不需要您额外付费。",
                  "您可以简单回我一句同意或者不需要就行。",
                  "我这边查得有点慢，稍后让专员给您回电说明，可以吗？",
                  "您如果改变主意，随时告诉我，我给您补登记。")
        for text in normal:
            self.assertFalse(E._check_templates(_replies(text)), "误报：%s" % text)


class ReportIntegrationTest(unittest.TestCase):
    def test_hits_are_structured(self):
        """命中明细必须是结构化字段，不能是一句自然语言备注。"""
        got = E._check_templates(_replies("我保证您三个月就回本"))
        item = got["false_promise"][0]
        self.assertEqual(item["rule"], "template:guarantee_profit")
        self.assertEqual(item["source"], "template")
        self.assertIn("word", item)

    def test_template_rules_are_substantive(self):
        """模板规则少于 3 条，"语义级"就名不副实；这条防止有人把规则删空。"""
        self.assertGreaterEqual(len(E._TEMPLATE_RULES), 3)
        for rule in E._TEMPLATE_RULES:
            self.assertIn("rule", rule)
            self.assertIn("category", rule)
            self.assertIsNotNone(rule["regex"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
