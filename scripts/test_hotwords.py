# -*- coding: utf-8 -*-
"""热词功能测试：run-task 参数构造 + 建表请求体。纯离线，不联网不花钱。

守三件事：
  1. 没配 vocabulary_id 时，run-task 参数与改动前逐字节一致（默认行为不变）；
  2. 配了 vocabulary_id 时，它出现在 parameters 里，与 format/sample_rate 同级
     （官方文档位置），speaker_diarization 等原有参数不被挤掉；
  3. 建表请求体字段正确（target_model=v2、词去重、500 上限、prefix 校验）；
  4. 四场景配置都有 asr.hotwords 且默认空（不启用）。
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modules import realtime_asr  # noqa: E402

try:
    from scripts import setup_hotwords as sh
except ImportError:
    import setup_hotwords as sh  # noqa: E402


class HotwordParamsTest(unittest.TestCase):
    def test_default_params_unchanged(self):
        """没配热词时，参数就是原来那两个键——默认行为逐字节不变。"""
        self.assertEqual(realtime_asr.build_run_params({}),
                         {"format": "pcm", "sample_rate": 16000})

    def test_vocabulary_id_placed_in_parameters(self):
        """配了 vocabulary_id → 出现在 parameters 里（官方位置）。"""
        cfg = {"hotwords": {"vocabulary_id": "vocab-abc123"}}
        params = realtime_asr.build_run_params(cfg)
        self.assertEqual(params["vocabulary_id"], "vocab-abc123")
        self.assertEqual(params["format"], "pcm")
        self.assertEqual(params["sample_rate"], 16000)

    def test_empty_vocabulary_id_not_sent(self):
        """空字符串（配置默认）等于没配——不发热词参数。"""
        for v in ("", "   ", None):
            with self.subTest(v=v):
                cfg = {"hotwords": {"vocabulary_id": v}}
                self.assertNotIn("vocabulary_id", realtime_asr.build_run_params(cfg))

    def test_existing_params_not_dropped(self):
        """加热词不能挤掉原有的说话人分离参数。"""
        cfg = {"speaker_diarization": True,
               "hotwords": {"vocabulary_id": "vocab-x"}}
        params = realtime_asr.build_run_params(cfg)
        self.assertTrue(params["speaker_diarization_enabled"])
        self.assertEqual(params["vocabulary_id"], "vocab-x")


class HotwordRequestTest(unittest.TestCase):
    def test_request_body_shape(self):
        """建表请求符合官方 speech-biasing create_vocabulary 结构。"""
        body = sh.build_request(["京东", "授权书", "商标"])
        self.assertEqual(body["model"], "speech-biasing")
        inp = body["input"]
        self.assertEqual(inp["action"], "create_vocabulary")
        self.assertEqual(inp["target_model"], "paraformer-realtime-v2")
        self.assertEqual(inp["prefix"], "supercandy")
        words = inp["vocabulary"]
        self.assertEqual([w["text"] for w in words], ["京东", "授权书", "商标"])
        self.assertTrue(all(w["weight"] == 4 and w["lang"] == "zh" for w in words))

    def test_dedup_and_strip(self):
        """重复词、空白词被清理。"""
        body = sh.build_request(["京东", " 京东 ", "", "   ", "商标"])
        texts = [w["text"] for w in body["input"]["vocabulary"]]
        self.assertEqual(texts, ["京东", "商标"])

    def test_empty_words_rejected(self):
        with self.assertRaises(ValueError):
            sh.build_request(["", "  "])

    def test_500_limit(self):
        with self.assertRaises(ValueError):
            sh.build_request(["词%d" % i for i in range(501)])

    def test_bad_prefix_rejected(self):
        for bad in ("Upper", "has-dash", "toolongprefix1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sh.build_request(["京东"], prefix=bad)

    def test_extract_vocabulary_id(self):
        self.assertEqual(sh.extract_vocabulary_id({"output": {"vocabulary_id": "v1"}}), "v1")
        self.assertEqual(sh.extract_vocabulary_id({"vocabulary_id": "v2"}), "v2")
        self.assertEqual(sh.extract_vocabulary_id({}), "")

    def test_hints_loaded_from_config(self):
        """词源来自 asr.hints（已有的那份维护好的词表）。"""
        hints = sh.load_hints("config/scene.json")
        self.assertIn("京东", hints)
        self.assertGreater(len(hints), 5)


class HotwordConfigTest(unittest.TestCase):
    VID = "vocab-supercandy-5f7bde8f48a84fcfb514c66e8464ff39"

    def test_all_scenes_have_valid_hotwords_config(self):
        """四场景都有 asr.hotwords，vocabulary_id 要么为空要么是正确的表 ID。

        不强制"必须空"——热词表建好后就填 ID 启用了（见 scripts/setup_hotwords.py）。
        真正守"没配就不发参数"的是 HotwordParamsTest.test_empty_vocabulary_id_not_sent。
        这里守的是配置合法性：别把 phrase_id（v1 的）或拼错的值填进来。
        """
        for rel in ("config/scene.json", "config/scenes/pop_reject.json",
                    "config/scenes/retail_recall.json",
                    "config/scenes/service_outbound.json"):
            with self.subTest(f=rel):
                with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                    cfg = json.load(f)
                hw = cfg["asr"].get("hotwords")
                self.assertIsInstance(hw, dict, "%s 应有 asr.hotwords" % rel)
                vid = hw.get("vocabulary_id", "").strip()
                if vid:
                    self.assertTrue(
                        vid.startswith("vocab-"),
                        "%s 的 vocabulary_id 应是 vocab- 开头（v2），不是 %r" % (rel, vid))
                    self.assertEqual(vid, self.VID,
                                     "%s 应指向已建好的热词表" % rel)

    def test_realtime_params_include_configured_id(self):
        """当前配置已启用热词：run-task 参数会带上 vocabulary_id。"""
        import config_loader
        params = realtime_asr.build_run_params(config_loader.asr_config())
        self.assertEqual(params.get("vocabulary_id"), self.VID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
