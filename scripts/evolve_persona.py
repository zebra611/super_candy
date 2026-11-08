"""性格维度的自进化闭环（11-08）。

和 scripts/evolve.py 的分工：
    evolve.py          改**场景话术**（stages.*），全体客户共用一套说法
    evolve_persona.py  改**性格 → 回复形式**的映射，只对对应性格的客户生效

为什么要单独一条链：把两类改动混在一个版本库里，"这次到底改了什么"说不清；
更重要的是基线会互相污染——场景话术一变，性格维度的分数也跟着变，
闸门会把"话术升级的收益"算到性格规则头上。

对照实验怎么做（关键）：
    不是"关掉性格功能测基线"。关掉功能测出来的是假基线。
    而是给子进程注入一份**候选规则表**（环境变量 PERSONA_ADAPT_CONFIG），
    同一套场景、同一批性格画像、同一个客服，只换规则表跑两遍。
    基线是 rules={} 的恒等映射——功能一直开着，只是还什么都没调。

闸门在 evolve.py 的五道之外多一道：
    按性格分组无回退。平均值涨了、但"多疑型"从 1/2 掉到 0/2，
    说明优化器只讨好了一类客户，这种收益不许上线。

不可进化的部分（写了也拒绝）：
    识别词表（modules/persona.py 里的常量）、enabled 开关、意图与流程。
    让优化器能改识别规则，它会把客户全判成最容易得分的性格来刷分。

用法：
    python scripts/evolve_persona.py init
    python scripts/evolve_persona.py propose --auto
    python scripts/evolve_persona.py gate config/evolution/persona/candidates/c001.json
    python scripts/evolve_persona.py promote p2
    python scripts/evolve_persona.py status
    python scripts/evolve_persona.py doctor
    python scripts/evolve_persona.py rollback
"""

import copy
import json
import os
import subprocess
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from scripts import evolve as _evo            # 复用同一套指标口径与内容哈希
from modules import persona as _persona

PERSONA_DIR = os.path.join(_ROOT, "config", "evolution", "persona")
VERSIONS_PATH = os.path.join(PERSONA_DIR, "versions.json")
VER_DIR = os.path.join(PERSONA_DIR, "versions")
CAND_DIR = os.path.join(PERSONA_DIR, "candidates")
LIVE_RULES = os.path.join(_ROOT, "config", "persona_adapt.json")
PERSONAS = "eval/personas_temperament.json"
BASELINE = "eval/reports/sim_persona_baseline.json"

_read = _evo._read_json
_write = _evo._write_json
_sha = _evo._sha
_now = _evo._now
_metrics = _evo._metrics
_fmt = _evo._fmt

ALLOWED_ACTIONS = ("strip_lead", "replace", "suffix", "avoid_repeat")
MAX_TAIL = 60


def _versions():
    if not os.path.exists(VERSIONS_PATH):
        return {"active": "", "baseline_json": "", "history": []}
    return _read(VERSIONS_PATH)


def _snapshot(ver):
    return os.path.join(VER_DIR, ver, "rules.json")


def run_sim(rules_path, out_json, tag=""):
    """子进程跑性格集。只换规则表，其余全部固定。"""
    env = dict(os.environ)
    env[_persona.ENV_RULES] = rules_path
    cmd = [sys.executable, os.path.join(_ROOT, "scripts", "sim_eval.py"),
           "--personas", PERSONAS, "--json", out_json, "--tag", tag, "--quiet"]
    r = subprocess.run(cmd, cwd=_ROOT, env=env, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_json):
        raise SystemExit("性格跑批失败：\n%s\n%s"
                         % ((r.stdout or "")[-1200:], (r.stderr or "")[-1200:]))
    payload = _read(out_json)
    want = _sha(rules_path)
    if payload.get("rules_sha") != want:
        raise SystemExit("跑批没跑在目标规则表上：结果 sha=%s，候选 sha=%s"
                         % (payload.get("rules_sha"), want))
    return payload


def _groups(payload):
    return payload.get("by_temper") or {}


