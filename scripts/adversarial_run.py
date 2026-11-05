"""对抗测试跑批器：把对抗集反复跑 N 轮，看结论稳不稳、薄弱点在哪。

用法：
    python3 scripts/adversarial_run.py               # 默认 3 轮
    python3 scripts/adversarial_run.py --rounds 5
    python3 scripts/adversarial_run.py --cases eval/adversarial_cases.json

为什么要有它（而不是直接用 run_eval）：
    1. 对抗集和基线集要分开：混在一起，一次改动到底伤了哪边说不清；
    2. 单轮结果不算数：规则版对同一句话的判定本该稳定，跑 3 轮结论不一致，
       说明有随机性在里头（缓存、时间、外部调用），比"通过率低"更严重；
    3. 报告要能贴进答辩材料，必须自带口径、样本量与复现命令。

它复用什么：run_eval.run_case() 与 run_eval.judge()。
判定语义一行都不另写——两套判定就是两个数，评审一问就露馅。

它不碰什么：不写 data/events.db（run_eval 启动时已断言临时库）、
不改任何人的文件、不改 seed_cases.json。
"""

import collections
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_eval as R                      # noqa: E402  复用跑批与临时库隔离

CASES_PATH = os.path.join(R._ROOT, "eval", "adversarial_cases.json")


def _git_rev():
    """记录被测代码的 commit。没有它，报告里的数字三个月后无法复现。"""
    try:
        import subprocess
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=R._ROOT, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"
REPORT_DIR = os.path.join(R._ROOT, "eval", "reports")


