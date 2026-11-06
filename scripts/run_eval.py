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
import shutil
import sys
import tempfile
from atexit import register as _atexit

# 让脚本能从任意目录运行：把项目根目录加进 sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import events                     # noqa: E402

# 事件库指向临时目录。这一行是必须的，不是可选的：
# 以前跑批直接写 data/events.db，跑一次 30 条用例就多 30 个 session。
# 实测后果——库里 305 个会话里 300 个是跑批产物，真人通话只有 5 通，
# 而且测试用例的 asr_result 耗时是 0.0ms，混进分位数会把首字 P50
# 拉到几十毫秒，看着漂亮实则是假的。评测数据绝不能和真实通话混在一个库里。
_tmp = tempfile.mkdtemp(prefix="sc_eval_")
events.DATA_DIR = _tmp
events.DB_PATH = os.path.join(_tmp, "eval.db")
events.init_db()
_atexit(lambda: shutil.rmtree(_tmp, ignore_errors=True))


def _assert_isolated():
    """启动自检：跑批的事件库必须在临时目录，绝不能落在仓库 data/ 下。

    为什么把它做成"启动就崩"而不是写进注释：历史上真出过事——
    data/events.db 里 305 个会话有 300 个是跑批产物，真人通话只剩 5 通，
    测试用例的 asr_result 耗时是 0.0ms，混进分位数把首字 P50 拉到几十毫秒。
    那次是靠人发现"数字太漂亮"才查出来的。这种坑不能靠自觉，得靠断言。
    """
    prod = os.path.abspath(os.path.join(_ROOT, "data"))
    db = os.path.abspath(events.DB_PATH)
    if db == prod or db.startswith(prod + os.sep):
        raise SystemExit("拒绝启动：跑批事件库指向生产目录 %s" % db)
    if not db.startswith(os.path.abspath(tempfile.gettempdir()) + os.sep):
        raise SystemExit("拒绝启动：跑批事件库不在临时目录 %s" % db)


_assert_isolated()

import config_loader              # noqa: E402

# C4/Q5：跑批场景可切换。EVAL_SCENE=pop_reject 时先重读配置再 import app，
# 否则 app 里那些「启动时读一次」的缓存会留在售后主场景，赛题用例会被
# 用售后话术与售后假客户跑一遍，分数看着绿其实测的不是赛题链路。
# 不设就保持默认（售后），老命令逐字节不变。
_EVAL_SCENE = os.environ.get("EVAL_SCENE", "").strip()
if _EVAL_SCENE:
    os.environ["APP_SCENE"] = _EVAL_SCENE
    config_loader.load(force=True)

# 跑批只验"业务流程对不对"，不真的合成音频。
# 不隔离的话 30 条用例 × 约 1.2 秒 = 白等 36 秒，还要花钱，
# 并且没有密钥的队友会在这里卡住——评测不该依赖外部服务。
config_loader.tts_config = lambda: {
    "provider": "browser", "voice": "zh-CN-XiaoxiaoNeural", "rate": 1.05}

