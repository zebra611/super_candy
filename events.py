"""事件流（M5）—— 全项目的地基。

用途：
    每一次"用户开口""识别结果""工具调用""播报""回写"都落一条结构化事件。
    质检（M6）、评测与进化（M7）只读这张表，不碰在线逻辑。

    这是哑链路里少数"真实现"的部分：因为数据一旦从第一天就在，后面所有人
    都省事；数据如果从第三周才开始记，前面两周的通话就白跑了。

存储：
    SQLite 单文件（data/events.db），标准库 sqlite3，无外部依赖。

连接管理（v2）：
    按「线程 × 库路径」复用连接，见 `_connect()`。原因：早期每次写入开关一次
    文件连接，QPS 一上来就变成磁盘操作；而 ThreadingHTTPServer 每个请求一个线程，
    连接不能跨线程共享（sqlite3 默认 check_same_thread=True），所以用线程局部。
    以库路径为键：测试会把 events.DB_PATH 指到临时库，换路径即换连接，不会串库。

    WAL + synchronous=NORMAL：读写不互相阻塞，看板查询不会卡住在线写入。
    「库路径变了但旧连接还开着」这个问题靠键区分，旧连接随线程退出被回收。

索引（v2）：
    session_id / type / ts / (session_id, turn_id) 四个。看板要按时间范围和事件
    类型做聚合，只有 session_id 索引时是全表扫；事件表一过十万行就明显变慢。

归档（v2）：
    见 `archive_events()`，超期事件搬到 data/archive/events-YYYYMM.db 再删除，
    主库体积可控。归档不是删除——分析要用时仍可查归档库。
"""

import json
import os
import sqlite3
import threading
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
# 索引四个，各有明确用途，不做"以防万一"的全字段索引——写入也要成本。
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
-- 按会话取整通事件（前端事件链、质检转录）
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
-- 按类型聚合（看板的事件分布、闭环率、错误数）
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
-- 按时间范围聚合（看板的近 N 天趋势、按天归档）
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
-- 会话内按轮次取（看板取某一轮的全链路）
CREATE INDEX IF NOT EXISTS idx_events_session_turn ON events(session_id, turn_id);
"""

# 线程局部连接表：thread_id -> {db_path: sqlite3.Connection}
_local = threading.local()


def _connect():
    """取当前线程的库连接，没有就建一个并缓存。

    为什么不用全局单例：sqlite3 连接不是线程安全的（check_same_thread），
    而服务端每个请求一个线程。线程局部既躲开了跨线程访问，又省掉了每次
    写入的开关销——一个线程处理完自己的请求后连接一直留着，下个请求直接用。

    为什么以路径为键：scripts/verify.py 等测试会把 DB_PATH 指到临时库，
    同一线程里可能先后用到两个库。按路径区分才不会把事件写进别的库。
    """
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = {}
        _local.conns = conns
    path = DB_PATH
    conn = conns.get(path)
    if conn is None:
        # timeout 是等锁的上限：WAL 下写写仍会短暂互斥，等一会儿比直接报
        # "database is locked" 友好；15 秒足够撑过看板的一次聚合查询。
        conn = sqlite3.connect(path, timeout=15.0)
        try:
            # WAL：读写不互斥（看板读的时候通话照常写）
            conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL：崩溃最多丢最后一个事务，换掉每次提交的 fsync
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=15000")
        except sqlite3.DatabaseError:
            # 老库/只读介质上设置 PRAGMA 可能失败，不该因此让写入挂掉
            pass
        conns[path] = conn
    return conn


def close_connections():
    """关掉当前线程缓存的连接。测试收尾、或需要搬移库文件时调用。

    不调用也不会泄漏：线程结束连接跟着回收。但如果测试要删除临时目录，
    Windows 上打开的文件句柄会挡住删除，所以提供这个显式入口。
    """
    conns = getattr(_local, "conns", None)
    if not conns:
        return
    for conn in conns.values():
        try:
            conn.close()
        except Exception:
            pass
    conns.clear()


def init_db():
    """建目录、建表。进程启动时调用一次，重复调用无副作用。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = _connect()
    conn.executescript(_SCHEMA)
    conn.commit()



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

    conn = _connect()
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


def list_events(session_id):
    """按会话取全部事件，按写入顺序返回。前端"事件链"面板与质检都从这里读。"""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, session_id, turn_id, ts, type, payload, latency_ms, version"
        " FROM events WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()

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
    conn = _connect()
    rows = conn.execute(
        "SELECT session_id, MAX(ts) AS last_ts, COUNT(*) AS n"
        " FROM events GROUP BY session_id ORDER BY last_ts DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
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
    conn = _connect()
    rows = conn.execute(
        "SELECT session_id, turn_id, payload, latency_ms FROM events WHERE type='turn_end'"
    ).fetchall()
    reports = conn.execute(
        "SELECT session_id, payload FROM events WHERE type='latency_report'"
    ).fetchall()
    closed = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM events WHERE type='writeback'"
    ).fetchone()[0]

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


# ---------------------------------------------------------------------------
# 看板聚合（M5）：事件流之外的第二层，只做"读出来、摆整齐"，不改任何在线逻辑
# ---------------------------------------------------------------------------

