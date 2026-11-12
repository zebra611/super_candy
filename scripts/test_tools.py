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

# 这套用例测的是**售后回访场景**（台词是"用着还行""权益""上门""订单"，
# 知识库条目也是售后那批）。默认场景已换成赛题 POP 资审，不钉死场景就会全红。
# 必须钉在 import config_loader **之前**——它的配置路径在 import 时就解析好了。
os.environ.setdefault("APP_SCENE", "service_outbound")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_loader                                              # noqa: E402
from modules import adapters, tools                               # noqa: E402
from modules.adapters import base as abase                        # noqa: E402
from modules.adapters.base import (ERROR_TIMEOUT, ERROR_UNKNOWN_TOOL,  # noqa: E402
                                   ERROR_UPSTREAM, BaseAdapter)
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

    def test_unimplemented_tool_returns_error_instead_of_raising(self):
        """契约里有、适配器没实现的工具必须返回错误码，不许抛异常。

        11-12 实测：`send_checklist` 只做在 MockAdapter 上，切到 real 调它
        直接抛 AttributeError。base.call 里的 getattr 原来在 try 外面，
        兜底接不住，异常冒到编排层——正好破了 dispatch 承诺的"失败不抛异常"。
        这条既是回归，也是给以后新增工具留的门：漏实现不会炸整通电话。
        """
        class _Incomplete(BaseAdapter):
            """故意什么都不实现：模拟"契约加了工具、适配器还没跟上"。"""
            name = "incomplete"

        ad = _Incomplete({"timeout_ms": 100})
        # 写类：没有 found 语义，只要求给错误码
        res = ad.call("send_checklist", "s1", "QZ202611060001", ["材料A"])
        self.assertEqual(res.get("error"), ERROR_UNKNOWN_TOOL, res)
        self.assertIn("_ms", res)
        self.assertIn("incomplete", res.get("message", ""), "报错要能看出是哪个适配器")
        # 读类（READ_METHODS 里的）：额外补 found=False，
        # 否则编排层拿不到"查不到"这个分支，一次未实现会变成整通电话断。
        res2 = ad.call("query_application", "QZ202611060001")
        self.assertEqual(res2.get("error"), ERROR_UNKNOWN_TOOL, res2)
        self.assertFalse(res2.get("found", True), res2)


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


class IdempotencyPersistence(unittest.TestCase):
    """T11 补：幂等落库之后，**进程重启**仍然成立。

    这条是 `docs/14` 待改进第 5 条的验收：原来是内存 dict，重启即失效，
    于是"同一键重放不建单"只在同一个进程里成立——线上重启一次就漏。
    """

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp(prefix="sc_tickets_")
        self.db = os.path.join(self.tmpdir, "tickets.db")
        self.cfg = {"provider": "mock", "timeout_ms": 300,
                    "idempotency_store": "sqlite", "idempotency_path": self.db}
        self.content = config_loader.writeback_template()
        self.content["need"] = "说明书看不懂"
        self.content["identity_confirmed"] = True

    def tearDown(self):
        adapters.reset_adapter()

    def _restart(self):
        """模拟进程退出 + 重启：清缓存、同一个 db 文件、全新实例。"""
        adapters.reset_adapter()
        return adapters.get_adapter(self.cfg, fresh=True)

    def test_replay_survives_restart(self):
        a1 = adapters.get_adapter(self.cfg, fresh=True)
        first = a1.call("create_ticket", "s-9", dict(self.content), "s-9:WRITEBACK:v1", 1)
        self.assertTrue(first["created"])

        a2 = self._restart()
        again = a2.call("create_ticket", "s-9", dict(self.content), "s-9:WRITEBACK:v1", 1)
        self.assertEqual(again["ticket_id"], first["ticket_id"])
        self.assertFalse(again["created"], "重启后同键重放不能建第二张")

    def test_correction_version_survives_restart(self):
        a1 = adapters.get_adapter(self.cfg, fresh=True)
        a1.call("create_ticket", "s-8", dict(self.content), "s-8:WRITEBACK:v1", 1)
        a2 = self._restart()
        a2.call("create_ticket", "s-8", dict(self.content), "s-8:WRITEBACK:v2", 2)
        a3 = self._restart()

        latest = a3.ticket_for_session("s-8")
        self.assertIsNotNone(latest, "重启后还要能查到这张单")
        self.assertEqual(latest["version"], 2, "重启后最新版本要保留")
        self.assertTrue(os.path.exists(self.db), "落库文件要真的存在")

    def test_reset_does_not_wipe_persistent_store(self):
        a1 = adapters.get_adapter(self.cfg, fresh=True)
        a1.call("create_ticket", "s-7", dict(self.content), "s-7:WRITEBACK:v1", 1)
        adapters.reset_adapter()          # 自检脚本会调，但**不能**抹掉落库的单
        a2 = adapters.get_adapter(self.cfg, fresh=True)
        self.assertIsNotNone(a2.ticket_for_session("s-7"))

    def test_memory_is_still_the_default(self):
        ad = adapters.get_adapter({"provider": "mock"}, fresh=True)
        self.assertIsInstance(ad._tickets, dict, "默认仍然是内存表，clone 下来零依赖")
        self.assertFalse(getattr(ad, "_persistent", False))


class ReviewFixes(unittest.TestCase):
    """阶段0 评审提的三条：超长诉求、枚举类型、real 的 ticket_for_session。"""

    def setUp(self):
        tools._reset_for_test()
        self.content = config_loader.writeback_template()

    def test_long_need_is_truncated_not_rejected(self):
        """P3：129 字诉求要能落单（截断），不能整单被拒。"""
        content = dict(self.content)
        content["need"] = "我" * 129
        tc = {"name": "create_ticket",
              "args": {"session_id": "s-long", "content": content,
                       "idempotency_key": "s-long:WRITEBACK:v1", "version": 1}}
        res = tools.dispatch(tc)
        self.assertIn("ticket_id", res, "超长诉求不能把整张单拒掉：%r" % (res,))
        self.assertTrue(res.get("truncated"), "要告诉调用方截断过")
        ticket = tools.ticket_for_session("s-long")
        self.assertIsNotNone(ticket)
        self.assertEqual(len(ticket["content"]["need"]), 100, "截断到 schema 上限")

    def test_consent_zero_is_rejected(self):
        """P8：0 == False 在 Python 里成立，枚举必须按类型比。"""
        bad = dict(self.content)
        bad["consent"] = 0
        res = tools.validate_writeback(bad)
        self.assertFalse(res["ok"], "consent=0 不是 bool 枚举成员")

    def test_consent_false_still_ok(self):
        ok = dict(self.content)
        ok["consent"] = False
        self.assertTrue(tools.validate_writeback(ok)["ok"])

    def test_real_ticket_for_session_returns_content(self):
        """P6：real 模式以前固定返回 content={} version=0，断言在静默错值。"""
        ad = RealAdapter({"provider": "real", "base_url": "http://127.0.0.1:1",
                          "timeout_ms": 300})
        ad._session_tickets["s-r"] = "T0001"
        ad._last_ticket["s-r"] = {"content": {"need": "屏幕碎了"}, "version": 2}
        ticket = ad.ticket_for_session("s-r")
        self.assertEqual(ticket["ticket_id"], "T0001")
        self.assertEqual(ticket["content"]["need"], "屏幕碎了")
        self.assertEqual(ticket["version"], 2)

if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
