"""大模型接入层的离线自测：闸门必须真的拦得住，降级必须真的不炸。

**这个文件不联网。** 所有"模型会怎么回"都由假函数注入。
理由：自检挂在 verify 里，每次改代码都要跑；一旦真调模型，
自检的耗时与结果就跟着网络和账号配额走——那种红/绿没有诊断价值。
真实链路的效果由 `scripts/selfplay.py` 出报告，那是另一件事。

覆盖四层，每层只测"最容易出错、且错了很难发现"的地方：

    llm.py          JSON 从模型输出里抠出来的规则；禁用/超预算时不抛异常
    customer_data   档案卡的受知边界（客户不知道的东西不能出现在给他的简报里）
    llm_user        动作族越界、编造数字、编造公司名、出戏词，全部要拦；
                    模型不可用时必须静默回退，且回退后的输出仍然可用
    agent_llm       禁语、编造事实、丢掉骨架里的公司名、说成另一件事，全部要拒

一个刻意的取舍：不测"模型说了什么内容更好"。
那属于效果评测，靠 selfplay 的对照实验回答；这里只测"不该过的有没有过"。
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from modules import agent_llm, customer_data, llm, llm_user, nlg  # noqa: E402
from modules.user_sim import (ANSWER_TAIL, ASK_WHY, CONFIRM, DENY,  # noqa: E402
                              HANGUP, UserSim)


class TestExtractJson(unittest.TestCase):
    """模型输出从来不是干净的 JSON，抠不出来就等于白调一次。"""

    def test_plain(self):
        self.assertEqual(llm.extract_json('{"a":1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(llm.extract_json('```json\n{"a":1}\n```'), {"a": 1})

    def test_with_leading_prose(self):
        self.assertEqual(llm.extract_json('好的，结果如下：{"a": 1, "b": "中"}'),
                         {"a": 1, "b": "中"})

    def test_nested(self):
        self.assertEqual(llm.extract_json('{"a":{"b":[1,2]}}'), {"a": {"b": [1, 2]}})

    def test_garbage_returns_none(self):
        for bad in ("", "hello", "[1,2,3", "{不是json}"):
            self.assertIsNone(llm.extract_json(bad), bad)


class TestLLMGate(unittest.TestCase):
    """通话主链路与训练脚本都依赖"调用层永不抛异常"。"""

    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        llm.reset_stats()

    def test_disabled_returns_error_not_raise(self):
        os.environ["LLM_DISABLE"] = "1"
        self.assertFalse(llm.available())
        out = llm.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "disabled")
        self.assertEqual(out["text"], "")

    def test_budget_gate(self):
        os.environ.pop("LLM_DISABLE", None)
        os.environ["LLM_MAX_CALLS"] = "0"
        # 0 表示"不限"，别把 0 当成"一次都不许"
        self.assertTrue(llm.available())
        os.environ["LLM_MAX_CALLS"] = "1"
        llm.reset_stats()
        # 直接把计数置到上限，而不是真调一次：这个文件不能联网。
        llm._STATS["calls"] = 1
        self.assertFalse(llm.available())
        out = llm.chat([{"role": "user", "content": "hi"}], timeout=1.0)
        self.assertIn("budget", out["error"] or "budget")


class TestCustomerData(unittest.TestCase):
    """档案卡的受知边界：给客户看的那段，绝不能出现他不知道的东西。"""

    @classmethod
    def setUpClass(cls):
        cls.data = customer_data.load()

    def test_stats_present(self):
        s = (self.data.get("_meta") or {}).get("stats") or {}
        self.assertGreaterEqual(s.get("cards", 0), 100)
        self.assertGreaterEqual(s.get("personas", 0), 300)

    def test_merchant_cards_have_source(self):
        bad = [c["card_id"] for c in self.data["cards"]
               if not (c.get("source") or {}).get("file")]
        self.assertEqual(bad, [], "档案卡必须能回溯到源文件")

    def test_brief_hides_internal_fields(self):
        """驳回码、审核员工号这类内部字段，不能出现在给客户的简报里。"""
        m = [c for c in self.data["cards"] if c["kind"] == "merchant"][0]
        text = customer_data.brief(m)
        self.assertNotIn("驳回码", text)
        self.assertNotIn("KB-POP-0", text)
        self.assertNotIn("KB-POP", text)
        self.assertNotIn(m["case"]["reject_code"], text)

    def test_brief_states_unknown(self):
        """必须显式告诉模型"这些你不知道"，否则它会替客户编。"""
        m = [c for c in self.data["cards"] if c["kind"] == "merchant"][0]
        self.assertIn("不知道", customer_data.brief(m))

    def test_pairing_is_deterministic(self):
        a, pa = customer_data.pair_for(3)
        b, pb = customer_data.pair_for(3)
        self.assertEqual(a["card_id"], b["card_id"])
        self.assertEqual(pa["id"], pb["id"])

    def test_links_cover_all_cards(self):
        links = self.data.get("links") or {}
        for c in self.data["cards"]:
            self.assertIn(c["card_id"], links)


class TestUserVeto(unittest.TestCase):
    """LLM 客户最容易犯的三件事：替客户补事实、越动作族、出戏。"""

    @classmethod
    def setUpClass(cls):
        cls.data = customer_data.load()
        cls.card = [c for c in cls.data["cards"] if c["kind"] == "merchant"][0]
        cls.persona = customer_data.persona_by_id(
            cls.data["links"][cls.card["card_id"]]) or {}
        # 京东人物库那批画像只有性格，没有 knows（"知不知道"）。
        # 不补的话 derive_facts 返回空字典，客户就变成"什么都不记得"的人，
        # 下面所有"报事实"的用例都会因为 give_without_fact 而红——
        # 那是用例没配全，不是闸门坏了。selfplay.py 里做了同样的事。
        if not cls.persona.get("knows"):
            cls.persona = dict(cls.persona)
            cls.persona["knows"] = {"phone_tail": True, "contact_name": True,
                                    "company_name": True, "application_id": True}

    def _sim(self):
        k = self.card["knows"]
        company = {"company_name": k["company_name"], "contact_name": k["contact_name"],
                   "phone_tail": k["phone_tail"], "application_id": k["application_id"],
                   "shop_name": k["shop_name"]}
        return llm_user.LLMUserSim(self.persona, company, card=self.card, seed="t")

    def test_reject_invented_number(self):
        sim = self._sim()
        base = {"action": CONFIRM, "missed": []}
        # 违规方向是 base → 模型给的动作，报错文本照这个顺序写
        self.assertEqual(sim._veto("我 2024 年报过一次", ANSWER_TAIL, base),
                         "cross_family(confirm->answer_tail)")

    def test_reject_cross_family(self):
        sim = self._sim()
        base = {"action": "ack", "missed": []}
        self.assertTrue(sim._veto("尾号是%s" % sim.facts["phone_tail"], ANSWER_TAIL, base))

    def test_reject_out_of_role(self):
        sim = self._sim()
        base = {"action": "ack", "missed": []}
        self.assertEqual(sim._veto("作为一个AI，我会配合你", "ack", base), "out_of_role")

    def test_reject_fact_not_matching(self):
        sim = self._sim()
        base = {"action": ANSWER_TAIL, "missed": []}
        # 正好跨过"给事实"这一族，但报的是别人家的尾号
        self.assertEqual(sim._veto("后四位是 9999", ANSWER_TAIL, base),
                         "fact_not_matching")

    def test_accept_own_fact(self):
        sim = self._sim()
        base = {"action": ANSWER_TAIL, "missed": []}
        self.assertEqual(sim._veto("后四位是 %s" % sim.facts["phone_tail"], ANSWER_TAIL, base), "")

    def test_self_repeat_blocked(self):
        sim = self._sim()
        sim._last_user_text = "哦，补这三样东西啊。"
        base = {"action": "ack", "missed": []}
        self.assertEqual(sim._veto("哦，补这三样东西啊。", "ack", base), "self_repeat")

    def test_feel_not_understood_downgrades_confirm(self):
        """模型自评"没听懂"，就不能被记成"确认"——这是假确认的入口。"""
        self.assertEqual(llm_user._align_action("那我得补啥材料？", CONFIRM), "ask_material")
        self.assertEqual(llm_user._align_action("明白了", CONFIRM), CONFIRM)
        # 带"哪来的"是质疑来源，归 ask_why 而不是笼统的 deny
        self.assertEqual(llm_user._align_action("啊？哪来的原品牌？", "ack"), "ask_why")


class TestUserFallback(unittest.TestCase):
    """模型不可用时，用户必须照常说话（否则整通电话在第一轮就断）。"""

    def setUp(self):
        os.environ["LLM_DISABLE"] = "1"
        data = customer_data.load()
        self.card = [c for c in data["cards"] if c["kind"] == "merchant"][0]
        self.persona = data.get("personas")[0]

    def tearDown(self):
        os.environ.pop("LLM_DISABLE", None)

    def test_falls_back_to_rule_and_still_talks(self):
        sim = llm_user.build(self.persona, self.card["knows"], card=self.card, seed="x")
        self.assertIsInstance(sim, UserSim)       # 没 key 时直接给规则版
        out = sim.say("请问您是%s吗？" % self.card["knows"]["company_name"])
        self.assertIsNotNone(out)
        self.assertTrue(out["text"])

    def test_disabled_still_returns_source_rule(self):
        """显式构造 LLM 版时也不许崩：它应当退回规则层并标 source=rule。"""
        k = self.card["knows"]
        company = {"company_name": k["company_name"], "contact_name": k["contact_name"],
                   "phone_tail": k["phone_tail"], "application_id": k["application_id"]}
        sim = llm_user.LLMUserSim(self.persona, company, card=self.card, seed="x")
        out = sim.say("请说一下预留手机号的后四位。")
        self.assertEqual(out["source"], "rule")
        self.assertTrue(out["text"])


class TestAdvSim(unittest.TestCase):
    """对抗客户接模型（训练回放用）：动作锁死，事实仍以规则层为准。

    训练回放里客户由模型开口，但**这一层不负责决定"做什么"**：
    难度 L1→L4 与"客服露出的破绽"都是从动作序列推出来的。模型一旦把
    「报尾号」说成「质问」，等级会虚涨、缺陷会变，看上去是训练变难了，
    实际是模拟器自己造的失败。所以这条用例守的就是"动作不许换"。
    """

    def setUp(self):
        os.environ.pop("LLM_DISABLE", None)
        self._orig_json = llm.chat_json
        self._orig_avail = llm.available
        llm.available = lambda: True
        self.profile = {
            "id": "A11", "label": "赶时间的供货商（挑战）", "goal": "confirm",
            "max_level": 4, "traits": {"patience": 6}, "style": ["麻利点"],
            "knows": {"phone_tail": True, "contact_name": True,
                      "application_id": True, "company_name": True},
        }
        self.company = {"company_name": "江西省安品商贸中心", "contact_name": "高超",
                        "phone_tail": "0497", "application_id": "QZ202611060014"}
        self.card = customer_data.card_by_application("QZ202611060014")

    def tearDown(self):
        llm.chat_json = self._orig_json
        llm.available = self._orig_avail

    def _fake(self, say, feel="", action=""):
        llm.chat_json = lambda *a, **k: (
            {"say": say, "action": action, "feel": feel, "got_answer": None},
            {"ok": True, "ms": 1})

    def _sim(self):
        return llm_user.LLMAdvSim(self.profile, self.company, card=self.card, seed="A11")

    def test_action_change_is_blocked(self):
        # 模型嘴上在应声，动作却标成"确认"——这一轮的规则动作是"报公司名"。
        self._fake("好的我明白了，就按你说的办。", action="confirm")
        sim = self._sim()
        out = sim.say("请问您是江西省安品商贸中心的负责人吗？")
        self.assertTrue(out["veto"].startswith("action_locked"))
        self.assertEqual(out["source"], "rule")            # 退回规则层原话
        self.assertTrue(out["text"])                       # 但话还在，通话不断
        self.assertEqual(out["action"], sim.rule.last_action)   # 动作仍按规则层

    def test_action_is_locked_directly(self):
        sim = self._sim()
        self.assertTrue(sim._veto("我尾号 0497", ASK_WHY,
                                  {"action": ANSWER_TAIL}).startswith("action_locked"))

    def test_fact_mismatch_is_blocked(self):
        sim = self._sim()
        base = {"action": ANSWER_TAIL}
        self.assertTrue(sim._veto("我尾号 1234", ANSWER_TAIL, base))
        self.assertEqual(sim._veto("尾号 0497", ANSWER_TAIL, base), "")

    def test_company_short_name_is_allowed(self):
        """真人不会每句都念全称；说"我们安品"应当放行，说别人的名字不行。"""
        sim = self._sim()
        base = {"action": llm_user.ANSWER_COMPANY}
        self.assertEqual(sim._veto("对，就是我们安品。", llm_user.ANSWER_COMPANY, base), "")
        self.assertTrue(sim._veto("对，我们是南昌鼎盛商贸中心。",
                                  llm_user.ANSWER_COMPANY, base))

    def test_grounded_line_passes(self):
        self._fake("我们这儿就是江西省安品商贸中心。")
        sim = self._sim()
        out = sim.say("请问您是江西省安品商贸中心的负责人吗？")
        # 动作恰好不是"报公司名"时，这句换动作会被拦——那也是正确行为。
        if out["source"] == "llm":
            self.assertIn("安品", out["text"])
        else:
            self.assertTrue(out["veto"].startswith("action_locked"))

    def test_level_and_weakness_come_from_rule_layer(self):
        """模型改了措辞，也不能改难度与弱点统计——它们指回"客服哪里没做好"。"""
        self._fake("嗯，你说。")
        sim = self._sim()
        sim.say("请问您是江西省安品商贸中心的负责人吗？")
        sim.say("请问您是江西省安品商贸中心的负责人吗？")   # 原话重复 = 客服复读
        self.assertGreaterEqual(sim.hits.get("repeat", 0), 1)
        self.assertGreaterEqual(max(sim.levels or [0]), 1)
        self.assertEqual(sim.rule.level, sim.level)

    def test_branch_company_is_same_entity(self):
        """档案写"…有限公司成都市分公司"，客户说"…有限公司"是同一家（A16 实测误拦）。"""
        sim = self._sim()
        sim.card = {"knows": {"company_name": "四川省栖梧云启科技有限公司成都市分公司"}}
        self.assertTrue(sim._entities_ok("对，我们是栖梧云启科技有限公司。"))
        self.assertFalse(sim._entities_ok("对，我们是南昌鼎盛商贸有限公司。"))

    def test_build_adversary_falls_back_when_disabled(self):
        llm.available = lambda: False
        from modules.adversary import AdversarySim
        self.assertIsInstance(llm_user.build_adversary(self.profile, self.company),
                              AdversarySim)


class TestAgentAssist(unittest.TestCase):
    """客服表达层的四道闸：说错事实、说禁语、丢掉公司名、说成另一件事。"""

    def setUp(self):
        self._orig = llm.chat
        self._orig_avail = llm.available
        llm.available = lambda: True
        self.session = {"slots": {"application": {
            "company_name": "北京市安食商贸有限公司", "brand_name": "暖时光",
            "reject_reason": "授权链路不完整", "required_materials": ["品牌授权书"]}},
            "outbound_target": {"company_name": "北京市安食商贸有限公司",
                                "phone_tail": "0086"}}

    def tearDown(self):
        llm.chat = self._orig
        llm.available = self._orig_avail

    def _fake(self, text):
        llm.chat = lambda *a, **k: {"ok": True, "text": text, "model": "fake",
                                    "ms": 1, "usage": {}, "retries": 0, "error": ""}

    def test_returns_none_when_llm_disabled(self):
        llm.available = lambda: False
        self.assertIsNone(agent_llm.assist(self.session, "请补充品牌授权书。", "NEED"))

    def test_rejects_banned_word(self):
        self._fake("您放心，我保证能通过。")
        self.assertIsNone(agent_llm.assist(self.session, "请补充品牌授权书。", "NEED"))

    def test_rejects_invented_amount(self):
        self._fake("补材料要交 3500 元。")
        self.assertIsNone(agent_llm.assist(self.session, "请补充品牌授权书。", "NEED"))

    def test_rejects_dropping_company_name(self):
        base = "请问您是北京市安食商贸有限公司的法定代表人吗？"
        self._fake("请问您是这个公司的法人吗？")
        self.assertIsNone(agent_llm.assist(self.session, base, "GREETING"))

    def test_rejects_off_intent(self):
        self._fake("我们会持续关注您的体验，感谢支持。")
        self.assertIsNone(agent_llm.assist(self.session, "请补充品牌授权书。", "NEED"))

    def test_accepts_grounded_reply(self):
        self._fake("您这笔是暖时光品牌的授权链路不完整，把品牌授权书补上就行。")
        got = agent_llm.assist(self.session, "需要补充品牌授权书。", "NEED",
                               user_text="我要补什么材料")
        self.assertTrue(got and "授权书" in got)

    def test_counts_and_caps(self):
        self._fake("您这笔是暖时光品牌的授权链路不完整，把品牌授权书补上就行。")
        s = {}
        for _ in range(8):
            agent_llm.assist(s, "需要补充品牌授权书。", "NEED", user_text="补什么")
        self.assertLessEqual(int(s.get("assist_count") or 0), 6)


class TestAnswersQuestion(unittest.TestCase):
    """检索命中了，但答的是不是他问的——这条闸门在对话层，放这里一起守。

    自对练挖出来的原句：客户问"海外采购凭证具体要哪几样"，命中的条目讲的是
    "商标状态待审"，客服照着念了，客户拿着错的答案去准备材料。
    """

    def _hit(self, ans):
        return {"id": "KB-X", "category": "商标注册证", "answer": ans}

    def setUp(self):
        from modules import dialogue
        self.f = dialogue._kb_answers_question
        self.app = {"required_materials": ["境外主体证明", "海外采购凭证", "公证认证文件"]}

    def test_rejects_answer_about_something_else(self):
        self.assertFalse(self.f(self._hit("需您提供已注册状态的对应类商标"),
                                "海外采购凭证具体要哪几样", self.app))

    def test_accepts_answer_containing_asked_material(self):
        self.assertTrue(self.f(self._hit("需要您补充海外采购凭证的原件"),
                               "海外采购凭证具体要哪几样", self.app))

    def test_passes_when_customer_asked_nothing_specific(self):
        self.assertTrue(self.f(self._hit("需您提供已注册状态的对应类商标"),
                               "为什么要驳回我", self.app))

    def test_short_material_segments_do_not_trigger(self):
        # "证明"这种 2 字片段不算——太短，会跟任何答案撞上
        app = {"required_materials": ["证明"]}
        self.assertTrue(self.f(self._hit("需您提供商标"), "证明要什么", app))


class TestNlgAssistSwitch(unittest.TestCase):
    """assist 是开关：关着的时候必须和接入前一模一样。"""

    def test_template_provider_ignores_assist(self):
        orig = nlg.config_loader.nlg_config
        nlg.config_loader.nlg_config = lambda: {"provider": "template", "assist": True}
        try:
            base = "为了核对身份，请说一下手机号后四位。"
            self.assertEqual(nlg.say({}, base, "IDENTITY"), base)
        finally:
            nlg.config_loader.nlg_config = orig

    def test_force_template_short_circuits_assist(self):
        orig = nlg.config_loader.nlg_config
        nlg.config_loader.nlg_config = lambda: {"provider": "dashscope", "assist": True}
        try:
            base = "为了核对身份，请说一下手机号后四位。"
            with nlg.template_only():
                self.assertEqual(nlg.say({}, base, "IDENTITY"), base)
        finally:
            nlg.config_loader.nlg_config = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
