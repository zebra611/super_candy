"""M3 对话编排 —— 规则版状态机（第一版真实现）。

职责：
    听懂用户说了什么（规则意图）→ 按状态机推进六个环节 → 缺什么问什么
    → 需要业务数据就查 → 拿到明确意图后触发回写。

这一版是什么：
    用关键词规则做意图判断，**不调用大模型**。它的价值是：
    1. 让整条链路有真实的业务语义，不再"每轮推进一格"；
    2. 给角色 2 一个能跑、能测的基线，之后把 _classify 换成模型输出即可；
    3. 让角色 3 有东西可评（30 条种子用例能跑出真实分数）。

这一版不是什么：
    不处理同义改写、不理解上下文指代、不支持用户跳步。
    下面每个处理器的 "_todo" 就是角色 2 的开工清单。

设计约定（与 docs/02_状态机定义.md 一致）：
    - 状态机管流程，话术层管措辞；
    - 身份未确认时，绝不透露订单、金额、地址等任何个人信息；
    - "明确拒绝"必须回写（业务侧要知道结果），"要求挂断"不回写；
    - 含糊回答不算同意，二次追问仍含糊则记 unknown 并回写。
"""

import re

import config_loader
from modules import kb, nlg, tools

# ---------------------------------------------------------------------------
# 意图关键词表
# 维护说明：这些词是"规则版"的全部智能所在。角色 2 接入模型后，
# 这张表可以退化为兜底（模型置信度低时再用规则）。
# ---------------------------------------------------------------------------

# 精确匹配的肯定回答（避免"不好意思"里的"好"被误判成同意）
#
# 注意这里**没有"嗯"**。以前"嗯"/"嗯嗯"在表里，CONFIRM 阶段用户"嗯"一声就被判成
# 明确同意并直接回写——docs/02 写的是"含糊回答不算同意"，这条规则和文档是反的。
# 单个语气词既可能是同意，也可能只是"我还在听"，证据强度不够，所以挪进 PURE_FILLER，
# 由置信度分级判为含糊，走二次追问（见下方的 classify）。
YES_EXACT = {"是", "对", "好", "行", "可以", "是的", "对的",
             "好的", "行吧", "是啊", "同意", "要", "需要", "没问题"}
# 包含即算肯定（长句里带这些词）
YES_CONTAINS = ("同意", "没问题", "那就办", "办吧", "可以可以", "就这样", "要了", "需要",
                "那我要", "就要", "我同意")

NO_EXACT = {"不", "不要", "不用", "不了", "算了", "拒绝", "没兴趣", "不需要", "别打"}
NO_CONTAINS = ("不要", "不用", "不需要", "没兴趣", "算了", "别打", "拒绝", "不办", "不了")

# 否认本人 / 打错电话
DENY_PERSON = ("不是本人", "我不是", "打错", "他不在", "不是他", "你找错了")

# 要求结束通话（与"拒绝"不同：拒绝要回写，挂断不回写）
HANGUP_WORDS = ("挂了", "先挂", "忙着", "没空", "不聊了", "别打了", "不要再打")

# 情绪信号
ANGRY_WORDS = ("投诉", "骗子", "烦", "垃圾", "滚", "气死", "恶心", "骚扰")

# 提问信号
ASK_MARKS = ("吗", "？", "?", "多少", "多久", "怎么", "什么", "能不能", "是不是", "可以吗")

# 要求转人工。**故意不含"投诉"**：投诉是情绪信号（见 ANGRY_WORDS），
# 要致歉承接后继续解决问题，不能一句"帮您转人工"就把电话挂掉。
# 评测用例 R04 就是"我要投诉你们"之后还要走完五个环节，含投诉会直接判负。
HANDOFF_WORDS = ("转人工", "找人工", "人工客服", "转客服", "转接人工",
                 "要真人", "真人接", "叫你们领导", "找你们领导")

# 弱肯定：嘴上答应了，但明显没拿定主意。
# "好吧""应该可以""差不多"这类回答如果直接当同意回写，业务侧会拿着一份
# 用户并不认账的工单去做后续动作，退订/投诉就从这儿来。所以给它们低于阈值的
# 置信度，在 CONFIRM 阶段触发一次追问，而不是直接落库。
WEAK_YES = ("好吧", "应该可以", "应该行", "应该吧", "差不多", "大概", "可能吧", "看看吧")

# ---------------------------------------------------------------------------
# 含糊输入的词表（T1：意图置信度分级）
#
# 为什么需要这一层：关键词规则只有"命中/没命中"两态，而真实通话里大量输入是
# 半句话——"你说是就是吧""嗯……""随便"。旧规则对这两种输入的处理都是错的，
# 但错法不同：
#     命中一个"嗯"  → 被当成同意，回写一个用户从没给过的结论（错在证据太弱）
#     一条都没命中  → 被当成诉求，直接推方案（错在用户会觉得客服在自说自话）
# 所以这里把"证据强度"显式化成 0~1 的置信度，再由各阶段决定低分要不要追问。
# ---------------------------------------------------------------------------

# 纯语气词：整句只有这些的时候，唯一的语义信息是"我还在听"，不是"我同意"。
PURE_FILLER = {"嗯", "嗯嗯", "啊", "哦", "喔", "呃", "额", "唉", "诶", "嘿", "哈"}

# 敷衍/含糊：说了等于没说。用**包含**匹配而不是整句匹配，
# 因为真实说法是"我也不太清楚""这个我还真不知道"，整句永远对不上。
VAGUE_MARKS = ("不太清楚", "不清楚", "不知道", "不晓得", "不记得", "说不好",
               "说不准", "不确定", "看情况", "考虑一下", "再想想", "让我想想",
               "你看着办", "你说了算", "你说是就是", "你定吧", "随便",
               "再说吧", "到时候再说", "无所谓", "都行", "都可以",
               # 连说两遍的"行吧行吧""行了行了"是不耐烦，不是同意。
               # 注意不能把单个"行吧"加进来——它在 YES_EXACT 里，是明确同意；
               # 也不能把"行吧"加进 YES_CONTAINS，否则"不行吧"会被判成同意。
               "行吧行吧", "行了行了", "知道了", "好知道了")

