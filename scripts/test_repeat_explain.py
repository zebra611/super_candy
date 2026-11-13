# -*- coding: utf-8 -*-
"""11-12 三张截图的回归：重复复读 / 开场白直呼大名 / 收线挂尾巴。

三条各自有**反向对照**，防止"改了但没生效"的假绿：
  - 详细解释必须走冻结（对照组：非冻结 intent 会被改写层砍成半句）；
  - 收线不能挂"我放慢一点说"（对照组：非 DONE 的同类句子必须仍能加后缀，
    否则说明性格规则整体失效，断言本身测不到东西）；
  - 第二遍解释必须与第一遍不同，且不再复述原因与材料清单。

用真实档案字段（required_materials / reject_reason / brand_name / category_l1 /
recheck_deadline），不用自造的假档案名——假数据会把 {application_desc} 拼成
"商城和3C类目类目"，看起来像 bug，实际是测试数据造错了。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_loader
import modules.dialogue as D
import modules.persona as persona
import modules.tools as T

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FLAGS = {"deny": False, "hangup": False, "angry": False, "complain": False,
         "no": False, "yes": False, "ask": False, "handoff": False,
         "digits": "", "vague": False, "conf": 0, "evidence": "none"}


def _app():
    """取一条真实外呼档案，字段齐、可复算；取不到时用等价的最小档案兜底。"""
    a = (T.next_outbound_target().get("application") or {})
    if a.get("brand_name") and a.get("required_materials"):
        return a
    return {"brand_name": "暖时光", "category_l1": "家用电器",
            "reject_reason": "授权链路不完整，缺少品牌方到一级经销商的逐级授权文件",
            "required_materials": ["品牌授权书", "逐级授权链路证明", "授权方营业执照"],
            "recheck_deadline": "2026-10-05",
            "company_name": "北京市安食商贸有限公司",
            "company_short_name": "安食", "shop_name": "安食官方旗舰店",
            "contact_role": "法定代表人"}


def _session(stage, facts_spoken=True):
    s = D.new_session("t-repeat")
    s["slots"]["application"] = _app()
    s["stage"] = stage
    if facts_spoken:
        s["facts_spoken"] = True
    return s


class TestExplainInsteadOfRepeat(unittest.TestCase):
    """客户说没听清 / 别重复了：给增量解释，不再逐字复读同一段。"""

    def test_hearing_gap_gives_frozen_detailed_explanation(self):
        for stage in ("NEED", "OFFER"):
            o = D._re_explain(_session(stage), stage,
                              "你能再说一遍，我根本没听清你在说什么", FLAGS)
            self.assertIsNotNone(o, stage)
            self.assertEqual(o["intent"], "explain_detail")
            t = o["reply_text"]
            # 冻结后必须是完整的一段：事实 / 清单 / 步骤 / 结果口径 / 热线都在。
            for frag in ("没有通过审核", "要补的材料", "分三步",
                         "4006180092", "完整审核"):
                self.assertIn(frag, t, "%s 缺 %r" % (stage, frag))
            self.assertNotIn("{", t, "占位符没填干净")

    def test_concise_request_gives_short_version(self):
        for stage in ("NEED", "OFFER"):
            o = D._re_explain(_session(stage), stage, "你别再重复了", FLAGS)
            self.assertIsNotNone(o, stage)
            self.assertEqual(o["intent"], "hurry_short")
            self.assertIn("不重复了", o["reply_text"])
            self.assertIn("4006180092", o["reply_text"])
            self.assertLess(len(o["reply_text"]), 90)

    def test_second_explanation_changes_angle(self):
        """连说两次没听清，第二遍不能把原因和清单再念一遍。"""
        s = _session("NEED")
        t1 = D._re_explain(s, "NEED", "我根本没听清", FLAGS)["reply_text"]
        t2 = D._re_explain(s, "NEED", "你再说一遍，还是没听清", FLAGS)["reply_text"]
        self.assertNotEqual(t1, t2, "两遍逐字相同 = 仍在复读")
        self.assertIn("驳回原因", t1)
        self.assertNotIn("驳回原因", t2)
        self.assertNotIn("一样都不能少", t2)
        self.assertIn("换个讲法", t2)
        self.assertIn("4006180092", t2)
        self.assertEqual(s.get("explain_detail_n"), 2)

    def test_specific_new_question_is_not_hijacked(self):
        """收费 / 怎么提交这类有专门答案的问题，不能被抢成重述驳回原因。"""
        for text in ("这个收费吗", "在哪个入口提交", "补上就能通过吗"):
            self.assertIsNone(D._re_explain(_session("NEED"), "NEED", text, FLAGS), text)
            self.assertIsNone(D._re_explain(_session("OFFER"), "OFFER", text, FLAGS), text)

    def test_no_facts_spoken_yet_means_no_intervention(self):
        self.assertIsNone(D._re_explain(_session("NEED", False), "NEED",
                                        "你再说一遍", FLAGS))
        self.assertIsNone(D._re_explain(_session("OFFER", False), "OFFER",
                                        "别重复了", FLAGS))

    def test_unrelated_sentence_is_ignored(self):
        self.assertIsNone(D._re_explain(_session("NEED"), "NEED",
                                        "好的，我知道了", FLAGS))

    def test_explicit_consent_is_never_hijacked(self):
        """"我这边听不清**但同意了**"是同意，不是没听清。
        11-12 Q2 课程自检实测：这句被抢成解释 → 售后画像 EV17-20 闭环全掉。"""
        for text in ("先这样，我这边听不清但同意了", "没听清，不过办吧",
                     "听不清，算了不用了"):
            f = dict(FLAGS, **{k: v for k, v in D.classify(text).items()
                               if k in ("yes", "no", "hangup", "deny")})
            self.assertIsNone(D._re_explain(_session("OFFER"), "OFFER", text, f), text)

    def test_no_application_means_no_intervention(self):
        """售后/召回场景没有申请单档案：这里不该讲资质驳回原因。"""
        s = _session("OFFER")
        s["slots"]["application"] = {}
        self.assertIsNone(D._re_explain(s, "OFFER", "我根本没听清", FLAGS))
        s2 = _session("OFFER")
        s2["slots"]["application"] = None
        self.assertIsNone(D._re_explain(s2, "OFFER", "别再重复了", FLAGS))


class TestGreetingDoesNotPresumeIdentity(unittest.TestCase):
    """开场白只确认单位，不念工商全称、不预设职务。"""

    TGT = {"company_name": "新疆维吾尔自治区美孚商贸有限公司",
           "company_short_name": "美孚", "shop_name": "美孚官方旗舰店",
           "contact_role": "品牌授权经办人"}

    def test_no_role_and_no_long_company_name(self):
        g = config_loader.stage_config("GREETING")
        for key in ("reply", "reply_confirm_company", "reply_lang_barrier",
                    "reply_protest_next"):
            line = D._fill_outbound(g[key], self.TGT)
            self.assertNotIn("品牌授权经办人", line, key)
            self.assertNotIn("新疆维吾尔自治区", line, key)
            self.assertNotIn("{", line, key)
        self.assertIn("请问您是", D._fill_outbound(g["reply"], self.TGT))
        self.assertIn("美孚官方旗舰店", D._fill_outbound(g["reply"], self.TGT))

    def test_competition_ring2_still_satisfied(self):
        """赛题四段式②不能省：模板仍带 {company_name}，开场白仍带单位名。"""
        for p in ("config/scene.json", "config/scenes/pop_reject.json"):
            with open(os.path.join(ROOT, p), encoding="utf-8") as f:
                g = json.load(f)["stages"]["GREETING"]
            self.assertIn("{company_name}", g["reply"], p)
            self.assertIn("资质审核", g["reply"], p)
            self.assertNotIn("驳回", g["reply"], p)

    def test_two_scene_files_keep_new_templates_in_sync(self):
        keys = ("reply_application_detail_rich", "reply_application_detail_rich_2",
                "reply_application_detail_short")
        with open(os.path.join(ROOT, "config/scene.json"), encoding="utf-8") as f:
            a = json.load(f)
        with open(os.path.join(ROOT, "config/scenes/pop_reject.json"),
                  encoding="utf-8") as f:
            b = json.load(f)
        for stage in ("NEED", "OFFER"):
            for k in keys:
                self.assertTrue(a["stages"][stage].get(k), "%s %s 缺失" % (stage, k))
                self.assertEqual(a["stages"][stage][k], b["stages"][stage].get(k),
                                 "%s %s 两份配置不一致" % (stage, k))


class TestNoSlowDownTailOnClosing(unittest.TestCase):
    """再见之后不许再挂"我放慢一点说"。"""

    def test_done_closing_has_no_tail(self):
        """走真实 _stage_done 路径（不是手传 next_stage 的假绿写法）。"""
        s = D.new_session("t-done")
        s["persona_code"] = "hard_of_hearing"
        s["stage"] = "DONE"
        f = dict(FLAGS, ask=True)
        out = D._stage_done(s, "还有别的问题吗", f)
        self.assertEqual(out["intent"], "hotline_close")
        self.assertNotIn("放慢一点说", out["reply_text"])
        self.assertIn("4006180092", out["reply_text"])
        self.assertIn("再见", out["reply_text"])
        # 官方热线话术逐字播出，没被改写层换措辞
        self.assertIn("感谢您的接听", out["reply_text"])

    def test_control_group_still_gets_suffix(self):
        """反向对照：非 DONE 的同类告别句必须仍能加后缀，否则本测试是假绿。"""
        s = D.new_session("t-wb")
        s["persona_code"] = "hard_of_hearing"
        out = D._reply(s, "感谢您的配合，再见。",
                       next_stage="WRITEBACK", intent="consent")
        self.assertIn("放慢一点说", out["reply_text"])

    def test_persona_adapt_respects_closing_marker(self):
        s = D.new_session("t-adapt")
        s["persona_code"] = "hard_of_hearing"
        bye = "如有疑问拨 4006180092，感谢您的接听，再见。"
        out, applied = persona.adapt(s, bye, "WRITEBACK", freeze=False)
        self.assertNotIn("放慢一点说", out)
        self.assertFalse(applied)


if __name__ == "__main__":
    unittest.main(verbosity=2)
