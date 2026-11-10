"""逐轮正负反馈：给客服的**每一句回复**打分，并指出该改哪个可学习开关。

为什么不复用 `attribution` 的会话级归因：
    归因回答的是“这通电话为什么没闭环”，一轮只算一次责任；
    自进化需要的是“这一句好在哪、坏在哪”，因为它要决定**下一轮改哪里**。
    两者的判据有交集（槽位丢失、把提问当杂音），但粒度与用途不同：
    归因用于聚失败簇、算通过率；反馈用于逐条记账、给调整提供目标。

四条硬约束（和归因器一致，理由同样是“结论可以被伪造，原话不能”）：
    1. 纯规则、不调模型：同一条会话重复判一百次，结论必须逐字一致；
    2. 只判**能被原话证实**的问题，每条都带 evidence（客服原话片段）；
    3. 判定与展示分离：`polarity` 只有 +/−/0 三档，理由写进 `codes`；
    4. 判据本身也进反向自测（scripts/test_self_evolve.py）：把好回复判成负面
       必须让自测变红，防止“为了让曲线好看而放宽判据”。

一个容易踩的坑：对抗器把 `found`（弱点命中）挂在**下一轮用户动作**上，
因为它是在看到客服上一句之后才观察的。直接拿 `turns[i].user.found` 当
“第 i 轮客服的问题”会整体错一拍，训练面板上的红标就是这么来的。
本模块全部从 `turns[i].agent.reply_text` 与上一句自己比对，不用 found。
"""

import difflib
import re

import config_loader

#: 用户在提问的动作。与 user_sim 的动作名对齐，不额外发明词。
ASK_ACTIONS = ("ask_why", "ask_material", "ask_fee", "reask_already_told")

#: 用户在提供可核验事实的动作。
FACT_ACTIONS = ("answer_tail", "answer_name", "answer_order", "answer_company")

#: 客服话里出现这些，说明这一类提问得到了正面回答。
#: 与 `modules/adversary.py::_ANSWER_CUES` 同一口径——被评测方与判分方不能各用一套。
ANSWER_CUES = {
    "ask_why": ("官方", "审核专员", "不是推销", "资质审核", "商家后台"),
    "ask_material": ("补充", "材料", "重新提交", "上传"),
    "ask_fee": ("不收取", "免费", "费用", "不收费"),
}

#: 客服把有实义的提问当噪声的措辞。
NOISE_WORDS = ("杂音", "没听清", "没太听清", "听不清", "信号不太好")

#: 「合规性延迟回答」：现在不能讲，但说清了为什么、并给了下一步。
#: 这类回复不算 no_answer——把"没答"和"按规定暂时不能答"混为一谈，
#: 会逼着优化器去泄露未核验的信息。判据只认**带原因**的说法，不认万能挡箭牌：
#: 只说"稍后专员联系您"仍然算没回答（那是把问题推走，不是解释）。
DEFER_WORDS = ("先确认", "核对身份", "确认您是哪家公司", "才能讲", "不能把别人",
               "核完", "核对上我就说", "确认之后")

#: 客服确实接住了用户给的事实。
ACK_WORDS = ("核对上了", "收到了", "记下了", "我记着", "收到", "您报的", "登记")

#: 「复述降级」：只报大类、把具体原因丢了。
GENERIC_WORDS = ("材料问题",)

#: 具体原因的抽取式样：「原因是X」「因X没有通过审核」。
_REASON_RE = re.compile(r"(?:原因是|原因为|因)([^，。；、]{4,24})")

_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()]")


def _stem(text, n=20):
    """去掉标点后的前 n 个字。

    保留给面板/调试用的粗口径；逐轮判定用的是 `_similar`（整句相似度），
    因为共同开头会把内容不同的两条回复误判成复读（11-09 A07 实测）。
    """
    return _PUNCT.sub("", text or "")[:n]


REPEAT_RATIO = 0.90


def _similar(a, b):
    """两句是否"实质上同一句"。

    为什么不用"前 N 字相同"：性格适配层会给不同回复加同一个开头
    （"这个问题我得先确认您是哪家公司才能讲……"），前 20 字一样、后半句
    却分别是"是这家公司的负责人吗"和"让负责人接一下"，那不是复读。
    也不能要求整句相等：适配层会追加"我放慢一点再说"这类尾巴，整句比较
    会漏判真实复读。实测这批会话：真复读相似度 0.92~1.00，非复读 ≤0.85，
    0.90 是一条能分开两堆的线，且方向安全——宁可漏判，不冤枉好回复。
    """
    na, nb = _PUNCT.sub("", a or ""), _PUNCT.sub("", b or "")
    if len(na) < 12 or len(nb) < 12:
        return False
    return difflib.SequenceMatcher(None, na, nb).ratio() >= REPEAT_RATIO


def _said(turn):
    return (turn.get("agent") or {}).get("reply_text") or ""


def _act(turn):
    return (turn.get("user") or {}).get("action") or ""


def _stage(turn):
    return (turn.get("agent") or {}).get("stage") or ""


