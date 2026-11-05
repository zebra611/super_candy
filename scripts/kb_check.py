# -*- coding: utf-8 -*-
"""知识库体检工具 —— 给人看的，不是给机器看的。

跑法：
    python3 scripts/kb_check.py                # 体检：统计 + 硬校验 + 抢占告警
    python3 scripts/kb_check.py --list         # 列出全部条目（编号/类别/出处/问法数）
    python3 scripts/kb_check.py --ask "能用多久"  # 问一句，看它怎么打分、为什么命中它
    python3 scripts/kb_check.py --probe        # 跑内置探针集，报命中准确率

为什么单独做一个脚本：
    `scripts/verify.py` 是"代码没错"的证据，`scripts/test_kb.py` 是"逻辑没错"的证据，
    但内容对不对（问法够不够、出处全不全、有没有过期条目）是**人**要判断的事。
    答辩现场被问"你怎么保证不是瞎答"，这个脚本的输出就是答案：
    每个答案都能指回某个条目的某条资料小节。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import kb  # noqa: E402

# 内置探针：一条命中的问法 + 一条应当被拒的问法，覆盖主要类别。
# 这些不是评测用例（那在 eval/ 里，归 3 号），只是内容体检的自查表。
PROBES = [
    ("这个要花钱吗", "KB-FEE-001"),
    ("这个权益能用多久", "KB-VALIDITY-001"),
    ("什么时候生效", "KB-TRIGGER-001"),
    ("保什么呀", "KB-COVERAGE-001"),
    ("怎么报修", "KB-REPAIR-001"),
    ("我不想要了能取消吗", "KB-CANCEL-001"),
    ("能退钱吗", "KB-REFUND-001"),
    ("你们怎么知道我的号码", "KB-PRIVACY-001"),
    ("为什么问我这些", "KB-IDENTITY-001"),
    ("你们是不是骗子", "KB-SALES-001"),
    ("能开发票吗", "KB-INVOICE-001"),
    ("我是不是买过了", "KB-DUPLICATE-001"),
    ("帮我转人工", "KB-TRANSFER-001"),
    ("我要投诉你们", "KB-COMPLAINT-001"),
    ("到期了怎么办", "KB-RENEW-001"),
    ("你们是哪个公司", "KB-OWNER-001"),
    ("你们几点上班", "KB-HOURS-001"),
    ("多久能上门", "KB-ONSITE-001"),
    ("我现在能用吗", "KB-URGENT-001"),
    ("能不能只要一项", "KB-PARTIAL-001"),
    ("最近有活动吗", "KB-PROMO-001"),
    ("你们公司股票能买吗", None),
    ("那你们老板是谁", None),
    ("今天天气怎么样", None),
]


def _head(title):
    print("\n" + title)
    print("─" * 62)


def overview(k):
    _head("① 概况")
    st = k.stats()
    print("  文件    %s" % st["path"])
    print("  版本    %s" % (st["version"] or "(未标)"))
    print("  条目    %d 条，覆盖 %d 类业务问题，引用 %d 处资料"
          % (st["items"], len(st["categories"]), st["sources"]))
    if st["placeholder"]:
        print("  ⚠️  内容为**占位**：官方资料包公布后必须逐条替换 answer 与 source_id，")
        print("     替换完把 config/kb.json 的 _meta.placeholder 改成 false。")
    print("\n  按类分布：")
    for cat, n in sorted(st["categories"].items(), key=lambda x: (-x[1], x[0])):
        print("    %-12s %d" % (cat, n))

    _head("② 硬校验（缺字段 / 重复 id / 禁用语 / 日期倒置 / 关键词共用）")
    problems = k.validate()
    if problems:
        for p in problems:
            print("  ✗ %s" % p)
        print("\n  → %d 个问题，修完才算可用。" % len(problems))
    else:
        print("  ✓ 全部通过（0 问题）")

    _head("③ 抢占告警（短词被长词包含，可能答串题）")
    warns = k.conflicts()
    if warns:
        for w in warns:
            print("  · %s" % w)
        print("\n  → 这是提示不是错误：出现时给长词那条补更具体的问法即可。")
    else:
        print("  ✓ 无")
    return len(problems)


def list_items(k):
    _head("条目清单")
    print("  %-18s %-10s %-6s %s" % ("编号", "类别", "问法数", "出处"))
    for it in k.items:
        print("  %-18s %-10s %-6d %s"
              % (it.get("id"), it.get("category") or "-",
                 len(it["_patterns"]), it.get("source_id")))


def ask(k, text):
    _head("问：%s" % text)
    rows = k.explain(text, top=5)
    if not rows:
        print("  没有任何候选（文本为空）")
        return
    for i, r in enumerate(rows):
        # 只有排第一且真的命中才打勾：其余即使命中也只是"候选"，
        # 让人一眼看出谁是赢家、谁只是也跟着命中了。
        flag = "✓" if (i == 0 and not r["reason"]) else "·"
        note = r["reason"] or "命中"
        print("  %s %-18s 分=%-5d %-10s 命中词=%s"
              % (flag, r["id"], r["score"], note, "、".join(r["matched"]) or "-"))
    hit = k.match(text)
    print()
    if hit:
        print("  答：%s" % hit["answer"])
        print("  依据：%s" % hit["source_id"])
    else:
        print("  答：（未命中，会走 reply_unknown 兜底）")


def probe(k):
    _head("探针集")
    ok = 0
    for text, want in PROBES:
        hit = k.match(text)
        got = hit["id"] if hit else None
        if got == want:
            ok += 1
            print("  ✓ %-22s → %s" % (text, got or "(正确拒答)"))
        else:
            print("  ✗ %-22s → 期望 %s，实际 %s" % (text, want, got))
    total = len(PROBES)
    print("\n  命中准确 %d/%d = %.1f%%" % (ok, total, ok * 100.0 / total))
    return total - ok


def main(argv):
    k = kb.load(force=True)
    if "--list" in argv:
        list_items(k)
        return 0
    if "--ask" in argv:
        i = argv.index("--ask")
        text = argv[i + 1] if i + 1 < len(argv) else ""
        if not text:
            print("用法：python3 scripts/kb_check.py --ask \"这个要花钱吗\"")
            return 2
        ask(k, text)
        return 0
    if "--probe" in argv:
        return 1 if probe(k) else 0

    problems = overview(k)
    if not problems and "--no-probe" not in argv:
        probe(k)
    print()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
