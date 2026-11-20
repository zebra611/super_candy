# -*- coding: utf-8 -*-
"""P3-3 断言：部署模式带旧幂等库时拒绝启动，本地开发不受影响。

守三件事：
  1. 部署模式（deployed=True）+ data/tickets.db 存在 → 拒绝（SystemExit）；
  2. 本地模式（deployed=False）即使有库也不拦——本地自练本来就会写 tickets.db；
  3. ALLOW_STALE_DB=1 显式放行；无库放过。
另加一条源码断言：app.py 的 main() 必须在 events.init_db() 之前调用守卫，
否则旧库一旦被 init_db 接管就"洗白"了，自检形同虚设。
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import runtime_db_guard as guard  # noqa: E402


class RuntimeDbGuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sw_dbguard_")
        self._bypass = os.environ.get("ALLOW_STALE_DB")
        os.environ.pop("ALLOW_STALE_DB", None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        if self._bypass is None:
            os.environ.pop("ALLOW_STALE_DB", None)
        else:
            os.environ["ALLOW_STALE_DB"] = self._bypass

    def _make_stale(self):
        with open(os.path.join(self._tmp, "tickets.db"), "wb") as f:
            f.write(b"old ticket data")

    def test_deployed_with_stale_db_refuses(self):
        """部署模式带旧库 → SystemExit，错误信息说清修法。"""
        self._make_stale()
        with self.assertRaises(SystemExit) as ctx:
            guard.check(self._tmp, deployed=True)
        msg = str(ctx.exception)
        self.assertIn("tickets.db", msg)
        self.assertIn("prep_deploy.py", msg)
        self.assertIn("ALLOW_STALE_DB", msg)

    def test_local_mode_never_refuses(self):
        """本地开发即使有 tickets.db 也不拦——自练/跑批本来就会写它。"""
        self._make_stale()
        self.assertEqual(guard.check(self._tmp, deployed=False), [])

    def test_no_stale_db_passes(self):
        """部署模式但库干净 → 放过。"""
        self.assertEqual(guard.check(self._tmp, deployed=True), [])

    def test_demo_db_not_counted_as_stale(self):
        """demo_events.db 是有意留下的种子库，不算旧库。"""
        with open(os.path.join(self._tmp, "demo_events.db"), "wb") as f:
            f.write(b"seed")
        self.assertEqual(guard.check(self._tmp, deployed=True), [])

    def test_bypass_allows_with_warning_list(self):
        """显式放行 → 不抛，返回被放行的库名（供日志）。"""
        self._make_stale()
        os.environ["ALLOW_STALE_DB"] = "1"
        self.assertEqual(guard.check(self._tmp, deployed=True), ["tickets.db"])

    def test_app_calls_guard_before_init_db(self):
        """源码断言：守卫调用必须在 main() 的 init_db() 之前，否则旧库被接管就洗白了。"""
        src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
        main_at = src.find("def main():")
        self.assertGreater(main_at, 0)
        body = src[main_at:]

        def call_pos(token):
            """找真正的调用行（行首是缩进 + token），跳过注释里提到的。"""
            for line_no, line in enumerate(body.splitlines()):
                if line.lstrip().startswith(token):
                    return line_no
            return -1

        guard_line = call_pos("runtime_db_guard.check(")
        init_line = call_pos("events.init_db()")
        self.assertGreater(guard_line, 0, "main() 应调用 runtime_db_guard.check")
        self.assertGreater(init_line, 0, "main() 应调用 events.init_db()")
        self.assertLess(guard_line, init_line,
                        "守卫必须在 init_db 之前，否则自检无效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
