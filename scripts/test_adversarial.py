"""对抗跑批器的单元测试：只测纯逻辑与异常分支，不调云端、不写仓库。

跑法：
    python3 scripts/test_adversarial.py
    python3 -m unittest scripts.test_adversarial -v

为什么单独写测试：这个脚本的价值是"给出可信的数字"。如果它自己会把
非法用例静默跑成 0 分、或把异常吞成通过，那它产出的所有数字都不可信。
所以校验、聚合、退出码这三块必须有测试兜底。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adversarial_run as A                 # noqa: E402


def _case(cid="X01", **over):
    base = {
        "id": cid,
        "category": "adversarial",
        "tag": "semantic",
        "goal": "占位目标",
        "turns": ["喂，是我", "尾号8866", "同意"],
        "expect": {"closed": True, "consent": True},
    }
    base.update(over)
    return base


class ValidateCasesTest(unittest.TestCase):
    def test_valid_case_passes(self):
        self.assertEqual(len(A.validate_cases([_case()])), 1)

    def test_repo_case_file_is_valid(self):
        """仓库里的对抗集必须能通过校验——防止有人改坏了 schema 还不知道。"""
        self.assertGreater(len(A.load_cases(A.CASES_PATH)), 0)

    def test_missing_field(self):
        case = _case()
        case.pop("goal")
        with self.assertRaises(A.CaseError) as ctx:
            A.validate_cases([case])
        self.assertIn("缺字段", str(ctx.exception))

    def test_duplicate_id(self):
        with self.assertRaises(A.CaseError) as ctx:
            A.validate_cases([_case("X01"), _case("X01")])
        self.assertIn("id 重复", str(ctx.exception))

    def test_unknown_expect_key(self):
        with self.assertRaises(A.CaseError) as ctx:
            A.validate_cases([_case(expect={"closed": True, "clsoed": False})])
        self.assertIn("未定义键", str(ctx.exception))

    def test_typo_field_detected(self):
        case = _case()
        case["expcet"] = {}
        with self.assertRaises(A.CaseError) as ctx:
            A.validate_cases([case])
        self.assertIn("未定义字段", str(ctx.exception))

    def test_bad_consent(self):
        with self.assertRaises(A.CaseError) as ctx:
            A.validate_cases([_case(expect={"consent": "yes"})])
        self.assertIn("consent", str(ctx.exception))

    def test_unknown_consent_is_accepted_but_documented(self):
        A.validate_cases([_case(expect={"consent": "unknown"})])

    def test_empty_turns(self):
        with self.assertRaises(A.CaseError) as ctx:
            A.validate_cases([_case(turns=[])])
        self.assertIn("turns", str(ctx.exception))

    def test_turns_not_string(self):
        with self.assertRaises(A.CaseError) as ctx:
            A.validate_cases([_case(turns=["喂", 8866])])
        self.assertIn("非字符串", str(ctx.exception))

    def test_bad_final_stage(self):
        with self.assertRaises(A.CaseError) as ctx:
            A.validate_cases([_case(expect={"final_stage": "FINISH"})])
        self.assertIn("final_stage", str(ctx.exception))

    def test_bad_must_not_stage(self):
        with self.assertRaises(A.CaseError) as ctx:
            A.validate_cases([_case(expect={"must_not_stage": ["OFFER", "NOPE"]})])
        self.assertIn("must_not_stage", str(ctx.exception))

    def test_bad_category(self):
        with self.assertRaises(A.CaseError) as ctx:
            A.validate_cases([_case(category="adv")])
        self.assertIn("category", str(ctx.exception))

    def test_not_a_list(self):
        with self.assertRaises(A.CaseError):
            A.validate_cases({"cases": []})

    def test_errors_are_aggregated(self):
        """一次列出全部问题，而不是改一条跑一次。"""
        bad1 = _case("X01")
        bad1.pop("goal")
        bad2 = _case("X02", category="oops")
        with self.assertRaises(A.CaseError) as ctx:
            A.validate_cases([bad1, bad2])
        self.assertIn("共 2 处", str(ctx.exception))


class MultiFileTest(unittest.TestCase):
    """两套对抗集必须能合并且 id 不撞——L9 的回归防线靠这个。"""

    def test_merge_all_adversarial_files(self):
        import glob
        files = sorted(glob.glob(os.path.join(A.CASES_DIR, "adversarial*.json")))
        self.assertGreaterEqual(len(files), 2)
        merged = []
        for path in files:
            merged.extend(A.load_cases(path))
        A.validate_cases(merged)          # 重复 id / 非法字段都会在这里炸
        self.assertGreaterEqual(len(merged), 20)

    def test_seed_set_has_holdout(self):
        """L7：留出集必须在 20%±2%，否则"不参与调优"这句话就不成立。"""
        with open(os.path.join(A.R._ROOT, "eval", "seed_cases.json"), encoding="utf-8") as f:
            raw = json.load(f)
        cases = raw["cases"] if isinstance(raw, dict) else raw
        hold = sum(1 for c in cases if c.get("holdout"))
        ratio = hold / float(len(cases))
        self.assertGreaterEqual(len(cases), 80)
        self.assertLessEqual(len(cases), 120)
        self.assertAlmostEqual(ratio, 0.2, delta=0.02)


class LoadCasesTest(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(A.CaseError) as ctx:
            A.load_cases("/tmp/definitely-not-exist-3hao.json")
        self.assertIn("找不到", str(ctx.exception))

    def test_broken_json_points_to_position(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write('[{"id": "X01",}]')
            with self.assertRaises(A.CaseError) as ctx:
                A.load_cases(path)
            self.assertIn("不是合法 JSON", str(ctx.exception))
        finally:
            os.remove(path)


class AggregateTest(unittest.TestCase):
    def _rows(self, verdicts):
        return [{"id": cid, "ok": ok, "closed": ok, "consent": None,
                 "problems": [] if ok else ["闭环不符：期望 True 实际 False"],
                 "stage": "DONE" if ok else "OFFER", "error": ""}
                for cid, ok in verdicts]

    def test_all_pass_is_stable(self):
        per_round = [self._rows([("A", True), ("B", True)]),
                     self._rows([("A", True), ("B", True)])]
        stats = A.aggregate(per_round, ["A", "B"])
        self.assertEqual(stats["rates"], [2, 2])
        self.assertEqual(stats["stable"], 2)
        self.assertEqual(stats["unstable"], [])
        self.assertAlmostEqual(stats["avg_rate"], 1.0)

    def test_flaky_case_is_unstable(self):
        per_round = [self._rows([("A", True)]), self._rows([("A", False)])]
        stats = A.aggregate(per_round, ["A"])
        self.assertEqual(stats["unstable"], ["A"])
        self.assertEqual(stats["stable"], 0)
        self.assertEqual(stats["per_case"]["A"]["passes"], 1)

    def test_failure_cluster_counts_stage_and_kind(self):
        rows = self._rows([("A", False), ("B", False)])
        stats = A.aggregate([rows], ["A", "B"])
        self.assertEqual(stats["clusters"][("OFFER", "闭环不符")], 2)

    def test_error_counted_separately(self):
        rows = [{"id": "A", "ok": False, "closed": False, "consent": None,
                 "problems": [], "stage": "ERROR", "error": "RuntimeError: boom"}]
        stats = A.aggregate([rows], ["A"])
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["clusters"][("ERROR", "执行异常")], 1)


class RunRoundTest(unittest.TestCase):
    def test_exception_is_isolation_not_crash(self):
        """单条用例炸了，整批不能跟着炸——否则一条边界输入就丢掉全部数据。"""
        original = A.R.run_case

        def boom(case):
            if case["id"] == "BAD":
                raise RuntimeError("模拟状态机崩溃")
            return {}, []

        A.R.run_case = boom
        try:
            rows = A.run_round([_case("OK1"), _case("BAD")])
        finally:
            A.R.run_case = original

        self.assertEqual(len(rows), 2)
        bad = [r for r in rows if r["id"] == "BAD"][0]
        self.assertIn("模拟状态机崩溃", bad["error"])
        self.assertFalse(bad["ok"])


class ReportPathTest(unittest.TestCase):
    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            stem = "adversarial_20260101_0000_r3"
            p1 = A._unique_path(stem, ".md", tmp)
            open(p1, "w").close()
            p2 = A._unique_path(stem, ".md", tmp)
            self.assertNotEqual(p1, p2)
            self.assertTrue(p2.endswith("-2.md"))


class ExitCodeTest(unittest.TestCase):
    def _run(self, argv):
        """静音执行：单测只断言返回值，不把业务输出混进测试日志。"""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            return A.main(argv + ["--no-report", "--quiet"])

    def test_bad_cases_return_2(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump([{"id": "X01"}], f)
            self.assertEqual(self._run(["--cases", path, "--rounds", "1"]), 2)
        finally:
            os.remove(path)

    def test_bad_rounds_return_2(self):
        self.assertEqual(self._run(["--rounds", "0"]), 2)

    def test_fail_under_triggers_1(self):
        """全部失败 + 阈值 0.6 → 退出码必须是 1（CI 靠它卡口）。"""
        original = A.run_round
        A.run_round = lambda cases: [
            {"id": c["id"], "ok": False, "closed": False, "consent": None,
             "problems": ["闭环不符：期望 True 实际 False"], "stage": "OFFER", "error": ""}
            for c in cases]
        try:
            rc = self._run(["--rounds", "1", "--fail-under", "0.6"])
        finally:
            A.run_round = original
        self.assertEqual(rc, 1)

    def test_runtime_error_returns_3(self):
        """有执行异常 → 退出码 3，且优先于阈值判断。"""
        original = A.run_round
        A.run_round = lambda cases: [
            {"id": c["id"], "ok": False, "closed": False, "consent": None,
             "problems": [], "stage": "ERROR", "error": "RuntimeError: boom"}
            for c in cases]
        try:
            rc = self._run(["--rounds", "1", "--fail-under", "0.0"])
        finally:
            A.run_round = original
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
