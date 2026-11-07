# -*- coding: utf-8 -*-
"""Q5：评测双场景——售后主场景与 pop_reject 赛题场景各出一份报告。

跑法：
    python scripts/run_eval_two_scenes.py
    python scripts/run_eval_two_scenes.py --date-tag 1106

为什么要两个场景分开跑：
    赛题演示要报赛题场景的数字。售后 88 条与 pop 340 条混在一个分母里，
    评委问「你这 100% 是哪个场景的」就答不清；而且两个场景的核身对象
    根本不同（售后按客户尾号/姓名，赛题按申请单/公司名），混跑没有意义。

为什么用子进程而不是一次跑两个场景：
    `run_eval` 的 `--scene` 必须在 `import app` 之前生效（app 有启动期缓存），
    同一进程里切第二次会拿到第一次的缓存。两个子进程各自干净。

报告落在 `eval/reports/`，文件名带场景与日期，不覆盖历史。
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL = os.path.join(ROOT, "eval")


def _merge_c4_cases():
    """把 9 个 C4 批次合并成一份临时用例文件（不落仓库）。"""
    files = sorted(glob.glob(os.path.join(EVAL, "adversarial_c4_batch*.json")))
    cases = []
    for p in files:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        cases.extend(data["cases"] if isinstance(data, dict) else data)
    if not cases:
        return None, 0
    fd, path = tempfile.mkstemp(prefix="c4_all_", suffix=".json")
    os.close(fd)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cases, f, ensure_ascii=False, indent=1)
    return path, len(cases)


def _run(label, scene, cases, report):
    cmd = [sys.executable, os.path.join(ROOT, "scripts", "run_eval.py"),
           "--cases", cases, "--report", report, "--strict"]
    if scene:
        cmd += ["--scene", scene]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("APP_SCENE", None)          # 不让上一个场景污染
    env.pop("EVAL_SCENE", None)
    print("\n" + "=" * 74)
    print("跑场景：%s（%s）" % (label, scene or "默认"))
    print("=" * 74)
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          timeout=1800, env=env)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    tail = [l for l in out if "通过率" in l or "闭环率" in l]
    for l in tail:
        print("  " + l)
    if not tail:
        print("  [未拿到汇总行] 末行：%s" % (out[-1][:120] if out else "无输出"))
    return {"label": label, "scene": scene or "main", "report": report,
            "exit": proc.returncode, "summary": tail,
            "tail": out[-3:]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-tag", default=datetime.now().strftime("%m%d"))
    args = ap.parse_args(argv)

    c4_path, c4_n = _merge_c4_cases()
    if c4_path is None:
        print("没有找到 eval/adversarial_c4_batch*.json，无法跑 pop 场景")
        return 2

    reports = os.path.join(ROOT, "docs", "eval_reports")
    os.makedirs(reports, exist_ok=True)
    runs = [
        # 显式写场景，不靠"默认值"：默认已是赛题 POP 资审，
        # 这里要跑的是售后回访场景那 88 条种子用例。
        _run("售后主场景", "service_outbound",
             os.path.join(EVAL, "seed_cases.json"),
             os.path.join(reports, "eval_main_%s.md" % args.date_tag)),
        _run("pop_reject 赛题", "pop_reject", c4_path,
             os.path.join(reports, "eval_pop_reject_%s.md" % args.date_tag)),
    ]
    try:
        os.remove(c4_path)
    except OSError:
        pass

    print("\n" + "=" * 74)
    print("双场景对比（%s）" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("=" * 74)
    for r in runs:
        print("  %-16s %-12s exit=%-2d %s"
              % (r["label"], r["scene"], r["exit"],
                 "｜".join(r["summary"]) or "无汇总"))
        print("     报告：%s" % os.path.relpath(r["report"], ROOT))
    bad = [r for r in runs if r["exit"] != 0]
    print()
    print("结论：%s" % ("两个场景都通过" if not bad
                       else "%d 个场景有失败用例（--strict 已返回非 0）" % len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
