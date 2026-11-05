"""工具适配层的离线自测（不联网、不起服务、不花钱）。

为什么单独一个文件：这几条是"接真实接口"的保险丝，`verify.py` 走的是
正向链路，覆盖不到失败路径——
    · 超时（对端慢）不能在 300ms 后还挂着，否则首字时延被拖垮；
    · 幂等（重试 / 改口）不能多建单，否则业务系统里两张单互相矛盾；
    · 回写格式（缺字段 / 类型错）必须在写之前拦下，否则脏数据落到工单里。

运行：
    python3 scripts/test_tools.py
    python3 scripts/test_tools.py -v

设计约束（同 test_nlg.py）：纯标准库、不引 pytest、不发真实请求。
RealAdapter 的错误码用"连一个关着的端口"来验，不需要真起 mock_business。
"""

import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_loader                                              # noqa: E402
from modules import adapters, tools                               # noqa: E402
from modules.adapters import base as abase                        # noqa: E402
from modules.adapters.base import (ERROR_TIMEOUT, ERROR_UPSTREAM,  # noqa: E402
                                   BaseAdapter)
from modules.adapters.mock import MockAdapter                     # noqa: E402
from modules.adapters.real import RealAdapter                     # noqa: E402


class AdapterSelection(unittest.TestCase):
    """provider 切换：默认必须是 mock，配置写错也要能跑起来。"""

    def tearDown(self):
        adapters.reset_adapter()

    def test_default_provider_is_mock(self):
        cfg = config_loader.tools_config()
        self.assertEqual(cfg.get("provider"), "mock",
                         "默认 provider 必须是 mock，否则没起 Mock 服务的队友跑不起来")
        self.assertIsInstance(adapters.get_adapter(fresh=True), MockAdapter)

    def test_switch_to_real(self):
        ad = adapters.get_adapter({"provider": "real", "base_url": "http://127.0.0.1:8100",
                                   "timeout_ms": 300}, fresh=True)
        self.assertIsInstance(ad, RealAdapter)
        self.assertEqual(ad.timeout_ms, 300)

    def test_unknown_provider_falls_back_to_mock(self):
        ad = adapters.get_adapter({"provider": "typo"}, fresh=True)
        self.assertIsInstance(ad, MockAdapter, "配置写错不该让整条链路起不来")


class MockParity(unittest.TestCase):
    """MockAdapter 必须与改造前的假数据逐字节一致。"""

    def setUp(self):
        self.ad = MockAdapter({"provider": "mock"})

    def test_query_customer_hit(self):
        res = self.ad.call("query_customer", "8866")
        self.assertTrue(res["found"])
        self.assertEqual(res["customer"]["customer_id"], "C1001")
        self.assertIn("_ms", res)

    def test_query_customer_miss_is_not_error(self):
        res = self.ad.call("query_customer", "0000")
        self.assertFalse(res["found"], "查不到是业务结果，不能变成 error")
        self.assertNotIn("error", res)

    def test_query_order(self):
        res = self.ad.call("query_order", "C1001")
        self.assertTrue(res["found"])
        self.assertEqual(res["order"]["order_id"], "O20261101001")


class Idempotency(unittest.TestCase):
    """T11：同一键重放不建单；改口是更新同一张单。"""

    def setUp(self):
        self.ad = MockAdapter({"provider": "mock"})
        self.content = config_loader.writeback_template()
        self.content["identity_confirmed"] = True
        self.content["need"] = "说明书看不懂"

    def test_replay_same_version(self):
        first = self.ad.call("create_ticket", "s-1", dict(self.content), "s-1:WRITEBACK:v1", 1)
        second = self.ad.call("create_ticket", "s-1", dict(self.content), "s-1:WRITEBACK:v1", 1)
        self.assertTrue(first["created"])
        self.assertEqual(second["ticket_id"], first["ticket_id"])
        self.assertFalse(second["created"], "同键重放不能建第二张")
        self.assertFalse(second["updated"])

    def test_correction_updates_same_ticket(self):
        v1 = self.ad.call("create_ticket", "s-2", dict(self.content), "s-2:WRITEBACK:v1", 1)
        v2 = self.ad.call("create_ticket", "s-2", dict(self.content), "s-2:WRITEBACK:v2", 2)
        self.assertEqual(v2["ticket_id"], v1["ticket_id"], "改口不能换工单号")
        self.assertTrue(v2["updated"])
        self.assertFalse(v2["created"])
        self.assertEqual(self.ad.ticket_for_session("s-2")["version"], 2)


class TimeoutBudget(unittest.TestCase):
    """T10：对端慢 1 秒，也必须 300ms 把控制权还回来。"""

    class _Slow(BaseAdapter):
        name = "slow"

        def query_customer(self, keyword):
            time.sleep(1.0)
            return {"found": True, "customer": {"name": "不该等到的结果"}}

        def query_order(self, customer_id):
            time.sleep(1.0)
            return {"found": True, "order": {}}

        def create_ticket(self, session_id, content, idempotency_key, version=1):
            time.sleep(1.0)
            return {"ticket_id": " troppo-tardi"}

    def test_timeout_returns_within_budget(self):
        ad = self._Slow({"timeout_ms": 300})
        t0 = time.time()
        res = ad.call("query_customer", "8866")
        elapsed = time.time() - t0
        self.assertLess(elapsed, 0.8, "300ms 预算没兜住，实际 %.2fs" % elapsed)
        self.assertEqual(res["error"], ERROR_TIMEOUT)
        self.assertFalse(res["found"], "查询类超时要走'没查到'分支，不能抛异常")
        self.assertNotIn("该等到的结果", str(res))

    def test_timeout_on_write_has_no_ticket(self):
        ad = self._Slow({"timeout_ms": 200})
        res = ad.call("create_ticket", "s-x", {}, "k", 1)
        self.assertEqual(res["error"], ERROR_TIMEOUT)
        self.assertNotIn("ticket_id", res)


