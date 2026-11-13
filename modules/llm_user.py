"""LLM 扮演的客户 —— 让"模拟用户"真的像个人，而不是十八句模板轮着念。

**为什么要换掉 `UserSim` 的说话方式**
    `modules/user_sim.py` 解决的是另一件事：让模拟用户**受知、受听、受耐心**，
    这三个约束把"模拟器造出来的假缺陷"压到了 0。但它说话是模板：
    "号码是店里注册的，我一时真想不起来"——十通电话里八通一模一样，
    不会追问细节、不会把话说成自己的事、不会因为客服绕圈子而换个问法。

    拿这种用户去调客服，等于用固定的考卷考一辈子：客服只要背下这十八句
    对应答案就够了，学不到"这个客户到底卡在哪"。用户说"用户不智能"，
    指的就是这里。

**但它不能把 `user_sim` 那三条约束丢掉。**
    这是本次改造最容易出错的地方：接上大模型后，模型会"体贴地"替客户
    把信息补全——客服没问，它先报尾号；档案里没有，它编一个出来。
    那会把 `docs/28` 里那个"假绿"错误原样犯一遍，只是换成了更贵的模型。

    所以本模块的架构是**两层的**：

        UserSim（规则层）  ── 负责"能做什么"：事实边界、耐心、信任、听错
              │               （这一层一个字节都不改，verify 244 条守着它）
              ▼
        LLM（表达层）      ── 负责"怎么说"：语气、措辞、追问的具体内容

    规则层先算出这一轮的**动作**（base_action）；LLM 只能在同一个"动作族"里
    换成更自然的表现，不能跨族。典型的三条硬边界：
        · 客服没问，规则层判 ACK → LLM 只能应声/跑题/反问，不能主动报事实；
        · 档案里没有的事实，规则层判 CANT_REMEMBER → LLM 不能说"我想起来了"；
        · 耐心耗尽，规则层判 HANGUP → LLM 不能继续聊。

    换句话说：**智能加在"怎么说"，不加在"能说什么"。**
    越界的输出会被 `_veto()` 打回规则层的句子，并在报告里记一次 `veto`，
    这样"模型到底帮上忙还是添了乱"是可数的，不靠感觉。

**失败必须无声降级。**
    没有 key、超时、返回不是 JSON、说了档案里没有的数字——一律回退到规则层
    那句话，并在结果里标 `source="rule"`。训练脚本据此统计 LLM 覆盖率。
"""

import difflib
import os
import re
import sys

from modules import llm
from modules.adversary import AdversarySim
from modules.user_sim import (
    ACK, ANSWER_COMPANY, ANSWER_NAME, ANSWER_ORDER, ANSWER_TAIL, ASK_FEE,
    ASK_MATERIAL, ASK_WHY, CANT_REMEMBER, CONFIRM, DENY, GAVE_FACT_ACTIONS,
    HANGUP, MISHEAR, NO_WRONG_PERSON, OFF_TOPIC, REASK_ALREADY_TOLD,
    REFUSE_INFO, YES_COMPANY, UserSim,
)

#: 允许 LLM 输出的动作全集（与规则层同名，归因/聚类才认得出来）。
ACTIONS = (ANSWER_TAIL, ANSWER_NAME, ANSWER_ORDER, ANSWER_COMPANY,
           CANT_REMEMBER, REFUSE_INFO, ASK_WHY, REASK_ALREADY_TOLD,
           ASK_MATERIAL, ASK_FEE, CONFIRM, DENY, YES_COMPANY,
           NO_WRONG_PERSON, MISHEAR, ACK, OFF_TOPIC, HANGUP)

