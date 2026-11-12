# -*- coding: utf-8 -*-
"""部署前把运行时事件库挪出部署包（11-12 线上事故的防线之一）。

为什么必须挪：`data/events.db` 是**运行时数据**，不是代码。部署工具会把整个
项目目录打包上传，于是本地正在写的库（含 `-wal`）被一起传上去；容器里拿到的
是一个半写状态的文件，SQLite 一读就报 `database disk image is malformed`。
11-12 19:5x 的“点文字对话提示服务没连上”就是这么来的。

做法：checkpoint → 移到 `data/archive/events-<时间戳>.db`，部署包里就只剩代码。
本地看板要查历史时，从 archive 里拷回来即可。

跑法：
    python scripts/prep_deploy.py          # 挪走
    python scripts/prep_deploy.py --keep   # 只看体检结果，不动文件
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA = os.path.join(ROOT, "data")
DB = os.path.join(DATA, "events.db")
ARCHIVE = os.path.join(DATA, "archive")


def main():
    ap = argparse.ArgumentParser(description="部署前把事件库移出部署包")
    ap.add_argument("--keep", action="store_true", help="只体检，不移文件")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print("没有本地事件库，部署包本来就是干净的")
        return 0

    size_mb = os.path.getsize(DB) / 1024.0 / 1024.0
    try:
        conn = sqlite3.connect(DB)
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except sqlite3.DatabaseError as exc:
        ok = "损坏：%s" % exc
    print("事件库 %.1f MB，integrity=%s" % (size_mb, ok))

    if args.keep:
        return 0

    os.makedirs(ARCHIVE, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for suffix in ("", "-wal", "-shm"):
        src = DB + suffix
        if os.path.exists(src):
            dst = os.path.join(ARCHIVE, "events-%s.db%s" % (stamp, suffix))
            shutil.move(src, dst)
            moved.append(os.path.basename(dst))
    print("已移出部署包：%s" % ", ".join(moved))
    print("要查历史：把 archive 里的文件拷回 data/events.db 即可")
    return 0


if __name__ == "__main__":
    sys.exit(main())
