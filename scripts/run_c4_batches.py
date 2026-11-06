# -*- coding: utf-8 -*-
"""C4 跑批驱动：逐批跑 9 个 adversarial_c4_batchNN.json，汇总通过率报告。

- 固定 EVAL_SCENE=pop_reject，保证跑的是赛题链路（申请单核身/官方话术/合规红线）；
- 每批 1 轮（用例是确定性规则链路，多轮只测稳定性，不改变通过率结论）；
- 报告只做统计与聚类，**不改用例、不改判定规则、不改系统实现**；
- 报告写工作区（生成物不进仓库），结构化报告仍落在 eval/reports/c4_1106/。

跑法：python scripts/run_c4_batches.py [--report <绝对路径>]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATCH_GLOB = os.path.join(ROOT, "eval", "adversarial_c4_batch*.json")
RUNNER = os.path.join(ROOT, "scripts", "adversarial_run.py")
OUT_DIR = os.path.join(ROOT, "eval", "reports", "c4_1106")
DEFAULT_REPORT = os.path.join(os.path.dirname(ROOT), "C4_通过率报告_1106.md")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=DEFAULT_REPORT)
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    env = dict(os.environ)
    env["EVAL_SCENE"] = "pop_reject"
    env["PYTHONIOENCODING"] = "utf-8"

    batches = sorted(glob.glob(BATCH_GLOB))
    if not batches:
        print("没有找到 C4 批次文件，先跑 scripts/gen_c4_cases.py", file=sys.stderr)
        return 2

    summary, failures, clusters = [], [], collections.Counter()
    for path in batches:
        name = os.path.basename(path)
        proc = subprocess.run(
            [sys.executable, RUNNER, "--cases", path, "--rounds", "1",
             "--out", OUT_DIR, "--quiet"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
        stdout = (proc.stdout or "").strip()
        m = re.search(r"结构化：(.+\.json)", stdout)
        if not m:
            print("批次 %s 没产出报告：%s %s" % (name, stdout[-200:],
                                                (proc.stderr or "")[-200:]), file=sys.stderr)
            return 3
        jpath = os.path.join(ROOT, m.group(1).strip().replace("/", os.sep))
        payload = json.load(open(jpath, encoding="utf-8"))
        cases = json.load(open(path, encoding="utf-8"))
        by_id = {c["id"]: c for c in cases}
        passed = round(payload["avg_rate"] * len(cases))
        summary.append((name, len(cases), passed, payload["avg_rate"] * 100))
        for row in (payload.get("raw") or [[]])[0]:
            if row.get("ok"):
                continue
            case = by_id.get(row["id"], {})
            qtype = (case.get("noise") or {}).get("question_type", "?")
            reason = (row.get("problems") or ["（无原因，执行异常）"])[0]
            failures.append((row["id"], qtype, reason))
            clusters[(qtype, reason.split("：")[0])] += 1
        print("%s: %d/%d = %.1f%%" % (name, passed, len(cases),
                                     payload["avg_rate"] * 100))

    total = sum(s[1] for s in summary)
    passed = sum(s[2] for s in summary)
    lines = [
        "# C4 评测扩容通过率报告（2026-11-06）",
        "",
        "- 用例矩阵：官方 68 个驳回场景 × 5 类追问 = **%d 条**，分 9 批（每批 ≤40）" % total,
        "- 跑法：`EVAL_SCENE=pop_reject python scripts/run_c4_batches.py`；"
        "赛题场景、nlg/tts 强制模板与浏览器合成（确定性，不联网）",
        "- 总体通过：**%d/%d = %.1f%%**" % (passed, total, passed * 100.0 / total),
        "- **未通过 %d 条**，全部如实列出；未为提分改任何用例、判定规则或系统实现" % len(failures),
        "",
        "## 分批明细",
        "",
        "| 批次 | 条数 | 通过 | 通过率 |",
        "|---|---|---|---|",
    ]
    lines += ["| %s | %d | %d | %.1f%% |" % s for s in summary]
    lines += ["", "## 失败聚类（追问类型 × 原因）", ""]
    if clusters:
        lines.append("| 追问类型 | 原因 | 条数 |")
        lines.append("|---|---|---|")
        lines += ["| %s | %s | %d |" % (k[0], k[1], v)
                  for k, v in clusters.most_common()]
    else:
        lines.append("（无）")
    lines += ["", "## 未通过用例清单", ""]
    lines += ["- `%s`（%s）：%s" % f for f in failures] or ["（无）"]
    lines += ["", "## 口径说明", "",
              "- 通过率按 `adversarial_run.py` 的判定语义：must_not 合规红线、"
              "writeback 事件闭环、consent=True 三项全过才算通过；",
              "- 结果预期类追问刻意含「能保证这次一定通过吗」，守的是不许承诺审核结果；",
              "- 失败项已按追问类型聚类，作为后续整改输入（本轮不改动规则）。"]

    body = "\n".join(lines) + "\n"
    report_path = os.path.abspath(args.report)
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "wb") as f:
        f.write(body.replace("\r\n", "\n").encode("utf-8"))
    print("=" * 60)
    print("总体 %d/%d = %.1f%%，失败 %d 条，报告：%s"
          % (passed, total, passed * 100.0 / total, len(failures), report_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
