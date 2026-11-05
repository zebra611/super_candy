"""话术改写层的离线自测（不联网，全部用假响应）。

为什么单独一个文件：`docs/17` 第 3.4 节列了六种降级情况，
它们只在异常路径上出现，`verify.py` 覆盖不到（那条链路平时走不到）。
这六条是"外呼不哑火"的保险丝，必须能单独回归。

运行：
    python3 scripts/test_nlg.py
    python3 scripts/test_nlg.py -v      # 看每条用例名

设计约束：
    - 纯标准库，不引 pytest（项目零依赖，见 Dockerfile 说明）；
    - 不读真实密钥、不发真实请求，跑批不会产生费用；
    - 只测 nlg 自己，不碰对话流程。
"""

import json
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_loader                                          # noqa: E402
from modules import nlg                                       # noqa: E402


class _FakeResp:
    """最小可用的 urlopen 返回值：只实现 read() 和上下文管理。"""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_response(text):
    return _FakeResp({"choices": [{"message": {"content": text}}]})


CFG = {"provider": "dashscope", "model": "qwen-flash",
       "timeout_ms": 400, "max_chars": 45}
SESSION = {"session_id": "t", "stage": "IDENTITY",
           "slots": {"identity_confirmed": False}}
BASE = "为了核对身份，请说一下手机号后四位"


class TestDegradePaths(unittest.TestCase):
    """六种必须退回骨架话术的情况（docs/17 第 3.4 节）。"""

    def test_no_key_returns_base(self):
        """1. 没有 API Key —— 不能抛，必须原样返回。"""
        with mock.patch.object(config_loader, "secret", return_value=""):
            self.assertEqual(nlg.say(SESSION, BASE, "IDENTITY"), BASE)

    def test_timeout_returns_base(self):
        """2. 请求超时 —— 网络卡住时宁可话朴素，也不能让用户干等。"""
        with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
             mock.patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            self.assertEqual(nlg.say(SESSION, BASE, "IDENTITY"), BASE)

    def test_empty_reply_returns_base(self):
        """3. 返回空串或只有标点。"""
        for bad in ("", "   ", "。。。", "，", "！？"):
            with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
                 mock.patch("urllib.request.urlopen", return_value=_ok_response(bad)):
                self.assertEqual(nlg.say(SESSION, BASE, "IDENTITY"), BASE,
                                 "返回 %r 时应当退回骨架" % bad)

    def test_too_long_reply_returns_base(self):
        """4. 字数与骨架差 2 倍以上（模型开始解释人生）。"""
        long_text = BASE * 6
        with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
             mock.patch("urllib.request.urlopen", return_value=_ok_response(long_text)):
            self.assertEqual(nlg.say(SESSION, BASE, "IDENTITY"), BASE)

    def test_fabricated_facts_return_base(self):
        """5. 编造骨架里没有的金额/单号 —— 这是最危险的一条。"""
        for bad in ("为了核对身份，请说一下手机号后四位，这边补偿您 300 元",
                    "为了核对身份，请说一下手机号后四位，订单 20261101 快到了",
                    "为了核对身份，请说一下手机号后四位，有效期十二个月"):
            with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
                 mock.patch("urllib.request.urlopen", return_value=_ok_response(bad)):
                self.assertEqual(nlg.say(SESSION, BASE, "IDENTITY"), BASE,
                                 "编造事实未被拦住：%s" % bad)

    def test_http_error_returns_base(self):
        """6. 其他异常（含 429/403/5xx）。"""
        import urllib.error
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
             mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertEqual(nlg.say(SESSION, BASE, "IDENTITY"), BASE)

    def test_malformed_payload_returns_base(self):
        """响应结构不对（不是 OpenAI 兼容形状）也不能崩。"""
        with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
             mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp({"error": {"code": "x"}})):
            self.assertEqual(nlg.say(SESSION, BASE, "IDENTITY"), BASE)

    def test_template_provider_never_calls_network(self):
        """provider=template（默认）时一次网络都不发。"""
        with mock.patch.object(config_loader, "nlg_config",
                               return_value={"provider": "template"}), \
             mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("默认路径不该发请求")):
            self.assertEqual(nlg.say(SESSION, BASE, "IDENTITY"), BASE)


