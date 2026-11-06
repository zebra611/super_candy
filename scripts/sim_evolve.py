#!/usr/bin/env python3
"""对抗课程自我进化：让测试自己变难，直到把系统打穿为止。

进化的对象是**测试课程**，不是客服代码：
    第 1 代  全矩阵（8 角色 × 6 场景）
    第 2 代  只保留第 1 代没打过的角色，铺满全部场景（扩大打击面）
    第 3 代  在第 2 代基础上把轮次上限加大（消耗战：看会不会越聊越崩）

为什么不"自动改客服话术"：改 dialogue.py 属 2A 的 R2A 区域，脚本自动改代码
既越界又不可审查。本脚本产出的是**缺口清单**——哪个角色、哪个场景、第几轮、
卡在哪个环节，交给 2A 照着改，改完重跑同一套课程即可验证。

用法：
    python3 scripts/sim_evolve.py                # 默认 3 代
    python3 scripts/sim_evolve.py --gens 5
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_eval as R                       # noqa: E402
import sim_dialogue as D                   # noqa: E402
import sim_evaluate as E                   # noqa: E402

REPORT_DIR = os.path.join(R._ROOT, "eval", "reports")


def evaluate_records(records):
    with open(D.SCENARIOS_PATH, "r", encoding="utf-8") as f:
        scenario_map = {s["id"]: s for s in json.load(f)["scenarios"]}
    return E.evaluate(records, scenario_map)


def next_generation(stats, records, personas, scenarios, gen_no):
    """根据上一代结果决定这一代打谁。规则简单、可解释，不做玄学调参。"""
    if gen_no == 2:
        # 只打没打过的角色，但铺满全部场景：验证是不是"换个业务就不会了"
        failed = {r["persona"] for r in records if not r["closed"] and r["identity_confirmed"]}
        if not failed:
            failed = {k for k, v in stats["by_persona"].items() if v["closed"] == 0}
        picked = [p for p in personas if p["id"] in failed] or personas
        return [(p, s) for p in picked for s in scenarios], 10

    # 第 3 代起：对最弱的三个角色打消耗战，轮次上限 +4
    ranked = sorted(stats["by_persona"].items(), key=lambda kv: (kv[1]["closed"], kv[1]["bad"]))
    weak = {k for k, _ in ranked[:3]}
    picked = [p for p in personas if p["id"] in weak] or personas
    return [(p, s) for p in picked for s in scenarios], 14


def main(argv=None):
    parser = argparse.ArgumentParser(description="对抗课程自我进化")
    parser.add_argument("--gens", type=int, default=3)
    parser.add_argument("--out", default=REPORT_DIR)
    args = parser.parse_args(argv)
    if args.gens < 1:
        print("--gens 至少为 1", file=sys.stderr)
        return 2

    personas = D.load_json(D.PERSONAS_PATH)["personas"]
    scenarios = D.load_json(D.SCENARIOS_PATH)["scenarios"]

    pairs = [(p, s) for p in personas for s in scenarios]
    max_turns = 10
    history, all_records = [], []

    for gen in range(1, args.gens + 1):
        records = [D.run_one(p, s, max_turns=max_turns) for p, s in pairs]
        all_records.extend(records)
        stats = evaluate_records(records)
        history.append({
            "gen": gen,
            "pairs": len(pairs),
            "max_turns": max_turns,
            "composite": stats["composite"],
            "chance_close": stats["t1b_chance_close"]["rate"],
            "chance_n": stats["t1b_chance_close"]["chance"],
            "empathy": stats["t4_empathy"]["rate"],
            "dirty": stats["t2_wrong_close"]["wrong"],
            "avg_turns": stats["t6_turns"]["avg"],
            "personas": sorted({p["id"] for p, _ in pairs}),
        })
        print("第 %d 代：%2d 局｜上限 %d 轮｜机会闭环 %s｜共情 %s｜综合 %.3f"
              % (gen, len(pairs), max_turns,
                 "%.0f%%" % (stats["t1b_chance_close"]["rate"] * 100)
                 if stats["t1b_chance_close"]["rate"] is not None else "-",
                 "%.0f%%" % (stats["t4_empathy"]["rate"] * 100)
                 if stats["t4_empathy"]["rate"] is not None else "-",
                 stats["composite"]))

        if gen == args.gens:
            break
        pairs, max_turns = next_generation(stats, records, personas, scenarios, gen + 1)

    # 始终打不过的角色：跨代统计，只算"核验通过却仍没闭环"的局
    stuck = {}
    for rec in all_records:
        if rec["identity_confirmed"] and not rec["closed"]:
            stuck[rec["persona"]] = stuck.get(rec["persona"], 0) + 1

    lines = ["# 对抗课程进化报告", ""]
    lines.append("- 时间：%s｜代数：%d｜累计对局：%d" %
                 (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  args.gens, len(all_records)))
    lines.append("- 进化对象：**测试课程**（难度与覆盖面），不是客服代码")
    lines.append("")
    lines += ["| 代 | 对局 | 上限轮次 | 机会闭环率 | 共情率 | 脏闭环 | 平均轮次 | 综合分 |",
              "|---|---|---|---|---|---|---|---|"]
    for h in history:
        lines.append("| %d | %d | %d | %s | %s | %d | %.1f | %.3f |"
                     % (h["gen"], h["pairs"], h["max_turns"],
                        "-" if h["chance_close"] is None else "%.0f%%" % (h["chance_close"] * 100),
                        "-" if h["empathy"] is None else "%.0f%%" % (h["empathy"] * 100),
                        h["dirty"], h["avg_turns"], h["composite"]))
    lines += ["", "## 始终打不过的角色（核验通过了仍没闭环）", ""]
    if stuck:
        lines += ["| 角色 | 失败次数 | 诊断 |", "|---|---|---|"]
        for pid, n in sorted(stuck.items(), key=lambda kv: -kv[1]):
            name = next((p["name"] for p in personas if p["id"] == pid), pid)
            lines.append("| %s %s | %d | 已核验但流程没走到回写，检查该角色的施压点是否让状态机卡在 OFFER/CONFIRM |"
                         % (pid, name, n))
    else:
        lines.append("无——所有核验通过的对局都完成了回写。")
    lines += ["", "## 结论怎么用", "",
              "1. 这份报告只说明**系统在哪些角色上打不过**，不证明改完就会好；",
              "2. 把上表交给 2A，按「角色 + 场景 + 环节」改 `modules/dialogue.py`；",
              "3. 改完重跑同一命令，看这张表里的失败次数是否下降——下降才算真提升。"]
    text = "\n".join(lines) + "\n"

    os.makedirs(args.out, exist_ok=True)
    stem = os.path.join(args.out, "sim_evolve_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M"))
    md, js = stem + ".md", stem + ".json"
    with open(md, "w", encoding="utf-8") as f:
        f.write(text)
    with open(js, "w", encoding="utf-8") as f:
        json.dump({"history": history, "stuck": stuck}, f, ensure_ascii=False, indent=2)
    print("报告：%s" % os.path.relpath(md, R._ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
