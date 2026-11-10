"""失败归因：把"这通电话没闭环"拆成系统缺陷 / 用例缺陷 / 模拟器设定。

为什么必须有这一层（这是 L2 能不能信的分水岭）：
    上一代回放式评测里，40 条失败有 24 条是"用例自己不可能通过"造成的
    （`docs/28` 清单 B：5 个人设的台词里根本没有可核身信息）。那 24 条如果
    直接丢进聚类器，聚出来的"高频缺陷"全是假的，之后每一步优化都在打空气。
    所以聚类之前先判**责任在谁**：只有 `agent` 类缺陷才允许进入优化池。

三类责任：
    agent  系统真的做错了（用户明明给了信息却没接住、把提问当杂音、绕圈重复）
    case   用例不可行（画像嘴里根本没有系统需要的那条信息）
    sim    模拟器设定导致的合理结局（秒挂、打错人、拒绝提供、跑题不确认）
           —— 不是缺陷，但必须记账，否则会被误当成"通过率怎么这么低"。

刻意用**规则**而不是 LLM 判官：
    归因结论要能逐条复算、能被队友质疑时当场回放。LLM 判官在这里既慢又会漂，
    而这几类缺陷的判据其实很确定（"用户报了尾号，下一句还在问后四位"）。
"""

# 缺陷码。**只增不改名**：聚类键与版本对比都按它对齐。
AGENT_SLOT_LOSS = "agent_slot_loss"          # 用户交了信息，客服没接住/又问一遍
AGENT_NOISE_ON_QUESTION = "agent_noise_on_question"  # 用户提问被当成"没听清/有杂音"
AGENT_LOOP = "agent_loop"                    # 连续重复同义话术，原地绕圈
AGENT_NO_PROGRESS = "agent_no_progress"      # 多轮不推进阶段且不给新信息
AGENT_PREMATURE_WRITEBACK = "agent_premature_writeback"  # "没听明白"被当成"拒绝"，直接回写
AGENT_PREMATURE_CLOSE = "agent_premature_close"          # 客户没同意/没核身，工单却回写了
AGENT_REPAIR_IGNORED = "agent_repair_ignored"            # 客户要重说，客服答「确认不到/转专员」
CASE_INFEASIBLE = "case_infeasible"          # 画像缺少系统必需的信息
SIM_BY_DESIGN = "sim_by_design"              # 秒挂/打错/拒绝/跑题，按设计不闭环

#: 只有这一类进优化池
AGENT_DEFECTS = (AGENT_SLOT_LOSS, AGENT_NOISE_ON_QUESTION, AGENT_LOOP,
                 AGENT_NO_PROGRESS, AGENT_PREMATURE_WRITEBACK,
                 AGENT_PREMATURE_CLOSE, AGENT_REPAIR_IGNORED)

#: 客户要重听、客服却拿"答不上来"的兜底话术顶上。
#: 为什么要单独成码：这类应答不会让闭环率掉下来（客户多半还会继续配合），
#: 所以通过率看着 17/17，实际每一轮都在答非所问——11-10 的 A17 就是这样：
#: OFFER 阶段客户说"信号不好，你再说一遍"，客服回"这个我这边确认不到，
#: 稍后让专员给您回电说明"，用例照样判过。闭环不等于做对。
_FALLBACK_MARKS = ("确认不到", "让专员给您回电", "让专员回电", "稍后让专员")

#: "没听懂"的典型说法。它和"我不同意"是两件事：
#: 前者该再解释一遍，后者才该记拒绝。混为一谈会把"还没听懂的人"记成"不愿整改"。
_COMPREHENSION_DENY = ("没明白", "没听明白", "没听懂", "不清楚", "搞不懂", "没太懂")
_REFUSAL_DENY = ("不办", "不需要", "不用了", "别打了", "不整改")

#: 判定"客服在重复问同一条"的关键词。
#:
#: contact_name 刻意**不收裸「姓名」**：性格适配层会在句尾挂「说联系人姓名也行」，
#: 客服原话其实是「核对上了，感谢配合……」——裸词会把"已经接住、顺手给个备选
#: 路径"误判成"又问一遍"（11-09 实测 A04/A06/A09 三条都是这个假阳）。
#: 判据宁可漏：记进去的每一条都要能被原话证实。
_RE_ASK = {
    "phone_tail": ("后四位", "后 4 位"),
    "contact_name": ("您的姓名", "怎么称呼", "怎么叫您", "您贵姓",
                     "姓名是", "报一下姓名", "确认一下姓名", "说一下姓名"),
    "application_id": ("申请单编号", "单号"),
}

