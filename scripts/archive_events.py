"""事件库归档（M5）—— 把旧事件搬到归档库，主库保持轻。

为什么需要：
    事件表只会变大。看板的按天聚合、质检的整通读取都会慢慢变慢，
    备份也会越来越久。归档不是清理，是"分层"：热数据留在主库，
    冷数据进 data/archive/，需要时 ATTACH 就能查。

用法：
    python scripts/archive_events.py                  # 预演，只报会搬多少
    python scripts/archive_events.py --days 30 --yes  # 真的搬走 30 天前的
    python scripts/archive_events.py --stats          # 只看库体检，不动数据

安全：
    默认 dry-run。要真搬必须显式给 --yes——归档是不可逆的（虽然数据还在归档库，
    但跨库查询需要额外一步，运营同学未必知道）。

归档之后主库不会自动变小：SQLite 删除行只标记空闲页。需要回收磁盘就再跑
`python scripts/archive_events.py --vacuum`（会在整理前先备份）。
"""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import events   # noqa: E402


def _fmt_ts(ms):
    if not ms:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000.0))


def print_stats():
    s = events.db_stats()
    print("事件库：%s" % s["db_path"])
    print("  体积：%s MB    行数：%s    会话：%s"
          % (s["size_mb"], s["events"], s["sessions"]))
    print("  时间范围：%s → %s" % (_fmt_ts(s["oldest_ms"]), _fmt_ts(s["newest_ms"])))
    print("  索引：%d 个（%s）" % (s["index_count"], ", ".join(s["indexes"])))
    return s


def main(argv):
    days = 30
    if "--days" in argv:
        i = argv.index("--days")
        try:
            days = int(argv[i + 1])
        except (IndexError, ValueError):
            print("--days 需要一个整数，例如 --days 30")
            return 2

    if "--stats" in argv:
        print_stats()
        return 0

    events.init_db()
    print_stats()
    print()

    dry = events.archive_events(before_days=days, dry_run=True)
    if not dry["moved"]:
        print("没有早于 %d 天的事件，不用归档。" % days)
        if "--vacuum" in argv:
            print("（没有删除行，vacuum 也没意义，跳过）")
        return 0

    print("早于 %d 天（%s 之前）的事件共 %d 条。"
          % (days, _fmt_ts(dry["cutoff_ms"]), dry["moved"]))

    if "--yes" not in argv:
        print("\n这是预演。要真的搬走，加上 --yes：")
        print("  python scripts/archive_events.py --days %d --yes" % days)
        return 0

    # 搬之前先整库备份：归档逻辑再简单，也不该拿唯一的数据副本赌
    backup = events.DB_PATH + ".bak-%s" % time.strftime("%Y%m%d%H%M%S")
    if os.path.exists(events.DB_PATH):
        events.close_connections()
        shutil.copy2(events.DB_PATH, backup)
        print("已备份主库 → %s" % backup)

    res = events.archive_events(before_days=days, dry_run=False)
    print("已归档 %d 条 → %s" % (res["moved"], res["target"]))

    if "--vacuum" in argv:
        print("正在整理主库（VACUUM）…")
        events.close_connections()
        conn = events._connect()
        conn.execute("VACUUM")
        conn.commit()
        print("整理完成。")

    print()
    print_stats()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
