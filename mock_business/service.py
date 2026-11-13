"""Mock 业务系统（M5/M8 交付物）—— 四个接口的独立模拟服务。

为什么要独立成一个服务，而不是塞进 app.py：
    1. 真的企业对接里，这些接口在业务方机房里，不在通话服务进程里。
       分开做，联调时才能换地址、换鉴权、演超时——塞进进程内就永远测不到这些。
    2. 2B（潘柏亦）的 modules/tools.py 要接真实接口，需要先有一个"长得像真的"
       的对端。这个服务就是那个对端，且不占用他的文件。
    3. 它能独立跑（`python mock_business/service.py`），答辩时可以单独演示
       "我把业务侧也模拟出来了"，不用起整个通话链路。

提供的接口（全部 JSON，UTF-8，无第三方依赖）：
    GET  /mock/health                      健康检查
    GET  /mock/customer?keyword=xxx        按姓名或手机号后四位查客户
    GET  /mock/order?customer_id=C001      按客户查订单（也可用 keyword）
    GET  /mock/application?keyword=xxx     按申请单号/公司名/姓名/后四位查资质驳回申请单
    POST /mock/ticket                      建工单（幂等，按 idempotency_key）
    GET  /mock/tickets                     列已建工单（运维/演示用）
    GET  /mock/stats                       调用计数与错误数（做看板联动用）

设计上的几条硬约定（和 contracts.TOOL_SCHEMAS 对齐，改字段要先改契约）：
    - 查不到是 200 + {"found": false}，不是 404。业务上"没这个人"是正常结果，
      用 404 表达会让调用方把"没查到"和"接口挂了"混在一起。
    - 建工单必须带 idempotency_key。同一 key 重复提交返回同一张单，
      created=false，不会产生第二张单——这是回写幂等的唯一保证。
    - 参数错是 400 + {"error": ..., "detail": ...}，不要静默返回空对象。

跑起来：
    python mock_business/service.py           # 默认 127.0.0.1:8100
    python mock_business/service.py 8101      # 换端口
"""

import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

_ROOT = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_ROOT, "data")
_TICKET_FILE = os.path.join(_DATA, "tickets.json")

# 工单表在内存里，落盘只是为了让重启后演示数据还在。
# 生产当然用数据库，这里刻意的：零依赖、看得懂、能直接改 JSON 造假数据。
_LOCK = threading.Lock()
_TICKETS = {}      # idempotency_key -> ticket dict
_STATS = {"customer": 0, "order": 0, "ticket": 0, "errors": 0}
_STARTED_AT = time.time()


