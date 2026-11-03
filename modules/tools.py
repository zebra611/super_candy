"""M4 工具与业务适配 —— 哑链路里的"假业务系统"。

职责（真实形态）：
    把大模型的工具调用请求，翻译成对名单/订单/工单系统的真实请求，
    并把结果结构化回写。要求：参数校验、错误码、幂等。

当前状态：三个工具全部返回写死的假数据。
    但**幂等逻辑是真的**——同一幂等键重复调用只产生一条工单，这条不需要等真接口。

负责人：角色 2（定义工具契约） + 角色 4（实现适配层）

后续待补充：
    1. 换成真实 Mock 接口（企业报名后提供的沙箱地址与账号）；
    2. 用适配器模式隔离：MockAdapter / RealAdapter 两套实现，配置切换，
       这样"外呼通道怎么实现"这个未知项不会阻塞现在的开发；
    3. 加超时与重试（现在的假实现不会超时，所以没写）；
    4. 错误码统一（业务不存在 / 参数非法 / 系统繁忙 要能区分）。
"""

import hashlib
import time

# 幂等存储：真实实现应换成数据库唯一索引，骨架阶段用内存 dict。
# _TICKETS          键 = 幂等键，值 = 已生成的工单（同一键重复调用不再写）
# _TICKET_BY_SESSION 键 = 会话 ID，值 = 这通电话的工单号（改口时更新同一张，不新开）
_TICKETS = {}
_TICKET_BY_SESSION = {}

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


def query_customer(keyword):
    """按姓名或手机尾号查客户。返回 dict(found, customer)。

    TODO 真实接口要处理：同名多结果、号码脱敏、无权限字段裁剪。
    """
    t0 = time.time()
    kw = (keyword or "").strip()
    for c in _FAKE_CUSTOMERS:
        if kw and (kw == c["name"] or kw == c["phone_tail"]):
            return {"found": True, "customer": c, "_ms": _ms(t0)}
    return {"found": False, "customer": None, "_ms": _ms(t0)}


def query_order(customer_id):
    """查客户最近一笔订单。返回 dict(found, order)。"""
    t0 = time.time()
    order = _FAKE_ORDERS.get(customer_id)
    return {"found": bool(order), "order": order, "_ms": _ms(t0)}


def create_ticket(session_id, content, idempotency_key, version=1):
    """登记工单 —— 这一步就是赛题要的"结构化结果回写"。

    幂等的边界（重要，容易做错）：
        同一个 idempotency_key 重复调用 → **返回同一条结果，created=False**，
        不会产生第二条记录。断网重试、用户重复确认都不会脏数据。
        但"用户改口"不是重复调用，它是**同一张工单的新版本**：
        幂等键递增（...:v2），工单号不变，内容被更新，updated=True。
        这样业务系统里始终只有一张工单、一个最终结论，不会留下两条互相矛盾的记录。

    参数：
        session_id       通话 ID（同时作为业务主键：一通电话一张工单）
        content          结构化字段 dict（形状来自 config 的 writeback_template）
        idempotency_key  幂等键，约定为 "会话ID:WRITEBACK:v版本号"
        version          第几次回写，从 1 开始；改口时递增
    返回：dict(ticket_id, created, updated, version, _ms)
    """
    t0 = time.time()
    if idempotency_key in _TICKETS:
        # 命中幂等：直接把上次的结果还回去，不新建也不更新
        t = _TICKETS[idempotency_key]
        return {"ticket_id": t["ticket_id"], "created": False, "updated": False,
                "version": t["version"], "_ms": _ms(t0)}

    # 同一通电话复用同一个工单号：改口是"更新"，不是"再开一张"
    ticket_id = _TICKET_BY_SESSION.get(session_id)
    updated = ticket_id is not None
    if ticket_id is None:
        # 用工单号的派生方式保持可预期，方便肉眼核对"同一通电话拿到的是同一个号"
        digest = hashlib.md5(session_id.encode("utf-8")).hexdigest()[:8].upper()
        ticket_id = "T%s" % digest
        _TICKET_BY_SESSION[session_id] = ticket_id

    ticket = {
        "ticket_id": ticket_id,
        "session_id": session_id,
        "content": content,
        "version": int(version),
        "updated_at": int(time.time()),
    }
    _TICKETS[idempotency_key] = ticket
    return {"ticket_id": ticket_id, "created": not updated, "updated": updated,
            "version": int(version), "_ms": _ms(t0)}


def ticket_for_session(session_id):
    """取这通电话最新一版的工单（没有则 None）。验证改口是否真的更正了结论时用。"""
    latest = None
    for t in _TICKETS.values():
        if t["session_id"] == session_id:
            if latest is None or t["version"] >= latest["version"]:
                latest = t
    return latest


def _reset_for_test():
    """清空内存中的工单。仅自检脚本使用，业务代码不要调。"""
    _TICKETS.clear()
    _TICKET_BY_SESSION.clear()


def dispatch(tool_call):
    """工具总入口：按 name 分发。同时校验参数，缺参直接报错。

    这是 app.py 唯一需要调用的函数，新增工具时只改这里的分发表。
    """
    name = (tool_call or {}).get("name")
    args = (tool_call or {}).get("args") or {}

    if name == "query_customer":
        return query_customer(args.get("keyword"))
    if name == "query_order":
        return query_order(args.get("customer_id"))
    if name == "create_ticket":
        return create_ticket(
            session_id=args.get("session_id", ""),
            content=args.get("content", {}),
            idempotency_key=args.get("idempotency_key", ""),
            version=args.get("version", 1),
        )
    # 未知工具：不抛异常，返回结构化错误，让上层能兜底（赛题要求异常兜底）
    return {"error": "UNKNOWN_TOOL", "name": name, "_ms": 0.0}


def _ms(t0):
    """把起始时间换成毫秒耗时，统一各工具返回的 _ms 字段。"""
    return round((time.time() - t0) * 1000, 2)
