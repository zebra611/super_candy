"""对抗集闭环：跑批 → 失败聚类 → 候选 → 双跑闸门（不自动上线）。

用法：
    python scripts/adversarial_evolve.py run
    python scripts/adversarial_evolve.py run --scene <场景.json> --json <报告.json> --quiet
    python scripts/adversarial_evolve.py propose --out config/evolution/candidates/adv001.json
    python scripts/adversarial_evolve.py gate config/evolution/candidates/adv001.json
    python scripts/adversarial_evolve.py check

与 evolve.py 的分工：evolve.py 管「10 通 L2 基线画像」的话术版本库；
本脚本管「自适应对抗画像集」，两者画像、报告、基线都分开，绝不互相改分母。
候选仍用 evolve.py 的 patches 格式，但闸门在这里跑：同一批对抗客户，
只换场景配置跑两遍，按 train / holdout / 个体 / 难度分组四道判据判收益。

子进程隔离是必须的：app 启动期会缓存场景，同进程连跑两份会拿到旧缓存，
出现「候选明明改了、分数没动」的假结论（evolve.py 踩过）。
"""

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_SCENE_ARG = ""
_args = sys.argv[1:]
for _i, _a in enumerate(_args):
    if _a == "--scene" and _i + 1 < len(_args):
        _SCENE_ARG = _args[_i + 1]
if _SCENE_ARG.endswith(".json"):
    os.environ["APP_CONFIG"] = (_SCENE_ARG if os.path.isabs(_SCENE_ARG)
                                else os.path.join(_ROOT, _SCENE_ARG))
    os.environ.pop("APP_SCENE", None)

import events  # noqa: E402
_tmp = tempfile.mkdtemp(prefix="sc_adversarial_")
events.DATA_DIR = _tmp
events.DB_PATH = os.path.join(_tmp, "sim.db")
events.init_db()
import atexit  # noqa: E402
atexit.register(lambda: shutil.rmtree(_tmp, ignore_errors=True))

import config_loader  # noqa: E402
from modules import tools  # noqa: E402
from modules import attribution as attr  # noqa: E402
from modules.cluster import cluster_defects  # noqa: E402
from modules.adversary import AdversarySim, WEAKNESS  # noqa: E402
from modules.user_sim import HANGUP  # noqa: E402

config_loader.tts_config = lambda: {"provider": "browser",
                                    "voice": "zh-CN-XiaoxiaoNeural", "rate": 1.05}
config_loader.nlg_config = lambda: {**config_loader.load().get("nlg", {}),
                                    "provider": "template"}

from app import end_call, handle_turn, start_call  # noqa: E402

PERSONAS_PATH = os.path.join(_ROOT, "eval", "personas_adversarial.json")
BASELINE = os.path.join(_ROOT, "eval", "reports", "adversarial_baseline.json")
CAND_DIR = os.path.join(_ROOT, "config", "evolution", "candidates")
ADV_DIR = os.path.join(_ROOT, "config", "evolution", "adversarial")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _scene_sha(path):
    with open(path, encoding="utf-8") as f:
        return __import__("hashlib").sha256(
            json.dumps(json.load(f), sort_keys=True, ensure_ascii=False)
            .encode("utf-8")).hexdigest()[:16]


def load_personas():
    return _load(PERSONAS_PATH).get("personas") or []


