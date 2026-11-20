# -*- coding: utf-8 -*-
"""P1-2 断言：code_only 簇能从候选落盘到 append-only 待办文件，且去重。

守两件事（缺一不可）：
  1. evolve.py propose 把 needs_code 簇写进候选 json 的 code_only 字段——
     以前只在终端 print，文件里没有，关掉终端待办就消失；
  2. auto_evolve.py 的 _collect_code_only_backlog 把各候选的 code_only 汇总成
     eval/reports/code_only_backlog.jsonl，且同一 (source,key,reason) 不重复写。

测试全程在临时目录跑，不碰真实候选/backlog。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import evolve as evo
from scripts import auto_evolve as ae


class CodeOnlyBacklogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sw_code_only_")
        self._cand_dir = os.path.join(self._tmp, "candidates")
        os.makedirs(self._cand_dir)
        self._backlog = os.path.join(self._tmp, "code_only_backlog.jsonl")
        self._orig = (ae._L2_DIR, ae._CODE_ONLY_BACKLOG)
        ae._L2_DIR = self._cand_dir
        ae._CODE_ONLY_BACKLOG = self._backlog

    def tearDown(self):
        ae._L2_DIR, ae._CODE_ONLY_BACKLOG = self._orig
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_cand(self, name, code_only):
        p = os.path.join(self._cand_dir, name)
        data = {"id": name[:-5], "patches": [], "code_only": code_only}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def test_evolve_propose_writes_code_only(self):
        """注入一个 needs_code 簇，propose 后候选文件里能查到它。"""
        from modules import cluster as cluster_mod
        key = "agent_no_progress|GREETING|no_wrong_person|not_me"
        needs = cluster_mod._NEEDS_CODE.get(key.split("|")[0])
        self.assertTrue(needs, "测试前提：该码位应属于 needs_code 簇")
        baseline = {"clusters": [{
            "key": key, "sessions": 7, "cause": "测试注入",
            "examples": ["证据"], "target": "",
        }]}
        base_path = os.path.join(self._tmp, "baseline.json")
        out_path = os.path.join(self._tmp, "cand.json")
        with open(base_path, "w", encoding="utf-8") as f:
            json.dump(baseline, f, ensure_ascii=False)
        rc = evo.cmd_propose({"from": base_path, "out": out_path})
        self.assertEqual(rc, 0)
        with open(out_path, encoding="utf-8") as f:
            cand = json.load(f)
        self.assertIn("code_only", cand, "候选必须有 code_only 字段")
        self.assertEqual(len(cand["code_only"]), 1)
        self.assertEqual(cand["code_only"][0]["key"], key)
        self.assertEqual(cand["code_only"][0]["reason"], needs)
        self.assertEqual(cand["code_only"][0]["sessions"], 7)

    def test_collect_writes_backlog_and_dedupes(self):
        """汇总函数落盘 JSONL，同一簇再跑一次不重复写。"""
        cluster = {"key": "agent_no_progress|IDENTITY|cant_remember|no_voice",
                   "reason": "槽位轮换", "sessions": 3}
        self._write_cand("adv_auto_001.json", [cluster])
        self._write_cand("auto_001.json", [])

        first = ae._collect_code_only_backlog()
        self.assertEqual(first, 1, "首轮应新增 1 条")
        with open(self._backlog, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], cluster["key"])
        self.assertEqual(rows[0]["source"], "adv_auto_001.json")
        self.assertEqual(rows[0]["sessions"], 3)
        self.assertIn("ts", rows[0])

        second = ae._collect_code_only_backlog()
        self.assertEqual(second, 0, "同簇再跑一次不应重复写")
        with open(self._backlog, encoding="utf-8") as f:
            self.assertEqual(len([x for x in f if x.strip()]), 1)

        # reason 变了（归因更新）应记一条新的
        updated = dict(cluster)
        updated["reason"] = "新归因"
        self._write_cand("adv_auto_001.json", [updated])
        added = ae._collect_code_only_backlog()
        self.assertEqual(added, 1)
        with open(self._backlog, encoding="utf-8") as f:
            self.assertEqual(len([x for x in f if x.strip()]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