# 证据强度（0~1）。数字不是拍脑袋来的，是按"这句话能不能单独支撑一次回写"定的：
#   EXACT     整句就是一个标准答案，"同意"两个字不会再有别的解释
#   DIGIT     报出 4 位尾号，是强身份证据（但可能是别人的号，所以不给满分）
#   CONTAINS  长句里顺带带了标准答案，"行吧那你办吧"里"办吧"也可能是抱怨
#   WEAK      弱肯定，"好吧"经常等于"我不想争了"
#   VAGUE     命中含糊词表
#   NONE      一条规则都没命中：可能是自由表达（"我要退款"），也可能是没听清
CONF_EXACT = 0.95
CONF_DIGIT = 0.93
CONF_CONTAINS = 0.75
CONF_WEAK = 0.50
CONF_VAGUE = 0.25
CONF_NONE = 0.45

# 低于这条线的不算"明确同意"。只用在 CONFIRM——别处用它会误伤自由表达：
# "我要退款"一条规则都不命中（conf 0.45），但它显然该继续推进，不该被追问。
YES_MIN_CONF = 0.60

# 同一个阶段连续追问几次就换人。对应 T2"同一槽位连续追问 ≤2 次，第 3 次转兜底"。
CLARIFY_MAX = 2

# ---------------------------------------------------------------------------
# 兜底话术：按"为什么会兜底"分开
#
# 原来只有一句"抱歉，我这边刚才没处理好，麻烦您再说一遍好吗？"糊住所有情况，
# 结果是三种完全不同的失败对用户说同一句话：
#     识别没听清   —— 该请对方再说一遍
#     接口超时     —— 再说一遍也没用，该承诺回电
#     业务拒绝     —— 更不能让他重复，该换人处理
# 用户重复三遍仍听到"麻烦您再说一遍"，体验比直接挂断还差。
#
# 结构：reason -> (配置键, 内置默认话术)
#     配置键先查 `fallback` 段；`writeback` 额外兼容旧的 `stages.WRITEBACK` 段。
#     内置默认是为了配置没补齐时也能跑（4 号补配置不用改代码）。
# ---------------------------------------------------------------------------
FALLBACK_SCENES = {
    "asr": ("reply_asr",
            "不好意思，这边没听清，您再说一遍好吗？"),
    "error": ("reply",
              "抱歉，我这边刚才没处理好，麻烦您再说一遍好吗？"),
    "timeout": ("reply_timeout",
                "我这边查询有点慢，稍后让专员给您回电说明，可以吗？"),
    "business": ("reply_business",
                 "这个情况我这边处理不了，稍后让专员跟您说明，可以吗？"),
    "tool": ("reply_tool",
             "我这边查得有点慢，稍后让专员给您回电说明，可以吗？"),
    "writeback": ("reply_fail",
                  "我这边登记时出了点问题，稍后会有专员联系您确认。感谢您的接听，再见。"),
}
_KEY_TO_REASON = dict((v[0], k) for k, v in FALLBACK_SCENES.items())


def new_session(session_id):
    """创建一次通话的内存状态。

    比骨架版多了几个计数器：身份核验尝试次数、确认问了几次、连续空输入次数。
    这些不是业务字段，但状态机靠它们决定"什么时候该放弃"。

    返回：dict
    """
    return {
        "session_id": session_id,
        "stage": "GREETING",
        "turn": 0,
        "slots": {
            "identity_confirmed": False,
            "need": "",
            "consent": "",      # true / false / unknown，未确认为空
            "customer": None,
            "order": None,
        },
        "closed": False,
        "identity_attempts": 0,
        "confirm_asks": 0,
        "empty_count": 0,
        # 因为"没听懂"追问了几次。到 CLARIFY_MAX 就转人工，不能让用户
        # 对着一句听不懂的话被反复问同一句（E04 那条用例骂的就是这个）。
        "clarify_asks": 0,
        # 身份核验拆成两个槽位分别追：尾号（唯一性高）和姓名。
        # 为什么要拆开：以前只记"问了几次"，问的是什么都丢了，
        # 于是永远是同一句"方便说一下您手机号后四位吗"——用户说了姓名
        # 也当没听见，只能靠再来一次。
        "identity_slots": {"tail": "", "name": ""},
        "slot_asks": {"tail": 0, "name": 0},
        "last_asked_slot": "",
        # 这一轮用户话的识别置信度。由 respond 写入，_reply 读取后落进事件。
        "last_intent_conf": 0.0,
        # 第几版回写。用户改口时递增，配合幂等键让"同一张工单被更新"而不是新开一张。
        "writeback_version": 0,
    }


def classify(text):
    """把一句话拆成若干意图标记 + 一个置信度（规则版）。

    返回 dict：
        deny   否认本人 / 打错电话
        hangup 要求挂断
        angry  带情绪
        no     明确拒绝
        yes    明确肯定
        ask    在提问
        handoff 要求转人工（不含"投诉"——投诉要承接后继续处理）
        digits 文本里的 4 位数字（手机尾号）
        vague  这一句是含糊/敷衍（说了等于没说）
        conf   这次识别的置信度 0~1，写进事件供统计与回归
        evidence  置信度来自哪条证据（exact/contains/digit/weak/vague/none）

    _todo 角色 2 接入大模型后，模型输出 intent + 置信度，本函数退化为
    低置信度时的兜底——这也是为什么它先把 conf 这一个数字先说清，
    接模型时只需要换"从哪来"，不用改"怎么用"。
    """
    t = (text or "").strip()
    n = _norm(t)
    flags = {"deny": False, "hangup": False, "angry": False,
             "no": False, "yes": False, "ask": False, "handoff": False,
             "digits": "", "vague": False, "conf": CONF_NONE, "evidence": "none"}

    # 命中的强证据先都收起来，最后取最强的那个当置信度。
    # 为什么不"先命中先返回"：一句话经常同时有多种信号
    # （"你们天天打电话烦不烦"= 带情绪 + 在质问），
    # 主意图由 _intent_name 按固定优先级取，置信度取最强证据，
    # 两件事分开算，统计口径才稳定。
    strong = []

    if any(w in t for w in DENY_PERSON):
        flags["deny"] = True
        strong.append(("deny", CONF_EXACT))
    if any(w in t for w in HANGUP_WORDS):
        flags["hangup"] = True
        strong.append(("hangup", CONF_CONTAINS))
    if any(w in t for w in ANGRY_WORDS):
        flags["angry"] = True
        strong.append(("angry", CONF_CONTAINS))

    if n in NO_EXACT:
        flags["no"] = True
        strong.append(("no", CONF_EXACT))
    elif any(w in t for w in NO_CONTAINS):
        flags["no"] = True
        strong.append(("no", CONF_CONTAINS))

    # 关键：命中否定词时不再判肯定，"不要"里含"要"就是典型坑
    if not flags["no"]:
        if n in YES_EXACT:
            flags["yes"] = True
            strong.append(("yes", CONF_EXACT))
        elif any(w in t for w in WEAK_YES):
            flags["yes"] = True
            strong.append(("yes", CONF_WEAK))
        elif any(w in t for w in YES_CONTAINS):
            flags["yes"] = True
            strong.append(("yes", CONF_CONTAINS))

    if any(m in t for m in ASK_MARKS):
        flags["ask"] = True
        strong.append(("ask", CONF_CONTAINS))
    # 同理："我不需要转人工"里的"转人工"是拒绝，不是要求
    if not flags["no"] and any(w in t for w in HANDOFF_WORDS):
        flags["handoff"] = True
        strong.append(("handoff", CONF_EXACT))

    # 手机尾号：连续 4 位数字
    digits = ""
    for i in range(len(t) - 3):
        seg = t[i:i + 4]
        if seg.isdigit():
            digits = seg
            break
    flags["digits"] = digits
    if digits:
        strong.append(("info", CONF_DIGIT))

    # 含糊判定：只有在**没有任何强证据**时才算含糊。
    # 为什么加这个前提——"我不知道这个保修多久"里带"不知道"，但它同时是提问
    # （"多久"）。按含糊处理会把一个真问题追问掉，用户会以为客服在打太极。
    # 反过来，"你说是就是吧"一条强规则都不命中，才是真正需要追问的那一类。
    if n in PURE_FILLER or (not strong and any(w in t for w in VAGUE_MARKS)):
        flags["vague"] = True
        flags["conf"] = CONF_VAGUE
        flags["evidence"] = "filler" if n in PURE_FILLER else "vague"
    elif strong:
        flags["conf"] = max(c for _, c in strong)
        flags["evidence"] = max(strong, key=lambda kv: kv[1])[0]
    else:
        flags["conf"] = CONF_NONE
        flags["evidence"] = "none"

    return flags


