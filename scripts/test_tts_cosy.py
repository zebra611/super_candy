"""换成 cosyvoice-v3-flash / 龙安欢 V3 的配置与请求体约束（11-13）。

用户截图是百炼「cosyvoice-v3-flash 音色列表」里的「龙安欢（V3）」
`longyingling_v3`。换它**确实要换模型**：端点、参数名、单价全变了。

这个文件不联网。真跑（用 config/secrets.json 里的 key 打一次接口、
下载音频确认是合法 mp3）单独做，结果记在 docs/61。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import voice  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENES = ["config/scene.json", "config/scenes/pop_reject.json",
          "config/scenes/retail_recall.json", "config/scenes/service_outbound.json"]

VOICE = "longyingling_v3"   # 龙应聆 V3：温和共情女（客服场景）

COSY = {
    "provider": "dashscope",
    "model": "cosyvoice-v3-flash",
    "voice_name": VOICE,
    "format": "mp3",
    "sample_rate": 24000,
}
QWEN = {
    "provider": "dashscope",
    "model": "qwen3-tts-flash",
    "voice_name": "Serena",
    "language_type": "Chinese",
}


class TestAllFourScenesAgree(unittest.TestCase):
    """四个场景各有自己的 tts 段，最容易改漏一个。

    线上走 pop_reject，但跑批 / 售后场景也用同一把 key；
    漏一个就会"主场景换了音色，切场景又变回去"。
    """

    def test_model_and_voice(self):
        for p in SCENES:
            d = json.load(open(os.path.join(_ROOT, p), encoding="utf-8"))
            t = d.get("tts") or {}
            self.assertEqual(t.get("model"), "cosyvoice-v3-flash", p)
            self.assertEqual(t.get("voice_name"), VOICE, p)
            self.assertIn("audio/tts/SpeechSynthesizer", t.get("endpoint") or "", p)
            self.assertNotIn("language_type", t,
                             "CosyVoice 没有 language_type：%s" % p)
            self.assertEqual(t.get("format"), "mp3", p)
            self.assertEqual(t.get("sample_rate"), 24000, p)

    def test_price_matches_new_model(self):
        """1 元/万字符 = 0.1 元/千字符（华北2 北京）。

        旧配置 0.08 是 qwen3-tts-flash 的价，不改会让看板成本低估 20%。
        **四个文件都要查**：11-13 换模型时只改了 config/scene.json，
        三个场景包的 budget 还是 0.08 —— 主场景对、切场景就少算 20%，
        正是这份"四个都同步"的说法把自己骗过去了。
        """
        for p in SCENES:
            d = json.load(open(os.path.join(_ROOT, p), encoding="utf-8"))
            self.assertEqual((d.get("budget") or {}).get("price_per_1k_chars_tts"),
                             0.1, "TTS 单价没跟着换模型：%s" % p)


class TestRequestBody(unittest.TestCase):
    """两个模型族的请求体不能混。"""

    def test_cosy_chinese_has_no_language_type(self):
        ep, body = voice.tts_request_body(COSY, "您好，请问您是安食官方旗舰店这边吗？")
        self.assertIn("audio/tts/SpeechSynthesizer", ep)
        self.assertEqual(body["model"], "cosyvoice-v3-flash")
        inp = body["input"]
        self.assertEqual(inp["voice"], VOICE)
        self.assertNotIn("language_type", inp,
                         "CosyVoice 不认 language_type，照旧塞进去有被判非法参数的风险")
        self.assertNotIn("language_hints", inp, "中文让模型自己判，不强行指定")
        self.assertEqual(inp["format"], "mp3")
        self.assertEqual(inp["sample_rate"], 24000)

    def test_cosy_english_gets_language_hints(self):
        """中英互切时英文句要按英文念，不能逐字母念成中文。"""
        _, body = voice.tts_request_body(
            COSY, "If you have any questions, please call 4006180092.")
        self.assertEqual(body["input"].get("language_hints"), ["en"])

    def test_short_english_is_still_english_for_tts(self):
        """短英文句（ok / yes）在 TTS 上也按英文，别把拉丁字母念成中文字母。"""
        _, body = voice.tts_request_body(COSY, "ok")
        self.assertEqual(body["input"].get("language_hints"), ["en"])

    def test_qwen_branch_kept_for_rollback(self):
        """换音色是配置决策：改回 qwen3-tts-flash 后，代码必须照旧能跑。"""
        ep, body = voice.tts_request_body(QWEN, "您好，这边核对一下申请单。")
        self.assertIn("multimodal-generation/generation", ep)
        self.assertEqual(body["input"].get("language_type"), "Chinese")
        self.assertNotIn("format", body["input"])
        _, en = voice.tts_request_body(QWEN, "Please call us back tomorrow.")
        self.assertEqual(en["input"].get("language_type"), "English")

    def test_cosy_default_endpoint_when_config_omits_it(self):
        """配置漏写 endpoint 时按模型族给默认值，不能回落到旧端点。"""
        cfg = dict(COSY)
        cfg.pop("endpoint", None)
        ep, _ = voice.tts_request_body(cfg, "您好")
        self.assertIn("audio/tts/SpeechSynthesizer", ep)


class TestAudioUrl(unittest.TestCase):
    """混合内容那道闸：OSS 给 http://，https 页面会直接拦掉播放。"""

    def test_http_upgraded(self):
        self.assertEqual(
            voice._https_audio_url("http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.mp3"),
            "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.mp3")

    def test_https_untouched(self):
        u = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.mp3"
        self.assertEqual(voice._https_audio_url(u), u)