def validate_candidate(cand, base_rules):
    """静态校验：只准改 rules.<性格>.<阶段> 的形式动作。"""
    problems = []
    rules = cand.get("rules") or {}
    if not rules:
        problems.append("候选没有任何规则")
    try:
        import config_loader
        forbidden = config_loader.forbidden_words() or []
    except Exception:
        forbidden = []
    for code, stages in rules.items():
        if code not in _persona.LABELS or code == "neutral":
            problems.append("未知性格码 %r（识别词表不可由候选定义）" % code)
            continue
        if not isinstance(stages, dict):
            problems.append("%s 的规则不是对象" % code)
            continue
        for stage, rule in stages.items():
            if not isinstance(rule, dict):
                problems.append("%s/%s 不是动作对象" % (code, stage))
                continue
            for key, val in rule.items():
                if key not in ALLOWED_ACTIONS:
                    problems.append("%s/%s 含不允许的动作 %r" % (code, stage, key))
                if key in ("suffix", "avoid_repeat"):
                    items = val if isinstance(val, list) else [val]
                    for t in items:
                        if not isinstance(t, str) or not t.strip():
                            problems.append("%s/%s 的 %s 有空文案" % (code, stage, key))
                            continue
                        if len(t) > MAX_TAIL:
                            problems.append("%s/%s 引导语 %d 字，超过 %d" % (code, stage, len(t), MAX_TAIL))
                        if any(m in t for m in ("{", "}", "【", "】", "XX")):
                            problems.append("%s/%s 引导语含占位符" % (code, stage))
                        for w in forbidden:
                            if w and w in t:
                                problems.append("%s/%s 引导语含合规禁语「%s」" % (code, stage, w))
                if key == "replace":
                    for pair in (val if isinstance(val, list) else []):
                        if not (isinstance(pair, list) and len(pair) == 2):
                            problems.append("%s/%s 的 replace 必须是 [old, new]" % (code, stage))
                            continue
                        import re
                        if re.findall(r"\d+", str(pair[0])) != re.findall(r"\d+", str(pair[1])):
                            problems.append("%s/%s 的 replace 改掉了数字（事实）" % (code, stage))
    return problems


def merge_rules(base, patch):
    out = copy.deepcopy(base)
    for code, stages in (patch or {}).items():
        tgt = out.setdefault("rules", {}).setdefault(code, {})
        for stage, rule in stages.items():
            tgt.setdefault(stage, {}).update(rule)
    return out


# ---------------------------------------------------------------------------
def cmd_init(args):
    os.makedirs(VER_DIR, exist_ok=True)
    ver = "p1"
    os.makedirs(os.path.join(VER_DIR, ver), exist_ok=True)
    _write(_snapshot(ver), _read(LIVE_RULES))
    out = os.path.join(_ROOT, BASELINE)
    payload = run_sim(_snapshot(ver), out, tag="persona_base_" + ver)
    m = _metrics(payload)
    data = {
        "active": ver, "baseline_json": BASELINE,
        "scene_sha": payload.get("scene_sha", ""),
        "personas_path": PERSONAS,
        "history": [{
            "version": ver, "parent": None, "created": _now(), "status": "active",
            "source": "恒等规则表基线", "metrics": m,
            "by_temper": _groups(payload), "rules_sha": _sha(_snapshot(ver)),
        }],
    }
    _write(VERSIONS_PATH, data)
    print("已初始化性格版本库：active=%s" % ver)
    print("基线：%s" % _fmt(m))
    print("按性格：%s" % "  ".join("%s %d/%d" % (k, v["ok"], v["total"])
                                 for k, v in sorted(_groups(payload).items())))
    return 0


def cmd_doctor(args):
    vd = _versions()
    if not vd.get("history"):
        print("还没有版本库，先跑 init")
        return 1
    active = vd["active"]
    live = _sha(LIVE_RULES)
    snap = _sha(_snapshot(active)) if os.path.exists(_snapshot(active)) else ""
    bj = vd.get("baseline_json") or ""
    bp = os.path.join(_ROOT, bj) if bj else ""
    base = _read(bp).get("rules_sha", "") if bp and os.path.exists(bp) else ""
    print("active        : %s" % active)
    print("线上规则表    : sha=%s" % live)
    print("active 快照   : sha=%s" % (snap or "(缺失)"))
    print("基线结果      : %s  sha=%s" % (bj or "(未登记)", base or "(缺失)"))
    problems = []
    if live != snap:
        problems.append("线上规则表与 active 快照不一致")
    if not base or base != snap:
        problems.append("基线结果不是用 active 规则表跑出来的")

    # --deep：和 evolve.py 对称。规则表哈希只能证明规则没变，
    # 证明不了对话代码没变；直接重跑一遍 active 规则表比对分数。
    if args.get("deep") and not problems:
        rec = next((h for h in vd["history"] if h["version"] == active), {})
        rec_m = rec.get("metrics") or {}
        want = rec_m.get("all_ok")
        tmp = os.path.join(_ROOT, "eval", "reports", "_persona_doctor.json")
        got_m = _metrics(run_sim(_snapshot(active), tmp, tag="doctor_" + active))
        got = got_m["all_ok"]
        print("深度复算：通过 %s/12 → %s/12；系统缺陷会话 %s → %s；缺陷总数 %s → %s"
              % (want, got, rec_m.get("agent_defect_sessions"),
                 got_m["agent_defect_sessions"], rec_m.get("defect_total"),
                 got_m["defect_total"]))
        if want is not None and got != want:
            problems.append("基线分数已失真（%s → %s）：代码变过，先跑 rebase" % (want, got))
        for key, label in (("agent_defect_sessions", "系统缺陷会话"),
                           ("defect_total", "缺陷总数")):
            w, g = rec_m.get(key), got_m.get(key)
            if w is not None and w != g:
                problems.append("基线%s已失真（%s → %s）：通过数没变但闸门判据口径变了，先跑 rebase"
                                % (label, w, g))
    if problems:
        print("\n体检不通过：")
        for x in problems:
            print("  - %s" % x)
        print("修法：python scripts/evolve_persona.py init（重定基线）")
        return 1
    print("\n体检通过：三处一致，可以跑闸门。")
    return 0