#: 客服把用户的话当噪声的典型措辞（Q1-D1 的原始现象）
_NOISE_WORDS = ("杂音", "没听清", "没太听清", "听不清", "信号不太好")

#: 按设计就不该闭环的画像目标
_DESIGNED_DROPOFF = ("refuse", "wrong_person", "hangup_now", "offtopic")

#: 客户明确表示"清楚了 / 明白了"的动作。出现它时，即使用例写着 expect.closed=False，
#: 也不能断定是客服擅自回写——可能是画像被说服了，属于**用例期望待复核**。
#: 只认这一个动作："我就是这家公司的负责人"（yes_company）是核身，不是同意整改。
_CONSENT_ACTIONS = ("confirm",)


def premature_close(turns, goal, expect_closed, closed):
    """期望「不该闭环」却真回写了，返回一条缺陷；否则 None。

    为什么单独成码（而不是并进 `unexplained`）：
        11-09 审 PR #44 时，A09/A14 的期望是**不闭环**，实际因复述插进确认计数
        产生了 `unknown` 回写。它会让通过率掉下来，但当时没有任何缺陷码能证实
        原因，只能记成 `unexplained`——下一轮再犯，还是要靠人眼从 rows 里翻。
        误闭环比"答一句确认不到"更贵：工单里多出一条"商家承诺整改"的错误结论。

    为什么不看最后一轮是否出现 `confirm` 就判：
        客户说了"清楚了"再回写，可能是画像真被说服了，那是用例期望该改，
        不是客服的错。判据宁可漏，不可冤枉——记进去的每一条都要能当场复算。
    """
    if expect_closed is not False or not closed:
        return None

    last_user = (turns[-1].get("user") or {}) if turns else {}
    if (last_user.get("action") or "") in _CONSENT_ACTIONS:
        return None

    wb, idx = None, len(turns)
    for i, t in enumerate(turns):
        agent = t.get("agent") or {}
        if (agent.get("next_stage") in ("WRITEBACK", "DONE")
                or agent.get("stage") in ("WRITEBACK", "DONE")):
            wb, idx = t, i
            break
    wb = wb or (turns[-1] if turns else {})
    user = wb.get("user") or {}
    return {
        "turn": idx + 1,
        "code": AGENT_PREMATURE_CLOSE,
        "stage": _stage(wb) or "CONFIRM",
        "user_action": user.get("action", ""),
        "agent_intent": _intent(wb),
        "evidence": "期望不闭环（goal=%s），客户说「%s」，客服仍回写了工单"
                    % (goal, (user.get("text") or last_user.get("text") or "")[:14]),
    }


def _agent_said(turn):
    return (turn.get("agent") or {}).get("reply_text") or ""


def _asks_again(turn, fact_key):
    """客服这一句是不是又在要同一条信息。"""
    said = _agent_said(turn)
    return any(w in said for w in _RE_ASK.get(fact_key, ()))


def _stage(turn):
    return (turn.get("agent") or {}).get("stage") or ""


def _intent(turn):
    return (turn.get("agent") or {}).get("intent") or "unknown"


