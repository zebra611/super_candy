# -*- coding: utf-8 -*-
"""知识库引擎离线自测（不联网、不依赖场景配置）。

跑法：python3 scripts/test_kb.py

覆盖三类：
    1. 出厂内容（config/kb.json）能不能过校验、能不能命中；
    2. 引擎规则：有效期、优先级、最短词长、优先级并列、归一化；
    3. 校验器本身有没有用——故意造错的库，必须被拦下来。

第 3 类容易被忽略：只测"好数据能过"，校验器永远显示通过，
等于没测。这里每条坏数据都断言"必须报错"。
"""

import json
import os
import sys
import tempfile
import unittest

# 这套用例测的是**售后回访场景**（台词是"用着还行""权益""上门""订单"，
# 知识库条目也是售后那批）。默认场景已换成赛题 POP 资审，不钉死场景就会全红。
# 必须钉在 import config_loader **之前**——它的配置路径在 import 时就解析好了。
os.environ.setdefault("APP_SCENE", "service_outbound")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import kb  # noqa: E402


def _write(items, meta=None):
    """把条目写进临时文件，返回路径。用完由 tearDown 里的列表清理。"""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"_meta": meta or {}, "items": items}, f, ensure_ascii=False)
    return path


def _item(**kw):
    base = {
        "id": "KB-X-001",
        "category": "test",
        "patterns": ["测试词"],
        "answer": "这是一个测试答案。",
        "source_id": "资料/测试.md#1",
        "valid_from": None,
        "valid_to": None,
        "forbidden": [],
        "priority": 0,
    }
    base.update(kw)
    return base


class ShippedContentTest(unittest.TestCase):
    """出厂内容：config/kb.json 必须干净、可用。"""

    def setUp(self):
        kb.set_path(None)
        self.kb = kb.load(force=True)

    def test_file_exists_and_not_empty(self):
        self.assertTrue(os.path.exists(self.kb.path), "找不到知识库文件：%s" % self.kb.path)
        self.assertFalse(self.kb.is_empty(), "知识库是空的")
        self.assertGreaterEqual(len(self.kb.items), 20, "条目数少于 20，覆盖面不够")

    def test_validate_clean(self):
        problems = self.kb.validate()
        self.assertEqual([], problems, "出厂内容有校验问题：\n" + "\n".join(problems))

    def test_every_item_has_source(self):
        """溯源性是维度 3 的评分点，不能有例外。"""
        for it in self.kb.items:
            self.assertTrue(it.get("source_id"), "%s 没有 source_id" % it.get("id"))

    def test_positive_matches(self):
        cases = {
            "这个要花钱吗": "KB-FEE-001",
            "这个权益能用多久": "KB-VALIDITY-001",
            "是不是免费的": "KB-FEE-001",
            "什么时候生效": "KB-TRIGGER-001",
            "保什么呀": "KB-COVERAGE-001",
            "怎么报修": "KB-REPAIR-001",
            "我不想要了能取消吗": "KB-CANCEL-001",
            "能退钱吗": "KB-REFUND-001",
            "你们怎么知道我的号码": "KB-PRIVACY-001",
            "为什么问我这些": "KB-IDENTITY-001",
            "你们是不是骗子": "KB-SALES-001",
            "能开发票吗": "KB-INVOICE-001",
            "我是不是买过了": "KB-DUPLICATE-001",
            "帮我转人工": "KB-TRANSFER-001",
            "我要投诉你们": "KB-COMPLAINT-001",
            "到期了怎么办": "KB-RENEW-001",
            "你们是哪个公司": "KB-OWNER-001",
            "你们几点上班": "KB-HOURS-001",
            "多久能上门": "KB-ONSITE-001",
            "我现在能用吗": "KB-URGENT-001",
            "能不能只要一项": "KB-PARTIAL-001",
            "最近有活动吗": "KB-PROMO-001",
        }
        for text, want in cases.items():
            hit = self.kb.match(text)
            self.assertIsNotNone(hit, "「%s」应当命中 %s，实际没命中" % (text, want))
            self.assertEqual(want, hit["id"], "「%s」应命中 %s，实际 %s" % (text, want, hit["id"]))

    def test_negative_no_match(self):
        """答不上来的必须答不上来——编造是外呼里最严重的事故。"""
        for text in ["你们公司股票能买吗", "那你们老板是谁", "今天天气怎么样",
                     "嗯", "好的", "不需要", "", None]:
            self.assertIsNone(self.kb.match(text), "「%s」不该命中任何条目" % text)

    def test_placeholder_flagged(self):
        """占位内容必须在 meta 里标出来，否则没人会记得替换。"""
        st = self.kb.stats()
        if st["placeholder"]:
            self.assertTrue(st["version"], "占位内容也要有版本号")


