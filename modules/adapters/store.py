"""幂等的持久化存储 —— 进程重启之后，同一通电话仍然是同一张工单。

为什么需要它（`docs/14` 待改进第 5 条）：
    工单的幂等表原来是进程内的 `dict`，服务一重启就清空。于是：
        「同一个幂等键重放 → 不重复建单」
    在重启后立刻失效——重放会再建一张，业务系统里就出现两张内容相同的单。
    赛题把"幂等性与一致性"单独列成评分点，这一条是它最容易被问倒的地方。

为什么默认还是内存：
    `docs/17` §3.2 的同一理由——队友 clone 下来没有额外依赖也得能跑。
    持久化是**开出来的能力**，不是默认行为：
        tools.idempotency_store = "memory"（默认）| "sqlite"
        tools.idempotency_path  = "data/tickets.db"（可省）

为什么用 SQLite 而不是 Redis/MySQL：
    项目零第三方依赖（见 Dockerfile），标准库自带 sqlite3，
    而且它的唯一索引天然能做幂等键的去重——这正是 `docs/14` 建议的方向。

这一层只做存储，不认识工单的业务字段，也不做校验：
    存什么、什么时候查，仍然由 adapters/mock.py 决定。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading

#: 默认落库位置（相对仓库根）。config 里没写 tools.idempotency_path 时用它。
DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "tickets.db")

_SCHEMA = """CREATE TABLE IF NOT EXISTS tickets (
    idempotency_key TEXT PRIMARY KEY,
    ticket_id       TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    content         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_session ON tickets(session_id);

CREATE TABLE IF NOT EXISTS ticket_sessions (
    session_id TEXT PRIMARY KEY,
    ticket_id  TEXT NOT NULL
);
"""


def _connect(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


class _Base:
    """两类表共用的连接与锁：适配器可能被并发调用（多路通话），必须串行化。"""

    def __init__(self, path):
        self.path = path
        self._conn = _connect(path)
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            self._conn.close()


class TicketTable(_Base):
    """`_tickets` 的替身：键 = 幂等键，值 = 工单 dict。

    只实现 `mock.py` 用到的那些 dict 操作（`in` / `[]` / `get` / `values` / `clear`），
    不假装自己是个完整映射——少一层"看起来像 dict 其实不是"的坑。
    """

    @staticmethod
    def _row_to_ticket(row):
        return {
            "ticket_id": row["ticket_id"],
            "session_id": row["session_id"],
            "content": json.loads(row["content"] or "{}"),
            "version": row["version"],
            "updated_at": row["updated_at"],
        }

    def __contains__(self, key):
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM tickets WHERE idempotency_key = ? LIMIT 1", (key,))
            return cur.fetchone() is not None

    def __getitem__(self, key):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM tickets WHERE idempotency_key = ?", (key,))
            row = cur.fetchone()
        if row is None:
            raise KeyError(key)
        return self._row_to_ticket(row)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key, ticket):
        with self._lock:
            self._conn.execute(
                "INSERT INTO tickets (idempotency_key, ticket_id, session_id, content,"
                " version, updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(idempotency_key) DO UPDATE SET"
                " ticket_id=excluded.ticket_id, session_id=excluded.session_id,"
                " content=excluded.content, version=excluded.version,"
                " updated_at=excluded.updated_at",
                (key, ticket["ticket_id"], ticket["session_id"],
                 json.dumps(ticket.get("content") or {}, ensure_ascii=False),
                 int(ticket.get("version") or 1), int(ticket.get("updated_at") or 0)))
            self._conn.commit()

    def values(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tickets").fetchall()
        return [self._row_to_ticket(r) for r in rows]

    def clear(self):
        with self._lock:
            self._conn.execute("DELETE FROM tickets")
            self._conn.commit()


class SessionTable(_Base):
    """`_ticket_by_session` 的替身：键 = 会话 ID，值 = 工单号。"""

    def __contains__(self, sid):
        return self.get(sid) is not None

    def get(self, sid, default=None):
        with self._lock:
            cur = self._conn.execute(
                "SELECT ticket_id FROM ticket_sessions WHERE session_id = ?", (sid,))
            row = cur.fetchone()
        return row["ticket_id"] if row else default

    def __setitem__(self, sid, ticket_id):
        with self._lock:
            self._conn.execute(
                "INSERT INTO ticket_sessions (session_id, ticket_id) VALUES (?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET ticket_id=excluded.ticket_id",
                (sid, ticket_id))
            self._conn.commit()

    def clear(self):
        with self._lock:
            self._conn.execute("DELETE FROM ticket_sessions")
            self._conn.commit()


def attach_sqlite(adapter, path):
    """把适配器里的两张内存表换成 SQLite 上的两张表（同一份文件）。

    只对**自己持有幂等表**的适配器有效（现在就是 MockAdapter）。
    RealAdapter 的持久化在对端：Mock 服务的 `tickets.json` 就是它的落库，
    这边不需要再存一份，否则就成了两个真相。
    """
    adapter._tickets = TicketTable(path)
    adapter._ticket_by_session = SessionTable(path)
    # 标记成"有落库"：reset_adapter() 不会把它的数据清掉
    adapter._persistent = True
    return adapter
