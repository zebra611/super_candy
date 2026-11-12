"""M4 工具与工具适配 —— 契约入口、回写校验、幂等。

分层（这一版才真正落地，之前全挤在一个文件里）：
    modules/tools.py          契约入口：dispatch() / validate_writeback()
    modules/adapters/base.py  超时、错误码、耗时打点
    modules/adapters/mock.py  内置假数据（**默认**，clone 下来就能跑）
    modules/adapters/real.py  真实接口（现阶段对端是 mock_business/）

为什么这么切：
    "数据从哪来"是会变的东西（现在假数据、之后沙箱、最后真接口），
    而"工具叫什么、参数是什么、返回什么形状"是契约，不能跟着变。
    所以契约留在 tools.py，实现退到 adapters/，切换只改 config/scene.json 的
    `tools.provider`——`app.py` 那一行 `tools.dispatch(tc)` 一个字都不用动。

边界（docs/17 §1）：这一层只管事实从哪来，不决定该不该问、怎么措辞。

后续待补充：
    1. 接企业真实沙箱时，只改 RealAdapter 的路径与鉴权；
    2. 幂等存储换数据库唯一索引（现在是内存 dict，进程重启会失效，见 docs/14 待改进第 5 条）；
    3. validate_writeback 的字段白名单，等 writeback_schema 定稿后再收紧。
"""

from __future__ import annotations

import time

from . import adapters
from .adapters.base import ERROR_INVALID_ARG, ERROR_UNKNOWN_TOOL

#: 回写校验失败的错误码（赛题"格式合规性"这一项的扣分点）
ERROR_WRITEBACK_INVALID = "WRITEBACK_INVALID"

# writeback_schema 的 type -> Python 类型断言
_TYPE_CHECKS = {
    "str": lambda v: isinstance(v, str),
    "bool": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "dict": lambda v: isinstance(v, dict),
    "list": lambda v: isinstance(v, list),
}


# ---------------------------------------------------------------------------
# 适配器出口
# ---------------------------------------------------------------------------
def _adapter():
    """当前 provider 对应的适配器（默认 mock）。改配置即切，不用改代码。"""
    return adapters.get_adapter()


# ---------------------------------------------------------------------------
# 契约入口（app.py 只认这一个函数）
# ---------------------------------------------------------------------------
def dispatch(tool_call):
    """工具总入口：按 name 分发。同时校验参数，缺参直接报错。

    这是 app.py 唯一需要调用的函数，新增工具时只改这里的分发表。
    返回形状与改造前一致：成功带 `_ms`，失败带 `error`（**不抛异常**，
    让编排层能兜底——赛题要求的"异常兜底"）。
    """
    name = (tool_call or {}).get("name")
    args = (tool_call or {}).get("args") or {}
    ad = _adapter()

    if name == "query_customer":
        return ad.call("query_customer", args.get("keyword"))

    if name == "query_order":
        return ad.call("query_order", args.get("customer_id"))

    if name == "query_application":
        return ad.call("query_application", args.get("keyword"))

    if name == "create_ticket":
        # 先截断再校验：超长诉求不该整单被拒（P3，行为倒退）
        content, notes = prepare_writeback(args.get("content") or {})
        # 回写前的格式闸门：字段型式不对就不往业务系统写，宁可这一轮失败
        # 让编排播兜底话术，也不要往工单里塞脏数据。
        check = validate_writeback(content)
        if not check["ok"]:
            return {"error": ERROR_WRITEBACK_INVALID,
                    "message": "；".join(check["errors"]),
                    "errors": check["errors"],
                    "_ms": check["ms"]}
        result = ad.call("create_ticket",
                         args.get("session_id", ""),
                         content,
                         args.get("idempotency_key", ""),
                         args.get("version", 1))
        if notes and isinstance(result, dict):
            # 让编排/事件流知道这次回写被截断过，但不影响"工单已经建出来"这个事实
            result.setdefault("truncated", notes)
        return result

    if name == "send_checklist":
        return ad.call("send_checklist", args.get("session_id", ""),
                       args.get("application_id", ""), args.get("materials") or [])

    # 未知工具：不抛异常，返回结构化错误，让上层能兜底（赛题要求异常兜底）
    return {"error": ERROR_UNKNOWN_TOOL, "name": name, "_ms": 0.0}


