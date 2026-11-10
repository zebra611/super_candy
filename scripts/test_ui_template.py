"""双端界面模板的验收测试：清单完整、能打单文件、可复算。"""

import hashlib
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_ui_preview as B          # noqa: E402

UI = os.path.join(ROOT, "eval", "ui")


class ManifestTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(UI, "avatars.json"), encoding="utf-8") as f:
            self.items = json.load(f)["avatars"]

    def test_four_avatars_two_per_gender(self):
        self.assertEqual(len(self.items), 4)
        self.assertEqual(sum(1 for a in self.items if a["gender"] == "f"), 2)
        self.assertEqual(sum(1 for a in self.items if a["gender"] == "m"), 2)

    def test_every_avatar_has_asset_and_note(self):
        for a in self.items:
            self.assertTrue(os.path.isfile(os.path.join(UI, "assets", a["file"])), a["id"])
            self.assertTrue(a["note"].strip(), a["id"])
            self.assertIn("has_headset", a)

    def test_ids_unique(self):
        ids = [a["id"] for a in self.items]
        self.assertEqual(len(ids), len(set(ids)))


def build_now():
    with open(B.TEMPLATE, encoding="utf-8") as f:
        return B.build(f.read(), B.load_manifest())


class BuildTest(unittest.TestCase):
    def test_single_file_has_no_external_refs(self):
        html = build_now()
        self.assertNotIn('src="assets/', html, "还有外链图片，单文件会漏图")
        self.assertEqual(html.count("data:image/jpeg;base64,"), len(B.load_manifest()))

    def test_build_is_deterministic(self):
        a = build_now().encode()
        b = build_now().encode()
        self.assertEqual(hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest())

    def test_committed_preview_matches_build(self):
        """库里的 preview.html 必须**逐字节**等于重建结果。

        旧版只比「两次内存构建」——那永远相等，而产物不可复算的原因在
        **落盘那一步**：Windows 文本模式把 \n 写成 \r\n，重建一次
        1447 行全变（372660→373107 字节，+447 正好是行数）。
        """
        want = build_now().encode("utf-8")
        with open(os.path.join(UI, "preview.html"), "rb") as f:
            got = f.read()
        self.assertNotIn(b"\r\n", got, "产物混入 CRLF：重建会整文件 diff")
        self.assertEqual(
            hashlib.sha256(got).hexdigest(), hashlib.sha256(want).hexdigest(),
            "库内 preview.html 与重建结果不一致：跑 python3 scripts/build_ui_preview.py 重新生成")

    def test_staff_blocks_rendered(self):
        html = build_now()
        self.assertIn("const STAFF=", html)
        self.assertIn("renderStaff", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