def _norm(text):
    """去掉首尾空白与标点，只用于"整句是否等于某个标准答案"这类比对。

    "嗯……" 和 "嗯" 必须判成同一件事，否则用户拖个长音就从"含糊"变成"没命中"。
    """
    return (text or "").strip().strip(" \t\r\n。，、！？!?,.~～…—-；;：:")


def _classify(text):
    """classify 的旧名字。其余模块与历史测试仍按这个名字调用，保留以免打断它们。"""
    return classify(text)


def _intent_name(flags):
    """把标记压成一个便于统计的意图名（写进事件的 intent 字段）。

    优先级顺序是**刻意**的：否认 > 挂断 > 转人工 > 带情绪 > 拒绝 > 肯定 > 提问。
    越靠前的越"改变流程走向"，一句话同时命中多种时必须听最硬的那个
    （"不需要，别打了"是拒绝不是挂断，因为拒绝要回写留痕）。
    新增的 vague 排在最后：它只在没有别的信号时成立，理论上不会和上面冲突，
    放在这里是防止将来加词表时把含糊误盖掉一个明确意图。
    """
    for k in ("deny", "hangup", "handoff", "angry", "no", "yes", "ask"):
        if flags.get(k):
            return k
    if flags.get("digits"):
        return "info"
    return "unclear" if flags.get("vague") else "unknown"


def _reply(session, text, next_stage=None, tool_call=None, intent="unknown",
           end_reason="", stage_label=None, kb_hit=None, intent_conf=None):
    """统一的返回构造：把回复、阶段迁移、事件字段一次组装好。

    注意 stage 是"这一轮所处的阶段"（回复归属），next_stage 才是迁移后的阶段。
    stage_label 用于覆盖回复归属：回写那句话属于 WRITEBACK 环节，但这一轮
    是从 CONFIRM 直接跳过去的，不覆盖的话事件的 stage 会记成 CONFIRM，
    质检统计"回写环节说没说要说的那句话"时就查不到。

    kb_hit 只在命中知识条目时传入（含 id / source_id / score / matched）。
    这里的意义是**溯源**：赛后如果有人问"这句'有效期十二个月'依据是什么"，
    事件里能直接翻到条目编号和资料小节，而不是只有一串回复文本。
    """
    stage_now = stage_label or session["stage"]

    # 话术收口：所有回复都从这一个函数出去，所以 LLM 改写只在这里接一次
    # （契约见 docs/17 第 3.1 节）。默认 provider=template，nlg.say 原样返回，
    # 行为与接入前完全一致——verify 53/53 就是用来守这条的。
    #
    # 但下面两类不改写：
    #   intent=error/empty —— 编排出错、用户沉默时，要的是确定和简短，
    #                         不能引入"模型换个说法"的不确定性；
    #   end_reason 有值     —— 挂断/拒绝话术，"再见"不能被改写成"拜拜下次聊"。
    if not end_reason and intent not in ("error", "empty"):
        text = nlg.say(session, text, stage_now)

    if next_stage:
        session["stage"] = next_stage
    # 置信度默认取"这一轮用户话的识别置信度"（respond 里算好放进 session）。
    # 放在 session 里而不是每个调用点都传参，是因为出口太多（回写、兜底、转人工、
    # 收尾都要走 _reply），漏传一处就会出现"某些事件的 conf 永远是 0"这种假数据。
    if intent_conf is None:
        intent_conf = session.get("last_intent_conf", 0.0)
    out = {
        "reply_text": text,
        "stage": stage_now,
        "next_stage": session["stage"],
        "tool_call": tool_call,
        "intent": intent,
        "intent_conf": round(float(intent_conf or 0.0), 2),
    }
    if end_reason:
        out["end_reason"] = end_reason
    if kb_hit:
        out["kb"] = {
            "id": kb_hit.get("id", ""),
            "category": kb_hit.get("category", ""),
            "source_id": kb_hit.get("source_id", ""),
            "score": kb_hit.get("score", 0),
            "matched": list(kb_hit.get("matched") or []),
        }
    return out


def _soothe(text, flags):
    """用户带情绪时，在话术前加一句安抚。文案来自配置，不写死。"""
    if flags.get("angry"):
        return config_loader.apology_prefix() + text
    return text


def _finish(session, text, reason):
    """收尾：进入 DONE，不再推进，也不再发起新话题。"""
    return _reply(session, text, next_stage="DONE", intent=reason, end_reason=reason)


