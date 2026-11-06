#!/usr/bin/env python3
"""一键评测套件：基线评测 + 对抗多轮 + 回写合规，产出一份统一报告。

为什么需要它（而不是继续各跑各的）：
    run_eval.py 出通过率、task2_metrics.py 出四项指标、adversarial_run.py 出对抗结果，
    三个脚本三套输出。评委问"这次改动到底有没有变好"，得有人把它们
    放进同一口径、同一入口、同一个退出码里——这就是本文件的职责。

它复用什么（不重复实现）：
    - run_eval.run_case() / run_eval.judge()        判定语义冻结（接口 6）
    - run_eval.closure_stats()                      闭环双指标的唯一定义
    - adversarial_run.load_cases() / run_round() / aggregate()  对抗集校验与多轮统计

它不算哪些数：
    T17 的意图覆盖率与槽位完整率。那两项在 task2_metrics.py 里会自己再跑一遍用例，
    重复执行成本高，且与本文件不共享会话，拿过来就变成两次跑批。
    要用请直接跑 `python3 scripts/task2_metrics.py`，两份报告并列即可。

用法：
    python3 scripts/eval_suite.py                       # 基线 + 对抗 3 轮
    python3 scripts/eval_suite.py --rounds 5 --fail-under 0.6
    python3 scripts/eval_suite.py --no-report --quiet   # 只看汇总与退出码
"""

import argparse
import collections
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adversarial_run as ADV          # noqa: E402  对抗集校验/多轮执行/聚合
import config_loader                   # noqa: E402
import run_eval as R                   # noqa: E402  基线跑批、判定、闭环双指标

REPORT_DIR = os.path.join(R._ROOT, "eval", "reports")
HISTORY_DIR = os.path.join(R._ROOT, "eval", "history")
VALID_CONSENT = {True, False, "unknown"}
FAILURE_STAGES = {"GREETING", "IDENTITY", "NEED", "OFFER", "CONFIRM", "WRITEBACK", "DONE"}


class SuiteError(ValueError):
    """套件入参或用例不合法。"""


def _git_rev():
    """报告必须能对应到某一份代码，否则三个月后对不上。"""
    try:
        import subprocess
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=R._ROOT, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def collect_writeback_metrics(rows):
    """回写合规率：主口径按 create_ticket 次数，辅口径按会话数。

    判据来源与 task2_metrics.py 一致：必填字段来自 config，不在本文件写死——
    4 号把 customer_id 加进 writeback_schema 后，这里自动跟着变。
    """
    try:
        required = sorted(set(config_loader.writeback_template().keys()) | {"writeback_version"})
    except Exception:
        required = []

    calls = ok_calls = 0
    sessions_ok = 0
    problems = []
    for row in rows:
        evs = row.get("evs") or []
        calls_here = 0
        session_bad = False
        for ev in evs:
            if ev["type"] != "tool_call":
                continue
            payload = ev.get("payload") or {}
            if payload.get("name") != "create_ticket":
                continue
            content = (payload.get("args") or {}).get("content") or {}
            calls += 1
            calls_here += 1

            missing = [k for k in required if k not in content]
            bad = []
            if content.get("consent") not in VALID_CONSENT:
                bad.append("consent=%r 非法" % content.get("consent"))
            if not isinstance(content.get("identity_confirmed"), bool):
                bad.append("identity_confirmed 非布尔")
            if not isinstance(content.get("writeback_version"), int):
                bad.append("writeback_version 非整数")

            if missing or bad:
                session_bad = True
                problems.append({"case": row["id"], "missing": missing, "bad": bad})
            else:
                ok_calls += 1
        if calls_here and not session_bad:
            sessions_ok += 1

    # 会话分母 = 真正发生过 create_ticket 的会话数。
    # 没回写的会话（拒绝后挂断、核验失败）不进分母，否则会把"没发生"记成"不合格"。
    session_total = len({r["id"] for r in rows if any(
        e["type"] == "tool_call" and (e["payload"] or {}).get("name") == "create_ticket"
        for e in (r.get("evs") or []))})

    return {
        "calls": calls,
        "ok_calls": ok_calls,
        "sessions": session_total,
        "sessions_ok": sessions_ok,
        "problems": problems,
        "rate_calls": (ok_calls / float(calls)) if calls else None,
        "rate_sessions": (sessions_ok / float(session_total)) if session_total else None,
    }


