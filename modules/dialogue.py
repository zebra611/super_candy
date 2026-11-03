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
from modules import tools

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


def _reply(session, text, next_stage=None, tool_call=None, intent="unknown", end_reason=""):
    """统一的返回构造：把回复、阶段迁移、事件字段一次组装好。

    注意 stage 是"这一轮所处的阶段"（回复归属），next_stage 才是迁移后的阶段。
    """
    stage_now = session["stage"]
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


def _do_writeback(session, declined=False):
    """执行回写：构造工具调用，把阶段推到 DONE。

    幂等键固定为 "<session_id>:WRITEBACK"——同一通电话重复回写只产生一条工单。
    回写内容来自 config 的 writeback_template，再用槽位覆盖。
    """
    cfg = config_loader.stage_config("WRITEBACK")
    content = config_loader.writeback_template()
    content["identity_confirmed"] = bool(session["slots"].get("identity_confirmed"))
    content["need"] = session["slots"].get("need") or "未获取"
    content["consent"] = session["slots"].get("consent")
    if session["slots"].get("order"):
        content["order_id"] = session["slots"]["order"].get("order_id")

    tool_call = {
        "name": "create_ticket",
        "args": {
            "session_id": session["session_id"],
            "content": content,
            "idempotency_key": "%s:WRITEBACK" % session["session_id"],
        },
    }
    text = cfg.get("reply_declined" if declined else "reply",
                   cfg.get("reply", "已为您登记。"))
    return _reply(session, text, next_stage="DONE", tool_call=tool_call,
                  intent="declined" if declined else "consent")


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
        session["slots"]["consent"] = False
        return _do_writeback(session, declined=True)

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
        session["slots"]["consent"] = False
        return _do_writeback(session, declined=True)

    # 说了方案之后必须走确认，不能默认同意
    confirm = config_loader.stage_config("CONFIRM")
    return _reply(session, confirm.get("reply", ""), next_stage="CONFIRM", intent=_intent_name(flags))


def _stage_confirm(session, text, flags):
    """CONFIRM：拿到明确答复。

    规则：明确肯定 → true；明确否定 → false；其他一律算含糊，
    追问一次后仍含糊就记 unknown——宁可记"不确定"，也不能把"嗯"当成同意。
    """
    cfg = config_loader.stage_config("CONFIRM")

    if flags["yes"]:
        session["slots"]["consent"] = True
        return _do_writeback(session)

    if flags["no"]:
        session["slots"]["consent"] = False
        return _do_writeback(session, declined=True)

    session["confirm_asks"] += 1
    if session["confirm_asks"] >= 1:
        session["slots"]["consent"] = "unknown"
        return _do_writeback(session, declined=True)
    return _reply(session, cfg.get("reply_again", cfg.get("reply", "")), intent="ambiguous")


def _stage_writeback(session, text, flags):
    """WRITEBACK：兜底入口。

    正常情况下应当在 NEED/OFFER/CONFIRM 里直接调用 _do_writeback 完成，
    走到这里说明上一轮没有正常迁移，直接补一次回写。
    """
    return _do_writeback(session, declined=(session["slots"].get("consent") is False))


def _stage_done(session, text, flags):
    """DONE：终态。只说结束语，不再发起新话题（继续推销会拉低交互质量分）。"""
    cfg = config_loader.stage_config("DONE")
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
    """用户没说话（空输入）：重问一次，连续两次就收尾。

    _todo 真实实现应该是"检测到沉默 → 重播 → 再沉默 → 挂断"，
    并记录 no_answer 事件供质检统计。
    """
    if session["empty_count"] >= 2:
        return _finish(session, config_loader.stage_config("GREETING").get("reply_hangup", ""), "silence")
    stage = session["stage"]
    text = config_loader.stage_config(stage).get("reply_again") \
        or config_loader.stage_config(stage).get("reply", "您还在吗？")
    return _reply(session, text, intent="empty")


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

    当前只做字面禁语匹配（config 的 forbidden_words），命中替换成 ***。
    _todo 真实实现要做语义级检测（"我保证您一定赚"这类没有关键词的虚假承诺），
    并且不是打码，而是重新生成一句合规表达；必说话术没说到也要能发现。
    """
    hits = []
    out = text or ""
    for w in config_loader.forbidden_words():
        if w and w in out:
            hits.append(w)
            out = out.replace(w, "***")
    return {"text": out, "blocked": bool(hits), "hits": hits}


def fallback_text(key="reply_fail"):
    """取兜底话术。目前用于回写失败时替换掉"已登记"那句。

    _todo 兜底话术应再细分：接口超时 / 业务拒绝 / 系统异常，各自不同。
    """
    return config_loader.stage_config("WRITEBACK").get(key, "稍后会有专员联系您。")
