# -*- coding: utf-8 -*-
"""Q1：画像工厂 40 条失败用例分诊。

跑法：
    python scripts/triage_personas.py
    python scripts/triage_personas.py --tag 1106 --json eval/reports/triage_personas_1106.json

产出（两张清单的原料）：
    eval/reports/triage_personas_<tag>.md   按 persona × 场景分组的 transcript + 特征
    eval/reports/triage_personas_<tag>.json 同上，供 Q2 课程进化直接消费

**分诊口径（这是本脚本唯一重要的事）**
    失败分两类，不能混：
      用例错 —— turns 与 persona/expect 自相矛盾，或用了系统明确不支持的说法
                （例：中文数字尾号「八八几几」，规则层只认阿拉伯数字）。
                改用例，不改代码；改完要说明为什么不是系统缺陷。
      系统错 —— 对话逻辑在**合理输入**上走错（例：用户问「你先说号码哪来的」
                被当成「有杂音没听清」，连问四轮后挂断）。这类编号进缺陷清单。

    脚本只输出**证据 + 规则特征**，不代替人下结论：
    特征能提示"这轮没给槽位且被判噪声"，但"该不该判噪声"要看上下文。
"""

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import run_eval as R                      # noqa: E402
from modules import dialogue as D         # noqa: E402

PERSONAS = os.path.join(ROOT, "eval", "personas_generated.json")
# 画像工厂的 id 与 persona 的对应：EV01-EV04 = C01 的 4 个场景，EV05-EV08 = C02 ……
PERSONA_NAMES = {
    "C01": "含糊老人", "C02": "愤怒客户", "C03": "蛮横客户", "C04": "得寸进尺",
    "C05": "听不清", "C06": "赶时间", "C07": "合规试探", "C08": "沉默挂断",
    "C09": "方言口音", "C10": "反复改口",
}
CN_DIGITS = re.compile(r"[零一二三四五六七八九]{4,}")


