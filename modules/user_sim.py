"""真实用户模拟器 —— 自进化（L2）的前提。

为什么必须重做模拟器：
    原来的 `eval/personas_generated.json` 是**固定台词回放**：每个 persona 按
    "开场→核身→诉求→异议→收尾"排好句子，跑批时顺着念，完全不看客服上一句说了什么。
    后果被 `docs/28` 自己记录下来了：40 条里 24 条失败，其中 5 个人设"台词里
    根本没有可核身信息"却要求闭环——**失败是模拟器造出来的，不是系统缺陷**。
    拿这种失败去聚类、去调话术，等于对着哈哈镜改妆。

真实用户与回放脚本的三点差别，也是本模块的三条硬约束：

    1. **受知**（epistemic limit）：他只知道自己知道的。
       手机尾号、联系人姓名这些事实从**本通电话真正拨出去的那条档案**里来；
       档案里没有的，他不知道；画像声明 `knows=False` 的，他也答不出来。
       —— 绝不出现"模拟器全知全能、随手报出正确尾号"这种假绿。

    2. **受听**（perceptual limit）：他只对**听到的那句话**反应。
       本模块拿不到 stage / intent / slots，只能从客服话术里推断"他刚被问了什么"
       （`detect_asks`）。听错、听漏是按 `hearing` 概率发生的，而且**同一要素只错一次**——
       真人让人再说一遍通常就听清了，不会一直听不清。
       —— 这样"客服没问、用户却先报了尾号"的诡异剧情在结构上不可能发生。

    3. **受耐心**（patience/trust）：重复同一句、反复要同一条信息、答非所问，
       都会掉耐心和信任；耗尽就挂断。核身之前先问"你哪来的号码"是常态。
       —— 挂断是**被逼出来的**，不是脚本写死的，所以能反映客服话术的真实质量。

还有一条工程约束：**确定性**。同一 persona、同一段客服话术，跑出来的用户话必须
逐字节一致，否则回归没有基准。随机数按 `(seed, turn)` 单独播种，
不用全局 random。

本模块**不模拟**什么（别误读）：
    - 不模拟声学层误差（那是 ASR 的事，见 modules/voice.py）；
    - 不模拟方言口音、背景噪声（VAD 语料负责那一层）；
    - 不做"自由发挥"的开放式生成——所有动作都来自有限动作集，
      因为动作名要作为失败归因的标签用。自由文本没法归因。
"""

import random

# ---------------------------------------------------------------------------
# 动作集。**这是标签，不是台词**：归因与聚类都按它分组，所以只增不改名。
# ---------------------------------------------------------------------------
ANSWER_TAIL = "answer_tail"            # 报出预留手机号后四位
ANSWER_NAME = "answer_name"            # 报出联系人姓名
ANSWER_ORDER = "answer_order"          # 报出申请单编号
ANSWER_COMPANY = "answer_company"      # 报出公司名（替代核身路径）
CANT_REMEMBER = "cant_remember"        # 知道有这回事，但记不住具体值
REFUSE_INFO = "refuse_info"            # 不愿提供（质疑来电身份）
ASK_WHY = "ask_why"                    # 反问"你哪来的号码/你谁啊"
REASK_ALREADY_TOLD = "reask_already_told"   # "我刚不是说了吗"（客服重复要同一条）
ASK_MATERIAL = "ask_material"          # 追问要补什么材料
ASK_FEE = "ask_fee"                    # 追问收不收费
CONFIRM = "confirm"                    # 表示清楚了/明白了
DENY = "deny"                          # 表示没明白/不认可
YES_COMPANY = "yes_company"            # 确认"我就是这家公司的XX"
NO_WRONG_PERSON = "no_wrong_person"    # "打错了/没这个人"
MISHEAR = "mishear"                    # "啊？你说什么"（听错后的自然反应）
ACK = "ack"                            # 应一声，不推进
OFF_TOPIC = "off_topic"                # 絮叨跑题
HANGUP = "hangup"                      # 主动结束通话

#: 用户"问出口但没得到答复"时最掉耐心。真人反问一句却没人回答，三两下就烦了。
_QUESTION_ACTIONS = (ASK_WHY, ASK_MATERIAL, ASK_FEE)
#: 客服话里出现这些词，才算"正面回答了那个追问"。
_ANSWER_CUES = {
    ASK_WHY: ("官方", "审核专员", "不是推销", "资质审核", "商家后台"),
    ASK_MATERIAL: ("补充", "材料", "重新提交", "上传"),
    ASK_FEE: ("不收取", "免费", "费用", "不收费"),
}

