"""双端自对练 —— 让模型扮演客户，和我们的客服真跑一遍。

和已有两个跑批脚本的分工（三个都要留着，别互相替代）：

    run_eval.py   固定台词，验证"业务流程通不通"。改动一次跑一次，快、确定。
    sim_eval.py   规则模拟用户（受知/受听/受耐心），验证"流程在各种人身上稳不稳"。
    selfplay.py   大模型扮演客户，验证"话说到不到位" —— 本文件。

为什么还要第三个：
    sim_eval 的用户只会十八种动作、每句话是模板。它能证明流程没坏，
    但证明不了"客户这么问，客服答得上来吗"——因为客户问的永远是那几句。
    换成一个会追问、会绕、会不服、会突然扯到别处的客户，暴露的是
    完全另一类问题：答非所问、复读、把"没答上"糊成"您看这样清楚了吗"。

数据从哪来：
    `data/customer_cards.json`（132 张档案卡 + 411 条画像，由
    `modules/customer_data.py` 从申请单/客户/订单/画像编译）。
    每通电话按序号取一张卡，用卡里的申请单号 `start_call(application_id=...)`，
    保证"客户报的尾号"和"客服手上的档案"是同一份——否则测出来的失败
    全是跑批自己造的。

两种对照（这才是能回答"智能了没"的部分）：
    --compare baseline   同一批卡、同一个客户模型，再跑一遍"模板用户 + 骨架话术"。
                         两个世界的差别只有"说不说得上话"，指标才有可比性。

判分：
    逐句正负复用 `modules/feedback.py`（和自进化闭环同一套判据，不另造一套）。
    额外的两个指标是这次要看的：
        stall        相邻两轮客服回复相似度 >=0.90（原地复读）
        unanswered   客户这一轮在追问，客服这一轮没答到（判据在 feedback 里）

用法：
    python scripts/selfplay.py --calls 6                     # 6 通：LLM 客户 vs 现有客服
    python scripts/selfplay.py --calls 6 --compare baseline  # 同时跑一遍模板世界
    python scripts/selfplay.py --calls 3 --assist on         # 开客服表达增强
    python scripts/selfplay.py --calls 2 --json x.json --report x.md --quiet
"""

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _scan(argv):
    o = {"calls": 6, "scene": os.environ.get("SP_SCENE") or "pop_reject",
         "user": "llm", "assist": "auto", "cards": "", "max_turns": 14,
         "json": "", "report": "", "tag": "", "compare": "", "quiet": False,
         "nlg": "", "seed": "selfplay", "kind": "auto"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--calls", "--scene", "--user", "--assist", "--cards", "--max-turns",
                 "--json", "--report", "--tag", "--compare", "--nlg", "--seed", "--kind"):
            if i + 1 >= len(argv):
                raise SystemExit("%s 缺少参数" % a)
            key = a.lstrip("-").replace("-", "_")
            val = argv[i + 1]
            o[key] = int(val) if key in ("calls", "max_turns") else val
            i += 2
            continue
        if a == "--quiet":
            o["quiet"] = True
        i += 1
    return o


_OPTS = _scan(sys.argv[1:])

if _OPTS["scene"]:
    if str(_OPTS["scene"]).endswith(".json"):
        os.environ["APP_CONFIG"] = (str(_OPTS["scene"]) if os.path.isabs(_OPTS["scene"])
                                    else os.path.join(_ROOT, _OPTS["scene"]))
        os.environ.pop("APP_SCENE", None)
    else:
        os.environ["APP_SCENE"] = str(_OPTS["scene"])
        os.environ.pop("APP_CONFIG", None)

import events                                    # noqa: E402

_tmp = tempfile.mkdtemp(prefix="sc_selfplay_")
events.DATA_DIR = _tmp
events.DB_PATH = os.path.join(_tmp, "selfplay.db")
events.init_db()

