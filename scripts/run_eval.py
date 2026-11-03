"""评测跑批：把 eval/seed_cases.json 的 30 条用例跑一遍，出分数表。

用法：
    python scripts/run_eval.py            # 跑全部用例
    python scripts/run_eval.py --fresh    # 先清空事件库再跑（推荐，便于对比）
    python scripts/run_eval.py N01 I02    # 只跑指定用例

它做什么：
    对每条用例开一个 session，按 turns 依次调 handle_turn（进程内直调，
    不走 HTTP，速度快），收齐事件流后按 expect 判定，最后打一张表。

它不做什么：
    不模拟真实音频、不测时延、不调大模型。它只验"业务流程对不对"。

判定用事件，不用回复文本——判"是否闭环"只看有没有 writeback 事件，
因为模型换个说法（"已经登记" / "帮您记下了"）文本匹配就会判错。
"""

import json
import os
import sys

# 让脚本能从任意目录运行：把项目根目录加进 sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import events                     # noqa: E402
import config_loader              # noqa: E402

# 跑批只验"业务流程对不对"，不真的合成音频。
# 不隔离的话 30 条用例 × 约 1.2 秒 = 白等 36 秒，还要花钱，
# 并且没有密钥的队友会在这里卡住——评测不该依赖外部服务。
config_loader.tts_config = lambda: {
    "provider": "browser", "voice": "zh-CN-XiaoxiaoNeural", "rate": 1.05}

from app import end_call, handle_turn, start_call   # noqa: E402

CASES_PATH = os.path.join(_ROOT, "eval", "seed_cases.json")


def load_cases():
    """读评测集。JSON 不合法会直接抛错——这是故意的，别拿坏数据跑分。"""
    with open(CASES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["cases"]


def run_case(case):
    """跑一条用例，返回 (运行结果, 事件流)。

    运行结果里带最后几轮的返回，用于读槽位（尤其是 consent）。
    """
    session_id = start_call()["session_id"]
    last = None
    for utterance in case.get("turns", []):
        last = handle_turn(session_id, user_text=utterance)
    evs = events.list_events(session_id)
    end_call(session_id)          # 会清掉内存会话，所以上面先把状态取出来
    return last or {}, evs


def judge(case, last, evs):
    """按 expect 判定一条用例，返回 (是否通过, 问题列表)。

    判定顺序：先看硬性违规（must_not / 禁止阶段），再看结局是否符合预期。
    违规优先，是因为合规问题比"没闭环"严重得多。
    """
    expect = case.get("expect", {})
    problems = []

    # 1) 收集所有 Agent 说过的话与经历过的阶段
    texts = [e["payload"].get("text", "") for e in evs if e["type"] == "agent_reply"]
    stages = [e["payload"].get("stage", "") for e in evs if e["type"] == "agent_reply"]
    joined = " ".join(texts)

    # 2) 硬性违规：说错话 / 走错阶段
    for bad in expect.get("must_not", []):
        if bad and bad in joined:
            problems.append("说了不该说的：%s" % bad)
    for bad_stage in expect.get("must_not_stage", []):
        if bad_stage in stages:
            problems.append("越界进入阶段：%s" % bad_stage)

    # 3) 结局判定
    closed = any(e["type"] == "writeback" for e in evs)
    if "closed" in expect and bool(expect["closed"]) != closed:
        problems.append("闭环不符：期望 %s 实际 %s" % (expect["closed"], closed))

    consent = (last.get("slots") or {}).get("consent")
    if "consent" in expect:
        want = expect["consent"]
        # 注意：consent 可能是 True / False / "unknown"，三者要分开判，
        # 不能简单做布尔比较，否则 "unknown" 会被当成 False 蒙过去。
        if want is True and consent is not True:
            problems.append("意向不符：期望同意，实际 %s" % consent)
        elif want is False and consent is not False:
            problems.append("意向不符：期望拒绝，实际 %s" % consent)

    if "final_stage" in expect:
        final_stage = (last.get("next_stage") or last.get("stage") or "")
        if final_stage != expect["final_stage"]:
            problems.append("终态不符：期望 %s 实际 %s" % (expect["final_stage"], final_stage))

    return (len(problems) == 0), problems, closed, consent


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fresh = "--fresh" in sys.argv

    if fresh:
        # 清库只为让这一轮的统计干净，不代表线上数据
        db = events.DB_PATH
        if os.path.exists(db):
            os.remove(db)
        events.init_db()
        print("已清空事件库，开始新一轮评测\n")

    all_cases = load_cases()
    cases = [c for c in all_cases if (not args or c["id"] in args)]
    if not cases:
        print("没有匹配的用例")
        return 1

    rows = []
    for case in cases:
        last, evs = run_case(case)
        ok, problems, closed, consent = judge(case, last, evs)
        rows.append({
            "id": case["id"], "category": case["category"],
            "ok": ok, "closed": closed, "consent": consent, "problems": problems,
        })

    # ---- 输出表格 ----
    print("%-5s %-13s %-6s %-6s %-10s %s" % ("ID", "类别", "结论", "闭环", "意向", "问题"))
    print("-" * 92)
    for r in rows:
        print("%-5s %-13s %-6s %-6s %-10s %s" % (
            r["id"], r["category"], "通过" if r["ok"] else "失败",
            "是" if r["closed"] else "否",
            str(r["consent"]) if r["consent"] not in ("", None) else "-",
            "；".join(r["problems"]) or "",
        ))

    # ---- 汇总 ----
    passed = sum(1 for r in rows if r["ok"])
    print("-" * 92)
    print("通过率：%d/%d = %.0f%%" % (passed, len(rows), passed * 100.0 / len(rows)))
    print("闭环率：%d/%d" % (sum(1 for r in rows if r["closed"]), len(rows)))

    by_cat = {}
    for r in rows:
        t, p = by_cat.get(r["category"], (0, 0))
        by_cat[r["category"]] = (t + 1, p + (1 if r["ok"] else 0))
    print("分类通过：", "  ".join("%s %d/%d" % (k, v[1], v[0]) for k, v in by_cat.items()))

    print("\n注意：这批用例是**文字驱动**的，不经过语音识别，也不测时延。"
          "它只证明业务流水线（环节推进、回写、纠错、合规）通了；"
          "100% 的成因与过拟合风险见 docs/06 第 7 节，汇报前必读。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
