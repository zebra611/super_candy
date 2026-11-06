# -*- coding: utf-8 -*-
"""C4 变异验证：故意让系统说违禁承诺，Q5 用例必须报红（防「全绿是假绿」）。"""
import io, json, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.environ["EVAL_SCENE"] = "pop_reject"
import run_eval as R
import config_loader

cases = json.load(open(os.path.join(ROOT, "eval", "adversarial_c4_batch01.json"),
                       encoding="utf-8"))

# 基线：不改任何东西，先确认这 40 条本来就全绿
base_fail = []
for c in cases:
    last, evs = R.run_case(c)
    ok, problems, _, _ = R.judge(c, last, evs)
    if not ok:
        base_fail.append((c["id"], problems))
print("基线失败：%d/40（应为 0）" % len(base_fail))

# 变异：同时让两层合规失效才有区分度——
#   ① 拦截话术改成明确承诺；② 输出侧 compliance_guard 不再过滤
#   （只改 ① 时 app.py 的护栏会把整句替换掉，agent_reply 里根本看不到违禁词，
#    那是「护栏生效」而不是「用例假绿」）。
cfg = config_loader.load()
orig = cfg["compliance"]["reply_blocked"]
cfg["compliance"]["reply_blocked"] = "没问题，一定通过，保证通过，包过。"
from modules import dialogue
orig_guard = dialogue.compliance_guard
dialogue.compliance_guard = lambda text: {
    "text": text, "blocked": False, "hits": [], "replaced": False}
red = []
for c in cases:
    last, evs = R.run_case(c)
    ok, problems, _, _ = R.judge(c, last, evs)
    if not ok:
        red.append((c["id"], (c.get("noise") or {}).get("question_type"), problems[0]))
cfg["compliance"]["reply_blocked"] = orig
dialogue.compliance_guard = orig_guard

print("变异后失败：%d/40" % len(red))
for r in red[:6]:
    print("   ", r)
q5 = sum(1 for r in red if r[1] == "结果预期")
print("其中结果预期类（Q5）报红：%d 条" % q5)
verdict = (not base_fail) and q5 >= 6
print("变异验证：%s" % ("PASS —— 用例有区分度，全绿不是假绿" if verdict
                       else "FAIL —— 用例可能假绿，需加断言"))
sys.exit(0 if verdict else 1)
