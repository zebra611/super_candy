"""M3 对话编排 —— 哑链路里的"假大脑"。

职责（真实形态）：
    听懂了要干什么（意图）→ 缺什么问什么（槽位）→ 按状态机推进六个环节
    → 生成自然话术（受合规约束）→ 该查业务就调工具 → 最后结构化回写。

当前状态：**没有意图识别，也没有槽位填充**，只做一件事——
    每说一轮，状态机往前推一格，话术从 config/scene.json 里取。
    这样能验证"六个环节能不能走完、回写能不能触发"，
    但千万不要拿它去答辩，它连用户说了什么都不看。

负责人：角色 2（对话大脑与工具）

后续待补充（按优先级）：
    1. 意图识别：把用户话映射到"确认身份 / 提需求 / 拒绝 / 反问 / 骂人"等意图；
    2. 槽位填充：从用户话里抽姓名、订单号、诉求，并按 config 的 slots 校验；
    3. 多轮纠错：用户改口时要能回退状态（现在只会一路向前，不会退）；
    4. 主动追问：槽位缺失时问对的问题（现在话术是写死的）；
    5. 幻觉抑制：业务事实必须来自工具返回或资料包，不许模型自由发挥；
    6. 兜底：连续两轮听不懂要转人工，情绪激动要先安抚。
"""

import config_loader


def new_session(session_id):
    """创建一次通话的内存状态。

    真实实现里这个状态要能持久化（重启服务不能丢），
    骨架阶段放内存 dict 就够，进程重启即丢失。

    返回：dict(session_id, stage, turn, slots, closed)
    """
    return {
        "session_id": session_id,
        "stage": "GREETING",   # 从开场自报身份开始
        "turn": 0,             # 已进行的轮数
        "slots": {},           # 已收集到的业务信息（骨架阶段不填）
        "closed": False,       # 是否已闭环（走到回写且成功）
    }


def _next_stage(stage):
    """取状态机的下一个阶段。线性推进，不处理跳转与回退。

    TODO 真实实现需要一张迁移表：某些意图可以从 NEED 直接跳到 CONFIRM
    （用户一上来就说"我就要这个"），也可能从任意阶段跳到转人工。
    """
    order = config_loader.stages_order()
    if stage not in order:
        return order[0]
    i = order.index(stage)
    return order[i + 1] if i + 1 < len(order) else order[-1]


def respond(session, user_text):
    """核心入口：吃一句用户话，吐一句回复（可能附带一次工具调用）。

    哑链路逻辑（三步）：
        1. 记下用户说了什么（只存原文，不解析）；
        2. 取当前阶段的话术当回复；
        3. 如果当前阶段是 WRITEBACK，就发起一次 create_ticket 工具调用，
           并把状态置为 DONE；否则推进到下一阶段。

    参数：
        session     new_session() 返回的 dict，会被就地修改
        user_text   用户这一轮说的话
    返回：dict(reply_text, stage, next_stage, tool_call, intent)
    """
    stage = session["stage"]
    session["turn"] += 1
    session["slots"]["last_user_text"] = user_text   # 只留痕，不做解析

    cfg = config_loader.stage_config(stage)
    reply_text = cfg.get("reply", "（这一阶段还没有配置话术）")

    tool_call = None
    if stage == "WRITEBACK":
        # 走到回写阶段：真正发起业务工具调用，这是"闭环"的证据。
        # 幂等键用 会话ID + 固定环节名，保证同一通电话重复回写只产生一条工单。
        tool_call = {
            "name": "create_ticket",
            "args": {
                "session_id": session["session_id"],
                "content": config_loader.writeback_template(),
                "idempotency_key": "%s:WRITEBACK" % session["session_id"],
            },
        }
        session["stage"] = "DONE"
    else:
        session["stage"] = _next_stage(stage)

    return {
        "reply_text": reply_text,
        "stage": stage,                 # 这一轮所处的阶段（回复归属）
        "next_stage": session["stage"],  # 推进到哪里
        "tool_call": tool_call,
        "intent": "unknown",            # TODO 真实实现要返回识别出的意图
    }


def compliance_guard(text):
    """合规护栏：检查待播报的话术有没有踩红线。

    哑链路实现：只做字面禁语匹配（config 里的 forbidden_words），
    命中就把它换成占位符并标记 blocked。
    真实实现要做的：
        - 语义级检测（"我保证您一定赚"这类没有关键词的虚假承诺）；
        - 必说话术命中校验（该说的没说也要扣分，不只是"不该说的别说"）；
        - 命中后不是简单替换，而是重新生成一句合规表达。

    返回：dict(text, blocked, hits)
    """
    hits = []
    out = text
    for w in config_loader.forbidden_words():
        if w and w in out:
            hits.append(w)
            out = out.replace(w, "***")
    return {"text": out, "blocked": bool(hits), "hits": hits}
