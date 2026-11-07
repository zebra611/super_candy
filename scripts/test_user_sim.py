"""模拟用户的离线自测 —— 证明"它像个真人"，也证明"它不会作弊"。

为什么这个文件比模拟器本身还重要：
    评测器一旦会编造事实，整套 L2 就变成自证。上一代回放式用例已经吃过一次亏
    （docs/28：24/40 失败里有一半是"用例自己不可能通过"）。
    所以下面三条硬约束必须能被单独复算：
        受知  不知道的事说不出，知道的事必须来自本通档案
        受听  没被问就不主动交信息；听错只错一次，且可归因
        受耐心 挂断是逼出来的，不是脚本写死的

运行：
    python3 scripts/test_user_sim.py
    python3 scripts/test_user_sim.py -v

纯标准库，不联网、不读密钥、不碰事件库。
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from modules import attribution as attr          # noqa: E402
from modules.user_sim import (                    # noqa: E402
    ANSWER_NAME, ANSWER_TAIL, CANT_REMEMBER, HANGUP, MISHEAR,
    GAVE_FACT_ACTIONS, UserSim, derive_facts, detect_asks,
)

# 本通电话真正拨出去的档案。注意：所有事实都必须从这里来，
# 测试里不允许出现"另一套编好的号码"——那就是自证。
COMPANY = {
    "company_name": "测试商贸有限责任公司",
    "contact_role": "法定代表人",
    "contact_name": "张三",
    "phone_tail": "8642",
    "application_id": "QZ202611060099",
}

ASK_TAIL = "好的，核对申请单需要您预留手机号的后四位，说四个数字就行。"
ASK_TAIL_OR_NAME = "您报一下手机号后四位，或者说联系人姓名，都可以。"
GREETING = "您好，我是京东商家资质审核专员，请问您是测试商贸有限责任公司的法定代表人吗？"


def _persona(**kw):
    p = {
        "id": "T01", "label": "测试", "goal": "confirm",
        "knows": {"phone_tail": True, "contact_name": True},
        "willing": {"phone_tail": "on_ask", "contact_name": "on_ask"},
        "traits": {"hearing": 1.0, "patience": 6, "trust0": 0.5,
                   "trust_gate": 0.2, "verbosity": 0.3},
        "style": [],
    }
    for k, v in kw.items():
        if k == "traits":
            p["traits"].update(v)
        else:
            p[k] = v
    return p


class TestEpistemicLimit(unittest.TestCase):
    """受知：不知道就说不出，知道的必须来自档案。"""

    def test_unknown_fact_is_never_invented(self):
        """画像不知道自己尾号 → 问三次也不能吐出正确尾号。"""
        sim = UserSim(_persona(knows={"phone_tail": False, "contact_name": True}),
                      COMPANY, seed="t1")
        outs = [sim.say(ASK_TAIL) for _ in range(3)]
        joined = " ".join(o["text"] for o in outs)
        self.assertNotIn("8642", joined, "模拟器编造了它不知道的尾号")
        self.assertNotIn("八六四二", joined, "模拟器编造了它不知道的尾号（中文读法）")
        self.assertEqual(outs[0]["action"], CANT_REMEMBER,
                         "不知道尾号却说别的话：%s" % outs[0]["text"])

    def test_known_fact_comes_from_record(self):
        """知道的尾号必须等于档案里的值，不是写死的。"""
        sim = UserSim(_persona(), COMPANY, seed="t2")
        out = sim.say(ASK_TAIL)
        self.assertEqual(out["action"], ANSWER_TAIL)
        self.assertTrue("8642" in out["text"] or "八六四二" in out["text"],
                        "报出的尾号和档案不一致：%s" % out["text"])

    def test_derive_facts_respects_knows(self):
        f = derive_facts(_persona(knows={"phone_tail": False, "contact_name": True}), COMPANY)
        self.assertNotIn("phone_tail", f, "knows=false 的字段不该出现在事实表里")
        self.assertIn("contact_name", f)


class TestPerceptualLimit(unittest.TestCase):
    """受听：没被问就不说；听错只一次；能从话里认出被问了什么。"""

    def test_does_not_volunteer_before_asked(self):
        """开场白只确认身份，没有要信息 → 不能主动报尾号。"""
        sim = UserSim(_persona(), COMPANY, seed="t3")
        out = sim.say(GREETING)
        self.assertEqual(out["action"], "yes_company")
        self.assertNotIn(out["action"], GAVE_FACT_ACTIONS)
        self.assertNotIn("8642", out["text"])

    def test_mishear_only_once(self):
        """听不清的人让它重复一次就够；第二次还听不清就是模拟器坏了。"""
        sim = UserSim(_persona(traits={"hearing": 0.0}), COMPANY, seed="t4")
        first = sim.say(ASK_TAIL)
        self.assertEqual(first["action"], MISHEAR)
        second = sim.say(ASK_TAIL)
        self.assertNotEqual(second["action"], MISHEAR,
                            "同一要素被听错两次：真人让人重复一遍通常就听清了")

    def test_hearing_impairment_is_recorded(self):
        """听错必须留下证据，否则归因时会被算成客服的错。"""
        sim = UserSim(_persona(traits={"hearing": 0.0}), COMPANY, seed="t5")
        out = sim.say(ASK_TAIL)
        self.assertTrue(out["missed"], "听错了却没记录 missed，归因会误判")

    def test_detect_asks_reads_words_not_stage(self):
        self.assertIn("tail", detect_asks(ASK_TAIL))
        self.assertIn("company", detect_asks(GREETING))
        self.assertEqual(detect_asks("好的，您说。"), [])

    def test_takes_alternative_path_when_offered(self):
        """客服说"尾号或姓名都行"，记不住尾号的人会报姓名（真人就这么做）。"""
        sim = UserSim(_persona(knows={"phone_tail": False, "contact_name": True}),
                      COMPANY, seed="t6")
        out = sim.say(ASK_TAIL_OR_NAME)
        self.assertEqual(out["action"], ANSWER_NAME,
                         "给了替代路径却还死盯尾号 —— 模拟器会把可闭环的人跑成挂断")


class TestPatience(unittest.TestCase):
    """受耐心：挂断是有原因的，且原因可解释。"""

    def test_patience_exhaustion_hangs_up(self):
        sim = UserSim(_persona(traits={"patience": 0}), COMPANY, seed="t7")
        out = sim.say(GREETING)
        self.assertEqual(out["action"], HANGUP)
        self.assertTrue(sim.ended)

    def test_repeat_question_costs_patience(self):
        sim = UserSim(_persona(), COMPANY, seed="t8")
        sim.say(ASK_TAIL)
        before = sim.patience
        sim.say(ASK_TAIL)          # 客服原地重复同一句
        self.assertLess(sim.patience, before, "客服重复同一句却不掉耐心")

    def test_unanswered_question_costs_patience(self):
        """问了不答最招人烦：追问之后客服没正面回答，耐心要掉。

        对照组同样跑一轮、但不提问：那一轮不能掉耐心。
        没有对照组的话，"耐心掉了"可能只是别的规则顺手扣的，测了等于没测。
        """
        explain = "针对授权链路不完整，您需要补充品牌授权书，然后在后台重新提交申请。"

        asking = UserSim(_persona(), COMPANY, seed="t10")
        o1 = asking.say(explain)
        self.assertEqual(o1["action"], "ask_material",
                         "客服讲了要补材料，用户却没追问：%s" % o1["text"])
        before = asking.patience
        asking.say("嗯，您说的我记下了，咱们接着说。")      # 答非所问
        self.assertLess(asking.patience, before, "追问没被回答，耐心却没掉")

        control = UserSim(_persona(), COMPANY, seed="t11")
        control.say(GREETING)                               # 只确认身份，不提问
        before2 = control.patience
        control.say("嗯，您说的我记下了，咱们接着说。")
        self.assertEqual(control.patience, before2, "没提问却凭空掉了耐心")


class TestDeterminism(unittest.TestCase):
    """同一 persona、同一段客服话术，必须逐字节一致，否则回归没有基准。"""

    def test_same_seed_same_transcript(self):
        def run():
            sim = UserSim(_persona(traits={"hearing": 0.5}), COMPANY, seed="t11")
            return [(o["action"], o["text"]) for o in
                    (sim.say(x) for x in (GREETING, ASK_TAIL, ASK_TAIL_OR_NAME))]
        self.assertEqual(run(), run())


class TestAttribution(unittest.TestCase):
    """归因：这是 L2 的分水岭——责任判错，后面每一步优化都在打空气。"""

    def _turns(self, pairs):
        return [{"user": {"action": ua, "text": ut},
                 "agent": {"reply_text": rt, "stage": st, "next_stage": nx,
                           "intent": it, "slots": {}}}
                for (ua, ut), (rt, st, nx, it) in pairs]

    def test_question_treated_as_noise(self):
        turns = self._turns([
            (("ask_material", "那我具体要补什么材料？"),
             ("不好意思，这边有点杂音没听清，您再说一次。", "IDENTITY", "IDENTITY", "noise")),
        ])
        codes = [d["code"] for d in attr.analyse_turns(turns)]
        self.assertIn(attr.AGENT_NOISE_ON_QUESTION, codes,
                      "用户明确在提问，被当成杂音却没被识别为缺陷")

    def test_slot_loss_after_answer(self):
        turns = self._turns([
            (("answer_tail", "后四位是 8642"),
             ("好的，核对申请单需要您预留手机号的后四位，说四个数字就行。",
              "IDENTITY", "IDENTITY", "identity_retry")),
        ])
        codes = [d["code"] for d in attr.analyse_turns(turns)]
        self.assertIn(attr.AGENT_SLOT_LOSS, codes,
                      "用户已报尾号、客服又问一遍，却没被判成槽位丢失")

    def test_case_infeasible_is_not_blamed_on_agent(self):
        """画像自己没这条信息 → 责任在用例，不能记到系统头上。"""
        session = {"id": "X1", "closed": False, "blocked_by_missing_fact": ["phone_tail"],
                   "turns": self._turns([
                       (("cant_remember", "我记不住"), ("您再想想？", "IDENTITY", "IDENTITY", "unclear")),
                   ])}
        res = attr.analyse_session(session, {"goal": "confirm", "expect": {"closed": True}})
        self.assertEqual(res["fault"], "case")

    def test_designed_dropoff_is_not_a_defect(self):
        """秒挂/打错人是设计如此，不能算"系统失败"。"""
        session = {"id": "X2", "closed": False, "blocked_by_missing_fact": [],
                   "turns": self._turns([
                       (("hangup", "行了行了"), ("感谢您的接听。", "GREETING", "DONE", "hangup")),
                   ])}
        res = attr.analyse_session(session, {"goal": "hangup_now", "expect": {"closed": False}})
        self.assertEqual(res["fault"], "sim")
        self.assertTrue(res["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
