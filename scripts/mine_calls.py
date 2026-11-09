#!/usr/bin/env python3
"""从**真实用户通话**里挖缺陷，而不是只靠模拟客户（11-09）。

三个命令：
    mine     扫 data/events.db 里的真实通话 → 缺陷归因 → 聚类 → 报告
    compare  按时间点切开真实通话，对比改前/改后的缺陷率（上线后验证）
    check    反向自测：造带缺陷的通话必须抓到、干净通话必须不报

为什么要有它：模拟集（10 通基线 / 12 性格 / 16 对抗）能覆盖已知套路，但真人的
说话方式、沉默、ASR 识别偏差、真实阶段跳转是模拟器造不出来的。真实通话里
暴露的问题按 support（涉及会话数）排序，才谈得上「按业务价值决定改什么」。

一条纪律：真实数据里的话术可能来自**旧版本**。报告对每条缺陷给
`in_current_config`（这句客服原话是否还存在于当前场景配置）——为 False 的只统计、
不提案；为 True 的才是现在就还在犯的错。

跑法：
    python scripts/mine_calls.py mine
    python scripts/mine_calls.py mine --days 7 --json eval/reports/real_calls.json
    python scripts/mine_calls.py compare --since 2026-11-09T00:00:00
    python scripts/mine_calls.py check
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import config_loader  # noqa: E402
from modules import real_audit as audit  # noqa: E402

DB_PATH = os.path.join(_ROOT, "data", "events.db")
REPORT_DIR = os.path.join(_ROOT, "eval", "reports")


# ---------------------------------------------------------------- 读真实通话
def _load_turns(conn, session_id):
    """把一通电话的事件还原成逐轮结构。只认四类事件，其余（音频/时延）与分析无关。"""
    rows = conn.execute(
        "SELECT turn_id, type, payload, ts FROM events WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    by_turn = {}
    closed = False
    started = 0
    for turn_id, etype, payload, ts in rows:
        try:
            p = json.loads(payload or "{}")
        except Exception:
            p = {}
        if not started:
            started = ts
        t = by_turn.setdefault(turn_id, {"i": turn_id, "user": {"text": "", "kind": "opening"},
                                         "agent": {}})
        if etype == "asr_result":
            t["user"] = {"text": p.get("text") or "", "kind": "voice"}
        elif etype == "no_answer":
            t["user"] = {"text": "", "kind": "silence"}
        elif etype == "agent_reply":
            t["agent"] = {"text": p.get("text") or "", "stage": p.get("stage") or "",
                          "next_stage": p.get("next_stage") or "",
                          "intent": p.get("intent") or ""}
        elif etype == "turn_end":
            closed = closed or bool(p.get("closed"))
    turns = [t for _, t in sorted(by_turn.items()) if t["agent"].get("text")]
    for t in turns:
        t["closed"] = closed
    return turns, started


def load_sessions(limit=0, days=0, db=DB_PATH):
    """读真实会话：{sid: {turns, ts, closed}}。没有任何客服回复的会话跳过。"""
    if not os.path.exists(db):
        return {}, db
    conn = sqlite3.connect(db)
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT session_id FROM events GROUP BY session_id ORDER BY MIN(ts) DESC")]
        if days:
            since = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
            ids = [r[0] for r in conn.execute(
                "SELECT session_id FROM events GROUP BY session_id"
                " HAVING MIN(ts) >= ? ORDER BY MIN(ts) DESC", (since,))]
        if limit:
            ids = ids[:int(limit)]
        out = {}
        for sid in ids:
            turns, ts = _load_turns(conn, sid)
            if not turns:
                continue
            out[sid] = {"turns": turns, "ts": ts,
                        "closed": bool(turns[-1].get("closed"))}
        return out, db
    finally:
        conn.close()


# ---------------------------------------------------------------- 当前配置判定
def _scene_text(cache={}):
    if "text" not in cache:
        try:
            with open(config_loader.scene_path(), encoding="utf-8") as f:
                cache["text"] = f.read()
        except Exception:
            cache["text"] = ""
    return cache["text"]


def _in_current_config(agent_text):
    """这句客服原话是否还存在于当前场景配置里。

    为什么这样判而不是按版本号：事件库的 version 是代码版本常量（全部同一个值），
    分不出哪通是旧话术、哪通是新话术。改用「原话是否还在配置里」这个可直接验证的
    事实——在，就是现在还会说；不在，就是历史遗留。
    """
    needle = (agent_text or "").strip().strip("。！？，")
    if len(needle) < 8:
        return False
    scene = _scene_text()
    return bool(scene) and needle[:24] in scene


# ---------------------------------------------------------------- 主流程
def analyse_sessions(sessions):
    rows = {}
    for sid, s in sessions.items():
        r = audit.scan_session(s["turns"])
        r["ts"] = s["ts"]
        r["closed"] = s["closed"]
        rows[sid] = r
    return rows


def summarise(rows):
    """按缺陷码汇总：会话数、占比、当前配置是否仍有、样例。"""
    total = len(rows)
    by_code = {}
    for sid, r in rows.items():
        seen, cur_seen = set(), set()
        for d in r["defects"]:
            code = d["code"]
            g = by_code.setdefault(code, {"code": code, "label": d["label"],
                                          "sessions": 0, "current": 0,
                                          "turns": 0, "samples": []})
            g["turns"] += 1
            if code not in seen:
                g["sessions"] += 1
                seen.add(code)
            # 同一会话同类缺陷只算一次；只要有一句原话还在配置里就算「仍会复现」
            if code not in cur_seen and _in_current_config(d.get("raw")):
                g["current"] += 1
                cur_seen.add(code)
            if len(g["samples"]) < 2:
                g["samples"].append({"session": sid, "turn": d["turn"],
                                     "evidence": d["evidence"]})
    out = []
    for g in by_code.values():
        g["rate"] = round(g["sessions"] / float(total), 4) if total else 0.0
        g["in_current_config"] = g["current"] > 0
        out.append(g)
    out.sort(key=lambda x: (-x["sessions"], x["code"]))
    return out


def mine(args):
    sessions, db = load_sessions(limit=args.limit, days=args.days, db=args.db)
    if not sessions:
        print("真实通话音：%s 里没有可用会话（没有客服回复的会话已跳过）" % db)
        return 1
    rows = analyse_sessions(sessions)
    codes = summarise(rows)

    # --verify：对「原话还在当前配置里」的缺陷，用当前代码重放同一批真实输入。
    # 只靠「原话还在配置里」判「仍会复现」会冤枉：句子留着但触发条件已经改了
    # （比如沉默那句还在配置里，但计数口径改了，不会再逐字重复）。
    if args.verify:
        import tempfile
        import shutil
        import events
        todo_codes = [c["code"] for c in codes if c["in_current_config"]]
        todo = [sid for sid, r in rows.items()
                if any(d["code"] in todo_codes for d in r["defects"])][:args.verify_n]
        if todo:
            tmp = tempfile.mkdtemp(prefix="sc_verify_")
            old_dir, old_db = events.DATA_DIR, events.DB_PATH
            events.DATA_DIR = tmp
            events.DB_PATH = os.path.join(tmp, "verify.db")
            events.init_db()
            cleared = {}
            try:
                for sid in todo:
                    before_codes = {d["code"] for d in rows[sid]["defects"]}
                    new_turns, _ = _replay_session(sessions[sid])
                    after_codes = {d["code"]
                                   for d in audit.scan_session(new_turns)["defects"]}
                    for code in before_codes & set(todo_codes):
                        cleared.setdefault(code, []).append(code not in after_codes)
            finally:
                events.DATA_DIR, events.DB_PATH = old_dir, old_db
                events.init_db()
                shutil.rmtree(tmp, ignore_errors=True)
            for c in codes:
                if c["code"] in cleared:
                    c["replay_clear"] = all(cleared[c["code"]])
                    c["replay_sessions"] = len(cleared[c["code"]])
    turns = sum(r["metrics"]["turns"] for r in rows.values())
    closed = sum(1 for r in rows.values() if r["metrics"]["closed"])
    stuck = sum(1 for r in rows.values() if r["metrics"]["max_stage"] in ("", "GREETING", "IDENTITY"))

    print("真实通话体检｜%d 通 / %d 轮｜回写 %d 通｜卡在核身 %d 通"
          % (len(rows), turns, closed, stuck))
    if not codes:
        print("  没有检出缺陷（判据见 modules/real_audit.py 顶部）")
    else:
        print("  %-24s %6s %7s %8s  %s" % ("缺陷码", "会话", "占比", "仍会复现", "样例"))
        for c in codes:
            sample = (c["samples"][0]["evidence"] if c["samples"] else "")[:46]
            if not c["in_current_config"]:
                state = "否（已修）"
            elif c.get("replay_clear"):
                state = "重放已消"
            else:
                state = "是"
            print("  %-24s %6d %6.1f%% %8s  %s"
                  % (c["code"], c["sessions"], c["rate"] * 100, state, sample))

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db": os.path.relpath(db, _ROOT).replace("\\", "/"),
        "sessions": len(rows), "turns": turns,
        "closed": closed, "stuck_at_identity": stuck,
        "by_code": codes,
        # 落盘前去掉 raw（原文只用于「是否仍在配置里」比对，不随报告流出）
        "per_session": {sid: {"defects": r["defects"], "metrics": r["metrics"]}
                        for sid, r in audit.strip_raw(rows).items() if r["defects"]},
    }
    out = args.json
    if out:
        if not os.path.isabs(out):
            out = os.path.join(_ROOT, out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("报告：%s" % os.path.relpath(out, _ROOT))
    return 0


def compare(args):
    """按时间点切开，对比改前/改后。用在候选上线之后——真实流量的验证器。"""
    sessions, _ = load_sessions(limit=args.limit, days=args.days, db=args.db)
    if not sessions:
        print("没有可用会话")
        return 1
    ts = datetime.fromisoformat(args.since).timestamp() * 1000
    before = {k: v for k, v in sessions.items() if v["ts"] < ts}
    after = {k: v for k, v in sessions.items() if v["ts"] >= ts}
    if not before or not after:
        print("切分后一侧为空：改前 %d 通、改后 %d 通。换一个 --since 试试。"
              % (len(before), len(after)))
        return 1
    rb, ra = analyse_sessions(before), analyse_sessions(after)

    def rate(rows, code):
        n = sum(1 for r in rows.values() if any(d["code"] == code for d in r["defects"]))
        return n, len(rows), (n / float(len(rows)) if rows else 0.0)

    all_codes = sorted({d["code"] for r in list(rb.values()) + list(ra.values())
                        for d in r["defects"]})
    print("改前 %d 通 ｜ 改后 %d 通（分界 %s）" % (len(rb), len(ra), args.since))
    print("  %-24s %-14s %-14s %s" % ("缺陷码", "改前", "改后", "变化"))
    for code in all_codes:
        nb, tb, pb = rate(rb, code)
        na, ta, pa = rate(ra, code)
        delta = (pa - pb) * 100
        print("  %-24s %3d/%3d %5.1f%%  %3d/%3d %5.1f%%  %+.1f 个百分点"
              % (code, nb, tb, pb * 100, na, ta, pa * 100, delta))
    return 0


def _replay_session(s):
    """把一通真实通话的用户话术喂给当前代码，返回（重放轮次, 提示）。

    单独抽出来是因为两个命令都要用：replay（给人看前后对比）与
    mine --verify（给报告标注「这句现在还会不会犯」）。
    """
    import events
    from app import start_call, handle_turn, end_call

    started = start_call()
    new_turns, notes = [], []
    try:
        for t in s["turns"]:
            utext = (t.get("user") or {}).get("text") or ""
            if (t.get("user") or {}).get("kind") == "opening":
                # 第 0 轮是客服开场白，没有用户输入：不喂给对话，否则会被当成沉默
                continue
            if "****" in utext:
                notes.append("第 %s 轮含被脱敏的数字，身份无法还原" % t.get("i"))
            res = handle_turn(started["session_id"], user_text=utext)
            new_turns.append({
                "i": t.get("i"), "user": {"text": utext, "kind": "voice"},
                "agent": {"text": res.get("reply_text", ""),
                          "stage": res.get("stage", ""),
                          "next_stage": res.get("next_stage", ""),
                          "intent": res.get("intent", "")},
                "closed": bool(res.get("closed")),
            })
    finally:
        end_call(started["session_id"])
    return new_turns, notes


def replay(args):
    """把真实用户说过的话原样喂给**当前代码**，看同一批输入下缺陷是否消失。

    这是「从真实对话提升」的验收器：改完话术或逻辑，不用等新通话，
    直接拿历史真实输入重跑一遍，对比同一批输入的缺陷条数。

    一个诚实的限制：入库时 ≥4 位数字已打码（隐私），身份核验那几轮重放不出来，
    所以调用方会看到「卡在核身」——那不是回归，是数据被脱敏了。本命令只对
    「指代含糊 / 复读 / 答非所问」这类不依赖身份的缺陷做前后对比。
    """
    import tempfile
    import shutil
    import events

    sessions, _ = load_sessions(limit=args.limit, days=args.days, db=args.db)
    if not sessions:
        print("没有可用会话")
        return 1
    ids = []
    if args.sessions:
        ids = [s.strip() for s in args.sessions.split(",") if s.strip()]
    elif args.code:
        rep_path = os.path.join(REPORT_DIR, "real_calls.json")
        if not os.path.exists(rep_path):
            print("先跑 mine 生成报告，再按 --code 选会话")
            return 1
        rep = json.load(open(rep_path, encoding="utf-8"))
        ids = [sid for sid, v in (rep.get("per_session") or {}).items()
               if any(d["code"] == args.code for d in v.get("defects") or [])]
    if not ids:
        print("没有选中会话：用 --sessions 或 --code")
        return 1
    pick = [(sid, sessions[sid]) for sid in ids if sid in sessions][:args.max_sessions]

    tmp = tempfile.mkdtemp(prefix="sc_replay_")
    old_dir, old_db = events.DATA_DIR, events.DB_PATH
    events.DATA_DIR = tmp
    events.DB_PATH = os.path.join(tmp, "replay.db")
    events.init_db()
    try:
        for sid, s in pick:
            before = audit.scan_session(s["turns"])
            new_turns, notes = _replay_session(s)
            after = audit.scan_session(new_turns)
            b_codes = sorted({d["code"] for d in before["defects"]})
            a_codes = sorted({d["code"] for d in after["defects"]})
            print("\n==== %s｜改前 %d 条 %s → 重放 %d 条 %s"
                  % (sid, len(before["defects"]), b_codes,
                     len(after["defects"]), a_codes))
            for note in notes[:2]:
                print("  注意：%s" % note)
            # 按「有过用户输入」对齐：开场白已经被跳过，不能直接 zip 原轨迹
            pairs = list(zip([t for t in s["turns"]
                              if (t.get("user") or {}).get("kind") != "opening"],
                             new_turns))
            for bt, at in pairs[:args.turns]:
                print("  用户：%s" % (((bt.get("user") or {}).get("text") or "（沉默）")[:32]))
                print("    改前：%s" % ((bt.get("agent") or {}).get("text") or "")[:56])
                print("    重放：%s" % ((at.get("agent") or {}).get("text") or "")[:56])
    finally:
        events.DATA_DIR, events.DB_PATH = old_dir, old_db
        events.init_db()
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


# ---------------------------------------------------------------- 反向自测
def _turn(i, utext, ktext, stage="IDENTITY", intent="", kind=None, closed=False):
    return {"i": i, "user": {"text": utext, "kind": kind or ("voice" if utext else "silence")},
            "agent": {"text": ktext, "stage": stage, "next_stage": stage,
                      "intent": intent}, "closed": closed}


def check(_args=None):
    checks = []

    def one(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    # 1) 沉默复读：两次沉默同一句 → 必须抓到
    turns = [_turn(0, "", "您好，请问是某某公司的经办人吗？", "GREETING"),
             _turn(1, "", "您还在吗？我这边听不到声音，您方便的时候说一声就行。"),
             _turn(2, "", "您还在吗？我这边听不到声音，您方便的时候说一声就行。")]
    r = audit.scan_session(turns)
    one("沉默两次同一句 → 抓 real_silence_repeat",
        any(d["code"] == "real_silence_repeat" for d in r["defects"]),
        str([d["code"] for d in r["defects"]]))

    # 2) 沉默但换说法 → 不许报
    turns = [_turn(1, "", "您还在吗？我这边听不到声音。"),
             _turn(2, "", "我这边还是听不到，您方便时喂一声。")]
    r = audit.scan_session(turns)
    one("沉默但换了说法 → 不报",
        not any(d["code"] == "real_silence_repeat" for d in r["defects"]),
        str([d["code"] for d in r["defects"]]))

    # 3) 指代含糊：只说「后四位」没说哪个号 → 报
    r = audit.scan_session([_turn(1, "是的", "麻烦您说一下店铺或公司信息后四位。")])
    one("只说「后四位」→ 报 real_vague_ref",
        any(d["code"] == "real_vague_ref" for d in r["defects"]),
        str([d["code"] for d in r["defects"]]))

    # 4) 反向：说清「预留手机号后四位」→ 不报
    r = audit.scan_session([_turn(1, "是的", "麻烦说下申请时预留手机号的后四位，四个数字。")])
    one("说清预留手机号 → 不报 real_vague_ref",
        not any(d["code"] == "real_vague_ref" for d in r["defects"]),
        str([d["code"] for d in r["defects"]]))

    # 5) 用银行卡当凭证 → 报 wrong_credential
    r = audit.scan_session([_turn(1, "在", "方便说一下银行卡后四位吗？")])
    one("银行卡后四位 → 报 real_wrong_credential",
        any(d["code"] == "real_wrong_credential" for d in r["defects"]),
        str([d["code"] for d in r["defects"]]))

    # 6) 已给数字又要一遍 → 报 slot_loss
    r = audit.scan_session([_turn(1, "尾号是****", "好的，麻烦再说一下尾号后四位。")])
    one("用户已报 **** 还要尾号 → 报 real_slot_loss",
        any(d["code"] == "real_slot_loss" for d in r["defects"]),
        str([d["code"] for d in r["defects"]]))

    # 7) 问「怎么做」只回原因 → 报；给了动作 → 不报
    r = audit.scan_session([_turn(1, "我该怎么做呢", "您这笔申请没有通过审核，原因是材料不全。", "NEED")])
    hit = any(d["code"] == "real_no_answer" for d in r["defects"])
    r2 = audit.scan_session([_turn(1, "我该怎么做呢",
                                   "您需要补充营业执照，然后在后台重新提交申请。", "NEED")])
    miss = not any(d["code"] == "real_no_answer" for d in r2["defects"])
    one("问怎么做只回原因 → 报；给动作 → 不报", hit and miss, "hit=%s miss=%s" % (hit, miss))

    # 8) 干净通话 → 一条都不报
    clean = [_turn(0, "是的", "好的，麻烦说下申请时预留手机号的后四位，四个数字就行。", "GREETING"),
             _turn(1, "尾号是****", "核对上了，感谢配合。您这笔申请因材料不全被驳回，需要补充营业执照。", "NEED"),
             _turn(2, "我该怎么做呢", "您需要补充营业执照和店铺认证截图，然后在后台重新提交。", "NEED"),
             _turn(3, "知道了", "好的，我记录一下，稍后短信同步到后台。", "WRITEBACK", closed=True)]
    r = audit.scan_session(clean)
    one("干净通话 → 零缺陷", not r["defects"], str([d["code"] for d in r["defects"]]))
    one("干净通话 → 回写与阶段识别正确",
        r["metrics"]["closed"] and r["metrics"]["max_stage"] == "WRITEBACK",
        str(r["metrics"]))

    # 9) 脱敏：数字与「某某女士」不许原样出现在报告里
    masked = audit.redact("尾号0086，虞静姝女士，单号QZ202611060001")
    one("脱敏：数字与姓名不落报告",
        "0086" not in masked and "虞静姝" not in masked and "QZ2026" not in masked,
        masked)

    # 10) 聚类：support 按会话去重，不按缺陷条数
    rows = {"s1": {"defects": [
                {"code": "real_repeat", "label": "x", "stage": "NEED",
                 "intent": "", "turn": 1, "evidence": "a"},
                {"code": "real_repeat", "label": "x", "stage": "NEED",
                 "intent": "", "turn": 2, "evidence": "b"}]},
            "s2": {"defects": [
                {"code": "real_repeat", "label": "x", "stage": "NEED",
                 "intent": "", "turn": 1, "evidence": "c"}]}}
    cl = audit.cluster(rows)
    one("聚类 support 按会话去重",
        len(cl) == 1 and cl[0]["support"] == 2 and len(cl[0]["samples"]) == 2,
        json.dumps(cl, ensure_ascii=False)[:80])

    ok = sum(1 for _, v, _ in checks if v)
    for name, v, detail in checks:
        print("  [%s] %s%s" % ("通过" if v else "不通过", name,
                               ("｜" + detail) if (detail and not v) else ""))
    print("结果：%d/%d 通过" % (ok, len(checks)))
    return 0 if ok == len(checks) else 1


def main():
    ap = argparse.ArgumentParser(description="真实通话体检与改前/改后对比")
    sub = ap.add_subparsers(dest="cmd")
    m = sub.add_parser("mine", help="扫真实通话，找缺陷并聚类")
    m.add_argument("--db", default=DB_PATH)
    m.add_argument("--limit", type=int, default=0)
    m.add_argument("--days", type=int, default=0)
    m.add_argument("--json", default=os.path.join(REPORT_DIR, "real_calls.json"))
    m.add_argument("--verify", action="store_true",
                   help="对仍在配置里的缺陷，用当前代码重放同一批真实输入复验")
    m.add_argument("--verify-n", type=int, default=6,
                   help="复验最多重放几通（默认 6）")
    c = sub.add_parser("compare", help="按时间点对比改前/改后")
    c.add_argument("--since", required=True, help="分界时间，如 2026-11-09T12:00:00")
    c.add_argument("--db", default=DB_PATH)
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--days", type=int, default=0)
    r = sub.add_parser("replay", help="把真实用户话术重放给当前代码，对比缺陷")
    r.add_argument("--sessions", default="", help="逗号分隔的会话 ID")
    r.add_argument("--code", default="", help="按缺陷码挑会话（取自 mine 报告）")
    r.add_argument("--max-sessions", type=int, default=3)
    r.add_argument("--turns", type=int, default=6)
    r.add_argument("--db", default=DB_PATH)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--days", type=int, default=0)
    sub.add_parser("check", help="反向自测")
    a = ap.parse_args()
    if a.cmd == "mine":
        return mine(a)
    if a.cmd == "compare":
        return compare(a)
    if a.cmd == "replay":
        return replay(a)
    return check(a)


if __name__ == "__main__":
    sys.exit(main())
