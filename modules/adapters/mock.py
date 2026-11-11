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

# ---------------------------------------------------------------------------
# 数据文件：config/mock/ 下的一整套 mock 数据（见 docs/34）
#   applications.json           原集 120 条（评测基线，一个字节都不许动）
#   applications_catalog.json   官方 68 个关单场景各一条（新增，补齐覆盖）
#   customers.json / orders.json 售后与召回场景的客户/订单
#   catalog.json                官方关单场景目录（68 条）+ 驳回话术索引
#   rebuttals.json              官方 52 条驳回话术
#   qa.json                     官方高频 QA + 特殊场景追问
#
# 读不到文件时**必须还能跑**：客户/订单回退到内置两条、申请单回退空表。
# 理由很简单——mock 本来就是"没有外部依赖也能演示"的那条路径，
# 文件被删了/RM 漏传了，自检和跑批不应该直接崩掉。
# ---------------------------------------------------------------------------
_MOCK_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "mock")

# 兜底客户/订单：与 mock_business/data 里的 C1001/C1002 同值。
_FALLBACK_CUSTOMERS = [
    {"customer_id": "C1001", "name": "张伟", "phone_tail": "8866", "level": "金卡"},
    {"customer_id": "C1002", "name": "李娜", "phone_tail": "2143", "level": "普通"},
]
_FALLBACK_ORDERS = {
    "C1001": {"order_id": "O20261101001", "amount": 2680.0, "status": "已签收"},
    "C1002": {"order_id": "O20261101002", "amount": 399.0, "status": "运输中"},
}

_CUSTOMERS = None
_ORDERS = None
# C5：POP 资质驳回申请单档案。懒加载 + 模块级缓存：读一次 ~600KB，
# 之后每轮核身都是内存匹配，不会把首字时延拖进 tools.timeout_ms 的 300ms 预算。
_APPLICATIONS = None
_CATALOG = None
_QA = None


def _load_items(fname):
    """读 config/mock/<fname> 的 items；缺失或损坏返回 []（调用方负责兜底）。"""
    try:
        with open(os.path.join(_MOCK_DIR, fname), encoding="utf-8") as f:
            return json.load(f).get("items") or []
    except (OSError, ValueError):
        return []


def _load_customers():
    global _CUSTOMERS
    if _CUSTOMERS is None:
        _CUSTOMERS = _load_items("customers.json") or list(_FALLBACK_CUSTOMERS)
    return _CUSTOMERS


def _load_orders():
    global _ORDERS
    if _ORDERS is None:
        got = _load_items("orders.json")
        if not got:
            got = [dict(v, customer_id=k) for k, v in _FALLBACK_ORDERS.items()]
        _ORDERS = got
    return _ORDERS


def _load_applications():
    """运行时申请单池 = 原集 120 条 + 官方 68 场景 68 条 = 188 条。

    顺序固定为"原集在前、新增在后"，是为了让 `next_outbound_target()`
    （按名单顺序取第一条待外呼）和 `get_application()`（按单号精确取）
    的行为与只有原集时**完全一致**——新增数据只增覆盖，不改既有路径。
    """
    global _APPLICATIONS
    if _APPLICATIONS is None:
        merged = list(_load_items("applications.json"))
        merged.extend(_load_items("applications_catalog.json"))
        _APPLICATIONS = merged
    return _APPLICATIONS


def _load_catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = _load_items("catalog.json")
    return _CATALOG


def _load_qa():
    global _QA
    if _QA is None:
        _QA = _load_items("qa.json")
    return _QA


def _ms(t0):
    return round((time.time() - t0) * 1000, 2)


# QA 检索用的停用片段：这些 n-gram 几乎出现在每条问句里，命中它没有区分度
_QA_STOP = {
    "什么", "怎么", "可以", "如果", "这个", "那个", "我们", "你们", "他们",
    "请问", "一下", "还是", "就是", "需要", "是不是", "有没有", "能不能",
}


