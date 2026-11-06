# -*- coding: utf-8 -*-
"""P4：查询类工具掉线时的编排回归 + 变异验证。

用例在 eval/fault_cases/p4_tool_error.json，本脚本负责**注入故障**：
把 tools.query_customer / query_application 替换成 adapters/base.py 的真实
错误形状 {"error": code, "found": False}，再跑一遍完整对话链路。

为什么不放进普通对抗跑批：正常跑批没有故障注入能力，而 P4 事故只在
上游掉线时发生——不注入就永远测不到，直跑会看到 0/3 的假红。
所以用例文件放在 eval/fault_cases/（不在 adversarial*.json 命名空间内），
由本脚本独占驱动；详见 eval/fault_cases/README.md。

验收：
    基线 3/3 通过；把 _lookup_failed 关掉（恢复只看 found 的旧行为）后
    必须报红，否则说明用例是假绿。

跑法：python scripts/test_p4_timeout.py
"""
from __future__ import annotations

import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import run_eval as R               # noqa: E402
from modules import dialogue as d  # noqa: E402
from modules import tools          # noqa: E402

CASES = json.load(open(os.path.join(ROOT, "eval", "fault_cases", "p4_tool_error.json"),
                       encoding="utf-8"))
ERR_BY_ID = {"P4-01": "TIMEOUT", "P4-02": "TIMEOUT", "P4-03": "UPSTREAM"}

_orig_customer = tools.query_customer
_orig_application = tools.query_application


def _inject(code):
    def fail(*_a, **_k):
        return {"error": code, "found": False, "message": "injected fault", "_ms": 0.0}
    tools.query_customer = fail
    tools.query_application = fail


def _restore():
    tools.query_customer = _orig_customer
    tools.query_application = _orig_application


def run_all():
    out = {}
    for c in CASES:
        _inject(ERR_BY_ID[c["id"]])
        sid = R.start_call()["session_id"]
        last = None
        try:
            for t in c["turns"]:
                last = R.handle_turn(sid, user_text=t)
            evs = R.events.list_events(sid)
            ok, probs, _, _ = R.judge(c, last, evs)
            out[c["id"]] = (ok, probs, last.get("end_reason") or "")
        finally:
            R.end_call(sid)
            _restore()
    return out


def main():
    base = run_all()
    green = sum(1 for v in base.values() if v[0])
    print("P4 故障注入基线：%d/%d 通过" % (green, len(base)))
    for cid, (ok, probs, end) in base.items():
        print("  [%s] %-7s end_reason=%-12s %s" %
              ("PASS" if ok else "FAIL", cid, end or "-", "" if ok else probs))
    if green != len(base):
        return 1

    # 变异：恢复「只看 found」的旧行为，用例必须报红
    orig = d._lookup_failed
    d._lookup_failed = lambda s: None
    try:
        red = run_all()
    finally:
        d._lookup_failed = orig
    nred = sum(1 for v in red.values() if not v[0])
    print("=" * 70)
    print("  [%s] 变异：_lookup_failed 失效 → %d/%d 条报红" %
          ("PASS" if nred >= 2 else "FAIL", nred, len(red)))
    print("P4 变异验证：%s" % ("PASS" if nred >= 2 else "FAIL"))
    return 0 if nred >= 2 else 1


if __name__ == "__main__":
    sys.exit(main())