class EngineRuleTest(unittest.TestCase):
    """引擎规则：这些是最容易在改代码时被悄悄破坏的行为。"""

    def setUp(self):
        self._paths = []

    def tearDown(self):
        for p in self._paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        kb.set_path(None)

    def _load(self, items, meta=None):
        p = _write(items, meta)
        self._paths.append(p)
        kb.set_path(p)
        return kb.load(force=True)

    def test_validity_window(self):
        from datetime import date
        k = self._load([
            _item(id="OLD", patterns=["老活动"], valid_from="2020-01-01", valid_to="2020-12-31"),
            _item(id="NEW", patterns=["新活动"], valid_from="2030-01-01", valid_to=None),
            _item(id="NOW", patterns=["当前活动"], valid_from="2026-01-01", valid_to="2026-12-31"),
        ])
        today = date(2026, 11, 5)
        self.assertIsNone(k.match("老活动怎么参加", today=today), "过期条目不该命中")
        self.assertIsNone(k.match("新活动怎么参加", today=today), "未生效条目不该命中")
        self.assertEqual("NOW", k.match("当前活动怎么参加", today=today)["id"])

    def test_expired_reason_visible(self):
        """过期要说清是过期，不能显示成"没命中关键词"——否则调内容时找不到原因。"""
        from datetime import date
        k = self._load([_item(id="OLD", patterns=["老活动"],
                              valid_from="2020-01-01", valid_to="2020-12-31")])
        rows = k.explain("老活动", today=date(2026, 11, 5))
        self.assertEqual("expired", rows[0]["reason"])

    def test_min_len_blocks_single_char(self):
        """单字关键词不许命中：一个"退"字会把"退一步说"也吃掉。"""
        k = self._load([_item(id="ONE", patterns=["退"], answer="退答案。")])
        self.assertIsNone(k.match("我要退"))

    def test_longer_pattern_wins(self):
        k = self._load([
            _item(id="SHORT", patterns=["多久"], answer="短。"),
            _item(id="LONG", patterns=["多久能上门"], answer="长。"),
        ])
        self.assertEqual("LONG", k.match("你们多久能上门")["id"])
        self.assertEqual("SHORT", k.match("能用多久")["id"])

    def test_priority_breaks_tie(self):
        k = self._load([
            _item(id="LOW", patterns=["同一个词"], priority=0),
            _item(id="HIGH", patterns=["同一个词"], priority=9),
        ])
        self.assertEqual("HIGH", k.match("同一个词")["id"])

    def test_normalization(self):
        """ASR 标点不稳定、用户可能打空格、全角半角混用，都要能命中。"""
        k = self._load([_item(id="N", patterns=["要花钱吗"], answer="免费。")])
        for text in ["这个 要花钱吗？", "这个要花钱吗", "这个要花钱吗!", "这个要花钱吗。"]:
            self.assertIsNotNone(k.match(text), "归一化失败：%r" % text)

    def test_explain_top_n(self):
        k = self._load([
            _item(id="A", patterns=["问题甲"], answer="甲。"),
            _item(id="B", patterns=["问题乙"], answer="乙。"),
        ])
        rows = k.explain("问题甲和问题乙", top=2)
        self.assertEqual(2, len(rows))

    def test_missing_file_degrades(self):
        kb.set_path("/tmp/__definitely_missing_kb__.json")
        self.assertFalse(kb.enabled())
        self.assertIsNone(kb.match("随便问点什么"))
        self.assertEqual([], kb.validate())


