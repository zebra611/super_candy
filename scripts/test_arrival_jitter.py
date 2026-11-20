# -*- coding: utf-8 -*-
"""到达间隔抖动指标测试（老师 11-20 口径纠正：TCP 不丢包，改测抖动）。

守四件事：
  1. 等间隔到达 → 抖动接近 0（正常网络）；
  2. 忽快忽慢到达 → 抖动显著升高（真实卡顿能被抓到）；
  3. 重复片不参与抖动计算（它不是真实到达节奏）；
  4. 抖动值写进 audio_stream 事件，且跨轮重置不污染。
纯离线，用注入时间戳代替真实 sleep，结果确定。
"""
import json
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app  # noqa: E402
import contracts  # noqa: E402

FRAME = b"\x00\x00" * contracts.FRAME_SAMPLES   # 一帧静音


class ArrivalJitterTest(unittest.TestCase):
    def setUp(self):
        self.sid = app.start_call()["session_id"]
        self.sess, _, _ = app._auth(self.sid, self._token())
        self.chunk = FRAME * 8      # 8 帧一片，标称 160ms

    def _token(self):
        # start_call 返回里带 read_token
        import app as A
        return A._SESSIONS[self.sid]["read_token"]

    def _feed_with_intervals(self, intervals):
        """按给定到达间隔（秒）喂片，第一片不计间隔。返回最后一片的统计。"""
        st = app._audio_state(self.sess)
        st["last_arrival_ts"] = 0.0
        st["transit"] = 0.0
        st["jitter_samples"] = 0
        base = time.time() - 10
        stat = None
        for i, gap_s in enumerate(intervals):
            if i > 0:
                # 注入"上一片到达时刻"，使本次到达间隔 = gap_s
                st["last_arrival_ts"] = time.time() - gap_s
            stat = app.ingest_audio_chunk(self.sess, i * 8, self.chunk)
        return stat

    def test_even_arrival_low_jitter(self):
        """等间隔 160ms 到达：抖动接近 0。"""
        stat = self._feed_with_intervals([0.0] + [0.16] * 30)
        self.assertLess(stat["jitter_ms"], 12.0,
                        "等间隔到达抖动应很低，实际 %.2fms" % stat["jitter_ms"])

    def test_bursty_arrival_high_jitter(self):
        """忽快忽慢到达：抖动显著高于等间隔。"""
        pattern = [0.0] + ([0.05, 0.30, 0.02, 0.35] * 8)
        stat = self._feed_with_intervals(pattern)
        self.assertGreater(stat["jitter_ms"], 40.0,
                           "忽快忽慢应被抓到，实际仅 %.2fms" % stat["jitter_ms"])

    def test_jitter_compares_to_even(self):
        """同样片数下，抖动的样本数正确累计。"""
        stat = self._feed_with_intervals([0.0] + [0.16] * 19)
        self.assertEqual(stat["jitter_samples"], 19)

    def test_duplicate_not_counted(self):
        """重复片（seq 倒退）不参与抖动：jitter_samples 不增加。"""
        self._feed_with_intervals([0.0, 0.16, 0.16])
        before = app._audio_state(self.sess)["jitter_samples"]
        app.ingest_audio_chunk(self.sess, 0, self.chunk)   # 重复旧片
        after = app._audio_state(self.sess)["jitter_samples"]
        self.assertEqual(after, before, "重复片不该计入抖动样本")

    def test_jitter_written_to_event_and_reset(self):
        """抖动写进 audio_stream 事件；跨轮重置后归零。"""
        self._feed_with_intervals([0.0] + [0.16] * 10 + [0.4] * 5)
        pcm, stat = app.flush_audio(self.sess, dropped=0)
        self.assertIn("jitter_ms", stat)
        self.assertGreater(stat["jitter_ms"], 0.0)
        # flush 后状态归零
        st = app._audio_state(self.sess)
        self.assertEqual(st["transit"], 0.0)
        self.assertEqual(st["last_arrival_ts"], 0.0)
        # 下一轮重新喂等间隔，抖动从零起算，不继承上一轮
        stat2 = self._feed_with_intervals([0.0] + [0.16] * 20)
        self.assertLess(stat2["jitter_ms"], 12.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
