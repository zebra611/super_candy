"""RealAdapter —— 打真实业务接口（现阶段对端是 mock_business/）。

为什么默认不用它：
    对端是独立进程（`python mock_business/service.py`，默认 8100），
    clone 下来没起它就活不了；默认 mock 才能保证"刚 clone 就能跑"。
    切过来只要改 `config/scene.json` 的 `tools.provider` 或环境变量。

三个容易做错的地方（mock_business/README.md 点名的那三条）：
    1. `found:false` 是业务结果，不是异常——不能抛，要让编排走"没查到"分支；
    2. `idempotency_key` 必须由 session 派生，不能每次新 UUID；
    3. 超时要短（300ms 预算），业务接口慢不该拖垮通话。

幂等的语义映射（T11）：
    Mock 服务的工单是按 `idempotency_key` 存的，没有"更新"接口。
    我们这一层补上映射：同一 session 的新版本仍然复用**同一个 ticket_id**，
    并回报 `updated=True`——业务系统里"一通电话一张单"的结论由我们保证。
    接真实工单系统（有 PUT/PATCH）时，把 `_remember` 那段换成真正的更新调用即可。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from .base import (ERROR_INVALID_ARG, ERROR_NOT_FOUND, ERROR_TIMEOUT,
                   ERROR_UPSTREAM, BaseAdapter, ToolError)


class RealAdapter(BaseAdapter):
    name = "real"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        # 幂等映射：session_id -> 工单号。Mock 无更新接口，这层负责"同一张单"的语义。
        self._session_tickets = {}
        # 最近一次回写的内容与版本：verify 里有断言要读 ticket_for_session 的 content（P6），
        # 以前这里固定返回 {} / 0，三条断言在"静默错值"。
        self._last_ticket = {}

    # ---- HTTP 基础设施 -------------------------------------------------------
    def _request(self, method, path, params=None, body=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=max(1, self.timeout_ms) / 1000.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except socket.timeout:
            raise ToolError(ERROR_TIMEOUT, "业务接口超时（>%dms）" % self.timeout_ms)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = (json.loads(exc.read().decode("utf-8")) or {}).get("error", "")
            except Exception:
                detail = ""
            if exc.code == 400:
                raise ToolError(ERROR_INVALID_ARG, detail or "参数非法（400）")
            if exc.code == 404:
                raise ToolError(ERROR_NOT_FOUND, detail or "对端没有这个资源（404）")
            raise ToolError(ERROR_UPSTREAM, detail or "业务接口 %d" % exc.code)
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), socket.timeout):
                raise ToolError(ERROR_TIMEOUT, "业务接口超时（>%dms）" % self.timeout_ms)
            raise ToolError(ERROR_UPSTREAM, "连不上业务接口：%s" % (exc.reason,))
        except json.JSONDecodeError:
            raise ToolError(ERROR_UPSTREAM, "业务接口返回的不是 JSON")

    # ---- 读 -----------------------------------------------------------------
    def query_customer(self, keyword):
        kw = (keyword or "").strip()
        if not kw:
            raise ToolError(ERROR_INVALID_ARG, "query_customer 缺 keyword")
        _, data = self._request("GET", "/mock/customer", {"keyword": kw})
        if not data.get("found"):
            # 业务上的"查不到"：不抛异常，让编排走"没查到"分支
            return {"found": False, "customer": None}
        return {"found": True, "customer": data.get("customer") or None}

    def query_order(self, customer_id):
        cid = (customer_id or "").strip()
        if not cid:
            raise ToolError(ERROR_INVALID_ARG, "query_order 缺 customer_id")
        _, data = self._request("GET", "/mock/order", {"customer_id": cid})
        orders = data.get("orders") or []
        if not orders:
            return {"found": False, "order": None}
        # 契约是"最近一笔"，对端返回的是列表，取第一笔
        return {"found": True, "order": orders[0]}

    def query_application(self, keyword):
        """C5：定位 POP 资质驳回申请单（对端 mock_business /mock/application）。"""
        kw = (keyword or "").strip()
        if not kw:
            raise ToolError(ERROR_INVALID_ARG, "query_application 缺 keyword")
        _, data = self._request("GET", "/mock/application", {"keyword": kw})
        if not data.get("found"):
            return {"found": False, "application": None}
        return {"found": True, "application": data.get("application") or None}

    # ---- 写 -----------------------------------------------------------------
    def create_ticket(self, session_id, content, idempotency_key, version=1):
        sid = (session_id or "").strip()
        key = (idempotency_key or "").strip()
        if not sid:
            raise ToolError(ERROR_INVALID_ARG, "create_ticket 缺 session_id")
        if not key:
            raise ToolError(ERROR_INVALID_ARG, "create_ticket 缺 idempotency_key")

        content = content or {}
        body = {
            "idempotency_key": key,
            "customer_id": content.get("customer_id", ""),
            "order_id": content.get("order_id", ""),
            "consent": content.get("consent"),
            "need": content.get("need", ""),
        }
        _, data = self._request("POST", "/mock/ticket", body=body)
        ticket = data.get("ticket") or {}
        remote_id = ticket.get("ticket_id") or ""

        # 幂等语义映射：同一通电话始终只认一个工单号
        self._last_ticket[sid] = {"content": dict(content), "version": int(version)}
        local_id = self._session_tickets.get(sid)
        if local_id is None:
            self._session_tickets[sid] = remote_id
            return {"ticket_id": remote_id, "created": True, "updated": False,
                    "version": int(version)}
        if local_id == remote_id:
            return {"ticket_id": remote_id, "created": False, "updated": False,
                    "version": int(version)}
        return {"ticket_id": local_id, "created": False, "updated": True,
                "version": int(version)}

    def ticket_for_session(self, session_id):
        ticket_id = self._session_tickets.get(session_id)
        if not ticket_id:
            return None
        last = self._last_ticket.get(session_id) or {}
        return {"ticket_id": ticket_id, "session_id": session_id,
                "content": dict(last.get("content") or {}),
                "version": int(last.get("version") or 0)}

    def reset(self):
        self._session_tickets.clear()
        self._last_ticket.clear()
