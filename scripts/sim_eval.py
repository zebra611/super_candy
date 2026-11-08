"""模拟真实用户跑批 —— L2 自进化的"感知器"。

和 scripts/run_eval.py 的分工：
    run_eval.py   跑**固定台词**用例，验证"业务流程通不通"（回归基准，不动）
    sim_eval.py   跑**会反应**的模拟用户，产出**可用于归因的逐轮证据**（本文件）

它做什么：
    1. 对每个画像开一通真电话（进程内直调 handle_turn，不经过 HTTP/ASR/TTS）；
    2. 每一轮先让 UserSim 看"客服实际说了什么"再决定自己说什么；
    3. 收齐逐轮记录后交给 modules/attribution.py 判定责任；
    4. 输出 JSON（给 evolve.py 消费）+ Markdown（给人看）+ 终端逐轮回放。

它不做什么（别误读）：
    - 不模拟声学误差、不测时延 —— 那是语音线的事；
    - 不判定"话说得好不好听" —— 只判流程、槽位、合规与责任归属。

用法：
    python scripts/sim_eval.py                                  # 赛题场景，全部画像
    python scripts/sim_eval.py --scene config/scenes/pop_reject.json
    python scripts/sim_eval.py --json eval/reports/sim_v1.json --report eval/reports/sim_v1.md
    python scripts/sim_eval.py S01 S03                          # 只跑指定画像
"""

import json
import os
import shutil
import sys
import tempfile
from atexit import register as _atexit
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# ---- 参数必须在 import app 之前定下来（app 启动期会缓存配置）-----------------
def _scan_argv(argv):
    opts = {"scene": "", "json": "", "report": "", "tag": "", "ids": [],
            "max_turns": 14, "verbose": True, "personas": ""}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--scene", "--json", "--report", "--tag", "--max-turns", "--personas"):
            if i + 1 >= len(argv):
                raise SystemExit("%s 缺少参数" % a)
            key = a.lstrip("-").replace("-", "_")
            opts["max_turns" if key == "max_turns" else key] = (
                int(argv[i + 1]) if key == "max_turns" else argv[i + 1])
            i += 2
            continue
        if a == "--quiet":
            opts["verbose"] = False
        elif not a.startswith("--"):
            opts["ids"].append(a)
        i += 1
    return opts


_OPTS = _scan_argv(sys.argv[1:])

_SCENE = (_OPTS["scene"] or os.environ.get("EVAL_SCENE") or "pop_reject").strip()
if _SCENE:
    # 允许传场景名或文件路径：路径走 APP_CONFIG（evolve.py 的候选版本就是这样注入的）
    if _SCENE.endswith(".json"):
        os.environ["APP_CONFIG"] = (_SCENE if os.path.isabs(_SCENE)
                                    else os.path.join(_ROOT, _SCENE))
        os.environ.pop("APP_SCENE", None)
    else:
        os.environ["APP_SCENE"] = _SCENE
        os.environ.pop("APP_CONFIG", None)

import events                      # noqa: E402

# 事件库必须隔离到临时目录：模拟跑批会产出几十通"通话"，
# 混进 data/events.db 会把真实通话的时延分位数与告警全带偏（run_eval 踩过）。
_tmp = tempfile.mkdtemp(prefix="sc_simeval_")
events.DATA_DIR = _tmp
events.DB_PATH = os.path.join(_tmp, "sim.db")
events.init_db()
_atexit(lambda: shutil.rmtree(_tmp, ignore_errors=True))

import config_loader               # noqa: E402
from modules import tools           # noqa: E402
from modules.user_sim import UserSim, HANGUP   # noqa: E402
from modules import attribution as attr         # noqa: E402

# 跑批不合成音频、不改写话术：要的是规则层的确定性行为。
config_loader.tts_config = lambda: {
    "provider": "browser", "voice": "zh-CN-XiaoxiaoNeural", "rate": 1.05}
config_loader.nlg_config = lambda: {**config_loader.load().get("nlg", {}),
                                    "provider": "template"}

