"""真实通话审计（11-09）。

和 modules/attribution.py 的分工：
    attribution  给**模拟**会话定性（有画像的 expect.closed、有 user.action 动作名）；
    real_audit   给**真实**通话找缺陷（没有画像、没有动作名，只有：
                 用户原话（≥4 位数字已按隐私配置打码）、客服原话、阶段、意图）。

为什么必须单独一套判据：真实通话里用户没有 `action` 字段（那是模拟器给的动作名），
归因器里「用户交了事实，客服又问一遍」这类判据直接搬过来会全部落空。真实数据里
能用的证据只有两类，本模块只用这两类：
    1. 逐字文本（客服两句原话是否相同、客服这句是否在要"后四位"）；
    2. 事件结构（这一轮是沉默还是识别出字、结束时有没有回写）。

每条缺陷都带 evidence（客服原话片段，人名与数字已脱敏），报告里直接展示——
结论可以被伪造，原话不能。判定全部是纯函数，不调模型、不联网、同输入同输出。

一个前提写清楚：真实通话里的「问题话术」可能是**旧版本**留下的。本模块只负责
找出来，是否仍然存在于当前配置由调用方（scripts/mine_calls.py）判断，
不在这里猜。
"""

import re

#: 缺陷码 → 人话（报告与看板共用）
CODES = {
    "real_silence_repeat": "连续沉默时客服逐字重复同一句",
    "real_repeat": "客服相邻两轮逐字重复",
    "real_loop3": "客服连续三轮逐字重复",
    "real_slot_loss": "用户已给数字，客服又要件同一条",
    "real_vague_ref": "核身要「后四位」但没说哪个号码",
    "real_wrong_credential": "把银行卡/身份证当核身凭证",
    "real_no_answer": "用户问「怎么做」，客服只回驳回原因",
}

#: 客服在要核身数字
_ASKS_TAIL = ("后四位", "尾号", "最后四位")
#: 说清楚了是「哪个号码」的锚点。缺了就是指代含糊
_TAIL_ANCHORS = ("手机", "预留", "联系电话", "来电号码", "电话")
#: 拿别的证件当核身凭证——手机尾号才是本场景的凭证
_WRONG_CRED = ("银行卡", "身份证", "统一社会信用代码", "营业执照号")

#: 用户在问「接下来怎么办」——客服只复述原因就是答非所问
_ACTION_ASK = ("怎么做", "怎么办", "怎么弄", "如何", "步骤", "流程", "接下来")
#: 真「可执行」的说法。注意「材料」不算——「原因是材料不全」也含这两个字，
#: 但那是原因不是动作，算进来会把答非所问全放过去（自测第 7 项踩过）。
_ACTION_WORDS = ("补充", "提交", "上传", "后台", "补齐", "重新申请", "准备好", "补办")
#: 只复述原因的信号
_REASON_WORDS = ("驳回原因", "没有通过审核", "未通过", "原因是")

#: 隐私：入库时已把 ≥4 位数字打成 *；报告里再兜一层，防旧数据或客服原话漏网
_MASK_DIGITS = re.compile(r"\d{4,}")
_NAME_CALL = re.compile(r"([\u4e00-\u9fa5]{2,3})(女士|先生)")


def redact(text):
    """报告用：数字打码 + 「某某女士/先生」改成「某女士/先生」。"""
    if not text:
        return ""
    out = _MASK_DIGITS.sub(lambda m: "*" * len(m.group(0)), str(text))
    return _NAME_CALL.sub(lambda m: "*" + m.group(2), out)


def _said(turn):
    return (turn.get("agent") or {}).get("text") or ""


def _user(turn):
    return turn.get("user") or {}


def _is_silence(turn):
    return (_user(turn).get("kind") == "silence") or not (_user(turn).get("text") or "").strip()


def _gave_digits(turn):
    """用户这一轮报了数字：入库时 ≥4 位被打成 *，所以「****」就是证据。"""
    return "*" * 4 in (_user(turn).get("text") or "")


def _asks_tail(text):
    return any(w in text for w in _ASKS_TAIL)


def _tail_is_clear(text):
    return any(w in text for w in _TAIL_ANCHORS)


