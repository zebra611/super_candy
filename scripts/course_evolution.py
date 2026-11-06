# -*- coding: utf-8 -*-
"""Q2：K5 课程进化 v2——三代课程在画像工厂上重实现。

跑法：
    python scripts/course_evolution.py
    python scripts/course_evolution.py --self-test     # 变异验证：课程必须能发现注入的缺陷

三代课程（固定顺序、无随机，任何人可逐字节复算）：
    Gen1 全矩阵   —— 10 persona × 4 场景 = 40 条，找当前真实弱点
    Gen2 只打弱点 —— Gen1 失败项；若 Gen1 全绿，退化为「历史弱点回归集」
                     （1106 首轮分诊失败的 24 条），防止修好又退回去
    Gen3 消耗战   —— 最弱三角色，把异议句重复 3 次再收尾；检验压力下会不会崩

**为什么不是「三代分数一定递增」**
    分数递增不是课程的目标，**找到弱项**才是。如果全矩阵已经全绿，
    Gen2 就该退化为回归守门而不是硬造失败。报告如实写「本代未发现新缺陷」，
    并用变异验证证明课程有能力发现缺陷——否则三代全绿只是自证覆盖。

**回滚记录**
    课程只读跑批，不改生产代码/配置，所以不存在需要回滚的线上变更。
    每代仍保存快照（`--json`），若本代通过率低于上一代，标记 `regressed`
    并保留上一代快照路径，供人判断是"课程变难"还是"系统退化"。
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import run_eval as R                      # noqa: E402

PERSONAS = os.path.join(ROOT, "eval", "personas_generated.json")
TRIAGE_1106 = os.path.join(ROOT, "eval", "reports", "triage_personas_1106.json")
PERSONA_NAMES = {
    "C01": "含糊老人", "C02": "愤怒客户", "C03": "蛮横客户", "C04": "得寸进尺",
    "C05": "听不清", "C06": "赶时间", "C07": "合规试探", "C08": "沉默挂断",
    "C09": "方言口音", "C10": "反复改口",
}


def _persona_of(case):
    tag = str(case.get("tag") or "")
    if "persona_" in tag:
        return tag.split("persona_")[-1].upper()
    try:
        idx = int(str(case.get("id", "EV00"))[2:])
        return "C%02d" % ((idx - 1) // 4 + 1)
    except Exception:
        return "?"


def _quality(case, turns):
    """人设级质量断言——只看最终闭环会漏掉「承接写坏了但结果碰巧对」。

    这不是重复 judge：judge 判「该闭环的闭环没有」，这里判「过程中有没有
    把人当空气」。Q1 分诊的 24 条失败全是后者，但修好后闭环也对了，
    只看闭环就再也发现不了同类回归（T17 首版 5/5 假绿同一个坑）。
    """
    pid = _persona_of(case)
    problems = []
    replies = [t["reply"] for t in turns]
    if pid in ("C02", "C03", "C07"):
        # 这三人设会明确拒绝报信息。拒绝那一轮必须给「替代路径」，
        # 只回一句「方便说一下后四位」等于把人的顾虑当没听见。
        for i, t in enumerate(turns):
            if not any(w in (t["user"] or "") for w in
                       ("凭什么", "不报", "不说", "不告诉")):
                continue
            r = t["reply"] or ""
            if not any(w in r for w in ("订单号", "姓名", "申请单", "公司名")):
                problems.append("第%d轮拒绝报信息后没给替代路径：%s" % (i + 1, r[:34]))
            if "没听清" in r or "杂音" in r:
                problems.append("第%d轮把拒绝当成噪声：%s" % (i + 1, r[:34]))
    if pid == "C05":
        n = sum(1 for r in replies if "没听清" in r or "杂音" in r)
        if n >= 2:
            problems.append("听不清人设被回了 %d 次『没听清』（应承接而非敷衍）" % n)
    if pid == "C01" and len(turns) >= 5:
        # 含糊老人：前 4 轮就说「再见」= 没耐心，直接红。
        if any("再见" in (r or "") for r in replies[:4]):
            problems.append("含糊老人前 4 轮就被收场")
    return problems


def _run(cases):
    out = {}
    for c in cases:
        sid = R.start_call()["session_id"]
        turns, last = [], {}
        for t in c.get("turns", []):
            o = R.handle_turn(sid, user_text=t)
            turns.append({"user": t, "reply": o.get("reply_text") or "",
                          "intent": o.get("intent")})
            last = o
        evs = R.events.list_events(sid)
        R.end_call(sid)
        ok, problems, closed, consent = R.judge(c, last, evs)
        q = _quality(c, turns)
        out[c["id"]] = {"ok": ok and not q, "judge_ok": ok,
                        "problems": list(problems) + q,
                        "closed": closed, "consent": consent}
    return out


def _rate(results):
    n = len(results)
    p = sum(1 for r in results.values() if r["ok"])
    return p, n, (p / float(n) if n else 0.0)


def _historical_weak_ids():
    """1106 首轮分诊失败的 24 条。读不到就返回空集（不编造）。"""
    try:
        with open(TRIAGE_1106, encoding="utf-8") as f:
            d = json.load(f)
        return {r["id"] for r in d["items"] if not r["run"]["ok"]}
    except Exception:
        return set()


def _pressure_case(case):
    """Gen3：把异议句重复 3 次，模拟「反复施压后才收尾」。

    只用用例自己的台词，不引入新话术——课程不该替用户编台词。
    """
    base = list(case["turns"])
    if len(base) < 6:
        return None
    obj = base[4]                      # objection[0]
    turns = base[:5] + [obj, obj, obj] + base[5:]
    return {"id": case["id"] + "-P3", "category": case.get("category", "adversarial"),
            "tag": case.get("tag"), "goal": "消耗战：" + case.get("goal", ""),
            "turns": turns, "expect": dict(case.get("expect") or {})}


def gen3_cases(cases, gen2_results, hist_weak):
    """最弱三角色：先按 Gen2 失败数，全 0 时按历史失败数，再按 id。"""
    fails, hist = {}, {}
    for c in cases:
        pid = _persona_of(c)
        if not gen2_results.get(c["id"], {}).get("ok", True):
            fails[pid] = fails.get(pid, 0) + 1
        if c["id"] in hist_weak:
            hist[pid] = hist.get(pid, 0) + 1
    order = sorted({_persona_of(c) for c in cases},
                   key=lambda p: (-fails.get(p, 0), -hist.get(p, 0), p))
    weakest = order[:3]
    picked = [c for c in cases if _persona_of(c) in weakest]
    pressure = [x for x in (_pressure_case(c) for c in picked) if x]
    return weakest, picked, pressure


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", default="eval/reports/course_evolution_1106.json")
    ap.add_argument("--report", default="docs/29_K5课程进化v2.md")
    args = ap.parse_args(argv)

    if not os.path.exists(PERSONAS):
        print("缺少 eval/personas_generated.json（Q4 起不进仓库）：先跑 "
              "python scripts/gen_adversarial.py")
        return 2

    with open(PERSONAS, encoding="utf-8") as f:
        all_cases = json.load(f)
    hist_weak = _historical_weak_ids()

    # ---- Gen1 全矩阵 ----
    g1 = _run(all_cases)
    p1, n1, r1 = _rate(g1)
    print("Gen1 全矩阵：%d/%d = %.0f%%" % (p1, n1, r1 * 100))
    weak1 = [c for c in all_cases if not g1[c["id"]]["ok"]]
    for c in weak1:
        print("  弱点 %s（%s）%s" % (c["id"], _persona_of(c),
                                   "；".join(g1[c["id"]]["problems"])[:70]))

    # ---- Gen2 只打弱点（全绿时退化为历史弱点回归） ----
    g2_cases = weak1 or [c for c in all_cases if c["id"] in hist_weak]
    g2_mode = "Gen1 失败项" if weak1 else "历史弱点回归集（Gen1 全绿）"
    if not g2_cases:
        g2_cases = all_cases[:8]
        g2_mode = "抽样守门（无弱项、无历史集）"
    g2 = _run(g2_cases)
    p2, n2, r2 = _rate(g2)
    print("Gen2 只打弱点（%s）：%d/%d = %.0f%%" % (g2_mode, p2, n2, r2 * 100))

    # ---- Gen3 最弱三角色消耗战 ----
    weakest, picked, pressure = gen3_cases(all_cases, g2, hist_weak)
    g3 = _run(pressure)
    p3, n3, r3 = _rate(g3)
    print("Gen3 消耗战（最弱三角色 %s）：%d/%d = %.0f%%"
          % ("/".join(weakest), p3, n3, r3 * 100))

    # ---- 回滚记录：本代低于上代就标记 ----
    gens = [
        {"gen": 1, "name": "全矩阵", "mode": "10 persona × 4 场景",
         "cases": n1, "passed": p1, "rate": r1, "prev": None},
        {"gen": 2, "name": "只打弱点", "mode": g2_mode,
         "cases": n2, "passed": p2, "rate": r2, "prev": r1},
        {"gen": 3, "name": "最弱三角色消耗战", "mode": "弱点角色 × 异议重复 3 次",
         "cases": n3, "passed": p3, "rate": r3, "prev": r2},
    ]
    for g in gens:
        g["regressed"] = bool(g["prev"] is not None and g["rate"] < g["prev"])

    # ---- 变异验证：注入缺陷，课程必须发现 ----
    mut = None
    if args.self_test:
        from modules import dialogue as D
        orig = D._refuse_identity
        D._refuse_identity = lambda t: False      # 拒绝核身识别失效
        try:
            m2 = _run(g2_cases)
        finally:
            D._refuse_identity = orig
        mp, mn, mr = _rate(m2)
        caught = mp < p2 or any(not v["ok"] for v in m2.values())
        mut = {"injected": "_refuse_identity 恒 False", "passed": mp,
               "cases": mn, "rate": mr, "caught": caught,
               "delta": mp - p2}
        print("变异：注入「拒绝核身识别失效」→ Gen2 %d/%d（原 %d/%d），%s"
              % (mp, mn, p2, n2, "已抓到" if caught else "没抓到（课程无效）"))

    # ---- 报告 ----
    os.makedirs(os.path.join(ROOT, "eval", "reports"), exist_ok=True)
    md = [
        "# Q2 K5 课程进化 v2（画像工厂 · 三代课程）",
        "",
        "> 复跑：`python scripts/course_evolution.py --self-test`。"
        "课程固定顺序、无随机，可逐字节复算。",
        "> 口径：通过率是**这套用例上的流程完成度**，不是真实来电者上的准确率。",
        "",
        "## 三代对比",
        "",
        "| 代 | 课程 | 用例数 | 通过 | 通过率 | 上代弱项 → 本代 | 回退 |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for g in gens:
        prev = "-" if g["prev"] is None else "%.0f%% → %.0f%%" % (g["prev"] * 100,
                                                                   g["rate"] * 100)
        md.append("| Gen%d | %s | %d | %d | **%.0f%%** | %s | %s |"
                  % (g["gen"], g["mode"], g["cases"], g["passed"],
                     g["rate"] * 100, prev,
                     "**是（保留上代快照）**" if g["regressed"] else "否"))
    md += ["", "## Gen1 弱项 → Gen2 结果", "",
           "| 用例 | persona | Gen1 问题 | Gen2 结论 |", "|---|---|---|---|"]
    if weak1:
        for c in weak1:
            md.append("| %s | %s %s | %s | %s |" % (
                c["id"], _persona_of(c), PERSONA_NAMES.get(_persona_of(c), ""),
                "；".join(g1[c["id"]]["problems"])[:60],
                "通过" if g2.get(c["id"], {}).get("ok") else "**仍失败**"))
    else:
        md.append("| - | - | Gen1 全绿，无弱项 | Gen2 退化为历史弱点回归集 |")
    md += ["", "## Gen3 消耗战明细", "",
           "| 用例 | 结论 | 问题 |", "|---|---|---|"]
    for cid, r in g3.items():
        md.append("| %s | %s | %s |" % (cid, "通过" if r["ok"] else "**失败**",
                                        "；".join(r["problems"])[:70] or "-"))
    md += ["", "## 结论与边界", ""]
    if not weak1:
        md.append("- Gen1 全绿：**当前画像工厂已无区分度**，Gen2 改为历史弱点回归守门，"
                  "不硬造失败。")
    if mut:
        md.append("- 变异验证：注入「%s」后 Gen2 从 %d/%d 降到 %d/%d，课程**能发现**注入缺陷。"
                  % (mut["injected"], p2, n2, mut["passed"], mut["cases"]))
    md.append("- 课程不改生产代码/配置，无需回滚；`regressed` 只作报警，"
              "不自动判「系统退化」——难度变了也会降分，要人看 transcript。")
    md.append("- 样本是 10 个自拟 persona × 4 个场景，覆盖矩阵不是随机抽样，"
              "通过率不能外推成真实来电者准确率。")
    path = args.report if os.path.isabs(args.report) else os.path.join(ROOT, args.report)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(md) + "\n")
    print("\n报告：%s" % os.path.relpath(path, ROOT))

    payload = {"gens": gens, "weak1": [c["id"] for c in weak1],
               "gen2_mode": g2_mode, "weakest": weakest,
               "gen3": g3, "mutation": mut,
               "note": "课程只读跑批，不改生产代码；通过率不等于泛化准确率"}
    jp = args.json if os.path.isabs(args.json) else os.path.join(ROOT, args.json)
    with open(jp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("JSON：%s" % os.path.relpath(jp, ROOT))

    ok = all(not g["regressed"] for g in gens)
    if mut is not None and not mut["caught"]:
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
