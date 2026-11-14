# -*- coding: utf-8 -*-
"""生成演示种子库 `data/demo_events.db`（只读，给答辩当天看板用）。

为什么需要：
    事件库是运行时数据，部署前必须移出部署包（坏库带上线会 `database disk
    image is malformed`，11-12 出过两次）。代价是容器每次重建，看板就归零——
    而答辩习惯是"先跑几通给人看，再看板讲数据"，中间一重启就空了。

做法：
    生成一批**标了 source=demo** 的事件，写进独立的 `demo_events.db`。
    看板用 `?db=demo` 只读打开它，页面上会明确标"演示数据"。
    它和真实事件库是两个文件，永远不会互相污染：
        · 读演示库走 events 的 db_path 参数，写路径只认 events.DB_PATH
        · 演示库随部署包带上线，真实库照旧被 prep_deploy 挪进 archive

跑法：
    python scripts/make_demo_db.py              # 30 通、近 7 天
    python scripts/make_demo_db.py --sessions 40
    python scripts/make_demo_db.py --out data/demo_events.db
"""
import argparse
import json
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import events  # noqa: E402

# 固定种子：同样的命令生成同样的数据，答辩前重跑不会变出另一套数字。
_SEED = 20261112

STAGES = ["GREETING", "IDENTITY", "NEED", "OFFER", "CONFIRM", "WRITEBACK"]
UTTERANCES = [
    "是我", "尾号八八六六", "用着还行吧", "这个要钱吗", "可以，我同意", "好的",
    "不需要，谢谢", "有点吵，听不太清", "你说什么权益", "我考虑一下",
]


def _payload(base=None):
    """所有演示事件的 payload 都带 demo 标记，便于事后一眼区分。"""
    p = {"source": "demo"}
    if base:
        p.update(base)
    return p


def build(out_path=None, sessions=30, days=7, quiet=False):
    """生成演示库。返回 (out_path, sessions, events)。

    时间戳按"近 N 天"铺开，看板的近 30 天趋势才有形状；全部落在同一天的话，
    趋势图会变成一根柱子，一看就是假数据。
    """
    out_path = out_path or os.path.join(events.DATA_DIR, "demo_events.db")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)

    rnd = random.Random(_SEED)

    # 直接借用 events 的建表与写入，但把库路径临时指到演示库。
    # 用完必须还原，否则同一进程里后续写入会落到演示库上。
    real_path = events.DB_PATH
    tmp_dir = os.path.dirname(out_path) or "."
    real_dir = events.DATA_DIR
    try:
        events.DATA_DIR = tmp_dir
        events.DB_PATH = out_path
        events.close_connections()
        events.init_db()

        total = 0
        now = time.time()
        for i in range(sessions):
            # 均匀铺在近 N 天内，最后一天多放几通（演示总是当天跑）
            # 四成落在当天（演示总是当天跑），其余铺开到近 N 天，趋势图才有形状
            day_off = 0 if i < sessions * 0.4 else rnd.randrange(0, days)
            base_ts = now - day_off * 86400 - rnd.randrange(0, 3600 * 6)
            sid = "demo_%02d_%s" % (i + 1, "%06x" % rnd.randrange(0xFFFFFF))
            closed = rnd.random() < 0.6
            turns = rnd.randrange(3, 7)
            voice_turns = max(1, turns - rnd.randrange(0, 2))

            def emit(etype, payload=None, turn=0, latency=0.0, ts=None):
                nonlocal total
                events.write_event(sid, etype, _payload(payload), turn_id=turn,
                                   latency_ms=latency)
                total += 1
                # write_event 用 now_ms() 打时间戳，这里事后按需改：演示数据要铺开，
                # 所以整通电话的 ts 统一回填（直接改库，写入路径不动）。
                if ts is not None:
                    conn = events._connect()
                    conn.execute("UPDATE events SET ts=? WHERE id=(SELECT MAX(id) FROM events)",
                                 (int(ts * 1000),))

            emit("call_started", {"stage": "GREETING", "demo_index": i + 1}, ts=base_ts)
            for t in range(1, turns + 1):
                ts = base_ts + t * rnd.randrange(6, 20)
                emit("turn_start", {"source": "asr" if t <= voice_turns else "text"},
                     turn=t, ts=ts)

                if t <= voice_turns:
                    emit("asr_result", {"text": rnd.choice(UTTERANCES),
                                        "provider": "demo", "fake": True,
                                        "latency_ms": rnd.randrange(260, 620)},
                         turn=t, ts=ts + 0.6)

                stage = STAGES[min(t - 1, len(STAGES) - 1)]
                emit("agent_reply", {"stage": stage,
                                     "text": "（演示话术，第 %d 轮）" % t},
                     turn=t, ts=ts + 0.8)

                if t == 1:
                    emit("tool_call", {"name": "query_customer", "args": {"keyword": "8866"}},
                         turn=t, ts=ts + 0.9)
                    emit("tool_result", {"found": True, "customer_id": "C1001"},
                         turn=t, ts=ts + 0.95)

                proc_ms = rnd.randrange(680, 1250)
                if closed and t == turns:
                    emit("tool_call", {"name": "create_ticket", "args": {"session_id": sid}},
                         turn=t, ts=ts + 2.0)
                    emit("tool_result", {"ticket_id": "TD%06d" % rnd.randrange(999999),
                                         "created": True}, turn=t, ts=ts + 2.05)
                    emit("writeback", {"ticket_id": "TD%06d" % rnd.randrange(999999),
                                       "created": True, "version": 1}, turn=t, ts=ts + 2.1)

                emit("turn_end", {"source": "asr" if t <= voice_turns else "text",
                                  "processing_ms": proc_ms,
                                  "server_first_audio_ms": proc_ms * 0.62,
                                  "est_cost_cny": round(rnd.uniform(0.002, 0.006), 4)},
                     turn=t, latency=proc_ms, ts=ts + proc_ms / 1000.0)

                # 前端上报的首字（T4−T0）：只语音轮有，比服务端处理再慢一点
                if t <= voice_turns:
                    emit("latency_report", {"turn_id": t,
                                            "first_audio_ms": proc_ms + rnd.randrange(120, 420),
                                            "estimate": False},
                         turn=t, ts=ts + proc_ms / 1000.0 + 0.5)

            if not closed and rnd.random() < 0.35:
                emit("no_answer", {"text": ""}, turn=turns, ts=base_ts + turns * 10)

            emit("call_ended", {"closed": closed, "turns": turns},
                 ts=base_ts + turns * 10 + 5)
    finally:
        events.close_connections()
        events.DB_PATH = real_path
        events.DATA_DIR = real_dir

    if not quiet:
        print("演示库已生成：%s" % out_path)
        print("  %d 通会话 / %d 条事件 / 近 %d 天" % (sessions, total, days))
        print("  看板打开方式：http://127.0.0.1:8000/dashboard?db=demo")
    return out_path, sessions, total


def main():
    ap = argparse.ArgumentParser(description="生成演示种子库（只读）")
    ap.add_argument("--sessions", type=int, default=30)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    build(out_path=args.out, sessions=args.sessions, days=args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
