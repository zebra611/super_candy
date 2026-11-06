#!/usr/bin/env python3
"""文字对抗对局生成器：让模拟客户与客服系统真打一通电话。

一半角色由系统演（8 类客户），一半由客服系统应答（app.handle_turn）。
每通电话记录完整对话、客服环节推进、事件流与合规结论，供评估与进化使用。

为什么客户用规则模拟而不是大模型：
    1. 可复现——同一角色同一轮次说的话每次都一样，回归才有基准；
    2. 零成本、离线——不依赖任何 API Key，队友 clone 下来就能跑；
    3. 可控——能精确制造"逼承诺""报错尾号""连续沉默"这类边界，而不是碰运气。

用法：
    python3 scripts/sim_dialogue.py                    # 8×6 全矩阵
    python3 scripts/sim_dialogue.py --pairs 6          # 只跑 6 组
    python3 scripts/sim_dialogue.py --persona P01 --scenario S03
"""

import argparse
import datetime
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_eval as R                       # noqa: E402  复用临时库隔离与 TTS 打桩
import events                              # noqa: E402
from modules import eval as eval_mod       # noqa: E402
from app import end_call, handle_turn, start_call   # noqa: E402

SIM_DIR = os.path.join(R._ROOT, "eval", "sim")
PERSONAS_PATH = os.path.join(SIM_DIR, "personas.json")
SCENARIOS_PATH = os.path.join(SIM_DIR, "scenarios.json")
RUNS_DIR = os.path.join(SIM_DIR, "runs")

# 客服阶段 → 客户台词类别。这层映射决定"客服问到哪，客户就答哪"，
# 否则客户会答非所问地乱跳，对局就失去意义。
STAGE_CLASS = {
    "GREETING": "greeting",
    "IDENTITY": "identity",
    "NEED": "need",
    "OFFER": "offer",
    "CONFIRM": "confirm",
    "WRITEBACK": "closing",
    "DONE": "closing",
}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _pick(pool, seed, turn):
    """确定性选句：同一 (角色,场景,轮次) 永远选同一句。

    不用 random：random 即使固定种子，也很容易被后续代码改动打乱序列，
    导致"同一版代码跑出两个结果"，回归就失去意义。
    """
    if not pool:
        return ""
    digest = hashlib.sha1(("%s|%d" % (seed, turn)).encode("utf-8")).hexdigest()
    return pool[int(digest[:8], 16) % len(pool)]


def customer_line(persona, scenario, stage, turn, seed):
    """生成本轮客户台词。场景词只在开局与需求环节掺入，避免每句都像念背景。"""
    cls = STAGE_CLASS.get(stage, "need")
    pool = list(persona.get("reactions", {}).get(cls) or [])

    if turn == 0:
        pool = list(persona.get("opening") or []) + pool
    if turn in (1, 2) and scenario.get("flavor"):
        pool = pool + list(scenario["flavor"])
    if turn >= 3 and persona.get("escalation"):
        # 谈不拢就升级——这是真实通话里最常见的走向，也是系统最容易被带崩的地方
        pool = pool + list(persona["escalation"])

    return _pick(pool, seed, turn)


def run_one(persona, scenario, max_turns=10):
    """跑一通电话，返回完整记录。异常不抛出，记为 error 供评估统计。"""
    seed = "%s|%s" % (persona["id"], scenario["id"])
    session_id = start_call()["session_id"]
    transcript, last = [], None
    error = ""

    try:
        for turn in range(max_turns):
            stage = (last or {}).get("next_stage") or (last or {}).get("stage") or "GREETING"
            line = customer_line(persona, scenario, stage, turn, seed)
            transcript.append({"role": "user", "text": line, "turn": turn})
            last = handle_turn(session_id, user_text=line)
            transcript.append({
                "role": "agent", "text": last.get("reply_text", ""),
                "stage": last.get("stage", ""), "next": last.get("next_stage", ""),
                "blocked": bool((last.get("compliance") or {}).get("blocked")),
                "turn": turn,
            })
            if last.get("closed") or (last.get("next_stage") or "") == "DONE":
                break
    except Exception as exc:                   # noqa: BLE001
        error = "%s: %s" % (type(exc).__name__, exc)

    evs = events.list_events(session_id)
    try:
        report = eval_mod.compliance_report(session_id)
        failed = report["failed"]
        red_line_failed = [c for c in failed if c != "must_say_hit"]
    except Exception:                          # noqa: BLE001
        red_line_failed = []

    slots = (last or {}).get("slots") or {}
    closed = any(e["type"] == "writeback" for e in evs)
    end_call(session_id)

    return {
        "persona": persona["id"],
        "persona_name": persona["name"],
        "archetype": persona["archetype"],
        "scenario": scenario["id"],
        "scenario_name": scenario["name"],
        "session_id": session_id,
        "turns": sum(1 for t in transcript if t["role"] == "user"),
        "closed": closed,
        "consent": slots.get("consent"),
        "identity_confirmed": bool(slots.get("identity_confirmed")),
        "need_captured": bool(slots.get("need")),
        "red_line_failed": red_line_failed,
        "error": error,
        "error_events": sum(1 for e in evs if e["type"] == "error"),
        "transcript": transcript,
    }


def run_matrix(personas, scenarios, limit=None):
    records = []
    pairs = [(p, s) for p in personas for s in scenarios]
    if limit:
        pairs = pairs[:limit]
    for persona, scenario in pairs:
        records.append(run_one(persona, scenario))
    return records


def save(records, out_dir=RUNS_DIR):
    os.makedirs(out_dir, exist_ok=True)
    stem = "sim_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(out_dir, stem + ".json")
    n = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, "%s-%d.json" % (stem, n))
        n += 1
    payload = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(records),
        "records": records,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="文字对抗对局生成器")
    parser.add_argument("--persona", default=None, help="只跑某个角色 id")
    parser.add_argument("--scenario", default=None, help="只跑某个场景 id")
    parser.add_argument("--pairs", type=int, default=None, help="只跑前 N 组")
    parser.add_argument("--turns", type=int, default=10, help="单通最多轮次")
    parser.add_argument("--out", default=RUNS_DIR)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    personas = load_json(PERSONAS_PATH)["personas"]
    scenarios = load_json(SCENARIOS_PATH)["scenarios"]
    if args.persona:
        personas = [p for p in personas if p["id"] == args.persona]
    if args.scenario:
        scenarios = [s for s in scenarios if s["id"] == args.scenario]
    if not personas or not scenarios:
        print("没有匹配的角色或场景", file=sys.stderr)
        return 2

    pairs = [(p, sc) for p in personas for sc in scenarios]
    if args.pairs:
        pairs = pairs[:args.pairs]

    records = []
    for persona, scenario in pairs:
            rec = run_one(persona, scenario, max_turns=args.turns)
            records.append(rec)
            if not args.quiet:
                print(" %s × %s  %2d 轮  闭环=%s 违规=%s 异常=%s"
                      % (persona["id"], scenario["id"], rec["turns"],
                         "是" if rec["closed"] else "否",
                         ",".join(rec["red_line_failed"]) or "无",
                         "有" if rec["error"] else "无"))

    path = save(records, args.out)
    closed = sum(1 for r in records if r["closed"])
    bad = sum(1 for r in records if r["red_line_failed"] or r["error"])
    print("完成：%d 局｜闭环 %d｜违规或异常 %d" % (len(records), closed, bad))
    print("落盘：%s" % os.path.relpath(path, R._ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