def collect_baseline(cases):
    """跑基线并汇总。失败不中断：单条异常记为失败并写入 problems。"""
    rows = []
    for case in cases:
        try:
            last, evs = R.run_case(case)
            ok, problems, closed, consent = R.judge(case, last, evs)
            rows.append({"id": case["id"], "category": case["category"],
                         "tag": case.get("tag", "-"), "ok": ok, "closed": closed,
                         "holdout": bool(case.get("holdout")),
                         "consent": consent, "problems": problems,
                         "turns": len(case.get("turns") or []),
                         "evs": evs, "error": ""})
        except Exception as exc:                       # noqa: BLE001
            rows.append({"id": case["id"], "category": case["category"],
                         "tag": case.get("tag", "-"), "ok": False, "closed": False,
                         "consent": None, "problems": ["执行异常：%s" % exc],
                         "turns": 0, "evs": [], "error": "%s: %s" % (type(exc).__name__, exc)})

    stats = R.closure_stats(cases, rows)
    by_cat = {}
    for r in rows:
        t, p = by_cat.get(r["category"], (0, 0))
        by_cat[r["category"]] = (t + 1, p + (1 if r["ok"] else 0))
    return rows, {
        "total": len(rows),
        "passed": sum(1 for r in rows if r["ok"]),
        "closed": sum(1 for r in rows if r["closed"]),
        "by_category": by_cat,
        "closure": stats,
        "avg_turns": round(sum(r["turns"] for r in rows) / float(len(rows)), 2) if rows else 0,
        "writeback": collect_writeback_metrics(rows),
        "errors": sum(1 for r in rows if r["error"]),
    }


def collect(rounds=3, seed_path=None, adv_path=None):
    """收集全部数据，返回结构化 payload（不写文件，便于 daily 复用）。"""
    seed_path = seed_path or R.CASES_PATH
    adv_path = adv_path or ADV.CASES_PATH
    started = datetime.datetime.now()

    try:
        seed_cases = R.load_cases() if seed_path == R.CASES_PATH else ADV.load_cases(seed_path)
        adv_cases = ADV.load_cases(adv_path)
    except (ValueError, OSError) as exc:
        raise SuiteError(str(exc))

    rows, base = collect_baseline(seed_cases)

    per_round = [ADV.run_round(adv_cases) for _ in range(rounds)]
    adv_stats = ADV.aggregate(per_round, [c["id"] for c in adv_cases])

    finished = datetime.datetime.now()
    return {
        "meta": {
            "generated_at": finished.strftime("%Y-%m-%d %H:%M:%S"),
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "commit": _git_rev(),
            "seed_cases": os.path.relpath(seed_path, R._ROOT),
            "adversarial_cases": os.path.relpath(adv_path, R._ROOT),
            "rounds": rounds,
            "duration_sec": round((finished - started).total_seconds(), 1),
        },
        "baseline": base,
        "baseline_rows": [
            {k: v for k, v in r.items() if k != "evs"} for r in rows
        ],
        "adversarial": {
            "total": len(adv_cases),
            "rounds": adv_stats["rounds"],
            "rates": adv_stats["rates"],
            "avg_rate": adv_stats["avg_rate"],
            "stable": adv_stats["stable"],
            "unstable": adv_stats["unstable"],
            "failed_ids": adv_stats["failed_ids"],
            "errors": adv_stats["errors"],
            "per_case": adv_stats["per_case"],
            "clusters": [{"stage": s, "type": k, "count": v}
                         for (s, k), v in adv_stats["clusters"].most_common()],
            "raw": per_round,
        },
    }