#: 会交出事实的动作 —— 归因时用它判断"用户已经给了，客服有没有接住"。
GAVE_FACT_ACTIONS = (ANSWER_TAIL, ANSWER_NAME, ANSWER_ORDER, ANSWER_COMPANY)

#: 每种事实对应的客服问法特征。刻意用**字面**而不是 stage：
#: 真实商家听不出"现在进入 IDENTITY 阶段"，只能听出"他让我报号码"。
_ASK_PATTERNS = (
    ("tail", ("后四位", "后 4 位")),
    ("order", ("申请单编号", "单号", "编号后六位")),
    ("name", ("您的姓名", "怎么称呼", "联系人姓名", "报一下姓名", "说一下姓名")),
    # 11-21：收尾问句口径改过。原来靠"需要我帮您转接人工客服吗"让模拟客户
    # 认出"客服在等我表态"——但系统根本没有转人工功能，那是虚假承诺，
    # 已改成 self-service 热线 + 中性确认问句。特征表必须跟着换，
    # 否则模拟客户听不见问句、一路 ACK/OFF_TOPIC，12 画像里 10 个不闭环。
    ("confirm", ("清楚了吗", "明白吗", "清楚没有", "还有疑问",
                 "这样说明白了吗", "回一句清楚了", "这样行吗",
                 "这么说清楚了吗", "听明白了吗",
                 # 兼容历史话术（老版本/存档回放仍可能出现）
                 "转接人工", "转人工", "帮您转接")),
    ("reason", ("没有通过审核", "驳回原因", "被驳回", "没通过")),
    ("material", ("需要补充", "补什么", "补充材料", "重新提交")),
    ("fee", ("收费", "要钱", "费用", "花钱")),
)


def detect_asks(text):
    """从客服这一句里推断"他被问了什么/听到了什么"。

    返回按**优先级**排好的要素名列表。优先级是刻意的：一句"请报一下手机号后四位"
    同时含"手机号"与"后四位"，只能算一次；而"请问您是XX公司的XX吗"要优先于
    后面附带的"我这边有一笔资质申请要核实"，否则用户会先答业务而不是先确认身份。

    为什么不用正则做完整句法：客服话术是模板驱动的，短句为主；这里要的是
    **稳定可预期**，不是覆盖率。认不出来就走 ACK/MISHEAR，反而更接近真人。
    """
    t = text or ""
    out = []
    # 身份确认句：官方话术的固定格式就是"请问您是（公司名）的（身份）吗？"
    if ("请问您是" in t and "吗" in t) or ("您是" in t and "公司" in t and "吗" in t):
        out.append("company")
    for key, words in _ASK_PATTERNS:
        if any(w in t for w in words):
            out.append(key)
    return out


def _digits_to_cn(s):
    """数字转中文读法。真人报号码有时念数字、有时念汉字，ASR 两种都要能吃。"""
    table = {"0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
             "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"}
    return "".join(table.get(c, c) for c in str(s))


def derive_facts(persona, company):
    """把"这个人知道什么"落到本通电话的真实档案上。

    关键设计：**不知道就不给值**。`knows` 里标 false 的，facts 里索性不出现这个键，
    渲染层拿不到值就只能说"记不住"。这样"模拟器编造事实"在结构上不可能发生。

    company 来自 `start_call()` 回传的 outbound_target（本通电话实际拨的那家），
    不是随机编的——否则用户报的尾号与系统档案对不上，会把"核身失败"误判成系统缺陷。
    """
    c = company or {}
    knows = persona.get("knows") or {}
    facts = {}
    if knows.get("phone_tail") and c.get("phone_tail"):
        facts["phone_tail"] = str(c["phone_tail"])
    if knows.get("contact_name") and c.get("contact_name"):
        facts["contact_name"] = str(c["contact_name"])
    if knows.get("application_id") and c.get("application_id"):
        facts["application_id"] = str(c["application_id"])
    if knows.get("company_name") and c.get("company_name"):
        facts["company_name"] = str(c["company_name"])
    if knows.get("shop_name") and c.get("shop_name"):
        facts["shop_name"] = str(c["shop_name"])
    return facts


