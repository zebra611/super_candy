"""失败聚类：把逐轮缺陷聚成"可动手的簇"。

为什么不用向量聚类：
    缺陷本身已经是结构化标签（缺陷码 × 阶段 × 用户动作 × 客服意图），
    在这个粒度上做 KMeans 只会把"同一个 bug 的两种表现"拆成两簇，
    再把两个不同的 bug 揉成一簇——不可解释，也没法验收。
    这里按**标签组合**精确分组：同一簇就是同一个 bug 的同一类表现，
    修完能一对一验证。

输出两档：
    clusters  细粒度（缺陷码|阶段|用户动作|客服意图）—— 用来定位与出候选
    by_code   粗粒度（只看缺陷码）—— 用来回答"当前最严重的是哪类问题"

每个簇带 `target`（该改哪个配置键）。**只允许指向话术配置**：
自进化的边界是"改说法、改策略"，不是让模型改代码或改事实。
"""

from modules import attribution as attr

#: 缺陷码 → 人话成因。给人看的，不参与判定。
_CAUSE = {
    attr.AGENT_SLOT_LOSS: "用户已经报出了信息，客服下一句仍在索要同一条（槽位没接住）",
    attr.AGENT_NOISE_ON_QUESTION: "用户在提问/拒绝/说记不住，客服把它当成「有杂音没听清」",
    attr.AGENT_LOOP: "连续三轮重复同句话术，原地绕圈",
    attr.AGENT_NO_PROGRESS: "整通电话没有推进过阶段，也没给出新信息",
    attr.AGENT_PREMATURE_WRITEBACK: "用户说「没听明白」，客服没复述就回写，被记成「拒绝整改」",
    attr.AGENT_PREMATURE_CLOSE: "客户没同意、也没核上身，工单却被回写了（误闭环污染工单）",
    attr.AGENT_REPAIR_IGNORED: "客户说「听不清/再说一遍」，客服回了「确认不到/转专员」",
}

#: 只能改代码、不能靠改话术解决的缺陷码。**必须显式标出来**：
#: 否则优化器会拿一句"我再解释一遍"去糊一个流程问题，看着指标涨了，
#: 实际只是把错误藏进了另一句话里。
_NEEDS_CODE = {
    attr.AGENT_PREMATURE_WRITEBACK: "CONFIRM 阶段对'没听明白'的否定判定（dialogue._stage_confirm）",
    attr.AGENT_NO_PROGRESS: "槽位轮换与收尾条件（dialogue._next_identity_slot）",
    attr.AGENT_PREMATURE_CLOSE: "回写前置条件（dialogue._do_writeback 的调用点与确认计数）",
    attr.AGENT_REPAIR_IGNORED: "重说请求的分支位置与提问守卫（dialogue._stage_offer）",
}

#: 缺陷码 × 阶段 × 用户动作 → 建议改哪个配置键。键路径相对场景 JSON。
#:
#: 为什么要把用户动作也算进键里：同样是"原地绕圈"，
#: 用户质疑身份时绕的是 `reply_ask_identity`，用户说记不住号码时绕的是
#: `reply_offtopic`——**改错那一句，指标不会动，还会把另一条路径搞坏**。
_TARGET = {
    (attr.AGENT_SLOT_LOSS, "IDENTITY"): "stages.IDENTITY.reply_ask_tail",
    (attr.AGENT_SLOT_LOSS, "GREETING"): "stages.GREETING.reply",
    (attr.AGENT_SLOT_LOSS, "NEED"): "stages.NEED.reply",
    (attr.AGENT_NOISE_ON_QUESTION, "IDENTITY"): "stages.IDENTITY.reply_noise",
    (attr.AGENT_NOISE_ON_QUESTION, "NEED"): "stages.NEED.reply",
    (attr.AGENT_NOISE_ON_QUESTION, "OFFER"): "stages.OFFER.reply",
    (attr.AGENT_LOOP, "IDENTITY"): "stages.IDENTITY.reply_reask",
    (attr.AGENT_LOOP, "OFFER"): "stages.OFFER.reply",
    (attr.AGENT_NO_PROGRESS, "IDENTITY"): "stages.IDENTITY.reply",
    # 用户动作细分
    (attr.AGENT_LOOP, "IDENTITY", "ask_why"): "stages.IDENTITY.reply_ask_identity",
    (attr.AGENT_LOOP, "IDENTITY", "cant_remember"): "stages.IDENTITY.reply_offtopic",
    (attr.AGENT_LOOP, "IDENTITY", "refuse_info"): "stages.IDENTITY.reply_offtopic",
    (attr.AGENT_LOOP, "IDENTITY", "off_topic"): "stages.IDENTITY.reply_offtopic",
    (attr.AGENT_LOOP, "IDENTITY", "ack"): "stages.IDENTITY.reply_offtopic",
}


def _target_for(code, stage, user_action=""):
    return (_TARGET.get((code, stage, user_action))
            or _TARGET.get((code, stage))
            or "")


def cluster_defects(sessions, personas):
    """聚合并返回细粒度簇（只含系统缺陷），按影响会话数降序。

    参数：
        sessions   sim_eval 的会话记录
        personas   画像原文（判 split / goal 用）
    """
    by_id = {p["id"]: p for p in personas}
    buckets = {}

    for s in sessions:
        p = by_id.get(s["id"]) or {}
        res = attr.analyse_session(s, p)
        for d in res["defects"]:
            if d["code"] not in attr.AGENT_DEFECTS:
                continue
            key = "%s|%s|%s|%s" % (d["code"], d["stage"] or "?", d["user_action"],
                                   d["agent_intent"])
            b = buckets.setdefault(key, {
                "key": key, "code": d["code"], "stage": d["stage"] or "?",
                "user_action": d["user_action"], "agent_intent": d["agent_intent"],
                "sessions": 0, "session_ids": [], "examples": [],
                "cause": _CAUSE.get(d["code"], ""),
                "target": _target_for(d["code"], d["stage"] or "", d["user_action"]),
                # 需要改代码而不是改话术：优化器必须跳过，只写进待办
                "needs_code": _NEEDS_CODE.get(d["code"], ""),
            })
            if s["id"] not in b["session_ids"]:      # 同一通只记一次
                b["session_ids"].append(s["id"])
                b["sessions"] += 1
            if len(b["examples"]) < 3:
                b["examples"].append("[%s] %s" % (s["id"], d["evidence"]))

    out = list(buckets.values())
    out.sort(key=lambda x: (-x["sessions"], x["key"]))
    return out


def group_by_code(clusters):
    """把细簇收成粗类，回答"现在最该修的是哪一类"。"""
    agg = {}
    for c in clusters:
        g = agg.setdefault(c["code"], {
            "code": c["code"], "cause": c["cause"], "sessions": 0,
            "clusters": 0, "session_ids": set(), "targets": [],
        })
        g["clusters"] += 1
        g["session_ids"].update(c["session_ids"])
        if c["target"] and c["target"] not in g["targets"]:
            g["targets"].append(c["target"])
    out = []
    for g in agg.values():
        g["sessions"] = len(g["session_ids"])
        g["session_ids"] = sorted(g["session_ids"])
        out.append(g)
    out.sort(key=lambda x: (-x["sessions"], x["code"]))
    return out