_ACTION_CN = {
    ANSWER_TAIL: "报出预留手机号后四位",
    ANSWER_NAME: "报出你的姓名",
    ANSWER_ORDER: "报出申请单编号/订单号",
    ANSWER_COMPANY: "确认公司就是你这家",
    CANT_REMEMBER: "说不记得具体号码/单号",
    REFUSE_INFO: "不愿意提供身份信息",
    ASK_WHY: "反问对方是谁、哪来的号码",
    REASK_ALREADY_TOLD: "说'我刚才不是说过了吗'",
    ASK_MATERIAL: "追问具体要补什么材料、去哪儿补",
    ASK_FEE: "追问收不收费",
    CONFIRM: "表示听明白了、会去办",
    DENY: "表示还没听明白、不认可",
    YES_COMPANY: "应一声'是我/对'",
    NO_WRONG_PERSON: "说打错了、没这个人",
    MISHEAR: "说没听清、让对方再说一遍",
    ACK: "只应一声（嗯/你说），不推进",
    OFF_TOPIC: "顺口扯一句别的（店里忙、后台卡）",
    HANGUP: "挂断（说先这样、我忙着呢）",
}

#: 动作族。**跨族即越界**，见模块文档。
_FAMILY = {
    ANSWER_TAIL: "give", ANSWER_NAME: "give", ANSWER_ORDER: "give",
    ANSWER_COMPANY: "give",
    CANT_REMEMBER: "unsure", REFUSE_INFO: "unsure",
    ASK_WHY: "challenge", REASK_ALREADY_TOLD: "challenge", DENY: "challenge",
    NO_WRONG_PERSON: "challenge",
    ASK_MATERIAL: "ask", ASK_FEE: "ask", MISHEAR: "ask",
    ACK: "idle", OFF_TOPIC: "idle",
    CONFIRM: "accept", YES_COMPANY: "accept",
    HANGUP: "end",
}

#: 同族内允许互换。跨族一律回退。
#: accept 额外放行 ask/challenge：客服问"清楚了吗"，客户回"那我得补啥"——
#: 这不是确认，是没听明白去追问。原表把这句判成越界退回了模板的
#: "清楚了，我回去弄"，正好造出用户抱怨的那种"没得到答案却说知道了"。
_ALLOWED_CROSS = {"give": {"give"}, "unsure": {"unsure", "challenge"},
                  "challenge": {"challenge", "unsure"}, "ask": {"ask"},
                  "idle": {"idle", "ask", "challenge"}, "accept": {"accept", "idle", "ask", "challenge"},
                  "end": {"end"}}

_SYSTEM = (
    "你在扮演一位接到客服外呼电话的客户。这是角色扮演，你不是助手。\n"
    "铁律：\n"
    "1. 你只能使用【你的档案】里给出的事实。档案里没有的，就说记不清、不清楚，绝不编。\n"
    "2. 绝不主动报出对方没问的信息；对方问了、而你知道，才答。\n"
    "3. 不要跳出角色，不要说'作为一个AI''我会配合'这类话。\n"
    "4. 说人话：1-2 句，不超过 40 个字，可以有语气词，可以带点情绪。\n"
    "5. 不要复读自己上一句；对方没回答你的问题时，可以再追问，但换个说法。\n"
    "6. 尽量把话说成自己的事：可以带上你自己的品牌、时间、店名、经营情况，"
    "听起来像个真人在讲自己的麻烦。\n"
    "7. 只输出 JSON，不要解释。\n"
    "输出字段：\n"
    '  "say"      ：你这一轮说的话（字符串）\n'
    '  "action"   ：从下面的动作里选一个（字符串）\n'
    '  "feel"     ：你此刻的感觉（听懂了/没听懂/被敷衍/生气/放心/不耐烦，任选）\n'
    '  "got_answer"：你这轮提了问就填 true/false；没提问就填 null\n'
)


def _facts_text(facts):
    if not facts:
        return "（你记不清任何号码、单号）"
    rows = []
    for k, cn in (("phone_tail", "预留手机号后四位"),
                  ("contact_name", "你的姓名"),
                  ("application_id", "申请单号"),
                  ("company_name", "公司全名"),
                  ("shop_name", "店铺名")):
        if facts.get(k):
            rows.append("%s：%s" % (cn, facts[k]))
    return "；".join(rows) or "（你记不清任何号码、单号）"