def _do_writeback(session, consent, changed=False):
    """执行回写：构造工具调用，把阶段推到 DONE。

    consent 只有三种取值，不能压缩成布尔：
        True      用户明确同意
        False     用户明确拒绝（拒绝也要回写，业务侧要知道这通电话的结果）
        "unknown" 问过一次仍含糊——宁可记"不确定"，也不能把"嗯"当成同意

    幂等键带版本号：第一次是 v1，用户改口后是 v2。
    同一个键重复调用不会新建（网络重试安全），版本递增则会更新同一张工单
    （`tools.create_ticket` 用 session_id 做业务主键）——这是"改口能纠正结论"的关键。

    参数：
        consent  本次回写的结论
        changed  是否是改口后的更正（决定用哪句播报话术）
    """
    cfg = config_loader.stage_config("WRITEBACK")
    session["slots"]["consent"] = consent

    version = int(session.get("writeback_version", 0)) + 1
    session["writeback_version"] = version

    content = config_loader.writeback_template()
    content["identity_confirmed"] = bool(session["slots"].get("identity_confirmed"))
    content["need"] = session["slots"].get("need") or "未获取"
    content["consent"] = consent
    content["writeback_version"] = version
    if session["slots"].get("order"):
        content["order_id"] = session["slots"]["order"].get("order_id")

    tool_call = {
        "name": "create_ticket",
        "args": {
            "session_id": session["session_id"],
            "content": content,
            "idempotency_key": "%s:WRITEBACK:v%d" % (session["session_id"], version),
            "version": version,
        },
    }

    if changed:
        key = "reply_changed" if consent is True else "reply_changed_declined"
        intent = "changed"
    elif consent is True:
        key = "reply"
        intent = "consent"
    elif consent is False:
        key = "reply_declined"
        intent = "declined"
    else:
        key = "reply_unknown"
        intent = "ambiguous_writeback"

    text = cfg.get(key) or cfg.get("reply", "已为您登记。")
    return _reply(session, text, next_stage="DONE", tool_call=tool_call,
                  intent=intent, stage_label="WRITEBACK")


# ---------------------------------------------------------------------------
# 含糊时的澄清追问
#
# 触发条件是"这一轮没拿到有效信息"（flags["vague"]），处理方式是**追问一句、
# 阶段不动**。为什么不能推进：外呼里推进一格等于"这一环过了"，后面所有判断都
# 建立在它的结论上。用户说"你说是就是吧"，如果直接推到方案沟通，等于替他确认了
# 一个他从没确认的前提——业务侧最后拿到的那张工单，用户是不认账的。
#
# 但追问不能无限次：同一句话问三遍比挂断更伤体验（E04 用例骂的就是这个）。
# 所以到 CLARIFY_MAX 就转人工，把判断权交回给人。
#
# 话术取值顺序：`fallback.<键>` → 内置默认。这样 4 号往配置里补一句就能改口吻，
# 不用等代码；配置没补时也有话说，不会因为缺一个键就抛 KeyError。
# ---------------------------------------------------------------------------
CLARIFY_TEXTS = {
    "NEED": (
        ("reply_unclear_need",
         "不好意思，我这边没太听清。您方便再说一下主要想了解什么吗？"),
        ("reply_unclear_need_2",
         "可能信号不太好。您是想了解产品使用，还是售后这方面的问题呢？"),
    ),
    "OFFER": (
        ("reply_unclear_offer",
         "我这边可能没讲清楚。您是想让我再讲一遍这个方案，还是有别的顾虑？"),
        ("reply_unclear_offer_2",
         "那我确认一下您的意思：您现在是同意，还是暂时不需要？简单回我一句就行。"),
    ),
}


def _clarify(session, stage):
    """含糊输入的统一出口：第一次换句话问，第二次换更窄的选择题，第三次转人工。

    返回 _reply / _finish 的结果（**不推进阶段**，除了转人工那一支）。
    """
    session["clarify_asks"] = int(session.get("clarify_asks", 0)) + 1
    if session["clarify_asks"] > CLARIFY_MAX:
        # 问过 CLARIFY_MAX 次还听不懂，就别再问了。注意这里不回写：
        # 用户从没表达过业务结论，写一张"未同意"的工单是替用户做决定。
        return _handoff(session, "handoff_unclear")

    pairs = CLARIFY_TEXTS.get(stage) or CLARIFY_TEXTS["NEED"]
    key, default = pairs[min(session["clarify_asks"], len(pairs)) - 1]
    return _reply(session, config_loader.fallback_text(key, default), intent="unclear")


# ---------------------------------------------------------------------------
# 身份核验的槽位驱动追问（T2）
#
# 旧实现只有一个计数器：问满两次就放弃。问句是配置里那一句固定的
# "方便说一下您手机号后四位吗"——用户说"我忘了尾号了"，下一句问的
# 还是同一件事，只是措辞换成问姓名（因为 reply_retry 恰好是问姓名，
# 属于"碰巧对了"，不是逻辑上知道该问什么）。
#
# 这里把身份拆成两个槽位，各自记"问过几次、拿到什么值"：
#     tail  手机尾号（唯一性高，优先问）
#     name  姓名（尾号的替代证据）
# 每轮先看用户给了什么，给了就查、查不到就清掉这个值（避免拿同一个错值
# 反复打接口），然后问**当前最缺、且还没问满**的那个槽位。
#
# 同一槽位最多问 SLOT_ASK_MAX 次，两遍都说不上来就收尾转专员——
# 追着问第三遍"您手机号后四位是多少"是外呼里最招投诉的动作。
# ---------------------------------------------------------------------------
SLOT_ASK_MAX = 2

IDENTITY_ASKS = {
    "tail": (
        ("reply_ask_tail",
         "好的，为了核对身份，方便说一下您手机号的尾号吗？就是后四位。"),
        ("reply_ask_tail_2",
         "那我换个方式核对：您手机号的最后四位是多少？说数字就行。"),
    ),
    "name": (
        ("reply_ask_name",
         "没关系，那方便说一下您的姓名吗？我核对一下。"),
        ("reply_ask_name_2",
         "您怎么称呼？说个姓也可以，我这边核对一下。"),
    ),
}

# 从一句话里抽姓名时，这些字出现就说明它不是名字。
# 不这么滤，"真不记得"这种四个汉字会被当成一个姓名去查库。
_NAME_BLOCK = "不没忘知道是我你他她您们吗呢吧啊的对先那这"