class WritebackValidation(unittest.TestCase):
    """T12：格式不合规就不写，且要能说清缺什么。"""

    def setUp(self):
        tools._reset_for_test()
        self.content = config_loader.writeback_template()

    def test_template_passes(self):
        res = tools.validate_writeback(dict(self.content))
        self.assertTrue(res["ok"], res["errors"])
        self.assertLess(res["ms"], 5, "校验是纯内存操作，不该超过 5ms")

    def test_missing_required_field_rejected(self):
        bad = dict(self.content)
        bad.pop("need", None)
        res = tools.validate_writeback(bad)
        self.assertFalse(res["ok"])
        self.assertTrue(any("need" in e for e in res["errors"]), res["errors"])

    def test_wrong_type_rejected(self):
        bad = dict(self.content)
        bad["identity_confirmed"] = "yes"           # 应该是 bool
        res = tools.validate_writeback(bad)
        self.assertFalse(res["ok"])
        self.assertTrue(any("identity_confirmed" in e for e in res["errors"]))

    def test_enum_rejected(self):
        bad = dict(self.content)
        bad["call_result"] = "随便写的"
        res = tools.validate_writeback(bad)
        self.assertFalse(res["ok"])

    def test_extra_tracking_fields_allowed(self):
        """编排会往 content 里加 order_id / corrections / writeback_version，
        它们不是 schema 字段，但绝不能被拦——拦了整条闭环就断了。"""
        ok = dict(self.content)
        ok["order_id"] = "O20261101001"
        ok["writeback_version"] = 2
        ok["corrections"] = [{"field": "name", "from": "李", "to": "张"}]
        self.assertTrue(tools.validate_writeback(ok)["ok"])

    def test_dispatch_rejects_and_writes_nothing(self):
        bad = dict(self.content)
        bad.pop("consent", None)
        tc = {"name": "create_ticket",
              "args": {"session_id": "s-bad", "content": bad,
                       "idempotency_key": "s-bad:WRITEBACK:v1", "version": 1}}
        res = tools.dispatch(tc)
        self.assertEqual(res["error"], "WRITEBACK_INVALID")
        self.assertIsNone(tools.ticket_for_session("s-bad"), "校验失败不许落单")

    def test_dispatch_accepts_valid(self):
        tc = {"name": "create_ticket",
              "args": {"session_id": "s-ok", "content": dict(self.content),
                       "idempotency_key": "s-ok:WRITEBACK:v1", "version": 1}}
        res = tools.dispatch(tc)
        self.assertIn("ticket_id", res, res)


class RealAdapterErrors(unittest.TestCase):
    """T10：三类错误码要能区分。这里验"连不上"，不需要真起服务。"""

    def test_connection_refused_maps_to_upstream(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # 端口已随 with 关闭，此时连过去必然失败
        ad = RealAdapter({"provider": "real", "base_url": "http://127.0.0.1:%d" % port,
                          "timeout_ms": 300})
        res = ad.call("query_customer", "8866")
        self.assertIn(res["error"], (ERROR_UPSTREAM, ERROR_TIMEOUT), res)
        self.assertFalse(res["found"])

    def test_missing_argument(self):
        ad = RealAdapter({"provider": "real", "base_url": "http://127.0.0.1:1",
                          "timeout_ms": 300})
        res = ad.call("query_customer", "")
        self.assertEqual(res["error"], "INVALID_ARG")


class FunctionCalling(unittest.TestCase):
    """T13：schema 导出与函数调用入口（不是"让模型选工具"）。"""

    def setUp(self):
        import contracts
        from modules import tool_schema
        self.contracts = contracts
        self.ts = tool_schema
        tools._reset_for_test()

    def test_export_passes_format_check(self):
        ok, errs = self.ts.validate_export()
        self.assertTrue(ok, errs)

    def test_every_contract_tool_exported(self):
        names = {t["function"]["name"] for t in self.ts.to_openai_tools()}
        self.assertEqual(names, set(self.contracts.TOOL_SCHEMAS))

    def test_optional_param_not_required(self):
        create = [t for t in self.ts.to_openai_tools()
                  if t["function"]["name"] == "create_ticket"][0]
        self.assertNotIn("version", create["function"]["parameters"]["required"])
        self.assertIn("session_id", create["function"]["parameters"]["required"])

    def test_mcp_shape(self):
        mcp = self.ts.to_mcp_tools()
        self.assertTrue(all("inputSchema" in t for t in mcp))
        self.assertEqual({t["name"] for t in mcp}, set(self.contracts.TOOL_SCHEMAS))

    def test_call_function_with_json_string(self):
        res = self.ts.call_function("query_customer", '{"keyword": "8866"}')
        self.assertTrue(res["found"])

    def test_call_function_bad_json(self):
        res = self.ts.call_function("query_customer", "{oops")
        self.assertEqual(res["error"], "INVALID_ARG")

    def test_unknown_tool(self):
        res = self.ts.call_function("rm_rf", "{}")
        self.assertEqual(res["error"], "UNKNOWN_TOOL")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
