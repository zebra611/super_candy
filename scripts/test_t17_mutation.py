# -*- coding: utf-8 -*-
"""T17 真实通话对抗用例的变异验证（防假绿）。

起因：第一版 5 条用例只看「最终有没有闭环」，把反问式承接和 3 位尾号
引导关掉之后，系统仍能在后面几轮把信息补回来闭环，5 条居然全绿。
那种绿是假的。现在的用例用 must_say/must_not 钉住**中间轮**行为，
本脚本反过来证明：防护一旦失效，用例必须报红。

三个变异：
    M1 反问式承接失效       → T17-R1 应红（没说明来意）
    M2 3 位尾号引导失效     → T17-R2 应红（没请用户补全四位）
    M3 反问词表放宽到真提问 → T17-R5 应红（把真提问当火气）

跑法：python scripts/test_t17_mutation.py
"""
from __future__ import annotations

import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import run_eval as R              # noqa: E402
from modules import dialogue as d  # noqa: E402

CASES = json.load(open(os.path.join(ROOT, "eval", "adversarial_t17_realcall.json"),
                       encoding="utf-8"))


def run_all():
    out = {}
    for c in CASES:
        sid = R.start_call()["session_id"]
        last = None
        for t in c["turns"]:
            last = R.handle_turn(sid, user_text=t)
        evs = R.events.list_events(sid)
        ok, probs, _, _ = R.judge(c, last, evs)
        out[c["id"]] = (ok, probs)
        R.end_call(sid)
    return out


def main():
    base = run_all()
    base_green = all(v[0] for v in base.values())
    print("基线：%d/%d 通过%s" % (sum(1 for v in base.values() if v[0]),
                                 len(base), "" if base_green else "  ← 基线就不绿，先修用例"))
    for cid, (ok, probs) in base.items():
        if not ok:
            print("  FAIL %s %s" % (cid, probs))
    if not base_green:
        return 1

    checks = []

    # M1 反问式承接失效
    orig = d._is_rhetorical_displeasure
    d._is_rhetorical_displeasure = lambda t: False
    try:
        r1 = run_all()
    finally:
        d._is_rhetorical_displeasure = orig
    checks.append(("M1 反问式承接失效 → T17-R1 应红", not r1["T17-R1"][0],
                   r1["T17-R1"][1]))

    # M2 3 位尾号引导失效
    orig2 = d._short_digit_run
    d._short_digit_run = lambda t: None
    try:
        r2 = run_all()
    finally:
        d._short_digit_run = orig2
    checks.append(("M2 3 位尾号引导失效 → T17-R2 应红", not r2["T17-R2"][0],
                   r2["T17-R2"][1]))

    # M3 词表放宽（把「这个按钮干什么用的」也当成火气）
    orig3 = d._RHETORICAL_EXCLUDE
    d._RHETORICAL_EXCLUDE = ()
    try:
        r3 = run_all()
    finally:
        d._RHETORICAL_EXCLUDE = orig3
    checks.append(("M3 反问词表放宽 → T17-R5 应红", not r3["T17-R5"][0],
                   r3["T17-R5"][1]))

    print("=" * 70)
    for label, ok, probs in checks:
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  实际：%s" % probs))
    npass = sum(1 for _, ok, _ in checks if ok)
    print("T17 变异验证：%d/%d 通过（全绿之后必须能变红，才不是假绿）"
          % (npass, len(checks)))
    return 0 if npass == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
