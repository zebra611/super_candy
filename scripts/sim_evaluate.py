#!/usr/bin/env python3
"""对抗对局评估：把对局记录压成 6 个指标 + 缺口清单。

指标设计原则（每条都能用事件或文本机械判定，不接受"表现良好"这种话）：
    T1 应闭环达成率  场景要求闭环的局里，真的产生 writeback 的比例
    T2 误闭环率      场景不该闭环却写了工单的比例（应恒为 0）
    T3 合规清洁率    没有红线违规、没有执行异常的局占比
    T4 共情承接率    客户出现负面情绪后，下一句客服是否安抚（关键词表判定）
    T5 需求捕获率    身份、诉求两项槽位都拿到的局占比
    T6 效率          平均轮次，越短越好（目标 ≤8）

综合分 = 合规闸门（T3<1 直接 0）× 加权和。合规是门槛不是分数，这条沿用赛题口径。
"""

import argparse
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_eval as R                      # noqa: E402  只为了拿 _ROOT

SIM_DIR = os.path.join(R._ROOT, "eval", "sim")
RUNS_DIR = os.path.join(SIM_DIR, "runs")
REPORT_DIR = os.path.join(R._ROOT, "eval", "reports")

# 负面情绪词：客户说出这些，表示这轮需要被承接，而不是继续念流程
NEGATIVE_CUES = ("骗子", "投诉", "烦", "气", "投诉你们", "报警", "敷衍", "不管",
                 "什么态度", "说几遍", "听不懂", "别打了", "坑", "烂")
# 安抚词：客服下一句里出现任意一个，即视为完成一次情绪承接
EMPATHY_CUES = ("理解", "抱歉", "对不起", "不好受", "别急", "我明白", "确实",
                "给您添麻烦", "换我我也", "先别急", "您说得对")


def latest_run():
    """取最新一份对局记录。

    按修改时间而不是文件名：同名加 -2/-3 后缀时，字符串排序会把
    `x-2.json` 排在 `x.json` 前面，取到的反而是旧数据。
    """
    files = glob.glob(os.path.join(RUNS_DIR, "sim_*.json"))
    if not files:
        raise SystemExit("还没有对局记录，先跑 python3 scripts/sim_dialogue.py")
    return max(files, key=os.path.getmtime)