def _qa_keywords(text):
    """把官方问句切成 2/3/4 字片段（只保留中文与字母数字），做最笨的包含匹配。

    不做语义相似度是刻意的：这里要的是"能不能复述官方原话"，
    猜得像但没有出处，反而比"没找到"更危险。
    """
    s = "".join(ch for ch in str(text or "")
                if "\u4e00" <= ch <= "\u9fff" or ch.isalnum())
    out = set()
    for n in (4, 3, 2):
        for i in range(0, max(0, len(s) - n + 1)):
            w = s[i:i + n]
            if w in _QA_STOP:
                continue
            out.add(w)
    return out


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

        覆盖 12 条（售后 + 召回两类场景），C1001/C1002 与 Mock 业务服务同值。
        TODO 真实接口要处理：同名多结果、号码脱敏、无权限字段裁剪。
        """
        t0 = time.time()
        kw = (keyword or "").strip()
        for c in _load_customers():
            if kw and (kw == c.get("name") or kw == str(c.get("phone_tail"))):
                return {"found": True, "customer": c, "_ms": _ms(t0)}
        return {"found": False, "customer": None, "_ms": _ms(t0)}

    def query_order(self, customer_id):
        """查客户**最近一笔**订单。返回 dict(found, order)。

        一个客户可以有多笔订单，这里按 created_at 取最新（字符串 ISO 日期，
        直接可比）。"最近一笔"是话术里的口径——"您上次下的那单"。
        """
        t0 = time.time()
        cid = (customer_id or "").strip()
        mine = [o for o in _load_orders() if o.get("customer_id") == cid]
        if not mine:
            return {"found": False, "order": None, "_ms": _ms(t0)}
        latest = max(mine, key=lambda o: str(o.get("created_at") or ""))
        return {"found": True, "order": latest, "_ms": _ms(t0)}

    def reject_catalog(self):
        """官方 68 个关单场景目录（只读）。编排侧/自检/看板都能按场景反查。"""
        t0 = time.time()
        items = _load_catalog()
        return {"found": bool(items), "items": items, "count": len(items),
                "_ms": _ms(t0)}

    def reject_script(self, code):
        """按场景 id（KB-POP-xxx）或二级分类关键词取驳回话术。"""
        t0 = time.time()
        kw = (code or "").strip()
        if not kw:
            return {"found": False, "scene": None, "_ms": _ms(t0)}
        items = _load_catalog()
        for it in items:
            if kw == it.get("id"):
                return {"found": True, "scene": it, "matched_by": "id", "_ms": _ms(t0)}
        hits = [it for it in items
                if any(kw in str(it.get(f) or "")
                       for f in ("subcategory", "scene_key", "domain", "close_mode"))]
        if len(hits) == 1:
            return {"found": True, "scene": hits[0], "matched_by": "contains",
                    "_ms": _ms(t0)}
        return {"found": False, "scene": None, "_ms": _ms(t0)}

    def lookup_qa(self, question):
        """从官方高频 QA / 特殊场景里找一条最像的答案（只读，不参与合规判定）。

        做法故意很笨：问句关键词命中越多越靠前，命中数为 0 就算没找到。
        不做语义相似度——这里要的是"能不能复述官方原话"，不是"猜得像"。
        """
        t0 = time.time()
        q = (question or "").strip()
        if not q:
            return {"found": False, "answer": "", "match": "", "_ms": _ms(t0)}
        best, best_score, best_len = None, 0, 0
        for it in _load_qa():
            score, longest = 0, 0
            for kw in _qa_keywords(it.get("question") or ""):
                if kw in q:
                    score += len(kw)
                    longest = max(longest, len(kw))
            if score > best_score or (score == best_score and longest > best_len):
                best, best_score, best_len = it, score, longest
        # 门槛：必须命中至少一个 4 字片段。只命中"怎么""可以"这种 2 字片段，
        # 说明只是泛泛重合，宁可不答，也不要拿一条不相关的官方答案去糊。
        if not best or best_len < 4:
            return {"found": False, "answer": "", "match": "", "_ms": _ms(t0)}
        return {"found": True, "answer": best.get("answer", ""),
                "match": best.get("question", ""), "qid": best.get("id"),
                "source": best.get("source"), "score": best_score, "_ms": _ms(t0)}

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

    def list_applications(self):
        """全部可电话外呼的档案（演示页"分类浏览示例数据"用，只读）。

        与 random_application 同一个池（close_mode=电话、公司名与岗位齐全）：
        京麦驳回档案核上即收尾，浏览出来也没法走完整对话，不放进去误导人。
        列表只回**展示字段**，身份证号/统一社会信用代码这类补全部件不进列表——
        完整档案走 /api/company/get 单条取（与随机预览同口径）。
        """
        t0 = time.time()
        pool = [r for r in _load_applications()
                if str(r.get("close_mode") or "").strip() == "电话"
                and (r.get("company_name") or "").strip()
                and (r.get("contact_role") or "").strip()]
        rows = [{
            "application_id": r.get("application_id"),
            "company_name": r.get("company_name"),
            "contact_role": r.get("contact_role"),
            "brand_name": r.get("brand_name"),
            "category_l1": r.get("category_l1"),
            "reject_category": r.get("reject_category"),
            "reject_reason": r.get("reject_reason"),
            "application_status": r.get("application_status"),
        } for r in pool]
        return {"found": True, "items": rows, "count": len(rows), "_ms": _ms(t0)}

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