# ---------------------------------------------------------------------------
# 回写字段校验（T12）
# ---------------------------------------------------------------------------
def validate_writeback(content):
    """按 config_loader.writeback_schema() 校验回写内容。

    返回：{"ok": bool, "errors": [人类可读的原因], "ms": 校验耗时}

    只校验 schema 里声明过的**必填**字段：类型、枚举、长度。
    **多余字段一律放行**——编排层会往 content 里加 writeback_version / order_id /
    corrections 这些"跟着工单走的轨迹"，它们不是脏数据，不能因为"schema 里没有"
    就把整张单拒掉。

    为什么这件事必须在写之前做：
        赛题维度 3 有"结构化回写完整度与格式合规性"。工单一旦写进业务系统，
        下游就按它的字段计费/派单；缺字段的单子事后只能人工修，代价比拒绝高得多。
    """
    t0 = time.time()
    errors = []

    try:
        import config_loader
        schema = config_loader.writeback_schema() or {}
    except Exception as exc:  # 配置读不出来时不要假装校验通过
        return {"ok": False, "errors": ["读不到 writeback_schema：%s" % exc],
                "ms": _ms(t0)}

    # 兼容骨架阶段的扁平写法：没有 required/optional 就整份当必填、不查类型
    if "required" in schema or "optional" in schema:
        required = schema.get("required") or {}
        optional = schema.get("optional") or {}
    else:
        required = {k: {} for k, v in schema.items() if not k.startswith("_")}
        optional = {}

    content = content or {}

    for field, spec in required.items():
        if field.startswith("_"):
            continue
        spec = spec if isinstance(spec, dict) else {}
        if field not in content or content[field] is None:
            errors.append("缺必填字段 %s（%s）" % (field, spec.get("desc", "")))
            continue
        errors.extend(_check_field(field, content[field], spec))

    for field, spec in optional.items():
        if field.startswith("_") or field not in content:
            continue
        spec = spec if isinstance(spec, dict) else {}
        if content[field] is None:
            continue  # optional 允许不填/为空
        errors.extend(_check_field(field, content[field], spec))

    return {"ok": not errors, "errors": errors, "ms": _ms(t0)}


def prepare_writeback(content):
    """回写前整理：超长字符串**截断**而不是整单拒绝。

    为什么是截断：schema 里 `need` 限 100 字，但用户一张嘴可能说 129 个字。
    旧行为是整单 `WRITEBACK_INVALID`，工单落不了、闭环判失败——比"少记 29 个字"严重得多。
    截断后 `need` 仍是同一件事的前 100 字，且把"截断过"写进返回，事件流里看得见。

    返回：(整理后的 content, 提示列表)。**不修改传入的 dict**，编排层还拿着它。
    """
    content = dict(content or {})
    notes = []
    try:
        import config_loader
        schema = config_loader.writeback_schema() or {}
    except Exception:
        return content, notes

    groups = []
    if "required" in schema or "optional" in schema:
        groups = [schema.get("required") or {}, schema.get("optional") or {}]
    else:
        groups = [schema]

    for group in groups:
        for field, spec in group.items():
            if field.startswith("_") or not isinstance(spec, dict):
                continue
            max_chars = spec.get("max_chars")
            value = content.get(field)
            if max_chars and isinstance(value, str) and len(value) > int(max_chars):
                content[field] = value[:int(max_chars)]
                notes.append("%s 超长，已截断到 %d 字" % (field, int(max_chars)))
    return content, notes