def _history_text(history, limit=8):
    if not history:
        return "（这是第一句）"
    rows = []
    for h in history[-limit:]:
        rows.append("%s：%s" % ("客服" if h["role"] == "agent" else "你", h["text"]))
    return "\n".join(rows)


class LLMUserSim(object):
    """接口与 `UserSim` 对齐：`say(agent_text) -> dict | None`，`ended` 属性。

    额外字段（规则版没有的）：
        source      "llm" / "rule"   —— 这句到底是模型说的还是模板说的
        veto        打回原因（空串表示模型通过）
        feel        LLM 自评的情绪
        got_answer  LLM 自评"我这一问被回答了没有"
        llm_ms      这一次调用耗时
    """

    def __init__(self, persona, company, card=None, seed=""):
        self.persona = persona or {}
        self.card = card or {}
        self.rule = UserSim(persona, company, seed=seed)   # 数值与边界的唯一来源
        self.facts = dict(self.rule.facts)
        self.blocked_by_missing_fact = list(self.rule.blocked_by_missing_fact)
        self.history = []
        self.turn = 0
        self.ended = False
        self.agent_text = ""
        self.llm_calls = 0
        self.llm_ok = 0
        self.vetoes = []          # [(turn, reason)] 供报告统计
        self.last_answer_ok = None
        self._pending_question = ""

    # -- 对外 -------------------------------------------------------------
    def say(self, agent_text):
        if self.ended:
            return None

        # 规则层照常跑：它维护 patience / trust / given / ended，
        # 也给出"这一轮客户能做什么"的基准动作。**不要绕过它**。
        base = self.rule.say(agent_text)
        if base is None:
            self.ended = True
            return None

        self.turn += 1
        self.agent_text = agent_text or ""
        out = {
            "turn": self.turn,
            "text": base["text"],
            "action": base["action"],
            "asks": base.get("asks") or [],
            "heard": base.get("heard") or "",
            "missed": list(base.get("missed") or []),
            "patience": base.get("patience"),
            "trust": base.get("trust"),
            "note": base.get("note") or "",
            "agent_text": agent_text or "",
            "source": "rule",
            "veto": "",
            "feel": "",
            "got_answer": None,
            "llm_ms": 0,
        }

        if not llm.available():
            self._append_history("agent", agent_text)
            self._append_history("user", out["text"])
            if base["action"] == HANGUP:
                self.ended = True
            return out

        obj, meta = llm.chat_json(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": self._prompt(base, agent_text)}],
            schema=('{"say":"","action":"%s","feel":"","got_answer":false}'
                    % base["action"]),
            temperature=0.85, max_tokens=220, timeout=25.0, tag="user_sim")
        self.llm_calls += 1
        out["llm_ms"] = int(meta.get("ms") or 0)
        if not obj:
            out["veto"] = "llm_%s" % (meta.get("error") or "fail")
            self.vetoes.append((self.turn, out["veto"]))
            self._append_history("agent", agent_text)
            self._append_history("user", out["text"])
            if base["action"] == HANGUP:
                self.ended = True
            return out

        say_text = str(obj.get("say") or "").strip().strip("\"'“”").strip()
        # 动作名一律小写：ACTIONS 里存的是 answer_tail / confirm 这类小写值。
        # 原来写的是 .upper()——于是模型给的任何动作都落不进 ACTIONS，
        # 一律退回 base 动作：跨族越界**从未真正被检查过**，覆盖率永远 1.0。
        action = str(obj.get("action") or "").strip().lower() or base["action"]
        action = action if action in ACTIONS else base["action"]
        # 标签纠偏：模型嘴上在追问，却把 action 标成"确认"。
        # 归因完全按 action 分桶，标错一次就把"客服没答"统计成"客户满意"。
        action = _align_action(say_text, action)
        # 自评说"没听懂/被敷衍"，就不能记成"确认"。
        # 11-11 实测原文：客户说"哦，补这三样东西啊。"，action=ack，
        # 但 feel=没听懂——他其实没搞明白，只是没词儿了。
        # 这种"假确认"正是用户抱怨的"明明没得到答案却说知道了"，
        # 必须在标签上区分开，否则归因永远看不到它。
        feel = str(obj.get("feel") or "")
        if ("没听懂" in feel or "被敷衍" in feel) and _FAMILY.get(action) in ("accept", "idle"):
            action = DENY

        veto = self._veto(say_text, action, base)
        if veto:
            out["veto"] = veto
            self.vetoes.append((self.turn, veto))
            self._append_history("agent", agent_text)
            self._append_history("user", out["text"])
        else:
            out.update({"text": say_text, "action": action, "source": "llm",
                        "feel": str(obj.get("feel") or "")[:12],
                        "got_answer": bool(obj.get("got_answer"))})
            self.llm_ok += 1
            self._append_history("agent", agent_text)
            self._append_history("user", say_text)

        if out["action"] == HANGUP:
            self.ended = True
            self.rule.ended = True
        return out

    # -- prompt -----------------------------------------------------------
    def _prompt(self, base, agent_text):
        p = self.persona
        traits = p.get("traits") or {}
        style = "、".join([str(s) for s in (p.get("style") or [])][:3]) or "普通"
        catch = "、".join([str(s) for s in (p.get("catchphrases") or [])][:3]) or "（无）"
        prof = p.get("profession") or ""
        label = p.get("label") or p.get("temper") or "普通客户"
        patience = int(base.get("patience") or 0)
        trust = float(base.get("trust") or 0.0)

        lines = [
            "【你的档案】",
            self._brief(),
            "",
            "【你知道并能报出的事实】%s" % _facts_text(self.facts),
            "",
            "【你的性格】%s%s；说话习惯：%s；偶尔的口头禅：%s"
            % (label, ("（%s）" % prof) if prof else "", style, catch),
            "【你此刻的状态】耐心 %d/6，对客服的信任 %.2f（0 完全不信，1 完全信）"
            % (patience, trust),
            "【动作说明】%s" % "；".join(
                "%s=%s" % (k, v) for k, v in _ACTION_CN.items()),
            "",
            "【对话记录】",
            _history_text(self.history),
            "",
            "【客服刚说】%s" % (agent_text or "（对方没说话）"),
            "",
            "【这一轮你打算】",
            "规则给出的基准动作是 %s（%s）。你可以换一种更自然的说法或做法，"
            "但不能越过你能知道的信息边界。" % (base["action"],
                                                _ACTION_CN.get(base["action"], "")),
        ]
        if base.get("missed"):
            lines.append("你刚才有几个词没听清（%s），可以让他再说一遍。"
                         % "、".join(base["missed"]))
        return "\n".join(lines)

    def _brief(self):
        from modules import customer_data
        if self.card:
            return customer_data.brief(self.card)
        return ""

    # -- 校验 -------------------------------------------------------------
    def _veto(self, say_text, action, base):
        """返回拒绝原因；空串表示这句可以播。**这是本模块的闸门**。"""
        if not say_text:
            return "empty"
        if len(say_text) > 90:
            return "too_long"
        # 别把自己上一句原样再说一遍。
        # 11-11 实测：客户连续三轮都是"哦，'轻日子'还没过审啊？"，
        # 读起来像卡带，也让"客户在追问"这件事失去了信息量——
        # 真人第二遍会换说法或者直接不耐烦，不会复制粘贴。
        if getattr(self, "_last_user_text", "") and difflib.SequenceMatcher(
                None, say_text, self._last_user_text).ratio() >= 0.85:
            return "self_repeat"
        # 纯数字的回复是合法的：报尾号时真人就说"零零八六"或"0086"。
        # 只在不是"交事实"的动作上要求有中文，否则会把正常的报号码拦掉
        # （第一版就是这么拦掉了一次 ANSWER_TAIL，覆盖率从 1.0 掉到 0.75）。
        if action not in GAVE_FACT_ACTIONS and not re.search(r"[\u4e00-\u9fff]", say_text):
            return "no_chinese"
        if re.search(r"(作为(一个)?(AI|人工智能|语言模型)|我是(一个)?(AI|模型)|我会配合)", say_text):
            return "out_of_role"
        # 跨动作族即越界：客服没问就先报事实，是最典型的假绿来源
        fam_base = _FAMILY.get(base["action"], "idle")
        fam_new = _FAMILY.get(action, "idle")
        if fam_new not in _ALLOWED_CROSS.get(fam_base, {fam_base}):
            return "cross_family(%s->%s)" % (base["action"], action)
        # 报事实的输出必须真的带着档案里的值，且不能夹带别的数字
        if action in GAVE_FACT_ACTIONS:
            key = {ANSWER_TAIL: "phone_tail", ANSWER_NAME: "contact_name",
                   ANSWER_ORDER: "application_id",
                   ANSWER_COMPANY: "company_name"}[action]
            val = str(self.facts.get(key) or "")
            if not val:
                return "give_without_fact"
            if not _mentions(say_text, val):
                # 公司名允许说"字号"：真人不会每句都念"江西省安品商贸中心"，
                # 说成"我们安品"是完全正常的。原判据要求全名原样出现，
                # 于是模型每次说短一点都被打回模板，客户一半的句子变回念稿
                # （11-11 实测 A11 第 1 轮就踩到）。只对**公司名**放宽，
                # 号码与单号仍要求对上数字——那两位错了就是另一单。
                core = _core_entity(val)
                if not (len(core) >= 2 and core in say_text):
                    return "fact_not_matching"
        if not self._numbers_ok(say_text):
            return "invented_number"
        if not self._entities_ok(say_text):
            return "invented_entity"
        return ""

    def _numbers_ok(self, text):
        """说出口的 3 位以上数字，必须是档案里的、或客服刚说过的。

        为什么是 3 位：真人说"后四位 0086"是正常的；但"我 2024 年报的"
        这种年份如果档案里没有，就是编的。2 位以内的数（"两三年""三次"）
        不查，查了会把正常口语全拦掉。
        """
        allowed = set()
        for v in (self.facts or {}).values():
            allowed.update(re.findall(r"\d+", str(v)))
        for v in (self.card.get("knows") or {}).values():
            allowed.update(re.findall(r"\d+", str(v)))
        allowed.update(re.findall(r"\d+", self.agent_text or ""))
        for num in re.findall(r"\d{3,}", text or ""):
            if num not in allowed:
                # 允许"0086"这类带前导零的写法与原值互相匹配
                if num.lstrip("0") not in {a.lstrip("0") for a in allowed}:
                    return False
        return True

    def _entities_ok(self, text):
        """不许说出档案以外的公司名/品牌名。

        只查"带公司/店/品牌后缀的词"：``\\w{2,}(有限公司|旗舰店|专卖店)``
        这样"官方"「审核」这类通用词不会被误伤。

        比对用**字号**而不是全名：档案里写"四川省栖梧云启科技有限公司成都市分公司"，
        客户嘴上说"栖梧云启科技"是同一家（11-11 实测 A16 第 1 轮被这条误拦，
        覆盖率白白掉 8 个点）。字号对不上才算编造。
        """
        t = text or ""
        # 只把"带机构后缀的词"当成公司名候选，'官方''审核'这类通用词不误伤。
        cands = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,12}(?:有限公司|有限责任公司|分公司|"
                           r"旗舰店|专卖店|经营部|商行)", t)
        if not cands:
            return True
        knows = [str(v) for v in (self.card.get("knows") or {}).values() if v]
        knows += [str(v) for v in (self.facts or {}).values() if v]
        cores = [c for c in (_core_entity(k) for k in knows) if len(c) >= 2]
        if not cores:
            return True                      # 档案里没有公司名，无从比对
        for cand in cands:
            core = _core_entity(cand)
            if len(core) < 2:
                continue
            # 候选词前面常带着"我们是/对，就是"这类口语，所以允许包含关系：
            # 只要字号在候选里、或者候选在字号里，就是同一家。
            if any(core in c or c in core for c in cores):
                continue
            return False
        return True

    def _append_history(self, role, text):
        if not text:
            return
        if role == "user":
            self._last_user_text = text
        self.history.append({"role": role, "text": text})
        if len(self.history) > 40:
            del self.history[:len(self.history) - 40]

    # -- 报告 -------------------------------------------------------------
    def report(self):
        return {"llm_calls": self.llm_calls, "llm_ok": self.llm_ok,
                "vetoes": [{"turn": t, "reason": r} for t, r in self.vetoes],
                "coverage": round(self.llm_ok / self.llm_calls, 3) if self.llm_calls else 0.0}


