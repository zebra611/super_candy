# -*- coding: utf-8 -*-
"""热词功能测试：run-task 参数构造 + 建表请求体。纯离线，不联网不花钱。

守三件事：
  1. 没配 vocabulary_id 时，run-task 参数与改动前逐字节一致（默认行为不变）；
  2. 配了 vocabulary_id 时，它出现在 parameters 里，与 format/sample_rate 同级
     （官方文档位置），speaker_diarization 等原有参数不被挤掉；
  3. 建表请求体字段正确（target_model=v2、词去重、500 上限、prefix 校验）；
  4. 四场景配置都有 asr.hotwords 且指向已建好的表；
  5. 商家信息分层提取：排除全部数字、人名降权、核心短词优先、去重、≤500。
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


class MerchantWordsTest(unittest.TestCase):
    """P：商家信息进热词表——提精度但不让非相关词被强行识别。"""

    def test_no_digits_at_all(self):
        """用户铁律：所有数字（单号/手机号/后四位/带序号品牌）一律不进表。"""
        words, _stats = sh.collect_merchant_words(["京东", "4006180092"])
        bad = [w for w, _weight in words if any(c.isdigit() for c in w)]
        self.assertEqual(bad, [], "热词表里不该有任何数字：%s" % bad[:5])
        # 具体确认那几个数字字段的值没混进来
        texts = {w for w, _ in words}
        for must_not in ("4006180092", "QZ202611060001", "8866"):
            self.assertNotIn(must_not, texts)

    def test_merchant_words_included(self):
        """商家品牌/简称/公司全称确实进表了（用户要的"方便核查"）。"""
        words, stats = sh.collect_merchant_words([])
        texts = {w for w, _ in words}
        self.assertIn("优光", texts, "品牌简称应进表")
        self.assertIn("乐人家", texts, "品牌名应进表")
        self.assertGreater(stats["in_table"], 400)

    def test_weight_tiers_core_higher_than_name_ext(self):
        """核心短词权重高于人名和长词——降低撞音误识别风险。"""
        words, _stats = sh.collect_merchant_words(["京东"])
        wmap = dict(words)
        self.assertEqual(wmap.get("京东"), sh._W_HINTS, "通用业务词应最高权重必保")
        # 人名若进表，权重必须低于核心层
        name_words = [w for w, _ in sh.collect_merchant_words([])[0]
                      if w in {"刘军", "张伟", "刘敏"}]
        for nw in name_words:
            self.assertEqual(wmap.get(nw), sh._W_NAME,
                             "人名 %s 应低权重" % nw)
        self.assertLess(sh._W_NAME, sh._W_CORE)

    def test_dedup_and_500_cap(self):
        """去重 + 不超 500 + 核心短词优先（裁剪先砍长词）。"""
        words, stats = sh.collect_merchant_words(["京东"])
        texts = [w for w, _ in words]
        self.assertEqual(len(texts), len(set(texts)), "不该有重复词")
        self.assertLessEqual(len(texts), sh._MAX_WORDS)
        self.assertEqual(stats["in_table"], len(texts))
        self.assertEqual(stats["dropped"], stats["unique"] - len(texts))
        # 传入的 hints 是最高权重，必须在裁剪中存活
        self.assertIn("京东", texts)

    def test_all_business_hints_survive_cap(self):
        """回归上一版的真 bug：满表裁剪时 3 字业务词不能被 2 字品牌挤掉。"""
        hints = sh.load_hints("config/scene.json")
        words, _stats = sh.collect_merchant_words(hints)
        texts = {w for w, _ in words}
        for h in hints:
            if any(c.isdigit() for c in h):
                continue  # 数字（4006180092）按用户铁律本就该排除
            with self.subTest(h=h):
                self.assertIn(h, texts, "业务词 %s 不该在 500 裁剪中丢失" % h)

    def test_short_words_before_long_on_cap(self):
        """同权重下短词排在长词前——500 满了先裁长词（ASR 对长词本就弱）。"""
        words, _stats = sh.collect_merchant_words([], max_words=300)
        w2 = [(w, len(w)) for w, weight in words if weight == sh._W_EXT]
        if len(w2) > 2:
            lens = [n for _w, n in w2]
            self.assertEqual(lens, sorted(lens), "扩展层应按词长升序")

    def test_overlong_words_filtered(self):
        """超长公司全称不进表：官方报 vocabulary format invalid 且 ASR 对长词无效。"""
        words, _stats = sh.collect_merchant_words([], include_names=False)
        over = [w for w, _ in words if len(w) > sh._MAX_WORD_LEN]
        self.assertEqual(over, [], "不该有超过 %d 字的词：%s" % (sh._MAX_WORD_LEN, over[:3]))

    def test_build_request_accepts_per_word_weight(self):
        """build_request 支持 (词, 权重) 元组，逐词权重生效。"""
        body = sh.build_request([("京东", 4), ("张伟", 2)])
        vocab = body["input"]["vocabulary"]
        self.assertEqual([v["weight"] for v in vocab], [4, 2])


class HotwordConfigTest(unittest.TestCase):
    VID = "vocab-scnn6-9df9c195fbcc4c0c801efc9a0a57aafa"

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

    def test_config_uses_full_merchant_table(self):
        """当前默认应是 260 词全商家表——实测热词数量不影响识别时延，
        没必要为了逐字字幕抖动牺牲覆盖度（见 e2e_first_audio 与 latency 对照实验）。"""
        import config_loader
        vid = config_loader.asr_config().get("hotwords", {}).get("vocabulary_id", "")
        self.assertEqual(vid, self.VID,
                         "默认应启用完整商家词表；若切到小表需在答辩说明覆盖度下降")

    def test_realtime_params_include_configured_id(self):
        """当前配置已启用热词：run-task 参数会带上 vocabulary_id。"""
        import config_loader
        params = realtime_asr.build_run_params(config_loader.asr_config())
        self.assertEqual(params.get("vocabulary_id"), self.VID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