from app import end_call, handle_turn, start_call   # noqa: E402

# 画像集可注入：性格维度用 eval/personas_temperament.json，
# 原有的 10 类基线画像保持默认，一个字段都不动。
PERSONAS_PATH = _OPTS["personas"] or os.path.join(_ROOT, "eval", "personas_sim.json")
if not os.path.isabs(PERSONAS_PATH):
    PERSONAS_PATH = os.path.join(_ROOT, PERSONAS_PATH)


def _scene_sha(path):
    """场景**内容**哈希（规范化 JSON）。用来证明"这份跑批结果对应的就是这份话术"。

    按内容而不是按字节：同一套场景在仓库里有两份文件，缩进不同、内容相同，
    字节哈希会给它们两个值，校验就变成了假警报。
    """
    import hashlib
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        canon = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def _rules_sha():
    """当前实际生效的性格规则表内容哈希（含候选注入）。"""
    import hashlib
    from modules import persona
    p = os.environ.get(persona.ENV_RULES) or os.path.join(
        _ROOT, "config", "persona_adapt.json")
    if not os.path.isabs(p):
        p = os.path.join(_ROOT, p)
    try:
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
        canon = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def load_personas():
    with open(PERSONAS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("personas") or []


def run_persona(persona, max_turns=14):
    """跑一通完整通话，返回会话记录（含逐轮证据）。"""
    tools._reset_for_test()        # 每个画像从干净的外呼名单开始，保证可复现
    started = start_call()
    sid = started["session_id"]
    company = started.get("company") or {}

    sim = UserSim(persona, company, seed=persona["id"])
    turns = []
    agent_text = started.get("opening") or ""
    end_by = ""
    closed = False

    for _ in range(max_turns):
        if not agent_text:
            end_by = "agent_silent"
            break
        user_out = sim.say(agent_text)
        if user_out is None:
            end_by = "user_hangup"
            break

        res = handle_turn(sid, user_text=user_out["text"])
        turns.append({
            "user": user_out,
            "agent": {
                "reply_text": res.get("reply_text", ""),
                "stage": res.get("stage", ""),
                "next_stage": res.get("next_stage", ""),
                "intent": res.get("intent", ""),
                "end_reason": res.get("end_reason", ""),
                "persona": res.get("persona") or {},
                "slots": res.get("slots") or {},
            },
        })

        if user_out["action"] == HANGUP:
            end_by = "user_hangup"
            break

        if res.get("end_reason"):
            end_by = "agent_end:%s" % res["end_reason"]
            break

        # 回写完成 / 进入 DONE 就该收线了。不在这里停，后面每一轮都会拿到
        # 同一句"感谢您的配合，再见"，在归因里被误判成 agent_loop——
        # 那是跑批的错，不是客服绕圈（第一次跑就被这条假象刷出 21 条假缺陷）。
        if res.get("closed") or res.get("next_stage") in ("WRITEBACK", "DONE"):
            end_by = "agent_closed"
            break

        agent_text = res.get("reply_text", "")
    else:
        end_by = "max_turns"

    if not end_by:
        end_by = "agent_silent"

    evs = events.list_events(sid)
    closed = any(e["type"] == "writeback" for e in evs)
    end_call(sid)

    return {
        "id": persona["id"],
        "label": persona.get("label", ""),
        "goal": persona.get("goal", ""),
        "split": persona.get("split", "train"),
        "company": {k: company.get(k) for k in
                    ("company_name", "contact_role", "phone_tail", "application_id")},
        "blocked_by_missing_fact": list(sim.blocked_by_missing_fact),
        "turns": turns,
        "closed": closed,
        "end_by": end_by,
        "final_stage": (turns[-1]["agent"]["next_stage"] if turns else ""),
    }


def _print_transcript(s):
    print("\n--- %s %s（%s，split=%s）---" % (s["id"], s["label"], s["goal"], s["split"]))
    print("    公司：%s｜联系人身份：%s｜档案尾号：%s"
          % (s["company"].get("company_name"), s["company"].get("contact_role"),
             s["company"].get("phone_tail")))
    for i, t in enumerate(s["turns"], 1):
        u, a = t["user"], t["agent"]
        extra = []
        if u.get("missed"):
            extra.append("听错:%s" % "、".join(u["missed"]))
        print("    %2d 用户[%s] %s%s" % (i, u["action"], u["text"],
                                         ("  (%s)" % ",".join(extra)) if extra else ""))
        print("       客服[%s/%s] %s" % (a["stage"], a["intent"], a["reply_text"][:70]))
    print("    结局：closed=%s｜end_by=%s｜final_stage=%s"
          % (s["closed"], s["end_by"], s["final_stage"]))


def main():
    personas = load_personas()
    if _OPTS["ids"]:
        personas = [p for p in personas if p["id"] in _OPTS["ids"]]
    if not personas:
        print("没有匹配的画像")
        return 1

    sessions = []
    for p in personas:
        s = run_persona(p, max_turns=_OPTS["max_turns"])
        if _OPTS["verbose"]:
            _print_transcript(s)
        sessions.append(s)

    summary = attr.summarise(sessions, personas)
    by_id = {p["id"]: p for p in personas}

    print("\n" + "=" * 78)
    print("模拟用户跑批结果（场景：%s）" % config_loader.scene_name())
    print("=" * 78)
    for r in summary["rows"]:
        print("  %-4s %-14s split=%-7s 期望闭环=%-5s 实际=%-5s 责任=%-11s %s"
              % (r["id"], by_id[r["id"]].get("label", ""), r["split"],
                 str(r["expect"].get("closed")), str(r["closed"]), r["fault"],
                 "通过" if r["ok"] else "**不符预期**"))
    print("-" * 78)
    print("总通过：%d/%d = %.0f%%" % (summary["all"]["ok"], summary["all"]["total"],
                                     summary["all"]["rate"] * 100))
    print("train：%d/%d｜holdout：%d/%d（holdout 仅验收，不得据此调话术）"
          % (summary["train"]["ok"], summary["train"]["total"],
             summary["holdout"]["ok"], summary["holdout"]["total"]))
    print("责任分布：%s" % json.dumps(summary["faults"], ensure_ascii=False))
    print("缺陷码计数：%s" % json.dumps(summary["codes"], ensure_ascii=False))

    # ---- 聚类（只聚 agent 类缺陷）----
    from modules.cluster import cluster_defects
    clusters = cluster_defects(sessions, personas)
    if clusters:
        print("\n失败聚类（仅系统缺陷，按影响会话数排序）：")
        for c in clusters:
            print("  [%2d 通] %s" % (c["sessions"], c["key"]))
            print("        证据：%s" % c["examples"][0])
    else:
        print("\n没有可归因到系统的缺陷簇。")

    by_temper = {}
    for r in summary["rows"]:
        code = by_id[r["id"]].get("temper", "neutral")
        g = by_temper.setdefault(code, {"total": 0, "ok": 0, "ids": [],
                                        "train_ok": 0, "holdout_ok": 0})
        g["total"] += 1
        g["ok"] += 1 if r["ok"] else 0
        g["ids"].append(r["id"])
        if r.get("split") == "holdout":
            g["holdout_ok"] += 1 if r["ok"] else 0
        else:
            g["train_ok"] += 1 if r["ok"] else 0
    if by_temper:
        print("按性格：%s" % "  ".join(
            "%s %d/%d" % (k, v["ok"], v["total"]) for k, v in sorted(by_temper.items())))

    payload = {
        "tag": _OPTS["tag"] or datetime.now().strftime("%m%d_%H%M"),
        "scene": config_loader.scene_name(),
        "scene_path": config_loader.scene_path(),
        # 场景文件内容哈希：给 evolve.py 校验"这份基线到底是用哪份话术跑出来的"。
        # 没有它就会出事：线上场景一升级，再"重跑一次基线"拿到的其实是新版本的分数，
        # 后面闸门拿它当基线比，等于用考过的卷子当没考过的标准（本轮真踩过）。
        "scene_sha": _scene_sha(config_loader.scene_path()),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {k: v for k, v in summary.items() if k != "rows"},
        "rows": summary["rows"],
        "clusters": clusters,
        "sessions": sessions,
        "personas": [{"id": p["id"], "split": p.get("split", "train"),
                      "goal": p.get("goal"), "expect": p.get("expect"),
                      "temper": p.get("temper", "neutral")} for p in personas],
        "personas_path": os.path.relpath(PERSONAS_PATH, _ROOT).replace("\\", "/"),
        # 规则表内容哈希：和 scene_sha 同一个道理。换了规则表却拿旧分数当基线，
        # 性格维度的闸门就全废了。
        "rules_sha": _rules_sha(),
        "by_temper": by_temper,
    }

    if _OPTS["json"]:
        path = _OPTS["json"] if os.path.isabs(_OPTS["json"]) else os.path.join(_ROOT, _OPTS["json"])
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("\nJSON 已写入：%s" % os.path.relpath(path, _ROOT))

    if _OPTS["report"]:
        path = _OPTS["report"] if os.path.isabs(_OPTS["report"]) else os.path.join(_ROOT, _OPTS["report"])
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_render_md(payload))
        print("报告已写入：%s" % os.path.relpath(path, _ROOT))

    return 0


