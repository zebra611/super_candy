# -*- coding: utf-8 -*-
"""C6 标注校验：覆盖、原文完整性、空话术隔离、可复算（含反向变异自测）。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "config", "kb_pop.json")
sys.path.insert(0, ROOT)
from scripts.gen_c6_annotations import CLASSES, placeholder_style  # noqa: E402


def validate(data):
    problems, stats = [], {}
    items = data.get("items") or []
    meta = (data.get("_meta") or {}).get("noise") or {}
    stats["count"] = len(items)
    stats["classes"] = len(meta.get("classes") or [])
    stats["annotated"] = sum(1 for r in items if r.get("noise"))

    if len(items) != 96:
        problems.append("条目数变了：%d（应 96）" % len(items))
    if stats["classes"] != len(CLASSES):
        problems.append("噪声类别数不符：%d（应 %d）" % (stats["classes"], len(CLASSES)))
    codes = {c["code"] for c in CLASSES}
    for r in items:
        for c in r.get("noise") or []:
            if c not in codes:
                problems.append("%s 标了未登记噪声码 %s" % (r["id"], c))
        if "noise" in r and not r["noise"]:
            problems.append("%s 的 noise 是空数组" % r["id"])
        # 占位符风格必须与原文一致（防标注漂移）
        if r.get("placeholder_style") and r["placeholder_style"] != placeholder_style(r.get("answer") or ""):
            problems.append("%s 的 placeholder_style 与原文不符" % r["id"])
        # 空话术必须被检索层隔离
        if not (r.get("answer") or "").strip() and not r.get("no_call"):
            problems.append("%s 是空话术但没打 no_call，会被播出空回复" % r["id"])
    if stats["annotated"] < 60:
        problems.append("标注覆盖过少：%d 条" % stats["annotated"])
    stats["counts"] = meta.get("counts") or {}
    return problems, stats


def main():
    raw = open(PATH, "rb").read()
    data = json.loads(raw.decode("utf-8"))
    problems, stats = validate(data)
    print("=" * 66)
    print("C6 标注校验：%d 条 / %d 类噪声 / %d 条带标注" % (
        stats["count"], stats["classes"], stats["annotated"]))
    print("各类命中：%s" % stats["counts"])
    if b"\r\n" in raw:
        problems.append("kb_pop.json 出现 CRLF")

    # 反向自测：故意改坏，校验器必须报红
    cases = {
        "删掉空话术的 no_call": lambda d: [r for r in d["items"]
                                        if not (r.get("answer") or "").strip()][0].pop("no_call", None),
        "标注码写错": lambda d: d["items"][0].__setitem__("noise", ["C6-XX"]),
        "删掉噪声类别": lambda d: d["_meta"]["noise"]["classes"].pop(),
        "原文被改写": lambda d: d["items"][0].__setitem__("answer", ""),
    }
    for name, mutate in cases.items():
        bad = copy.deepcopy(data)
        mutate(bad)
        probs, _ = validate(bad)
        caught = bool(probs)
        print("  [%s] 变异「%s」→ %s" % ("PASS" if caught else "FAIL", name,
                                        probs[0] if probs else "校验器没抓到"))
        if not caught:
            problems.append("反向自测没抓到：" + name)

    # 可复算：重新生成一次，逐字节比较
    import subprocess
    before = hashlib.sha256(raw).hexdigest()
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                 "gen_c6_annotations.py")],
                   cwd=ROOT, check=True, capture_output=True)
    after = hashlib.sha256(open(PATH, "rb").read()).hexdigest()
    if before != after:
        problems.append("标注不可复算（重复生成结果不一致）")
    else:
        print("  [PASS] 重复生成逐字节一致")

    print("-" * 66)
    if problems:
        print("FAIL %d 项：" % len(problems))
        for p in problems[:10]:
            print("  -", p)
    else:
        print("全部通过：覆盖/原文完整性/空话术隔离/可复算/反向自测")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