import config_loader                             # noqa: E402
from modules import customer_data, feedback, llm, tools   # noqa: E402
from modules.user_sim import UserSim, HANGUP     # noqa: E402

config_loader.tts_config = lambda: {"provider": "browser"}   # 不合成音频


def _nlg_mode(mode):
    """把 provider/assist 切成三档，供对照实验用。

    template = 念骨架（改造前的世界）
    rewrite  = 600ms 快速改写（上一版线上）
    assist   = 快速改写 + 需要讲清楚时用本单档案组织（这一版）
    """
    base = dict((config_loader.load() or {}).get("nlg") or {})

    def cfg():
        c = dict(base)
        if mode == "template":
            c["provider"] = "template"
            c["assist"] = False
        else:
            c["provider"] = "dashscope"
            c["assist"] = (mode == "assist")
        return c
    config_loader.nlg_config = cfg


def _mode_for(opts):
    if opts.get("nlg"):
        return opts["nlg"]
    a = str(opts.get("assist") or "auto")
    if a == "on":
        return "assist"
    if a == "off":
        return "rewrite"
    return "assist" if (config_loader.load().get("nlg") or {}).get("assist") else "rewrite"


def _pick_cards(n, kind):
    # 只挑"可以外呼"的档案：京麦单与"无需外呼"单的官方口径就是不打电话，
    # 用它去跑自对练，第一轮就会 agent_end:jingmai_no_call——
    # 那不是客服不行，是这一单根本不该在这个渠道打。
    #
    # kind="auto" 时按场景判：开了 application_mode（POP 资审）用商家卡，
    # 否则（售后/召回）用客户卡。不判的话售后场景会一张卡都取不到。
    if kind in (None, "", "auto"):
        kind = "merchant" if (config_loader.load() or {}).get("application_mode") else "consumer"
    data = customer_data.load()
    cards = []
    for c in (data.get("cards") or []):
        if c.get("kind") != kind:
            continue
        case = c.get("case") or {}
        if case.get("close_mode") == "京麦" or case.get("call_status") == "无需外呼":
            continue
        cards.append(c)
    if not cards:
        return []
    out = []
    for i in range(n):
        out.append(cards[i % len(cards)])
    return out


def _sim_for(card, mode, seed):
    from modules import llm_user
    persona = customer_data.persona_by_id(
        (customer_data.load().get("links") or {}).get(card["card_id"])) or {}
    # 客户当然知道自己公司的名字、自己报的尾号。
    # 京东人物库那 320 条画像只有"性格/语气"，没有 knows / willing 两段
    # （它们原本是给"固定台词回放"用的，不需要"知不知道"）。直接拿去做规则用户，
    # `derive_facts` 会返回空字典——客户变成"什么都不记得"的人，
    # 于是一通都闭不了环。11-11 第一次跑对照实验就是这么跑出 0/6 的：
    # 那不是客服的锅，是跑批把人设配漏了。
    if not (persona.get("knows")):
        persona = dict(persona)
        persona["knows"] = {"phone_tail": True, "contact_name": True,
                            "company_name": True, "application_id": True}
    if not (persona.get("willing")):
        persona = dict(persona)
        persona["willing"] = {"phone_tail": "on_ask", "contact_name": "on_ask"}
    company = {
        "company_name": (card.get("knows") or {}).get("company_name") or "",
        "contact_name": (card.get("knows") or {}).get("contact_name") or "",
        "phone_tail": (card.get("knows") or {}).get("phone_tail") or "",
        "shop_name": (card.get("knows") or {}).get("shop_name") or "",
        "application_id": (card.get("knows") or {}).get("application_id") or "",
    }
    if mode == "rule" or not llm.available():
        return UserSim(persona, company, seed=seed), persona
    return llm_user.LLMUserSim(persona, company, card=card, seed=seed), persona