_ADV_SYSTEM = (
    "你在扮演一位接到客服外呼电话的商家。这是角色扮演，你不是助手。\n"
    "铁律：\n"
    "1. 你只能使用【你的档案】里给出的事实。档案里没有的，就说记不清、不清楚，绝不编。\n"
    "2. 绝不主动报出对方没问的信息；对方问了、而你知道，才答。\n"
    "3. 不要跳出角色，不要说'作为一个AI''我会配合'这类话。\n"
    "4. 说人话：1-2 句，不超过 40 个字，可以有语气词、可以有情绪，像在手机那头说话。\n"
    "5. 不要复读自己上一句。\n"
    "6. 这一轮你**必须**按【本轮动作】来，不能改成别的动作（比如不能说成'好的我明白了'）。\n"
    "7. 只输出 JSON，不要解释。\n"
    "输出字段：\n"
    '  "say"      ：你这一轮说的话（字符串）\n'
    '  "feel"     ：你此刻的感觉（听懂了/没听懂/被敷衍/生气/放心/不耐烦，任选）\n'
    '  "got_answer"：你这轮提了问就填 true/false；没提问就填 null\n'
)


class LLMAdvSim(LLMUserSim):
    """对抗客户 + 模型口语：策略仍是 `AdversarySim`，只有措辞交给模型。

    **为什么不照搬 `LLMUserSim` 的"同族可换"**
        这一层的产出一半是"客户表现"，一半是"客服露出的弱点"
        （repeat / no_answer / slot_loss / premature …），而弱点是从**动作序列**
        推出来的。模型把「报尾号」改成「质问」，等级会涨、缺陷会变——
        看上去是模型让训练更狠，其实是模拟器自己造的失败。
        所以这里动作**锁死**，模型只负责把同一句话说得像真人。

    接口与 `AdversarySim.say()` 对齐，额外多三个字段：
        source  "llm" / "rule"    这句是模型说的还是模板说的
        veto    打回原因（空串=模型通过）
        level   这一轮难度（仍由规则层算，模型不参与）
    """

    def __init__(self, profile, company, card=None, seed=""):
        self.persona = profile or {}
        self.card = card or {}
        self.rule = AdversarySim(profile, company, seed=seed)
        self.facts = dict(self.rule.facts)
        self.blocked_by_missing_fact = self.rule.blocked_by_missing_fact
        self.history = []
        self.turn = 0
        self.ended = False
        self.agent_text = ""
        self.llm_calls = 0
        self.llm_ok = 0
        self.vetoes = []
        self.last_answer_ok = None

    # 报告与训练脚本读这几个字段，转交给规则层，保证与纯规则版同源
    @property
    def level(self):
        return self.rule.level

    @property
    def levels(self):
        return self.rule.levels

    @property
    def hits(self):
        return self.rule.hits

    @property
    def patience(self):
        return self.rule.patience

    def say(self, agent_text):
        if self.ended:
            return None
        base = self.rule.say(agent_text)
        if base is None:
            self.ended = True
            return None
        self.turn += 1
        self.agent_text = agent_text or ""
        out = {
            "turn": self.turn,
            "text": base["text"],
            "action": base["action"],
            "level": int(base.get("level") or 1),
            "probe": base.get("probe") or "",
            "given_fact": base.get("given_fact") or "",
            "found": list(base.get("found") or []),
            "patience": self.rule.patience,
            "agent_text": agent_text or "",
            "source": "rule",
            "veto": "",
            "feel": "",
            "got_answer": None,
            "llm_ms": 0,
        }
        # 挂断这一句不交给模型：它是"耐心耗尽"的判定结果，
        # 让模型改写成"那我先挂了哈"会把耐心边界的语义写模糊。
        if base["action"] == HANGUP or not llm.available():
            self._append_history("agent", agent_text)
            self._append_history("user", out["text"])
            if base["action"] == HANGUP:
                self.ended = True
            return out

        obj, meta = llm.chat_json(
            [{"role": "system", "content": _ADV_SYSTEM},
             {"role": "user", "content": self._prompt(base, agent_text)}],
            schema=('{"say":"","action":"%s","feel":"","got_answer":null}'
                    % base["action"]),
            temperature=0.85, max_tokens=200, timeout=25.0, tag="adv_sim")
        self.llm_calls += 1
        out["llm_ms"] = int(meta.get("ms") or 0)
        if not obj:
            out["veto"] = "llm_%s" % (meta.get("error") or "fail")
            self.vetoes.append((self.turn, out["veto"]))
            self._append_history("agent", agent_text)
            self._append_history("user", out["text"])
            return out

        say_text = str(obj.get("say") or "").strip().strip("\"'“”").strip()
        # 模型被允许声明动作，只是为了**被拦**：声明与规则层不一致就是越界。
        # 不让它声明、只按规则层的动作走，会在日志里丢掉"模型想改动作"这件事，
        # 拦截次数变成 0，看起来模型从没越界。
        # 动作名一律小写：ACTIONS 里存的是 answer_tail / confirm 这类小写值。
        # 原来写的是 .upper()——于是模型给的任何动作都落不进 ACTIONS，
        # 一律退回 base 动作：跨族越界**从未真正被检查过**，覆盖率永远 1.0。
        action = str(obj.get("action") or "").strip().lower() or base["action"]
        action = action if action in ACTIONS else base["action"]
        veto = self._veto(say_text, action, base)
        if veto:
            out["veto"] = veto
            self.vetoes.append((self.turn, veto))
            self._append_history("agent", agent_text)
            self._append_history("user", out["text"])
        else:
            out.update({"text": say_text, "source": "llm",
                        "feel": str(obj.get("feel") or "")[:12],
                        "got_answer": obj.get("got_answer")})
            self.llm_ok += 1
            self._append_history("agent", agent_text)
            self._append_history("user", say_text)
        return out

    def _prompt(self, base, agent_text):
        p = self.persona
        traits = p.get("traits") or {}
        style = "、".join(str(s) for s in (p.get("style") or [])[:3]) or "普通"
        label = p.get("label") or "普通商家"
        extra = []
        if traits.get("patience"):
            extra.append("耐心 %d" % int(traits["patience"]))
        if traits.get("mishear_every"):
            extra.append("耳朵不太好，容易听不清")
        soft = ""
        if base.get("found"):
            # 不直接把缺陷码告诉模型，换成客户视角的一句体感。
            soft = "你觉得对方说话有点绕、像在念稿子，可以不客气一点。"
        lines = [
            "【你的档案】",
            self._brief() or "（没有额外档案，你只记得自己的号码和单号）",
            "",
            "【你知道并能报出的事实】%s" % _facts_text(self.facts),
            "",
            "【你是谁】%s；说话习惯：%s%s" % (label, style,
                                         ("；" + "；".join(extra)) if extra else ""),
            "【现在】第 %d 轮，你的火气等级 L%d（数字越大越不耐烦）"
            % (self.turn, base.get("level") or 1),
            "",
            "【对话记录】",
            _history_text(self.history),
            "",
            "【客服刚说】%s" % (agent_text or "（对方没说话）"),
            "",
            "【本轮动作】%s（%s）" % (base["action"], _ACTION_CN.get(base["action"], "")),
            "把这个动作说成你自己的话，别改动作本身。",
        ]
        if soft:
            lines.append(soft)
        return "\n".join(lines)

    def _veto(self, say_text, action, base):
        # 动作锁死：模型换了动作就直接退回模板那句。
        if action != base["action"]:
            return "action_locked(%s->%s)" % (base["action"], action)
        return LLMUserSim._veto(self, say_text, action, base)


