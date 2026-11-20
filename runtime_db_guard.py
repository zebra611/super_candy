"""运行时库自检 —— 部署时拒绝带旧幂等库启动（P3-3）。

为什么只查 tickets.db，不查 events.db：
    events.db 是 app 每次启动自己 init_db() 维护的事件流，第二次启动必然存在，
    把它算成"旧库"会让任何部署过的实例重启就被拒——不可行。
    tickets.db 则不同：幂等存储默认是 memory（tools.idempotency_store），app
    **绝不会自己创建**它。它若出现在部署包里，只能是本地带过来的旧工单库——
    演示"同一通电话只建一张单"时会读到别的通话的旧单，看着幂等生效，其实是假绿。
    这正是清单 P3-3 要堵的洞。

什么时候检查：
    只在**部署模式**（app.py 以 PORT 环境变量启动）检查。本地开发照常用库，
    行为逐字节不变——本地跑批/自练本来就会写 tickets.db。

逃生舱：
    ALLOW_STALE_DB=1 显式放行。用于"确实要带旧库上线查历史"的场合，
    用完记得关；默认严格拦截。
"""

import os

# 只列"app 自己不会创建、存在即为遗留"的库。加新库要同时想清楚
# app 会不会自己建它——会不会误伤重启，是这张清单的唯一判据。
STALE_DBS = ("tickets.db",)

_ENV_BYPASS = "ALLOW_STALE_DB"


def find_stale(data_dir):
    """返回部署包里残留的旧库文件名列表（相对名）。"""
    found = []
    for name in STALE_DBS:
        if os.path.exists(os.path.join(data_dir, name)):
            found.append(name)
    return found


def check(data_dir, deployed):
    """部署模式下发现旧库就抛 SystemExit；否则静默返回。

    deployed 由 app.py 传入（它用 "PORT" in os.environ 判断）。
    返回被跳过的库名（放行时）或空列表，便于日志与测试断言。
    """
    if not deployed:
        return []
    if os.environ.get(_ENV_BYPASS, "").strip() in ("1", "true", "yes"):
        return find_stale(data_dir)
    stale = find_stale(data_dir)
    if not stale:
        return []
    raise SystemExit(
        "拒绝启动：部署包里带了旧运行时库 %s。\n"
        "  这些库会让幂等读到本地旧单、看着生效其实是假绿。\n"
        "  修法：部署前跑 `python scripts/prep_deploy.py` 把库移进 data/archive/。\n"
        "  确需保留：设 ALLOW_STALE_DB=1 显式放行（用完关掉）。"
        % ", ".join("data/" + n for n in stale))