def _asks_again(turn, fact_key):
    """客服这一句是不是又在索要同一条信息（精确措辞，不用宽泛的 detect_asks）。

    contact_name 不收裸「姓名」：性格适配层会在句尾挂「说联系人姓名也行」，
    客服原话其实是「核对上了，感谢配合……」——裸词会把"已经接住、顺手给个
    备选路径"误判成"又问一遍"（A04/A06 第 3 轮实测，11-10 与 attribution
    同一处假阳，两边词表必须一致，否则同一句话在面板与归因里两种结论）。
    """
    said = _said(turn)
    marks = {
        "phone_tail": ("后四位", "后 4 位", "尾号", "最后四位"),
        "contact_name": ("您的姓名", "怎么称呼", "怎么叫您", "您贵姓",
                         "姓名是", "报一下姓名", "确认一下姓名", "说一下姓名"),
        "application_id": ("申请单编号", "单号"),
    }.get(fact_key, ())
    return any(w in said for w in marks)


_FACT_OF_ACTION = {"answer_tail": "phone_tail", "answer_name": "contact_name",
                   "answer_order": "application_id"}


def _generic_summary(turns, i):
    """复述时把前面讲过的具体原因降级成“材料问题”。

    只在**前面确实讲过具体原因**时判：否则“材料问题”只是场景配置的通用口径，
    不是丢信息。证据必须能指回前面那一句，不能凭印象。
    """
    if i <= 0:
        return ""
    said = _said(turns[i])
    if not any(w in said for w in GENERIC_WORDS):
        return ""
    for prev in turns[:i]:
        m = _REASON_RE.search(_said(prev))
        if not m:
            continue
        reason = m.group(1).strip()
        if reason and reason not in said and len(reason) >= 4:
            return "前面说过「%s」，这一句只剩「材料问题」" % reason[:16]
    return ""


def judge_turn(turns, i):
    """判第 i 轮（0 基）。返回 {polarity, codes, why, evidence, target, stage, action}。

    polarity：− 有任一可证实的负面；+ 没有负面且有可证实的正面；0 其余。
    负面优先：一句话既推进了阶段又把用户的问题答歪了，仍然算负面——
    推进是流程分，答歪是体验分，两件事不能互相抵消。
    """
    t = turns[i]
    user, agent = t.get("user") or {}, t.get("agent") or {}
    said = agent.get("reply_text") or ""
    act = user.get("action") or ""
    stage = agent.get("stage") or ""
    prev = _said(turns[i - 1]) if i else ""

    neg, why, ev = [], [], ""

    if said and prev and _similar(said, prev):
        neg.append("repeat")
        why.append("与上一句实质相同（客户听到的是复读）")
        ev = ev or said[:40]

    if act in ASK_ACTIONS:
        cues = ANSWER_CUES.get(act) or ()
        # 「确认不到」+ 问的是**本单就有答案**的事（补什么材料 / 号码哪来的）
        # 优先判没回答：客服说「这个我这边确认不到，稍后让专员用**官方**号码回电」
        # 时，`ask_why` 的关键词里正好有「官方」，旧判据会把它算成回答了来源质疑——
        # 客户问「你哪来的我号码」，得到的仍是"我不知道，让专员打给你"
        # （11-10 A15 第 6 轮实测，属假绿：漏判比误判更难被发现）。
        #
        # 只对这两类问题生效，不横扫：客户问「后台哪个入口」这类画外问题，
        # 答不上来后体面收线是对的，把收线也算缺陷会让优化器去追一个改不动的数
        # （实测横扫后负面 6→10，自进化连试两轮都被"新增负面会话"挡住）。
        _own_fact_q = re.search(r"(补什么|要补|材料|哪来|怎么有我|凭什么|怎么信|谁给的)",
                                user.get("text") or "")
        _deflected = bool(_own_fact_q) and "确认不到" in said
        if _deflected or (cues and not any(c in said for c in cues)
                          and not any(w in said for w in DEFER_WORDS)):
            neg.append("no_answer")
            why.append("客户在问，客服答「确认不到」" if _deflected
                       else "客户在问，回复里没有这一类问题的正面信息")
            ev = ev or said[:40]

    fact = _FACT_OF_ACTION.get(act)
    if fact and _asks_again(t, fact):
        neg.append("slot_loss")
        why.append("客户刚给了「%s」，这一句又在要同一条" % fact)
        ev = ev or said[:40]

    if act in ("ask_why", "ask_material", "ask_fee") and any(w in said for w in NOISE_WORDS):
        neg.append("noise_on_question")
        why.append("把有实义的提问当成没听清/有杂音")
        ev = ev or said[:40]

    gs = _generic_summary(turns, i)
    if gs:
        neg.append("generic_summary")
        why.append("复述丢了具体原因：" + gs)
        ev = ev or said[:40]

    if re.search(r"\{[^}]*\}|XX|xx", said):
        neg.append("placeholder")
        why.append("播出了未渲染的占位符")
        ev = ev or said[:40]

    # 同一个要求在连续三轮里反复出现，且中间客户没有给出新事实 → 原地打转。
    if i >= 2 and said:
        keys = []
        for j in (i - 2, i - 1, i):
            k = [w for w in ("后四位", "申请单编号", "单号", "您的姓名", "怎么称呼")
                 if w in _said(turns[j])]
            keys.append(k[0] if k else "")
        if keys[0] and keys[0] == keys[1] == keys[2] \
                and not any(_act(turns[j]) in FACT_ACTIONS for j in (i - 1, i)):
            neg.append("stalled")
            why.append("连续三轮停在同一个要求上（%s）" % keys[0])
            ev = ev or said[:40]

    if neg:
        return {"polarity": "-", "codes": neg, "why": "；".join(why), "evidence": ev,
                "stage": stage, "action": act,
                "target": _target(neg, stage, user.get("text") or "")}

    # ---- 正面：必须能被原话证实，不能“没扣分就算好” ----
    pos, pwhy = [], []
    if act in ASK_ACTIONS and any(c in said for c in (ANSWER_CUES.get(act) or ())):
        pos.append("answered")
        pwhy.append("正面回答了客户的问题")
    if act in ASK_ACTIONS and not any(c in said for c in (ANSWER_CUES.get(act) or ())) \
            and any(w in said for w in DEFER_WORDS):
        pos.append("deferred")
        pwhy.append("按合规说明了为什么现在不能答，并给出下一步")
    if fact and not _asks_again(t, fact) and any(w in said for w in ACK_WORDS):
        pos.append("fact_acked")
        pwhy.append("接住了客户给的事实")
    nxt = agent.get("next_stage") or ""
    if nxt and nxt != stage:
        must = (config_loader.stage_config(nxt).get("must_say") or [])
        if not must or all(k in said for k in must):
            pos.append("advanced")
            pwhy.append("推进到 %s%s" % (nxt, "且必答要素齐全" if must else ""))
    if pos:
        return {"polarity": "+", "codes": pos, "why": "；".join(pwhy),
                "evidence": said[:40], "stage": stage, "action": act, "target": ""}
    return {"polarity": "0", "codes": [], "why": "既无缺陷也未推进（追问/过渡句）",
            "evidence": said[:40], "stage": stage, "action": act, "target": ""}


