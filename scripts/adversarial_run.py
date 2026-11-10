#!/usr/bin/env python3
"""对抗测试跑批器（v2）：把对抗集反复跑 N 轮，回答三个问题。

    1. 这批对抗扛不扛得住 —— 每轮通过率；
    2. 结论稳不稳 —— 同一输入多轮是否给同一判定（不确定性比低分更严重）；
    3. 弱在哪 —— 失败聚到「末环节 × 问题类型」，供 2A/2B 整改。

设计约束（来自 CONTRIBUTING.md 与两人分工方案，不是随手定的）：
    - 判定只复用 scripts/run_eval.py 的 run_case() / judge()：接口 6 已冻结判定语义，
      本文件不复制第二份判定，否则同一批用例会出现两个分数；
    - 只写 eval/reports/；不碰 data/events.db（run_eval 启动时已断言临时库）；
    - 零第三方依赖；用例先校验再执行，坏数据不许跑出半截结果；
    - 退出码要能被 CI 感知：0 达标 / 1 低于阈值 / 2 输入错误 / 3 执行期异常。

用法：
    python3 scripts/adversarial_run.py                       # 默认 3 轮
    python3 scripts/adversarial_run.py --rounds 5 --fail-under 0.6
    python3 scripts/adversarial_run.py --cases eval/adversarial_cases.json --json
"""

import argparse
import collections
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _prescan_scene(argv):
    """在自己的参数表里先捞出 `--scene`，因为场景必须早于 `import run_eval` 定下来。

    这不是多此一举：`run_eval._scan_argv` 就是靠读 sys.argv 决定场景的，
    而 app 里有几处「启动时读一次」的缓存——import 之后再切，赛题用例会被
    售后话术与售后假客户跑一遍。11-09 实测：340 条 C4（POP 关单场景）用例
    在默认售后场景下跑出 31/374 = 8.3%，切到 `--scene pop_reject` 后
    batch01 是 40/40 = 100%。同一批数据、同一份代码，差别只在场景有没有切对。
    """
    for i, a in enumerate(argv):
        if a == "--scene" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--scene="):
            return a.split("=", 1)[1]
    return ""


#: 这一趟跑的是哪个场景（空串 = config/scene.json 的默认场景）。
_SCENE = _prescan_scene(sys.argv)

#: 哪些对抗集属于哪个场景。C4 批次是 4 号按官方 68 个关单场景生成的，
#: 跑它们必须用 pop_reject，否则开场就是「京东客服中心的小采，请问是张先生本人吗」，
#: 客户报的尾号跟这套售后假客户一个也对不上，340 条全部卡在 IDENTITY。
#: 新增场景批次时在这里登记；没登记的一律按默认场景跑。
SCENE_OF_FILE = {
    "adversarial_c4_": "pop_reject",
}


def scene_of_file(path):
    name = os.path.basename(path)
    for prefix, sc in SCENE_OF_FILE.items():
        if name.startswith(prefix):
            return sc
    return ""


import run_eval as R                       # noqa: E402  复用跑批与临时库隔离

CASES_DIR = os.path.join(R._ROOT, "eval")
CASES_PATH = os.path.join(CASES_DIR, "adversarial_cases.json")   # 兼容旧用法
REPORT_DIR = os.path.join(R._ROOT, "eval", "reports")

# 用例 schema：与 eval/seed_cases.json 保持同源，只多一个 adversarial 说明字段。
REQUIRED_FIELDS = ("id", "category", "goal", "turns", "expect")
OPTIONAL_FIELDS = ("tag", "holdout", "adversarial", "noise", "source")
EXPECT_KEYS = {"closed", "consent", "final_stage", "must_not", "must_not_stage",
               "must_say", "must_not_intent", "must_say_intent"}
CATEGORIES = {"normal", "missing_slot", "refuse", "emotion",
              "induce", "correction", "knowledge", "adversarial",
              # Q4：从画像工厂分诊转正的回归用例单独一类。
              # 不混进 adversarial，是因为要能一眼看出「这条是守历史缺陷的」。
              "regression"}