class ValidatorTest(unittest.TestCase):
    """校验器必须真的能拦下坏数据。"""

    def setUp(self):
        self._paths = []

    def tearDown(self):
        for p in self._paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        kb.set_path(None)

    def _problems(self, items):
        p = _write(items)
        self._paths.append(p)
        return kb.load(p, force=True).validate()

    def test_missing_required(self):
        bad = _item()
        bad.pop("source_id")
        self.assertTrue(any("source_id" in x for x in self._problems([bad])))

    def test_duplicate_id(self):
        self.assertTrue(any("id 重复" in x for x in self._problems([_item(), _item()])))

    def test_empty_patterns(self):
        self.assertTrue(any("patterns 为空" in x
                            for x in self._problems([_item(patterns=["", "  "])])))

    def test_forbidden_word_in_answer(self):
        got = self._problems([_item(answer="我们可以保证不会再出问题。")])
        self.assertTrue(any("禁用语" in x for x in got), got)

    def test_item_level_forbidden(self):
        got = self._problems([_item(answer="这个可以报销。", forbidden=["报销"])])
        self.assertTrue(any("禁用语" in x for x in got), got)

    def test_inverted_dates(self):
        got = self._problems([_item(valid_from="2026-12-31", valid_to="2026-01-01")])
        self.assertTrue(any("早于" in x for x in got), got)

    def test_duplicate_keyword(self):
        got = self._problems([
            _item(id="A", patterns=["同一个词"]),
            _item(id="B", patterns=["同一个词"]),
        ])
        self.assertTrue(any("共用" in x for x in got), got)

    def test_short_word_capture_warning(self):
        """「多久」被「多久能上门」包含 → 必须给出抢占告警。"""
        p = _write([
            _item(id="A", patterns=["多久"], answer="A。"),
            _item(id="B", patterns=["多久能上门"], answer="B。"),
        ])
        self._paths.append(p)
        warns = kb.load(p, force=True).conflicts()
        self.assertTrue(any("多久" in w for w in warns), warns)


class PlaceholderTest(unittest.TestCase):
    """C2：官方话术的【】占位符——先原样念，后槽位填充。

    官方案例里的占位符有两套写法（空的【】与【XX】【XXX】【XXXX】），
    这里两种都要覆盖，且都要能说清"哪几个还没填上"。
    """

    CASE = "您申请的【XX】品牌资质，因【驳回原因】未通过，请在【】天内补充。"

    def test_no_slots_keeps_verbatim(self):
        text, missing = kb.render(self.CASE)
        self.assertEqual(self.CASE, text, "没给槽位时必须原样念，不能删也不能猜")
        self.assertEqual(["XX", "驳回原因", "（空占位符）"], missing)

    def test_filled_slots_replaced(self):
        text, missing = kb.render(self.CASE, {"XX": "米家", "驳回原因": "授权书过期"})
        self.assertIn("米家品牌资质", text)
        self.assertIn("授权书过期", text)
        self.assertNotIn("【XX】", text)
        self.assertEqual(["（空占位符）"], missing, "只该剩空的那个")

    def test_slot_key_is_placeholder_text(self):
        """槽位键就是括号里的原文——这条不写清，接 C1 时必然对不上。"""
        text, _ = kb.render(self.CASE, {"XX": "米家"})
        self.assertNotIn("米家", kb.render(self.CASE, {"brand": "米家"})[0],
                         "键名对不上时不该生效")

    def test_empty_slot_value_does_not_replace(self):
        """传了键但值是空串 → 视为没填，不能渲染出一个空洞。"""
        text, missing = kb.render("申请【XX】品牌", {"XX": ""})
        self.assertIn("【XX】", text)
        self.assertIn("XX", missing)

    def test_plain_answer_unchanged(self):
        """没有占位符的普通答案，行为必须与接入前逐字节一致。"""
        text, missing = kb.render("这份权益是免费的。", {"XX": "米家"})
        self.assertEqual("这份权益是免费的。", text)
        self.assertEqual([], missing)

    def test_match_carries_missing(self):
        """match() 的返回里要带 missing，调用方不用自己再解析一遍。"""
        hit = kb.match("这个要花钱吗")
        self.assertIsNotNone(hit)
        self.assertEqual([], hit["missing"], "出厂知识库不该有没填的占位符")


if __name__ == "__main__":
    unittest.main(verbosity=2)
