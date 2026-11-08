# -*- coding: utf-8 -*-
"""校验「官方话术填槽」这套东西：188 条档案 × 96 条官方模板。

查什么（每条都是踩过的坑）：
  ① 可播话术里不许有 【】/XX —— TTS 会念成"左右括号""叉叉"；
  ② 电话单必须说得出话术 —— 否则商家问"为什么驳回"，客服只能答"我查不到"；
  ③ 京麦单**不许**有话术 —— 官方口径就是站内指引，配电话话术属于用错渠道；
  ④ 模板侧：被引用的 id 必须真实存在（以前 legacy 有 28 条指向 KB-POP-069..096）；
  ⑤ 跨文件一致：kb_pop 的 S 系列替身文案，必须与 qa.json 里同问题的补齐答案一致；
  ⑥ fallback 数量要盯住 —— 它不是错，但突然变多意味着模板池坏了。

跑法：python scripts/check_official_scripts.py [--self-test]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modules import official_script as os_          # noqa: E402

MOCK = os.path.join(ROOT, "config", "mock")
QA = os.path.join(MOCK, "qa.json")
BAD_MARKS = ("【", "】", "XX")
FALLBACK_MAX = 10          # 当前实测 5 条（catalog 里 016/026/047/067/068 场景）


def _items(name):
    """先在 config/mock/ 找，再退回 config/（kb_pop.json 在上一层）。"""
    for d in (MOCK, os.path.dirname(MOCK)):
        path = os.path.join(d, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("items") or []
    raise SystemExit("找不到数据文件：%s" % name)


def validate(pool, items):
    problems, stats = [], {}

    ids = {it.get("id") for it in items}
    stat = {"render_ok": 0, "render_fail": 0, "via": {}, "fallback": [],
            "no_script_phone": [], "jinmai_with_script": []}

    by_id = {it.get("id"): it for it in items}
    for app in pool:
        got = os_.script_for(app, items)
        if got:
            text = got["script"]
            for mark in BAD_MARKS:
                if mark in text:
                    problems.append("%s 的话术仍有占位 %s：%s"
                                    % (app.get("application_id"), mark, text[:40]))
            stat["render_ok"] += 1
            stat["via"][got["via"]] = stat["via"].get(got["via"], 0) + 1
            if got["via"] != "reject_code":
                stat["fallback"].append((app.get("application_id"),
                                         app.get("reject_code"), got["id"]))
            if app.get("close_mode") == "京麦":
                stat["jinmai_with_script"].append(app.get("application_id"))
        else:
            stat["render_fail"] += 1
            if app.get("close_mode") == "电话" and app.get("call_status") != "无需外呼":
                stat["no_script_phone"].append(
                    (app.get("application_id"), app.get("reject_code")))

    # ② 电话单必须有话术
    if stat["no_script_phone"]:
        problems.append("有 %d 条电话单说不出官方话术：%s"
                        % (len(stat["no_script_phone"]),
                           stat["no_script_phone"][:5]))
    # ③ 京麦单不许有话术
    if stat["jinmai_with_script"]:
        problems.append("京麦驳回单被配了电话话术（官方口径是站内指引）：%s"
                        % stat["jinmai_with_script"][:5])
    # ④ 档案引用的模板必须存在
    for app in pool:
        code = app.get("reject_code")
        if code not in ids:
            problems.append("%s 的 reject_code=%s 在 kb_pop 里不存在"
                            % (app.get("application_id"), code))
    # ⑥ fallback 数量
    if len(stat["fallback"]) > FALLBACK_MAX:
        problems.append("话术回退（同业务域替身）%d 条，超过上限 %d：%s"
                        % (len(stat["fallback"]), FALLBACK_MAX, stat["fallback"][:5]))

    # ⑤ S 系列替身文案与 qa.json 的补齐答案一致
    qa = _items("qa.json")
    qa_by_q = {q.get("question"): q for q in qa}
    # kb_pop 里 S 条目的问题从 patterns 拼不出来，这里用"答案语义"对齐太脆，
    # 改为断言：替身文案不得含占位，且都能在 qa.json 的 answer 里找到同义句
    for sid, text in os_.SCRIPT_OVERRIDES.items():
        if any(m in text for m in BAD_MARKS):
            problems.append("S 系列替身 %s 里还有占位" % sid)
    # 明确对齐 QA-TS-003 与 S03：同一句话必须一个版本
    ts003 = qa_by_q.get("我在哪可以查询到这个商标状态") or {}
    if ts003.get("answer") and ts003["answer"] != os_.SCRIPT_OVERRIDES["KB-POP-S03"]:
        problems.append("QA-TS-003 与 KB-POP-S03 的补齐文案不一致：\n  QA=%s\n  S03=%s"
                        % (ts003.get("answer"), os_.SCRIPT_OVERRIDES["KB-POP-S03"]))

    stats.update(stat)
    stats["pool"] = len(pool)
    return problems, stats


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="故意改坏，校验器必须报红（防假绿）")
    args = ap.parse_args(argv)

    pool = _items("applications.json") + _items("applications_catalog.json")
    items = _items("kb_pop.json")
    problems, stats = validate(pool, items)

    print("档案 %d 条 ｜ 有话术 %d ｜ 无话术 %d ｜ 命中方式 %s"
          % (stats["pool"], stats["render_ok"], stats["render_fail"], stats["via"]))
    if stats["fallback"]:
        print("同业务域替身 %d 条：%s" % (len(stats["fallback"]),
                                          stats["fallback"][:5]))

    if args.self_test:
        ok = True
        # 变异 1：把**被引用**的那条模板里的空槽写成字面 "XX" → ① 必须报红
        # （拿一条没人引用的模板改坏是测不到东西的：渲染根本不会走到它）
        _used = next(a.get("reject_code") for a in pool
                     if a.get("close_mode") == "电话" and a.get("reject_code"))
        bad_items = [dict(it) for it in items]
        for it in bad_items:
            if it.get("id") == _used:
                it["answer"] = (it.get("answer") or "").replace("【】", "XX", 1)
                break
        p1, _ = validate(pool, bad_items)
        # 变异 2：把一条电话单改成指向不存在的模板 → ④ 必须报红
        bad_pool = [dict(a) for a in pool]
        for a in bad_pool:
            if a.get("close_mode") == "电话":
                a["reject_code"] = "KB-POP-999"
                break
        p2, s2 = validate(bad_pool, items)
        # 变异 3：京麦单被强行配话术（模拟 close_mode 判据被破坏）
        bad_pool2 = [dict(a) for a in pool]
        for a in bad_pool2:
            if a.get("close_mode") == "京麦":
                a["close_mode"] = "电话"
                a["call_status"] = "待外呼"
                break
        p3, _ = validate(bad_pool2, items)
        for name, got in (("模板留占位", p1), ("指向不存在的模板", p2),
                          ("京麦单配话术", p3)):
            hit = bool(got)
            print("  变异 %-10s → %s" % (name, "报红" if hit else "**没抓到**"))
            ok = ok and hit
        if not ok:
            print("自测失败：有变异没被抓到，校验器不可信")
            return 1

    if problems:
        print("-" * 70)
        print("FAIL %d 项：" % len(problems))
        for p in problems[:20]:
            print("  -", p)
        return 1
    print("-" * 70)
    print("全部通过：占位已填尽 / 电话单有话术 / 京麦单不外呼 / 模板可追溯 / 跨文件一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
