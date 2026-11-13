#!/usr/bin/env python3
"""多轮自进化：跑批 → 逐轮正负反馈 → 改一个可学习开关 → 复跑同一批客户 → 留档。

用法（项目根目录）：
    python scripts/self_evolve.py run                      # 默认最多 6 轮
    python scripts/self_evolve.py run --scene random        # 随机挑一个场景跑（防过拟合）
    python scripts/self_evolve.py run --rounds 8 --ids A01,A04,A07
    python scripts/self_evolve.py report                   # 看最近一次
    python scripts/self_evolve.py check                    # 反向自测（含一次迷你真跑）
    python scripts/self_evolve.py promote <run_id> --yes   # 把学到的策略写进线上配置

为什么必须一轮只改一处：
    改两处再复跑，收益只能算在“这两处”头上，分不清是哪一处起了作用，
    分不清就无法在下一轮继续（无效的那处会一直占着开关）。
    一轮一处、每处都复跑同一批客户，日志里的每条“调整 → 结果”才是一一对应的。

什么能自动改、什么不能：
    能改：`evolution_policy` 里的分支开关（是否复述、某类问题路由到哪句话术、
          复述是否带具体原因、开场确认是否换问法）。白名单见 modules/policy.py。
    不能改：代码逻辑、must_say 这类合规结构、客户画像、判据本身。
    判据与画像一变，历史分数就不能比了；闭环不给自己改考卷的权力。

四道验收（任一不过就退回这一处改动）：
    1. 目标缺陷码的条数必须下降（不是“总分好看”，是指名道姓那条少了）；
    2. 负面反馈总条数必须下降；
    3. 通过数不降、holdout 不降（有 holdout 时）；
    4. 没有任何一通电话从“零负面”变成“有负面”（不许拆东墙补西墙）。
"""

import argparse
import copy
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from modules import feedback, policy  # noqa: E402

_PY = sys.executable
_BASE_SCENE = os.path.join(_ROOT, "config", "scenes", "pop_reject.json")
_OUT_ROOT = os.path.join(_ROOT, "eval", "reports", "self_evolve")
_INDEX = os.path.join(_OUT_ROOT, "index.jsonl")
#: 本次 run 真正在跑的场景。`--scene random` 会在 cmd_run 里改写它。
_SCENE_PATH = _BASE_SCENE
#: 默认上架目标：当前线上场景 + 它的运行时副本。
_LIVE_FILES = (os.path.join(_ROOT, "config", "scenes", "pop_reject.json"),
               os.path.join(_ROOT, "config", "scene.json"))


def _scene_candidates():
    """config/scenes 下所有真场景（排除 .bak 备份）。"""
    d = os.path.join(_ROOT, "config", "scenes")
    return [os.path.join(d, f) for f in sorted(os.listdir(d))
            if f.endswith(".json") and ".bak" not in f]


def _resolve_scene(arg):
    """把 --scene 解析成绝对路径；`random` 从**所有**场景里随机挑。

    为什么不只挑"已接入自进化"的场景：
        只在 pop_reject 上反复调参，等于对着同一张卷子刷分。抽到没有可学面的
        场景时，报告会如实写"无可自动调整的簇"，并把诊断（负面构成、缺陷码）
        留在日志里——那是"要不要把这个场景接入"的依据，不是白跑。
    """
    arg = (arg or "").strip()
    if not arg or arg == "default":
        return _BASE_SCENE
    if arg == "random":
        cands = _scene_candidates() or [_BASE_SCENE]
        pick = random.choice(cands)
        print("随机场景：%s（候选 %s）"
              % (os.path.basename(pick)[:-5],
                 "、".join("%s%s" % (os.path.basename(c)[:-5],
                                     "✓" if _scene_ready(c) else "✗")
                          for c in cands)))
        if not _scene_ready(pick):
            print("  注意：%s 的 start_call 不返回客户档案（售后/零售场景按 C5 场景隔离"
                  "**故意不接外呼名单**），客服问什么客户都答不出，\n"
                  "  闭环率必然是 0——这不是对话质量差，是数据没接。"
                  "本轮数字只能看诊断，不能用来调参。"
                  % os.path.basename(pick)[:-5])
        return pick
    path = arg if os.path.isabs(arg) else os.path.join(_ROOT, "config", "scenes",
                                                       arg + ".json")
    if not os.path.exists(path):
        raise SystemExit("没有这个场景：%s（可用：%s）"
                         % (arg, "、".join(os.path.basename(c)[:-5]
                                          for c in _scene_candidates())))
    return path


def _scene_ready(path):
    """这个场景能不能跑出可信的闭环数字：必须有客户档案（start_call 会给公司/单号）。

    售后/零售场景不接外呼名单是**设计如此**（C5 场景隔离：两个业务域不共用数据）。
    拿资审画像去跑它们，客户手里没有可核身事实，闭环永远达不成——那是数据没接，
    不是客服答得差。判断写在这里，随机抽场景时才不会把两者混为一谈。
    """
    try:
        return bool(_load(path).get("application_mode"))
    except (OSError, ValueError):
        return False