def render(p):
    """把 payload 渲染成答辩可直接引用的报告。"""
    m = p["meta"]
    b = p["baseline"]
    c = b["closure"]
    w = b["writeback"]
    a = p["adversarial"]
    out = ["# 评测套件报告", ""]
    out.append("- 时间：%s（耗时 %ss）" % (m["generated_at"], m["duration_sec"]))
    out.append("- 代码：commit `%s`" % m["commit"])
    out.append("- 基线：`%s`；对抗：`%s`" % (m["seed_cases"], m["adversarial_cases"]))
    out.append("- 判定：复用 `run_eval.judge()`（接口 6 冻结）；对抗集与基线分开统计")
    out.append("")
    out += ["## 一、基线（%d 条）" % b["total"], "",
            "| 指标 | 数值 |", "|---|---|"]
    out.append("| 通过率 | %d/%d = %.0f%% |" % (b["passed"], b["total"],
                                         b["passed"] * 100.0 / b["total"]))
    out.append("| 应闭环 | %d/%d = %.0f%% |" % (c["should_ok"], c["should"], c["rate"] * 100))
    out.append("| 误闭环 | %d/%d（应为 0） |" % (c["wrongly"], c["should_not"]))
    out.append("| 未声明 closed | %d 条（不计入） |" % c["undeclared"])
    out.append("| 回写合规（次数） | %s |" % (
        "%d/%d = %.0f%%" % (w["ok_calls"], w["calls"], w["rate_calls"] * 100)
        if w["rate_calls"] is not None else "无回写样本"))
    out.append("| 回写合规（会话） | %s |" % (
        "%d/%d = %.0f%%" % (w["sessions_ok"], w["sessions"], w["rate_sessions"] * 100)
        if w["rate_sessions"] is not None else "无回写样本"))
    out.append("| 平均轮数 | %.2f |" % b["avg_turns"])
    out.append("| 执行异常 | %d 次 |" % b["errors"])
    out.append("")
    out.append("分类通过：" + "  ".join("%s %d/%d" % (k, v[1], v[0])
                                    for k, v in b["by_category"].items()))
    out.append("")
    out += ["## 二、对抗（%d 条 × %d 轮）" % (a["total"], a["rounds"]), "",
            "| 指标 | 数值 |", "|---|---|"]
    out.append("| 每轮通过 | %s |" % " ｜ ".join(
        "%d/%d" % (r, a["total"]) for r in a["rates"]))
    out.append("| 平均通过率 | %.1f%% |" % (a["avg_rate"] * 100))
    out.append("| 结论稳定率 | %d/%d = %.0f%% |" % (a["stable"], a["total"],
                                          a["stable"] * 100.0 / a["total"]))
    out.append("| 未通过用例 | %d 条（%s） |" % (len(a["failed_ids"]),
                                          "、".join(a["failed_ids"]) or "无"))
    out.append("")
    out += ["## 三、失败聚类（对抗，跨轮合并）", ""]
    if a["clusters"]:
        out += ["| 末环节 | 问题类型 | 次数 |", "|---|---|---|"]
        for cl in a["clusters"]:
            out.append("| %s | %s | %d |" % (cl["stage"], cl["type"], cl["count"]))
    else:
        out.append("无失败。")
    out.append("")
    out += ["## 四、结论与局限", "",
            "1. 通过率已接近天花板，真正有信息量的是**闭环率、留出集分数、对抗稳定率**三个数；",
            "2. 对抗未通过的用例是真实能力缺口，不是用例写错（用例缺陷已在编写时剔除）；",
            "3. 本报告为**文字驱动**：噪声类只验证「听不清时的文本」，不代表真实声学噪声；",
            "4. T17 的意图覆盖率与槽位完整率不在本报告，见 `python3 scripts/task2_metrics.py`。"]
    return "\n".join(out) + "\n"



# ---------------------------------------------------------------------------
# L8：失败聚类 + 版本对比
#
# 聚类只按 judge 给出的 problems 归类，不引入第二套判定——
# 多一套语义就多一个数，评审一问"以哪个为准"就答不上来。
# ---------------------------------------------------------------------------