def run_one(persona, max_turns=14):
    tools._reset_for_test()
    started = start_call()
    sid = started["session_id"]
    company = started.get("company") or {}
    sim = AdversarySim(persona, company, seed=persona["id"])
    turns, agent_text, end_by = [], started.get("opening") or "", ""

    for _ in range(max_turns):
        if not agent_text:
            end_by = "agent_silent"
            break
        user_out = sim.say(agent_text)
        if user_out is None:
            end_by = "user_hangup"
            break
        res = handle_turn(sid, user_text=user_out["text"])
        turns.append({"user": user_out, "agent": {
            "reply_text": res.get("reply_text", ""),
            "stage": res.get("stage", ""), "next_stage": res.get("next_stage", ""),
            "intent": res.get("intent", ""), "end_reason": res.get("end_reason", ""),
        }})
        if user_out["action"] == HANGUP:
            end_by = "user_hangup"
            break
        if res.get("end_reason"):
            end_by = "agent_end:%s" % res["end_reason"]
            break
        if res.get("closed") or res.get("next_stage") in ("WRITEBACK", "DONE"):
            end_by = "agent_closed"
            break
        agent_text = res.get("reply_text", "")
    else:
        end_by = "max_turns"

    evs = events.list_events(sid)
    closed = any(e["type"] == "writeback" for e in evs)
    end_call(sid)
    return {
        "id": persona["id"], "label": persona.get("label", ""),
        "goal": persona.get("goal", ""), "split": persona.get("split", "train"),
        "company": {k: company.get(k) for k in
                    ("company_name", "contact_role", "phone_tail", "application_id")},
        "blocked_by_missing_fact": list(sim.blocked_by_missing_fact),
        "turns": turns, "closed": closed, "end_by": end_by,
        "final_stage": (turns[-1]["agent"]["next_stage"] if turns else ""),
        "level_peak": max(sim.levels) if sim.levels else 1,
        "weakness": dict(sim.hits),
    }


def by_level(rows, sessions):
    """按峰值难度分组：平均值涨了但 L4 全塌，不算收益。"""
    peaks = {s["id"]: s.get("level_peak", 1) for s in sessions}
    out = {}
    for r in rows:
        k = "L%d" % peaks.get(r["id"], 1)
        g = out.setdefault(k, {"total": 0, "ok": 0, "ids": []})
        g["total"] += 1
        g["ok"] += 1 if r["ok"] else 0
        g["ids"].append(r["id"])
    return out


def build_report(personas, quiet=False, tag="adversarial"):
    sessions = [run_one(p) for p in personas]
    summary = attr.summarise(sessions, personas)
    clusters = cluster_defects(sessions, personas)
    weakness = {k: 0 for k in WEAKNESS}
    for s in sessions:
        for k, v in (s.get("weakness") or {}).items():
            weakness[k] = weakness.get(k, 0) + v
    payload = {
        "tag": tag, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scene": config_loader.scene_name(), "scene_path": config_loader.scene_path(),
        "scene_sha": _scene_sha(config_loader.scene_path()),
        "summary": {k: v for k, v in summary.items() if k != "rows"},
        "rows": summary["rows"], "clusters": clusters, "sessions": sessions,
        "personas": [{"id": p["id"], "split": p.get("split", "train"),
                      "goal": p.get("goal"), "expect": p.get("expect"),
                      "max_level": p.get("max_level", 4)} for p in personas],
        "personas_path": os.path.relpath(PERSONAS_PATH, _ROOT).replace("\\", "/"),
        "by_level": by_level(summary["rows"], sessions),
        "weakness_totals": weakness,
    }
    if not quiet:
        print("对抗跑批：通过 %d/%d｜train %d/%d｜holdout %d/%d｜系统缺陷会话 %d"
              % (summary["all"]["ok"], summary["all"]["total"],
                 summary["train"]["ok"], summary["train"]["total"],
                 summary["holdout"]["ok"], summary["holdout"]["total"],
                 summary["agent_defect_sessions"]))
        print("按难度：%s" % "  ".join("%s %d/%d" % (k, v["ok"], v["total"])
                                     for k, v in sorted(payload["by_level"].items())))
        print("客服弱点命中：%s" % json.dumps(weakness, ensure_ascii=False))
        for c in clusters:
            print("  [%d 通] %s｜目标 %s｜%s"
                  % (c["sessions"], c["key"], c["target"] or "-",
                     c["needs_code"] or "可改话术"))
    return payload


