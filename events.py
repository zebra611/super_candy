"""事件流（M5）—— 全项目的地基。

用途：
    每一次"用户开口""识别结果""工具调用""播报""回写"都落一条结构化事件。
    质检（M6）、评测与进化（M7）只读这张表，不碰在线逻辑。

    这是哑链路里少数"真实现"的部分：因为数据一旦从第一天就在，后面所有人
    都省事；数据如果从第三周才开始记，前面两周的通话就白跑了。

存储：
    SQLite 单文件（data/events.db），标准库 sqlite3，无外部依赖。

设计取舍（刻意从简）：
    每次写入新开一个连接，不做连接池。理由：骨架阶段 QPS 极低，
    换来的是多线程下零锁风险。真实压测时再换连接池（见 TODO）。

后续待补充：
    - TODO 换成连接池 / 或改成异步写队列，避免每次写入都开关文件；
    - TODO 增加按时间范围、按事件类型的查询索引（目前只有 session_id 索引）；
    - TODO 事件表会持续增长，需要归档策略（超过 N 天的原始音频引用可清理）。
"""

import json
import os
import sqlite3
import time

from contracts import EVENT_VERSION

# 事件库文件位置：项目根目录下的 data/events.db
_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "events.db")

# 建表语句。
# 刻意不设外键与复杂索引：骨架阶段只按 session_id 明细查询 + 全表算分位数。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    turn_id    INTEGER NOT NULL DEFAULT 0,
    ts         INTEGER NOT NULL,
    type       TEXT    NOT NULL,
    payload    TEXT    NOT NULL DEFAULT '{}',
    latency_ms REAL    NOT NULL DEFAULT 0,
    version    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""


def init_db():
    """建目录、建表。进程启动时调用一次，重复调用无副作用。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def now_ms():
    """当前时间戳（毫秒）。全项目统一用它打时间戳，保证口径一致。"""
    return int(time.time() * 1000)


def write_event(session_id, event_type, payload=None, turn_id=0, latency_ms=0.0):
    """写一条事件。这是全项目被调用最多的函数。

    参数：
        session_id  通话 ID
        event_type  必须取自 contracts.EVENT_TYPES，写错了不会报错但质检会漏
        payload     事件内容 dict，会序列化成 JSON 落库
        turn_id     第几轮；通话级事件（call_started 等）填 0
        latency_ms  该环节耗时，没有就填 0

    返回：事件自增 id。

    后续待补充：
        - TODO 校验 event_type 是否在 EVENT_TYPES 内，非法值直接抛错（骨架阶段
          故意不校验，方便四个人随意加事件试跑）。
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "INSERT INTO events (session_id, turn_id, ts, type, payload, latency_ms, version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                turn_id,
                now_ms(),
                event_type,
                json.dumps(payload or {}, ensure_ascii=False),
                float(latency_ms),
                EVENT_VERSION,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_events(session_id):
    """按会话取全部事件，按写入顺序返回。前端"事件链"面板与质检都从这里读。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, session_id, turn_id, ts, type, payload, latency_ms, version"
            " FROM events WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "session_id": r[1],
            "turn_id": r[2],
            "ts": r[3],
            "type": r[4],
            "payload": json.loads(r[5] or "{}"),
            "latency_ms": r[6],
            "version": r[7],
        })
    return out


def recent_sessions(limit=20):
    """最近 N 次通话（按最后一条事件时间倒序）。给看板和评测挑样本用。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT session_id, MAX(ts) AS last_ts, COUNT(*) AS n"
            " FROM events GROUP BY session_id ORDER BY last_ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    return [{"session_id": r[0], "last_ts": r[1], "event_count": r[2]} for r in rows]


def _percentile(sorted_vals, p):
    """线性插值求分位数。输入已排序。P50 用它、P95 也用它，口径统一。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def metrics():
    """算时延指标：每轮端到端耗时的 P50 / P95，以及轮数与闭环通话数。

    口径（骨架阶段自定义，拿到官方定义后要覆盖这里）：
        一轮的端到端耗时记在 turn_end 事件上，由 app.py 在轮次收尾时写入。

    后续待补充：
        - TODO 官方口径明确后，改成"用户末字 → 首包出声"的分段口径；
        - TODO 增加按版本（version）分组的对比，供自进化前后对比使用。
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT latency_ms FROM events WHERE type='turn_end' AND latency_ms > 0"
        ).fetchall()
        closed = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM events WHERE type='writeback'"
        ).fetchone()[0]
    finally:
        conn.close()

    vals = sorted(r[0] for r in rows)
    return {
        "turns": len(vals),
        "p50_ms": round(_percentile(vals, 50), 1),
        "p95_ms": round(_percentile(vals, 95), 1),
        "closed_sessions": int(closed or 0),
    }
