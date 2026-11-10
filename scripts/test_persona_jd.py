"""京东人物库的验收测试：广度、唯一性、可复算、字段齐备、能不能真跑。

为什么「可复算」要写死成 `build(320) == 库里那份`：
    初版是 `F.build(50) == F.build(50)`——纯函数两次调用必然相等，永远不可能红。
    实测把注释里说「已修」的强度轮换改回 `idx % 3`（会让 180 种语气组合塌缩成
    60 种），11 条测试**全绿**，而生成器已经和库里的文件分叉 214 条。
    覆盖类断言读的是磁盘上的文件，永远发现不了「生成器变质」。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "eval"))

import persona_factory_jd as F          # noqa: E402

PATH = os.path.join(ROOT, "eval", "personas_jd.json")


def load():
    with open(PATH, encoding="utf-8") as f:
        return json.load(f)["personas"]


def content_key(p):
    """除 id 外的全部内容：用来判「两条是不是同一个客户」。"""
    return json.dumps({k: v for k, v in p.items() if k != "id"},
                      ensure_ascii=False, sort_keys=True)


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.items = load()

    def test_size(self):
        self.assertGreaterEqual(len(self.items), 300)

    def test_professions_hundreds(self):
        n = len({i["profession"] for i in self.items})
        self.assertGreaterEqual(n, 150, "职业只有 %d 种" % n)

    def test_professions_not_split_by_space(self):
        """职业名里不能有空格——词表是按空格切的。

        初版写的「UI 设计师」「4S 店销售」被 `.split()` 切成「UI」「设计师」
        和「4S」「店销售」，于是 8 条画像的职业是「我是做4S的」。
        """
        ascii_only = [p for p in F.PROFESSIONS if p.isascii()]
        self.assertFalse(ascii_only,
                         "纯 ASCII 职业 %s：多半是多词职业被空格切开" % ascii_only)
        for fragment in ("设计师", "店销售"):
            self.assertNotIn(fragment, F.PROFESSIONS,
                             "「%s」是被切开的碎片，应写成完整的职业名" % fragment)

    def test_tone_variety(self):
        labels = len({i["tone"]["label"] for i in self.items})
        combos = len({(i["tone"]["label"], i["tone"]["level"]) for i in self.items})
        self.assertGreaterEqual(labels, 55, "语气标签只有 %d" % labels)
        self.assertGreaterEqual(combos, 150, "语气组合只有 %d" % combos)

    def test_attitudes(self):
        self.assertGreaterEqual(len({i["attitude"] for i in self.items}), 25)

    def test_languages_and_dialects(self):
        self.assertGreaterEqual(len({i["language"]["code"] for i in self.items}), 18)
        names = {i["language"]["name"] for i in self.items}
        self.assertIn("粤语", names)
        self.assertIn("四川话", names)

    def test_scenarios(self):
        self.assertEqual(len({i["scenario"]["id"] for i in self.items}), 10)

    def test_content_unique(self):
        """去 id 后必须条条不同。

        初版四个维度的最小正周期都是 180，`idx` 与 `idx+180` 产出逐字节相同的内容：
        320 条里 140 条是副本、只改了 id，而文档写的是「全量唯一」。
        """
        keys = [content_key(p) for p in self.items]
        dup = len(keys) - len(set(keys))
        self.assertEqual(dup, 0, "有 %d 条内容重复（先查各维度的周期）" % dup)


class ShapeTest(unittest.TestCase):
    def setUp(self):
        self.items = load()

    def test_required_fields(self):
        need = {"id", "scenario", "profession", "tone", "attitude", "language",
                "utterances", "expect", "tags"}
        for p in self.items:
            self.assertFalse(need - set(p), p["id"])

    def test_reply_guidance_present(self):
        """没有'得体回复要点'，人物库就只能挑错，说不出该怎么答。"""
        for p in self.items:
            self.assertTrue(p["scenario"]["reply_guide"].strip(), p["id"])
            self.assertTrue(p["scenario"]["need"].strip(), p["id"])

    def test_utterances_use_language_markers(self):
        for p in self.items:
            self.assertEqual(len(p["utterances"]), 4, p["id"])
            joined = " ".join(p["utterances"])
            marker = p["language"]["markers"][0]
            self.assertIn(marker, joined, "%s 没用上本语言的标志词" % p["id"])

    def test_ids_unique(self):
        ids = [p["id"] for p in self.items]
        self.assertEqual(len(ids), len(set(ids)))

    def test_hassle_matches_scenario(self):
        """台词第二句的「为什么这通电话重要」必须和场景对得上。

        初版写死「平时没空盯物流」，于是发票、退款、假货场景的客户也在说物流
        （96 条）。这类台词一旦拿去跑模拟，客服答物流就成了「答非所问」的假缺陷。
        """
        logistic = {"S01", "S02", "S09"}
        wrong = [p["id"] for p in self.items
                 if p["scenario"]["id"] not in logistic and "物流" in p["utterances"][1]]
        self.assertFalse(wrong, "%d 条非物流场景在说物流：%s" % (len(wrong), wrong[:3]))


class DeterminismTest(unittest.TestCase):
    def test_rebuild_equals_committed_file(self):
        """生成器重建 == 库里的文件（条数、顺序、台词全算）。

        旧版比的是 `build(50) == build(50)`，永远成立；生成器改坏、库没重生成，
        这条测试照样绿。改成和磁盘文件比对，生成器一动就红。
        """
        filed = load()
        built = F.build(len(filed))
        self.assertEqual(len(built), len(filed))
        for b, f in zip(built, filed):
            self.assertEqual(b, f, "%s 与生成器不一致：先跑 --n 重新生成"
                             % f.get("id"))


class SimAdapterTest(unittest.TestCase):
    """能不能真的注入 sim_eval 跑——不能注入的话，这 320 条只是素材。"""

    def setUp(self):
        self.sim = F.build_sim(load())

    def test_sim_schema_complete(self):
        need = {"id", "label", "goal", "split", "knows", "willing", "traits",
                "style", "catchphrases", "expect"}
        for p in self.sim:
            self.assertFalse(need - set(p), p["id"])

    def test_traits_in_range(self):
        for p in self.sim:
            t = p["traits"]
            self.assertEqual(set(t), {"hearing", "patience", "trust0",
                                      "trust_gate", "verbosity"}, p["id"])
            self.assertTrue(0.0 < t["hearing"] <= 1.0, p["id"])
            self.assertGreaterEqual(t["patience"], 1, p["id"])
            self.assertTrue(0.0 <= t["trust0"] <= 1.0, p["id"])

    def test_every_attitude_mapped(self):
        """30 种态度都要有信任档，漏一个就会在转换时 KeyError。"""
        self.assertEqual(set(F._TRUST0), set(F.ATTITUDES))

    def test_generated_sim_file_can_be_injected(self):
        """生成 3 条 sim 画像，真喂给 sim_eval 跑一遍（不是只查字段）。"""
        sample = self.sim[:3]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "jd_sim.json")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"personas": sample}, f, ensure_ascii=False)
            run = subprocess.run(
                [sys.executable, os.path.join(ROOT, "scripts", "sim_eval.py"),
                 "--personas", path],
                cwd=ROOT, capture_output=True, text=True, timeout=300)
        out = (run.stdout or "") + (run.stderr or "")
        self.assertEqual(run.returncode, 0, out[-400:])
        self.assertIn("总通过", out, "sim_eval 没跑起来：%s" % out[-300:])
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
