"""工具适配层基类 —— 契约、错误码、超时。

为什么要有这一层：
    `modules/tools.py` 现在是"假业务系统"，接真实接口时最怕两件事——
    一是数据源换了行为跟着变（流程被打断），二是接口慢了把首字时延拖垮。
    所以把"从哪拿数据"收进适配器，`tools.dispatch()` 的形状一个字都不动：

        provider=mock → MockAdapter（内置假数据，行为与今天逐字节一致）
        provider=real → RealAdapter（打 Mock 业务系统 / 真实接口）

三分边界（docs/17 §1）：这一层只管"事实从哪来"，不决定"该不该问、怎么措辞"。

错误码只分三类（赛题要求"业务不存在 / 参数非法 / 系统繁忙 要能区分"）：
    NOT_FOUND     业务上查不到（**不是异常**，走"没查到"话术分支）
    INVALID_ARG   调用方参数非法（我们自己的 bug 或编排给错参数）
    UPSTREAM_BUSY 对端 5xx / 连接失败 / 限流
另有 TIMEOUT：超过 tools.timeout_ms 预算（默认 300ms）。

超时为什么要用线程：`sleep(1)` 这种阻塞调用，光"事后看耗时"是拦不住的，
必须在预算到点时立刻把控制权还给编排层，否则用户对着话筒等 1 秒。
"""

from __future__ import annotations

import concurrent.futures as _futures
import time

# ---- 错误码 ----------------------------------------------------------------
ERROR_NOT_FOUND = "NOT_FOUND"
ERROR_INVALID_ARG = "INVALID_ARG"
ERROR_UPSTREAM = "UPSTREAM_BUSY"
ERROR_TIMEOUT = "TIMEOUT"
ERROR_UNKNOWN_TOOL = "UNKNOWN_TOOL"

#: 三类业务错误 + 超时，事件流里要能区分开
ERROR_CODES = (ERROR_NOT_FOUND, ERROR_INVALID_ARG, ERROR_UPSTREAM, ERROR_TIMEOUT)

DEFAULT_TIMEOUT_MS = 300
DEFAULT_BASE_URL = "http://127.0.0.1:8100"

#: 工具名 -> 适配器方法名。新增工具时只改这里 + contracts.TOOL_SCHEMAS。
TOOL_METHODS = {
    "query_customer": "query_customer",
    "query_order": "query_order",
    "query_application": "query_application",  # C5：POP 资质申请单定位
    "create_ticket": "create_ticket",
}

#: 只读方法：出错/超时时也要返回 `found=False`，让编排层走"查不到"分支，
#: 而不是把一次超时变成整通电话崩掉（赛题要求"失败不抛异常"）。
READ_METHODS = {
    "query_customer", "query_order", "query_application",
    "reject_catalog", "reject_script", "lookup_qa",
}

_POOL = _futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")


class ToolError(Exception):
    """带错误码的工具异常。适配器内部抛它，基类负责翻译成结构化返回。"""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def run_bounded(fn, timeout_ms, *args, **kwargs):
    """在 timeout_ms 内跑 fn；超时抛 ToolError(TIMEOUT)，**不等它跑完**。

    注：拿不回已经起跑的那条线程（Python 没有安全的强杀），但它会被丢弃，
    结果不再影响本次调用——对通话来说，"慢"和"没做"必须一样处理。
    """
    budget = max(1, int(timeout_ms or DEFAULT_TIMEOUT_MS))
    future = _POOL.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=budget / 1000.0)
    except _futures.TimeoutError:
        future.cancel()
        raise ToolError(ERROR_TIMEOUT, "工具调用超过 %dms" % budget)


def _ms(t0):
    return round((time.time() - t0) * 1000, 2)


class BaseAdapter:
    """三个工具的公共外壳：超时、错误码、耗时打点。

    子类只需实现 `query_customer` / `query_order` / `create_ticket` 三个方法，
    出错就 raise ToolError(code, msg)，**不要自己 return 错误 dict**。
    """

    name = "base"

    def __init__(self, cfg=None):
        cfg = dict(cfg or {})
        self.cfg = cfg
        try:
            self.timeout_ms = int(cfg.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        except (TypeError, ValueError):
            self.timeout_ms = DEFAULT_TIMEOUT_MS
        self.base_url = (cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

    # ---- 子类实现 ---------------------------------------------------------
    def query_customer(self, keyword):
        raise NotImplementedError

    def query_order(self, customer_id):
        raise NotImplementedError

    def create_ticket(self, session_id, content, idempotency_key, version=1):
        raise NotImplementedError

    def ticket_for_session(self, session_id):
        """这通电话最新一版工单（没有则 None）。只读，自检用。"""
        return None

    # ---- 只读的"资料类"接口：默认返回空结果，不抛异常 --------------------
    # 这三个接口给的是**官方资料包**里的静态内容（驳回场景目录、驳回话术、高频 QA）。
    # 默认实现放在基类：真实 provider 没接这几张表时，调用方拿到的是
    # {"found": False} 而不是 AttributeError——线上一通电话不该因为
    # "这版适配器还没实现某个查询"直接崩掉。
    def reject_catalog(self):
        return {"found": False, "items": [], "count": 0}

    def reject_script(self, code):
        return {"found": False, "scene": None}

    def lookup_qa(self, question):
        return {"found": False, "answer": "", "match": ""}

    # ---- 统一外壳 ---------------------------------------------------------
    def call(self, method, *args, **kwargs):
        """带超时与错误码的调用。返回 dict 必带 `_ms`。"""
        t0 = time.time()
        fn = getattr(self, method)
        try:
            out = run_bounded(fn, self.timeout_ms, *args, **kwargs)
        except ToolError as exc:
            return self._fail(exc.code, str(exc), t0, read_op=method in READ_METHODS)
        except Exception as exc:  # 适配器自己没兜住的，算对端问题
            return self._fail(ERROR_UPSTREAM, str(exc)[:160], t0,
                              read_op=method in READ_METHODS)
        if isinstance(out, dict):
            out.setdefault("_ms", _ms(t0))
            return out
        return out

    @staticmethod
    def _fail(code, message, t0, read_op=False):
        """错误统一形状。

        read_op=True 时补 `found=False`——查询类工具的超时/故障，对编排层而言
        和"查不到"是同一个分支（赛题要求的"失败不抛异常"）；但 `error` 字段
        保留，事件流里仍能区分是超时还是真的没这个人。
        """
        out = {"error": code, "message": message, "_ms": _ms(t0)}
        if read_op:
            out["found"] = False
        return out

    # ---- 便捷方法 ---------------------------------------------------------
    def error_count(self):  # pragma: no cover - 给看板/自检用的占位
        return getattr(self, "_errors", 0)