def aggregate(days=None):
    """按日/按类型聚合事件，供指标看板用。

    参数：
        days  None = 全部历史；否则只统计最近 N 天（含今天）

    返回：
        {
          "sessions": 会话数, "events": 事件数, "turns": 轮次数,
          "closed_sessions": 走到回写的会话数, "close_rate": 闭环率,
          "errors": error 事件数, "error_rate": 每轮错误率,
          "barge_ins": 打断次数, "no_answer": 沉默轮次,
          "by_type": {事件类型: 条数}, "by_day": [{date, events, sessions}],
          "cost_cny": 成本估算合计, "since_ts": 窗口起点, "window_days": days
        }

    为什么单独一个函数而不塞进 metrics()：metrics() 是"对外承诺的时延口径"，
    字段被文档和答辩引用，只该追加不该长胖。看板是内部观察工具，口径可以更宽。
    """
    conn = _connect()
    since = 0
    if days:
        since = now_ms() - int(days) * 86400 * 1000

    where = "WHERE ts >= ?" if since else ""
    args = (since,) if since else ()

    total = conn.execute("SELECT COUNT(*) FROM events " + where, args).fetchone()[0]
    sessions = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM events " + where, args).fetchone()[0]
    closed = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM events "
        + (where + " AND " if where else "WHERE ") + "type='writeback'", args).fetchone()[0]
    by_type = conn.execute(
        "SELECT type, COUNT(*) FROM events " + where + " GROUP BY type", args).fetchall()
    turns = dict(by_type).get("turn_end", 0)
    errors = dict(by_type).get("error", 0)
    barge = dict(by_type).get("barge_in", 0)
    silent = dict(by_type).get("no_answer", 0)

    # 按天：ts 是毫秒，SQLite 的 date() 只吃秒，所以先除 1000
    by_day = conn.execute(
        "SELECT date(ts/1000, 'unixepoch', 'localtime') AS d,"
        "       COUNT(*) AS n, COUNT(DISTINCT session_id) AS s"
        " FROM events " + where + " GROUP BY d ORDER BY d DESC LIMIT 30", args).fetchall()

    cost = conn.execute(
        "SELECT payload FROM events " + (where + " AND " if where else "WHERE ")
        + "type='turn_end'", args).fetchall()
    cost_sum = 0.0
    for (payload,) in cost:
        try:
            cost_sum += float(json.loads(payload or "{}").get("est_cost_cny") or 0)
        except (ValueError, TypeError):
            pass

    return {
        "sessions": int(sessions or 0),
        "events": int(total or 0),
        "turns": int(turns or 0),
        "closed_sessions": int(closed or 0),
        "close_rate": round((closed or 0) / sessions, 4) if sessions else 0.0,
        "errors": int(errors or 0),
        "error_rate": round((errors or 0) / turns, 4) if turns else 0.0,
        "barge_ins": int(barge or 0),
        "no_answer": int(silent or 0),
        "by_type": {k: v for k, v in sorted(by_type, key=lambda r: -r[1])},
        "by_day": [{"date": r[0], "events": r[1], "sessions": r[2]} for r in by_day],
        "cost_cny": round(cost_sum, 4),
        "since_ts": since,
        "window_days": days,
    }


def db_stats():
    """事件库体检：体积、行数、时间跨度、索引是否就位。

    用途：盘前检查"库是不是被跑批污染了"（docs/14 提到踩过一次，305 个会话里
    300 个是跑批产物），以及归档脚本跑完看效果。
    """
    conn = _connect()
    rows = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM events").fetchone()
    sessions = conn.execute("SELECT COUNT(DISTINCT session_id) FROM events").fetchone()[0]
    idx = [r[1] for r in conn.execute("PRAGMA index_list(events)").fetchall()]
    size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    # -wal / -shm 也算进实际占用，否则删了没 vacuum 会以为没变小
    for suffix in ("-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            size += os.path.getsize(p)
    return {
        "db_path": DB_PATH,
        "size_bytes": size,
        "size_mb": round(size / 1024.0 / 1024.0, 2),
        "events": rows[0],
        "sessions": sessions,
        "oldest_ms": rows[1],
        "newest_ms": rows[2],
        "indexes": idx,
        "index_count": len(idx),
    }


def archive_events(before_days=30, dry_run=False):
    """把 N 天前的事件搬到 data/archive/events-YYYYMM.db，再从主库删除。

    为什么归档而不是直接删：质检复盘、答辩举证都要翻旧通话；但主库无限长会
    让看板的按天聚合越来越慢。归档库保持同一张表结构，需要时 ATTACH 就能查。

    dry_run=True 只统计不搬，先看会动多少行——上线前先这么跑一次。

    返回：{"moved": 行数, "target": 归档库路径, "cutoff_ms": 截止时间, "dry_run": bool}
    """
    cutoff = now_ms() - int(before_days) * 86400 * 1000
    conn = _connect()
    n = conn.execute("SELECT COUNT(*) FROM events WHERE ts < ?", (cutoff,)).fetchone()[0]
    if dry_run or not n:
        return {"moved": int(n or 0), "target": "", "cutoff_ms": cutoff, "dry_run": True}

    archive_dir = os.path.join(DATA_DIR, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    stamp = time.strftime("%Y%m", time.localtime(cutoff / 1000.0))
    target = os.path.join(archive_dir, "events-%s.db" % stamp)

    dst = sqlite3.connect(target)
    try:
        dst.executescript(_SCHEMA)
        rows = conn.execute(
            "SELECT id, session_id, turn_id, ts, type, payload, latency_ms, version"
            " FROM events WHERE ts < ? ORDER BY id", (cutoff,)).fetchall()
        dst.executemany(
            "INSERT INTO events (id, session_id, turn_id, ts, type, payload, latency_ms, version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        dst.commit()
    finally:
        dst.close()

    conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    conn.commit()
    return {"moved": int(n), "target": target, "cutoff_ms": cutoff, "dry_run": False}
