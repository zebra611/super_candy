"""清理事件库里的测试噪声，保留真人通话。

背景：
    run_eval.py 直接往生产库写事件（verify.py 用临时库，它没有）。
    跑一次 30 条用例就多 30 个 session，跑十次就是 300 个。
    结果是面板上"305 通电话"，其中真人才 2 通——指标全被 mock 数据带偏：
    测试用例的 asr_result 耗时是 0.0ms，混进分位数会把 P50 直接拉到几十毫秒，
    看着很漂亮，实际是假的。

判定规则（宁可漏删，不可错删）：
    测试会话 = 所有 asr_result 的 latency_ms 都 <= 0（真识别不可能 0 毫秒）
    空壳会话 = 既没有 turn_end 也没有 asr_result（start 后直接 end 的残留）

    真人通话 = 有 asr_result 且 latency_ms > 0。这类一律保留，哪怕只有一轮。

用法：
    python scripts/clean_events.py            # 只预览，不动库
    python scripts/clean_events.py --yes      # 真删（先自动备份）
    python scripts/clean_events.py --yes --archive-only   # 只导出归档，不删
"""

import json
import os
import shutil
import sqlite3
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import events  # noqa: E402

BACKUP_DIR = os.path.join(_ROOT, "data", "backup")
ARCHIVE_DIR = os.path.join(_ROOT, "data", "archive")


def classify(conn):
    """把每个 session 归到三类：真人 / 测试 / 空壳。"""
    rows = conn.execute("""
        select session_id,
               sum(type='turn_end')            as turns,
               sum(type='asr_result')          as asr_n,
               sum(type='asr_result' and
                   json_extract(payload,'$.latency_ms') > 0) as asr_real
        from events group by session_id
    """).fetchall()

    real, test, empty = [], [], []
    for sid, turns, asr_n, asr_real in rows:
        if asr_real:
            real.append(sid)
        elif asr_n:
            test.append(sid)          # 有识别但耗时全 0 → mock
        else:
            empty.append(sid)         # 连识别都没有 → 空壳
    return real, test, empty


def export_jsonl(conn, session_ids, path):
    """把指定会话的全部事件导出成 jsonl，按 session 分组，便于事后复查。"""
    if not session_ids:
        return 0
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for sid in session_ids:
            for eid, tid, ts, ty, payload, lat, ver in conn.execute(
                    "select id,turn_id,ts,type,payload,latency_ms,version "
                    "from events where session_id=? order by id", (sid,)):
                f.write(json.dumps({
                    "session_id": sid, "event_id": eid, "turn_id": tid,
                    "ts": ts, "type": ty,
                    "payload": json.loads(payload), "latency_ms": lat,
                    "version": ver,
                }, ensure_ascii=False) + "\n")
                n += 1
    return n


def main():
    do_delete = "--yes" in sys.argv
    archive_only = "--archive-only" in sys.argv

    if not os.path.exists(events.DB_PATH):
        print("找不到事件库：%s" % events.DB_PATH)
        return 1

    conn = sqlite3.connect(events.DB_PATH)
    real, test, empty = classify(conn)
    total = len(real) + len(test) + len(empty)

    ev_count = conn.execute("select count(*) from events").fetchone()[0]

    print("事件库：%s" % events.DB_PATH)
    print("  会话 %d 个 / 事件 %d 条" % (total, ev_count))
    print()
    print("  真人通话（有真实识别耗时）  %3d 个  ← 保留" % len(real))
    for sid in real:
        txts = [json.loads(p).get("text") for (p,) in conn.execute(
            "select payload from events where session_id=? and type='asr_result' "
            "order by id", (sid,))]
        t0 = conn.execute("select min(ts) from events where session_id=?",
                          (sid,)).fetchone()[0]
        when = time.strftime("%m-%d %H:%M", time.localtime(t0 / 1000))
        print("      %s  %s  %s" % (sid, when, (txts or [])[:4]))
    print()
    print("  测试跑批（识别耗时全为 0）   %3d 个  ← 清理" % len(test))
    print("  空壳会话（无轮次无识别）     %3d 个  ← 清理" % len(empty))

    victims = test + empty
    if not victims:
        print("\n没有需要清理的。")
        return 0

    if not do_delete:
        print("\n以上是预览。确认无误后加 --yes 真删（会先自动备份整个库）。")
        return 0

    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    backup = os.path.join(BACKUP_DIR, "events_%s_before_clean.db" % stamp)
    shutil.copy2(events.DB_PATH, backup)
    print("\n已备份 → %s" % backup)

    arch = os.path.join(ARCHIVE_DIR, "test_sessions_%s.jsonl" % stamp)
    n = export_jsonl(conn, victims, arch)
    print("已归档 → %s（%d 条事件）" % (arch, n))

    if archive_only:
        print("--archive-only：只归档，不删除。")
        return 0

    ph = ",".join("?" * len(victims))
    conn.execute("delete from events where session_id in (%s)" % ph, victims)
    conn.commit()

    left = conn.execute("select count(*), count(distinct session_id) from events").fetchone()
    print("\n已删除 %d 个会话。" % len(victims))
    print("剩余：%d 条事件 / %d 个会话（全部是真人通话）" % (left[0], left[1]))
    print("备份留在 data/backup/，要恢复直接用它覆盖 data/events.db 即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