_NAME_PATTERNS = (
    re.compile(r"我姓([\u4e00-\u9fa5])"),
    re.compile(r"我叫([\u4e00-\u9fa5]{2,4})"),
    re.compile(r"我是([\u4e00-\u9fa5]{2,3})"),
    re.compile(r"([\u4e00-\u9fa5]{1,3})(?:先生|女士)"),
)


def _identity_text(key, default):
    """身份环节的话术取值：阶段配置 → 兜底配置 → 内置默认。

    三级回落是为了"4 号补配置不用改代码"：现在内置默认就能跑，
    配置一补上就自动以配置为准。
    """
    return (config_loader.stage_config("IDENTITY").get(key)
            or config_loader.fallback_text(key)
            or default)


def _extract_name(text):
    """从一句话里抽一个可信的姓名候选，抽不到就返回空串。

    只认明确的表达方式（我姓X / 我叫XX / 我是XX / X先生），以及
    "整句就是一个 2~4 字中文词"这一种弱形式——后者是为了接住
    "对，张伟"这种补充回答。宁可抽不到，也不能把"真不记得"当名字。
    """
    t = (text or "").strip()
    if not t:
        return ""
    for pat in _NAME_PATTERNS:
        m = pat.search(t)
        if not m:
            continue
        cand = m.group(1)
        if any(ch in _NAME_BLOCK for ch in cand):
            continue
        return cand
    for seg in re.split(r"[，,。、！？!?\s]+", t):
        seg = seg.strip()
        if 2 <= len(seg) <= 4 and all("\u4e00" <= ch <= "\u9fa5" for ch in seg):
            if not any(ch in _NAME_BLOCK for ch in seg):
                return seg
    return ""


def _next_identity_slot(session):
    """下一句该问哪个槽位。两个都问满了返回 None（该收尾了）。

    排序键是"问得少的优先，其次优先问和上一轮不同的那个"——
    连问两遍同一件事（"您尾号多少" → "您尾号到底多少"）是明确该避免的。
    """
    asks = session.get("slot_asks") or {}
    last = session.get("last_asked_slot") or ""
    want_other = "name" if last == "tail" else "tail"
    order = sorted(("tail", "name"),
                   key=lambda s: (asks.get(s, 0), 0 if s == want_other else 1))
    for slot in order:
        if asks.get(slot, 0) < SLOT_ASK_MAX:
            return slot
    return None


def _ask_identity(session, slot, next_stage=None):
    """按槽位取话术追问，并记账（问了几次、上次问的是哪个）。

    next_stage 只在"从 GREETING 首次进入核验"时传 IDENTITY：
    那一句在事件里仍归属 GREETING（它是对开场白的回应），
    后续追问则不推进（next_stage=None，阶段本来就在 IDENTITY）。
    """
    pairs = IDENTITY_ASKS[slot]
    idx = min(session["slot_asks"].get(slot, 0), len(pairs) - 1)
    session["slot_asks"][slot] = session["slot_asks"].get(slot, 0) + 1
    session["last_asked_slot"] = slot
    key, default = pairs[idx]
    return _reply(session, _identity_text(key, default), next_stage=next_stage,
                  intent="identity_retry")


# ---------------------------------------------------------------------------
# 各阶段的处理器
# 约定：每个处理器拿 (session, text, flags)，返回 _reply(...) 的结果
# ---------------------------------------------------------------------------

def _stage_greeting(session, text, flags):
    """GREETING：自报身份，确认是不是本人。

    如果用户在第一句里就把尾号报了，直接核验并进入 NEED——
    不能让用户"说了等于白说"，再问一遍是最招人烦的体验问题。

    _todo 真实实现要判"接电话的是不是本人"，现在只区分否认 / 挂断 / 带尾号 / 其它。
    """
    cfg = config_loader.stage_config("GREETING")
    if flags["deny"]:
        return _finish(session, cfg.get("reply_deny", "抱歉打错了，再见。"), "not_me")
    if flags["hangup"]:
        return _finish(session, cfg.get("reply_hangup", "抱歉打扰您了，再见。"), "hangup")

    # 用户主动报了尾号：当场核验，核上就直接跳到需求确认
    if flags["digits"]:
        hit = tools.query_customer(flags["digits"])
        if hit.get("found"):
            session["slots"]["identity_confirmed"] = True
            session["slots"]["customer"] = hit.get("customer")
            session["slots"]["identity_via"] = "tail"
            identity_cfg = config_loader.stage_config("IDENTITY")
            return _reply(session, _soothe(identity_cfg.get("reply_ok", ""), flags),
                          next_stage="NEED", intent="identity_ok")
        # 报了但核不上：直接改问姓名——刚报过的尾号再问一遍
        # 是最没信息量的一句（用户会以为你没听见他说话）。
        # 不把这个无效值存进槽位，是为了避免下一轮拿它再查一次。
        return _ask_identity(session, "name", next_stage="IDENTITY")

    # 主动报了姓名，同样当场核验。理由和报尾号一样：说了不能等于白说。
    name = _extract_name(text)
    if name:
        hit = tools.query_customer(name)
        if hit.get("found"):
            session["slots"]["identity_confirmed"] = True
            session["slots"]["customer"] = hit.get("customer")
            session["slots"]["identity_via"] = "name"
            session["identity_slots"]["name"] = name
            identity_cfg = config_loader.stage_config("IDENTITY")
            return _reply(session, _soothe(identity_cfg.get("reply_ok", ""), flags),
                          next_stage="NEED", intent="identity_ok")
        # 报了名字但库里没有：记下来，改问尾号（尾号唯一性更高）
        session["identity_slots"]["name"] = name

    # 其它任何应答都往下走：核对身份。这里就用"问尾号"那一句——
    # 它是这一环的第一个问题，走同一套话术与记账，别让第一问和后续追问
    # 变成两套逻辑（以前就是这样：第一问读 stages.IDENTITY.reply，
    # 追问读 reply_retry，两边措辞对不上，也没有"问过几次"的记录）。
    return _ask_identity(session, "tail", next_stage="IDENTITY")