STAGES = {"GREETING", "IDENTITY", "NEED", "OFFER", "CONFIRM", "WRITEBACK", "DONE"}
MAX_TURNS = 20


class CaseError(ValueError):
    """用例集不合法。一次性列出全部问题，不要改一条跑一次。"""


def _git_rev():
    """记录被测代码的 commit。没有它，报告里的数字三个月后无法复现。"""
    try:
        import subprocess
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=R._ROOT, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def validate_cases(raw):
    """校验并归一化用例集。返回 list；有问题抛 CaseError（附条目序号与字段名）。

    为什么校验放在执行前：一次跑批 30 次执行、几十秒，等跑到第 8 条才发现
    expect 拼错，前面 7 条的样本也白跑了；而且半截结果最容易被当成"跑完了"。
    """
    if not isinstance(raw, list) or not raw:
        raise CaseError("用例集必须是非空数组，当前是 %s" % type(raw).__name__)

    errors, seen = [], set()
    for idx, case in enumerate(raw, 1):
        where = "第 %d 条" % idx
        if not isinstance(case, dict):
            errors.append("%s：不是对象" % where)
            continue

        missing = [f for f in REQUIRED_FIELDS if not case.get(f)]
        if missing:
            errors.append("%s：缺字段 %s" % (where, "、".join(missing)))
            continue
        where = "第 %d 条（%s）" % (idx, case["id"])

        cid = case["id"]
        if not isinstance(cid, str):
            errors.append("%s：id 必须是字符串" % where)
        elif cid in seen:
            errors.append("%s：id 重复" % where)
        else:
            seen.add(cid)

        if case["category"] not in CATEGORIES:
            errors.append("%s：category 不在允许集合内（%s）" % (where, case["category"]))

        unknown = set(case) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
        if unknown:
            errors.append("%s：存在未定义字段 %s（拼错字段名不会被判定读到）"
                          % (where, "、".join(sorted(unknown))))

        turns = case["turns"]
        if not isinstance(turns, list) or not turns:
            errors.append("%s：turns 必须是非空数组" % where)
        elif len(turns) > MAX_TURNS:
            errors.append("%s：turns 超过 %d 轮" % (where, MAX_TURNS))
        elif any(not isinstance(t, str) for t in turns):
            errors.append("%s：turns 里有非字符串项" % where)

        expect = case["expect"]
        if not isinstance(expect, dict) or not expect:
            errors.append("%s：expect 必须是非空对象" % where)
            continue
        bad_keys = set(expect) - EXPECT_KEYS
        if bad_keys:
            errors.append("%s：expect 含未定义键 %s" % (where, "、".join(sorted(bad_keys))))

        consent = expect.get("consent")
        if "consent" in expect and consent not in (True, False, "unknown"):
            # judge() 只认 true/false；"unknown" 目前是死值，写进来等于该条永远通过。
            errors.append("%s：consent 只能是 true/false/\"unknown\"，当前 %r" % (where, consent))
        stage = expect.get("final_stage")
        if "final_stage" in expect and stage not in STAGES:
            errors.append("%s：final_stage 非法（%s）" % (where, stage))
        for key in ("must_not", "must_not_stage", "must_say",
                    "must_not_intent", "must_say_intent"):
            if key in expect:
                val = expect[key]
                if not isinstance(val, list) or any(not isinstance(x, str) or not x for x in val):
                    errors.append("%s：%s 必须是非空字符串数组" % (where, key))
        if "must_not_stage" in expect:
            bad = [s for s in expect["must_not_stage"] if s not in STAGES]
            if bad:
                errors.append("%s：must_not_stage 含非法阶段 %s" % (where, "、".join(bad)))

    if errors:
        raise CaseError("用例集校验失败（共 %d 处）：\n  - %s" % (len(errors), "\n  - ".join(errors)))
    return raw