def _persona_of(case):
    """从 tag（persona_c01）反查 persona id；没有就按序号推。"""
    tag = str(case.get("tag") or "")
    m = re.search(r"persona_(c\d+)", tag, re.I)
    if m:
        return m.group(1).upper()
    try:
        idx = int(str(case.get("id", "EV00"))[2:])
        return "C%02d" % ((idx - 1) // 4 + 1)
    except Exception:
        return "?"


def _run(case):
    sid = R.start_call()["session_id"]
    turns = []
    last = {}
    for t in case.get("turns", []):
        o = R.handle_turn(sid, user_text=t)
        f = D.classify(t)
        turns.append({
            "user": t, "stage": o.get("stage"), "next": o.get("next_stage"),
            "intent": o.get("intent"), "reply": o.get("reply_text") or "",
            "flags": {k: f.get(k) for k in ("ask", "no", "yes", "vague", "angry",
                                            "complain", "digits", "evidence")},
        })
        last = o
    evs = R.events.list_events(sid)
    R.end_call(sid)
    ok, problems, closed, consent = R.judge(case, last, evs)
    return {
        "ok": ok, "problems": problems, "closed": closed, "consent": consent,
        "end_reason": last.get("end_reason") or "",
        "writeback": sum(1 for e in evs if e["type"] == "writeback"),
        "turns": turns,
    }


def _features(case, run):
    users = [t["user"] for t in run["turns"]]
    joined = " ".join(users)
    noise_turns = [i + 1 for i, t in enumerate(run["turns"])
                   if t["intent"] == "noise"]
    ask_turns = [i + 1 for i, t in enumerate(run["turns"])
                 if t["flags"].get("ask")]
    return {
        "noise_rounds": len(noise_turns),
        "noise_at": noise_turns,
        "ask_rounds": len(ask_turns),
        "ask_at": ask_turns,
        "has_arabic_digit": bool(re.search(r"\d", joined)),
        "has_chinese_digit": bool(CN_DIGITS.search(joined)),
        "end_reason": run["end_reason"],
        "writeback": run["writeback"],
        "expect_closed": (case.get("expect") or {}).get("closed"),
        "expect_consent": (case.get("expect") or {}).get("consent"),
    }


def _suspects(feat):
    """规则提示（不是结论）：帮助人快速定位，误报可接受，漏报不可接受。"""
    s = []
    if feat["noise_rounds"] >= 3:
        s.append("连续 %d 轮被判噪声（提问/质疑被当成『没听清』）"
                 % feat["noise_rounds"])
    if feat["ask_rounds"] and feat["noise_rounds"]:
        s.append("%d 轮是真提问（ask），其中若干轮落进噪声通道" % feat["ask_rounds"])
    if feat["has_chinese_digit"] and not feat["has_arabic_digit"]:
        s.append("只给了中文数字尾号（规则层只认阿拉伯数字）")
    if feat["end_reason"] in ("refused_identity", "identity_failed", "no_voice"):
        s.append("以 %s 收尾" % feat["end_reason"])
    if feat["expect_closed"] and not feat["writeback"]:
        s.append("期望闭环但无 writeback")
    if feat["expect_consent"] is False and feat["expect_closed"] is True:
        s.append("expect 自相矛盾检查：consent=False 但 closed=True（需人工确认是否设计如此）")
    return s


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="1106")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    if not os.path.exists(PERSONAS):
        print("缺少 eval/personas_generated.json（Q4 起不进仓库）：先跑 "
              "python scripts/gen_adversarial.py")
        return 2

    with open(PERSONAS, encoding="utf-8") as f:
        cases = json.load(f)

    results = []
    for case in cases:
        run = _run(case)
        feat = _features(case, run)
        results.append({
            "id": case["id"], "persona": _persona_of(case),
            "persona_name": PERSONA_NAMES.get(_persona_of(case), ""),
            "goal": case.get("goal", ""), "expect": case.get("expect"),
            "turns_in": case.get("turns", []),
            "run": run, "features": feat,
            "suspects": _suspects(feat),
        })

    fails = [r for r in results if not r["run"]["ok"]]
    print("=" * 78)
    print("Q1 分诊：%d 条，失败 %d 条" % (len(results), len(fails)))
    print("=" * 78)
    by_persona = {}
    for r in fails:
        by_persona.setdefault(r["persona"], []).append(r)
    for pid in sorted(by_persona):
        rs = by_persona[pid]
        avg_noise = sum(x["features"]["noise_rounds"] for x in rs) / float(len(rs))
        print("  %-4s %-8s 失败 %d 条｜平均噪声轮 %.1f｜失败 id：%s"
              % (pid, PERSONA_NAMES.get(pid, ""), len(rs), avg_noise,
                 ", ".join(x["id"] for x in rs)))

    # ---- markdown ----
    reports = os.path.join(ROOT, "eval", "reports")
    os.makedirs(reports, exist_ok=True)
    md = [
        "# Q1 画像工厂分诊（%s）" % args.tag,
        "",
        "> 生成：`python scripts/triage_personas.py --tag %s`；用例：`eval/personas_generated.json`。" % args.tag,
        "> 口径：脚本只出**证据 + 规则特征**，归因由人定稿（见文末两张清单）。",
        "",
        "## 失败面",
        "",
        "总数 %d，失败 **%d**。" % (len(results), len(fails)),
        "",
        "| persona | 名称 | 失败数 | 平均噪声轮 | 失败 id |",
        "|---|---|---:|---:|---|",
    ]
    for pid in sorted(by_persona):
        rs = by_persona[pid]
        md.append("| %s | %s | %d | %.1f | %s |" % (
            pid, PERSONA_NAMES.get(pid, ""), len(rs),
            sum(x["features"]["noise_rounds"] for x in rs) / float(len(rs)),
            ", ".join(x["id"] for x in rs)))
    md.append("")
    md.append("## 逐条 transcript")
    for r in fails:
        md.append("")
        md.append("### %s ｜ %s %s" % (r["id"], r["persona"], r["persona_name"]))
        md.append("")
        md.append("目标：%s" % r["goal"])
        md.append("")
        md.append("期望：`%s`｜实际：闭环=%s consent=%s end_reason=%s writeback=%d"
                  % (json.dumps(r["expect"], ensure_ascii=False),
                     r["run"]["closed"], r["run"]["consent"],
                     r["run"]["end_reason"], r["run"]["writeback"]))
        md.append("")
        md.append("| # | 用户 | 阶段 | 意图 | 客服回复 |")
        md.append("|---|---|---|---|---|")
        for i, t in enumerate(r["run"]["turns"], 1):
            md.append("| %d | %s | %s→%s | %s | %s |" % (
                i, t["user"], t["stage"], t["next"], t["intent"],
                t["reply"].replace("|", "／")))
        if r["suspects"]:
            md.append("")
            md.append("规则提示：" + "；".join(r["suspects"]))
    path = os.path.join(reports, "triage_personas_%s.md" % args.tag)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(md) + "\n")
    print("\n分诊报告：%s" % os.path.relpath(path, ROOT))

    if args.json:
        jp = args.json if os.path.isabs(args.json) else os.path.join(ROOT, args.json)
        os.makedirs(os.path.dirname(jp) or ".", exist_ok=True)
        with open(jp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"total": len(results), "fails": len(fails),
                       "items": results}, f, ensure_ascii=False, indent=1)
        print("分诊 JSON：%s" % os.path.relpath(jp, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
