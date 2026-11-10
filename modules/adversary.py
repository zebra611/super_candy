"""自适应对抗客户模拟器（11-08）。

和 modules/user_sim.py 的分工：
    UserSim       固定画像参数，按受知/受听/受耐心反应，用于 10 通与 12 画像基线；
    AdversarySim  在同样的三条约束上，额外**观察客服弱点并主动升级难度**，
                  用于独立对抗集。两者不共用随机序列，基线一个字节都不会被改。

为什么要单独一个文件：把「难度递进」塞进 UserSim，哪怕加一个分支，
既有画像的 rng 调用次数就可能变，8/10、12/12 的历史基线立刻失真。

四条硬约束（写死在这里，不进可进化规则表）：
    1. 只看客服**实际播出去的那句话**（reply_text）。不读 stage / intent / slots /
       画像标签，也读不到客服内部状态——否则等于开卷考试，优化器照着标签刷分。
    2. 不编造事实：报出的尾号/姓名/单号只能来自本通电话真实档案（derive_facts）。
    3. 难度由客服表现驱动：重复、答非所问、没接住已给的信息、指代含糊、
       把「没听懂」当同意……每命中一次就加压；不是按轮次念固定台词。
    4. 确定性：random.Random(seed)，同一画像 + 同一段客服话 → 逐字节一致。

输出动作名沿用 user_sim 的动作集，归因器与失败聚类不用改一行就能接。
"""

import random
import re

from modules import user_sim as _us
from modules.user_sim import (
    ACK, ANSWER_COMPANY, ANSWER_NAME, ANSWER_ORDER, ANSWER_TAIL,
    ASK_FEE, ASK_MATERIAL, ASK_WHY, CANT_REMEMBER, CONFIRM, DENY,
    HANGUP, MISHEAR, NO_WRONG_PERSON, OFF_TOPIC, REASK_ALREADY_TOLD,
    REFUSE_INFO, YES_COMPANY, derive_facts, detect_asks,
)

#: 弱点名 → 人话（写进报告，便于人复核）
WEAKNESS = {
    "repeat": "连续重复同一句",
    "no_answer": "上一轮的问题没被正面回答",
    "slot_loss": "已经给过的信息又被要了一遍",
    "vague_ref": "核身指代含糊（不知道要哪个后四位）",
    "noise_on_question": "把提问当成有杂音没听清",
    "premature": "用户说没听懂却直接回写收线",
    "stalled": "连续多轮停在同一个要求上",
}

#: 客服话里出现这些，说明上一轮的提问得到了正面回答
_ANSWER_CUES = {
    ASK_WHY: ("官方", "审核专员", "不是推销", "资质审核", "商家后台"),
    ASK_MATERIAL: ("补充", "材料", "重新提交", "上传"),
    ASK_FEE: ("不收取", "免费", "费用", "不收费"),
}

#: 给过的事实 → detect_asks 的要素名
_FACT_TO_ASK = {
    "phone_tail": "tail", "application_id": "order",
    "contact_name": "name", "company_name": "company",
}

#: 指代含糊 / 噪声 / 提前收线的字面信号
_VAGUE_MARKS = ("信息后四位", "店铺或公司信息", "那几个数", "相关信息")
_NOISE_MARKS = ("杂音", "没听清", "信号不好", "再说一次")
_PREMATURE_MARKS = ("记录下来了", "稍后会有短信", "感谢您的配合", "都已记录")

_TEMPLATES = {
    YES_COMPANY: "对，是我，你说",
    ANSWER_TAIL: "尾号{phone_tail}",
    ANSWER_NAME: "我叫{contact_name}",
    ANSWER_ORDER: "单号后六位{application_id}",
    ANSWER_COMPANY: "公司名是{company_name}",
    CANT_REMEMBER: "那号我一时真想不起来",
    REFUSE_INFO: "我凭什么报给你，你谁啊",
    ASK_WHY: "你哪来的我号码，怎么信你",
    ASK_MATERIAL: "那我具体要补什么材料",
    ASK_FEE: "这个要收费吗，别是推销吧",
    CONFIRM: "清楚了，我按你说的弄",
    DENY: "哎呀，我还是没听明白",
    REASK_ALREADY_TOLD: "我刚不是说了吗，就那几个数",
    MISHEAR: "啊？信号不好，你再说一遍",
    ACK: "嗯",
    OFF_TOPIC: "我跟你说啊，上次那个审核拖了半个月",
    NO_WRONG_PERSON: "打错了，没这个人",
    HANGUP: "不说了，我忙着呢",
}