def _stage_identity(session, text, flags):
    """IDENTITY：核对身份。这是全流程的安全关口。

    规则：拿到 4 位尾号或一个能查到的姓名 → 通过；否则允许重试一次，
    两次都不行就结束（真实实现应转人工，当前用 DONE + end_reason 标记）。

    规则：
        1. 从这一句里尽量收槽位（尾号、姓名），收到就先拿去查；
        2. 查不到就把这个值清掉，绝不拿同一个错值反复打接口；
        3. 然后问**当前最缺、且还没问满**的那个槽位；
        4. 两个槽位都问满 SLOT_ASK_MAX 次还没核上，收尾转专员。

    _todo 真实实现要判"接电话的是不是本人"（声纹 / 关系人），
    现在只做到"信息对得上"。
    """
    cfg = config_loader.stage_config("IDENTITY")

    if flags["deny"]:
        return _finish(session, config_loader.stage_config("GREETING").get("reply_deny", ""), "not_me")

    # 用户在核验环节就明确拒绝：不要再追着问信息，直接结束。
    # 注意这里不回写——身份都没确认，写回去的业务结果没有意义。
    if flags["no"] or flags["hangup"]:
        return _finish(session, config_loader.stage_config("GREETING").get("reply_hangup", ""), "refused_identity")

    slots = session["identity_slots"]

    # ① 先收槽位。用户这一轮说了什么就记什么，后说的覆盖先说的
    #    （"不对，我姓张"要能盖掉前面的"我姓李"——写进一个值就不可能叠加）。
    if flags["digits"]:
        slots["tail"] = flags["digits"]
    name = _extract_name(text)
    if name:
        slots["name"] = name

    # ② 拿收到的值去查。尾号优先：它唯一性比姓名高（同名太常见）。
    for slot in ("tail", "name"):
        val = slots.get(slot)
        if not val:
            continue
        hit = tools.query_customer(val)
        if hit.get("found"):
            session["slots"]["identity_confirmed"] = True
            session["slots"]["customer"] = hit.get("customer")
            # 留痕：这一通是靠什么核上的，事后能查（也方便看槽位完整率）
            session["slots"]["identity_via"] = slot
            return _reply(session, _soothe(cfg.get("reply_ok", ""), flags),
                          next_stage="NEED", intent="identity_ok")
        # 查不到：把这个值清掉。不清的话下一轮会拿同一个错值再查一遍，
        # 表现为"用户明明改口了，系统还按旧号核验"。
        session["identity_slots"][slot] = ""

    session["identity_attempts"] += 1

    # ③ 问最缺的那一个；两个都问满了就收尾
    slot = _next_identity_slot(session)
    if slot is None:
        return _finish(session, cfg.get("reply_fail", "这边核对不上，稍后让专员联系您。"),
                       "identity_failed")
    return _ask_identity(session, slot)


def _stage_need(session, text, flags):
    """NEED：先查业务数据，再问诉求。绝不能"没查就问"。

    顺序很关键：进入本阶段先调 query_order 拿真实订单，
    有订单就说订单细节（可溯源），没订单就走通用回访话术。
    """
    cfg = config_loader.stage_config("NEED")

    # 只在第一次进入时查，避免每轮都打接口
    if session["slots"].get("order") is None and not session["slots"].get("order_queried"):
        session["slots"]["order_queried"] = True
        cid = (session["slots"].get("customer") or {}).get("customer_id")
        if cid:
            res = tools.query_order(cid)
            if res.get("found"):
                session["slots"]["order"] = res["order"]

    # 用户一上来就明确拒绝 → 直接去回写（拒绝也要留痕）
    if flags["no"]:
        session["slots"]["need"] = "用户表示不需要"
        return _do_writeback(session, False)

    # 含糊/敷衍 → 追问，不把"说了等于没说"记成诉求
    if flags.get("vague"):
        return _clarify(session, "NEED")

    # 用户在提问 → 用查到的订单信息回答，仍停在本阶段
    if flags["ask"]:
        order = session["slots"].get("order")
        if order:
            tpl = cfg.get("reply_order_detail", "")
            return _reply(session, tpl.format(amount=order.get("amount"), status=order.get("status")),
                          intent="ask")
        return _reply(session, cfg.get("reply_no_order", cfg.get("reply", "")), intent="ask")

    # 正常表达诉求 → 记录并进入方案沟通
    session["slots"]["need"] = text
    offer = config_loader.stage_config("OFFER")
    return _reply(session, _soothe(offer.get("reply", ""), flags),
                  next_stage="OFFER", intent="need")


def _stage_offer(session, text, flags):
    """OFFER：讲清方案与权益。

    用户提问时走两段式：先查 `modules/kb.py`（命中就带出处播报），
    查不到才用配置里的固定话术 / `reply_unknown`。这样"答得上来"的范围
    由知识条目决定，而不是由代码里的 if 分支决定——加一类问题只改 json，不改代码。

    _todo 权益内容必须来自企业资料包。现在 `config/kb.json` 里是**占位内容**
    （`_meta.placeholder = true`），官方资料包公布后要逐条替换 answer 与 source_id，
    替换完把 placeholder 改成 false。内容是否替换过，`scripts/kb_check.py` 会提示。
    """
    cfg = config_loader.stage_config("OFFER")

    # 含糊/敷衍先判。这里尤其危险：用户"嗯……"一声就被推进 CONFIRM，
    # 下一轮客服问"您同意吗"，用户会觉得刚说过的话没被听见。
    # 放在提问分支前面，是因为"啊？"这类只有语气词的输入虽然带问号，
    # 但语义是"我没听清"，不是"我要问一个具体问题"。
    if flags.get("vague"):
        return _clarify(session, "OFFER")

    # 提问：先查知识库（带出处），查不到再退回配置里的固定话术
    if flags["ask"]:
        hit = kb.match(text)
        if hit:
            return _reply(session, hit["answer"], intent="ask", kb_hit=hit)
        # 老路径：knowledge 文件还没落位（或问题确实不在库里）时行为完全不变。
        # 保留它不是为了兼容，是因为「答不上来的问题」和「库里没写的问题」
        # 必须落到同一句 reply_unknown，不能让两种原因产生两种说法。
        if any(w in text for w in config_loader.answer_keywords("fee")):
            return _reply(session, cfg.get("reply_ask_fee", ""), intent="ask")
        if any(w in text for w in config_loader.answer_keywords("validity")):
            return _reply(session, cfg.get("reply_ask_validity", ""), intent="ask")
        return _reply(session, cfg.get("reply_unknown", ""), intent="ask")

    # 明确拒绝 → 回写（拒绝也留痕）
    if flags["no"]:
        return _do_writeback(session, False)

    # 说了方案之后必须走确认，不能默认同意
    confirm = config_loader.stage_config("CONFIRM")
    return _reply(session, confirm.get("reply", ""), next_stage="CONFIRM", intent=_intent_name(flags))


