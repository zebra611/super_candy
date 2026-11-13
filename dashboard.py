"""指标看板聚合（M5 的一部分）—— 把事件库读成人能一眼看懂的几块。

为什么单独一个模块而不写进 app.py 的路由里：
    路由函数只该负责"收参数、调函数、发 JSON"。聚合逻辑一旦写进 Handler，
    就没法被脚本复用（答辩前想导一张表、CI 里想断言看板数字都对不上号），
    也没法单独测。这一层只读，不碰任何在线链路。

口径纪律（和 docs/04 一致，不许自创）：
    - 时延：直接复用 events.metrics()，不在这里重算分位数。重算就等于有两套口径。
    - 闭环：唯一证据是 writeback 事件，不看阶段名、不看回复文本。
    - 合规：复用 modules/eval.py 的 compliance_report()，不另立标准。
      它自己标了 is_placeholder=True，看板要如实显示"未完成"，不能抹平。

性能：
    看板每 15 秒最多重算一次（_CACHE_TTL）。逐会话质检要读整通事件，
    10 通大约几十毫秒；不缓存的话页面轮询会把读压力叠到在线写入上。
"""

import os
import threading
import time

import config_loader
import cost_guard
import events
from modules import eval as eval_mod

# 缓存有效期（秒）。看板自己每 10 秒拉一次，15 秒缓存足够且不会看到隔夜数据。
# 演示种子库路径。放在 data/ 下、与 events.db 并列，但它是**只读**的静态文件。
DEMO_DB = os.path.join(events.DATA_DIR, "demo_events.db")

_CACHE_TTL = 15.0
_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def _session_row(sess):
    """给一通电话算一行摘要。数据全部来自事件流，不另存一份。"""
    sid = sess["session_id"]
    evs = events.list_events(sid)
    turns = sum(1 for e in evs if e["type"] == "turn_end")
    closed = any(e["type"] == "writeback" for e in evs)
    errors = sum(1 for e in evs if e["type"] == "error")
    barge = sum(1 for e in evs if e["type"] == "barge_in")
    cost = 0.0
    for e in evs:
        if e["type"] == "turn_end":
            try:
                cost += float(e["payload"].get("est_cost_cny") or 0)
            except (TypeError, ValueError):
                pass
    # 走到了哪个环节：用 agent_reply 里的 stage 推断展示，仅用于人看，不用于判闭环
    stage = ""
    for e in evs:
        if e["type"] == "agent_reply" and e["payload"].get("stage"):
            stage = e["payload"]["stage"]
    return {
        "session_id": sid,
        "turns": turns,
        "closed": closed,
        "errors": errors,
        "barge_ins": barge,
        "cost_cny": round(cost, 4),
        "stage": stage,
        "last_ts": sess.get("last_ts", 0),
        "event_count": sess.get("event_count", len(evs)),
    }


def quality(limit=10):
    """最近 N 通电话的质检汇总，口径完全来自 modules/eval.py（角色 3 定的）。

    返回：
        {
          "sessions_checked": 检查了几通,
          "closed": 其中闭环几通,
          "failed_checks": {检查项: 失败次数},   # 哪一项最容易挂，比总分有用
          "avg_score": 平均分,
          "is_placeholder": 质检本身是否仍是占位实现,
          "note": 为什么不能把它当最终结论
        }

    为什么不在这里算"合规率"一个数：合规有几个维度，压成一个率会掩盖
    "是禁语漏了还是必说话术没说到"。失败项分布比一个百分比更能指导修改。
    """
    sessions = events.recent_sessions(limit=int(limit))
    failed = {}
    scores = []
    closed = 0
    checked = 0
    placeholder = False
    for s in sessions:
        sid = s["session_id"]
        try:
            rep = eval_mod.compliance_report(sid)
        except Exception:
            continue
        checked += 1
        closed += 1 if rep.get("closed") else 0
        scores.append(float(rep.get("score") or 0))
        for name in rep.get("failed") or []:
            failed[name] = failed.get(name, 0) + 1
        placeholder = placeholder or bool(rep.get("is_placeholder"))
    return {
        "sessions_checked": checked,
        "closed": closed,
        "failed_checks": failed,
        "avg_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "is_placeholder": placeholder,
        "note": ("质检仍为占位实现（语义级合规、模拟用户未做），"
                 "这里的数字只能说明'词表与必说话术有没有过'，不能当最终合规率汇报"),
    }