def cmd_rebase(args):
    """用当前线上规则表重定基线，登记新版本（保留历史、可回滚）。

    规则表被手工改过、或对话代码变了导致分数漂移，都该走这里，
    而不是假装基线还成立。
    """
    vd = _versions()
    if not vd.get("history"):
        raise SystemExit("先跑 init")
    active = vd["active"]
    ver = "p%d" % (len(vd["history"]) + 1)
    os.makedirs(os.path.join(VER_DIR, ver), exist_ok=True)
    _write(_snapshot(ver), _read(LIVE_RULES))
    out = os.path.join(_ROOT, "eval", "reports", "sim_persona_base_%s.json" % ver)
    payload = run_sim(_snapshot(ver), out, tag="persona_base_" + ver)
    m = _metrics(payload)
    rec = {"version": ver, "parent": active, "created": _now(), "status": "active",
           "source": "rebase：线上规则表/对话代码变更，重定基线",
           "candidate": None, "metrics": m, "by_temper": _groups(payload),
           "rules_sha": _sha(_snapshot(ver))}
    for h in vd["history"]:
        if h["version"] == active and h.get("status") == "active":
            h["status"] = "superseded"
    vd["history"].append(rec)
    vd["active"] = ver
    vd["baseline_json"] = os.path.relpath(out, _ROOT).replace("\\", "/")
    _write(VERSIONS_PATH, vd)
    print("已重定性格基线：%s → %s｜%s" % (active, ver, _fmt(m)))
    print("按性格：%s" % "  ".join("%s %d/%d" % (k, v["ok"], v["total"])
                                 for k, v in sorted(_groups(payload).items())))
    return 0


def cmd_propose(args):
    vd = _versions()
    if not vd.get("history"):
        raise SystemExit("先跑 init")
    bj = os.path.join(_ROOT, vd.get("baseline_json") or BASELINE)
    if not os.path.exists(bj):
        raise SystemExit("找不到基线结果：%s" % vd.get("baseline_json"))
    payload = _read(bj)
    groups = _groups(payload)

    # 只挑**真的有重复或没闭环**的性格：给所有性格都塞规则等于没做针对性。
    weak = {k: v for k, v in groups.items() if v["ok"] < v["total"]}
    print("基线里没跑满的性格：%s" % (
        "、".join("%s %d/%d" % (k, v["ok"], v["total"]) for k, v in sorted(weak.items()))
        or "（无）"))

    # 内置模板：确定性、不调模型。文案只做「重复时换一种引导」，
    # 不改事实、不承诺结果（承诺是合规红线）。
    templates = {
        "skeptical": ["您也可以对照申请单编号核实一下。", "刚才这段是官方口径，您可以再核一遍。"],
        "forgetful": ["您慢慢想，我这边等着。", "说联系人姓名或者单号后六位也行。"],
        "impatient": ["我直接说重点。", "就这一句，您听完就行。"],
        "quiet": ["您回一个字也行。", "报四个数字就可以。"],
        "hard_of_hearing": ["我放慢一点再说。", "关键就是手机号后四位。"],
        "talkative": ["咱们先回到这笔申请。", "我先把要紧的说完。"],
        "cooperative": ["清楚了咱们就往下走。", "您看这样说明白了吗？"],
    }
    rules = {}
    for code in sorted(weak):
        opts = templates.get(code)
        if not opts:
            continue
        rules[code] = {"IDENTITY": {"avoid_repeat": opts},
                       "NEED": {"avoid_repeat": opts}}
    if not rules:
        print("没有可生成的针对性规则。")
        return 1

    os.makedirs(CAND_DIR, exist_ok=True)
    out = args.get("out") or os.path.join(CAND_DIR, "c001.json")
    if not os.path.isabs(out):
        out = os.path.join(_ROOT, out)
    cand = {
        "id": os.path.basename(out).replace(".json", ""),
        "created": _now(), "base_version": vd["active"],
        "from_baseline": vd.get("baseline_json"),
        "targets": sorted(weak),
        "rationale": "基线里这几类性格存在「同一句连念多遍 → 耐心耗尽」；只在重复轮追加轮换引导语，官方原句一字不动。",
        "rules": rules,
    }
    _write(out, cand)
    print("候选已写入：%s" % os.path.relpath(out, _ROOT))
    print("下一步：python scripts/evolve_persona.py gate %s" % os.path.relpath(out, _ROOT))
    return 0