def _stage_confirm(session, text, flags):
    """CONFIRM：拿到明确答复。

    规则：明确肯定 → true；明确否定 → false；其他一律算含糊。
    含糊**先追问一次**（"您可以简单回我一句同意或者不需要"），
    第二次仍含糊才记 unknown——宁可记"不确定"，也不能把"嗯"当成同意。

    为什么追问这一次这么重要：犹豫的用户是外呼里最值钱的一类（还没决定），
    直接一句"那就不打扰您了"等于把本来能成的一单关掉，而且业务侧收到的
    是"客户拒绝"这个错误结论。以前这里写成 `confirm_asks >= 1`，
    导致 reply_again 是死代码、第一次含糊就挂断，这是已修的真实缺陷。

    另外这里多了一道置信度闸门：只有 conf ≥ YES_MIN_CONF 的肯定才算同意。
    "好吧""应该可以""差不多"这类弱肯定一律按含糊处理，追问一次再落库——
    它们听起来是答应，实质是"我不想跟你争了"，据此回写同意，
    后面退订和投诉就是从这里来的。
    """
    cfg = config_loader.stage_config("CONFIRM")

    if flags["yes"] and flags.get("conf", 1.0) >= YES_MIN_CONF:
        return _do_writeback(session, True)

    if flags["no"]:
        return _do_writeback(session, False)

    session["confirm_asks"] += 1
    if session["confirm_asks"] < 2:
        return _reply(session, cfg.get("reply_again", cfg.get("reply", "")), intent="ambiguous")
    return _do_writeback(session, "unknown")


def _stage_writeback(session, text, flags):
    """WRITEBACK：兜底入口。

    正常情况下应当在 NEED/OFFER/CONFIRM 里直接调用 _do_writeback 完成，
    走到这里说明上一轮没有正常迁移，直接补一次回写。
    结论沿用已有槽位；没有就记 unknown（不知道结果比编一个结果安全）。
    """
    consent = session["slots"].get("consent")
    if consent not in (True, False, "unknown"):
        consent = "unknown"
    return _do_writeback(session, consent)


def _stage_done(session, text, flags):
    """DONE：终态。默认只说结束语，不再发起新话题（继续推销会拉低交互质量分）。

    但终态不等于"不再听"。两种输入必须处理，否则会出现两个真实缺陷：
        1. 用户改口（"等等，我不要了" / "那我要"）——不回写就更正，
           业务侧会一直拿着旧结论；
        2. 用户提问（"这个是免费的吧"）——不回答直接说再见，是最伤体验的一种。

    改口靠递增幂等键版本重写同一张工单，不新增阶段（不改状态机契约）。
    """
    cfg = config_loader.stage_config("DONE")
    current = session["slots"].get("consent")

    # 改口同样要过置信度闸门：终态里一句"好吧"如果被当成"我要了"，
    # 就会拿一个弱肯定去覆盖一张已经定稿的工单，比不回写更糟。
    yes_ok = flags["yes"] and flags.get("conf", 1.0) >= YES_MIN_CONF
    target = True if yes_ok else (False if flags["no"] else None)
    already_written = int(session.get("writeback_version", 0)) > 0
    # 注意这里不能要求 current 是布尔值：含糊回写记的是 "unknown"，
    # 用户之后说清"那我要"时必须能把它更正过来，否则工单永远停在"不确定"。
    if target is not None and already_written and current != target:
        return _do_writeback(session, target, changed=True)

    if flags["ask"]:
        return _reply(session, cfg.get("reply_ask") or cfg.get("reply", ""), intent="ask")

    return _reply(session, cfg.get("reply", "感谢您的接听，再见。"), intent="done")


_HANDLERS = {
    "GREETING": _stage_greeting,
    "IDENTITY": _stage_identity,
    "NEED": _stage_need,
    "OFFER": _stage_offer,
    "CONFIRM": _stage_confirm,
    "WRITEBACK": _stage_writeback,
    "DONE": _stage_done,
}


def _handle_empty(session):
    """用户这一轮没说话（沉默 / 空输入）。

    规则：第一次重问一句，连续第二次仍无应答就收尾挂断。
    这条路径以前是死代码——app.py 把空输入替换成"（空输入）"这个非空字符串，
    于是永远走不到这里。现在 app.py 会如实传空串，并由它记 no_answer 事件。

    _todo 真实实现应区分两种情况：完全没声音（网络/静音）vs 有声音但 ASR 没识别出字。
    后者不该直接挂断，应该重播一次并提示"没听清"。
    """
    cfg = config_loader.silence_config()
    if session["empty_count"] >= 2:
        return _finish(session, cfg.get("reply_final", "看来信号不太好，稍后再联系您，再见。"), "silence")
    return _reply(session, cfg.get("reply", "您还在吗？"), intent="empty")


def forced_end(session, reason):
    """被容量/时长闸门强制收尾（不是用户的意思，所以不推进业务结论）。

    触发条件见 config.budget：轮次超限、单通时长超限。
    为什么要做这个：外呼是按时长计费的，没有上限的话一个死循环或一个
    不爱挂断的用户就能把当天额度烧完。先能拦，再谈优化。
    """
    cfg = config_loader.stage_config("DONE")
    key = {"max_turns": "reply_budget_turns",
           "max_call_seconds": "reply_budget_time"}.get(reason)
    text = (cfg.get(key) if key else "") or cfg.get("reply", "感谢您的接听，再见。")
    return _finish(session, text, reason)


def fallback_for(reason="error"):
    """按失败原因取兜底话术。reason 取 FALLBACK_SCENES 的键。

    取值顺序：`fallback.<键>` → （仅 writeback）`stages.WRITEBACK.<键>` → 内置默认。
    为什么 writeback 要单独多查一层：那批话术早就写在 `stages.WRITEBACK` 里了，
    直接搬到 `fallback` 段等于让 4 号改配置、让 3 号的用例重新对齐，收益为零。

    为什么别的 reason 不去查 stages：`WRITEBACK.reply` 是**回写成功**的话术，
    兜底时误拿到它就会对着用户说"已经为您登记完成"——这是最严重的一类事故。
    所以只对白名单里的键开放第二次查找。
    """
    key, default = FALLBACK_SCENES.get(reason, FALLBACK_SCENES["error"])
    text = config_loader.fallback_text(key)
    if text:
        return text
    if reason == "writeback":
        text = config_loader.stage_config("WRITEBACK").get(key)
        if text:
            return text
    return default


