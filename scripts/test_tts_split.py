# -*- coding: utf-8 -*-
"""TTS 分段回归（11-12 20:37）：长回复的首字不该被"首段太长"拖住。

背景（真机实测）：英文 415 字回复被切成 `[229, 48, 4, 48, 44, 42]`——
前 229 字里没有标点，旧逻辑直接把这 229 字当首段，首字 3507ms、整轮 8.5 秒。
修完之后首段 5 字，首字 380–630ms。

这里只测**切分本身**（纯函数、不联网），真机首字由 docs/52 的记录与
scripts/verify.py 的事件链负责。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modules import i18n, voice

_APP = {"brand_name": "暖时光", "category_l1": "家用电器", "company_short_name": "安食",
        "reject_reason": "授权链路不完整，缺少品牌方到一级经销商的逐级授权文件",
        "required_materials": ["品牌授权书", "逐级授权链路证明", "授权方营业执照"]}


class SplitForTtsTest(unittest.TestCase):

    def test_long_english_head_is_short(self):
        """首段必须短：英文长句过去会切出 229 字的首段。"""
        text = i18n.fill_en(i18n.EN_REPLIES["official_script"], _APP)
        parts = voice._split_for_tts(text)
        self.assertLessEqual(len(parts[0]), 16, "首段 %d 字，首字会被拖慢：%r"
                             % (len(parts[0]), parts[0]))

    def test_english_cuts_on_word_boundary(self):
        """不能把英文单词切成两半：首段与每段都应以空格或标点收尾。"""
        text = i18n.fill_en(i18n.EN_REPLIES["official_script"], _APP)
        parts = voice._split_for_tts(text)
        for i, p in enumerate(parts[:-1]):
            self.assertTrue(p.endswith((" ", "，", "。", "！", "？", "；", ",", ".", "!", "?", ";")),
                            "第 %d 段把单词切开了：%r" % (i, p[-12:]))

    def test_join_is_lossless(self):
        """拼接必须逐字还原，否则客户会听到漏字/重字。"""
        samples = [
            i18n.fill_en(i18n.EN_REPLIES["official_script"], _APP),
            i18n.fill_en(i18n.EN_REPLIES["identity_ok"], _APP),
            "核对上了，感谢配合。暖时光品牌、家用电器类目没有通过审核，原因是授权链路不完整，"
            "缺少品牌方到一级经销商的逐级授权文件，需要补充品牌授权书、逐级授权链路证明、授权方营业执照。",
            "Your application was not approved please provide the brand authorization letter now",
            "好的。",
            "嗯",
        ]
        for text in samples:
            parts = voice._split_for_tts(text)
            self.assertEqual("".join(parts), text.strip(), "拼不回去：%r" % (parts,))

    def test_segment_count_is_capped(self):
        """段数上限：请求数就是限流口径，切十几段会撞 429。"""
        text = " ".join(["Your application was not approved because of the authorization chain"] * 6)
        parts = voice._split_for_tts(text)
        self.assertLessEqual(len(parts), 12)
        self.assertGreater(len(parts), 1, "长句必须真的切开")

    def test_tiny_tail_is_merged(self):
        """碎段合并：不为「resubmit it.」这种尾巴单独开一次请求。"""
        text = i18n.fill_en(i18n.EN_REPLIES["official_script"], _APP)
        parts = voice._split_for_tts(text)
        for p in parts[1:]:
            self.assertGreater(len(p), 10, "尾部碎段没并回去：%r" % p)

    def test_chinese_unchanged(self):
        """中文没有空格，行为保持原样：按标点切、首段短。"""
        text = "核对上了，感谢配合。暖时光品牌、家用电器类目没有通过审核。"
        parts = voice._split_for_tts(text)
        self.assertEqual("".join(parts), text)
        self.assertLessEqual(len(parts[0]), 8)
        self.assertTrue(parts[0].endswith("，"))


if __name__ == "__main__":
    unittest.main()
