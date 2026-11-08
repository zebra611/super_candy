# -*- coding: utf-8 -*-
"""官方话术填槽的离线自测（不联网、不调模型）。

测的是 `modules/official_script.py`：把 `config/kb_pop.json` 里带【】的官方模板，
用本通档案的真值填成能念的句子。三条底线：
    ① 填满的句子不许再出现 【】/XX（语音里出现"括号括号""叉叉"就是事故）；
    ② 填不满的必须**判失败**，不许硬凑（宁可不说，也不能念半句）；
    ③ 京麦驳回不配电话话术（官方口径是站内指引）。

运行：python scripts/test_official_script.py [-v]
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import official_script as os_                     # noqa: E402


def _app(**kw):
    base = {"application_id": "T1", "brand_name": "麦冬", "shop_name": "麦冬官方旗舰店",
            "company_name": "浙江省麦冬商贸有限公司", "category_l1": "家用电器",
            "category_l2": "厨房小电", "category_l3": "电饭煲",
            "required_materials": ["品牌授权书", "逐级授权链路证明", "授权方营业执照"],
            "reject_category": "品牌-授权", "reject_code": "KB-POP-010",
            "close_mode": "电话", "call_status": "待外呼"}
    base.update(kw)
    return base


class BlankSlots(unittest.TestCase):
    """空【】按上下文填。"""

    def test_brand_slot(self):
        text, miss = os_.render_script("您申请的【】品牌，因授权链不完整。", _app())
        self.assertEqual(miss, [])
        self.assertIn("麦冬品牌", text)

    def test_status_slots_are_opposite(self):
        """"状态为【】故不符合"与"提供【】状态的"，两处不能填同一个词。"""
        tpl = "因商标状态为【】故不符合要求，需您提供【】状态的【】类商标"
        text, miss = os_.render_script(tpl, _app())
        self.assertEqual(miss, [])
        self.assertIn("状态为待审故不符合要求", text)
        self.assertIn("提供已注册状态", text)

    def test_materials_take_turns(self):
        """一句里两个材料空槽，不能填成同一项。"""
        tpl = "因授权书内容不完整，缺少【】、【】，请补齐后重新提交"
        text, miss = os_.render_script(tpl, _app())
        self.assertEqual(miss, [])
        self.assertIn("品牌授权书", text)
        self.assertIn("逐级授权链路证明", text)

    def test_chain_two_ends(self):
        tpl = "因授权链不完整，缺少【】到【】的授权关系"
        text, miss = os_.render_script(tpl, _app())
        self.assertEqual(miss, [])
        self.assertIn("品牌方到贵公司", text)

    def test_shop_name_rule(self):
        tpl = "因您的店铺名称【】不符合命名规则，需您协助修改【】"
        text, miss = os_.render_script(tpl, _app())
        self.assertEqual(miss, [])
        self.assertIn("麦冬官方旗舰店", text)

    def test_company_status(self):
        tpl = "因【XXX公司】在国家企业信用信息公示系统查询状态为：【】"
        text, miss = os_.render_script(tpl, _app())
        self.assertEqual(miss, [])
        self.assertIn("浙江省麦冬商贸有限公司", text)
        self.assertIn("经营异常", text)


class Notices(unittest.TestCase):
    """具名占位：有的是槽位，有的是"要说的话"。"""

    def test_option_takes_first(self):
        tpl = "因【授权是否限制经营选择错误/类目超出授权范围】，需您协助【】"
        text, miss = os_.render_script(tpl, _app())
        self.assertEqual(miss, [])
        self.assertIn("授权是否限制经营选择错误", text)
        self.assertNotIn("/", text)

    def test_named_instruction_keeps_wording(self):
        tpl = "因【授权时间】已失效，需您协助操作【重新开具授权书】"
        text, miss = os_.render_script(tpl, _app())
        self.assertEqual(miss, [])
        self.assertIn("因授权时间已失效", text)
        self.assertIn("协助操作重新开具授权书", text)

    def test_sku_template_is_not_fillable(self):
        """档案里没有 SKU，这类模板必须判失败，而不是编一个 SKU。"""
        tpl = "您申请的SKU【】，因授权书内容不完整，缺少【】、【】"
        text, miss = os_.render_script(tpl, _app())
        self.assertTrue(miss, "缺字段的模板不该被判成可播")
        for mark in ("【", "】"):
            self.assertIn(mark, text)      # 原文保留，不删不猜


class ScriptSelection(unittest.TestCase):
    """挑哪一条话术。"""

    def test_jingmai_has_no_phone_script(self):
        got = os_.script_for(_app(close_mode="京麦", reject_code="KB-POP-003"))
        self.assertIsNone(got, "京麦驳回走站内指引，不该配电话话术")

    def test_phone_app_always_has_readable_script(self):
        pool = _load_pool()
        for app in pool:
            got = os_.script_for(app)
            if app.get("close_mode") == "京麦" or app.get("call_status") == "无需外呼":
                self.assertIsNone(got, "%s 是京麦单却配了话术" % app["application_id"])
                continue
            self.assertIsNotNone(got, "%s 是电话单却说不出话术" % app["application_id"])
            for mark in ("【", "】", "XX"):
                self.assertNotIn(mark, got["script"],
                                 "%s 的话术里还有 %s" % (app["application_id"], mark))

    def test_s_series_override(self):
        """S 系列原文含 XX，播报时必须换成补齐版。"""
        text, miss = os_.render_script(
            "您可以在国家商标网中查询商标状态，在商标状态变为XX后重新提交申请",
            _app(), "KB-POP-S03")
        self.assertEqual(miss, [])
        self.assertNotIn("XX", text)
        self.assertIn("已注册", text)

    def test_dialogue_text_strips_greeting_and_hotline(self):
        s = ("尊敬的商家伙伴您好，我是京东资质审核专员，您申请的麦冬品牌，"
             "因授权链不完整，请补齐后重新提交，如有疑问您可以电话联系：4006180092，"
             "我们将竭诚为您服务！")
        self.assertEqual(os_.dialogue_text(s), "您申请的麦冬品牌，因授权链不完整，请补齐后重新提交")

    def test_unfillable_ids_never_chosen(self):
        """026/067/068 要用档案没有的字段，候选池里必须已经排掉。"""
        for cat in ("品牌-授权", "商品与行业资质", "新增产品线"):
            ids = os_.candidates(cat)
            for bad in os_.UNFILLABLE_IDS:
                self.assertNotIn(bad, ids, "%s 的候选池里不该有 %s" % (cat, bad))


def _load_pool():
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = []
    for name in ("applications.json", "applications_catalog.json"):
        with open(os.path.join(root, "config", "mock", name), encoding="utf-8") as f:
            out.extend(json.load(f).get("items") or [])
    return out


if __name__ == "__main__":
    unittest.main(verbosity=2)