def cmd_run(a):
    personas = load_personas()
    if a.ids:
        personas = [p for p in personas if p["id"] in a.ids.split(",")]
    payload = build_report(personas, quiet=a.quiet, tag=a.tag)
    out = a.json or BASELINE
    if not os.path.isabs(out):
        out = os.path.join(_ROOT, out)
    _save(out, payload)
    if not a.quiet:
        print("报告：%s" % os.path.relpath(out, _ROOT))
    return 0


def cmd_propose(a):
    base = _load(BASELINE)
    clusters = base.get("clusters") or []
    optional = [c for c in clusters if not c.get("needs_code") and c.get("target")]
    code_only = [c for c in clusters if c.get("needs_code")]
    patches, seen = [], {}
    for c in optional:
        key = c["target"]
        if key in seen:
            seen[key]["clusters"].append(c["key"])
            continue
        cfg = _load(os.path.join(_ROOT, "config", "scenes", "pop_reject.json"))
        node = cfg
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        item = {"path": key, "old": node, "value": "", "cluster": c["key"],
                "clusters": [c["key"]], "rationale": ""}
        seen[key] = item
        patches.append(item)
    out = a.out or os.path.join(CAND_DIR, "adv001.json")
    if not os.path.isabs(out):
        out = os.path.join(_ROOT, out)
    # 归因器没标出、但期望确实没达成的会话：单独列出来，绝不进优化池。
    # 拿这种失败去调话术，等于对着没诊断的病开药（11-08 A03/A10 实测：
    # 客户还在问「你哪来的我号码」，客服却回写 unknown 收线，现有缺陷码没覆盖）。
    unexplained = []
    by_sid = {s["id"]: s for s in base.get("sessions") or []}
    for r in base.get("rows") or []:
        if r["ok"] or r.get("defects"):
            continue
        s = by_sid.get(r["id"]) or {}
        last = (s.get("turns") or [{}])[-1]
        unexplained.append({
            "id": r["id"], "fault": r.get("fault"),
            "expect": r.get("expect"), "closed": r.get("closed"),
            "evidence": ((last.get("agent") or {}).get("reply_text") or "")[:80],
        })
    cand = {"id": os.path.basename(out).replace(".json", ""),
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "from_baseline": os.path.relpath(BASELINE, _ROOT).replace("\\", "/"),
            "targets": [c["key"] for c in optional], "patches": patches,
            "code_only": [{"key": c["key"], "reason": c["needs_code"]}
                          for c in code_only],
            "unexplained": unexplained}
    _save(out, cand)
    print("可优化簇 %d 个，只能改代码的簇 %d 个，待归因失败 %d 通；候选骨架：%s"
          % (len(optional), len(code_only), len(unexplained),
             os.path.relpath(out, _ROOT)))
    for c in code_only:
        print("  [只改代码] %s —— %s" % (c["key"], c["needs_code"]))
    for u in unexplained:
        print("  [待归因] %s（%s）：客服末句「%s」" % (u["id"], u["fault"], u["evidence"]))
    return 0


def _apply(cand):
    from scripts import evolve as evo
    base = _load(os.path.join(_ROOT, "config", "scenes", "pop_reject.json"))
    return evo.apply_candidate(base, cand)