def load_cases(path):
    """读文件 + 校验。JSON 语法错误也要给出可定位的信息。"""
    if not os.path.isfile(path):
        raise CaseError("找不到用例文件：%s" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as exc:
        raise CaseError("用例文件不是合法 JSON：第 %d 行第 %d 列，%s"
                        % (exc.lineno, exc.colno, exc.msg))
    return validate_cases(raw)


def run_round(cases):
    """跑一轮。单条用例异常不终止整批，记为 error 并继续。

    为什么这么设计：对抗集本来就会喂进边界输入，某条把状态机搞崩是**有效结论**，
    应该被记录并计入统计，而不是让整批跑批挂掉、什么数都拿不到。
    """
    rows = []
    for case in cases:
        try:
            last, evs = R.run_case(case)
            ok, problems, closed, consent = R.judge(case, last, evs)
            replies = [e for e in evs if e["type"] == "agent_reply"]
            stage = (replies[-1]["payload"].get("stage") if replies else "") or "-"
            rows.append({"id": case["id"], "ok": ok, "closed": closed,
                         "consent": consent, "problems": problems,
                         "stage": stage, "error": ""})
        except Exception as exc:                       # noqa: BLE001 —— 单条兜底必须宽
            rows.append({"id": case["id"], "ok": False, "closed": False,
                         "consent": None, "problems": [], "stage": "ERROR",
                         "error": "%s: %s" % (type(exc).__name__, exc)})
    return rows


def aggregate(per_round, case_ids):
    """把多轮结果压成统计量。纯函数，便于单测。"""
    rounds = len(per_round)
    per_case = {}
    for cid in case_ids:
        verdicts = []
        for rows in per_round:
            hit = next((r for r in rows if r["id"] == cid), None)
            verdicts.append(bool(hit and hit["ok"]))
        per_case[cid] = {"passes": sum(verdicts), "stable": len(set(verdicts)) == 1}

    failed = [r for rows in per_round for r in rows if not r["ok"]]
    clusters = collections.Counter()
    for row in failed:
        kind = row["problems"][0].split("：")[0] if row["problems"] else (
            "执行异常" if row["error"] else "未分类")
        clusters[(row["stage"], kind)] += 1

    rates = [sum(1 for r in rows if r["ok"]) for rows in per_round]
    errors = sum(1 for rows in per_round for r in rows if r["error"])
    unstable = [cid for cid, v in per_case.items() if not v["stable"]]
    return {
        "rounds": rounds,
        "rates": rates,
        "total_runs": len(case_ids) * rounds,
        "avg_rate": (sum(rates) / float(len(rates)) / len(case_ids)) if rates and case_ids else 0.0,
        "per_case": per_case,
        "stable": len(case_ids) - len(unstable),
        "unstable": unstable,
        "failed_ids": sorted({r["id"] for r in failed}),
        "clusters": clusters,
        "errors": errors,
    }


def render(case_map, per_round, stats, meta):
    """生成 markdown。数字、口径、样本量、复现命令缺一不可。"""
    out = ["# 对抗测试报告（自动生成）", ""]
    out.append("- 生成时间：%s" % meta["generated_at"])
    out.append("- 被测代码：commit `%s`" % meta["commit"])
    out.append("- 对抗集：`%s`，%d 条" % (meta["cases_path"], len(case_map)))
    out.append("- 轮次：%d 轮，共 %d 次执行" % (stats["rounds"], stats["total_runs"]))
    out.append("- 判定：复用 `scripts/run_eval.py::judge()`（语义冻结，未另写）")
    out.append("- 事件库：临时库（`run_eval` 启动时断言，不写 `data/events.db`）")
    out.append("")

    out += ["## 一、总体", "", "| 指标 | 数值 |", "|---|---|"]
    out.append("| 每轮通过 | %s |" % " ｜ ".join(
        "%d/%d" % (r, len(case_map)) for r in stats["rates"]))
    out.append("| 平均通过率 | %.1f%%（%d 次执行） |"
               % (stats["avg_rate"] * 100, stats["total_runs"]))
    out.append("| **结论稳定率** | **%d/%d = %.0f%%** |"
               % (stats["stable"], len(case_map),
                  stats["stable"] * 100.0 / len(case_map)))
    out.append("| 未通过用例（去重） | %d |" % len(stats["failed_ids"]))
    out.append("| 执行异常 | %d 次 |" % stats["errors"])
    out.append("")

    out += ["## 二、逐条结果", "",
            "| 用例 | 方向 | " + " | ".join("第%d轮" % (i + 1) for i in range(stats["rounds"]))
            + " | 通过率 | 结论稳定 |",
            "|---|---|" + "---|" * stats["rounds"] + "---|---|"]
    for cid, info in stats["per_case"].items():
        marks = []
        for rows in per_round:
            hit = next((r for r in rows if r["id"] == cid), None)
            marks.append("✅" if hit and hit["ok"] else ("⚠️" if hit and hit["error"] else "❌"))
        out.append("| %s | %s | %s | %d/%d | %s |"
                   % (cid, case_map[cid].get("tag", "-"), " | ".join(marks),
                      info["passes"], stats["rounds"], "是" if info["stable"] else "**否**"))
    out.append("")

    out += ["## 三、失败聚类（跨轮合并，共 %d 次失败）" % sum(stats["clusters"].values()), ""]
    if stats["clusters"]:
        out += ["| 末环节 | 问题类型 | 次数 |", "|---|---|---|"]
        for (stage, kind), cnt in stats["clusters"].most_common():
            out.append("| %s | %s | %d |" % (stage, kind, cnt))
    else:
        out.append("无失败。")
    out.append("")

    out += ["## 四、结论", ""]
    if stats["unstable"]:
        out.append("1. **结论不稳定**：%s。先查随机性来源（缓存、时间依赖、外部调用），"
                   "再谈通过率——同一输入两个答案，说明判定链路上有非确定因素。"
                   % "、".join(stats["unstable"]))
    else:
        out.append("1. 结论稳定：%d 条用例在 %d 轮里判定一致，规则版对同一输入是确定性的。"
                   % (len(case_map), stats["rounds"]))
    if stats["errors"]:
        out.append("2. **有 %d 次执行异常**，已按失败计入，详见每条用例的 ⚠️ 标记。" % stats["errors"])
    else:
        out.append("2. 无执行异常。")
    weakest = sorted(stats["per_case"].items(), key=lambda kv: kv[1]["passes"])[:3]
    out.append("3. 最薄弱三条：%s。" % "；".join(
        "%s（%d/%d 通过）" % (cid, info["passes"], stats["rounds"]) for cid, info in weakest))
    out.append("4. 局限：本测试是**文字驱动**的，噪声类用例只验证了「听不清时的文本」，"
               "不是真实声学噪声；真实噪声需赛场三类音源 + 真麦克风复测。")
    out.append("")
    out += ["## 五、复现", "", "```bash",
            "python3 scripts/adversarial_run.py --rounds %d" % stats["rounds"], "```"]
    return "\n".join(out) + "\n"


def _unique_path(stem, suffix, out_dir):
    """同名文件不覆盖：adversarial_x_r3.md → -2.md → -3.md。

    为什么不覆盖：同一版本跑两次，第一次可能是"改前"、第二次是"改后"，
    覆盖掉就丢了对比基准。序号后缀最省事且不会歧义。
    """
    path = os.path.join(out_dir, "%s%s" % (stem, suffix))
    n = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, "%s-%d%s" % (stem, n, suffix))
        n += 1
    return path


