# -*- coding: utf-8 -*-
"""生成评委交付包：results/ 逐用例 JSON + 源码 zip（≤50MB，可直接上传）。

用法：
    python scripts/export_submission.py                # 跑评测 + 打包
    python scripts/export_submission.py --limit 40     # 只跑前 40 条（快速自检）
    python scripts/export_submission.py --no-eval      # 跳过评测，只按现有 results 打包

产物：
    results/<case_id>.json      每个测试用例一个 JSON，文件名与用例 id 完全一致
    results/_summary.json       汇总（通过率、分类统计、生成时间）
    dist/super_candy_submission_<日期>.zip

为什么 results 是"一个用例一个文件"：
    赛题表单要求"每张图片给出一个 JSON 文件，且 JSON 文件名与去掉图片后缀的
    名字完全一致"。本项目是语音/对话系统，没有图片输入，等价口径是
    **每个测试用例一个 JSON，文件名 = 用例 id**（`results/C4-001.json` 对应
    用例 C4-001）。字段里保留 input / expect / actual / verdict，评审可以直接
    逐条核对，不必先跑代码。
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

_SCENE_NO = re.compile(r"官方场景\s*(\d+)")

OUT_DIR = os.path.join(ROOT, "results")
DIST_DIR = os.path.join(ROOT, "dist")
DEFAULT_CASES = sorted(glob.glob(os.path.join(ROOT, "eval", "adversarial_c4_batch*.json")))

#: 交付包白名单（目录以 / 结尾表示整目录）。**config/secrets.json 永不入包。**
INCLUDE_DIRS = ("modules/", "web/", "scripts/", "docs/", "mock_business/",
                "config/mock/", "config/scenes/")
INCLUDE_FILES = (
    "app.py", "contracts.py", "config_loader.py", "events.py", "dashboard.py",
    "cost_guard.py", "runtime_db_guard.py", "ws_channel.py",
    "config/scene.json", "config/kb_pop.json", "config/evolution/versions.json",
    # 只发模板不发真 key：评委照 README 第二节复制成 secrets.json 再填自己的。
    # 漏了这个文件，包里就没有密钥模板，README 那步"复制 example"会直接失败。
    "config/secrets.example.json",
    "config/evolution/versions/v74/", "requirements.txt", "requirements-vad.txt",
    "README_评审.md", "启动.bat", "启动.command",
    "data/customer_cards.json", "data/demo_events.db",
)
EXCLUDE_DIRS = {"__pycache__", ".git", ".github", ".githooks", "node_modules",
                "dist", "competition", "data/models", "data/archive",
                "data/backup", "data/logs", "data/vad_corpus",
                "data/vad_corpus_holdout", "data/vad_real", "data/vad_calib",
                "data/speaker_corpus", "eval/reports", "eval/ui"}
EXCLUDE_FILES = {"config/secrets.json"}


def _run_eval(cases_files, limit):
    """跑评测集，返回逐用例结果。复用 run_eval 的 run_case/judge，口径一致。

    可复现：每条用例按**自己报的尾号**固定到申请单（`applications.json` 里
    verify_by_tail 唯一），跑不到再按 goal 的「官方场景NN」兜底。
    不固定的话 `start_call()` 从外呼名单随机取单，同一份代码两次跑出两套结果，
    评审没法核对。每条结果都记下实际用的 application_id。
    """
    sys.argv = ["export_submission", "--scene", "pop_reject"]   # 必须是赛题场景
    import run_eval as RE                                       # noqa: E402

    main_apps = json.load(open(os.path.join(ROOT, "config", "mock", "applications.json"),
                               encoding="utf-8"))["items"]
    by_tail = {}
    for a in main_apps:
        if a.get("close_mode") == "电话" and a.get("call_status") not in ("无需外呼", "无效号码"):
            by_tail.setdefault(a.get("verify_by_tail"), []).append(a["application_id"])

    cat_apps = json.load(open(os.path.join(ROOT, "config", "mock",
                                           "applications_catalog.json"),
                              encoding="utf-8"))["items"]
    by_code = {}
    for a in cat_apps:
        if a.get("close_mode") == "电话" and a.get("call_status") not in ("无需外呼", "无效号码"):
            by_code.setdefault(a.get("reject_code"), []).append(a["application_id"])

    fallback = next(iter(by_tail.values()))[0]
    current = {"app": fallback}
    _orig_start = RE.start_call

    def _fixed_start(*a, **k):
        return _orig_start(application_id=current["app"], lock_target=True)

    RE.start_call = _fixed_start

    def _pick(case):
        """按场景号选单，并把用例里的尾号换成这张单的真实尾号。

        为什么不能只按尾号选：C4 用例生成时尾号是
        `tails[(i * 5 + len(qcode)) % len(tails)]` 随便取的，与场景无关；
        照它绑单会出现"客户说资质造假，客服答授权链路不完整"的错位对话，
        后面自然走不到闭环（实测 105 条这样假失败）。
        按场景号（goal 里的「官方场景NN」→ KB-POP-NN）选单，内容才对得上；
        尾号只是核身脚手架，换成该单真实尾号即可，用例的追问意图不变。
        """
        m2 = _SCENE_NO.search(case.get("goal") or "")
        code = "KB-POP-%03d" % int(m2.group(1)) if m2 else ""
        if code and code not in by_code:
            # 这个场景在官方档案里没有可电话外呼的单（京麦站内信 / 号码无效 /
            # 无需外呼）。电话外呼系统对它不适用，跳过并记账，不硬跑出假失败。
            return None, code, "", False, "该场景无可用电话档案（京麦/无效号码/无需外呼）"
        app = (by_code.get(code) or [fallback])[0]
        rec = next((x for x in cat_apps if x["application_id"] == app), {})
        tail = str(rec.get("verify_by_tail") or "")
        rewritten = False
        if tail:
            turns = []
            for t in case.get("turns") or []:
                nt = re.sub(r"尾号\s*\d{4}", "尾号" + tail, t)
                rewritten = rewritten or (nt != t)
                turns.append(nt)
            case["turns"] = turns
        return app, code, tail, rewritten, ""

    cases = []
    for f in cases_files:
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data["cases"] if isinstance(data, dict) else data
        for c in rows:
            c.setdefault("_source", os.path.basename(f))
            cases.append(c)
    if limit:
        cases = cases[:limit]

    out = []
    skipped = []
    t_all = time.time()
    for i, case in enumerate(cases, 1):
        goal = case.get("goal") or ""
        app, code, tail, rewritten, skip_reason = _pick(case)
        if skip_reason:
            skipped.append({"case_id": case.get("id") or ("case_%03d" % i),
                            "reject_code": code, "reason": skip_reason})
            continue
        current["app"] = app
        t0 = time.time()
        last, evs = RE.run_case(case)
        ok, problems, closed, consent = RE.judge(case, last, evs)
        replies = [e["payload"] for e in evs if e["type"] == "agent_reply"]
        out.append({
            "case_id": case.get("id") or ("case_%03d" % i),
            "category": case.get("category", ""),
            "tag": case.get("tag", ""),
            "source_file": case.get("_source", ""),
            "goal": goal,
            "application_id": current["app"],
            "reject_code": code,
            "verify_tail": tail,
            "tail_rewritten": rewritten,
            "input": {"turns": case.get("turns", [])},
            "expect": case.get("expect", {}),
            "actual": {
                "replies": [
                    {"stage": r.get("stage"), "next_stage": r.get("next_stage"),
                     "intent": r.get("intent"), "text": r.get("text", "")}
                    for r in replies
                ],
                "closed": closed,
                "consent": consent,
                "final_stage": (last.get("next_stage") or last.get("stage") or ""),
            },
            "verdict": "pass" if ok else "fail",
            "problems": problems,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        })
        if i % 20 == 0 or i == len(cases):
            print("  跑完 %d/%d，通过 %d" % (i, len(cases),
                                            sum(1 for x in out if x["verdict"] == "pass")))
    return out, skipped, round((time.time() - t_all) * 1000, 1)


def _write_results(rows, skipped, elapsed):
    os.makedirs(OUT_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(OUT_DIR, "*.json")):
        os.remove(old)
    for r in rows:
        with open(os.path.join(OUT_DIR, r["case_id"] + ".json"), "w",
                  encoding="utf-8", newline="\n") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
            f.write("\n")

    by_cat = {}
    for r in rows:
        k = r["category"] or "未分类"
        s = by_cat.setdefault(k, {"total": 0, "pass": 0})
        s["total"] += 1
        s["pass"] += 1 if r["verdict"] == "pass" else 0

    # 失败按场景聚合：评审一眼能看出是"某个场景的档案不适用"还是"系统性缺陷"
    fail_by_scene = {}
    for r in rows:
        if r["verdict"] == "pass":
            continue
        scene = (r["case_id"].split("-")[1] if "-" in r["case_id"] else r["case_id"])
        item = fail_by_scene.setdefault(scene, {"reject_code": r.get("reject_code", ""),
                                                "fail": 0, "cases": []})
        item["fail"] += 1
        item["cases"].append(r["case_id"])
        if r.get("problems"):
            item["reason"] = r["problems"][0]
    passed = sum(1 for r in rows if r["verdict"] == "pass")
    summary = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scene": "pop_reject（商家资质审核驳回外呼）",
        "cases": len(rows),
        "pass": passed,
        "pass_rate": round(passed / len(rows), 4) if rows else 0.0,
        "elapsed_ms": elapsed,
        "by_category": by_cat,
        "fail_by_scene": fail_by_scene,
        "skipped": len(skipped),
        "skip_reason": "官方档案里该场景没有可电话外呼的单（京麦站内信 / 号码无效 / 无需外呼），"
                       "电话外呼链路不适用，不硬跑成失败",
        "skipped_cases": skipped,
        "naming": "一个用例一个 JSON，文件名 = 用例 id（对应表单『每张图片一个 JSON』的等价口径）",
        "fields": ["case_id", "category", "tag", "source_file", "goal",
                   "application_id", "reject_code", "verify_tail", "tail_rewritten",
                   "input.turns", "expect", "actual.replies", "actual.closed",
                   "actual.consent", "actual.final_stage", "verdict", "problems"],
    }
    with open(os.path.join(OUT_DIR, "_summary.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
        f.write("\n")
    # 防假绿：写出来的文件数必须等于结果条数。少一个就说明 case_id 撞了，
    # 那样评审按文件名找用例会对不上——宁可这里直接报错，也不要静默丢结果。
    n_files = len(glob.glob(os.path.join(OUT_DIR, "C4*.json")))
    if n_files != len(rows):
        raise SystemExit("results 文件数 %d 与结果条数 %d 不一致：检查用例 id 是否重复"
                         % (n_files, len(rows)))
    return summary


def _should_skip(rel):
    rel = rel.replace("\\", "/")
    if rel in EXCLUDE_FILES or rel.split("/")[-1] in EXCLUDE_FILES:
        return True
    for d in EXCLUDE_DIRS:
        if rel == d or rel.startswith(d + "/"):
            return True
    return False


def _collect():
    files = []
    for name in INCLUDE_DIRS:
        base = os.path.join(ROOT, name.rstrip("/"))
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, ROOT).replace("\\", "/")
                if not _should_skip(rel):
                    files.append((p, rel))
    for name in INCLUDE_FILES:
        p = os.path.join(ROOT, name)
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    rel = os.path.relpath(fp, ROOT).replace("\\", "/")
                    if not _should_skip(rel):
                        files.append((fp, rel))
        elif os.path.isfile(p) and not _should_skip(name):
            files.append((p, name.replace("\\", "/")))
    # eval 用例（只带用例 JSON，不带 reports）
    for p in glob.glob(os.path.join(ROOT, "eval", "*.json")):
        files.append((p, os.path.relpath(p, ROOT).replace("\\", "/")))
    # results 必须进包（评审要求）
    for p in glob.glob(os.path.join(OUT_DIR, "*")):
        if os.path.isfile(p):
            files.append((p, os.path.relpath(p, ROOT).replace("\\", "/")))
    # 去重
    seen, out = set(), []
    for p, rel in files:
        if rel not in seen:
            seen.add(rel)
            out.append((p, rel))
    return sorted(out, key=lambda x: x[1])


def _zip(files):
    os.makedirs(DIST_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    zpath = os.path.join(DIST_DIR, "super_candy_submission_%s.zip" % stamp)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p, rel in files:
            z.write(p, "super_candy/" + rel)
    return zpath


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条，便于自检")
    ap.add_argument("--no-eval", action="store_true", help="跳过评测，用现有 results 打包")
    ap.add_argument("--cases", nargs="*", default=None, help="指定用例文件")
    args = ap.parse_args()

    if not args.no_eval:
        files = args.cases or DEFAULT_CASES
        print("跑评测：%d 个文件" % len(files))
        rows, skipped, elapsed = _run_eval(files, args.limit)
        summary = _write_results(rows, skipped, elapsed)
        print("结果：%d/%d 通过（%.1f%%），跳过 %d 条（场景不可电话外呼），写入 %s"
              % (summary["pass"], summary["cases"], summary["pass_rate"] * 100,
                 summary["skipped"], os.path.relpath(OUT_DIR, ROOT)))
    if not os.path.isdir(OUT_DIR):
        raise SystemExit("没有 results/：先跑评测，或去掉 --no-eval")

    files = _collect()
    zpath = _zip(files)
    size = os.path.getsize(zpath)
    print("打包：%s（%d 个文件，%.1f MB）"
          % (os.path.relpath(zpath, ROOT), len(files), size / 1024 / 1024))
    if size > 50 * 1024 * 1024:
        raise SystemExit("超过 50MB 上限，检查 INCLUDE_* 白名单")
    print("上限 50MB：通过")


if __name__ == "__main__":
    main()
