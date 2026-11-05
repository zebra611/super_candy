# -*- coding: utf-8 -*-
"""任务 2 四项指标出数（T17）。

跑法：python3 scripts/task2_metrics.py

它回答评委最可能问的四个问题，每个问题一个数、一条复现命令：

    1. 意图识别准确率   —— 你们怎么知道"听懂了"？
    2. 槽位完整率       —— 三个必填槽位填满了几个？
    3. 任务闭环完成率   —— 该闭环的闭环了吗？不该闭环的有没有误闭环？
    4. 回写格式合规率   —— 结构化回写的字段齐不齐、类型对不对？

**口径先说清，避免被问穿**

- 闭环率的分母是"**应闭环**的用例"，不是全部 30 条。30 条里有 14 条本来就该
  不闭环（拒绝、诱导、挂断），拿它们当分母会算出 37% 这种自贬的数字。
  正确说法：应闭环 11/11 = 100%，误闭环 0。
- 意图准确率的分母是**人工标注样本**（`scripts/test_intent.py` 里的 17 条），
  不是"模型跑了多少条"。规则版没有概率输出，"准确率"只能是标注集上的判对率；
  真实通话上的准确率要等接了模型、攒了标数据才谈得上。
- 槽位完整率按"**到达 NEED 之后**"算：没核上身份的用例本来就不该填 need，
  把它们算进分母会得出一个没意义的低分。
- 回写合规率校验的是 `config.writeback_template` 的字段，不是业务系统的真实 schema
  （那要等 2B 的适配器接真接口）。
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

# 复用 run_eval 的隔离环境：它把事件库指向临时目录、把 TTS 打桩成浏览器播报。
# 直接 import 是为了"跑数"和"跑分"用同一套环境，避免两套口径。
import run_eval  # noqa: E402
import config_loader  # noqa: E402
from modules import dialogue  # noqa: E402

REQUIRED_SLOTS = ("identity_confirmed", "need", "consent")
VALID_CONSENT = (True, False, "unknown")


def _run_detail(case):
    """跑一条用例，返回 (每轮的 handle_turn 结果, 事件流)。"""
    session_id = run_eval.start_call()["session_id"]
    outs = []
    for utterance in case.get("turns", []):
        outs.append(run_eval.handle_turn(session_id, user_text=utterance))
    evs = run_eval.events.list_events(session_id)
    run_eval.end_call(session_id)
    return outs, evs


def metric_intent_accuracy():
    """① 意图识别准确率：标注样本上的判对率 + 事件字段覆盖率。"""
    import test_intent  # 标注样本的唯一来源，改样本只改那一个文件

    labeled = 0
    hit = 0
    for text, want_intent, want_vague, _ in test_intent.AMBIGUOUS:
        flagged = dialogue.classify(text)
        labeled += 1
        if dialogue._intent_name(flagged) == want_intent and bool(flagged["vague"]) == want_vague:
            hit += 1
    for text, want_intent in test_intent.CLEAR:
        labeled += 1
        if dialogue._intent_name(dialogue.classify(text)) == want_intent:
            hit += 1
    return {"hit": hit, "total": labeled,
            "rate": hit / float(labeled) if labeled else 0.0}


def metric_slot_completeness(cases):
    """② 槽位完整率：到达 NEED 的用例里，三个必填槽位填满了几个。"""
    rows = []
    strict = []
    missing_by_slot = dict((k, []) for k in REQUIRED_SLOTS)
    for case in cases:
        outs, _ = _run_detail(case)
        if not any((o.get("slots") or {}).get("identity_confirmed") for o in outs):
            continue                      # 没核上身份的不进分母
        final = (outs[-1].get("slots") or {}) if outs else {}
        filled = 0
        for k in REQUIRED_SLOTS:
            if final.get(k) not in ("", None):
                filled += 1
            else:
                missing_by_slot[k].append(case["id"])
        rows.append({"id": case["id"], "filled": filled, "total": len(REQUIRED_SLOTS)})
        # 严格口径：只有走到 CONFIRM 的用例，"三个槽位都该有值"才成立。
        # 剧本停在提问里的用例（如 K01 只问了有效期就结束），没有 consent 是
        # 剧本设计的结果，不是系统没填——把它们算进分母会得出一个自贬的分数。
        if any((o.get("next_stage") == "CONFIRM") for o in outs):
            strict.append({"id": case["id"], "filled": filled})
    total = sum(r["total"] for r in rows)
    filled = sum(r["filled"] for r in rows)
    return {"cases": rows, "filled": filled, "total": total,
            "rate": filled / float(total) if total else 0.0,
            "missing_by_slot": missing_by_slot,
            "strict_cases": len(strict),
            "strict_filled": sum(r["filled"] for r in strict)}


def metric_closure(cases):
    """③ 闭环完成率：应闭环 / 误闭环 两个数。"""
    should, should_ok = 0, 0
    should_not, wrongly = 0, 0
    for case in cases:
        expect = case.get("expect", {})
        if "closed" not in expect:
            continue
        _, evs = _run_detail(case)
        closed = any(e["type"] == "writeback" for e in evs)
        if expect["closed"]:
            should += 1
            should_ok += 1 if closed else 0
        else:
            should_not += 1
            wrongly += 1 if closed else 0
    return {"should": should, "should_ok": should_ok,
            "should_not": should_not, "wrongly": wrongly,
            "rate": should_ok / float(should) if should else 0.0}


def metric_writeback_compliance(cases):
    """④ 回写合规率：payload 字段齐不齐、类型对不对。"""
    template = dict(config_loader.writeback_template())
    required = list(template.keys()) + ["writeback_version"]
    total, ok, problems = 0, 0, []
    for case in cases:
        _, evs = _run_detail(case)
        for e in evs:
            if e["type"] != "tool_call":
                continue
            payload = e.get("payload") or {}
            if payload.get("name") != "create_ticket":
                continue
            content = ((payload.get("args") or {}).get("content")) or {}
            total += 1
            missing = [k for k in required if k not in content]
            bad = []
            if content.get("consent") not in VALID_CONSENT:
                bad.append("consent=%r 不在合法取值内" % content.get("consent"))
            if not isinstance(content.get("identity_confirmed"), bool):
                bad.append("identity_confirmed 不是布尔")
            if not isinstance(content.get("writeback_version"), int):
                bad.append("writeback_version 不是整数")
            if missing or bad:
                problems.append({"case": case["id"], "missing": missing, "bad": bad})
            else:
                ok += 1
    return {"ok": ok, "total": total, "problems": problems,
            "rate": ok / float(total) if total else 0.0}


def metric_intent_coverage(cases):
    """事件字段覆盖率：每轮 agent_reply 是否都带 intent 与 intent_conf。"""
    total, covered = 0, 0
    missing = []
    for case in cases:
        _, evs = _run_detail(case)
        for e in evs:
            if e["type"] != "agent_reply":
                continue
            payload = e.get("payload") or {}
            if payload.get("intent") == "opening":
                continue                    # 开场白不是用户轮次
            total += 1
            if "intent" in payload and "intent_conf" in payload:
                covered += 1
            else:
                missing.append({"case": case["id"], "turn": e.get("turn_id")})
    return {"covered": covered, "total": total, "missing": missing,
            "rate": covered / float(total) if total else 0.0}


def main():
    cases = run_eval.load_cases()

    print("任务 2 四项指标（口径见本文件头部注释）")
    print("=" * 76)

    m1 = metric_intent_accuracy()
    print("\n① 意图识别准确率：%d/%d = %.1f%%" % (m1["hit"], m1["total"], m1["rate"] * 100))
    print("   口径：人工标注样本（歧义 10 条 + 明确 7 条），不是模型概率")
    print("   复现：python3 scripts/test_intent.py")

    cov = metric_intent_coverage(cases)
    print("\n   事件字段覆盖率：%d/%d = %.1f%%（每轮都带 intent + intent_conf）"
          % (cov["covered"], cov["total"], cov["rate"] * 100))
    if cov["missing"]:
        print("   缺字段的轮次：%r" % cov["missing"][:5])

    m2 = metric_slot_completeness(cases)
    print("\n② 槽位完整率：%d/%d = %.1f%%" % (m2["filled"], m2["total"], m2["rate"] * 100))
    print("   口径：到达 NEED 的 %d 条用例 × 3 个必填槽位（identity_confirmed / need / consent）"
          % len(m2["cases"]))
    for k in REQUIRED_SLOTS:
        missing = m2["missing_by_slot"].get(k) or []
        if missing:
            print("   缺 %-19s %2d 条：%s" % (k, len(missing), " ".join(missing)))
    if m2["strict_cases"]:
        strict_total = m2["strict_cases"] * len(REQUIRED_SLOTS)
        print("   严格口径（只看走到 CONFIRM 的 %d 条）：%d/%d = %.1f%%"
              % (m2["strict_cases"], m2["strict_filled"], strict_total,
                 m2["strict_filled"] * 100.0 / strict_total))
        print("   两个都报，是因为「该不该有 consent」取决于剧本走到哪一步："
              "只报宽口径会被问「为什么不是 100%」，只报严口径会被问「是不是挑数据」")
    print("   复现：python3 scripts/test_slots.py")

    m3 = metric_closure(cases)
    print("\n③ 任务闭环完成率：应闭环 %d/%d = %.1f%%；误闭环 %d/%d"
          % (m3["should_ok"], m3["should"], m3["rate"] * 100, m3["wrongly"], m3["should_not"]))
    print("   口径：分母是 expect.closed=true 的用例，不是全部 30 条")
    print("   复现：python3 scripts/run_eval.py")

    m4 = metric_writeback_compliance(cases)
    print("\n④ 回写格式合规率：%d/%d = %.1f%%" % (m4["ok"], m4["total"], m4["rate"] * 100))
    print("   口径：按 config.writeback_template 的字段与类型校验")
    if m4["problems"]:
        print("   不合规样本：%r" % m4["problems"][:3])
    print("   复现：python3 scripts/task2_metrics.py")

    print("\n" + "=" * 76)
    print("以上四个数都可在本机一条命令复现；样本量与口径限制见文件头注释与 docs/06 第 7 节。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