def write_report(text, payload, out_dir, stem):
    os.makedirs(out_dir, exist_ok=True)
    md_path = _unique_path(stem, ".md", out_dir)
    json_path = md_path[:-3] + ".json"        # 与 md 同名前缀，便于成对查找
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return md_path, json_path


def build_parser():
    p = argparse.ArgumentParser(
        description="对抗测试跑批：多轮执行、稳定性统计、失败聚类。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rounds", type=int, default=3, help="轮次，默认 3")
    p.add_argument("--cases", default=None,
                   help="对抗集路径；不给则合并 eval/adversarial*.json 全部")
    p.add_argument("--out", default=REPORT_DIR, help="报告目录，默认 eval/reports/")
    p.add_argument("--fail-under", type=float, default=None,
                   help="平均通过率下限（0~1）；低于则退出码 1，用于 CI 卡口")
    p.add_argument("--no-report", action="store_true", help="只打印，不落盘")
    p.add_argument("--quiet", action="store_true", help="只打印汇总行")
    # 场景必须在 import run_eval 之前定下来，所以它只是在这里"被认识"，
    # 真正的生效靠模块顶部的 _prescan_scene 读 sys.argv（见那里的注释）。
    p.add_argument("--scene", default=None,
                   help="场景名（如 pop_reject）。不传 = config/scene.json 的默认场景")
    p.add_argument("--all-scenes", action="store_true",
                   help="按场景分批各跑一遍并汇总（CI 用这个，避免跨场景互相污染）")
    return p