def cluster_failures(rows):
    """把未通过用例聚成 5 类，返回 [{reason, count, cases[]}]。

    分类依据全部来自 judge 的 problem 前缀，机械可判：
        说错了   说了不该说的（must_not 命中）
        越界     越界进入阶段（must_not_stage 命中）
        没听懂   意向不符（consent 判错）
        漏闭环   闭环不符（该闭没闭 / 不该闭却闭了）
        终态错   终态不符
    """
    buckets = {}
    for r in rows:
        if r["ok"]:
            continue
        reasons = set()
        for p in r["problems"]:
            head = p.split("：")[0]
            reasons.add({"说了不该说的": "说错了", "越界进入阶段": "越界",
                         "意向不符": "没听懂", "闭环不符": "漏闭环",
                         "终态不符": "终态错"}.get(head, "其他"))
        for reason in reasons or {"其他"}:
            buckets.setdefault(reason, []).append(r["id"])
    order = ["说错了", "越界", "没听懂", "漏闭环", "终态错", "其他"]
    return [{"reason": k, "count": len(buckets[k]), "cases": buckets[k]}
            for k in order if k in buckets]


def build_version(payload, name):
    """把一次跑批压成可比较的快照。字符串只用于展示，比较一律用数字。"""
    b = payload["baseline"]
    return {
        "name": name,
        "saved_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "commit": payload["meta"]["commit"],
        "total": b["total"], "passed": b["passed"], "closed": b["closed"],
        "should": b["closure"]["should"], "should_ok": b["closure"]["should_ok"],
        "wrongly": b["closure"]["wrongly"],
        "holdout_total": sum(1 for r in payload["baseline_rows"] if r.get("holdout")),
        "holdout_passed": sum(1 for r in payload["baseline_rows"]
                              if r.get("holdout") and r["ok"]),
        "cases": [{"id": r["id"], "ok": r["ok"], "closed": r["closed"]}
                  for r in payload["baseline_rows"]],
    }


def compare_versions(base, cur):
    """对比两次快照。过拟合判据只有一条：留出集通过率下降。"""
    def rate(d):
        return d["passed"] / float(d["total"]) if d["total"] else 0.0

    def hrate(d):
        return (d["holdout_passed"] / float(d["holdout_total"])
                if d["holdout_total"] else None)

    b_cases = {c["id"]: c for c in base["cases"]}
    c_cases = {c["id"]: c for c in cur["cases"]}
    better = [i for i in c_cases if c_cases[i]["ok"] and not b_cases.get(i, {}).get("ok")]
    worse = [i for i in c_cases if not c_cases[i]["ok"] and b_cases.get(i, {}).get("ok")]
    regression = (hrate(base) is not None and hrate(cur) is not None
                  and hrate(cur) < hrate(base))

    table = "\n".join([
        "| 版本 | 通过率 | 应闭环 | 误闭环 | 留出集 |",
        "|---|---|---|---|---|",
        "| %s | %d/%d（%.0f%%） | %d/%d | %d | %d/%d |"
        % (base["name"], base["passed"], base["total"], rate(base) * 100,
           base["should_ok"], base["should"], base["wrongly"],
           base["holdout_passed"], base["holdout_total"]),
        "| %s | %d/%d（%.0f%%） | %d/%d | %d | %d/%d |"
        % (cur["name"], cur["passed"], cur["total"], rate(cur) * 100,
           cur["should_ok"], cur["should"], cur["wrongly"],
           cur["holdout_passed"], cur["holdout_total"]),
    ])
    return {"table": table, "regression": regression,
            "better": better, "worse": worse,
            "rate_delta": rate(cur) - rate(base),
            "holdout_delta": (None if hrate(base) is None or hrate(cur) is None
                              else hrate(cur) - hrate(base))}

def _unique(path):
    n = 2
    while os.path.exists(path):
        root, ext = os.path.splitext(path)
        path = "%s-%d%s" % (root, n, ext)
        n += 1
    return path


def write_report(text, payload, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    stem = "suite_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M")
    md_path = _unique(os.path.join(out_dir, stem + ".md"))
    json_path = md_path[:-3] + ".json"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return md_path, json_path