def _live_files(base_scene_rel):
    """上架要写哪几份：该场景文件；若它就是当前线上场景，再写运行时副本。

    随机抽到别的场景时**不动** config/scene.json——那会把线上场景换掉，
    一次调参实验不该改变线上在跑哪个业务。
    """
    base = base_scene_rel if os.path.isabs(base_scene_rel) \
        else os.path.join(_ROOT, base_scene_rel)
    files = [base]
    if os.path.basename(base) == "pop_reject.json":
        files.append(os.path.join(_ROOT, "config", "scene.json"))
    return files

#: 调整库：一条负面反馈目标 → 一处配置改动。
#: 全部是**打开已有分支**或**把某类问题路由到已有话术**，不新增文案、不改代码。
ADJUSTMENTS = [
    {
        "id": "confirm_specific",
        "target": "policy:confirm_specific",
        "change": (("evolution_policy", "confirm_specific"), True),
        "expect": "generic_summary",
        "why": "复述时用具体驳回原因与材料，不降级成“材料问题”",
    },
    {
        "id": "repair.OFFER",
        "target": "policy:repair.OFFER",
        "change": (("evolution_policy", "repair", "OFFER"), True),
        "expect": "repeat",
        "why": "客户说“再说一遍”时复述上一段，而不是回“确认不到”",
    },
    {
        "id": "repair.CONFIRM",
        "target": "policy:repair.CONFIRM",
        "change": (("evolution_policy", "repair", "CONFIRM"), True),
        "expect": "repeat",
        "why": "确认环节的“再说一遍”走复述，不走“没听懂两次收线”",
    },
    {
        "id": "greeting_company_alt",
        "target": "policy:greeting_company_alt",
        "change": (("evolution_policy", "greeting_company_alt"), True),
        "expect": "repeat",
        "why": "开场公司确认连问不改字，换成“一句是/否”与“让负责人接”交替",
    },
    {
        "id": "route.OFFER.material",
        "target": "policy:answer_routes.OFFER.material",
        "change": (("evolution_policy", "answer_routes", "OFFER", "material"),
                   "reply_application_detail"),
        "expect": "no_answer",
        "why": "OFFER 被问补什么材料时，用本单材料清单回答",
    },
    {
        "id": "route.OFFER.reason",
        "target": "policy:answer_routes.OFFER.reason",
        "change": (("evolution_policy", "answer_routes", "OFFER", "reason"),
                   "reply_application_detail"),
        "expect": "no_answer",
        "why": "OFFER 被问为什么驳回时，用本单驳回原因回答",
    },
    {
        "id": "route.OFFER.provenance",
        "target": "policy:answer_routes.OFFER.provenance",
        "change": (("evolution_policy", "answer_routes", "OFFER", "provenance"),
                   "reply_provenance"),
        "expect": "no_answer",
        "why": "被问“号码哪来的”时，先讲清号码来源与用途",
    },
    {
        "id": "route.CONFIRM.material",
        "target": "policy:answer_routes.CONFIRM.material",
        "change": (("evolution_policy", "answer_routes", "CONFIRM", "material"),
                   "reply_application_answer"),
        "expect": "no_answer",
        "why": "确认环节被问补什么材料时，用本单材料清单回答并回到确认",
    },
    {
        "id": "route.CONFIRM.reason",
        "target": "policy:answer_routes.CONFIRM.reason",
        "change": (("evolution_policy", "answer_routes", "CONFIRM", "reason"),
                   "reply_application_answer"),
        "expect": "no_answer",
        "why": "确认环节被问驳回原因时，用本单原因回答并回到确认",
    },
    {
        "id": "route.CONFIRM.provenance",
        "target": "policy:answer_routes.CONFIRM.provenance",
        "change": (("evolution_policy", "answer_routes", "CONFIRM", "provenance"),
                   "reply_provenance"),
        "expect": "no_answer",
        "why": "确认环节被质疑号码来源时，回答来源而不是“确认不到”",
    },
    {
        "id": "route.CONFIRM.fee",
        "target": "policy:answer_routes.CONFIRM.fee",
        "change": (("evolution_policy", "answer_routes", "CONFIRM", "fee"),
                   "reply_ask_fee"),
        "expect": "no_answer",
        "why": "确认环节被问收费时，明确回答不收费",
    },
]


# ---------------------------------------------------------------------------
# 基础读写
# ---------------------------------------------------------------------------

def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def _sha(cfg):
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _set_path(cfg, parts, value):
    node = cfg
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            node[p] = nxt
        node = nxt
    node[parts[-1]] = value


def _get_path(cfg, parts):
    node = cfg
    for p in parts:
        if not isinstance(node, dict):
            return None
        node = node.get(p)
    return node


