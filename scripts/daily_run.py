#!/usr/bin/env python3
"""每日全量评测：跑一次套件，落每日快照与历史曲线。

分工（为什么单独一个文件）：
    eval_suite.py 负责"算出什么"（口径、判定、报告）；
    daily_run.py  负责"什么时候跑、跑到哪、失败怎么办"（调度与归档）。
    两者混在一起，换个调度方式（launchd / CI / 手动）就要改计算代码。

行为：
    1. 调 eval_suite.collect() 跑一遍基线 + 对抗；
    2. 落 eval/runs/YYYY-MM-DD.json，同名加 -2/-3，不覆盖；
    3. 追加一行到 eval/runs/history.csv（表头自动写），便于画趋势；
    4. 任何一步失败写 eval/runs/error.log（含堆栈），返回 1，绝不静默成功。

用法：
    python3 scripts/daily_run.py
    python3 scripts/daily_run.py --rounds 5

定时（二选一，不要两个都装）：
    A. macOS launchd：见 docs/25_评测工具链使用手册.md 第 5 节
    B. 手动：每天收工前跑一次；或写进 .githooks/pre-push
"""

import argparse
import datetime
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eval_suite as ES                  # noqa: E402

ROOT = ES.R._ROOT
RUNS_DIR = os.path.join(ROOT, "eval", "runs")
CSV_PATH = os.path.join(RUNS_DIR, "history.csv")
ERROR_LOG = os.path.join(RUNS_DIR, "error.log")

CSV_FIELDS = ("date", "time", "commit", "cases", "passed", "should", "should_ok",
              "wrongly", "writeback_ok", "writeback_calls",
              "adv_avg", "adv_stable", "adv_total", "duration_sec")


def next_path(day):
    """同一天重复跑不覆盖：2026-11-06.json → -2.json → -3.json。

    为什么保留：一天跑两次通常是因为中途改了东西，第一份恰好是"改前"的证据。
    """
    path = os.path.join(RUNS_DIR, day + ".json")
    n = 2
    while os.path.exists(path):
        path = os.path.join(RUNS_DIR, "%s-%d.json" % (day, n))
        n += 1
    return path, n - 1


def append_csv(row):
    os.makedirs(RUNS_DIR, exist_ok=True)
    fresh = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", encoding="utf-8") as f:
        if fresh:
            f.write(",".join(CSV_FIELDS) + "\n")
        f.write(",".join(str(row[k]) for k in CSV_FIELDS) + "\n")


def write_error(step, exc):
    os.makedirs(RUNS_DIR, exist_ok=True)
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write("[%s] 失败于：%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), step))
        f.write("%s: %s\n" % (type(exc).__name__, exc))
        f.write(traceback.format_exc())
        f.write("-" * 60 + "\n")


def summarize(payload):
    b = payload["baseline"]
    a = payload["adversarial"]
    m = payload["meta"]
    return {
        "date": m["generated_at"][:10],
        "time": m["generated_at"][11:],
        "commit": m["commit"],
        "cases": b["total"],
        "passed": b["passed"],
        "should": b["closure"]["should"],
        "should_ok": b["closure"]["should_ok"],
        "wrongly": b["closure"]["wrongly"],
        "writeback_ok": b["writeback"]["ok_calls"],
        "writeback_calls": b["writeback"]["calls"],
        "adv_avg": round(a["avg_rate"], 4),
        "adv_stable": a["stable"],
        "adv_total": a["total"],
        "duration_sec": m["duration_sec"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="每日全量评测并归档")
    parser.add_argument("--rounds", type=int, default=3, help="对抗轮次，默认 3")
    args = parser.parse_args(argv)
    if args.rounds < 1:
        print("--rounds 至少为 1", file=sys.stderr)
        return 2

    step = "收集数据"
    try:
        payload = ES.collect(args.rounds)
        row = summarize(payload)

        step = "落盘"
        os.makedirs(RUNS_DIR, exist_ok=True)
        path, nth = next_path(row["date"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        append_csv(row)
    except Exception as exc:                # noqa: BLE001 —— 兜底要宽，否则失败会被吞
        write_error(step, exc)
        print("失败（步骤：%s），详见 %s" % (step, os.path.relpath(ERROR_LOG, ROOT)),
              file=sys.stderr)
        return 1

    print("已归档：%s（第 %d 次）" % (os.path.relpath(path, ROOT), nth))
    print("基线 %d/%d｜应闭环 %d/%d｜回写 %d/%d｜对抗 %.0f%%｜稳定 %d/%d｜耗时 %ss"
          % (row["passed"], row["cases"], row["should_ok"], row["should"],
             row["writeback_ok"], row["writeback_calls"],
             row["adv_avg"] * 100, row["adv_stable"], row["adv_total"],
             row["duration_sec"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
