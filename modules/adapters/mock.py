"""MockAdapter —— 内置假数据（行为与改造前的 modules/tools.py 逐字节一致）。

这不是"临时凑数"：默认 provider 必须是 mock，否则队友 clone 下来
没有 Mock 服务就起不来，自检和跑批也会依赖外部进程（docs/17 §3.2 同样的理由）。

所以这里的三个工具**故意不联网**，返回的字段名、字段顺序、`_ms` 口径
都和原实现保持一致——`scripts/verify.py` 的断言才不会被"我只是重构了一下"弄红。

幂等：键=idempotency_key 存已建工单；同一通电话（session_id）复用同一个工单号，
版本递增时是"更新同一张"而不是"再开一张"。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time

from .base import BaseAdapter

# 假客户库：模拟企业提供的脱敏名单。
_FAKE_CUSTOMERS = [
    {"customer_id": "C1001", "name": "张伟", "phone_tail": "8866", "level": "金卡"},
    {"customer_id": "C1002", "name": "李娜", "phone_tail": "2143", "level": "普通"},
]

# 假订单库：模拟订单系统。
_FAKE_ORDERS = {
    "C1001": {"order_id": "O20261101001", "amount": 2680.0, "status": "已签收"},
    "C1002": {"order_id": "O20261101002", "amount": 399.0, "status": "运输中"},
}

# C5：POP 资质驳回申请单档案（120 份虚构模拟数据，见 config/mock/SCHEMA.md）。
# 懒加载 + 模块级缓存：读一次 282KB，之后每轮核身都是内存四路径匹配，
# 不会把首字时延拖进 tools.timeout_ms 的 300ms 预算。
_APPLICATIONS = None
_APPLICATIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "mock", "applications.json")


def _load_applications():
    global _APPLICATIONS
    if _APPLICATIONS is None:
        try:
            with open(_APPLICATIONS_PATH, encoding="utf-8") as f:
                _APPLICATIONS = json.load(f).get("items") or []
        except (OSError, ValueError):
            # 档案缺失/损坏时降级成"查无申请"，不能让核身链路直接崩掉
            _APPLICATIONS = []
    return _APPLICATIONS


def _ms(t0):
    return round((time.time() - t0) * 1000, 2)


class MockAdapter(BaseAdapter):
    name = "mock"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        # 幂等存储：真实实现应换成数据库唯一索引，骨架阶段用内存 dict。
        # _TICKETS           键 = 幂等键，值 = 已生成的工单（同一键重复调用不再写）
        # _TICKET_BY_SESSION 键 = 会话 ID，值 = 这通电话的工单号（改口更新同一张，不新开）
        self._tickets = {}
        self._ticket_by_session = {}

    # ---- 读 -----------------------------------------------------------------
    def query_customer(self, keyword):
        """按姓名或手机尾号查客户。返回 dict(found, customer)。

        TODO 真实接口要处理：同名多结果、号码脱敏、无权限字段裁剪。
        """
        t0 = time.time()
        kw = (keyword or "").strip()
        for c in _FAKE_CUSTOMERS:
            if kw and (kw == c["name"] or kw == c["phone_tail"]):
                return {"found": True, "customer": c, "_ms": _ms(t0)}
        return {"found": False, "customer": None, "_ms": _ms(t0)}

    def query_order(self, customer_id):
        """查客户最近一笔订单。返回 dict(found, order)。"""
        t0 = time.time()
        order = _FAKE_ORDERS.get(customer_id)
        return {"found": bool(order), "order": order, "_ms": _ms(t0)}

    def query_application(self, keyword):
        """C5：四路径定位 POP 资质驳回申请单，返回 dict(found, application)。

        四条路径与档案的 verify_by_* 字段一一对应；公司名允许「简称/全称子串」
        （真实语音里商家常只报字号 + 有限公司，不会逐字念全称）。
        精确匹配优先，长子串次之——避免短字号把多条档案同时捞出来。
        """
        t0 = time.time()
        kw = (keyword or "").strip()
        items = _load_applications()
        if kw:
            exact = ("verify_by_name", "verify_by_tail",
                     "verify_by_company", "verify_by_application")
            for key in exact:
                for rec in items:
                    if kw == rec.get(key):
                        return {"found": True, "application": rec,
                                "matched_by": key, "_ms": _ms(t0)}
            if len(kw) >= 4:
                for key in ("verify_by_company", "company_short_name", "shop_name"):
                    hits = [r for r in items if kw in str(r.get(key) or "")]
                    if len(hits) == 1:
                        return {"found": True, "application": hits[0],
                                "matched_by": key + ":contains", "_ms": _ms(t0)}
        return {"found": False, "application": None, "_ms": _ms(t0)}

    def next_outbound_target(self):
        """外呼名单：取下一条**要打电话**的申请单。

        为什么需要这个：真实外呼是从名单打的——电话打给哪家，系统本来就知道。
        官方 52 条话术因此是"请问您是（商家公司名/店铺名）…吗？"，而不是
        "请问是贵公司的负责人吗？"（后者连是谁都不知道，商家凭什么信）。

        之前系统里根本没有"本通电话的目标"这个概念：query_application 只在
        核身时按用户给的关键词盲查全部档案，所以开场白填不出公司名。

        选取规则：关单方式=电话（京麦驳回的不外呼）且 call_status=待外呼。
        找不到就返回 found=False —— 演示/自检时可以没有名单，不能因此崩掉。
        """
        t0 = time.time()
        for rec in _load_applications():
            if str(rec.get("close_mode") or "").strip() != "电话":
                continue
            if str(rec.get("call_status") or "").strip() != "待外呼":
                continue
            return {"found": True, "application": rec,
                    "matched_by": "outbound_list", "_ms": _ms(t0)}
        return {"found": False, "application": None, "_ms": _ms(t0)}

    def random_application(self):
        """测试/演示专用：从『可电话外呼』的档案里随机抽一条作为外呼目标。

        为什么只取 close_mode=电话：京麦驳回档案核上即收尾、不进六阶段，
        抽它出来测试时客服只会说一句"站内看指引"就挂，没法验证完整的
        质审→核身→驳回→确认→回写链路。随机按钮本意就是"随便找家公司测功能"，
        所以默认圈定电话外呼这一批（97 条），既保证流程走得通，又保留足够多样性。

        与 next_outbound_target 的区别：那个按"名单顺序"取第一条待外呼，
        这条是"随机"，给测试者制造不同的对话起点，避免每次都从同一家公司开场。
        """
        t0 = time.time()
        pool = [r for r in _load_applications()
                if str(r.get("close_mode") or "").strip() == "电话"
                and (r.get("company_name") or "").strip()
                and (r.get("contact_role") or "").strip()]
        if not pool:
            return {"found": False, "application": None, "_ms": _ms(t0)}
        rec = random.choice(pool)
        return {"found": True, "application": rec,
                "matched_by": "random_test", "_ms": _ms(t0)}

    def get_application(self, app_id):
        """按申请单号精确取一条档案。供"指定某家公司复测"用（可复现）。"""
        t0 = time.time()
        kw = (app_id or "").strip()
        if not kw:
            return {"found": False, "application": None, "_ms": _ms(t0)}
        for rec in _load_applications():
            if str(rec.get("application_id") or "").strip() == kw:
                return {"found": True, "application": rec,
                        "matched_by": "application_id", "_ms": _ms(t0)}
        return {"found": False, "application": None, "_ms": _ms(t0)}

    # ---- 写 -----------------------------------------------------------------
    def create_ticket(self, session_id, content, idempotency_key, version=1):
        """登记工单 —— 这一步就是赛题要的"结构化结果回写"。

        幂等的边界（重要，容易做错）：
            同一个 idempotency_key 重复调用 → **返回同一条结果，created=False**，
            不会产生第二条记录。断网重试、用户重复确认都不会脏数据。
            但"用户改口"不是重复调用，它是**同一张工单的新版本**：
            幂等键递增（...:v2），工单号不变，内容被更新，updated=True。
        """
        t0 = time.time()
        key = (idempotency_key or "").strip()
        if not key:
            from .base import ERROR_INVALID_ARG, ToolError
            raise ToolError(ERROR_INVALID_ARG, "create_ticket 缺 idempotency_key")

        if key in self._tickets:
            # 命中幂等：直接把上次的结果还回去，不新建也不更新
            t = self._tickets[key]
            return {"ticket_id": t["ticket_id"], "created": False, "updated": False,
                    "version": t["version"], "_ms": _ms(t0)}

        sid = (session_id or "").strip()
        if not sid:
            from .base import ERROR_INVALID_ARG, ToolError
            raise ToolError(ERROR_INVALID_ARG, "create_ticket 缺 session_id")

        # 同一通电话复用同一个工单号：改口是"更新"，不是"再开一张"
        ticket_id = self._ticket_by_session.get(sid)
        updated = ticket_id is not None
        if ticket_id is None:
            # 用工单号的派生方式保持可预期，方便肉眼核对"同一通电话拿到的是同一个号"
            digest = hashlib.md5(sid.encode("utf-8")).hexdigest()[:8].upper()
            ticket_id = "T%s" % digest
            self._ticket_by_session[sid] = ticket_id

        ticket = {
            "ticket_id": ticket_id,
            "session_id": sid,
            "content": content,
            "version": int(version),
            "updated_at": int(time.time()),
        }
        self._tickets[key] = ticket
        return {"ticket_id": ticket_id, "created": not updated, "updated": updated,
                "version": int(version), "_ms": _ms(t0)}

    def ticket_for_session(self, session_id):
        """取这通电话最新一版的工单（没有则 None）。验证改口是否真的更正了结论时用。"""
        latest = None
        for t in self._tickets.values():
            if t["session_id"] == session_id:
                if latest is None or t["version"] >= latest["version"]:
                    latest = t
        return latest

    # ---- 测试钩子 -----------------------------------------------------------
    def reset(self):
        """清空内存中的工单。仅自检脚本使用，业务代码不要调。"""
        self._tickets.clear()
        self._ticket_by_session.clear()