def _target(neg, stage, user_text):
    """把负面反馈映射到**可学习开关**；映射不了就返回空（只记账，不自动改）。"""
    if "repeat" in neg and stage in ("OFFER", "CONFIRM"):
        return "policy:repair.%s" % stage
    if "repeat" in neg and stage == "GREETING":
        return "policy:greeting_company_alt"
    if "no_answer" in neg and stage in ("OFFER", "CONFIRM"):
        from modules import policy
        cls = policy.question_class(user_text)
        if cls:
            return "policy:answer_routes.%s.%s" % (stage, cls)
    if "generic_summary" in neg:
        return "policy:confirm_specific"
    return ""


def judge_session(session):
    """判一整通，返回逐轮条目 + 计数。"""
    turns = session.get("turns") or []
    rows = []
    for i in range(len(turns)):
        r = judge_turn(turns, i)
        r["turn"] = i + 1
        r["session"] = session.get("id", "")
        r["user_text"] = (turns[i].get("user") or {}).get("text", "")
        rows.append(r)
    neg = [r for r in rows if r["polarity"] == "-"]
    pos = [r for r in rows if r["polarity"] == "+"]
    codes = {}
    for r in neg:
        for c in r["codes"]:
            codes[c] = codes.get(c, 0) + 1
    return {"id": session.get("id", ""), "turns": rows, "neg": len(neg), "pos": len(pos),
            "neutral": len(rows) - len(neg) - len(pos), "codes": codes,
            "targets": sorted({r["target"] for r in neg if r["target"]})}


def summarise(report):
    """整批汇总：正/负条数、按缺陷码、按可学习目标、逐会话。"""
    sessions = [judge_session(s) for s in (report.get("sessions") or [])]
    codes, targets = {}, {}
    for s in sessions:
        for c, n in s["codes"].items():
            codes[c] = codes.get(c, 0) + n
        for t in s["targets"]:
            targets[t] = targets.get(t, 0) + 1
    neg = sum(s["neg"] for s in sessions)
    pos = sum(s["pos"] for s in sessions)
    turns = sum(len(s["turns"]) for s in sessions)
    return {
        "turns": turns, "neg": neg, "pos": pos,
        "neg_rate": round(neg / float(turns), 4) if turns else 0.0,
        "codes": dict(sorted(codes.items(), key=lambda kv: -kv[1])),
        "targets": dict(sorted(targets.items(), key=lambda kv: -kv[1])),
        "sessions": [{"id": s["id"], "neg": s["neg"], "pos": s["pos"],
                      "codes": s["codes"], "turns": len(s["turns"])} for s in sessions],
        "detail": sessions,
        "failed_sessions": [{"id": r["id"], "fault": r.get("fault")}
                            for r in (report.get("rows") or []) if not r.get("ok")],
    }