def _index_append(entry):
    """追加一条索引；同一次运行的旧行先删掉，避免时间不同、指标相同的重复记录。

    只按 (run_id, kind) 去重：一次训练和它的上架记录是两件事，都该留下。
    """
    rows = []
    if os.path.exists(_INDEX):
        with open(_INDEX, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    old = json.loads(line)
                except ValueError:
                    rows.append(line)
                    continue
                if (old.get("run_id") == entry.get("run_id")
                        and old.get("kind", "train") == entry.get("kind", "train")):
                    continue
                rows.append(line)
    rows.append(json.dumps(entry, ensure_ascii=False))
    os.makedirs(os.path.dirname(_INDEX) or ".", exist_ok=True)
    with open(_INDEX, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(rows) + "\n")


# ---------------------------------------------------------------------------
# 一次评测 = 一份配置 + 一批客户 + 逐轮反馈
# ---------------------------------------------------------------------------

def evaluate(cfg, run_dir, tag, ids="", max_turns=14):
    """在一个子进程里跑同一批客户，返回报告 + 逐轮反馈汇总。

    子进程隔离沿用 adversarial_evolve 的做法：app 启动期会缓存场景，
    同进程连跑两份会拿到旧缓存，出现“改了配置、分数没动”的假结论。
    """
    scene = os.path.join(run_dir, "scene_%s.json" % tag)
    out = os.path.join(run_dir, "report_%s.json" % tag)
    _save(scene, cfg)
    cmd = [_PY, os.path.join(_ROOT, "scripts", "adversarial_evolve.py"), "run",
           "--scene", os.path.relpath(scene, _ROOT),
           "--json", os.path.relpath(out, _ROOT), "--quiet", "--tag", tag]
    if ids:
        cmd += ["--ids", ids]
    r = subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise SystemExit("跑批失败（%s）：\n%s\n%s" % (tag, r.stdout[-1500:], r.stderr[-1500:]))
    report = _load(out)
    fb = feedback.summarise(report)
    return {"report": report, "fb": fb, "metrics": _metrics(report, fb),
            "scene": os.path.relpath(scene, _ROOT).replace("\\", "/"), "sha": _sha(cfg)}


def _metrics(report, fb):
    s = report.get("summary") or {}
    return {
        "pass": {"ok": (s.get("all") or {}).get("ok", 0),
                 "total": (s.get("all") or {}).get("total", 0)},
        "train": {"ok": (s.get("train") or {}).get("ok", 0),
                  "total": (s.get("train") or {}).get("total", 0)},
        "holdout": {"ok": (s.get("holdout") or {}).get("ok", 0),
                    "total": (s.get("holdout") or {}).get("total", 0)},
        "agent_defect_sessions": s.get("agent_defect_sessions", 0),
        "neg": fb["neg"], "pos": fb["pos"], "turns": fb["turns"],
        "codes": fb["codes"],
        "by_stage_code": _by_stage_code(fb),
        "neg_sessions": [x["id"] for x in fb["sessions"] if x["neg"] > 0],
    }


def _by_stage_code(fb):
    out = {}
    for sess in fb["detail"]:
        for t in sess["turns"]:
            if t["polarity"] != "-":
                continue
            for c in t["codes"]:
                k = "%s|%s" % (c, t["stage"] or "?")
                out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def clusters(fb):
    """按可学习目标聚合负面反馈：目标 → 条数、涉及会话、示例原话。"""
    agg = {}
    for sess in fb["detail"]:
        for t in sess["turns"]:
            if t["polarity"] != "-" or not t["target"]:
                continue
            c = agg.setdefault(t["target"], {
                "target": t["target"], "count": 0, "codes": {},
                "sessions": [], "examples": []})
            c["count"] += 1
            for code in t["codes"]:
                c["codes"][code] = c["codes"].get(code, 0) + 1
            if sess["id"] not in c["sessions"]:
                c["sessions"].append(sess["id"])
            if len(c["examples"]) < 3:
                c["examples"].append({
                    "session": sess["id"], "turn": t["turn"],
                    "user": (t["user_text"] or "")[:60],
                    "agent": (t["evidence"] or "")[:60],
                    "why": t["why"],
                })
    out = []
    for c in agg.values():
        c["codes"] = dict(sorted(c["codes"].items(), key=lambda kv: -kv[1]))
        out.append(c)
    return sorted(out, key=lambda c: (-c["count"], c["target"]))


def code_only(fb):
    """没映射到开关的负面反馈：只能改代码，闭环不自动动，但必须写进日志。"""
    agg = {}
    for sess in fb["detail"]:
        for t in sess["turns"]:
            if t["polarity"] != "-" or t["target"]:
                continue
            for code in t["codes"]:
                k = "%s|%s" % (code, t["stage"] or "?")
                a = agg.setdefault(k, {"key": k, "code": code, "stage": t["stage"],
                                       "count": 0, "sessions": []})
                a["count"] += 1
                if sess["id"] not in a["sessions"]:
                    a["sessions"].append(sess["id"])
    return sorted(agg.values(), key=lambda a: -a["count"])


# ---------------------------------------------------------------------------
# 验收
# ---------------------------------------------------------------------------

def check(before, after, adj):
    """四道验收；返回 [(名字, 是否通过, 细节)]。"""
    bm, am = before["metrics"], after["metrics"]
    code = adj["expect"]
    b_code = sum(v for k, v in bm["by_stage_code"].items() if k.startswith(code + "|"))
    a_code = sum(v for k, v in am["by_stage_code"].items() if k.startswith(code + "|"))
    new_neg = sorted(set(am["neg_sessions"]) - set(bm["neg_sessions"]))
    hold_ok = (am["holdout"]["ok"] >= bm["holdout"]["ok"]
               if bm["holdout"]["total"] else True)
    return [
        ("目标缺陷下降", a_code < b_code, "%s %d→%d" % (code, b_code, a_code)),
        ("负面反馈总量下降", am["neg"] < bm["neg"],
         "%d→%d（正面 %d→%d）" % (bm["neg"], am["neg"], bm["pos"], am["pos"])),
        ("通过数不降", am["pass"]["ok"] >= bm["pass"]["ok"],
         "%d/%d→%d/%d" % (bm["pass"]["ok"], bm["pass"]["total"],
                          am["pass"]["ok"], am["pass"]["total"])),
        ("holdout 不降", hold_ok,
         "%d/%d→%d/%d" % (bm["holdout"]["ok"], bm["holdout"]["total"],
                          am["holdout"]["ok"], am["holdout"]["total"])),
        ("无新增负面会话", not new_neg, "、".join(new_neg) or "无"),
    ]


def pick(cl, tried):
    """挑一个还没试过、且目标有调整库条目的簇（条数多的先改）。"""
    for c in cl:
        if c["target"] in tried:
            continue
        adj = next((a for a in ADJUSTMENTS if a["target"] == c["target"]), None)
        if adj:
            return c, adj
    return None, None


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

_FB_FIELDS = ("round", "session", "turn", "polarity", "codes", "why", "evidence",
              "target", "stage", "action", "user_text", "agent_text")


def _write_feedback_jsonl(path, round_no, fb, decision):
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for sess in fb["detail"]:
            for t in sess["turns"]:
                row = {
                    "round": round_no, "session": sess["id"], "turn": t["turn"],
                    "polarity": t["polarity"], "codes": t["codes"], "why": t["why"],
                    "evidence": t["evidence"], "target": t["target"],
                    "stage": t["stage"], "action": t["action"],
                    "user_text": (t["user_text"] or "")[:120],
                    "agent_text": (t["evidence"] or "")[:120],
                    "decision": decision,
                }
                f.write(json.dumps({k: row[k] for k in _FB_FIELDS},
                                   ensure_ascii=False) + "\n")


def _round_md(rd):
    b, a = rd["before"]["metrics"], rd["after"]["metrics"]
    lines = ["## 第 %d 轮" % rd["round"], ""]
    lines.append("- 开始：负面 %d 条 / 正面 %d 条 / 通过 %d/%d（配置 %s）"
                 % (b["neg"], b["pos"], b["pass"]["ok"], b["pass"]["total"],
                    rd["before"]["sha"]))
    lines.append("- 反馈构成：%s" % (json.dumps(rd["feedback"]["codes"], ensure_ascii=False)
                                   or "无"))
    att = rd.get("attempt")
    if not att:
        lines += ["- 本轮没有可自动调整的簇（剩下的缺陷只能改代码），闭环收敛。", ""]
        return "\n".join(lines)
    lines.append("- 调整：`%s` —— %s" % (att["adjustment"], att["why"]))
    lines.append("- 改动：%s" % json.dumps(att["change"], ensure_ascii=False))
    for c in att["checks"]:
        lines.append("  - [%s] %s：%s"
                     % ("通过" if c["pass"] else "不过", c["name"], c["detail"]))
    lines.append("- 判定：**%s**%s" % ("采纳" if att["decision"] == "accepted" else "退回",
                                     "（%s）" % att["reason"] if att.get("reason") else ""))
    lines.append("- 结束：负面 %d 条 / 正面 %d 条 / 通过 %d/%d（配置 %s）"
                 % (a["neg"], a["pos"], a["pass"]["ok"], a["pass"]["total"],
                    rd["after"]["sha"]))
    lines.append("")
    return "\n".join(lines)


def _md_header(run):
    m = run["rounds"][0]["before"]["metrics"] if run.get("rounds") else {}
    last = run["rounds"][-1]["after"]["metrics"] if run.get("rounds") else m
    lines = [
        "# 客服自进化训练日志", "",
        "- 运行编号：`%s`" % run["run_id"],
        "- 开始时间：%s" % run["started_at"],
        "- 批次：%s（%d 通客户）" % (run["batch"], m.get("pass", {}).get("total", 0)),
        "- 基线配置：`%s`（sha %s）" % (run["base_scene"], run["base_sha"]),
        "- 末轮配置：`%s`（sha %s）" % (run["final_scene"], run["final_sha"]),
        "- 逐轮明细：`feedback.jsonl`（每一轮、每一句话的判定与证据）",
        "",
        "## 总账", "",
        "| 轮 | 负面 | 正面 | 通过 | 调整 | 判定 |",
        "|---|---|---|---|---|---|",
    ]
    for rd in run["rounds"]:
        a = rd["after"]["metrics"]
        att = rd.get("attempt") or {}
        lines.append("| %d | %d | %d | %d/%d | %s | %s |" % (
            rd["round"], a["neg"], a["pos"], a["pass"]["ok"], a["pass"]["total"],
            att.get("adjustment", "—"), att.get("decision", "无")))
    lines += ["", "负面反馈 %d → %d 条（%+.0f%%）；正面反馈 %d → %d 条；通过 %d/%d → %d/%d。"
              % (m["neg"], last["neg"],
                 (last["neg"] - m["neg"]) / float(m["neg"] or 1) * 100,
                 m["pos"], last["pos"],
                 m["pass"]["ok"], m["pass"]["total"],
                 last["pass"]["ok"], last["pass"]["total"]), ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def cmd_run(a):
    global _SCENE_PATH
    _SCENE_PATH = _resolve_scene(getattr(a, "scene", ""))
    base = _load(_SCENE_PATH)
    run_id = a.run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    run_dir = os.path.join(_OUT_ROOT, run_id)
    os.makedirs(run_dir, exist_ok=True)
    fb_path = os.path.join(run_dir, "feedback.jsonl")
    if os.path.exists(fb_path):
        os.remove(fb_path)

    work = copy.deepcopy(base)
    print("多轮自进化｜批次 %s｜最多 %d 轮｜每轮只改一处，改完复跑同一批客户"
          % (a.batch, a.rounds))
    print("基线配置：%s（sha %s）"
          % (os.path.relpath(_SCENE_PATH, _ROOT), _sha(base)))
    print("开始跑第 0 轮（未学习基线）…")
    cur = evaluate(work, run_dir, "r00", a.ids, a.max_turns)
    m0 = cur["metrics"]
    print("  基线：通过 %d/%d｜负面 %d｜正面 %d｜构成 %s"
          % (m0["pass"]["ok"], m0["pass"]["total"], m0["neg"], m0["pos"],
             json.dumps(m0["codes"], ensure_ascii=False)))

    run = {
        "run_id": run_id, "started_at": _now(), "batch": a.batch,
        "ids": a.ids, "rounds_requested": a.rounds, "max_turns": a.max_turns,
        "base_scene": os.path.relpath(_SCENE_PATH, _ROOT).replace("\\", "/"),
        "base_sha": _sha(base), "base_metrics": m0,
        "base_feedback": cur["fb"]["codes"], "base_clusters": clusters(cur["fb"]),
        "rounds": [], "accepted": [], "rejected": [], "code_only": code_only(cur["fb"]),
        "scenes": {"r00": cur["scene"]},
    }
    _write_feedback_jsonl(fb_path, 0, cur["fb"], "baseline")

    tried = set()
    for rno in range(1, a.rounds + 1):
        cl = clusters(cur["fb"])
        top, adj = pick(cl, tried)
        target = top["target"] if top else ""
        rd = {"round": rno, "started_at": _now(),
              "before": {"metrics": cur["metrics"], "sha": cur["sha"]},
              "feedback": {"turns": cur["fb"]["turns"], "neg": cur["fb"]["neg"],
                           "pos": cur["fb"]["pos"], "codes": cur["fb"]["codes"],
                           "targets": cur["fb"]["targets"]},
              "clusters": cl, "attempt": None, "accepted": [], "rejected": []}
        if not adj:
            print("\n第 %d 轮｜没有可自动调整的簇，收敛。" % rno)
            rd["after"] = {"metrics": cur["metrics"], "sha": cur["sha"]}
            run["rounds"].append(rd)
            _save(os.path.join(run_dir, "round_%02d.json" % rno), rd)
            break

        trial = copy.deepcopy(work)
        parts, value = adj["change"]
        before_val = _get_path(trial, list(parts))
        _set_path(trial, list(parts), value)
        print("\n第 %d 轮｜簇 `%s`（%d 条，会话 %s）→ 调整 %s：%s"
              % (rno, target, top["count"], "、".join(top["sessions"]),
                 adj["id"], adj["why"]))
        after = evaluate(trial, run_dir, "r%02d" % rno, a.ids, a.max_turns)
        checks = check(cur, after, adj)
        passed = all(ok for _, ok, _ in checks)
        for n, ok, d in checks:
            print("  [%s] %s：%s" % ("通过" if ok else "不过", n, d))

        att = {"adjustment": adj["id"], "target": target, "why": adj["why"],
               "change": {".".join(parts): value}, "previous": before_val,
               "after_sha": after["sha"],
               "checks": [{"name": n, "pass": bool(ok), "detail": d} for n, ok, d in checks],
               "decision": "accepted" if passed else "rejected",
               "reason": "" if passed else "；".join(
                   "%s：%s" % (n, d) for n, ok, d in checks if not ok)}
        if passed:
            work = trial
            cur = after
            tried.add(target)
            run["accepted"].append({"round": rno, "adjustment": adj["id"],
                                    "target": target, "change": att["change"]})
            rd["accepted"].append(adj["id"])
            print("  [采纳] 负面 %d→%d，正面 %d→%d，通过 %d/%d"
                  % (rd["before"]["metrics"]["neg"], after["metrics"]["neg"],
                     rd["before"]["metrics"]["pos"], after["metrics"]["pos"],
                     after["metrics"]["pass"]["ok"], after["metrics"]["pass"]["total"]))
        else:
            tried.add(target)
            run["rejected"].append({"round": rno, "adjustment": adj["id"],
                                    "target": target, "reason": att["reason"]})
            rd["rejected"].append({"adjustment": adj["id"], "reason": att["reason"]})
            print("  [退回] 这一处改动没有净收益，配置未动；原因已留档")
        rd["attempt"] = att
        rd["after"] = {"metrics": cur["metrics"], "sha": cur["sha"]}
        run["rounds"].append(rd)
        _save(os.path.join(run_dir, "round_%02d.json" % rno), rd)
        _write_feedback_jsonl(fb_path, rno, after["fb"], att["decision"])

        if cur["metrics"]["neg"] == 0:
            print("\n第 %d 轮｜负面反馈清零，提前收敛。" % rno)
            break

    final_scene = os.path.join(run_dir, "final.scene.json")
    _save(final_scene, work)
    run.update({"finished_at": _now(), "final_scene":
                os.path.relpath(final_scene, _ROOT).replace("\\", "/"),
                "final_sha": _sha(work),
                "final_metrics": cur["metrics"],
                "policy_learned": work.get("evolution_policy") or {},
                "policy_desc": policy.describe(work.get("evolution_policy") or {}),
                "code_only": code_only(cur["fb"]),
                "scenes": dict(run.get("scenes") or {})})
    run["scenes"]["final"] = run["final_scene"]
    _save(os.path.join(run_dir, "run.json"), run)

    md = _md_header(run) + "\n" + "\n".join(_round_md(rd) for rd in run["rounds"])
    if run["code_only"]:
        md += "\n## 只能改代码的缺陷（闭环不自动动）\n\n"
        md += "| 缺陷 | 次数 | 会话 |\n|---|---|---|\n"
        for c in run["code_only"]:
            md += "| %s | %d | %s |\n" % (c["key"], c["count"], "、".join(c["sessions"]))
        md += ("\n这些没有对应的可学习开关：判据没把它映射到配置，闭环就不碰它。\n"
               "要修只能改 dialogue 代码，改完必须重定基线，否则前后分数不可比。\n")
    with open(os.path.join(run_dir, "training_log.md"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(md + "\n")

    _index_append({
        "kind": "train",
        "run_id": run_id, "at": run["started_at"], "rounds": len(run["rounds"]),
        "batch": a.batch, "ids": a.ids,
        "neg": [m0["neg"], run["final_metrics"]["neg"]],
        "pos": [m0["pos"], run["final_metrics"]["pos"]],
        "pass": [[m0["pass"]["ok"], m0["pass"]["total"]],
                 [run["final_metrics"]["pass"]["ok"],
                  run["final_metrics"]["pass"]["total"]]],
        "accepted": [x["adjustment"] for x in run["accepted"]],
        "changed": bool(run["accepted"]),
    })

    print("\n" + "=" * 70)
    print("训练完成：%s" % run_id)
    print("负面反馈 %d → %d 条；正面 %d → %d 条；通过 %d/%d → %d/%d"
          % (m0["neg"], run["final_metrics"]["neg"], m0["pos"],
             run["final_metrics"]["pos"], m0["pass"]["ok"], m0["pass"]["total"],
             run["final_metrics"]["pass"]["ok"], run["final_metrics"]["pass"]["total"]))
    print("采纳 %d 处：%s" % (len(run["accepted"]),
                            "、".join(x["adjustment"] for x in run["accepted"]) or "无"))
    if run["rejected"]:
        print("退回 %d 处：%s" % (len(run["rejected"]),
                                "、".join(x["adjustment"] for x in run["rejected"])))
    if run["code_only"]:
        print("只能改代码的缺陷：%s"
              % "、".join(c["key"] for c in run["code_only"][:6]))
    print("日志：%s" % os.path.relpath(os.path.join(run_dir, "training_log.md"), _ROOT))
    print("逐句记录：%s" % os.path.relpath(fb_path, _ROOT))
    print("学到的策略：%s" % run["policy_desc"])
    print("上架（需人工确认）：python scripts/self_evolve.py promote %s --yes" % run_id)
    return 0


# ---------------------------------------------------------------------------
# 查看 / 自测 / 上架
# ---------------------------------------------------------------------------

def _latest_run():
    if not os.path.isdir(_OUT_ROOT):
        return None
    cands = [d for d in os.listdir(_OUT_ROOT)
             if os.path.isfile(os.path.join(_OUT_ROOT, d, "run.json"))]
    if not cands:
        return None
    return max(cands, key=lambda d: os.path.getmtime(
        os.path.join(_OUT_ROOT, d, "run.json")))


def cmd_report(a):
    run_id = a.run_id if a.run_id and a.run_id != "latest" else _latest_run()
    if not run_id:
        print("还没有训练记录：先跑 python scripts/self_evolve.py run")
        return 1
    run = _load(os.path.join(_OUT_ROOT, run_id, "run.json"))
    if a.json:
        print(json.dumps(run, ensure_ascii=False, indent=1))
        return 0
    print("运行 %s｜%s｜批次 %s" % (run["run_id"], run["started_at"], run["batch"]))
    print("%-4s %-6s %-6s %-8s %-22s %s" % ("轮", "负面", "正面", "通过", "调整", "判定"))
    for rd in run["rounds"]:
        m = rd["after"]["metrics"]
        att = rd.get("attempt") or {}
        print("%-4d %-6d %-6d %-8s %-22s %s"
              % (rd["round"], m["neg"], m["pos"],
                 "%d/%d" % (m["pass"]["ok"], m["pass"]["total"]),
                 att.get("adjustment", "—"), att.get("decision", "无")))
    print("\n学到的策略：%s" % run.get("policy_desc", ""))
    for x in run["code_only"]:
        print("只能改代码：%s（%d 次）" % (x["key"], x["count"]))
    print("日志：eval/reports/self_evolve/%s/training_log.md" % run_id)
    return 0


def cmd_check(a):
    """反向自测：判据、调整库、验收闸门、日志与隔离。

    每一条都配一个**必须失败**的反例：判据放宽、调整无收益、往生产配置写，
    只要其中一处被偷偷放开，这里就会红。
    """
    checks = []

    def ck(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    # 1) 逐轮判据：真复读要判负；只是共同开头不同问题不判负
    turns = [
        {"user": {"text": "您好", "action": "ack"},
         "agent": {"reply_text": "这个问题我得先确认您是哪家公司才能讲，是这家公司的负责人吗？",
                   "stage": "GREETING", "next_stage": "GREETING"}},
        {"user": {"text": "啊，你哪来的我号码", "action": "ask_why"},
         "agent": {"reply_text": "这个问题我得先确认您是哪家公司才能讲，您只要回我一句是或不是？",
                   "stage": "GREETING", "next_stage": "GREETING"}},
        {"user": {"text": "嗯", "action": "ack"},
         "agent": {"reply_text": "这个问题我得先确认您是哪家公司才能讲，您只要回我一句是或不是？",
                   "stage": "GREETING", "next_stage": "GREETING"}},
    ]
    r1 = feedback.judge_turn(turns, 1)
    r2 = feedback.judge_turn(turns, 2)
    ck("共同开头但内容不同不判复读", "repeat" not in r1["codes"], str(r1["codes"]))
    ck("实质相同的相邻回复判复读", "repeat" in r2["codes"], str(r2["codes"]))

    # 2) 合规性延迟回答不算“没回答”；万能挡箭牌仍算
    t_ok = [{"user": {"text": "补什么材料", "action": "ask_material"},
             "agent": {"reply_text": "这个我得先核对身份才能讲，核对上我马上说给您。",
                       "stage": "IDENTITY", "next_stage": "IDENTITY"}}]
    t_bad = [{"user": {"text": "补什么材料", "action": "ask_material"},
              "agent": {"reply_text": "这个我这边确认不到，稍后让专员给您回电。",
                        "stage": "OFFER", "next_stage": "OFFER"}}]
    ck("说明原因的延迟回答不算没回答",
       "no_answer" not in feedback.judge_turn(t_ok, 0)["codes"])
    ck("“确认不到/转专员”仍判没回答",
       "no_answer" in feedback.judge_turn(t_bad, 0)["codes"])

    # 3) 槽位丢失只在“刚给了又再要”时判
    t_slot = [{"user": {"text": "我叫马涛", "action": "answer_name"},
               "agent": {"reply_text": "核对上了，感谢配合。请问您的姓名是？",
                         "stage": "IDENTITY", "next_stage": "NEED"}}]
    ck("给了姓名又被要一遍判槽位丢失",
       "slot_loss" in feedback.judge_turn(t_slot, 0)["codes"])

    # 4) 调整库：目标必须能对应到 policy 白名单，且默认配置下都是“未学”
    base = _load(_BASE_SCENE)
    known = set(policy.LEARNABLE)
    ok_targets = all(ADJUSTMENTS and a["change"][0][1] in known for a in ADJUSTMENTS)
    ck("调整库只动白名单开关", ok_targets,
       "、".join(a["id"] for a in ADJUSTMENTS if a["change"][0][1] not in known) or "全部合规")
    dup = len({a["id"] for a in ADJUSTMENTS}) != len(ADJUSTMENTS)
    ck("调整库无重复 id", not dup)

    # 5) 验收闸门：无收益必须退回；有收益且不回退才采纳
    fake_b = {"metrics": {"neg": 10, "pos": 20, "codes": {"no_answer": 4},
                          "by_stage_code": {"no_answer|CONFIRM": 4},
                          "neg_sessions": ["A1"], "pass": {"ok": 8, "total": 10},
                          "holdout": {"ok": 2, "total": 2}}}
    fake_flat = copy.deepcopy(fake_b)
    fake_good = copy.deepcopy(fake_b)
    fake_good["metrics"].update({"neg": 8, "pos": 22,
                                 "by_stage_code": {"no_answer|CONFIRM": 2}})
    fake_bad = copy.deepcopy(fake_b)
    fake_bad["metrics"].update({"neg": 9, "by_stage_code": {"no_answer|CONFIRM": 2},
                                "neg_sessions": ["A1", "A2"]})
    adj = next(a for a in ADJUSTMENTS if a["expect"] == "no_answer")
    flat_ok = all(ok for _, ok, _ in check(fake_b, fake_flat, adj))
    good_ok = all(ok for _, ok, _ in check(fake_b, fake_good, adj))
    bad_ok = all(ok for _, ok, _ in check(fake_b, fake_bad, adj))
    ck("无收益的改动被退回", not flat_ok)
    ck("有收益且不回退才采纳", good_ok)
    ck("拆东墙补西墙（新增负面会话）被退回", not bad_ok)

    # 6) 迷你真跑：两轮、只挑两通客户，验证日志结构 + 不碰线上配置
    before_live = [_sha(_load(p)) for p in _LIVE_FILES]
    import tempfile
    tmp_root = tempfile.mkdtemp(prefix="sc_selfev_")
    global _OUT_ROOT, _INDEX
    old_out, old_index = _OUT_ROOT, _INDEX
    _OUT_ROOT = tmp_root
    _INDEX = os.path.join(tmp_root, "index.jsonl")
    try:
        ns = argparse.Namespace(run_id="chk", batch="check", ids="A01,A04",
                                rounds=2, max_turns=14, scene="")
        cmd_run(ns)
        run_dir = os.path.join(tmp_root, "chk")
        files = ("run.json", "feedback.jsonl", "training_log.md",
                 "round_01.json", "final.scene.json")
        missing = [f for f in files if not os.path.exists(os.path.join(run_dir, f))]
        ck("迷你真跑产出完整日志", not missing,
           ("缺 " + "、".join(missing)) if missing else "5 个文件齐全")
        run = _load(os.path.join(run_dir, "run.json"))
        rows = [json.loads(x) for x in
                open(os.path.join(run_dir, "feedback.jsonl"), encoding="utf-8")
                if x.strip()]
        ck("逐句记录字段齐全",
           rows and all(set(_FB_FIELDS) <= set(r) for r in rows), "%d 行" % len(rows))
        ck("每轮都有开始时/结束时指标与判定",
           all(rd.get("before") and rd.get("after") and "attempt" in rd
               for rd in run["rounds"]), "%d 轮" % len(run["rounds"]))
        ck("迷你跑没有改动线上配置",
           [_sha(_load(p)) for p in _LIVE_FILES] == before_live)
    finally:
        _OUT_ROOT, _INDEX = old_out, old_index
        shutil.rmtree(tmp_root, ignore_errors=True)

    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        print("  [%s] %s%s" % ("通过" if ok else "失败", name,
                               "｜" + detail if detail else ""))
    print("结果：%d/%d 通过" % (passed, len(checks)))
    return 0 if passed == len(checks) else 1


def cmd_promote(a):
    """把一次训练学到的策略写进两份线上场景文件（人工确认后才做）。

    上架前重跑一次：基线 vs 本次最终配置。收益门槛与训练时一致
    （负面反馈必须严格下降、通过数不降、holdout 不降）；不过就拒绝。
    上线前留 `.bak_<run>` 备份，回滚只需拷回来。
    """
    run_id = a.run_id
    run_dir = os.path.join(_OUT_ROOT, run_id)
    run = _load(os.path.join(run_dir, "run.json"))
    live = _live_files(run["base_scene"])
    if not a.yes:
        print("这会改动线上配置：")
        for p in live:
            print("  - %s" % os.path.relpath(p, _ROOT))
        print("确认请加 --yes")
        return 2
    if not run["accepted"]:
        print("这次训练没有任何被采纳的改动，不上架。")
        return 1
    final_cfg = _load(os.path.join(_ROOT, run["final_scene"]))
    bm = run["base_metrics"]
    fm = run["final_metrics"]
    problems = []
    if not fm["neg"] < bm["neg"]:
        problems.append("负面反馈没有下降：%d→%d" % (bm["neg"], fm["neg"]))
    if fm["pass"]["ok"] < bm["pass"]["ok"]:
        problems.append("通过数下降：%d→%d" % (bm["pass"]["ok"], fm["pass"]["ok"]))
    if bm["holdout"]["total"] and fm["holdout"]["ok"] < bm["holdout"]["ok"]:
        problems.append("holdout 下降")
    if problems:
        print("上架前复算不通过，拒绝写入：\n  " + "\n  ".join(problems))
        return 1
    changes = []
    for p in live:
        cur = _load(p)
        old = cur.get("evolution_policy")
        bak = "%s.bak_%s" % (p, run_id)
        if not os.path.exists(bak):
            shutil.copy2(p, bak)
        cur["evolution_policy"] = copy.deepcopy(final_cfg.get("evolution_policy") or {})
        _save(p, cur)
        changes.append((os.path.relpath(p, _ROOT), old, cur["evolution_policy"]))
    run["promoted_at"] = _now()
    run["promoted_files"] = [c[0] for c in changes]
    _save(os.path.join(run_dir, "run.json"), run)
    _index_append({"kind": "promote", "run_id": run_id,
                   "promoted_at": run["promoted_at"],
                   "files": run["promoted_files"]})
    print("已写入线上配置（策略 %s）：" % run.get("policy_desc", ""))
    for name, _old, new in changes:
        print("  %s → %s" % (name, json.dumps(new, ensure_ascii=False)))
    print("备份：各文件旁的 .bak_%s；回滚：拷回备份并重启服务" % run_id)
    print("提醒：配置改动后请重跑 python scripts/adversarial_evolve.py run 重定基线。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--rounds", type=int, default=6)
    r.add_argument("--batch", default="adversarial")
    r.add_argument("--scene", default="",
                   help="场景名 / random（随机挑一个）；默认 pop_reject")
    r.add_argument("--ids", default="")
    r.add_argument("--run-id", default="")
    r.add_argument("--max-turns", type=int, default=14)
    q = sub.add_parser("report")
    q.add_argument("run_id", nargs="?", default="latest")
    q.add_argument("--json", action="store_true")
    sub.add_parser("check")
    p = sub.add_parser("promote")
    p.add_argument("run_id")
    p.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "report":
        return cmd_report(a)
    if a.cmd == "check":
        return cmd_check(a)
    if a.cmd == "promote":
        return cmd_promote(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
