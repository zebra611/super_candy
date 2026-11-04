"""成本与容量闸门（M8 的一部分）—— 让"花了多少钱"变成可见、可拦。

为什么单独一个模块：
    预算判断散落在路由里，最后一定变成"这里判一次、那里判一次，谁都不全"。
    统一到这里，路由只问一句 `allow()`，返回 ok 还是拒。

三个层次，刻意分开（混在一起就分不清是"贵"还是"忙"）：
    1. 单通预算   —— 已有，在 app._budget_reason()，按轮次/时长/金额结束本通；
    2. 日成本闸门 —— 本模块，全站当天累计超过硬上限就拒新轮次；
    3. 速率限制   —— 本模块，每会话每分钟轮次上限，挡住"脚本狂刷"。

默认全部关闭（guard_enabled=false）。
    原因：骨架阶段硬熔断会让演示莫名其妙失败，而比赛更怕"演示时突然不能用"。
    但开关给了、数字给了、日志给了——需要时改一行配置就能上，不用改代码。

口径：
    成本一律用事件库里 turn_end.payload.est_cost_cny 累加，不另算一份。
    两套算法必然对不上，对不上的数字在答辩现场最容易被问穿。
"""

import json
import threading
import time

import config_loader
import events

# 每个会话的最近请求时间戳（秒），用于分钟级限流。key = session_id
_HITS = {}
_HITS_LOCK = threading.Lock()
_HITS_MAX_SESSIONS = 2000   # 防内存无限增长：超了就整体清理一次


def _today_start_ms(now_ms=None):
    """今天 00:00 的毫秒时间戳。用本地时区，和看板的按天聚合保持一致。"""
    now = (now_ms if now_ms is not None else events.now_ms()) / 1000.0
    lt = time.localtime(now)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return int(start * 1000)


def daily_cost(now_ms=None):
    """当天累计成本（元），按 turn_end 的 est_cost_cny 求和。

    为什么不用"按单价 × 用量"重算：那条路要复刻一遍计费公式，
    一旦配置改了单价，历史数据就会跟着变——账目不能这么算。
    """
    since = _today_start_ms(now_ms)
    conn = events._connect()
    rows = conn.execute(
        "SELECT payload FROM events WHERE type='turn_end' AND ts >= ?", (since,)
    ).fetchall()
    total = 0.0
    for (payload,) in rows:
        try:
            total += float(json.loads(payload or "{}").get("est_cost_cny") or 0)
        except (ValueError, TypeError):
            pass
    return round(total, 4)


def budget_cfg():
    """取预算配置，带默认值。缺字段不该报错，只该用保守默认。"""
    cfg = config_loader.budget_config()
    return {
        "enabled": bool(cfg.get("guard_enabled", False)),
        "daily_hard_limit_cny": float(cfg.get("daily_hard_limit_cny") or 50.0),
        "daily_warn_cny": float(cfg.get("daily_warn_cny") or 5.0),
        "rate_limit_per_min": int(cfg.get("rate_limit_per_min") or 0),   # 0 = 不限
    }


def enabled():
    """闸门总开关。关着的时候 allow() 恒通过，不产生任何额外开销。"""
    return budget_cfg()["enabled"]


def _rate_ok(session_id, limit):
    """滑动窗口限流：最近 60 秒内该会话的轮次数是否还允许。

    用滑动窗口而不是固定窗口：固定窗口在边界处会放过 2 倍流量
    （59 秒来 30 次、61 秒再来 30 次），演示刷脚本时正好卡在这个缝上。
    """
    now = time.time()
    with _HITS_LOCK:
        if len(_HITS) > _HITS_MAX_SESSIONS:
            # 简单粗暴地清掉一分钟没动静的会话，够用且不会泄漏
            stale = [k for k, v in _HITS.items() if not v or now - v[-1] > 120]
            for k in stale:
                _HITS.pop(k, None)
        hits = [t for t in _HITS.get(session_id, []) if now - t < 60]
        if len(hits) >= limit:
            _HITS[session_id] = hits
            return False, len(hits)
        hits.append(now)
        _HITS[session_id] = hits
        return True, len(hits)


def allow(session_id):
    """问一句"这一轮能不能放行"。返回 (ok, reason, detail)。

    检查顺序：开关 → 日成本 → 速率。
    先查成本的因为它是全局的、更严重；速率是单会话的，慢一步无所谓。
    """
    cfg = budget_cfg()
    if not cfg["enabled"]:
        return True, "", {}

    used = daily_cost()
    if used >= cfg["daily_hard_limit_cny"]:
        return False, "DAILY_COST_LIMIT", {
            "used_cny": used,
            "limit_cny": cfg["daily_hard_limit_cny"],
            "hint": "当天累计成本已达硬上限，可上调 budget.daily_hard_limit_cny 或次日再试",
        }

    limit = cfg["rate_limit_per_min"]
    if limit > 0:
        ok, n = _rate_ok(session_id, limit)
        if not ok:
            return False, "RATE_LIMITED", {
                "per_min": limit, "count": n,
                "hint": "该会话一分钟内轮次过多，稍等再试",
            }
    return True, "", {}


def snapshot():
    """给看板/健康检查用的成本快照。"""
    cfg = budget_cfg()
    used = daily_cost()
    return {
        "enabled": cfg["enabled"],
        "today_cny": used,
        "daily_hard_limit_cny": cfg["daily_hard_limit_cny"],
        "daily_warn_cny": cfg["daily_warn_cny"],
        "rate_limit_per_min": cfg["rate_limit_per_min"],
        "warn": used >= cfg["daily_warn_cny"],
        "over_limit": used >= cfg["daily_hard_limit_cny"],
    }


def reset_limiter():
    """清空限流计数。测试用，避免用例之间互相影响。"""
    with _HITS_LOCK:
        _HITS.clear()
