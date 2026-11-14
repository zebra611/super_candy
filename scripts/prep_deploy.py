# -*- coding: utf-8 -*-
"""部署前把运行时事件库挪出部署包（11-12 线上事故的防线之一）。

为什么必须挪：`data/events.db` 是**运行时数据**，不是代码。部署工具会把整个
项目目录打包上传，于是本地正在写的库（含 `-wal`）被一起传上去；容器里拿到的
是一个半写状态的文件，SQLite 一读就报 `database disk image is malformed`。
11-12 19:5x 的“点文字对话提示服务没连上”就是这么来的。

做法：checkpoint → 移到 `data/archive/<名字>-<时间戳>.db`，部署包里就只剩代码。
本地看板要查历史时，从 archive 里拷回来即可。

**两个库都要挪，不只是 events.db**：
`data/tickets.db`（幂等工单库）同样是运行时数据。留在包里会有两种后果：
带上上一轮的工单 → 演示"同一通电话只建一张单"时可能读到旧单，看着幂等生效、
实际是别的通话（假绿）；或者库结构换了版本，容器里直接报错。
`tools.idempotency_store` 现在是 memory（默认不开 sqlite），但一旦有人把配置改成
sqlite，这里不跟着改就会踩坑——所以清单写死在下面一张表里，别再单点判断。

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

# 运行时库清单：(标签, 路径)。加新库必须加到这里，否则它会跟着部署包上线。
# verify.py 有一条断言专门盯这张表与 adapters.store.DEFAULT_PATH 是否一致——
# 防的就是"新加了库、清理清单忘了改"。
RUNTIME_DBS = [
    ("events", DB),
    ("tickets", os.path.join(DATA, "tickets.db")),
]


def _check_and_move(label, path, keep, stamp):
    """体检一个库；不 keep 就移进 archive。返回被移走的文件名列表。"""
    if not os.path.exists(path):
        return []
    size_mb = os.path.getsize(path) / 1024.0 / 1024.0
    try:
        conn = sqlite3.connect(path)
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except sqlite3.DatabaseError as exc:
        ok = "损坏：%s" % exc
    print("%s 库 %.1f MB，integrity=%s" % (label, size_mb, ok))

    if keep:
        return []

    moved = []
    for suffix in ("", "-wal", "-shm"):
        src = path + suffix
        if os.path.exists(src):
            dst = os.path.join(ARCHIVE, "%s-%s.db%s" % (label, stamp, suffix))
            shutil.move(src, dst)
            moved.append(os.path.basename(dst))
    return moved


def ensure_demo_db():
    """确保部署包里有演示种子库；已存在就不动（避免每次部署换一套数字）。"""
    demo = os.path.join(DATA, "demo_events.db")
    if os.path.exists(demo):
        return demo
    try:
        from scripts.make_demo_db import build as _build_demo
    except ImportError:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        from make_demo_db import build as _build_demo
    _build_demo(out_path=demo, quiet=True)
    print("已生成演示种子库：data/demo_events.db（看板 ?db=demo 可看）")
    return demo


def main():
    ap = argparse.ArgumentParser(description="部署前把运行时库移出部署包")
    ap.add_argument("--keep", action="store_true", help="只体检，不移文件")
    args = ap.parse_args()

    present = [(l, p) for l, p in RUNTIME_DBS if os.path.exists(p)]
    if not present:
        print("没有本地运行时库，部署包本来就是干净的")
        if not args.keep:
            ensure_demo_db()
        return 0

    if args.keep:
        for label, path in RUNTIME_DBS:
            _check_and_move(label, path, True, "")
        return 0

    os.makedirs(ARCHIVE, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    moved = []
    for label, path in RUNTIME_DBS:
        moved += _check_and_move(label, path, False, stamp)

    print("已移出部署包：%s" % (", ".join(moved) if moved else "无"))
    print("要查历史：把 archive 里的 events-*.db 拷回 data/events.db 即可")

    # 演示种子库：真实库移走之后看板会归零；随包带一份只读种子库，
    # 答辩当天即使容器重建过，看板仍有数据可讲（页面上标明是演示数据）。
    ensure_demo_db()

    # 移完再扫一遍：给"清单漏了某个库"兜底。demo_events.db 是有意留下的，
    # 不算漏——它只读、且页面上明确标注，不参与真实统计。
    leftovers = sorted(
        f for f in os.listdir(DATA)
        if f.endswith(".db") and f != "demo_events.db")
    if leftovers:
        print("⚠ data/ 下仍有未归档的库：%s（确认它们该不该进部署包）"
              % ", ".join(leftovers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
