#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录制：让模型客服跑几通，把它说过的每一句存进 mock。

## 为什么要有这个脚本

`modules/agent_free` 打开后，客服说的话由模型现场组织——好听，但**不可复现**：
同一通电话跑两遍，话不一样，闸门今天绿明天红。所以模型不能只活在线上，
它说过的话要能落成数据：

    eval/fixtures/agent_free/<画像>.json   这一通的逐轮实录（客户话 + 客服话）
    config/scenes/<场景>_lines.json        按环节汇总的话术库（给回放/自查用）

以后 `AGENT_FREE=replay` 就按这两份数据出话：不调模型、可逐字节复现，
而说的仍然是模型组织过的人话——不是骨架。

## 跑法

    python scripts/record_agent.py --n 3            # 前 3 个画像，客户也用模型
    python scripts/record_agent.py --n 6 --user rule  # 客户走规则档（更快、更可控）

## 边界

- 不写生产事件库：`_train_run` 自己切临时目录，录完就删。
- 不改配置：开关只在本进程内生效，跑完不留痕。
- 录制结果**不覆盖**已有文件里的其它画像，只按画像 id 增量写。
"""

import argparse
import glob
import io
import json
import os
import sys
import time

# 录制就是要开模型。这里必须**显式写 1**，不能只 pop：
# `scripts/adversarial_evolve.py` 顶部有 `setdefault("AGENT_FREE", "0")`（跑批隔离），
# 而录制脚本 import app 时会顺带 import 它——pop 掉之后那行 setdefault 立刻
# 又把它设成 0，表现是"录了三通，模型说 0 轮"（第一次就是这么翻车的）。
os.environ["AGENT_FREE"] = "1"
os.environ.setdefault("APP_SCENE", "pop_reject")
os.environ.setdefault("APP_CONFIG", "")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FIX_DIR = os.path.join(ROOT, "eval", "fixtures", "agent_free")


def _personas(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f).get("personas") or []


def _agent_source(turn):
    """这一轮的话是模型说的还是骨架说的。"""
    agent = turn.get("agent") or {}
    if agent.get("reply_source"):
        return agent["reply_source"]
    return "unknown"


def record(persona, user="llm", max_turns=14):
    from app import _train_run
    out = _train_run(persona, user=user, max_turns=max_turns)
    rows = []
    for i, t in enumerate(out.get("turns") or [], 1):
        u = t.get("user") or {}
        a = t.get("agent") or {}
        rows.append({
            "turn": i,
            "stage": a.get("stage"),
            "next_stage": a.get("next_stage"),
            "intent": a.get("intent"),
            "user": u.get("text") or "",
            "agent": a.get("reply_text") or "",
            "source": _agent_source(t),
            "end_reason": a.get("end_reason") or "",
        })
    return {
        "persona_id": persona.get("id") or "",
        "persona_label": persona.get("label") or "",
        "application_id": out.get("card_id") or "",
        "user_mode": out.get("user_mode") or user,
        "model": out.get("llm_model") or "",
        "verdict": out.get("verdict") or "",
        "closed": bool(out.get("closed")),
        "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "turns": rows,
    }


def main():
    ap = argparse.ArgumentParser(description="录制模型客服的逐轮话术")
    ap.add_argument("--n", type=int, default=3, help="录前几个画像（默认 3）")
    ap.add_argument("--user", default="llm", choices=("llm", "rule"),
                    help="客户侧走模型还是规则（默认 llm）")
    ap.add_argument("--max-turns", type=int, default=14)
    ap.add_argument("--personas", default=os.path.join(
        ROOT, "eval", "personas_adversarial.json"))
    args = ap.parse_args()

    os.makedirs(FIX_DIR, exist_ok=True)
    picked = _personas(args.personas)[:max(1, args.n)]
    if not picked:
        print("没有可用画像：%s" % args.personas)
        return 1

    saved, lines = [], []
    for p in picked:
        pid = p.get("id") or "unknown"
        try:
            rec = record(p, user=args.user, max_turns=args.max_turns)
        except Exception as exc:
            print("  %-4s 失败：%s" % (pid, str(exc)[:90]))
            continue
        path = os.path.join(FIX_DIR, "%s.json" % pid)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
            f.write("\n")
        n_model = sum(1 for r in rec["turns"] if r["source"] == "model")
        print("  %-4s %2d 轮（模型说 %2d 轮）%s → %s"
              % (pid, len(rec["turns"]), n_model, rec["verdict"], path))
        saved.append(path)
        for r in rec["turns"]:
            if r["agent"]:
                lines.append({"stage": r["stage"], "user": r["user"],
                              "reply": r["agent"], "persona": pid})

    # 汇总话术库：按环节分组，回放与自查都从这份读。
    #
    # 放 config/mock/ 而不是 config/scenes/——`config/scenes/` 下每个 json
    # 都会被当成**场景包**校验（缺 scene_name/stages 直接判红），
    # 话术库不是场景包，放进去会让"主场景与每个场景包都通过校验"那条挂掉。
    # 第一次就放在 scenes/ 下，verify 当场 248/251。
    scene = os.environ.get("APP_SCENE") or "pop_reject"
    lib_path = os.path.join(ROOT, "config", "mock", "agent_lines.json")
    by_stage = {}
    for row in lines:
        by_stage.setdefault(row["stage"] or "UNKNOWN", []).append(
            {"user": row["user"], "reply": row["reply"], "persona": row["persona"]})
    payload = {
        "_comment": "模型客服说过的话，按环节汇总。由 scripts/record_agent.py 生成，"
                    "不要手改——重跑脚本会覆盖。用途：AGENT_FREE=replay 时按环节检索，"
                    "以及回答'这句人话是哪次跑的'。",
        "scene": scene,
        "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "personas": [os.path.basename(p)[:-5] for p in saved],
        "count": len(lines),
        "by_stage": by_stage,
    }
    with io.open(lib_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")

    print()
    print("录了 %d 通、%d 句 → %s" % (len(saved), len(lines), lib_path))
    print("逐轮实录：%s" % FIX_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
