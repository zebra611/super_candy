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

from contracts import EVENT_TYPES, EVENT_VERSION, LATENCY_GATE_MS

# 事件库文件位置：项目根目录下的 data/events.db
_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "events.db")

# 超过这个耗时的轮次记为超时：不并入分位数，但要在报告里写明条数。
# 依据 docs/04 第 4 节的统计口径。
TIMEOUT_MS = 10000

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

    事件类型在写入前校验：不在 contracts.EVENT_TYPES 里直接抛错。
    为什么要管这个：早期为了"方便随便加事件试跑"故意不校验，结果一旦拼错
    （比如把 turn_end 写成 turn_ended），事件照样写进库，只是质检和面板
    永远看不到它——数据静默缺失，比直接崩掉难查得多。要加新事件，
    先改 contracts 再改调用处，这正是契约该有的约束力。
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(
            "未登记的事件类型：%r。合法值是 contracts.EVENT_TYPES 里的 16 种；"
            "要新增事件请先改契约，不要在调用处临时造名字。" % (event_type,))

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


def metrics(gate_ms=LATENCY_GATE_MS):
    """算时延指标。**两套口径并列**，因为它们回答两个不同的问题。

    口径 A「处理耗时」= turn_end.payload.processing_ms
        服务端从收到请求到返回回复文本的耗时。回答"服务器快不快"。
        它不含用户说话之前的部分，也不含声音真正播出来——**不是首字响应**。

    口径 B「首字响应」= latency_report.payload.first_audio_ms（T4 − T0）
        浏览器实测：用户说完/点发送 → 听到第一帧声音。回答"用户等多久"，
        这才是赛题 2 秒门槛对应的数字。
        没有前端上报时，退回 turn_end.payload.server_first_audio_ms
        （服务端下界：请求到达 → 首包就绪），并标 estimated=True。

    样本规则（与 docs/04 一致）：
        - 首字口径只统计语音轮（source=asr）；打字轮单列，仅供调试参考；
        - 超过 TIMEOUT_MS 的记为超时，不并入分位数，但要单独报条数；
        - 样本数 < 20 不出 P95（少样本的 P95 没有意义）。

    返回：dict，字段见函数末尾。新增字段只追加，不改已有名字。
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT session_id, turn_id, payload, latency_ms FROM events WHERE type='turn_end'"
        ).fetchall()
        reports = conn.execute(
            "SELECT session_id, payload FROM events WHERE type='latency_report'"
        ).fetchall()
        closed = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM events WHERE type='writeback'"
        ).fetchone()[0]
    finally:
        conn.close()

    # 前端上报按 (session, turn) 建索引；同一轮重复上报以最后一条为准
    client = {}
    timeouts = 0
    for sid, payload in reports:
        p = json.loads(payload or "{}")
        v = p.get("first_audio_ms")
        if not isinstance(v, (int, float)) or v <= 0:
            continue
        if v >= TIMEOUT_MS:
            timeouts += 1
            continue
        client[(sid, p.get("turn_id"))] = (float(v), bool(p.get("estimate")))

    turns = []
    for sid, tid, payload, latency in rows:
        p = json.loads(payload or "{}")
        turns.append({
            "session_id": sid,
            "turn_id": tid,
            "source": p.get("source", "text"),
            "processing_ms": float(p.get("processing_ms") or latency or 0),
            "server_first_audio_ms": p.get("server_first_audio_ms"),
            "cost_cny": float(p.get("est_cost_cny") or 0),
        })

    processing = sorted(t["processing_ms"] for t in turns
                        if 0 < t["processing_ms"] < TIMEOUT_MS)

    def collect(sources):
        """取指定来源轮次的首字样本，返回 (样本, 估算条数)。"""
        vals, estimated = [], 0
        for t in turns:
            if t["source"] not in sources:
                continue
            hit = client.get((t["session_id"], t["turn_id"]))
            if hit:
                vals.append(hit[0])
                estimated += 1 if hit[1] else 0
            elif t["server_first_audio_ms"]:
                vals.append(float(t["server_first_audio_ms"]))
                estimated += 1
        return sorted(vals), estimated

    voice_vals, voice_est = collect({"asr"})
    text_vals, _ = collect({"text"})

    def p95(vals):
        # 样本太少时 P95 等于最大值，容易造成误判，干脆不出
        return _percentile(vals, 95) if len(vals) >= 20 else None

    within = sum(1 for v in voice_vals if v <= gate_ms)

    return {
        "turns": len(turns),
        "turns_voice": sum(1 for t in turns if t["source"] == "asr"),
        "turns_text": sum(1 for t in turns if t["source"] == "text"),
        "turns_silence": sum(1 for t in turns if t["source"] == "silence"),
        # 口径 A：服务端处理耗时
        "processing_p50_ms": round(_percentile(processing, 50), 1),
        "processing_p95_ms": round(p95(processing) or 0, 1),
        # 口径 B：首字响应（语音轮，浏览器实测）
        "first_audio_p50_ms": round(_percentile(voice_vals, 50), 1),
        "first_audio_p95_ms": round(p95(voice_vals) or 0, 1),
        "first_audio_samples": len(voice_vals),
        "first_audio_estimated": voice_est,
        "first_audio_timeouts": timeouts,
        "gate_ms": int(gate_ms),
        # 打字轮的参考值：不是门槛口径，只用来判断"是不是服务端在拖慢"
        "first_audio_text_p50_ms": round(_percentile(text_vals, 50), 1),
        "first_audio_text_samples": len(text_vals),
        "closed_sessions": int(closed or 0),
        "est_cost_cny": round(sum(t["cost_cny"] for t in turns), 4),
    }