class TestAuditionTool(unittest.TestCase):
    """换声之后，专门用来"试听换声音"的那个工具必须还能用。

    两个坑都是真跑撞出来的：
    1. 候选表原本全是 qwen3-tts 的音色，换模型后整表被接口逐条拒；
    2. 合成早就是分段返回（mode=segments），工具却只认 mode=url，
       任何音色都报"没有拿到音频链接"——看着像音色不对，其实是没读返回。
    """

    @classmethod
    def setUpClass(cls):
        cls.scripts = os.path.join(_ROOT, "scripts")
        if cls.scripts not in sys.path:
            sys.path.insert(0, cls.scripts)
        import audition_tts
        cls.A = audition_tts

    def test_candidates_follow_model_family(self):
        cosy = [n for n, _ in self.A.pick_candidates({"model": "cosyvoice-v3-flash"})]
        self.assertEqual(cosy[0], VOICE,
                         "线上音色必须排在默认表第一位，否则试听默认听不到它")
        self.assertIn(VOICE, cosy, "线上音色必须在默认表里")
        qwen = [n for n, _ in self.A.pick_candidates({"model": "qwen3-tts-flash"})]
        self.assertIn("Serena", qwen, "回滚到 qwen3-tts 时旧表要还能用")
        self.assertNotIn("Serena", cosy, "跨族音色会被接口拒，不许混进 CosyVoice 表")

    def test_voices_override_without_editing_code(self):
        got = self.A.pick_candidates({"model": "cosyvoice-v3-flash"},
                                     ["a_v3", "b_v3"])
        self.assertEqual([n for n, _ in got], ["a_v3", "b_v3"])

    def test_segments_are_downloadable(self):
        got = {"mode": "segments",
               "segments": [{"audio_url": "u1"}, {"audio_url": "u2"}]}
        self.assertEqual(self.A.audio_urls(got), ["u1", "u2"])

    def test_single_url_still_works(self):
        self.assertEqual(self.A.audio_urls({"mode": "url", "audio_url": "u"}), ["u"])

    def test_browser_fallback_says_why(self):
        """合成失败会退回浏览器播报，此时要报原因，不能吞成"没有链接"。"""
        with self.assertRaises(RuntimeError) as ctx:
            self.A.audio_urls({"mode": "browser", "error": "接口返回 400"})
        self.assertIn("400", str(ctx.exception))

    def test_strip_id3_keeps_audio(self):
        body = b"\xff\xfb\x90\x00" + b"\x00" * 16
        tag = b"ID3\x00\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10
        self.assertEqual(self.A.strip_id3(tag + body), body)
        self.assertEqual(self.A.strip_id3(body), body, "没有 ID3 头时原样返回")


if __name__ == "__main__":
    unittest.main(verbosity=2)
