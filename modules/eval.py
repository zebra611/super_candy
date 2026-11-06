"""M6 质检与 M7 评测/自进化 —— 质检报告已可用，评测与进化仍是占位。

现在能做的（真实现）：
    transcribe_session  从事件流还原"谁在什么时候说了什么"；
    compliance_report   逐项检查：闭环、字面禁语、分类违规（辱骂/虚假承诺/诱导）、
                        隐私泄露（完整手机号/身份证号）、必说话术是否说到；
    score_session       以 writeback 事件为唯一闭环证据（与质检口径一致）。

还没做的（角色 3 的开工清单）：
    1. 语义级合规检测（换个说法说同样的话，字面词表抓不到）；
    2. build_eval_set：把 eval/seed_cases.json 扩到 80–120 条并留 20% 留出集；
    3. simulate_user：自动扮演对抗性客户，把评测从"人工点"变成"跑批"；
    4. judge：规则评分 + LLM-as-Judge 双通道，同一输入必须同一分数（可复现）；
    5. cluster_failures：失败聚类到 没听清/没听懂/没查对/说错了/漏环节；
    6. propose_optimization + regression：产出优化项，跑同一批用例做前后对比。

负责人：角色 3（质检评测与自进化）
"""

import re

import config_loader
import events
from contracts import COMPLIANCE_CHECKS

# 完整手机号（11 位）或身份证号（18 位，末位可能是 X）。
# 只做字面级检测：把"念出完整号码"这种最严重的情况先挡住，
# "换种说法透露"要靠语义检测，那还没做。
_PII_PATTERN = re.compile(r"(?<!\d)(?:\d{11}|\d{17}[\dXx])(?!\d)")

# ---------------------------------------------------------------------------
# L4 语义级合规：句式模板层
#
# 为什么要有这一层：config 里的分类词表只做 `w in text` 的字面包含，
# 客服换一种说法（"这个名额今天给您留着，明天就没了"）就抓不到。
# 把"换个说法"归纳成句式，比穷举词表更能覆盖改写，而且零成本、可解释。
#
# 这些模板是占位内容，和词表同源问题；真实话术规范由企业提供后替换本表即可。
# ---------------------------------------------------------------------------
_TEMPLATE_RULES = (
    {"rule": "template:guarantee_profit", "category": "false_promise",
     "regex": re.compile(r"(?:保证|绝对|肯定|百分百|包您|包你)[^，。！？;；]{0,10}"
                         r"(?:赚|赚到|回本|有效|没问题|不亏)")},
    {"rule": "template:conditional_threat", "category": "inducement",
     "regex": re.compile(r"不[^，。！？;；]{0,6}就[^，。！？;；]{0,10}"
                         r"(?:没了|取消|涨价|收回|停掉|作废)")},
    {"rule": "template:scarcity", "category": "inducement",
     "regex": re.compile(r"最后一天|仅剩|名额有限|错过就没|今天不留就没了")},
    {"rule": "template:absolute_promise", "category": "false_promise",
     "regex": re.compile(r"(?:一定|肯定|百分百)[^，。！？;；]{0,8}(?:能|会)(?:退|赔|赔钱|到账)")},
)

# 否定语境豁免。命中位置往前看几个字，出现这些词就说明是在"否认"而不是"承诺"。
# 这是误报的主要来源：把"不能给您保证"当成承诺抓出来，比漏报更伤。
_NEGATION_WORDS = ("不", "无法", "没法", "不能", "不会", "不要", "别", "并非", "没有")
_NEGATION_WINDOW = 6


def _is_negated(text, start):
    """命中位置前面一小段里有没有否定词。"""
    window = text[max(0, start - _NEGATION_WINDOW):start]
    return any(w in window for w in _NEGATION_WORDS)


def _check_templates(replies):
    """按句式模板找"换个说法"的违规。

    返回 {类别: [{"rule": 规则名, "word": 命中原文, "source": "template"}]}
    只给命中原文和规则名，不写自然语言理由——理由要能被程序分析。
    """
    hits = {}
    for r in replies:
        text = r["text"] or ""
        if not text:
            continue
        for rule in _TEMPLATE_RULES:
            for m in rule["regex"].finditer(text):
                if _is_negated(text, m.start()):
                    continue
                hits.setdefault(rule["category"], []).append(
                    {"rule": rule["rule"], "word": m.group(0), "source": "template"})
    return hits