def load_cases(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_once(cases):
    """跑一轮，返回逐条结论（含末环节，供聚类用）。"""
    rows = []
    for case in cases:
        last, evs = R.run_case(case)
        ok, problems, closed, consent = R.judge(case, last, evs)
        replies = [e for e in evs if e["type"] == "agent_reply"]
        stage = (replies[-1]["payload"].get("stage") if replies else "") or "-"
        rows.append({
            "id": case["id"],
            "tag": case.get("tag", "-"),
            "ok": ok,
            "closed": closed,
            "consent": consent,
            "problems": problems,
            "stage": stage,
        })
    return rows


def cluster(rows):
    """按「末环节 + 问题首句」聚簇。只做统计，不下结论。"""
    buckets = collections.Counter()
    for r in rows:
        if r["ok"]:
            continue
        key = (r["stage"], r["problems"][0].split("：")[0] if r["problems"] else "未分类")
        buckets[key] += 1
    return buckets


def render(cases, rounds, per_round):
    n = len(cases)
    total_runs = n * len(per_round)

    # 逐条：每轮结论与稳定性
    table = ["| 用例 | 方向 | " + " | ".join("第%d轮" % (i + 1) for i in range(len(per_round)))
             + " | 通过率 | 结论稳定 |",
             "|---|---|" + "---|" * len(per_round) + "---|---|"]
    stable_cnt = 0
    per_case = []
    for idx, case in enumerate(cases):
        verdicts = [per_round[i][idx]["ok"] for i in range(len(per_round))]
        stable = len(set(verdicts)) == 1
        stable_cnt += 1 if stable else 0
        passed = sum(1 for v in verdicts if v)
        per_case.append((case["id"], passed, stable, per_round[0][idx]["problems"]))
        table.append("| %s | %s | %s | %d/%d | %s |"
                     % (case["id"], case.get("tag", "-"),
                        " | ".join("✅" if v else "❌" for v in verdicts),
                        passed, len(verdicts), "是" if stable else "**否**"))

    round_rates = [sum(1 for r in rows if r["ok"]) for rows in per_round]
    avg = sum(round_rates) / float(len(per_round))

    # 失败聚类：跨轮合并
    all_fail = []
    for rows in per_round:
        all_fail.extend([r for r in rows if not r["ok"]])
    clusters = cluster(all_fail)

    out = []
    out.append("# 对抗测试报告（自动生成）\n")
    out.append("- 生成时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    out.append("- 被测代码：commit `%s`" % _git_rev())
    out.append("- 对抗集：`eval/adversarial_cases.json`，%d 条" % n)
    out.append("- 轮次：%d 轮，共 %d 次用例执行" % (len(per_round), total_runs))
    out.append("- 判定：复用 `scripts/run_eval.py::judge()`（语义冻结，未另写）")
    out.append("- 事件库：临时库（`run_eval` 启动时断言，不写 `data/events.db`）\n")
    out.append("## 一、总体\n")
    out.append("| 指标 | 数值 |")
    out.append("|---|---|")
    out.append("| 每轮通过 | %s |" % " ｜ ".join(
        "%d/%d" % (r, n) for r in round_rates))
    out.append("| 平均通过率 | %.1f%%（%d 次执行） |" % (avg * 100.0 / n, total_runs))
    out.append("| **结论稳定率** | **%d/%d = %.0f%%**（同一用例每轮结论一致） |"
               % (stable_cnt, n, stable_cnt * 100.0 / n))
    failed_ids = {r["id"] for rows in per_round for r in rows if not r["ok"]}
    out.append("| 未通过用例数（去重） | %d |" % len(failed_ids))
    out.append("")
    out.append("## 二、逐条结果\n")
    out.extend(table)
    out.append("")
    out.append("## 三、失败聚类（跨轮合并，共 %d 次失败）\n" % len(all_fail))
    if clusters:
        out.append("| 末环节 | 问题类型 | 次数 |")
        out.append("|---|---|---|")
        for (stage, kind), cnt in clusters.most_common():
            out.append("| %s | %s | %d |" % (stage, kind, cnt))
    else:
        out.append("无失败。")
    out.append("")
    out.append("## 四、结论\n")
    unstable = [c[0] for c in per_case if not c[2]]
    if unstable:
        out.append("1. **存在不稳定用例**：%s。同一输入结论不一致，先查随机性来源"
                   "（外部接口、缓存、时间依赖），再谈通过率。" % "、".join(unstable))
    else:
        out.append("1. 结论稳定：%d 条用例在 %d 轮里判定完全一致，说明规则版对同一输入是确定性的。"
                   % (n, len(per_round)))
    weakest = sorted(per_case, key=lambda x: x[1])[:3]
    out.append("2. 最薄弱三条：%s。" % "；".join(
        "%s（%d/%d 通过）" % (c[0], c[1], len(per_round)) for c in weakest))
    out.append("3. 局限：本测试是**文字驱动**的，噪声类用例只验证了「听不清时的文本」"
               "而不是真实声学噪声；真实噪声需赛场三类音源 + 真麦克风复测。")
    out.append("")
    out.append("## 五、复现\n")
    out.append("```bash\npython3 scripts/adversarial_run.py --rounds %d\n```" % len(per_round))
    return "\n".join(out), {"avg": avg, "stable": stable_cnt, "n": n,
                            "unstable": unstable, "rates": round_rates}


def main():
    argv = sys.argv[1:]
    rounds = 3
    cases_path = CASES_PATH
    if "--rounds" in argv:
        rounds = int(argv[argv.index("--rounds") + 1])
    if "--cases" in argv:
        cases_path = argv[argv.index("--cases") + 1]

    cases = load_cases(cases_path)
    if not cases:
        print("对抗集为空：%s" % cases_path)
        return 1

    print("对抗测试：%d 条用例 × %d 轮" % (len(cases), rounds))
    per_round = []
    for i in range(rounds):
        rows = run_once(cases)
        per_round.append(rows)
        ok = sum(1 for r in rows if r["ok"])
        print("  第 %d 轮：通过 %d/%d" % (i + 1, ok, len(rows)))

    text, stat = render(cases, rounds, per_round)

    os.makedirs(REPORT_DIR, exist_ok=True)
    name = "adversarial_%s_r%d.md" % (datetime.datetime.now().strftime("%Y%m%d_%H%M"), rounds)
    path = os.path.join(REPORT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")

    print("\n平均通过率 %.1f%%｜结论稳定率 %d/%d"
          % (stat["avg"] * 100.0 / stat["n"], stat["stable"], stat["n"]))
    if stat["unstable"]:
        print("不稳定用例：%s" % "、".join(stat["unstable"]))
    print("报告已写入：%s" % os.path.relpath(path, R._ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