class UserSim(object):
    """一个有记忆、有情绪、有知识边界的商家。

    用法：
        sim = UserSim(persona, company, seed="S03")
        out = sim.say(agent_text)        # 返回这一轮他说的话（含动作标签）
        sim.ended                        # 他是否已经挂断

    `out` 的字段：
        text     他说出口的话（就是要喂给 ASR 之后那一步的文本）
        action   动作标签（归因/聚类用）
        asks     他从客服那句里听出来的要素
        patience 剩余耐心
        trust    当前信任
        note     人话解释（报告里直接展示，便于复核）
    """

    def __init__(self, persona, company, seed=""):
        self.p = persona or {}
        self.seed = "%s|%s" % (self.p.get("id", "?"), seed)
        self.company = company or {}
        self.facts = derive_facts(self.p, company)
        traits = self.p.get("traits") or {}
        self.hearing = float(traits.get("hearing", 1.0))
        self.patience = int(traits.get("patience", 6))
        self.trust = float(traits.get("trust0", 0.3))
        self.trust_gate = float(traits.get("trust_gate", 0.2))
        self.verbosity = float(traits.get("verbosity", 0.3))
        # 同一要素只可能听错一次：真人让人重复一遍通常就听清了。
        self._heard_attempted = set()
        self.given = set()
        self.asked = set()          # 他问过客服什么（问过就不再重复问）
        self.turn = 0
        self.ended = False
        self.last_agent = ""
        self.repeat = 0
        self._last_action = ""
        # 知道、但档案里没有对应值 —— 这是**用例不可行**，不是系统的错。
        self.blocked_by_missing_fact = []
        for k in ("phone_tail", "contact_name", "application_id", "company_name"):
            if (self.p.get("knows") or {}).get(k) and not self.facts.get(k):
                self.blocked_by_missing_fact.append(k)

    # -- 感知 -------------------------------------------------------------
    def _perceive(self, agent_text):
        """听他听到的样子。返回 (听到的文本, 听错的要素列表)。

        `hearing` 是"这一句能听清"的概率。听不清时只把**关键要素词**糊掉，
        不把整句打乱——真人听漏的往往正是那个关键名词（"后四位"），
        而不是所有字。糊掉后 detect_asks 认不出来，他就会反问，链路自然多一轮。
        """
        text = agent_text or ""
        if self.hearing >= 1.0:
            return text, []
        missed = []
        for term in ("后四位", "申请单编号", "怎么称呼", "您的姓名", "清楚了吗"):
            if term not in text:
                continue
            if term in self._heard_attempted:
                continue
            self._heard_attempted.add(term)     # 只可能错一次
            if self.rng_float() > self.hearing:
                text = text.replace(term, "……")
                missed.append(term)
        return text, missed

    def rng_float(self):
        """按 (seed, turn) 播种的独立随机源 —— 同输入必得同输出。"""
        return random.Random("%s#%d" % (self.seed, self.turn)).random()

    def _pick(self, options):
        if not options:
            return ""
        return options[random.Random("%s#%d" % (self.seed, self.turn)).randrange(len(options))]

    # -- 决策 -------------------------------------------------------------
    def _willing(self, key):
        """愿不愿说。on_ask=问了就说；refuse=先质疑；never=绝不说。"""
        return (self.p.get("willing") or {}).get(key, "on_ask")

    def _knows(self, key):
        return bool(self.facts.get(key))

    def _trust_ok(self, key):
        """这条信息现在愿不愿意说。

        `willing` 三档的区别被这里落成"信任门槛"，不是"说/不说"的硬开关：
            on_ask  达到 trust_gate 就说（普通人）
            refuse  要多一层安抚（质疑型：问一次"你哪来的号码"才肯给）
            never   永远不说（真拒绝）
        上一版把 refuse 写成"永远拒绝"，结果商家被安抚两次了还在反问，
        这不是"谨慎"，是模拟器不会改变主意——真人会被说服的。
        """
        w = self._willing(key)
        if w == "never" or not self._knows(key):
            return False
        need = self.trust_gate if w == "on_ask" else max(self.trust_gate, 0.8)
        return self.trust >= need

    def _decide(self, asks, missed):
        """动作决策。**顺序即优先级**，见模块顶部三条约束。"""
        goal = self.p.get("goal") or "confirm"

        # 0) 耐心耗尽 —— 挂断是被逼出来的，不是脚本写死的
        if self.patience <= 0:
            return HANGUP, "耐心已耗尽"

        # 1) 听漏了，且这句没听出任何可执行的要求 → 反问（真人第一反应）
        if missed and not asks:
            return MISHEAR, "没听清「%s」" % "、".join(missed)

        # 2) 身份确认句：先确认"是不是我"
        if "company" in asks:
            if goal == "wrong_person":
                return NO_WRONG_PERSON, "画像设定：打错人"
            return YES_COMPANY, "确认是本人"

        # 3) 要手机号后四位
        if "tail" in asks:
            if "phone_tail" in self.given:
                self.patience -= 1
                return REASK_ALREADY_TOLD, "客服重复要同一条信息"
            if self._trust_ok("phone_tail"):
                return ANSWER_TAIL, "报出尾号"
            # 给不出/不愿给尾号时，**看这句话里还有没有别的路**。
            # 真人听到"报尾号，或者说姓名也行"，会挑自己能答的那条：
            # "号码我记不住，我姓王"——上一版死盯尾号，把可闭环的人跑成挂断，
            # 这是模拟器的缺陷，不是客服的。
            if ("name" in asks and self._trust_ok("contact_name")):
                return ANSWER_NAME, "尾号给不了，改报姓名（客服给了替代路径）"
            if ("order" in asks and self._trust_ok("application_id")):
                return ANSWER_ORDER, "尾号给不了，改报单号"
            if self._willing("phone_tail") == "never" or (
                    not self._knows("phone_tail") and self._willing("contact_name") == "never"):
                return REFUSE_INFO, "画像设定：不提供身份信息"
            if not self._knows("phone_tail"):
                return CANT_REMEMBER, "记不住尾号"
            return ASK_WHY, "信任不足（%.2f<%.2f）" % (self.trust, self.trust_gate)

        # 4) 要姓名
        if "name" in asks:
            if "contact_name" in self.given:
                self.patience -= 1
                return REASK_ALREADY_TOLD, "客服重复要姓名"
            if not self._knows("contact_name"):
                return CANT_REMEMBER, "记不住全名"
            if self._willing("contact_name") == "never":
                return REFUSE_INFO, "画像设定：不报姓名"
            if not self._trust_ok("contact_name"):
                return ASK_WHY, "信任不足（%.2f<%.2f）" % (self.trust, self.trust_gate)
            return ANSWER_NAME, "报出姓名"

        # 5) 要申请单编号
        if "order" in asks:
            if not self._knows("application_id"):
                return CANT_REMEMBER, "记不住申请单号"
            return ANSWER_ORDER, "报出申请单号"

        # 6) 复述确认
        if "confirm" in asks:
            if goal in ("confirm", "question_only") and self.given:
                return CONFIRM, "确认已清楚"
            if goal in ("refuse", "offtopic"):
                return DENY, "画像设定：不确认"
            return DENY, "还没听明白（尚未拿到任何进度）"

        # 7) 客服讲了驳回原因/材料 → 真人会追问"那我要补啥/收钱吗"
        if "material" in asks and "material" not in self.asked:
            self.asked.add("material")
            return ASK_MATERIAL, "追问要补什么"
        if "fee" in asks and "fee" not in self.asked:
            self.asked.add("fee")
            return ASK_FEE, "追问收不收费"
        if "reason" in asks and goal == "question_only" and "reason" not in self.asked:
            self.asked.add("reason")
            return ASK_MATERIAL, "追问整改动作"

        # 8) 什么都没听出来
        if self.rng_float() > 0.55 + 0.3 * (1 - self.verbosity):
            return OFF_TOPIC, "絮叨一句（画像 verbosity 高）"
        return ACK, "应一声，不推进"

    # -- 表达 -------------------------------------------------------------
    def _render(self, action, note):
        """把动作说成人话。**只有这里能产出事实值**，别的分支一律不碰 self.facts。"""
        style = self.p.get("style") or []
        pre = self._pick(style) if style else ""
        catch = self.p.get("catchphrases") or []

        def wrap(s):
            out = (pre + "，" + s) if pre else s
            # 性格口头禅：只有**显式性格画像**带这个字段时才多取一次随机数。
            # 既有 10 类画像没有它，随机序列一个字节都不能变——否则 L2 的
            # 8/10 基线会被"顺手加个功能"悄悄改写，闸门就失去意义了。
            if catch and self.rng_float() < float(self.p.get("catchphrase_rate", 0.6)):
                out = self._pick(catch) + "，" + out
            return out

        if action == ANSWER_TAIL:
            v = self.facts["phone_tail"]
            variants = [
                v,
                _digits_to_cn(v),
                "后四位是 %s" % v,
                "我看看啊……%s，对，%s" % (v, _digits_to_cn(v)),
            ]
            return wrap(self._pick(variants))
        if action == ANSWER_NAME:
            v = self.facts["contact_name"]
            return wrap(self._pick(["我姓%s" % v[0], "我叫%s" % v, "就是%s" % v]))
        if action == ANSWER_ORDER:
            return wrap("单号后六位是 %s" % self.facts["application_id"][-6:])
        if action == ANSWER_COMPANY:
            return wrap("对，%s" % self.facts.get("company_name", "是我们"))
        if action == CANT_REMEMBER:
            return wrap(self._pick([
                "号码是店里注册的，我一时真想不起来",
                "那号在我儿子手机上，我记不住",
                "我这儿没纸笔，记不得那几个数",
            ]))
        if action == REFUSE_INFO:
            return wrap(self._pick([
                "我凭什么告诉你，你先说你是哪个部门的",
                "这种电话我接多了，号码我不会报",
                "你先别问号，你说说这电话到底什么事",
            ]))
        if action == ASK_WHY:
            return wrap(self._pick([
                "你哪来的我号码？",
                "你们是官方的吗，我怎么信你",
                "先说说你是干什么的，再说号码",
            ]))
        if action == REASK_ALREADY_TOLD:
            return wrap(self._pick([
                "我刚不是说了吗，就那几个数",
                "我报过了呀，你怎么还问",
            ]))
        if action == ASK_MATERIAL:
            return wrap(self._pick([
                "那我具体要补什么材料？",
                "补完了是重新提交一遍吗？",
                "这个得去哪儿弄，后台哪个入口？",
            ]))
        if action == ASK_FEE:
            return wrap(self._pick(["这个收钱吗？", "要交费不？", "重新提交还要花钱吗"]))
        if action == CONFIRM:
            return wrap(self._pick(["清楚了，我回去弄", "明白了", "知道了，我按你说的补"]))
        if action == DENY:
            return wrap(self._pick(["我还是没听明白", "不太清楚，你再说一遍", "这我搞不懂"]))
        if action == YES_COMPANY:
            return wrap(self._pick(["对，是我", "是我，你说", "嗯，我就是"]))
        if action == NO_WRONG_PERSON:
            return wrap(self._pick(["不是，你打错了吧", "没这个人", "我们家没这个公司"]))
        if action == MISHEAR:
            return wrap(self._pick(["啊？你说什么", "信号不好，你再说一遍", "你刚说啥"]))
        if action == OFF_TOPIC:
            return wrap(self._pick([
                "这两天后台老卡，你顺便帮我看看",
                "我跟你说啊，上次那个审核也是拖了半个月",
            ]))
        if action == HANGUP:
            return wrap(self._pick(["行了行了，我知道了，先这样", "不说了，我忙着呢"]))
        return wrap(self._pick(["嗯", "你说", "听着呢"]))

    # -- 对外 -------------------------------------------------------------
    def say(self, agent_text):
        """看客服说了什么，回一句。已挂断则返回 None。"""
        if self.ended:
            return None

        self.turn += 1
        heard, missed = self._perceive(agent_text)

        # 客服原地重复同一句 → 耐心掉得更快（真人最烦这个）
        if agent_text and agent_text == self.last_agent:
            self.repeat += 1
            self.patience -= 2
        else:
            self.repeat = 0
        self.last_agent = agent_text or ""

        # 客服自报身份/说明来意 → 信任上升（这是"凭什么信你"的解法）
        if any(w in (agent_text or "") for w in ("资质审核", "审核专员", "官方", "不是推销")):
            self.trust = min(1.0, self.trust + 0.35)
        # 客服正面回答了用户的追问 → 也算加信任
        if self.asked and any(w in (agent_text or "") for w in ("不收取任何费用", "免费", "需要补充", "重新提交")):
            self.trust = min(1.0, self.trust + 0.15)

        # 上一轮我问了问题，这一轮客服没正面回答 → 掉耐心。
        # 为什么要单独一条：只靠"重复同一句"掉耐心，遇到"换着说法绕圈子"的客服
        # 就永远耗不尽耐心，急躁画像也就永远触发不了——那等于没测到这类人。
        if self._last_action in _QUESTION_ACTIONS:
            cues = _ANSWER_CUES.get(self._last_action, ())
            if not any(c in (agent_text or "") for c in cues):
                self.patience -= 1

        action, note = self._decide(detect_asks(heard), missed)

        if action in GAVE_FACT_ACTIONS:
            if action == ANSWER_TAIL:
                self.given.add("phone_tail")
            elif action == ANSWER_NAME:
                self.given.add("contact_name")
            elif action == ANSWER_ORDER:
                self.given.add("application_id")
            elif action == ANSWER_COMPANY:
                self.given.add("company_name")

        if action == HANGUP:
            self.ended = True
        self._last_action = action

        return {
            "turn": self.turn,
            "text": self._render(action, note),
            "action": action,
            "asks": detect_asks(heard),
            "heard": heard,
            "missed": list(missed),
            "patience": self.patience,
            "trust": round(self.trust, 2),
            "note": note,
            "agent_text": agent_text or "",
        }