def transcribe_session(session_id):
    """把一通电话的事件流还原成"谁在什么时候说了什么"。

    L6 的三件事都在这里处理：
        1. 打断造成的半句——被打断的那句标 interrupted 并补省略号，不当作完整发言；
        2. ASR 中间结果去重——连续两次识别文本完全相同（流式 partial 与 final），只留一条；
        3. 时间对齐——ts 强制单调不减，并给出相邻轮的 dt_ms，不允许出现负数时延。

    返回：list[dict(role, text, ts, turn, dt_ms, interrupted?)]
    """
    evs = events.list_events(session_id)
    barge_turns = {e["turn_id"] for e in evs if e["type"] == "barge_in"}

    turns, last_user = [], None
    for ev in evs:
        if ev["type"] == "asr_result":
            text = (ev["payload"].get("text") or "").strip()
            if not text:
                continue
            if last_user is not None and last_user["text"] == text:
                # partial 与 final 同文：合并成一条，时间取最后一次（更接近说完的时刻）
                last_user["ts"] = ev["ts"]
                continue
            last_user = {"role": "user", "text": text, "ts": ev["ts"],
                         "turn": ev["turn_id"]}
            turns.append(last_user)
        elif ev["type"] == "agent_reply":
            text = ev["payload"].get("text", "") or ""
            item = {"role": "agent", "text": text, "ts": ev["ts"], "turn": ev["turn_id"]}
            if ev["turn_id"] in barge_turns:
                item["interrupted"] = True
                if text and not text.endswith(("。", "！", "？", "…")):
                    item["text"] = text + "…（被打断）"
            turns.append(item)
            last_user = None      # 客服说话即视为这一轮用户表达结束

    prev = None
    for i, t in enumerate(turns):
        if prev is not None and t["ts"] < prev:
            t["ts"] = prev        # 时钟回拨或乱序：钳到上一轮，保证单调
        t["dt_ms"] = 0 if i == 0 else t["ts"] - turns[i - 1]["ts"]
        prev = t["ts"]
    return turns


def _agent_replies(evs):
    """取出所有客服说过的话，带上它所属的阶段与去向，供各项检查复用。

    stage 是这句话属于哪个环节，next 是这句话把流程推进到哪。
    必说话术的检查要用到 next：进入某环节的那句话，往往是在上一个环节说的
    （比如"您上次有一笔订单"是在核验通过时说的，属于 NEED 的开场）。
    """
    return [{"stage": e["payload"].get("stage", ""),
             "next": e["payload"].get("next_stage", ""),
             "text": e["payload"].get("text", "")}
            for e in evs if e["type"] == "agent_reply"]


def _check_must_say(replies):
    """检查每个走到了的环节，有没有说到配置里的必说话术。

    判定范围（一个"环节窗口"）= 该环节自己的回复 + 把流程推进到该环节的那句回复。
    这样判定才不会被"开场白承担了下一环节的提问"这种正常写法误伤。

    没有走到的环节直接跳过（不算通过也不算失败）——没发生的环节不该被判违规。
    返回：dict(status, checked_stages, missing)
    """
    missing = []
    checked = 0
    for stage, cfg in config_loader.stages_config().items():
        words = [w for w in (cfg.get("must_say") or []) if w]
        if not words:
            continue
        window = [r["text"] for r in replies if r["stage"] == stage or r["next"] == stage]
        if not window:
            continue
        checked += 1
        joined = " ".join(window)
        lack = [w for w in words if w not in joined]
        if lack:
            missing.append({"stage": stage, "missing": lack})

    if not checked:
        status = "skipped"
    else:
        status = "fail" if missing else "pass"
    return {"status": status, "checked_stages": checked, "missing": missing}


def _check_categories(replies):
    """按违规类型分组做字面检测，返回 {类别: 命中的词}。

    词表来自 config.compliance.categories，是占位内容；真实词表由企业提供。
    这里刻意按类别分开，而不是笼统报"命中禁语"——质检报告要能指出是哪一类问题。
    """
    hits = {}
    for name, words in config_loader.compliance_categories().items():
        found = sorted({w for r in replies for w in words if w and w in r["text"]})
        if found:
            hits[name] = found
    return hits