def judge(base, got):
    """五道判据的纯函数：同一批对抗客户，只换场景配置，逐项比对。"""
    bm, gm = base["summary"], got["summary"]
    base_rows = {x["id"]: x["ok"] for x in base["rows"]}
    got_rows = {x["id"]: x["ok"] for x in got["rows"]}
    regressions = [i for i, ok in base_rows.items() if ok and not got_rows.get(i)]
    bg, gg = base.get("by_level") or {}, got.get("by_level") or {}
    return [
        ("train 不降", gm["train"]["ok"] >= bm["train"]["ok"],
         "%d → %d" % (bm["train"]["ok"], gm["train"]["ok"])),
        ("holdout 不降", gm["holdout"]["ok"] >= bm["holdout"]["ok"],
         "%d → %d" % (bm["holdout"]["ok"], gm["holdout"]["ok"])),
        ("有实际收益", gm["all"]["ok"] > bm["all"]["ok"]
         or gm["agent_defect_sessions"] < bm["agent_defect_sessions"],
         "通过 %d→%d，系统缺陷会话 %d→%d"
         % (bm["all"]["ok"], gm["all"]["ok"],
            bm["agent_defect_sessions"], gm["agent_defect_sessions"])),
        ("无个体回退", not regressions, "、".join(regressions) or "无"),
        ("按难度分组无回退",
         all(gg.get(k, {}).get("ok", 0) >= v.get("ok", 0) for k, v in bg.items()),
         "  ".join("%s %d→%d" % (k, v["ok"], gg.get(k, {}).get("ok", 0))
                   for k, v in sorted(bg.items()))),
    ]