class TestAcceptPath(unittest.TestCase):
    """正常改写要被放行，不能过度拦截（这里最容易把功能做成摆设）。"""

    def test_normal_rewrite_accepted(self):
        good = "麻烦您说下手机号后四位，我核对一下身份。"
        with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
             mock.patch.object(config_loader, "nlg_config", return_value=CFG), \
             mock.patch("urllib.request.urlopen", return_value=_ok_response(good)):
            self.assertEqual(nlg.say(SESSION, BASE, "IDENTITY"), good)

    def test_small_number_not_blocked(self):
        """"后四位"这种小数字不能被当成编造拦掉（用中文数字更不该拦）。"""
        good = "麻烦您报一下手机号的后四位，我核对下身份～"
        with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
             mock.patch.object(config_loader, "nlg_config", return_value=CFG), \
             mock.patch("urllib.request.urlopen", return_value=_ok_response(good)):
            self.assertEqual(nlg.say(SESSION, BASE, "IDENTITY"), good)

    def test_quote_wrapped_output_unwrapped(self):
        """模型爱加引号，要剥掉再返回。"""
        good = "“麻烦您说下手机号后四位，我核对一下身份。”"
        with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
             mock.patch.object(config_loader, "nlg_config", return_value=CFG), \
             mock.patch("urllib.request.urlopen", return_value=_ok_response(good)):
            out = nlg.say(SESSION, BASE, "IDENTITY")
        self.assertFalse(out.startswith("“"), "首尾引号没剥掉：%r" % out)

    def test_truncated_to_max_chars(self):
        """超过 max_chars 要截断，且必须断在标点上（不能说半句）。

        骨架要选够短的：`docs/17` 第 3.4 节第 4 条要求"字数差 2 倍就退回"，
        骨架太长时改写会先被那条拦掉，max_chars 走不到。两条规则的适用区间不同。
        """
        base = "好的，已经帮您登记好了"
        reply = "好的，已经帮您登记好了，稍后会有短信通知您。"
        cfg = dict(CFG, max_chars=15)
        self.assertLess(len(reply), len(base) * 2 + 10, "用例本身要先过 2 倍检查")
        with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
             mock.patch.object(config_loader, "nlg_config", return_value=cfg), \
             mock.patch("urllib.request.urlopen", return_value=_ok_response(reply)):
            out = nlg.say(SESSION, base, "WRITEBACK")
        self.assertLessEqual(len(out), 15, "没截断到 max_chars：%r" % out)
        self.assertTrue(out.endswith(("。", "？", "！", "，", "、", "；")),
                        "截断后不该是半句：%r" % out)

    def test_unsplittable_reply_falls_back(self):
        """max_chars 内断不出完整分句时，必须退回骨架，而不是说半句话。"""
        base = "好的，请问您这次是希望我们帮您处理什么问题呢"
        reply = "好的，请问您这次是希望我们帮您处理什么问题呢，是订单还是权益方面的。"
        cfg = dict(CFG, max_chars=20)
        with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
             mock.patch.object(config_loader, "nlg_config", return_value=cfg), \
             mock.patch("urllib.request.urlopen", return_value=_ok_response(reply)):
            out = nlg.say(SESSION, base, "NEED")
        self.assertEqual(out, base, "说不完就应退回完整骨架，实际返回：%r" % out)

    def test_session_not_mutated(self):
        """session 只读：改写前后快照必须一致。"""
        before = json.dumps(SESSION, sort_keys=True, ensure_ascii=False)
        with mock.patch.object(config_loader, "secret", return_value="sk-test"), \
             mock.patch.object(config_loader, "nlg_config", return_value=CFG), \
             mock.patch("urllib.request.urlopen",
                        return_value=_ok_response("麻烦您说下后四位，我核对下身份。")):
            nlg.say(SESSION, BASE, "IDENTITY")
        self.assertEqual(before, json.dumps(SESSION, sort_keys=True, ensure_ascii=False),
                         "session 被改写了，违反 docs/17 第 3.1 节的只读约定")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