def run_call(card, user_mode="llm", max_turns=14, seed="selfplay"):
    """跑一通电话。返回会话记录（结构与 sim_eval 一致，feedback 能直接判）。"""
    from app import end_call, handle_turn, start_call

    tools._reset_for_test()
    app_id = (card.get("case") or {}).get("application_id") or None
    started = start_call(application_id=app_id)
    sid = started["session_id"]

    sim, persona = _sim_for(card, user_mode, seed="%s|%s" % (seed, card["card_id"]))
    turns, end_by = [], ""
    agent_text = started.get("opening") or ""

    for _ in range(max_turns):
        if not agent_text:
            end_by = "agent_silent"
            break
        u = sim.say(agent_text)
        if u is None:
            end_by = "user_hangup"
            break
        res = handle_turn(sid, user_text=u["text"])
        turns.append({
            "user": u,
            "agent": {
                "reply_text": res.get("reply_text", ""),
                "stage": res.get("stage", ""),
                "next_stage": res.get("next_stage", ""),
                "intent": res.get("intent", ""),
                "end_reason": res.get("end_reason", ""),
                "kb": res.get("kb"), "qa": res.get("qa"),
            },
        })
        if u.get("action") == HANGUP:
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
        "id": card["card_id"],
        "card_id": card["card_id"],
        "label": (persona.get("label") or persona.get("profession")
                  or persona.get("id") or ""),
        "persona_id": persona.get("id", ""),
        "goal": persona.get("goal", ""),
        "split": persona.get("split", "train"),
        "company": {k: (started.get("company") or {}).get(k) for k in
                    ("company_name", "contact_role", "phone_tail", "application_id")},
        "turns": turns,
        "closed": closed,
        "end_by": end_by,
        "final_stage": turns[-1]["agent"]["next_stage"] if turns else "",
        "sim_source": "llm" if hasattr(sim, "report") else "rule",
        "sim_report": sim.report() if hasattr(sim, "report") else {},
    }


def _metrics(sess):
    """一通电话的折算指标。"""
    turns = sess.get("turns") or []
    stalls = 0
    for i in range(1, len(turns)):
        a = (turns[i - 1].get("agent") or {}).get("reply_text") or ""
        b = (turns[i].get("agent") or {}).get("reply_text") or ""
        if a and b and feedback._similar(a, b):
            stalls += 1
    from modules import feedback as fb
    judged = [fb.judge_turn(turns, i) for i in range(len(turns))]
    neg = [j for j in judged if j["polarity"] == "-"]
    unanswered = [j for j in neg if j["action"] in fb.ASK_ACTIONS]

    # 客户"说了多少种话"：把标点去掉后去重。这个数直接回答"用户智能了没"——
    # 模板客户只有十八句来回换，这个比值会很低；模型客户每句都不一样。
    user_texts = [((t.get("user") or {}).get("text") or "").strip() for t in turns]
    norm = [feedback._PUNCT.sub("", t) for t in user_texts if t]
    uniq = len(set(norm))

    # 客户自评"这一问我被回答了没有"。**只统计他真在问的那几轮**：
    # 客户报尾号、应一声的时候也算进去，会把"没获答率"稀释成一堆无意义的样本
    # （第一版 56 个样本里有一半是他根本没提问的轮次）。
    from modules import feedback as fb2
    asked_flags = []
    for t in turns:
        u = t.get("user") or {}
        g = u.get("got_answer")
        if g is None:
            continue
        is_ask = (u.get("action") in fb2.ASK_ACTIONS
                  or "？" in (u.get("text") or "") or "?" in (u.get("text") or ""))
        if is_ask:
            asked_flags.append(bool(g))

    return {
        "turns": len(turns),
        "closed": bool(sess.get("closed")),
        "end_by": sess.get("end_by"),
        "neg": len(neg),
        "pos": sum(1 for j in judged if j["polarity"] == "+"),
        "stall": stalls,
        "unanswered": len(unanswered),
        "codes": _count([c for j in neg for c in j["codes"]]),
        "uniq_user": uniq,
        "user_turns": len(norm),
        "avg_user_len": round(sum(len(t) for t in user_texts) / float(len(user_texts)), 1)
                        if user_texts else 0.0,
        "felt_unanswered": sum(1 for g in asked_flags if not g),
        "asked": len(asked_flags),
        "llm_calls": (sess.get("sim_report") or {}).get("llm_calls", 0),
        "llm_ok": (sess.get("sim_report") or {}).get("llm_ok", 0),
        "vetoes": len((sess.get("sim_report") or {}).get("vetoes") or []),
    }


