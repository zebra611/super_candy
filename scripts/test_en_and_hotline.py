# -*- coding: utf-8 -*-
"""11-12 晚（20:07 / 20:09 / 20:10）三条需求回归：

    ① 英文不能半中半英 —— 除品牌名/主体名（专有名词）外全部英文；
    ② 所有收线句在"再见"之前必须给热线 4006180092；
    ③ 纯文字模式不许等 TTS（首字超 2 秒的耗时全在没人听的合成上）。

纪律：正向断言都配反向对照，避免"词表放宽了什么都能过"的假绿；
英文覆盖用**两个档案池的全部条目**逐条渲染，不是抽几条好看的数据。
"""
import io
import json
import os
import re
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader
from modules import dialogue as D, i18n

_ZH = re.compile(r"[\u4e00-\u9fff]")


def _apps():
    """两个档案池的全部条目（120 + 68），英文覆盖必须逐条过。"""
    out = []
    for name in ("applications.json", "applications_catalog.json"):
        path = os.path.join(ROOT, "config", "mock", name)
        with io.open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else (data.get("items") or [])
        out.extend(items)
    return out


def _proper_nouns(app):
    """允许保留中文的部分：品牌名 + 公司简称/店铺名/公司全称。"""
    vals = [app.get(k) for k in ("brand_name", "company_short_name",
                                 "shop_name", "company_name")]
    return [str(v).strip() for v in vals if str(v or "").strip()]


class EnglishOnlyTest(unittest.TestCase):
    """① 英文话术里不许漏出中文（专有名词除外）。"""

    def test_all_templates_render_english_for_every_application(self):
        apps = _apps()
        self.assertGreater(len(apps), 150, "两个档案池应该都读到了")
        bad = []
        for app in apps:
            for key, tpl in list(i18n.EN_REPLIES.items()) + list(i18n.EN_BY_STAGE.items()):
                text = i18n.fill_en(tpl, app)
                self.assertNotIn("{", text, "%s 占位符没填干净：%s" % (key, text))
                stripped = text
                for noun in _proper_nouns(app):
                    stripped = stripped.replace(noun, "")
                if _ZH.search(stripped):
                    bad.append((app.get("record_no") or app.get("application_id"),
                                key, text))
        self.assertEqual([], bad[:3], "英文里漏出中文（共 %d 条）" % len(bad))

    def test_reason_and_materials_are_translated(self):
        app = {"brand_name": "荃汀", "category_l1": "运动户外",
               "reject_reason": "授权方联系不上，需补充品牌授权书后重新提交",
               "required_materials": ["品牌授权书"], "company_short_name": "安食"}
        text = i18n.fill_en(i18n.EN_REPLIES["official_script"], app)
        self.assertIn("荃汀", text, "品牌名要保留（后台专有名词）")
        self.assertNotIn("品牌、", text, "不许再出现「XX品牌、XX类目」这种半成品")
        self.assertIn("Sports & Outdoors", text)
        self.assertIn("couldn't reach the authorizing party", text)
        self.assertIn("brand authorization letter", text)


    def test_application_id_does_not_switch_to_english(self):
        """反向对照：报申请单号不是"在说英语"，不能把整通切成英文。

        实测翻车：C5 四路径核身·申请单号那一轮，客户报 QZ202611060001，
        detect 看到两个拉丁字母就切英文，中文客户收到一句英文回复。
        """
        self.assertEqual(i18n.detect("QZ202611060001"), "zh")
        self.assertEqual(i18n.detect("SHOP20260101"), "zh")
        self.assertEqual(i18n.detect("yes"), "en", "真说英语仍要切英文")
        self.assertEqual(i18n.detect("ok, thanks"), "en")


class ClosingHotlineTest(unittest.TestCase):
    """② 所有收线路径：热线必须在"再见"之前。"""

    def test_every_closing_has_hotline_before_goodbye(self):
        cases = [
            ("闭环", ["是的", "0086", "清楚了清楚了"]),
            ("拒绝", ["不是，你打错了"]),
            ("挂断", ["行了别打了"]),
            ("转人工", ["我要找人工"]),
        ]
        prev = config_loader.scene_path()
        config_loader.set_scene("pop_reject")
        try:
            import app as A
            for label, turns in cases:
                sid = A.start_call()["session_id"]
                for t in turns:
                    out = A.handle_turn(sid, user_text=t, text_only=True)
                text = out.get("reply_text") or ""
                self.assertIn(D._HOTLINE, text, "%s 收线没有热线：%s" % (label, text))
                self.assertLess(text.index(D._HOTLINE), text.index("再见"),
                                "%s 热线必须在再见之前：%s" % (label, text))
                self.assertTrue(text.endswith("再见。"), "%s 仍应以再见收尾" % label)
                A.end_call(sid)
        finally:
            config_loader.set_scene(prev)

    def test_hotline_not_repeated_when_already_given(self):
        s = D.new_session("t-hotline-dup")
        once = "如有疑问，您也可以拨打京东商家热线 4006180092，感谢您的接听，再见。"
        out = D._reply(s, once, next_stage="DONE", intent="consent")
        text = out["reply_text"]
        # 措辞层会调整虚词（"您也可以"→"您可"），所以这里只守"号码只出现一次"，
        # 不逐字比对——逐字断言会变成测措辞层，而不是测这条护栏。
        self.assertEqual(text.count("4006180092"), 1, "已经给过热线就不要再加第二遍：%s" % text)
        self.assertTrue(text.endswith("再见。"))

    def test_mid_call_sentence_gets_no_hotline(self):
        """反向对照：中间轮次不挂热线，否则每轮念一遍电话号就是复读机。"""
        s = D.new_session("t-hotline-mid")
        mid = "您这单要补的是营业执照，在商家后台重新提交就行。"
        out = D._reply(s, mid, next_stage="OFFER", intent="ask")
        self.assertNotIn(D._HOTLINE, out["reply_text"])

    def test_english_closing_has_hotline(self):
        text = i18n.fill_en(i18n.EN_REPLIES["consent_checklist"], {})
        self.assertIn("4006180092", text)
        self.assertLess(text.index("4006180092"), text.index("Goodbye"))


class TextOnlyNoTtsTest(unittest.TestCase):
    """③ 纯文字模式不等 TTS：首字延时的最大一块在合成上。"""

    def test_text_only_skips_tts(self):
        prev = config_loader.scene_path()
        config_loader.set_scene("pop_reject")
        try:
            import app as A
            sid = A.start_call()["session_id"]
            t0 = time.time()
            out = A.handle_turn(sid, user_text="请问您是哪位", text_only=True)
            cost = (time.time() - t0) * 1000
            self.assertEqual(out["tts"].get("skipped"), "text_only")
            self.assertEqual(out["latency"]["tts_ms"], 0)
            self.assertLess(cost, 2000, "纯文字一轮不该超过 2 秒（实测 %.0fms）" % cost)
            A.end_call(sid)
        finally:
            config_loader.set_scene(prev)

    def test_voice_mode_still_synthesizes(self):
        """反向对照：语音模式不能因为上面那条改动就悄悄不合成。"""
        prev = config_loader.scene_path()
        config_loader.set_scene("pop_reject")
        try:
            import app as A
            sid = A.start_call()["session_id"]
            out = A.handle_turn(sid, user_text="请问您是哪位", text_only=False)
            self.assertNotEqual(out["tts"].get("skipped"), "text_only")
            A.end_call(sid)
        finally:
            config_loader.set_scene(prev)


if __name__ == "__main__":
    unittest.main()