def _render_md(payload):
    s = payload["summary"]
    L = ["# 模拟用户跑批：%s" % payload["scene"], "",
         "> 生成：%s｜场景文件：`%s`" % (payload["generated_at"],
                                    os.path.relpath(payload["scene_path"], _ROOT).replace("\\", "/")),
         "> 用户是**会反应的模拟器**（受知/受听/受耐心约束），不是固定台词；",
         "> 归因分 agent / case / sim 三类，只有 agent 类进优化池。", "",
         "## 汇总", "",
         "| 指标 | 值 |", "|---|---|",
         "| 总通过 | %d/%d = %.0f%% |" % (s["all"]["ok"], s["all"]["total"], s["all"]["rate"] * 100),
         "| train | %d/%d |" % (s["train"]["ok"], s["train"]["total"]),
         "| holdout（仅验收） | %d/%d |" % (s["holdout"]["ok"], s["holdout"]["total"]),
         "| 责任分布 | %s |" % json.dumps(s["faults"], ensure_ascii=False),
         "| 缺陷码计数 | %s |" % json.dumps(s["codes"], ensure_ascii=False), "",
         "## 逐条", "",
         "| ID | 画像 | split | 期望闭环 | 实际 | 责任 | 结论 |",
         "|---|---|---|---|---|---|---|"]
    by_id = {p["id"]: p for p in payload["personas"]}
    for r in payload["rows"]:
        L.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            r["id"], by_id[r["id"]].get("goal", ""), r["split"],
            r["expect"].get("closed"), r["closed"], r["fault"],
            "通过" if r["ok"] else "**不符预期**"))
    L += ["", "## 缺陷聚类（仅系统缺陷）", ""]
    if not payload["clusters"]:
        L.append("无。")
    else:
        L += ["| 会话数 | 聚类键 | 证据 |", "|---|---|---|"]
        for c in payload["clusters"]:
            L.append("| %d | `%s` | %s |" % (c["sessions"], c["key"],
                                             c["examples"][0].replace("|", "\\|")))
    L += ["", "## 逐轮回放", ""]
    for sess in payload["sessions"]:
        L.append("### %s %s（closed=%s，end_by=%s）" % (sess["id"], sess["label"],
                                                       sess["closed"], sess["end_by"]))
        L.append("")
        for i, t in enumerate(sess["turns"], 1):
            L.append("%d. **用户**（%s）：%s" % (i, t["user"]["action"], t["user"]["text"]))
            L.append("   **客服**（%s/%s）：%s" % (t["agent"]["stage"], t["agent"]["intent"],
                                                t["agent"]["reply_text"]))
        L.append("")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