def _count(items):
    out = {}
    for k in items:
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def run_batch(cards, user_mode, max_turns, seed, label):
    sessions = []
    t0 = time.time()
    for idx, card in enumerate(cards):
        if not _OPTS["quiet"]:
            sys.stderr.write("[%s] %d/%d %s\n" % (label, idx + 1, len(cards), card["card_id"]))
        sessions.append(run_call(card, user_mode=user_mode, max_turns=max_turns, seed=seed))
    ms = int((time.time() - t0) * 1000)
    rows = [_metrics(s) for s in sessions]
    agg = {
        "calls": len(rows),
        "closed": sum(1 for r in rows if r["closed"]),
        "turns": sum(r["turns"] for r in rows),
        "neg": sum(r["neg"] for r in rows),
        "pos": sum(r["pos"] for r in rows),
        "stall": sum(r["stall"] for r in rows),
        "unanswered": sum(r["unanswered"] for r in rows),
        "codes": _count([c for r in rows for c in r["codes"]]),
        "llm_calls": sum(r["llm_calls"] for r in rows),
        "llm_ok": sum(r["llm_ok"] for r in rows),
        "uniq_user": sum(r["uniq_user"] for r in rows),
        "user_turns": sum(r["user_turns"] for r in rows),
        "felt_unanswered": sum(r["felt_unanswered"] for r in rows),
        "asked": sum(r["asked"] for r in rows),
        "vetoes": sum(r["vetoes"] for r in rows),
        "ms": ms,
    }
    agg["close_rate"] = round(agg["closed"] / float(agg["calls"]), 3) if agg["calls"] else 0.0
    agg["neg_rate"] = round(agg["neg"] / float(agg["turns"]), 3) if agg["turns"] else 0.0
    agg["avg_turns"] = round(agg["turns"] / float(agg["calls"]), 2) if agg["calls"] else 0.0
    agg["uniq_ratio"] = (round(agg["uniq_user"] / float(agg["user_turns"]), 2)
                         if agg["user_turns"] else 0.0)
    agg["avg_user_len"] = (round(sum(r["avg_user_len"] * r["user_turns"] for r in rows)
                                 / float(agg["user_turns"]), 1)
                           if agg["user_turns"] else 0.0)
    return {"label": label, "user": user_mode, "agg": agg, "rows": rows, "sessions": sessions}


def _fmt_pct(x):
    return "%.0f%%" % (x * 100)