def cmd_gate(args):
    cand_path = args.get("candidate")
    if not cand_path:
        raise SystemExit("用法：evolve_persona.py gate <候选文件>")
    cand_path = cand_path if os.path.isabs(cand_path) else os.path.join(_ROOT, cand_path)
    cand = _read(cand_path)
    vd = _versions()
    if not vd.get("history"):
        raise SystemExit("先跑 init")
    if cmd_doctor({}) != 0:
        raise SystemExit("体检不通过，拒绝闸门")

    active = vd["active"]
    base_cfg = _read(_snapshot(active))
    problems = validate_candidate(cand, base_cfg)
    if problems:
        print("候选不合规，未进闸门：")
        for p in problems:
            print("  - %s" % p)
        cand["gate"] = {"result": "rejected_static", "reasons": problems, "at": _now()}
        _write(cand_path, cand)
        return 1

    ver = "p%d" % (len(vd["history"]) + 1)
    os.makedirs(os.path.join(VER_DIR, ver), exist_ok=True)
    variant = merge_rules(base_cfg, cand.get("rules"))
    variant["version"] = ver
    _write(_snapshot(ver), variant)

    out_json = os.path.join(_ROOT, "eval", "reports", "sim_persona_%s.json" % ver)
    payload = run_sim(_snapshot(ver), out_json, tag=ver)
    m = _metrics(payload)
    g = _groups(payload)

    bj = os.path.join(_ROOT, vd.get("baseline_json") or BASELINE)
    base_payload = _read(bj)
    bm = _metrics(base_payload)
    bg = _groups(base_payload)
    base_rows = {r["id"]: r["ok"] for r in base_payload.get("rows") or []}
    rows = {r["id"]: r["ok"] for r in payload.get("rows") or []}
    regressions = [i for i, ok in base_rows.items() if ok and not rows.get(i, False)]

    print("基线（%s）：%s" % (active, _fmt(bm)))
    print("候选（%s）：%s" % (ver, _fmt(m)))

    checks = [
        ("train 不降", m["train_ok"] >= bm["train_ok"],
         "%d → %d" % (bm["train_ok"], m["train_ok"])),
        ("holdout 不降", m["holdout_ok"] >= bm["holdout_ok"],
         "%d → %d" % (bm["holdout_ok"], m["holdout_ok"])),
        ("有实际收益", m["all_ok"] > bm["all_ok"]
         or m["agent_defect_sessions"] < bm["agent_defect_sessions"],
         "总通过 %d→%d，系统缺陷会话 %d→%d"
         % (bm["all_ok"], m["all_ok"], bm["agent_defect_sessions"], m["agent_defect_sessions"])),
        ("无个体回退", not regressions, "、".join(regressions) or "无"),
        ("缺陷总数不增", m["defect_total"] <= bm["defect_total"],
         "%d → %d" % (bm["defect_total"], m["defect_total"])),
        # 第六道：平均值涨、某一类性格塌了，不算收益。
        ("按性格分组无回退",
         all(g.get(k, {}).get("ok", 0) >= v.get("ok", 0) for k, v in bg.items()),
         "  ".join("%s %d→%d" % (k, v.get("ok", 0), g.get(k, {}).get("ok", 0))
                   for k, v in sorted(bg.items()))),
    ]
    print()
    for name, ok, detail in checks:
        print("  [%s] %s：%s" % ("通过" if ok else "拒绝", name, detail))
    passed = all(ok for _, ok, _ in checks)

    rec = {
        "version": ver, "parent": active, "created": _now(),
        "status": "gated" if passed else "rejected",
        "source": os.path.relpath(cand_path, _ROOT).replace("\\", "/"),
        "candidate": cand.get("rules"), "metrics": m, "by_temper": g,
        "baseline_metrics": bm, "rules_sha": _sha(_snapshot(ver)),
        "gate": [{"check": n, "pass": bool(ok), "detail": d} for n, ok, d in checks],
    }
    vd["history"].append(rec)
    _write(VERSIONS_PATH, vd)
    cand["gate"] = {"result": "passed" if passed else "rejected", "version": ver,
                    "checks": rec["gate"], "at": _now()}
    _write(cand_path, cand)
    if passed:
        print("\n闸门通过，可上线：python scripts/evolve_persona.py promote %s" % ver)
        return 0
    print("\n闸门未通过，版本 %s 仅留档，不许上线。" % ver)
    return 1