def analyse_turns(turns):
    """逐轮找缺陷，返回 [{turn, code, stage, user_action, agent_intent, evidence}]。

    只找**能被原始文本证实**的缺陷：每条都带 evidence（客服原话片段），
    报告里直接展示，方便人复核——结论可以被伪造，原话不能。
    """
    out = []

    for i, t in enumerate(turns):
        user = t.get("user") or {}
        action = user.get("action") or ""
        said = _agent_said(t)
        nxt = turns[i + 1] if i + 1 < len(turns) else None

        # 1) 用户交了事实，客服**这一句的回复**还在要同一条 → 槽位丢失。
        #    必须看同一轮：用户报完尾号，客服当场又"麻烦说一下后四位"，
        #    这就是没接住。上一版看的是下一轮，等于晚一拍才发现，
        #    而且用户已经因此重复一遍，证据被污染了。
        if action in ("answer_tail", "answer_name", "answer_order"):
            key = {"answer_tail": "phone_tail", "answer_name": "contact_name",
                   "answer_order": "application_id"}[action]
            if _asks_again(t, key):
                out.append({
                    "turn": i + 1, "code": AGENT_SLOT_LOSS, "stage": _stage(t),
                    "user_action": action, "agent_intent": _intent(t),
                    "evidence": "用户已报「%s」，客服仍问：%s"
                                % (user.get("text", "")[:12], said[:40]),
                })

        # 2) 客户说「听不清/你再说一遍」，客服回「确认不到 / 让专员回电」。
        #    这不是"答不上来"，是根本没把重说请求当成一个动作：客户要的是
        #    把上一句再讲一遍，客服却去查知识库或转专员。它不会掉闭环率
        #    （客户多半还会继续配合），所以必须单独记，否则永远藏在"通过"里。
        if action == "mishear" and any(w in said for w in _FALLBACK_MARKS):
            out.append({
                "turn": i + 1, "code": AGENT_REPAIR_IGNORED, "stage": _stage(t),
                "user_action": action, "agent_intent": _intent(t),
                "evidence": "客户要重听，客服回兜底：%s" % said[:40],
            })

        # 3) 用户在提问，客服回"有杂音/没听清" → 把有实义的输入当噪声
        if action in ("ask_why", "ask_material", "ask_fee", "cant_remember",
                      "refuse_info", "reask_already_told"):
            if any(w in said for w in _NOISE_WORDS):
                out.append({
                    "turn": i + 1, "code": AGENT_NOISE_ON_QUESTION, "stage": _stage(t),
                    "user_action": action, "agent_intent": _intent(t),
                    "evidence": "用户问：%s；客服答：%s"
                                % (user.get("text", "")[:16], said[:40]),
                })

        # 4) 同一句反复出现（换了标点也算） → 原地绕圈
        if i >= 2:
            a, b, c = turns[i - 2], turns[i - 1], t
            if (_agent_said(a) == _agent_said(b) == _agent_said(c)
                    and _agent_said(c)):
                out.append({
                    "turn": i + 1, "code": AGENT_LOOP, "stage": _stage(t),
                    "user_action": action, "agent_intent": _intent(t),
                    "evidence": "连续三轮同一句：%s" % _agent_said(t)[:40],
                })

        # 5) 用户说"没听明白"，客服这一轮直接回写 ⇒ 把"没听懂"当成了"不同意"。
        #    规格是「两次不确认才记 unknown」，而这里第一次就落库 false。
        #    为什么单列：它不会让通过率掉下来（工单照样建出来了），
        #    但业务侧收到的是"商家拒绝整改"这个错误结论——沉默的错误最贵。
        user_text_now = user.get("text") or ""
        # 「进了 DONE」不等于「回写了」。11-08 修完「没听明白」后，客服会在
        # 第二次没听懂时转专员收线（_finish，带 end_reason，不产生回写事件）；
        # 旧判据只看阶段，把这种收线误报成「提前回写」——是假阳性，不是缺陷。
        _agent_now = t.get("agent") or {}
        wrote_back = (not _agent_now.get("end_reason")
                      and (_stage(t) in ("WRITEBACK", "DONE")
                           or _agent_now.get("next_stage") in ("WRITEBACK", "DONE")))
        if (action == "deny" and wrote_back
                and any(w in user_text_now for w in _COMPREHENSION_DENY)
                and not any(w in user_text_now for w in _REFUSAL_DENY)):
            out.append({
                "turn": i + 1, "code": AGENT_PREMATURE_WRITEBACK, "stage": "CONFIRM",
                "user_action": action, "agent_intent": _intent(t),
                "evidence": "用户说「%s」，客服直接回写：%s"
                            % (user_text_now[:14], said[:40]),
            })

    # 4) 走完一轮通话还停在 IDENTITY 且始终没核上 → 记为无进展（只记一次）
    if len(turns) >= 4:
        stages = [_stage(t) for t in turns]
        if stages and all(s in ("", "GREETING", "IDENTITY") for s in stages):
            out.append({
                "turn": len(turns), "code": AGENT_NO_PROGRESS, "stage": stages[-1],
                "user_action": (turns[-1].get("user") or {}).get("action", ""),
                "agent_intent": _intent(turns[-1]),
                "evidence": "%d 轮全部停留在 %s" % (len(turns), "/".join(sorted(set(stages)))),
            })
    return out


