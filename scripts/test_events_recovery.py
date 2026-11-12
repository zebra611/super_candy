# -*- coding: utf-8 -*-
"""事件库损坏自愈（11-12 线上事故回归）。

线上表现：点「文字对话」→ `服务没连上（Unexpected token 's', "socket hang up"
is not valid JSON）`。真因是容器里的 `data/events.db` 变成
`database disk image is malformed`，开单第一件事 `write_event(call_started)`
抛异常，连接被掐断，网关只回一句纯文本。

这里守四件事：
  ① 坏库能自愈（隔离 + 重建），不是让请求失败；
  ② 运行中坏掉的库，写入要重试一次；
  ③ 实在写不进去也不许抛——事件是遥测，不能把电话干掉；
  ④ 正常库不受影响（反向对照）。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import events


class EventsRecoveryTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evrec-")
        self._db = events.DB_PATH
        events.close_connections()
        events.reset_degrade()          # 熔断窗口是进程级的，测试间必须清
        events.DB_PATH = os.path.join(self.tmp, "events.db")

    def tearDown(self):
        events.close_connections()
        events.reset_degrade()
        events.DB_PATH = self._db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _corrupt(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"SQLite format 3\x00" + os.urandom(2048))

    def test_corrupt_db_is_quarantined_and_rebuilt(self):
        self._corrupt(events.DB_PATH)
        events.init_db()                     # 不许抛
        eid = events.write_event("s_recover", "call_started", {"x": 1})
        self.assertGreater(eid, 0, "坏库重建后应该能正常写入")
        left = [n for n in os.listdir(self.tmp) if ".corrupt-" in n]
        self.assertTrue(left, "坏库应该被隔离成 *.corrupt-<时间戳>，而不是被静默删掉")
        rows = events.list_events("s_recover")
        self.assertEqual(1, len(rows))

    def test_write_after_runtime_corruption(self):
        """库是在进程运行中坏的：缓存里那条连接还「好用」，必须换掉重试。"""
        events.init_db()
        events.write_event("s_a", "call_started", {})
        events.close_connections()           # 模拟连接被回收
        self._corrupt(events.DB_PATH)
        eid = events.write_event("s_b", "call_started", {})
        self.assertGreater(eid, 0, "运行中坏库也应自愈后写入成功")

    def test_write_never_raises_when_db_unusable(self):
        """连隔离都做不到（目录不可写）时，只许丢事件，不许抛异常。"""
        real_replace = os.replace
        real_remove = os.remove
        os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
        os.remove = lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
        try:
            self._corrupt(events.DB_PATH)
            eid = events.write_event("s_c", "call_started", {})
            self.assertEqual(0, eid, "写不进去就返回 0，不能把异常抛给通话")
        finally:
            os.replace = real_replace
            os.remove = real_remove

    def test_healthy_db_still_writes(self):
        """反向对照：正常库不能因为加了自愈逻辑就变慢或写坏。"""
        events.init_db()
        ids = [events.write_event("s_ok", "turn_start", {"i": i}) for i in range(3)]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(3, len(events.list_events("s_ok")))
        self.assertFalse([n for n in os.listdir(self.tmp) if ".corrupt-" in n])


class RequestGuardTest(unittest.TestCase):
    """异常必须变成 JSON，而不是把连接掐断（前端才不会只看到 socket hang up）。"""

    def test_guard_returns_json_error(self):
        import app as A

        class Fake(object):
            def __init__(self):
                self.sent = None
                self.code = None

            def _send_json(self, obj, code=200):
                self.sent = obj
                self.code = code

        # 直接把被测方法绑到假对象上：Handler 继承 BaseRequestHandler，
        # 真构造要 socket/server，测这一层不需要。
        Fake._guard = A.Handler._guard
        handler = Fake()

        def boom():
            raise sqlite3.DatabaseError("database disk image is malformed")

        handler._guard(boom)
        self.assertEqual(500, handler.code)
        self.assertEqual("SERVER_ERROR", handler.sent.get("error"))
        self.assertIn("malformed", handler.sent.get("detail", ""))


if __name__ == "__main__":
    unittest.main()
