"""评测套件与每日归档的单元测试：只测纯逻辑与失败分支，不跑真实用例。

跑法：
    python3 scripts/test_eval_suite.py

为什么这些测试重要：这套工具产出的是答辩引用的数字。
一旦"回写合规率"的分母算错、或每日归档在失败时静默成功，
报告上的数字就会变成假证据——比没有报告更糟。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import daily_run as D                       # noqa: E402
import eval_suite as ES                     # noqa: E402


def _tool_call(consent=True, confirmed=True, version=1, drop=None):
    # 字段集合与 config.writeback_template() 对齐：模板里有 note，真实 content 也带它。
    content = {"call_result": "connected", "identity_confirmed": confirmed,
               "need": "咨询", "consent": consent, "agent_version": "v1",
               "note": "占位", "writeback_version": version}
    if drop:
        content.pop(drop, None)
    return {"type": "tool_call", "payload": {"name": "create_ticket",
                                             "args": {"content": content}}}


def _row(cid, calls):
    return {"id": cid, "evs": calls}


class WritebackMetricsTest(unittest.TestCase):
    def test_all_valid(self):
        rows = [_row("A", [_tool_call()]), _row("B", [_tool_call(consent=False)])]
        m = ES.collect_writeback_metrics(rows)
        self.assertEqual((m["calls"], m["ok_calls"]), (2, 2))
        self.assertEqual((m["sessions"], m["sessions_ok"]), (2, 2))
        self.assertEqual(m["rate_calls"], 1.0)
        self.assertEqual(m["rate_sessions"], 1.0)

    def test_missing_required_field(self):
        m = ES.collect_writeback_metrics([_row("A", [_tool_call(drop="need")])])
        self.assertEqual(m["ok_calls"], 0)
        self.assertEqual(m["problems"][0]["missing"], ["need"])

    def test_bad_consent_value(self):
        m = ES.collect_writeback_metrics([_row("A", [_tool_call(consent="yes")])])
        self.assertEqual(m["ok_calls"], 0)
        self.assertIn("consent", m["problems"][0]["bad"][0])

    def test_wrong_types(self):
        m = ES.collect_writeback_metrics([_row("A", [_tool_call(confirmed="true")])])
        self.assertEqual(m["ok_calls"], 0)
        m = ES.collect_writeback_metrics([_row("A", [_tool_call(version="1")])])
        self.assertEqual(m["ok_calls"], 0)

    def test_one_bad_call_marks_whole_session(self):
        """改口会产生第二次回写：第二次写错，会话口径必须判不合格。"""
        rows = [_row("C01", [_tool_call(), _tool_call(drop="need")])]
        m = ES.collect_writeback_metrics(rows)
        self.assertEqual((m["calls"], m["ok_calls"]), (2, 1))
        self.assertEqual((m["sessions"], m["sessions_ok"]), (1, 0))
        self.assertEqual(m["rate_calls"], 0.5)
        self.assertEqual(m["rate_sessions"], 0.0)

    def test_no_writeback_session_excluded(self):
        """拒绝后挂断没有 create_ticket，不能进分母，否则"没发生"被记成"不合格"。"""
        m = ES.collect_writeback_metrics([_row("R01", [])])
        self.assertEqual(m["calls"], 0)
        self.assertIsNone(m["rate_calls"])
        self.assertIsNone(m["rate_sessions"])


class UniquePathTest(unittest.TestCase):
    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "suite_x.md")
            open(first, "w").close()
            second = ES._unique(first)
            self.assertNotEqual(first, second)
            self.assertTrue(second.endswith("-2.md"))


def _payload(rate=1.0, adv=1.0, errors=0):
    return {
        "meta": {"generated_at": "2026-11-06 00:00:00", "commit": "abc1234",
                 "duration_sec": 1.0, "seed_cases": "eval/seed_cases.json",
                 "adversarial_cases": "eval/adversarial_cases.json", "rounds": 1},
        "baseline": {
            "total": 10, "passed": int(round(rate * 10)), "closed": 5,
            "by_category": {"normal": (3, 3)},
            "closure": {"should": 5, "should_ok": 5, "should_not": 4,
                        "wrongly": 0, "undeclared": 1, "rate": 1.0},
            "writeback": {"calls": 4, "ok_calls": 4, "sessions": 4, "sessions_ok": 4,
                          "problems": [], "rate_calls": 1.0, "rate_sessions": 1.0},
            "avg_turns": 3.5, "errors": errors,
        },
        "baseline_rows": [],
        "adversarial": {
            "total": 4, "rounds": 1, "rates": [int(adv * 4)], "avg_rate": adv,
            "stable": 4, "unstable": [], "failed_ids": [], "errors": 0,
            "per_case": {"A": {"passes": 1, "stable": True}}, "clusters": [], "raw": [],
        },
    }


class ClusterTest(unittest.TestCase):
    """L8：聚类只认 judge 的 problem 前缀，不引入第二套判定。"""

    def _row(self, cid, ok, problems):
        return {"id": cid, "ok": ok, "problems": problems}

    def test_groups_by_reason(self):
        rows = [self._row("A", False, ["意向不符：期望同意，实际 False"]),
                self._row("B", False, ["闭环不符：期望 True 实际 False"]),
                self._row("C", True, [])]
        got = {c["reason"]: c["cases"] for c in ES.cluster_failures(rows)}
        self.assertEqual(got["没听懂"], ["A"])
        self.assertEqual(got["漏闭环"], ["B"])
        self.assertNotIn("C", [i for v in got.values() for i in v])

    def test_multi_reason_case_counted_once_per_reason(self):
        rows = [self._row("X", False, ["说了不该说的：保证有效", "意向不符：期望同意，实际 False"])]
        got = {c["reason"]: c["count"] for c in ES.cluster_failures(rows)}
        self.assertEqual(got["说错了"], 1)
        self.assertEqual(got["没听懂"], 1)

    def test_no_failure(self):
        self.assertEqual(ES.cluster_failures([self._row("A", True, [])]), [])


class VersionCompareTest(unittest.TestCase):
    """L8：过拟合判据只有一条——留出集下降。"""

    def _ver(self, name, passed, total, hold, hold_total, cases=None):
        return {"name": name, "passed": passed, "total": total, "closed": 0,
                "should": 0, "should_ok": 0, "wrongly": 0,
                "holdout_passed": hold, "holdout_total": hold_total,
                "cases": cases or []}

    def test_regression_when_holdout_drops(self):
        base = self._ver("v1", 9, 10, 4, 4)
        cur = self._ver("v2", 9, 10, 2, 4)
        self.assertTrue(ES.compare_versions(base, cur)["regression"])

    def test_no_regression_when_holdout_flat(self):
        base = self._ver("v1", 9, 10, 4, 4)
        cur = self._ver("v2", 10, 10, 4, 4)
        res = ES.compare_versions(base, cur)
        self.assertFalse(res["regression"])
        self.assertGreater(res["rate_delta"], 0)

    def test_better_and_worse_lists(self):
        base = self._ver("v1", 1, 2, 1, 2, cases=[{"id": "A", "ok": True, "closed": True},
                                                  {"id": "B", "ok": False, "closed": False}])
        cur = self._ver("v2", 1, 2, 1, 2, cases=[{"id": "A", "ok": False, "closed": False},
                                                 {"id": "B", "ok": True, "closed": True}])
        res = ES.compare_versions(base, cur)
        self.assertEqual(res["better"], ["B"])
        self.assertEqual(res["worse"], ["A"])


class SuiteCliTest(unittest.TestCase):
    def _run(self, argv, payload):
        import contextlib
        import io
        original = ES.collect
        ES.collect = lambda *a, **k: payload
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                return ES.main(argv + ["--no-report"])
        finally:
            ES.collect = original

    def test_ok_returns_0(self):
        self.assertEqual(self._run(["--rounds", "1"], _payload()), 0)

    def test_bad_rounds_returns_2(self):
        self.assertEqual(self._run(["--rounds", "0"], _payload()), 2)

    def test_bad_threshold_returns_2(self):
        self.assertEqual(self._run(["--fail-under", "1.5"], _payload()), 2)

    def test_below_threshold_returns_1(self):
        self.assertEqual(self._run(["--fail-under", "0.95"], _payload(rate=0.5)), 1)

    def test_errors_return_3(self):
        self.assertEqual(self._run(["--rounds", "1"], _payload(errors=2)), 3)

    def test_report_written_and_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, js = ES.write_report("hello", _payload(), tmp)
            self.assertTrue(os.path.isfile(md))
            with open(js, encoding="utf-8") as f:
                self.assertIn("baseline", json.load(f))


def _silent(fn, *args, **kwargs):
    """静音执行：单测只断言结果，不把业务输出混进测试日志。"""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return fn(*args, **kwargs)


class DailyRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = (D.RUNS_DIR, D.CSV_PATH, D.ERROR_LOG)
        D.RUNS_DIR = self.tmp.name
        D.CSV_PATH = os.path.join(self.tmp.name, "history.csv")
        D.ERROR_LOG = os.path.join(self.tmp.name, "error.log")

    def tearDown(self):
        D.RUNS_DIR, D.CSV_PATH, D.ERROR_LOG = self._old
        self.tmp.cleanup()

    def test_archive_and_no_overwrite(self):
        original = ES.collect
        ES.collect = lambda rounds: _payload()
        try:
            self.assertEqual(_silent(D.main, ["--rounds", "1"]), 0)
            self.assertEqual(_silent(D.main, ["--rounds", "1"]), 0)
        finally:
            ES.collect = original
        files = sorted(os.listdir(self.tmp.name))
        self.assertIn("2026-11-06.json", files)
        self.assertIn("2026-11-06-2.json", files)

    def test_csv_header_written_once(self):
        self.assertTrue(_silent(D.main, ["--rounds", "1"]) in (0, 1))
        original = ES.collect
        ES.collect = lambda rounds: _payload()
        try:
            _silent(D.main, ["--rounds", "1"])
            _silent(D.main, ["--rounds", "1"])
        finally:
            ES.collect = original
        with open(D.CSV_PATH, encoding="utf-8") as f:
            lines = f.read().strip().split("\n")
        self.assertEqual(lines[0].split(",")[0], "date")
        self.assertEqual(sum(1 for l in lines if l.startswith("date")), 1)

    def test_failure_writes_error_log(self):
        original = ES.collect

        def boom(rounds):
            raise RuntimeError("模拟跑批崩溃")

        ES.collect = boom
        try:
            rc = _silent(D.main, ["--rounds", "1"])
        finally:
            ES.collect = original
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(D.ERROR_LOG))
        with open(D.ERROR_LOG, encoding="utf-8") as f:
            self.assertIn("模拟跑批崩溃", f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