def _check_privacy(replies):
    """检测客服是否念出了完整手机号/身份证号（字面级）。"""
    found = []
    for r in replies:
        for m in _PII_PATTERN.finditer(r["text"] or ""):
            found.append(m.group())
    return sorted(set(found))


def compliance_report(session_id):
    """出一份质检报告：逐项给结论，能判的判，判不了的明说判不了。

    为什么不用一个笼统的分数了事：赛题把合规当门槛，报告必须能指出
    "哪一项没过、在哪句话上过的"，否则改不动。

    返回：dict(session_id, closed, blocked_count, checks, score, is_placeholder, notes)
    """
    evs = events.list_events(session_id)
    replies = _agent_replies(evs)

    closed = any(e["type"] == "writeback" for e in evs)
    blocked = [e["payload"] for e in evs
               if e["type"] == "agent_reply" and e["payload"].get("blocked")]
    categories = _check_categories(replies)
    templates = _check_templates(replies)
    privacy_hits = _check_privacy(replies)
    must_say = _check_must_say(replies)

    def category_check(name):
        """命中明细统一成结构化字段。

        加了模板层之后必须能区分"词表抓到的"还是"句式抓到的"——
        两者要修的地方不一样：词表要补词，模板要补句式。
        """
        literal = categories.get(name) or []
        hits = [{"rule": "literal:%s" % name, "word": w, "source": "wordlist"} for w in literal]
        hits.extend(templates.get(name) or [])
        return {"status": "fail" if hits else "pass", "hits": hits, "note": ""}

    checks = {
        "no_insult": category_check("insult"),
        "no_false_promise": category_check("false_promise"),
        "no_inducement": category_check("inducement"),
        "no_privacy_leak": {
            "status": "fail" if privacy_hits else "pass",
            "hits": privacy_hits,
            "note": "只检测完整手机号/身份证号，语义泄露待实现",
        },
        "must_say_hit": must_say,
        # 契约里定义的检查项如果这里没有对应实现，补一个 skipped，避免"漏项不报"
        **{name: {"status": "skipped", "note": "未实现"}
           for name in COMPLIANCE_CHECKS if name not in
           ("no_insult", "no_false_promise", "no_inducement", "no_privacy_leak", "must_say_hit")},
    }

    failed = [name for name, c in checks.items() if c["status"] == "fail"]
    checked = [name for name, c in checks.items() if c["status"] != "skipped"]

    # 分数只是把各项结论压成一个便于排序的数字，明细永远比分数重要。
    score = 1.0
    if not closed:
        score -= 0.5
    score -= 0.2 * len(failed)
    score = max(0.0, round(score, 2))

    return {
        "session_id": session_id,
        "closed": closed,
        "must_say_stage_reached": must_say["status"] != "skipped",
        "blocked_count": len(blocked),
        "blocked_detail": [{"hits": b.get("hits", []), "replaced": b.get("replaced", False)}
                           for b in blocked],
        "checks": checks,
        "failed": failed,
        "checked": checked,
        "score": score,
        # L4 落地后，语义级合规的判断依据从"没有"变成"句式模板 + 否定豁免"，
        # 因此不再把"语义级合规检测"列为未实现项。仍然标占位，是因为：
        # 模板表是占位内容（真实话术规范待企业提供），开放域改写仍需 LLM-Judge。
        "is_placeholder": True,
        "semantic_layer": "template",
        "placeholder_reason": "句式模板为占位内容（真实话术规范待企业提供）；开放域语义改写与模拟用户跑批仍待实现",
    }


def score_session(session_id):
    """给一通电话打分（评测基线用）。

    闭环的唯一证据是 writeback 事件，不看回复文本、也不看阶段名——
    "已经为您登记"和"帮您记下了"是同一件事，文本匹配会判错；
    而 DONE 只是礼貌收尾，走到 DONE 不代表业务结果回写了。

    返回：dict(session_id, closed, score, detail)
    """
    evs = events.list_events(session_id)
    closed = any(e["type"] == "writeback" for e in evs)
    stages_seen = sorted({e["payload"].get("stage") for e in evs if e["type"] == "agent_reply"} - {None, ""})
    return {
        "session_id": session_id,
        "closed": closed,
        "score": 1.0 if closed else 0.0,
        "detail": {"stages_seen": stages_seen},
        "is_placeholder": True,
    }