def cmd_gate(a):
    cand_path = a.candidate if os.path.isabs(a.candidate) \
        else os.path.join(_ROOT, a.candidate)
    cand = _load(cand_path)
    if not os.path.exists(BASELINE):
        print("先跑 run 生成对抗基线")
        return 1
    if not cand.get("patches") or not all(str(p.get("value") or "").strip()
                                          for p in cand["patches"]):
        print("候选的 patches[].value 还没填，拒绝闸门")
        return 1
    from scripts import evolve as evo
    static = evo.validate_candidate(
        cand, _load(os.path.join(_ROOT, "config", "scenes", "pop_reject.json")))
    if static:
        print("候选不合规，未进闸门：")
        for p in static:
            print("  - %s" % p)
        return 1
    os.makedirs(ADV_DIR, exist_ok=True)
    variant = os.path.join(ADV_DIR, "%s.scene.json" % cand.get("id", "cand"))
    _save(variant, _apply(cand))
    out = os.path.join(_ROOT, "eval", "reports",
                       "adversarial_%s.json" % cand.get("id", "cand"))
    r = subprocess.run([sys.executable, os.path.join(_ROOT, "scripts",
                        "adversarial_evolve.py"), "run", "--scene", variant,
                        "--json", out, "--quiet", "--tag", "gate"],
                       cwd=_ROOT, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        print("候选跑批失败：\n%s" % (r.stderr or r.stdout)[-1200:])
        return 1
    base, got = _load(BASELINE), _load(out)
    checks = judge(base, got)
    for name, ok, detail in checks:
        print("  [%s] %s：%s" % ("通过" if ok else "拒绝", name, detail))
    passed = all(ok for _, ok, _ in checks)
    result = {"result": "passed" if passed else "rejected",
              "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "baseline": os.path.relpath(BASELINE, _ROOT).replace("\\", "/"),
              "candidate": os.path.relpath(cand_path, _ROOT).replace("\\", "/"),
              "checks": [{"check": n, "pass": bool(ok), "detail": d}
                         for n, ok, d in checks]}
    _save(os.path.join(_ROOT, "eval", "reports",
                       "adversarial_gate_%s.json" % cand.get("id", "cand")), result)
    cand["adversarial_gate"] = result
    _save(cand_path, cand)
    print("\n%s：候选%s。脚本不会自动上线。"
          % ("闸门通过" if passed else "闸门未通过",
             "可人工评估上架" if passed else "只留档"))
    return 0 if passed else 1


def cmd_check(a):
    """反向自测：确定性、不念占位符、不编事实、难度随弱点上升。"""
    from modules.adversary import AdversarySim as AS, _TEMPLATES
    p = {"id": "CHK", "max_level": 4, "traits": {"patience": 20},
         "knows": {"phone_tail": True}, "style": ["哎呀"]}
    company = {"phone_tail": "0086", "contact_name": "晏青梧",
               "company_name": "闻樾", "application_id": "123456"}

    def play(replies):
        s = AS(p, company, seed="CHK")
        return [s.say(t) for t in replies], s

    seq = ["请问您是闻樾的负责人吗？"] + ["好的，核对申请单需要您预留手机号的后四位，说四个数字就行。"] * 6
    o1, s1 = play(seq)
    o2, s2 = play(seq)
    ok_det = json.dumps(o1, ensure_ascii=False) == json.dumps(o2, ensure_ascii=False)
    ok_no_placeholder = all("{" not in (x or {}).get("text", "")
                            and "}" not in (x or {}).get("text", "")
                            and "XX" not in (x or {}).get("text", "")
                            for x in o1 if x)
    ok_escalate = s1.level >= 3 and s1.hits["repeat"] >= 2
    _, calm = play(["请问您是闻樾的负责人吗？",
                    "您的号码是商家入驻时预留的联系方式，官方审核只用来核对这笔资质申请。",
                    "闻樾品牌因授权链不完整没有通过审核，需要补充品牌授权书后重新提交。"])
    ok_calm = calm.level <= 2
    p2 = dict(p, knows={"company_name": True})
    s3 = AS(p2, company, seed="CHK2")
    s3.level = 1
    text, action = s3._render("answer_tail")
    ok_fact = "{" not in text and "phone_tail" in s3.blocked_by_missing_fact

    # 闸门第六道反向自测：总通过涨了、但 L4 从 1/2 塌到 0/2，必须被拦。
    def _sum(ok, train, holdout, defects):
        return {"all": {"ok": ok}, "train": {"ok": train},
                "holdout": {"ok": holdout}, "agent_defect_sessions": defects}
    fake_base = {"summary": _sum(6, 4, 2, 2), "rows": [{"id": "X", "ok": True}],
                 "by_level": {"L2": {"ok": 1, "total": 2},
                              "L4": {"ok": 1, "total": 2}}}
    fake_got = {"summary": _sum(7, 4, 3, 2), "rows": [{"id": "X", "ok": True}],
                "by_level": {"L2": {"ok": 2, "total": 2},
                             "L4": {"ok": 0, "total": 2}}}
    jc = {n: ok for n, ok, _ in judge(fake_base, fake_got)}
    ok_group = bool(jc["有实际收益"]) and not jc["按难度分组无回退"]

    from scripts import evolve as evo
    cfg = _load(os.path.join(_ROOT, "config", "scenes", "pop_reject.json"))
    bad = {"patches": [{"path": "stages.IDENTITY.reply_ask_tail",
                        "value": "您好，{机构名称}核对一下",
                        "old": cfg["stages"]["IDENTITY"]["reply_ask_tail"]}]}
    ok_static = bool(evo.validate_candidate(bad, cfg))
    import inspect
    ok_iface = list(inspect.signature(AS.say).parameters) == ["self", "agent_text"]

    checks = [("同种子逐字节可复现", ok_det), ("绝不念出花括号/XX 占位", ok_no_placeholder),
              ("客服复读时难度确实上升", ok_escalate),
              ("客服正常回答时不乱加压", ok_calm),
              ("档案缺事实时不编造、不念占位", ok_fact),
              ("闸门第六道：平均值涨但 L4 塌了会拒绝", ok_group),
              ("静态校验能拦下新增花括号占位", ok_static),
              ("say() 只收客服文本，拿不到阶段/意图/槽位", ok_iface)]
    for n, ok in checks:
        print("  [%s] %s" % ("通过" if ok else "失败", n))
    return 0 if all(ok for _, ok in checks) else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--scene", default="")
    r.add_argument("--json", default="")
    r.add_argument("--tag", default="adversarial")
    r.add_argument("--ids", default="")
    r.add_argument("--quiet", action="store_true")
    p = sub.add_parser("propose")
    p.add_argument("--out", default="")
    g = sub.add_parser("gate")
    g.add_argument("candidate")
    sub.add_parser("check")
    a = ap.parse_args()
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "propose":
        return cmd_propose(a)
    if a.cmd == "gate":
        return cmd_gate(a)
    if a.cmd == "check":
        return cmd_check(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
