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

import config_loader
from modules import nlg, tools

# ---------------------------------------------------------------------------
# 意图关键词表
# 维护说明：这些词是"规则版"的全部智能所在。角色 2 接入模型后，
# 这张表可以退化为兜底（模型置信度低时再用规则）。
# ---------------------------------------------------------------------------

# 精确匹配的肯定回答（避免"不好意思"里的"好"被误判成同意）
YES_EXACT = {"是", "对", "好", "行", "可以", "嗯", "嗯嗯", "是的", "对的",
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
        # 第几版回写。用户改口时递增，配合幂等键让"同一张工单被更新"而不是新开一张。
        "writeback_version": 0,
    }


def _classify(text):
    """把一句话拆成若干意图标记（规则版）。

    返回 dict：
        deny   否认本人 / 打错电话
        hangup 要求挂断
        angry  带情绪
        no     明确拒绝
        yes    明确肯定
        ask    在提问
        digits 文本里的 4 位数字（手机尾号）

    _todo 角色 2 接入大模型后，这里改成"模型输出的 intent + 置信度"，
    并保留本函数作为低置信度时的兜底。
    """
    t = (text or "").strip()
    flags = {"deny": False, "hangup": False, "angry": False,
             "no": False, "yes": False, "ask": False, "digits": ""}

    flags["deny"] = any(w in t for w in DENY_PERSON)
    flags["hangup"] = any(w in t for w in HANGUP_WORDS)
    flags["angry"] = any(w in t for w in ANGRY_WORDS)
    flags["no"] = t in NO_EXACT or any(w in t for w in NO_CONTAINS)
    # 关键：命中否定词时不再判肯定，"不要"里含"要"就是典型坑
    flags["yes"] = (not flags["no"]) and (t in YES_EXACT or any(w in t for w in YES_CONTAINS))
    flags["ask"] = any(m in t for m in ASK_MARKS)

    # 手机尾号：连续 4 位数字
    digits = ""
    for i in range(len(t) - 3):
        seg = t[i:i + 4]
        if seg.isdigit():
            digits = seg
            break
    flags["digits"] = digits
    return flags


def _intent_name(flags):
    """把标记压成一个便于统计的意图名（写进事件的 intent 字段）。"""
    for k in ("deny", "hangup", "angry", "no", "yes", "ask"):
        if flags.get(k):
            return k
    return "info" if flags.get("digits") else "unknown"


def _reply(session, text, next_stage=None, tool_call=None, intent="unknown",
           end_reason="", stage_label=None):
    """统一的返回构造：把回复、阶段迁移、事件字段一次组装好。

    注意 stage 是"这一轮所处的阶段"（回复归属），next_stage 才是迁移后的阶段。
    stage_label 用于覆盖回复归属：回写那句话属于 WRITEBACK 环节，但这一轮
    是从 CONFIRM 直接跳过去的，不覆盖的话事件的 stage 会记成 CONFIRM，
    质检统计"回写环节说没说要说的那句话"时就查不到。
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
    out = {
        "reply_text": text,
        "stage": stage_now,
        "next_stage": session["stage"],
        "tool_call": tool_call,
        "intent": intent,
    }
    if end_reason:
        out["end_reason"] = end_reason
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
            identity_cfg = config_loader.stage_config("IDENTITY")
            return _reply(session, _soothe(identity_cfg.get("reply_ok", ""), flags),
                          next_stage="NEED", intent="identity_ok")

    # 其它任何应答都往下走：核对身份
    identity_cfg = config_loader.stage_config("IDENTITY")
    return _reply(session, _soothe(identity_cfg.get("reply", ""), flags),
                  next_stage="IDENTITY", intent=_intent_name(flags))


def _stage_identity(session, text, flags):
    """IDENTITY：核对身份。这是全流程的安全关口。

    规则：拿到 4 位尾号或一个能查到的姓名 → 通过；否则允许重试一次，
    两次都不行就结束（真实实现应转人工，当前用 DONE + end_reason 标记）。

    _todo 槽位驱动：缺尾号问尾号，缺姓名问姓名，而不是固定问法。
    """
    cfg = config_loader.stage_config("IDENTITY")

    if flags["deny"]:
        return _finish(session, config_loader.stage_config("GREETING").get("reply_deny", ""), "not_me")

    # 用户在核验环节就明确拒绝：不要再追着问信息，直接结束。
    # 注意这里不回写——身份都没确认，写回去的业务结果没有意义。
    if flags["no"] or flags["hangup"]:
        return _finish(session, config_loader.stage_config("GREETING").get("reply_hangup", ""), "refused_identity")

    # 用尾号或姓名去查客户档案
    keyword = flags["digits"] or text
    hit = tools.query_customer(keyword)
    if hit.get("found"):
        session["slots"]["identity_confirmed"] = True
        session["slots"]["customer"] = hit.get("customer")
        return _reply(session, _soothe(cfg.get("reply_ok", ""), flags),
                      next_stage="NEED", intent="identity_ok")

    # 没查到：允许重试一次
    session["identity_attempts"] += 1
    if session["identity_attempts"] >= 2:
        return _finish(session, cfg.get("reply_fail", "这边核对不上，稍后让专员联系您。"), "identity_failed")
    return _reply(session, cfg.get("reply_retry", cfg.get("reply", "")),
                  intent="identity_retry")


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

    _todo 权益内容必须来自企业资料包；现在是配置里的固定话术，
    接真实资料包后要做检索 + 引用，不能自由生成。
    """
    cfg = config_loader.stage_config("OFFER")

    # 提问：用标准答法，不要临场编
    if flags["ask"]:
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
    """
    cfg = config_loader.stage_config("CONFIRM")

    if flags["yes"]:
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

    target = True if flags["yes"] else (False if flags["no"] else None)
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


def safe_fallback(session):
    """编排环节自己出错时的兜底回复：认错 + 请对方再说一遍，阶段不推进。

    不推进阶段是刻意的：这一轮没听懂，下一轮应该从同一个环节重来，
    而不是假装已经过了这一关（否则会漏掉必说话术与确认）。
    """
    text = config_loader.fallback_text("reply", "抱歉，我这边刚才没处理好，麻烦您再说一遍好吗？")
    return _reply(session, text, intent="error")


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
        return _handle_empty(session)
    session["empty_count"] = 0

    flags = _classify(text)

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
    """取兜底话术。目前用于回写失败时替换掉"已登记"那句。

    _todo 兜底话术应再细分：接口超时 / 业务拒绝 / 系统异常，各自不同。
    """
    return config_loader.stage_config("WRITEBACK").get(key, "稍后会有专员联系您。")