def scan_session(turns):
    """扫一通真实通话，返回 {defects, metrics}。

    turns 每项：{"i":轮次, "user":{"text","kind"}, "agent":{"text","stage",
    "next_stage","intent"},"closed":bool}
    """
    defects = []

    def add(i, code, stage, evidence, **extra):
        item = {"turn": i, "code": code, "label": CODES.get(code, code),
                "stage": stage or "", "evidence": redact(evidence),
                # raw 只给「这句话是否还在当前配置里」做比对，报告落盘前必须删掉
                "raw": (said or "")[:60]}
        item.update(extra)
        defects.append(item)

    silence_hits = []
    for idx, t in enumerate(turns):
        said = _said(t)
        u = _user(t)
        stage = (t.get("agent") or {}).get("stage") or ""

        # 1) 沉默时逐字复读：同一句「您还在吗」在多次沉默里一字不差地重复
        if _is_silence(t) and said:
            silence_hits.append((idx, said))
        if _is_silence(t):
            for prev_idx, prev_said in silence_hits[:-1]:
                if prev_said == said:
                    add(t.get("i"), "real_silence_repeat", stage,
                        "第 %d 轮与第 %d 轮沉默时逐字同一句：%s"
                        % (t.get("i"), turns[prev_idx].get("i"), said[:40]),
                        occurrences=len(silence_hits))
                    break

        # 2) 相邻两轮逐字重复（含非沉默轮）：用户听感就是「卡带了」
        if idx >= 1 and said and said == _said(turns[idx - 1]):
            add(t.get("i"), "real_repeat", stage, "与上一轮逐字同一句：%s" % said[:40])

        # 3) 连续三轮逐字重复
        if idx >= 2 and said and said == _said(turns[idx - 1]) == _said(turns[idx - 2]):
            add(t.get("i"), "real_loop3", stage, "连续三轮同一句：%s" % said[:40])

        # 4) 用户刚报了数字（****），客服当场又要「后四位/尾号」
        if _gave_digits(t) and _asks_tail(said):
            add(t.get("i"), "real_slot_loss", stage,
                "用户已报「%s」，客服仍问：%s" % (u.get("text", "")[:12], said[:40]))

        # 5) 要「后四位」却没说清是哪个号码
        if _asks_tail(said) and not _tail_is_clear(said):
            add(t.get("i"), "real_vague_ref", stage, "要数字但指代不明：%s" % said[:48])

        # 6) 拿银行卡/身份证当核身凭证
        if _asks_tail(said) and any(w in said for w in _WRONG_CRED):
            add(t.get("i"), "real_wrong_credential", stage,
                "用错凭证核身：%s" % said[:48])

        # 7) 用户问「怎么做」，客服只回原因、不给动作
        if any(w in (u.get("text") or "") for w in _ACTION_ASK) \
                and any(w in said for w in _REASON_WORDS) \
                and not any(w in said for w in _ACTION_WORDS):
            add(t.get("i"), "real_no_answer", stage,
                "用户问：%s；客服只回原因：%s"
                % ((u.get("text") or "")[:16], said[:40]))

    silences = sum(1 for t in turns if _is_silence(t))
    stages = [(t.get("agent") or {}).get("stage") or "" for t in turns]
    return {
        "defects": defects,
        "metrics": {
            "turns": len(turns),
            "silence_turns": silences,
            "reached_need": any(s in ("NEED", "OFFER", "CONFIRM", "WRITEBACK", "DONE")
                                for s in stages),
            "closed": bool(turns and turns[-1].get("closed")),
            "max_stage": _max_stage(stages),
        },
    }


_STAGE_ORDER = ["GREETING", "IDENTITY", "NEED", "OFFER", "CONFIRM", "WRITEBACK", "DONE"]


def _max_stage(stages):
    """最深到达的阶段（用于看「卡在核身」的比例）。"""
    best = 0
    seen = set(stages)
    for i, name in enumerate(_STAGE_ORDER):
        if name in seen:
            best = i
    return _STAGE_ORDER[best] if seen else ""


def strip_raw(rows):
    """落盘前去掉 raw：报告只留脱敏证据，原文不随文件流出。"""
    for r in rows.values():
        for d in r.get("defects") or []:
            d.pop("raw", None)
    return rows


def cluster(defects_by_session):
    """按（缺陷码, 阶段, 意图）聚类。support = 涉及会话数。

    evidence 取两条，且只留脱敏原话——报告给人复核，不给机器刷分。
    """
    groups = {}
    for sid, item in defects_by_session.items():
        # 兼容两种入参：{"defects":[...]}（scan_session 的返回）或直接是缺陷列表
        defects = (item.get("defects") if isinstance(item, dict) else item) or []
        for d in defects:
            key = "%s|%s|%s" % (d["code"], d["stage"] or "?", d.get("intent") or "?")
            g = groups.setdefault(key, {"key": key, "code": d["code"],
                                        "label": d["label"], "stage": d["stage"],
                                        "sessions": set(), "samples": []})
            g["sessions"].add(sid)
            if len(g["samples"]) < 2:
                g["samples"].append({"session": sid, "turn": d["turn"],
                                     "evidence": d["evidence"]})
    out = []
    for g in groups.values():
        g["support"] = len(g["sessions"])
        g["sessions"] = sorted(g["sessions"])  # 集合转成列表：报告要能直接 json 落盘
        out.append(g)
    out.sort(key=lambda x: (-x["support"], x["code"]))
    return out