def cmd_promote(args):
    ver = args.get("version")
    if not ver:
        raise SystemExit("用法：evolve_persona.py promote <版本号>")
    vd = _versions()
    rec = next((h for h in vd["history"] if h["version"] == ver), None)
    if not rec:
        raise SystemExit("没有这个版本：%s" % ver)
    if rec.get("status") not in ("gated", "active", "rolled_back"):
        raise SystemExit("版本 %s 状态是 %s，没过闸门不许上线" % (ver, rec.get("status")))
    if not os.path.exists(_snapshot(ver)):
        raise SystemExit("快照不存在：%s" % ver)
    backup = os.path.join(VER_DIR, ver, "live_before_promote.json")
    _write(backup, _read(LIVE_RULES))
    _write(LIVE_RULES, _read(_snapshot(ver)))
    prev = vd["active"]
    vd["active"] = ver
    rec["status"] = "active"
    rec["promoted_at"] = _now()
    for h in vd["history"]:
        if h["version"] == prev and h["status"] == "active":
            h["status"] = "superseded"
    _write(VERSIONS_PATH, vd)
    print("已上线：%s → %s（原 %s）" % (prev, ver, os.path.relpath(LIVE_RULES, _ROOT)))
    print("线上原件备份：%s" % os.path.relpath(backup, _ROOT))
    print("回滚：python scripts/evolve_persona.py rollback")
    return 0


def cmd_rollback(args):
    vd = _versions()
    active = vd.get("active")
    rec = next((h for h in vd["history"] if h["version"] == active), None)
    if not rec or not rec.get("parent"):
        raise SystemExit("当前是初始版本，没有可回滚的上一版")
    prev = rec["parent"]
    if not os.path.exists(_snapshot(prev)):
        raise SystemExit("上一版快照不存在：%s" % prev)
    _write(LIVE_RULES, _read(_snapshot(prev)))
    rec["status"] = "rolled_back"
    prev_rec = next((h for h in vd["history"] if h["version"] == prev), None)
    if prev_rec:
        prev_rec["status"] = "active"
    vd["active"] = prev
    _write(VERSIONS_PATH, vd)
    print("已回滚：%s → %s" % (active, prev))
    return 0


def cmd_status(args):
    vd = _versions()
    if not vd.get("history"):
        print("还没有版本库，先跑 init")
        return 0
    print("当前生效：%s｜画像集：%s" % (vd["active"], PERSONAS))
    print("%-5s %-12s %-20s %-8s %s" % ("版本", "状态", "指标", "父版本", "来源"))
    print("-" * 92)
    for h in vd["history"]:
        m = h.get("metrics") or {}
        print("%-5s %-12s %-20s %-8s %s" % (
            h["version"], h.get("status", ""),
            ("%d/%d｜缺陷 %d" % (m.get("all_ok", 0), m.get("all_total", 0),
                                 m.get("agent_defect_sessions", 0))),
            h.get("parent") or "-", h.get("source", "")))
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    args, positional = {}, []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("--"):
            key = a[2:].replace("-", "_")
            if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
                args[key] = rest[i + 1]
                i += 2
            else:
                args[key] = True
                i += 1
        else:
            positional.append(a)
            i += 1
    if cmd == "gate" and positional:
        args["candidate"] = positional[0]
    if cmd == "promote" and positional:
        args["version"] = positional[0]
    if cmd == "propose" and positional and "out" not in args:
        args["out"] = positional[0]
    table = {"init": cmd_init, "doctor": cmd_doctor, "rebase": cmd_rebase,
             "propose": cmd_propose, "gate": cmd_gate, "promote": cmd_promote,
             "rollback": cmd_rollback, "status": cmd_status}
    fn = table.get(cmd)
    if not fn:
        print("未知命令：%s（可用：%s）" % (cmd, "/".join(sorted(table))))
        return 2
    return fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