def _load(name):
    """读一份沙箱数据。文件不存在返回空列表，不抛错（首次跑不该因为缺文件起不来）。"""
    path = os.path.join(_DATA, name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("[mock] 缺少数据文件 %s，按空数据处理" % path)
        return []
    except Exception as e:
        print("[mock] 数据文件 %s 解析失败：%s" % (path, e))
        return []


_CUSTOMERS = _load("customers.json")
_ORDERS = _load("orders.json")

# C5：POP 资质驳回申请单档案在仓库根的 config/mock/（120 份虚构数据）。
# 沙箱服务与默认 mock adapter 共用同一份数据，切换 provider 不会换一套业务事实。
_APPLICATIONS_FILE = os.path.join(
    os.path.dirname(_ROOT), "config", "mock", "applications.json")


def _load_applications():
    try:
        with open(_APPLICATIONS_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("items") or []
    except (OSError, ValueError) as e:
        print("[mock] 申请单档案读取失败：%s" % e)
        return []


_APPLICATIONS = _load_applications()


def _persist_tickets():
    """把工单写回磁盘。写完才算建单成功——否则重启就"丢单"，演示会很尴尬。"""
    os.makedirs(_DATA, exist_ok=True)
    tmp = _TICKET_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(_TICKETS.values()), f, ensure_ascii=False, indent=2)
    os.replace(tmp, _TICKET_FILE)   # 原子替换：写一半崩了也不会留下坏文件


def _restore_tickets():
    try:
        with open(_TICKET_FILE, "r", encoding="utf-8") as f:
            for t in json.load(f):
                _TICKETS[t.get("idempotency_key", t.get("ticket_id"))] = t
    except FileNotFoundError:
        pass
    except Exception as e:
        print("[mock] 工单存档读取失败：%s" % e)


def find_customer(keyword):
    """按姓名 / 手机号后四位 / 客户号查人。返回客户 dict 或 None。

    为什么三种都能查：真实外呼里坐席拿到的是"张先生 + 尾号 8866"，
    未必知道客户号；工具层不该逼调用方先知道主键。
    """
    kw = (keyword or "").strip()
    if not kw:
        return None
    for c in _CUSTOMERS:
        if kw in (c.get("customer_id"), c.get("name")):
            return c
        if kw and kw == str(c.get("phone_tail")):
            return c
        # 姓名里包含也算命中：用户说"张先生"，档案存"张伟"
        if kw and kw in (c.get("name") or ""):
            return c
    return None


def find_application(keyword):
    """C5：按姓名/后四位/公司名/申请单号四路径定位资质驳回申请单。"""
    kw = (keyword or "").strip()
    if not kw:
        return None, ""
    for key in ("verify_by_name", "verify_by_tail",
                "verify_by_company", "verify_by_application"):
        for rec in _APPLICATIONS:
            if kw == rec.get(key):
                return rec, key
    if len(kw) >= 4:
        for key in ("verify_by_company", "company_short_name", "shop_name"):
            hits = [r for r in _APPLICATIONS if kw in str(r.get(key) or "")]
            if len(hits) == 1:
                return hits[0], key + ":contains"
    return None, ""


def orders_of(customer_id=None, keyword=None):
    """查订单。给了 customer_id 就精确查，否则用 keyword 先定位客户。"""
    cid = customer_id
    if not cid and keyword:
        c = find_customer(keyword)
        cid = c.get("customer_id") if c else None
    if not cid:
        return []
    return [o for o in _ORDERS if o.get("customer_id") == cid]


class Handler(BaseHTTPRequestHandler):
    server_version = "super_candy-mock/1.0"

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, error, detail=""):
        _STATS["errors"] += 1
        self._json({"error": error, "detail": detail}, code)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/mock/health":
            self._json({
                "ok": True,
                "customers": len(_CUSTOMERS),
                "orders": len(_ORDERS),
                "applications": len(_APPLICATIONS),
                "tickets": len(_TICKETS),
                "uptime_sec": round(time.time() - _STARTED_AT, 1),
            })
            return

        if path == "/mock/customer":
            _STATS["customer"] += 1
            kw = (qs.get("keyword") or [""])[0]
            if not kw.strip():
                self._err(400, "BAD_REQUEST", "缺少 keyword（姓名 / 手机号后四位 / 客户号）")
                return
            c = find_customer(kw)
            # 查不到是 200 + found=false：业务上的"没有"，不是技术上的"出错"
            self._json({"found": bool(c), "customer": c})
            return

        if path == "/mock/order":
            _STATS["order"] += 1
            cid = (qs.get("customer_id") or [""])[0]
            kw = (qs.get("keyword") or [""])[0]
            if not cid and not kw:
                self._err(400, "BAD_REQUEST", "需要 customer_id 或 keyword 之一")
                return
            rows = orders_of(cid, kw)
            self._json({"found": bool(rows), "count": len(rows), "orders": rows})
            return

        if path == "/mock/application":
            _STATS["application"] = _STATS.get("application", 0) + 1
            kw = (qs.get("keyword") or [""])[0]
            if not kw.strip():
                self._err(400, "BAD_REQUEST", "缺少 keyword（姓名 / 后四位 / 公司名 / 申请单号）")
                return
            rec, by = find_application(kw)
            self._json({"found": bool(rec), "application": rec, "matched_by": by})
            return

        if path == "/mock/tickets":
            self._json({"count": len(_TICKETS), "tickets": list(_TICKETS.values())})
            return

        if path == "/mock/stats":
            self._json({"calls": dict(_STATS),
                        "uptime_sec": round(time.time() - _STARTED_AT, 1)})
            return

        self._err(404, "NOT_FOUND", "没有这个接口：%s" % path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/mock/ticket":
            self._err(404, "NOT_FOUND", "没有这个接口：%s" % path)
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._err(400, "BAD_REQUEST", "请求体为空")
            return
        if length > 1_000_000:
            self._err(413, "PAYLOAD_TOO_LARGE", "工单请求体上限 1MB")
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._err(400, "BAD_JSON", str(e))
            return

        _STATS["ticket"] += 1
        key = (body.get("idempotency_key") or "").strip()
        if not key:
            # 没幂等键就建单 = 重试会重复下单，这在回写场景里是事故，直接在入口拦掉
            self._err(400, "MISSING_IDEMPOTENCY_KEY",
                      "回写必须带 idempotency_key（建议 session_id:turn_id）")
            return

        with _LOCK:
            exist = _TICKETS.get(key)
            if exist:
                self._json({"created": False, "ticket": exist,
                            "note": "同一 idempotency_key 已建过单，返回原单"})
                return
            # content 原样整段存下来，不做字段白名单。
            # 为什么：调用方回写的业务字段是随赛题场景变的（售后是
            # reject_category / reject_summary / materials_required…），
            # 对端如果只挑四个自己认识的字段，其余全丢，工单就变成空单——
            # 而调用方读的是自己的本地副本，照样显示"成功"，这是最典型的假绿。
            # 存原始 content 的代价是存储大一点，收益是"回写到底写了什么"可被对端验证。
            content = body.get("content")
            ticket = {
                "ticket_id": "T" + uuid.uuid4().hex[:10].upper(),
                "idempotency_key": key,
                "customer_id": body.get("customer_id") or "",
                "order_id": body.get("order_id") or "",
                "result": body.get("result") or "connected",
                "consent": body.get("consent"),
                "need": body.get("need") or "",
                "note": body.get("note") or "",
                "content": content if isinstance(content, dict) else {},
                "created_at": int(time.time() * 1000),
                "source": body.get("source") or "voice-agent",
            }
            _TICKETS[key] = ticket
            _persist_tickets()
        self._json({"created": True, "ticket": ticket}, 201)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("MOCK_PORT", "8100"))
    host = os.environ.get("MOCK_HOST") or "127.0.0.1"
    _restore_tickets()
    server = ThreadingHTTPServer((host, port), Handler)
    print("Mock 业务系统已启动")
    print("  监听：%s:%d" % (host, port))
    print("  数据：%d 个客户 / %d 笔订单 / %d 份资质申请单 / %d 张历史工单"
          % (len(_CUSTOMERS), len(_ORDERS), len(_APPLICATIONS), len(_TICKETS)))
    print("  接口：/mock/health /mock/customer /mock/order /mock/application /mock/ticket /mock/tickets")
    print("  停止：Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