def summary(days=None, session_limit=10, use_cache=True, db="live"):
    """看板要的全部数据，一次返回。

    分成五块，对应看板上的五个区：
        scene    跑的是哪套场景、配置有没有问题
        headline 大数字：会话数、闭环率、首字 P50/P95、成本
        latency  两套口径明细（服务端处理 vs 浏览器首字）
        volume   事件类型分布 + 近 30 天趋势
        sessions 最近几通的逐条摘要
        quality  质检汇总（角色 3 的口径）
        db       库体积/行数/索引，用来判断"数据是不是被跑批污染了"

    参数 db：`live`（默认，真实通话）或 `demo`（演示种子库，只读）。
    演示库是**只读**打开的：走 events 的 db_path 参数，写路径永远只认 live，
    所以切到 demo 不会把演示数据写回生产库。
    """
    global _cache
    db = (db or "live").strip().lower()
    db_path = None
    if db == "demo":
        db_path = DEMO_DB
        if not os.path.exists(db_path):
            raise FileNotFoundError(
                "演示库不存在：%s；先生成一次：python scripts/make_demo_db.py" % db_path)

    with _lock:
        # 只缓存 live：演示库是静态文件，多算一次也不贵，缓存反而会在
        # 重新生成种子库后继续吐旧数字。
        if (use_cache and days is None and db == "live" and _cache["data"] is not None
                and time.time() - _cache["ts"] < _CACHE_TTL):
            return _cache["data"]

        agg = events.aggregate(days=days, db_path=db_path)
        met = events.metrics(db_path=db_path)
        reps = [_session_row(s) for s in events.recent_sessions(limit=int(session_limit))]

        data = {
            "generated_at": events.now_ms(),
            "window_days": days,
            "data_source": db,
            "scene": config_loader.describe(),
            "headline": {
                "sessions": agg["sessions"],
                "turns": agg["turns"],
                "close_rate": agg["close_rate"],
                "closed_sessions": agg["closed_sessions"],
                "first_audio_p50_ms": met["first_audio_p50_ms"],
                "first_audio_p95_ms": met["first_audio_p95_ms"],
                "first_audio_samples": met["first_audio_samples"],
                "p95_min_samples": met.get("p95_min_samples", 20),
                "gate_ms": met["gate_ms"],
                "cost_cny": agg["cost_cny"],
                "barge_ins": agg["barge_ins"],
                "errors": agg["errors"],
            },
            "latency": {
                "processing_p50_ms": met["processing_p50_ms"],
                "processing_p95_ms": met["processing_p95_ms"],
                "first_audio_p50_ms": met["first_audio_p50_ms"],
                "first_audio_p95_ms": met["first_audio_p95_ms"],
                "first_audio_text_p50_ms": met["first_audio_text_p50_ms"],
                "p95_min_samples": met.get("p95_min_samples", 20),
                "first_audio_estimated": met["first_audio_estimated"],
                "first_audio_timeouts": met["first_audio_timeouts"],
                "note": "首字口径只统计语音轮（source=asr）；处理耗时是服务端口径。两者不能混着报。",
            },
            "volume": {
                "by_type": agg["by_type"],
                "by_day": agg["by_day"],
            },
            "sessions": reps,
            "quality": quality(limit=session_limit),
            "cost": cost_guard.snapshot(),
            "db": events.db_stats(db_path=db_path),
        }
        _cache["ts"] = time.time()
        _cache["data"] = data
        return data


def invalidate():
    """清缓存。切场景、归档完事件后调用，避免看板还显示旧数字。"""
    global _cache
    with _lock:
        _cache = {"ts": 0.0, "data": None}