def _render_md(payload):
    L = []
    main = payload["runs"][0]
    a = main["agg"]
    L += ["# 双端自对练：%s" % payload["scene"], "",
          "> 生成：%s｜客户模型：%s｜客服表达层：%s｜模型：%s"
          % (payload["generated_at"], payload["user_mode"], payload["nlg_mode"],
             payload["llm_model"]),
          "> 客户 = 大模型按档案扮演（%d 张卡 / %d 条画像），客服 = 线上真实链路"
          "（状态机 + 话术层），不是另写一个假客服。" % (payload["cards"], payload["personas"]),
          "",
          "## 总览", "",
          "| 指标 | 值 |", "|---|---|",
          "| 通话数 | %d |" % a["calls"],
          "| 闭环 | %d/%d（%s） |" % (a["closed"], a["calls"], _fmt_pct(a["close_rate"])),
          "| 平均轮数 | %.2f |" % a["avg_turns"],
          "| 客服负面句 | %d / %d 轮（%s） |" % (a["neg"], a["turns"], _fmt_pct(a["neg_rate"])),
          "| 正面句 | %d |" % a["pos"],
          "| 原地复读 | %d 次 |" % a["stall"],
          "| 客户追问未被回答 | %d 次 |" % a["unanswered"],
          "| 客户句子去重率 | %s（%d/%d 句不同） |"
          % (_fmt_pct(a.get("uniq_ratio", 0)), a.get("uniq_user", 0), a.get("user_turns", 0)),
          "| 客户平均句长 | %.1f 字 |" % a.get("avg_user_len", 0),
          "| 客户自评「这问没被回答」（参考） | %d / %d 次 |"
          % (a.get("felt_unanswered", 0), a.get("asked", 0)),
          "| 客户越界被拦 | %d 次 |" % a.get("vetoes", 0),
          "| 客户侧模型调用 | %d 次（成功 %d） |" % (a["llm_calls"], a["llm_ok"]),
          "| 总耗时 | %.1fs |" % (a["ms"] / 1000.0),
          ""]

    if len(payload["runs"]) > 1:
        L += ["## 对照：模板世界 vs 智能世界", "",
              "同一批档案卡、同一批画像，只换「客户会不会说话」和「客服怎么组织语言」。", "",
              "| 指标 | " + " | ".join(r["label"] for r in payload["runs"]) + " |",
              "|---" * (len(payload["runs"]) + 1) + "|"]
        for key, name in (("closed", "闭环"), ("avg_turns", "平均轮数"),
                          ("neg", "客服负面句"), ("neg_rate", "负面率"),
                          ("stall", "原地复读"), ("unanswered", "追问未答"),
                          ("uniq_ratio", "客户句子去重率"),
                          ("avg_user_len", "客户平均句长"),
                          ("felt_unanswered", "客户自评未获答（参考）")):
            cells = []
            for r in payload["runs"]:
                v = r["agg"].get(key, "—")
                cells.append(_fmt_pct(v) if key in ("neg_rate", "uniq_ratio") else str(v))
            L.append("| %s | %s |" % (name, " | ".join(cells)))
        L.append("")

    L += ["## 缺陷码", ""]
    if not a["codes"]:
        L.append("无。")
    else:
        L += ["| 缺陷码 | 次数 |", "|---|---|"]
        for k, v in a["codes"].items():
            L.append("| `%s` | %d |" % (k, v))
    L.append("")

    L += ["## 逐通实录", ""]
    for sess, m in zip(main["sessions"], main["rows"]):
        L.append("### %s（%s）｜客户端：%s｜%s｜%d 轮"
                 % (sess["id"], sess["label"], sess["sim_source"],
                    "闭环" if sess["closed"] else "未闭环（%s）" % sess["end_by"],
                    m["turns"]))
        L.append("")
        L.append("档案：%s｜联系人：%s｜尾号：%s"
                 % (sess["company"].get("company_name"),
                    sess["company"].get("contact_role"),
                    sess["company"].get("phone_tail")))
        L.append("")
        for i, t in enumerate(sess["turns"], 1):
            u, ag = t["user"], t["agent"]
            tag = []
            if u.get("source"):
                tag.append(u["source"])
            if u.get("veto"):
                tag.append("拦:%s" % u["veto"])
            if u.get("feel"):
                tag.append(u["feel"])
            print_extra = ("〔%s〕" % "，".join(tag)) if tag else ""
            L.append("%d. **客户**（%s）%s%s" % (i, u.get("action", ""), u.get("text", ""),
                                               print_extra))
            hit = []
            if ag.get("kb"):
                hit.append("KB:%s" % ag["kb"].get("id"))
            if ag.get("qa"):
                hit.append("QA:%s" % ag["qa"].get("id"))
            L.append("   **客服**（%s→%s/%s）%s%s"
                     % (ag.get("stage"), ag.get("next_stage"), ag.get("intent"),
                        ag.get("reply_text", ""),
                        ("　" + " ".join(hit)) if hit else ""))
        L.append("")

    L += ["## 客户说了什么（动作分布）", ""]
    acts = {}
    for sess in main["sessions"]:
        for t in sess["turns"]:
            k = (t["user"] or {}).get("action") or "?"
            acts[k] = acts.get(k, 0) + 1
    L += ["| 动作 | 次数 | 含义 |", "|---|---|---|"]
    from modules.llm_user import _ACTION_CN
    for k, v in sorted(acts.items(), key=lambda kv: -kv[1]):
        L.append("| `%s` | %d | %s |" % (k, v, _ACTION_CN.get(k, "")))
    L.append("")
    return "\n".join(L) + "\n"