def _check_field(field, value, spec):
    """单个字段的类型 / 枚举 / 长度检查。"""
    errors = []
    ftype = (spec.get("type") or "").strip()
    checker = _TYPE_CHECKS.get(ftype)
    if checker and not checker(value):
        errors.append("字段 %s 类型应为 %s，实际是 %s"
                      % (field, ftype, type(value).__name__))
        return errors  # 类型都不对，后面就别查了

    values = spec.get("values")
    if values is not None:
        # 类型也要比：`0 == False` 在 Python 里成立，光比 `in` 会让 consent=0 混过布尔枚举（P8）
        if not any(type(value) is type(x) and value == x for x in values):
            errors.append("字段 %s 取值 %r 不在枚举 %r 内" % (field, value, values))

    # 超长**不判错**：回写前会截断（见 prepare_writeback）。
    # 用户说了 129 个字就整单拒绝，等于因为他话多就丢了这张单（P3）。
    return errors


# ---------------------------------------------------------------------------
# 兼容层：老调用点与自检脚本仍按函数名调，签名保持不变
# ---------------------------------------------------------------------------
def query_customer(keyword):
    """按姓名或手机尾号查客户。返回 dict(found, customer)。"""
    return _adapter().call("query_customer", keyword)


def query_order(customer_id):
    """查客户最近一笔订单。返回 dict(found, order)。"""
    return _adapter().call("query_order", customer_id)


def query_application(keyword):
    """C5：按姓名/后四位/公司名/申请单号定位 POP 资质驳回申请单。

    返回 dict(found, application)；查不到不算异常（found=False），
    由编排层决定走"查无申请"话术还是旧的售后客户核身。
    """
    return _adapter().call("query_application", keyword)


def next_outbound_target():
    """外呼名单里的下一通该打的电话（带 found / application）。

    没有名单时返回 found=False，调用方自己决定怎么降级——不能让通话开不起来。
    """
    return _adapter().call("next_outbound_target")


def random_application():
    """测试/演示专用：从可电话外呼的档案里随机抽一条作为外呼目标。

    与 next_outbound_target 的区别见 modules/adapters/mock.py 的同名方法注释。
    返回 dict(found, application, matched_by)。
    """
    return _adapter().call("random_application")


def get_application(app_id):
    """按申请单号精确取一条档案（可复现复测）。返回 dict(found, application)。"""
    return _adapter().call("get_application", app_id)


def list_applications():
    """全部可电话外呼的档案（展示字段）。演示页"分类浏览示例数据"用。"""
    return _adapter().call("list_applications")


def reject_catalog():
    """官方 68 个关单场景目录。返回 dict(found, items, count)。

    只读资料，不参与状态机推进——给自检、看板、"这单属于哪个官方场景"的反查用。
    provider 没接这张表时返回 found=False 与空列表（不会抛异常）。
    """
    return _adapter().call("reject_catalog")


def reject_script(code):
    """按场景 id（KB-POP-xxx）或二级分类关键词取官方驳回话术。"""
    return _adapter().call("reject_script", code)


def lookup_qa(question):
    """从官方高频 QA / 特殊场景里取一条答案（找不到返回 found=False，不编）。"""
    return _adapter().call("lookup_qa", question)


def create_ticket(session_id, content, idempotency_key, version=1):
    """登记工单 —— 结构化结果回写。幂等语义见 adapters/mock.py。"""
    content, notes = prepare_writeback(content)
    check = validate_writeback(content)
    if not check["ok"]:
        return {"error": ERROR_WRITEBACK_INVALID,
                "message": "；".join(check["errors"]),
                "errors": check["errors"], "_ms": check["ms"]}
    result = _adapter().call("create_ticket", session_id, content,
                             idempotency_key, version)
    if notes and isinstance(result, dict):
        result.setdefault("truncated", notes)
    return result


def checklist_for_application(application_id):
    """查某申请单的清单是否已发到后台（自检/看板用，返回 checklist_id 或空）。"""
    ad = _adapter()
    return (getattr(ad, "_checklist_by_app", {}) or {}).get(application_id)


def ticket_for_session(session_id):
    """取这通电话最新一版的工单（没有则 None）。"""
    return _adapter().ticket_for_session(session_id)


def _reset_for_test():
    """清空内存中的工单。仅自检脚本使用，业务代码不要调。"""
    adapters.reset_adapter()


def _ms(t0):
    """把起始时间换成毫秒耗时，统一各工具返回的 _ms 字段。"""
    return round((time.time() - t0) * 1000, 2)