def _align_action(say_text, action):
    """让动作标签跟"说出来的话"一致。只在不一致时改，一致时原样返回。

    动机很具体（实测踩到）：客服问"您清楚了吗"，模型回了
    "授权链路不完整？那我得补啥材料？"，却把 action 标成 confirm。
    归因按 action 分桶，这一标就把"客户没听懂、在追问"记成了
    "客户已确认"——正是用户抱怨的"明明没得到答案却说知道了"。
    """
    if action not in (CONFIRM, YES_COMPANY, ACK):
        return action
    t = say_text or ""
    if not re.search(r"[？?]|什么|怎么|哪|多少|为啥|凭什么|啥", t):
        return action
    if re.search(r"材料|补什么|补啥|哪儿补|在哪|怎么补", t):
        return ASK_MATERIAL
    if re.search(r"收费|要钱|花钱|费用|钱吗", t):
        return ASK_FEE
    if re.search(r"谁|哪来的|凭什么|官方|你们是", t):
        return ASK_WHY
    return DENY


#: 机构后缀。切分用它，比对时先按它拆段，段内的行政区再单独剥。
_ORG_RE = re.compile(r"(股份有限公司|有限责任公司|有限公司|分公司|分店|公司|商贸中心|"
                     r"商贸|百货|经营部|商行|旗舰店|专卖店|中心|店)")