def main():
    nlg_mode = _mode_for(_OPTS)
    _nlg_mode(nlg_mode)

    cards = _pick_cards(_OPTS["calls"], _OPTS["kind"])
    if not cards:
        print("没有可用的档案卡，请先运行 python -m modules.customer_data")
        return 1
    if _OPTS["cards"]:
        idx = [int(x) for x in str(_OPTS["cards"]).replace("，", ",").split(",") if x.strip()]
        cards = [_pick_cards(max(idx) + 1, _OPTS["kind"])[i] for i in idx]

    runs = []
    if _OPTS["compare"] == "baseline":
        _nlg_mode("template")
        runs.append(run_batch(cards, "rule", _OPTS["max_turns"], _OPTS["seed"],
                              "模板客户 + 骨架话术"))
        _nlg_mode(nlg_mode)

    runs.insert(0, run_batch(cards, _OPTS["user"], _OPTS["max_turns"], _OPTS["seed"],
                             "LLM 客户 + %s" % ("表达增强" if nlg_mode == "assist"
                                                else "快速改写" if nlg_mode == "rewrite"
                                                else "骨架话术")))

    payload = {
        "tag": _OPTS["tag"] or datetime.now().strftime("%m%d_%H%M"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scene": config_loader.scene_name(),
        "scene_path": config_loader.scene_path(),
        "user_mode": _OPTS["user"],
        "nlg_mode": nlg_mode,
        "cards": len(cards),
        "personas": (customer_data.load().get("_meta") or {}).get("stats", {}).get("personas", 0),
        "llm_model": llm.stats().get("model"),
        "llm_stats": llm.stats(),
        "runs": runs,
    }

    # 终端摘要
    print("=" * 78)
    print("双端自对练（场景：%s，客户=%s，客服表达层=%s）"
          % (payload["scene"], _OPTS["user"], nlg_mode))
    print("=" * 78)
    for r in runs:
        a = r["agg"]
        print("  %-24s 闭环 %d/%d  平均 %.2f 轮  负面 %d/%d  复读 %d  追问未答 %d"
              % (r["label"], a["closed"], a["calls"], a["avg_turns"],
                 a["neg"], a["turns"], a["stall"], a["unanswered"]))
    print("  缺陷码：%s" % json.dumps(runs[0]["agg"]["codes"], ensure_ascii=False))
    print("  LLM 调用：%d 次（成功 %d）｜%s"
          % (runs[0]["agg"]["llm_calls"], runs[0]["agg"]["llm_ok"],
             json.dumps({k: v for k, v in llm.stats().items() if k != "recent"},
                        ensure_ascii=False)))

    if _OPTS["json"]:
        p = _OPTS["json"] if os.path.isabs(_OPTS["json"]) else os.path.join(_ROOT, _OPTS["json"])
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("JSON 已写入：%s" % os.path.relpath(p, _ROOT))
    if _OPTS["report"]:
        p = _OPTS["report"] if os.path.isabs(_OPTS["report"]) else os.path.join(_ROOT, _OPTS["report"])
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(_render_md(payload))
        print("报告已写入：%s" % os.path.relpath(p, _ROOT))

    shutil.rmtree(_tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