def load_run(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def empathy_stats(records):
    """共情承接率：负面轮次里，客服下一句含安抚词的比例。"""
    total = hit = 0
    misses = []
    for rec in records:
        tr = rec.get("transcript") or []
        for i, item in enumerate(tr):
            if item["role"] != "user":
                continue
            if not any(c in (item["text"] or "") for c in NEGATIVE_CUES):
                continue
            total += 1
            nxt = next((x for x in tr[i + 1:] if x["role"] == "agent"), None)
            if nxt and any(c in (nxt["text"] or "") for c in EMPATHY_CUES):
                hit += 1
            else:
                misses.append({"persona": rec["persona"], "scenario": rec["scenario"],
                               "turn": item["turn"], "customer": item["text"][:40]})
    return {"total": total, "hit": hit,
            "rate": (hit / float(total)) if total else None,
            "misses": misses[:20]}


def evaluate(records, scenario_map):
    total = len(records)
    should = [r for r in records if scenario_map[r["scenario"]]["expect_closed"]]
    should_not = [r for r in records if not scenario_map[r["scenario"]]["expect_closed"]]

    should_ok = sum(1 for r in should if r["closed"])
    # 机会闭环率：只有核验通过的局才谈得上闭环。
    # 客户拒绝报尾号（P03/P07/P08）导致无法闭环，是客户选择，不是系统失败；
    # 把它算进分母会把"合规地停手"误判成"能力不足"。
    chance = [r for r in records if r["identity_confirmed"]]
    chance_ok = sum(1 for r in chance if r["closed"])
    # 脏闭环：没核验身份却写了工单——这是真正要防的（工单里全是空信息）
    dirty_close = [r for r in records if r["closed"] and not r["identity_confirmed"]]
    wrongly = len(dirty_close)
    dirty = [r for r in records if r["red_line_failed"] or r["error"]]
    captured = sum(1 for r in records if r["identity_confirmed"] and r["need_captured"])
    empathy = empathy_stats(records)
    avg_turns = round(sum(r["turns"] for r in records) / float(total), 2) if total else 0

    t1 = (should_ok / float(len(should))) if should else None
    t1b = (chance_ok / float(len(chance))) if chance else None
    t2 = (wrongly / float(total)) if total else 0.0
    t3 = 1.0 - len(dirty) / float(total) if total else 0.0
    t4 = empathy["rate"]
    t5 = captured / float(total) if total else 0.0
    t6 = max(0.0, 1.0 - min(1.0, avg_turns / 10.0))

    composite = 0.0
    if t3 == 1.0:
        composite = (0.35 * (t1b or 0) + 0.20 * (t4 or 0) + 0.20 * t5
                     + 0.15 * t6 + 0.10 * (1.0 - t2))

    by_persona = {}
    for r in records:
        item = by_persona.setdefault(r["persona"], {"name": r["persona_name"],
                                                    "archetype": r["archetype"],
                                                    "n": 0, "closed": 0, "bad": 0})
        item["n"] += 1
        item["closed"] += 1 if r["closed"] else 0
        item["bad"] += 1 if (r["red_line_failed"] or r["error"]) else 0

    by_scenario = {}
    for r in records:
        item = by_scenario.setdefault(r["scenario"], {"name": r["scenario_name"],
                                                      "n": 0, "closed": 0})
        item["n"] += 1
        item["closed"] += 1 if r["closed"] else 0

    return {
        "total": total,
        "t1_should_close": {"should": len(should), "ok": should_ok, "rate": t1},
        "t1b_chance_close": {"chance": len(chance), "ok": chance_ok, "rate": t1b},
        "t2_wrong_close": {"should_not": len(should_not), "wrong": wrongly, "rate": t2,
                           "cases": [{"persona": r["persona"], "scenario": r["scenario"]}
                                     for r in dirty_close][:10]},
        "t3_compliance": {"dirty": len(dirty), "rate": t3,
                          "cases": [{"persona": r["persona"], "scenario": r["scenario"],
                                     "failed": r["red_line_failed"], "error": r["error"]}
                                    for r in dirty][:20]},
        "t4_empathy": empathy,
        "t5_slot": {"captured": captured, "rate": t5},
        "t6_turns": {"avg": avg_turns, "score": t6},
        "composite": round(composite, 4),
        "by_persona": by_persona,
        "by_scenario": by_scenario,
        "failures": [{"persona": r["persona"], "scenario": r["scenario"],
                      "turns": r["turns"], "closed": r["closed"],
                      "red_line": r["red_line_failed"], "error": r["error"]}
                     for r in records if not r["closed"] and scenario_map[r["scenario"]]["expect_closed"]][:30],
    }


def render(stats, meta):
    def pct(x):
        return "-" if x is None else "%.0f%%" % (x * 100)

    out = ["# 文字对抗测试报告", ""]
    out.append("- 时间：%s｜对局：%d 局｜数据：`%s`" % (meta["generated_at"], stats["total"], meta["source"]))
    out.append("- 角色：8 类客户｜场景：6 类业务｜每局最多 10 轮｜客户台词确定性生成（同输入同台词）")
    out.append("")
    out += ["## 一、总指标", "", "| 指标 | 数值 | 口径 |", "|---|---|---|"]
    out.append("| T1 应闭环达成率 | %s（%d/%d） | 场景要求闭环的局里真的产生 writeback |"
               % (pct(stats["t1_should_close"]["rate"]), stats["t1_should_close"]["ok"],
                  stats["t1_should_close"]["should"]))
    out.append("| T1b 机会闭环率（主） | %s（%d/%d） | 核验通过的局里闭环的比例 |"
               % (pct(stats["t1b_chance_close"]["rate"]), stats["t1b_chance_close"]["ok"],
                  stats["t1b_chance_close"]["chance"]))
    out.append("| T2 脏闭环率 | %s（%d 局） | 未核验身份却写了工单，应为 0 |"
               % (pct(stats["t2_wrong_close"]["rate"]), stats["t2_wrong_close"]["wrong"]))
    out.append("| T3 合规清洁率 | %s（脏 %d 局） | 无红线违规、无执行异常 |"
               % (pct(stats["t3_compliance"]["rate"]), stats["t3_compliance"]["dirty"]))
    out.append("| T4 共情承接率 | %s（%d/%d） | 客户负面表达后，下一句客服含安抚语 |"
               % (pct(stats["t4_empathy"]["rate"]), stats["t4_empathy"]["hit"],
                  stats["t4_empathy"]["total"]))
    out.append("| T5 需求捕获率 | %s（%d 局） | 身份 + 诉求两项槽位都拿到 |"
               % (pct(stats["t5_slot"]["rate"]), stats["t5_slot"]["captured"]))
    out.append("| T6 平均轮次 | %.1f 轮（得分 %.2f） | 越短越好，目标 ≤8 |"
               % (stats["t6_turns"]["avg"], stats["t6_turns"]["score"]))
    out.append("| **综合分** | **%.3f** | 合规闸门 × 加权和；T3<100%% 直接 0 |" % stats["composite"])
    out.append("")

    out += ["## 二、按角色", "", "| 角色 | 类型 | 对局 | 闭环 | 脏局 |", "|---|---|---|---|---|"]
    for pid, p in sorted(stats["by_persona"].items()):
        out.append("| %s %s | %s | %d | %d | %d |" % (pid, p["name"], p["archetype"],
                                                      p["n"], p["closed"], p["bad"]))
    out.append("")
    out += ["## 三、按场景", "", "| 场景 | 名称 | 对局 | 闭环 |", "|---|---|---|---|"]
    for sid, s in sorted(stats["by_scenario"].items()):
        out.append("| %s | %s | %d | %d |" % (sid, s["name"], s["n"], s["closed"]))
    out.append("")

    out += ["## 四、应闭环却没闭环（缺口清单）", ""]
    if stats["failures"]:
        out += ["| 角色 | 场景 | 轮次 | 红线 | 异常 |", "|---|---|---|---|---|"]
        for f in stats["failures"]:
            out.append("| %s | %s | %d | %s | %s |"
                       % (f["persona"], f["scenario"], f["turns"],
                          ",".join(f["red_line"]) or "-", (f["error"] or "-")[:40]))
    else:
        out.append("无。")
    out.append("")

    out += ["## 五、共情缺口（客户发火后没被接住）", ""]
    if stats["t4_empathy"]["misses"]:
        out += ["| 角色 | 场景 | 轮 | 客户原话 |", "|---|---|---|---|"]
        for m in stats["t4_empathy"]["misses"]:
            out.append("| %s | %s | %d | %s |" % (m["persona"], m["scenario"], m["turn"], m["customer"]))
    else:
        out.append("无（所有负面表达都得到了承接）。")
    out.append("")
    out += ["## 六、说明与局限", "",
            "1. 客户台词是**规则生成**，不是大模型即兴发挥：可复现、零成本，但不会说出规则外的句子；",
            "2. 本系统是**诊断工具**，不自动修改客服代码。要真正提升通话表现，需 2A 按缺口清单改 `dialogue.py`；",
            "3. 本系统属工具型智能客服，不适用《人工智能拟人化互动服务管理暂行办法》，但须遵守合成语音标识、个人信息保护与消保条例（详见 README）。"]
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="对抗对局评估")
    parser.add_argument("--run", default=None, help="对局记录路径，默认取最新")
    parser.add_argument("--out", default=REPORT_DIR)
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args(argv)

    path = args.run or latest_run()
    payload = load_run(path)
    with open(os.path.join(SIM_DIR, "scenarios.json"), "r", encoding="utf-8") as f:
        scenario_map = {s["id"]: s for s in json.load(f)["scenarios"]}

    stats = evaluate(payload["records"], scenario_map)
    meta = {"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": os.path.relpath(path, R._ROOT)}
    text = render(stats, meta)

    if not args.no_report:
        os.makedirs(args.out, exist_ok=True)
        stem = os.path.join(args.out, "sim_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M"))
        md, js = stem + ".md", stem + ".json"
        n = 2
        while os.path.exists(md):
            md, js = "%s-%d.md" % (stem, n), "%s-%d.json" % (stem, n)
            n += 1
        with open(md, "w", encoding="utf-8") as f:
            f.write(text)
        with open(js, "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "stats": stats}, f, ensure_ascii=False, indent=2)
        print("报告：%s" % os.path.relpath(md, R._ROOT))

    print("综合分 %.3f｜机会闭环 %d/%d｜脏闭环 %d｜合规脏局 %d｜共情 %s｜平均 %.1f 轮"
          % (stats["composite"], stats["t1b_chance_close"]["ok"], stats["t1b_chance_close"]["chance"],
             stats["t2_wrong_close"]["wrong"], stats["t3_compliance"]["dirty"],
             ("%.0f%%" % (stats["t4_empathy"]["rate"] * 100)) if stats["t4_empathy"]["rate"] is not None else "-",
             stats["t6_turns"]["avg"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