def run_all_scenes(args):
    """按场景分批各跑一遍，再把结果汇总成一行。

    为什么非得起子进程：场景在 `import app` 时就定死了，同一个进程里切不回来。
    汇总口径用 **总通过数 / 总用例数**，不是各批通过率的平均——
    340 条和 34 条不能等权，等权会让小样本主导结论（这正是
    `docs/06` 第 7 节点过的"分母变了分数就没法比"）。
    """
    import glob
    import re
    import subprocess

    files = sorted(glob.glob(os.path.join(CASES_DIR, "adversarial*.json")))
    scenes = []
    for path in files:
        sc = scene_of_file(path)
        if sc not in scenes:
            scenes.append(sc)

    total = 0
    passed = 0
    broken = []
    for sc in scenes:
        # 故意不加 --quiet：quiet 会把「第 N 轮：通过 x/y」也压掉，
        # 只剩一个「平均通过率 91.2%」，那个百分比四舍五入过，
        # 反推通过数会把小样本算错。宁可多抓几行再自己过滤。
        cmd = [sys.executable, os.path.abspath(__file__),
               "--rounds", str(args.rounds), "--no-report"]
        if sc:
            cmd += ["--scene", sc]
        proc = subprocess.run(cmd, cwd=R._ROOT, capture_output=True,
                              text=True, timeout=3600)
        out = (proc.stdout or "") + (proc.stderr or "")
        print("=" * 74)
        print("场景：%s" % (sc or "(默认)"))
        print("=" * 74)
        keep = ("轮：通过", "轮: 通过", "平均通过率", "对抗测试：", "跳过", "场景：")
        shown = [ln for ln in out.splitlines() if any(k in ln for k in keep)]
        print("\n".join(shown) if shown else (out.strip() or "(无输出)"))
        hits = re.findall(r"轮[：:]\s*通过\s*(\d+)/(\d+)", out)
        if hits:
            p, n = hits[-1]
            passed += int(p)
            total += int(n)
        else:
            broken.append(sc or "(默认)")

    print("=" * 74)
    if total:
        print("全部场景汇总：%d/%d = %.1f%%" % (passed, total, 100.0 * passed / total))
    if broken:
        print("以下场景没有产出可解析的结果，先看上面的原始输出：%s" % "、".join(broken))
        return 1
    if args.fail_under is not None and total and (passed / float(total)) < args.fail_under:
        print("低于 --fail-under %.2f" % args.fail_under)
        return 1
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.all_scenes:
        # --cases 是"只跑这一份"，--all-scenes 是"按场景全都跑一遍"，语义互斥。
        # 静默忽略其中一个会让 CI 以为跑了全套、实际只跑了一份。
        if args.cases:
            print("--all-scenes 不能和 --cases 同时用：前者按场景跑全部批次，后者只跑指定用例",
                  file=sys.stderr)
            return 2
        return run_all_scenes(args)

    if args.rounds < 1:
        print("--rounds 至少为 1", file=sys.stderr)
        return 2
    if args.fail_under is not None and not (0.0 <= args.fail_under <= 1.0):
        print("--fail-under 必须在 0~1 之间", file=sys.stderr)
        return 2

    try:
        if args.cases:
            cases = load_cases(args.cases)
            _skipped = []
        else:
            # 默认合并 eval/adversarial*.json，但**只取属于当前场景的那几批**。
            # 全都合进来是 11-09 那次 8.3% 的直接原因：340 条 POP 关单场景用例
            # 混进售后场景跑，开场话术、假客户、核身凭证全是另一套，
            # 全部卡在 IDENTITY，看起来像"系统很差"，实际是用例跑错了场景。
            # 分开存是为了"哪一批是回归防线"一眼可辨，按场景取才不会互相污染。
            import glob
            files = sorted(glob.glob(os.path.join(CASES_DIR, "adversarial*.json")))
            cases = []
            _skipped = []
            for path in files:
                want = scene_of_file(path)
                if want != _SCENE:
                    _skipped.append((os.path.basename(path), want or "(默认)"))
                    continue
                cases.extend(load_cases(path))
    except CaseError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not cases:
        # 场景名拼错 / 新批次没登记时，过滤后会是空集。先于 validate_cases 判：
        # 那个校验只会说「当前是 list」，看不出是场景没对上。
        print("没有属于场景 %s 的用例：检查 --scene 拼写，或把新批次登记到 "
              "SCENE_OF_FILE（当前登记：%s）"
              % (_SCENE or "(默认)", "、".join("%s→%s" % kv for kv in SCENE_OF_FILE.items())
                 or "无"), file=sys.stderr)
        return 2

    try:
        validate_cases(cases)
    except CaseError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if _skipped and not args.quiet:
        print("场景：%s（跳过 %d 批不属于本场景的用例集）"
              % (_SCENE or "(默认)", len(_skipped)))
        for name, want in _skipped:
            if want == "(默认)":
                # `--scene <默认>` 不是一条能敲的命令：默认场景就是不传 --scene。
                print("  跳过 %-34s 属于默认场景，请不带 --scene 单独跑" % name)
            else:
                print("  跳过 %-34s 属于 %s，请用 --scene %s 单独跑"
                      % (name, want, want))

    case_map = {c["id"]: c for c in cases}
    if not args.quiet:
        print("对抗测试：%d 条 × %d 轮" % (len(cases), args.rounds))

    per_round = []
    for i in range(args.rounds):
        rows = run_round(cases)
        per_round.append(rows)
        if not args.quiet:
            print("  第 %d 轮：通过 %d/%d" % (i + 1, sum(1 for r in rows if r["ok"]), len(rows)))

    stats = aggregate(per_round, [c["id"] for c in cases])
    meta = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "commit": _git_rev(),
        "cases_path": os.path.relpath(args.cases, R._ROOT) if args.cases else "eval/adversarial*.json（合并）",
    }
    text = render(case_map, per_round, stats, meta)

    payload = {
        "meta": meta,
        "rounds": stats["rounds"],
        "rates": stats["rates"],
        "avg_rate": round(stats["avg_rate"], 4),
        "stable": stats["stable"],
        "total": len(cases),
        "unstable": stats["unstable"],
        "failed_ids": stats["failed_ids"],
        "errors": stats["errors"],
        "per_case": stats["per_case"],
        "clusters": [{"stage": s, "type": k, "count": v}
                     for (s, k), v in stats["clusters"].most_common()],
        "raw": per_round,
    }

    if not args.no_report:
        md_path, json_path = write_report(
            text, payload, args.out,
            "adversarial_%s_r%d" % (datetime.datetime.now().strftime("%Y%m%d_%H%M"), args.rounds))
        print("报告：%s" % os.path.relpath(md_path, R._ROOT))
        print("结构化：%s" % os.path.relpath(json_path, R._ROOT))

    print("平均通过率 %.1f%%｜结论稳定率 %d/%d｜异常 %d 次｜未通过 %d 条"
          % (stats["avg_rate"] * 100, stats["stable"], len(cases),
             stats["errors"], len(stats["failed_ids"])))

    if stats["errors"]:
        return 3
    if args.fail_under is not None and stats["avg_rate"] < args.fail_under:
        print("低于阈值 %.0f%%，判定不达标" % (args.fail_under * 100), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
