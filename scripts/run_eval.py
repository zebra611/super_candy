"""评测跑批：把评测集的用例跑一遍，出分数表。

用法：
    python scripts/run_eval.py                      # 跑默认评测集（售后主场景）
    python scripts/run_eval.py --scene pop_reject   # 跑赛题场景（Q5）
    python scripts/run_eval.py --cases eval/personas_generated.json
    python scripts/run_eval.py --strict             # 有用例失败时 exit 1（Q3，接 CI）
    python scripts/run_eval.py --report eval/reports/eval_main.md
    python scripts/run_eval.py N01 I02              # 只跑指定用例

`--scene` 必须在 import app **之前**生效：app 里有几处「启动时读一次」的缓存，
# 跑批必须可复现：模型客服每次说的话不一样，验收口径会漂，这里强制关掉。
import os as _os_agent
_os_agent.environ.setdefault("AGENT_FREE", "0")

import 之后再切场景，赛题用例会被售后话术与售后假客户跑一遍——
分数看着绿，其实测的不是赛题链路。所以参数在模块级就解析（见 `_scan_argv`）。

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
from datetime import datetime

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

# C4/Q5：跑批场景可切换，且必须赶在 `import app` 之前。
# 这里在模块级解析参数，而不是等 main()——app 在 import 时就把配置读进缓存了，
# 事后调 set_scene 只能改「每轮现读」的那部分，启动期缓存的仍留在旧场景。
# 不传 --scene 且不设 EVAL_SCENE 时跑 **service_outbound（售后回访）**。
# 这里必须写死，不能"跟随默认场景"：
#   · 跑批用例（T17 真实通话、Q1 分诊回归、Q2 课程）是在售后场景下设计的，
#     must_say 钉的是售后话术（如"我是京东客服中心"）；
#   · 默认场景后来换成了赛题 POP 资审，"跟随默认"会让这批用例集体失配——
#     实测踩过：默认一换，T17 立刻从 6/6 掉到 5/6，报错的是用例不是代码。
#   · 老命令（不带 --scene）逐字节不变：它本来跑的就是售后场景。
def _scan_argv(argv):
    """一次性解析参数。main() 与模块级都读它，避免两处口径漂移。"""
    opts = {"scene": "", "cases": "", "report": "",
            "strict": False, "fresh": False, "ids": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        matched = False
        for key in ("scene", "cases", "report"):
            flag = "--" + key
            if a == flag and i + 1 < len(argv):
                opts[key] = argv[i + 1]
                i += 2
                matched = True
                break
            if a.startswith(flag + "="):
                opts[key] = a.split("=", 1)[1]
                i += 1
                matched = True
                break
        if matched:
            continue
        if a == "--strict":
            opts["strict"] = True
        elif a == "--fresh":
            opts["fresh"] = True
        elif not a.startswith("--"):
            opts["ids"].append(a)
        i += 1
    return opts


_OPTS = _scan_argv(sys.argv[1:])

_EVAL_SCENE = ((_OPTS["scene"] or os.environ.get("EVAL_SCENE", "")).strip()
               or "service_outbound")
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

CASES_PATH = (_OPTS["cases"] if _OPTS["cases"]
              else os.path.join(_ROOT, "eval", "seed_cases.json"))
if not os.path.isabs(CASES_PATH):
    CASES_PATH = os.path.join(_ROOT, CASES_PATH)


def load_cases():
    """读评测集。JSON 不合法会直接抛错——这是故意的，别拿坏数据跑分。

    支持顶层是 {"cases": [...]} 或直接是列表（画像工厂产物是列表）。
    """
    with open(CASES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["cases"] if isinstance(data, dict) else data


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


def _write_report(path, rows, cs, by_cat):
    """把这一轮结果落一份 markdown（Q5：两个场景分开存档，不靠翻聊天记录）。

    字段只在报告里出现、不参与判定——报告算错了会误导人，
    所以判定口径一律复用 `judge` 的结论，这里只做排版。
    """
    path = path if os.path.isabs(path) else os.path.join(_ROOT, path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    passed = sum(1 for r in rows if r["ok"])
    lines = [
        "# 评测报告：%s" % (_EVAL_SCENE or "售后主场景"),
        "",
        "> 生成时间：%s；用例文件：`%s`；事件库为临时库，生产库未被写入。"
        % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           os.path.relpath(CASES_PATH, _ROOT)),
        "> 本报告是**文字驱动**的流程验证，不含语音识别与时延；"
        "通过率不等于真实来电者上的准确率（口径见 `docs/24`、`docs/21`）。",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        "| 通过率 | **%d/%d = %.0f%%** |" % (passed, len(rows),
                                              passed * 100.0 / max(1, len(rows))),
        "| 应闭环达成 | %d/%d = %.0f%% |" % (cs["should_ok"], cs["should"],
                                             cs["rate"] * 100),
        "| 误闭环 | %d/%d |" % (cs["wrongly"], cs["should_not"]),
        "| 未声明不计入 | %d 条 |" % cs["undeclared"],
        "",
        "分类通过：" + "；".join("%s %d/%d" % (k, v[1], v[0])
                             for k, v in by_cat.items()),
        "",
        "## 明细",
        "",
        "| ID | 类别 | 结论 | 闭环 | 意向 | 问题 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            r["id"], r["category"], "通过" if r["ok"] else "**失败**",
            "是" if r["closed"] else "否",
            str(r["consent"]) if r["consent"] not in ("", None) else "-",
            "；".join(r["problems"]) or ""))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


def main():
    args = _OPTS["ids"]
    fresh = _OPTS["fresh"]

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

    if _OPTS["report"]:
        _p = _write_report(_OPTS["report"], rows, cs, by_cat)
        print("报告已写入：%s" % os.path.relpath(_p, _ROOT))

    # Q3：--strict 只在显式传参时改变退出码。不传参仍返回 0，
    # 现有调用方（C4 跑批、演示脚本）行为逐字节不变。
    if _OPTS["strict"] and passed != len(rows):
        print("\n--strict：%d 条未通过，返回 1" % (len(rows) - passed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