#: 难度池：等级越高，配合概率越低、攻击手法越多
_POOLS = {
    1: [YES_COMPANY, ANSWER_TAIL, ASK_MATERIAL, CONFIRM, ACK],
    2: [YES_COMPANY, ANSWER_TAIL, ANSWER_NAME, ASK_WHY, ASK_MATERIAL,
        MISHEAR, OFF_TOPIC, ACK],
    3: [YES_COMPANY, ANSWER_TAIL, ANSWER_NAME, REFUSE_INFO, CANT_REMEMBER,
        ASK_WHY, ASK_FEE, ASK_MATERIAL, REASK_ALREADY_TOLD, OFF_TOPIC, DENY],
    4: [REFUSE_INFO, CANT_REMEMBER, ASK_WHY, ASK_FEE, REASK_ALREADY_TOLD,
        OFF_TOPIC, DENY, MISHEAR, NO_WRONG_PERSON, ACK],
}

#: 命中弱点 → 下一轮优先打哪里
_PROBE_FOR = {
    "repeat": REASK_ALREADY_TOLD,
    "no_answer": ASK_WHY,
    "slot_loss": REASK_ALREADY_TOLD,
    "vague_ref": ASK_WHY,
    "noise_on_question": MISHEAR,
    "premature": DENY,
    "stalled": OFF_TOPIC,
}

#: 客服在问「确认了吗」——只看播出文本，不读阶段/意图/槽位。
#: 低压力配合者听到这句会如实回答，不会继续质疑；高压力画像不受影响。
_CONFIRM_PROMPT_MARKS = ("您清楚了吗", "清楚了或者", "还有疑问", "同意或者不需要")


def _stem(text, n=14):
    """去掉空白与标点后的前 n 个字。

    为什么不用整句相等：客服用 _soothe 给同一句话追加「您慢慢想，我这边等着」
    这类尾巴，整句比较会漏判；而用户听到的就是"又问了一遍"。按主干比更贴近
    真实感受，也能让训练回放里的「客服复读」标得出来（11-08 实测 A07 第 1~3 轮）。
    """
    t = re.sub(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()]", "", text or "")
    return t[:n]

_ACTION_FACT = {
    ANSWER_TAIL: "phone_tail", ANSWER_NAME: "contact_name",
    ANSWER_ORDER: "application_id", ANSWER_COMPANY: "company_name",
}