def analyse_session(session, persona):
    """给一整通电话定性：期望是否达成、责任在谁、聚类键是什么。

    参数：
        session  sim_eval 产出的会话记录（含 turns / company / closed / end_by）
        persona  画像原文（取 goal / split / expect）

    返回 dict，字段：
        ok         期望是否达成（closed 与 expect.closed 一致）
        closed     是否真的回写了工单
        defects    [{code, stage, user_action, agent_intent, evidence}]
        fault      责任归属：agent / case / sim / none
        keys       聚类键列表（fault != agent 的不进优化池）
    """
    turns = session.get("turns") or []
    expect = persona.get("expect") or {}
    goal = persona.get("goal") or "confirm"
    closed = bool(session.get("closed"))
    defects = analyse_turns(turns)

    want_closed = expect.get("closed")
    ok = (want_closed is None) or (bool(want_closed) == closed)

    # 期望「不该闭环」却真回写了：补一条能证实的缺陷，不留给 unexplained。
    # 与 `agent_premature_writeback` 互斥：那条已经说明"没听明白被当成拒绝"，
    # 同一通电话再记一条只是把同一件事数两遍。
    if not any(d["code"] == AGENT_PREMATURE_WRITEBACK for d in defects):
        _close = premature_close(turns, goal, want_closed, closed)
        if _close:
            defects.append(_close)

    # ---- 责任判定（顺序即优先级）----
    # 用例不可行优先于一切：画像自己没这条信息，系统怎么做都闭不了环。
    infeasible = session.get("blocked_by_missing_fact") or []
    agent_defects = [d for d in defects if d["code"] in AGENT_DEFECTS]

    if infeasible and not closed:
        fault = "case"
    elif agent_defects:
        fault = "agent"
    elif goal in _DESIGNED_DROPOFF and not closed:
        fault = "sim"
    elif not ok:
        # 期望没达成、又找不到可证实的系统缺陷 → 不能硬扣帽子，单列待查
        fault = "unexplained"
    else:
        fault = "none"

    keys = []
    for d in defects:
        if d["code"] in AGENT_DEFECTS:
            keys.append("%s|%s|%s|%s" % (d["code"], d["stage"] or "?",
                                         d["user_action"], d["agent_intent"]))

    return {
        "ok": ok, "closed": closed, "fault": fault,
        "defects": defects, "keys": sorted(set(keys)),
        "expect": expect, "goal": goal,
        "blocked_by_missing_fact": infeasible,
    }


def summarise(sessions, personas_by_id):
    """整批汇总：通过率、按 split 分、按责任分。

    为什么 split 要分开算：holdout 只做验收，如果它和 train 混在一个数字里，
    就没法回答"这个改动是泛化了还是只拟合了调参集"。
    """
    by_id = {p["id"]: p for p in personas_by_id}
    rows = []
    for s in sessions:
        p = by_id.get(s["id"]) or {}
        a = analyse_session(s, p)
        rows.append({**a, "id": s["id"], "label": s.get("label", ""),
                     "split": p.get("split", "train")})

    def _agg(sub):
        if not sub:
            return {"total": 0, "ok": 0, "rate": 0.0}
        n = len(sub)
        k = sum(1 for r in sub if r["ok"])
        return {"total": n, "ok": k, "rate": round(k / float(n), 4)}

    faults = {}
    codes = {}
    for r in rows:
        faults[r["fault"]] = faults.get(r["fault"], 0) + 1
        for d in r["defects"]:
            codes[d["code"]] = codes.get(d["code"], 0) + 1

    return {
        "rows": rows,
        "all": _agg(rows),
        "train": _agg([r for r in rows if r["split"] == "train"]),
        "holdout": _agg([r for r in rows if r["split"] == "holdout"]),
        "faults": faults,
        "codes": codes,
        "agent_defect_sessions": sum(1 for r in rows if r["fault"] == "agent"),
        "case_defect_sessions": sum(1 for r in rows if r["fault"] == "case"),
    }