# nlg 同理必须强制 template（与 verify.py #26 同理）：评测断言的是规则层的确定性
# 行为，qwen-flash 改写会改措辞；且本机开着 dashscope 时每轮真调模型——
# 实测把整场评测拖到逐条 60s 超时（2026-11-06，演示机场景）。评测不该依赖
# 外部服务，也不该随本机话术开关波动。adversarial_run.py 复用本模块，一并生效。
config_loader.nlg_config = lambda: {**config_loader.load().get("nlg", {}), "provider": "template"}

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
    intents = [e["payload"].get("intent", "") for e in evs if e["type"] == "agent_reply"]
    joined = " ".join(texts)

    # 2) 硬性违规：说错话 / 走错阶段 / 该说的没说
    for bad in expect.get("must_not", []):
        if bad and bad in joined:
            problems.append("说了不该说的：%s" % bad)
    # must_say 是 must_not 的反面：光靠「最终闭环」判不出中间轮有没有接住
    # 用户的话。T17 真实通话用例就吃过这个亏——把反问式承接和 3 位尾号
    # 引导关掉之后，系统仍能在后面几轮把信息补回来闭环，5 条用例全绿。
    # 加这一条，中间轮的「说明来意」「请说四位」才能被真正钉住。
    for good in expect.get("must_say", []):
        if good and good not in joined:
            problems.append("该说的没说：%s" % good)
    for bad_stage in expect.get("must_not_stage", []):
        if bad_stage in stages:
            problems.append("越界进入阶段：%s" % bad_stage)
    # 意图断言比文本断言稳：开场白里本来就带「我是京东客服中心」，
    # 用 must_not 去排除"没说明来意"会连开场白一起误伤（T17-R5 实测）。
    # intent 是每轮 agent_reply 的结构化字段，才能干净地区分
    # 「真提问走噪声通道」和「被误判成反问式火气走了 complain 承接」。
    for bad_intent in expect.get("must_not_intent", []):
        if bad_intent in intents:
            problems.append("不该出现的意图：%s" % bad_intent)
    for good_intent in expect.get("must_say_intent", []):
        if good_intent and good_intent not in intents:
            problems.append("该出现却没出现的意图：%s" % good_intent)

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


def closure_stats(cases, rows):
    """闭环双指标：应闭环（该闭的闭没闭上）与误闭环（不该闭的闭上了）。

    为什么不能用"闭环数 / 全部用例数"：30 条用例里有 14 条**按设计就不该闭环**
    （用户拒绝、要求挂断、身份核验失败、沉默挂机），把它们算进分母，
    得到的是"37%"，看起来像系统有一半业务没做完——评委一问就穿帮。
    真正该回答的是两个问题：
        该闭环的有没有闭环   → should_ok / should
        不该闭环的有没有乱闭  → wrongly / should_not（这个数应为 0）
    没在 expect 里声明 closed 的用例不参与统计，单列条数，避免被悄悄算成 0。
    """
    by_id = {r["id"]: r for r in rows}
    should = should_ok = should_not = wrongly = undeclared = 0
    for case in cases:
        expect = case.get("expect") or {}
        if "closed" not in expect:
            undeclared += 1
            continue
        row = by_id.get(case["id"])
        if row is None:            # 只跑了一部分用例时，没跑的按"未声明"处理
            undeclared += 1
            continue
        if expect["closed"]:
            should += 1
            should_ok += 1 if row["closed"] else 0
        else:
            should_not += 1
            wrongly += 1 if row["closed"] else 0
    return {
        "should": should, "should_ok": should_ok,
        "should_not": should_not, "wrongly": wrongly,
        "undeclared": undeclared,
        "rate": (should_ok / float(should)) if should else 0.0,
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fresh = "--fresh" in sys.argv

    if fresh:
        # 现在每轮都跑在全新的临时库里，--fresh 已经没有实际作用。
        # 保留这个参数只是不让老命令报错，顺便把"它以前会删生产库"这件事说清楚。
        print("提示：跑批已改用临时库，--fresh 不再需要（也不会删 data/events.db）\n")

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
            "holdout": bool(case.get("holdout")),
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
    cs = closure_stats(cases, rows)
    print("闭环率：应闭环 %d/%d = %.0f%%｜误闭环 %d/%d｜未声明 %d 条不计入"
          % (cs["should_ok"], cs["should"], cs["rate"] * 100,
             cs["wrongly"], cs["should_not"], cs["undeclared"]))
    print("事件库：%s（临时库，生产库 data/events.db 未被写入）" % events.DB_PATH)

    # 留出集只用于验收：它一旦参与调优，就不再是留出集。
    _hold = [r for r in rows if r.get("holdout")]
    if _hold:
        _hp = sum(1 for r in _hold if r["ok"])
        _hc = sum(1 for r in _hold if r["closed"])
        print("留出集：通过 %d/%d（%.0f%%）｜闭环 %d/%d｜**仅作验收用，不得据此调整规则或提示词**"
              % (_hp, len(_hold), _hp * 100.0 / len(_hold), _hc, len(_hold)))

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