def fallback_key(reason="error"):
    """某个 reason 对应的配置键，供写需求和查配置时对照。"""
    return FALLBACK_SCENES.get(reason, FALLBACK_SCENES["error"])[0]


def _handoff(session, reason="handoff"):
    """转人工：礼貌收尾，并且**不下业务结论**。

    为什么不回写：转人工意味着"这通电话没谈完"，业务结论应该由人工给出。
    这时候写一张"未同意"的工单，等于替用户做了决定，人工接手时还会看到
    一条自相矛盾的记录。

    为什么走 `_finish` 而不是直接返回：`_finish` 会把阶段推到 DONE 并带上
    end_reason，`_reply` 见到 end_reason 就跳过话术改写——"再见"被模型
    改写成"拜拜下次聊"是外呼里很难解释的事故。

    reason 是给质检归因用的原因码（user_request / handoff_unclear / …）。
    话术不变，变的是事件里那一个字段——"为什么把这通电话交出去"必须能查。
    """
    cfg = config_loader.stage_config("DONE")
    text = (cfg.get("reply_handoff")
            or config_loader.fallback_text("reply_handoff")
            or "好的，我帮您转接人工同事，稍后会有专人和您联系，感谢您的接听，再见。")
    return _finish(session, text, reason)


def safe_fallback(session, reason="error"):
    """编排环节自己出错时的兜底回复：认错 + 按原因给不同处理，阶段不推进。

    不推进阶段是刻意的：这一轮没听懂，下一轮应该从同一个环节重来，
    而不是假装已经过了这一关（否则会漏掉必说话术与确认）。

    reason 只影响"说哪句"，不影响状态机——兜底永远不该改变业务进度。
    """
    return _reply(session, fallback_for(reason), intent="error")


def opening_line():
    """开场白：外呼是客服先说话，不是等用户先开口。

    为什么单独一个函数：外呼场景里"开场自报身份"是六环节的第一环，也是合规要求
    （必须说清是谁、是哪家公司）。以前它只作为"回应用户第一句"的素材存在，
    结果用户一上来就报尾号时，客服直接从"核对上了"开始，自报身份被整段跳过——
    既拿不到必说话术分，也不符合外呼的合规惯例。

    返回：str
    """
    return config_loader.stage_config("GREETING").get("reply", "您好。")


def respond(session, user_text):
    """核心入口：吃一句用户话，吐一句回复（可能附带一次工具调用）。

    全局规则（先于任何阶段判断）：
        - 空输入 → _handle_empty
        - 要求挂断 → 直接结束，不回写（与"明确拒绝"区分）
    其余交给当前阶段的处理器。

    参数：
        session    new_session() 返回的 dict，会被就地修改
        user_text  用户这一轮说的话
    返回：dict(reply_text, stage, next_stage, tool_call, intent)
    """
    text = (user_text or "").strip()
    session["turn"] += 1
    session["slots"]["last_user_text"] = text

    if not text:
        session["empty_count"] += 1
        # 空输入没有"意图"可言。显式归零，避免上一轮的高置信度被这一轮复用，
        # 事件里出现"用户没说话但 conf=0.95"这种没法解释的数。
        session["last_intent_conf"] = 0.0
        return _handle_empty(session)
    session["empty_count"] = 0

    flags = _classify(text)
    # 识别置信度落到 session，_reply 会把它带进每个事件。
    # 放在这里而不是让每个 handler 传参：出口有十几个，漏一处就是一个假数据点。
    session["last_intent_conf"] = flags.get("conf", 0.0)

    # 全局：要求转人工。放在挂断判断之前——"我要真人"和"别打了"是两种相反的诉求，
    # 前者要承诺有人接手，后者要立刻结束；同时出现时以转人工为准（用户还在听）。
    # 原因码沿用 W7 定下的 "handoff"（用户主动要求），
    # 这里不改是因为 3 号的用例与看板已按这个值统计；新增的"听不懂"那条分支
    # 用 "handoff_unclear" 区分，两者在事件里能分开归因。
    if flags["handoff"] and session["stage"] not in ("WRITEBACK", "DONE"):
        return _handoff(session)

    # 全局：要求挂断。注意与"明确拒绝"区分——两者同时出现时按拒绝处理，
    # 因为拒绝是业务结果，必须回写留痕（"不需要，别打了"属于这一类）。
    if flags["hangup"] and not flags["no"] and session["stage"] not in ("WRITEBACK", "DONE"):
        return _finish(session, config_loader.stage_config("GREETING").get("reply_hangup", ""), "hangup")

    handler = _HANDLERS.get(session["stage"], _stage_done)
    return handler(session, text, flags)


def compliance_guard(text):
    """合规护栏：检查待播报的话术有没有踩红线。

    命中禁语的处理是**整句替换**，不是打码。原因：
        - 打码后的句子会被 TTS 念成"星星星星"，用户听到的是怪音；
        - 打码只遮住了关键词，句子的诱导语义还在（"我保证您***赚钱"）。
    所以命中即整句换成配置里的合规话术，并把命中词记进事件供质检归因。

    另外做一次自检：如果配置的合规话术里本身含禁语，就退回一句最保守的兜底，
    否则会出现"用来兜底的话自己违规"这种荒谬情况。

    _todo 真实实现要做语义级检测（换个说法说同样的话），并重新生成一句
    合规表达而不是整句换掉——现在换句会丢上下文，但至少不会播错。
    """
    out = text or ""
    hits = [w for w in config_loader.forbidden_words() if w and w in out]
    if not hits:
        return {"text": out, "blocked": False, "hits": [], "replaced": False}

    safe = config_loader.blocked_reply()
    if any(w and w in safe for w in config_loader.forbidden_words()):
        safe = "这部分我需要请专员和您说明，可以吗？"
    return {"text": safe, "blocked": True, "hits": hits, "replaced": True}


def fallback_text(key="reply_fail"):
    """按**配置键**取兜底话术（老接口，`app.py:525` 在用）。

    注意它吃的是"键"不是"原因"——`reply_fail` 是配置里已有的键名。
    新代码请用 `fallback_for(reason)`，按"为什么会兜底"来取，
    可读性更好，也不会把键名和原因混成一个概念。
    """
    reason = _KEY_TO_REASON.get(key)
    if reason:
        return fallback_for(reason)
    return config_loader.fallback_text(key) or ""
