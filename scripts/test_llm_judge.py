# -*- coding: utf-8 -*-
"""P1-1 断言：LLM-as-Judge 可复现、temperature=0、失败回落规则分。

全程用注入的假 HTTP（不联网、不花钱、不依赖真密钥）：
    - 同输入判两次必须同分（可复现性，官方硬要求）；
    - 请求体 temperature=0，模型名与 rubric 版本进结果，prompt hash 稳定；
    - 没密钥 / 网络失败 / 输出不是 JSON → 回落规则分，标 judge_fallback。
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader  # noqa: E402
from modules import llm_judge as lj  # noqa: E402

EVENTS = [
    {"type": "asr_result", "payload": {"text": "我想问一下驳回原因"}},
    {"type": "agent_reply", "payload": {"stage": "NEED", "text": "原因是材料不全"}},
    {"type": "writeback", "payload": {"ticket_id": "T001"}},
]
RULE = {"closed": True, "compliance_ok": True, "quality_score": 95, "ok": True}


def fake_ok_post(url, body, headers, timeout):
    """稳定的假模型：按 prompt 长度给分，保证同输入同输出。"""
    score = 80 if "材料不全" in body["messages"][0]["content"] else 40
    return {"choices": [{"message": {"content": json.dumps({
        "closed": True, "compliance_ok": True,
        "quality_score": score, "reason": "流程完整",
    }, ensure_ascii=False)}}]}


class LlmJudgeTest(unittest.TestCase):
    def setUp(self):
        self._key = os.environ.get("DASHSCOPE_API_KEY")
        os.environ["DASHSCOPE_API_KEY"] = "sk-test-validator"
        self.cfg = {"mode": "both", "model": "qwen-flash",
                    "rubric_version": "r1", "rubric": lj._DEFAULT_RUBRIC,
                    "endpoint": "https://example.invalid/chat", "timeout_s": 5}

    def tearDown(self):
        if self._key is None:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        else:
            os.environ["DASHSCOPE_API_KEY"] = self._key

    def test_reproducible_same_input_same_score(self):
        """同一会话判两次：分数、hash、模型名、理由完全一致。"""
        j = lj.LlmJudge(self.cfg, http_post=fake_ok_post)
        a = j.judge(EVENTS, RULE)
        b = j.judge(EVENTS, RULE)
        self.assertEqual(a["judge"], "llm")
        self.assertEqual(a["quality_score"], b["quality_score"])
        self.assertEqual(a["prompt_sha256"], b["prompt_sha256"])
        self.assertEqual(a["model"], "qwen-flash")
        self.assertEqual(a["rubric_version"], "r1")
        self.assertFalse(a["judge_fallback"])

    def test_temperature_zero_and_prompt_hash_stable(self):
        """请求体 temperature=0；prompt hash 由 build_prompt 唯一决定。"""
        seen = {}

        def capture(url, body, headers, timeout):
            seen["body"] = body
            return fake_ok_post(url, body, headers, timeout)

        j = lj.LlmJudge(self.cfg, http_post=capture)
        j.judge(EVENTS, RULE)
        self.assertEqual(seen["body"]["temperature"], 0)
        self.assertEqual(seen["body"]["model"], "qwen-flash")
        prompt = lj.build_prompt(EVENTS, self.cfg["rubric"])
        expected_hash = lj._prompt_hash(prompt, "qwen-flash", "r1")
        self.assertEqual(expected_hash, lj._prompt_hash(prompt, "qwen-flash", "r1"))
        self.assertEqual(len(expected_hash), 16)   # 16 位 hex 摘要
        # 事件顺序变了，prompt 就得变——证明 hash 真的绑在输入上
        changed = lj._prompt_hash(lj.build_prompt(EVENTS[::-1], self.cfg["rubric"]),
                                  "qwen-flash", "r1")
        self.assertNotEqual(changed, expected_hash)

    def test_fallback_when_no_key(self):
        """没密钥 → 回落规则分，标 fallback，理由写明原因。"""
        orig = config_loader.secret
        config_loader.secret = lambda name, default="": ""
        try:
            j = lj.LlmJudge(self.cfg, http_post=fake_ok_post)
            out = j.judge(EVENTS, RULE)
        finally:
            config_loader.secret = orig
        self.assertEqual(out["judge"], "rule_fallback")
        self.assertTrue(out["judge_fallback"])
        self.assertTrue(out["closed"])                 # 取规则判定
        self.assertEqual(out["quality_score"], 95)
        self.assertIn("dashscope_api_key", out["fallback_reason"])

    def test_fallback_on_network_error(self):
        """网络失败 → 回落，不抛异常。"""
        def boom(url, body, headers, timeout):
            raise OSError("connection refused")

        j = lj.LlmJudge(self.cfg, http_post=boom)
        out = j.judge(EVENTS, RULE)
        self.assertEqual(out["judge"], "rule_fallback")
        self.assertIn("connection refused", out["fallback_reason"])

    def test_fallback_on_bad_json(self):
        """模型输出不是 JSON → 回落。"""
        def bad(url, body, headers, timeout):
            return {"choices": [{"message": {"content": "对不起我不能回答"}}]}

        j = lj.LlmJudge(self.cfg, http_post=bad)
        out = j.judge(EVENTS, RULE)
        self.assertTrue(out["judge_fallback"])

    def test_judge_session_respects_mode(self):
        """mode=rule 时 judge_session 直接返回 None，不调模型。"""
        self.cfg["mode"] = "rule"
        # 临时替换 judge_config 返回 rule 模式
        orig = lj.judge_config
        lj.judge_config = lambda: self.cfg
        try:
            self.assertIsNone(lj.judge_session(EVENTS, RULE))
        finally:
            lj.judge_config = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