class AdversarySim(object):
    """一个会挑软肋、不断加压的客户。接口与 UserSim.say() 对齐。"""

    def __init__(self, profile, company, seed=""):
        self.profile = profile
        self.facts = derive_facts(profile, company)
        self.rng = random.Random("adv:%s" % (seed or profile.get("id", "")))
        self.max_level = int(profile.get("max_level") or 4)
        self.traits = profile.get("traits") or {}
        self.patience = int(self.traits.get("patience") or 8)
        self.level = 1
        self.turn = 0
        self.ended = False
        self.last_agent = ""
        self.last_action = ""
        self.last_given = ""
        self.ask_streak = ""
        self.streak_n = 0
        self.confirm_prompt = False
        self.hits = {k: 0 for k in WEAKNESS}
        self.levels = []
        self.blocked_by_missing_fact = []
        self.style = list(profile.get("style") or ["哎呀"])

    # -- 观察：只用客服播出去的那句话 -------------------------------------
    def _observe(self, agent_text):
        t = agent_text or ""
        self.confirm_prompt = any(w in t for w in _CONFIRM_PROMPT_MARKS)
        found = []
        if t and self.last_agent and _stem(t) == _stem(self.last_agent):
            found.append("repeat")
        if self.last_action in _ANSWER_CUES:
            cues = _ANSWER_CUES[self.last_action]
            if not any(c in t for c in cues):
                found.append("no_answer")
        asks = detect_asks(t)
        if self.last_given and _FACT_TO_ASK.get(self.last_given) in asks:
            found.append("slot_loss")
        if any(w in t for w in _VAGUE_MARKS):
            found.append("vague_ref")
        if self.last_action in (ASK_WHY, ASK_MATERIAL, ASK_FEE) \
                and any(w in t for w in _NOISE_MARKS):
            found.append("noise_on_question")
        if self.last_action == DENY and any(w in t for w in _PREMATURE_MARKS):
            found.append("premature")
        key = asks[0] if asks else ""
        if key and key == self.ask_streak:
            self.streak_n += 1
        else:
            self.ask_streak, self.streak_n = key, 1
        if self.streak_n >= 4:
            found.append("stalled")
        return found, asks

    def _escalate(self, found):
        """难度由客服弱点驱动：命中越多、轮次越长，压力越大。"""
        for w in found:
            self.hits[w] += 1
        total = sum(self.hits.values())
        self.level = min(self.max_level, 1 + total // 2 + self.turn // 4)
        return total

    def _cooperate_p(self):
        return max(0.12, 0.82 - 0.18 * (self.level - 1))

    # -- 决策：哪里弱打哪里 ------------------------------------------------
    def _choose(self, found, asks):
        # 生理性听不清（档案 traits.mishear_every）：每隔 N 轮非要重听一遍。
        # 与弱点驱动的 MISHEAR 分开——那条是「客服把提问当噪声」才触发的惩罚，
        # 这条不看客服表现：客服做对了也照样被要求再说一次。考的是
        # 「重说请求能不能被当成重说」：OFFER 阶段若把它当提问去检索知识库，
        # 就会回「这个我这边确认不到」，客户只好一遍遍重复（真实通话里踩过）。
        # 收到「您清楚了吗」那一轮不插，否则客户永远表不了态，用例变成死循环。
        every = int(self.traits.get("mishear_every") or 0)
        if every and self.turn and self.turn % every == 0 and not self.confirm_prompt:
            return MISHEAR, "hearing"

        # 低压力「配合者」对照画像：客服复述完问「您清楚了吗」，这类商家会
        # 如实答「清楚了」，而不是继续把同一句质疑重复到通话结束。不补这条，
        # A01 这种 label 写着「低压力配合者」、goal=confirm 的画像会被对抗器
        # 逼成连续 8 轮质疑，造出和自身定义相反的假失败；它原来的「通过」
        # 靠的也是客服在质疑中回写 unknown 这个错误行为（假绿）。
        # 边界写死：只作用于 max_level<=2 的对照组，难度仍由弱点命中数驱动，
        # 其余 8 个高压力画像的动作池、升级逻辑一字不改。
        if (self.profile.get("goal") == "confirm" and self.max_level <= 2
                and self.confirm_prompt):
            return CONFIRM, ""
        if found and self.level >= 2 and self.rng.random() < 0.72:
            for w in found:
                probe = _PROBE_FOR.get(w)
                if probe:
                    return probe, w
        want = next((k for k in ("tail", "name", "order", "company") if k in asks), "")
        action_for = {"tail": (ANSWER_TAIL, "phone_tail"),
                      "name": (ANSWER_NAME, "contact_name"),
                      "order": (ANSWER_ORDER, "application_id"),
                      "company": (ANSWER_COMPANY, "company_name")}
        if want and want in action_for and self.rng.random() < self._cooperate_p():
            action, fact = action_for[want]
            if fact in self.facts:
                return action, ""
            self.blocked_by_missing_fact.append(fact)
            return CANT_REMEMBER, ""
        pool = _POOLS.get(self.level) or _POOLS[1]
        action = self.rng.choice(pool)
        # 高压力画像不会突然说「清楚了，按你说的弄」——那就不是对抗客户了。
        # 不禁用核身事实动作：全禁会让通话永远卡在 IDENTITY，那种失败是模拟器造的。
        if int(self.profile.get("max_level") or 4) >= 4 and action == CONFIRM:
            action = DENY
        return action, ""

    # -- 渲染：不编事实，只按等级加噪 --------------------------------------
    def _render(self, action):
        need = _ACTION_FACT.get(action)
        if need and need not in self.facts:
            # 受知约束：档案里没有的事实，一个字都不许编，更不能把 {占位符} 念出去。
            if need not in self.blocked_by_missing_fact:
                self.blocked_by_missing_fact.append(need)
            action = CANT_REMEMBER
            text = _TEMPLATES[CANT_REMEMBER]
        else:
            text = _TEMPLATES[action]
            for k, v in self.facts.items():
                text = text.replace("{%s}" % k, str(v))
        if self.level >= 2 and self.rng.random() < 0.5:
            text = self.rng.choice(self.style) + "，" + text
        if self.level >= 3 and self.rng.random() < 0.35:
            text = text + "；还有，这个得去哪儿弄，后台哪个入口？"
        if self.level >= 4 and self.rng.random() < 0.3:
            text = "我说，" + text + "，你这都问第几遍了"
        return text, action

    def say(self, agent_text):
        """看客服说了什么，回一句；已挂断返回 None。"""
        if self.ended:
            return None
        self.turn += 1
        found, asks = self._observe(agent_text)
        self._escalate(found)
        self.levels.append(self.level)

        for w in found:
            self.patience -= 2 if w in ("repeat", "premature") else 1
        if self.patience <= 0:
            self.ended = True
            return {"text": _TEMPLATES[HANGUP], "action": HANGUP,
                    "level": self.level, "probe": "patience_exhausted",
                    "given_fact": "", "found": list(found)}

        action, probe = self._choose(found, asks)
        text, action = self._render(action)
        given = _ACTION_FACT.get(action, "") if action in _ACTION_FACT else ""
        if given and given not in self.facts:
            given = ""
        self.last_agent = agent_text or ""
        self.last_action = action
        self.last_given = given
        if action == HANGUP:
            self.ended = True
        return {"text": text, "action": action, "level": self.level,
                "probe": probe, "given_fact": given, "found": list(found)}