def build_parser():
    p = argparse.ArgumentParser(description="一键评测套件：基线 + 对抗 + 回写合规")
    p.add_argument("--rounds", type=int, default=3, help="对抗轮次，默认 3")
    p.add_argument("--cases", default=R.CASES_PATH, help="基线用例路径")
    p.add_argument("--adv", default=ADV.CASES_PATH, help="对抗用例路径")
    p.add_argument("--out", default=REPORT_DIR, help="报告目录")
    p.add_argument("--fail-under", type=float, default=None,
                   help="通过率下限（0~1），同时约束基线与对抗，低于返回 1")
    p.add_argument("--no-report", action="store_true")
    p.add_argument("--cluster", action="store_true", help="打印失败聚类")
    p.add_argument("--save-version", default=None, help="把本次结果存成版本快照")
    p.add_argument("--compare", default=None, help="与已存版本对比")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.rounds < 1:
        print("--rounds 至少为 1", file=sys.stderr)
        return 2
    if args.fail_under is not None and not (0.0 <= args.fail_under <= 1.0):
        print("--fail-under 必须在 0~1 之间", file=sys.stderr)
        return 2

    try:
        payload = collect(args.rounds, args.cases, args.adv)
    except SuiteError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not args.no_report:
        md_path, json_path = write_report(render(payload), payload, args.out)
        print("报告：%s" % os.path.relpath(md_path, R._ROOT))
        print("结构化：%s" % os.path.relpath(json_path, R._ROOT))

    b = payload["baseline"]
    a = payload["adversarial"]
    base_rate = b["passed"] / float(b["total"])
    print("基线 %d/%d（%.0f%%）｜应闭环 %d/%d｜回写 %d/%d｜对抗 %.0f%%｜稳定 %d/%d｜异常 %d"
          % (b["passed"], b["total"], base_rate * 100,
             b["closure"]["should_ok"], b["closure"]["should"],
             b["writeback"]["ok_calls"], b["writeback"]["calls"],
             a["avg_rate"] * 100, a["stable"], a["total"],
             b["errors"] + a["errors"]))

    if args.cluster:
        clusters = cluster_failures(payload["baseline_rows"])
        print("\n===== 失败聚类 =====")
        if not clusters:
            print("无失败用例。")
        for c in clusters:
            print("  %-6s %2d 条：%s" % (c["reason"], c["count"], "、".join(c["cases"])))

    if args.save_version:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        path = os.path.join(HISTORY_DIR, args.save_version + ".json")
        if os.path.exists(path):
            print("拒绝覆盖：%s 已存在（旧版本是回滚点，不能被覆盖）"
                  % os.path.relpath(path, R._ROOT))
            return 2
        with open(path, "w", encoding="utf-8") as f:
            json.dump(build_version(payload, args.save_version), f,
                      ensure_ascii=False, indent=2)
        print("已存版本：%s" % os.path.relpath(path, R._ROOT))

    if args.compare:
        base_path = os.path.join(HISTORY_DIR, args.compare + ".json")
        if not os.path.exists(base_path):
            print("找不到基线版本：%s" % os.path.relpath(base_path, R._ROOT))
            return 2
        with open(base_path, "r", encoding="utf-8") as f:
            base = json.load(f)
        cur = build_version(payload, "本次（%s）" % payload["meta"]["commit"])
        res = compare_versions(base, cur)
        print("\n===== 版本对比 =====")
        print(res["table"])
        print("通过率 Δ %+.1f 个百分点｜留出集 Δ %s"
              % (res["rate_delta"] * 100,
                 "-" if res["holdout_delta"] is None
                 else "%+.1f 个百分点" % (res["holdout_delta"] * 100)))
        if res["better"]:
            print("变好：%s" % "、".join(res["better"]))
        if res["worse"]:
            print("变差：%s" % "、".join(res["worse"]))
        if res["regression"]:
            print("!!! 留出集下降，疑似过拟合，建议回滚到 %s !!!" % args.compare)

    if b["errors"] + a["errors"]:
        return 3
    if args.fail_under is not None and (base_rate < args.fail_under
                                        or a["avg_rate"] < args.fail_under):
        print("低于阈值 %.0f%%，判定不达标" % (args.fail_under * 100), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