_ADMIN_HEAD = re.compile(r"^[\u4e00-\u9fff]{2,4}?(省|自治区|特别行政区|市|区|县|镇|州)")


def _core_entity(name):
    """公司名里真正用来辨认的那截：拆掉机构后缀，再剥掉开头的行政区。

    "江西省安品商贸中心" → "安品"；"北京市安食商贸有限公司" → "安食"；
    "四川省栖梧云启科技有限公司成都市分公司" → "栖梧云启科技"。

    只用于"公司名"这一种比对，别拿它放宽号码类的校验——
    号码差一位就是另一单，公司名少说两个字还是同一个人。
    """
    s = str(name or "")
    kept = []
    for seg in _ORG_RE.split(s):
        if not seg or _ORG_RE.fullmatch(seg):
            continue
        for _ in range(3):                     # "广州市天河区丰禾" 要剥两层
            stripped = _ADMIN_HEAD.sub("", seg)
            if stripped == seg:
                break
            seg = stripped
        kept.append(seg)
    return "".join(kept).strip("的")


def _mentions(text, value):
    """客户报值时可能换写法："0086" 说成 "零零八六"、"00八六"。

    这里只做数字形态的宽松匹配；姓名/公司名要求原样出现。
    """
    if value and value in text:
        return True
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return False
    # 中文数字读法
    cn = {"0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
          "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"}
    if "".join(cn.get(c, c) for c in digits) in text:
        return True
    # 只在末尾几位上匹配（"后四位是 0086"）
    return len(digits) >= 3 and digits[-3:] in re.sub(r"\D", "", text)


def build(persona, company, card=None, seed=""):
    """工厂：有 key 且没被禁用时给 LLM 版，否则直接给规则版。

    调用方（selfplay.py）不需要判断环境——这里判一次，别处都省事。
    """
    if not llm.available():
        return UserSim(persona, company, seed=seed)
    return LLMUserSim(persona, company, card=card, seed=seed)


def build_adversary(profile, company, card=None, seed=""):
    """对抗画像的工厂：有 key 给「模型口语版」，否则给纯规则版。

    训练回放的演示走这里；`scripts/adversarial_evolve.py` 的跑批默认**不走**——
    跑批是闸门，要可复现，见该文件 run_one() 的说明。
    """
    if not llm.available():
        return AdversarySim(profile, company, seed=seed)
    return LLMAdvSim(profile, company, card=card, seed=seed)


if __name__ == "__main__":
    sys.stderr.write("这个模块由 scripts/selfplay.py 调用，不单独运行\n")
