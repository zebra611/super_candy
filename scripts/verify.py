"""自检脚本 —— 用来验证"代码实际做了什么"，不是"文档说做了什么"。

为什么要这个东西：
    过去出现过这样的问题——文档写"已实现"，代码里却是死代码或硬编码。
    口头汇报和文档都不可靠，唯一可靠的是**实际跑一遍，看原始输出**。

怎么用：
    python scripts/verify.py

它会做三件事：
    1. 打印当前提交与改动文件（来自 git，不是我自己写的说明）；
    2. 逐条跑断言，并把**原始事件链**打出来，让你自己判断对错；
    3. 最后打印一张指标快照，确认双口径时延真的在算。

设计原则：
    - 只用标准库，不依赖服务是否启动；
    - 用临时数据库，不污染 data/events.db；
    - 每条都打印实际数据，不只打印"PASS"——结论可以被伪造，原始输出不能。
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 先把事件库指向临时目录，避免把自检数据写进正式库
import events  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="sc_verify_")
events.DATA_DIR = _tmp
events.DB_PATH = os.path.join(_tmp, "verify.db")
events.init_db()

import config_loader  # noqa: E402
import contracts  # noqa: E402
from app import (_acquire, _auth, _release, end_call, handle_turn,  # noqa: E402
                 report_client_metric, start_call)
from modules import dialogue, eval as eval_mod, tools  # noqa: E402

results = []

# 让"用户报尾号"这类剧本可以反复跑：工单是内存态的，每次自检从头开始
tools._reset_for_test()


def check(name, ok, detail):
    """记一条断言。detail 必须是可以被人自己看懂的证据。"""
    results.append((name, ok))
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    for line in detail:
        print("        " + line)


def event_types(session_id):
    return [e["type"] for e in events.list_events(session_id)]


def payloads(session_id, etype):
    return [e["payload"] for e in events.list_events(session_id) if e["type"] == etype]


def payload_of(session_id, etype):
    got = payloads(session_id, etype)
    return got[0] if got else None


def run_turns(turns, audio=False):
    """开一通电话，按剧本说完，返回 (最后一条返回, session_id)。

    audio=False 走文字输入，不消耗识别、也不需要麦克风；只有测语音链路时才传音频。
    """
    sid = start_call()["session_id"]
    last = None
    for t in turns:
        last = handle_turn(sid, audio_bytes=b"\x00" * 3200) if audio else handle_turn(sid, user_text=t)
    return last, sid


def git(*args):
    """调用 git 拿第三方记录。没装 git 就返回空，不影响断言执行。"""
    try:
        out = subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip()
    except Exception:
        return ""


def main():
    print("=" * 74)
    print("证据 1：git 记录（这部分不是我自己写的）")
    print("=" * 74)
    head = git("log", "-1", "--format=%h %ad %s", "--date=short")
    print("  当前提交: %s" % (head or "（取不到，可能没装 git）"))
    changed = git("show", "--stat", "--oneline", "HEAD")
    if changed:
        print("  最近一次提交改了这些文件:")
        for line in changed.splitlines()[1:]:
            print("    " + line.strip())
    print("  另可在 GitHub 网页上直接看 diff，那边的历史我改不了。")

    print()
    print("=" * 74)
    print("证据 2：契约里有哪些事件类型")
    print("=" * 74)
    need = {"no_answer", "latency_report", "error"}
    check("EVENT_TYPES 含 no_answer / latency_report / error",
          need <= set(contracts.EVENT_TYPES),
          ["实际共 %d 个：%s" % (len(contracts.EVENT_TYPES),
                             ", ".join(sorted(contracts.EVENT_TYPES)))])

    print()
    print("=" * 74)
    print("证据 3：文字输入这一轮写了什么，尾号有没有被打码")
    print("=" * 74)
    out = handle_turn((sid_a := start_call()["session_id"]), user_text="是我，尾号8866")
    types = event_types(sid_a)
    asr = payload_of(sid_a, "asr_result")
    check("文字输入不写 vad_result", "vad_result" not in types,
          ["实际事件链：%s" % " → ".join(types)])
    check("文字输入标 source=text 且 fake=false",
          bool(asr) and asr.get("source") == "text" and asr.get("fake") is False,
          ["asr_result.payload = %s" % json.dumps(asr, ensure_ascii=False)])
    check("入库的识别文本里尾号被打码（原始值只留在内存）",
          bool(asr) and "8866" not in asr.get("text", "") and "****" in asr.get("text", ""),
          ["入库文本 = %r" % (asr or {}).get("text"),
           "原始输入 = '是我，尾号8866'；对话逻辑仍用原文，所以身份核验通过：" +
           ("是" if out["next_stage"] == "NEED" else "否（next_stage=%s）" % out["next_stage"])])
    end_call(sid_a)

    print()
    print("=" * 74)
    print("证据 4：用户不说话时，系统会不会假装他说了话")
    print("=" * 74)
    sid_b = start_call()["session_id"]
    out1 = handle_turn(sid_b, user_text="")
    types_b = event_types(sid_b)
    check("沉默写 no_answer 事件，且不写 asr_result",
          "no_answer" in types_b and "asr_result" not in types_b,
          ["实际事件链：%s" % " → ".join(types_b),
           "no_answer.payload = %s" % json.dumps(payload_of(sid_b, "no_answer"), ensure_ascii=False)])
    check("沉默不推进状态机，且说的是追问",
          out1["next_stage"] == "GREETING" and ("还在吗" in out1["reply_text"] or "听不到" in out1["reply_text"]),
          ["stage=%s → next_stage=%s" % (out1["stage"], out1["next_stage"]),
           "实际回复：%s" % out1["reply_text"]])

    out2 = handle_turn(sid_b, user_text="")
    check("连续两次沉默才挂断", out2["next_stage"] == "DONE",
          ["第二次实际回复：%s" % out2["reply_text"],
           "next_stage = %s" % out2["next_stage"]])
    end_call(sid_b)

    print()
    print("=" * 74)
    print("证据 5：含糊回答会不会被当成拒绝")
    print("=" * 74)
    sid_c = start_call()["session_id"]
    for t in ["是我，8866", "用着还行", "可以"]:
        handle_turn(sid_c, user_text=t)
    amb1 = handle_turn(sid_c, user_text="嗯……")
    check("第一次含糊：追问一次，不挂断、不回写",
          amb1["next_stage"] == "CONFIRM" and "同意或者不需要" in amb1["reply_text"],
          ["实际回复：%s" % amb1["reply_text"],
           "next_stage = %s（应为 CONFIRM）" % amb1["next_stage"],
           "writeback 事件数 = %d（应为 0）" % len(payloads(sid_c, "writeback"))])
    amb2 = handle_turn(sid_c, user_text="再说吧")
    last_ticket = tools.ticket_for_session(sid_c) or {}
    check("第二次仍含糊：记 unknown 并回写（不是拒绝）",
          amb2["next_stage"] == "DONE" and amb2["slots"].get("consent") == "unknown"
          and len(payloads(sid_c, "writeback")) == 1,
          ["consent = %r" % amb2["slots"].get("consent"),
           "工单内容 consent = %r" % (last_ticket.get("content") or {}).get("consent"),
           "实际回复：%s" % amb2["reply_text"]])
    fixed = handle_turn(sid_c, user_text="那我要吧")
    check("unknown 之后说清了，也要能更正过来",
          fixed["slots"].get("consent") is True and len(payloads(sid_c, "writeback")) == 2,
          ["consent = %r" % fixed["slots"].get("consent"),
           "writeback 事件数 = %d" % len(payloads(sid_c, "writeback")),
           "实际回复：%s" % fixed["reply_text"]])
    end_call(sid_c)

    print()
    print("=" * 74)
    print("证据 6：明确同意 → 回写；再改口 → 同一张工单被更正")
    print("=" * 74)
    sid_d = start_call()["session_id"]
    for t in ["是我，8866", "用着还行", "可以", "同意"]:
        done = handle_turn(sid_d, user_text=t)
    ticket_v1 = tools.ticket_for_session(sid_d) or {}
    wb1 = payloads(sid_d, "writeback")
    check("同意后闭环，工单结论为 true",
          done["closed"] and done["slots"].get("consent") is True
          and (ticket_v1.get("content") or {}).get("consent") is True,
          ["writeback 事件：%s" % json.dumps(wb1, ensure_ascii=False),
           "工单 %s v%s" % (ticket_v1.get("ticket_id"), ticket_v1.get("version"))])

    changed = handle_turn(sid_d, user_text="等等，我不要了")
    ticket_v2 = tools.ticket_for_session(sid_d) or {}
    wb2 = payloads(sid_d, "writeback")
    check("DONE 之后改口：再回写一次并更正结论",
          changed["slots"].get("consent") is False and len(wb2) == 2
          and wb2[1].get("version") == 2,
          ["writeback 事件数 = %d，第二条 version = %s"
           % (len(wb2), wb2[1].get("version") if len(wb2) > 1 else "-"),
           "实际回复：%s" % changed["reply_text"]])
    check("改口是更新同一张工单，不是另开一张",
          ticket_v1.get("ticket_id") == ticket_v2.get("ticket_id")
          and (ticket_v2.get("content") or {}).get("consent") is False,
          ["工单号 v1 = %s，v2 = %s" % (ticket_v1.get("ticket_id"), ticket_v2.get("ticket_id")),
           "工单最新内容 consent = %r" % (ticket_v2.get("content") or {}).get("consent")])
    end_call(sid_d)

    print()
    print("=" * 74)
    print("证据 7：禁语命中后被整句替换，而不是打码")
    print("=" * 74)
    guard = dialogue.compliance_guard("我保证您百分百赚钱，不办就没名额了")
    check("命中禁语 → 整句换成合规话术",
          guard["blocked"] and "***" not in guard["text"]
          and not any(w in guard["text"] for w in config_loader.forbidden_words()),
          ["命中词：%s" % "、".join(guard["hits"]),
           "实际播报文本：%s" % guard["text"],
           "配置的合规话术：%s" % config_loader.blocked_reply()])

    print()
    print("=" * 74)
    print("证据 8：质检报告能不能判必说话术与分类违规")
    print("=" * 74)
    sid_e = start_call()["session_id"]
    for t in ["是我，8866", "用着还行", "可以", "同意"]:
        handle_turn(sid_e, user_text=t)
    report = eval_mod.compliance_report(sid_e)
    check("必说话术检查已实现（不再是 skipped）",
          report["checks"]["must_say_hit"]["status"] == "pass",
          ["must_say_hit = %s" % json.dumps(report["checks"]["must_say_hit"], ensure_ascii=False)])
    check("五项合规检查都有结论，没有漏项",
          all(k in report["checks"] for k in contracts.COMPLIANCE_CHECKS),
          ["各项状态：%s" % json.dumps({k: v["status"] for k, v in report["checks"].items()},
                                   ensure_ascii=False)])

    sid_e2 = start_call()["session_id"]
    handle_turn(sid_e2, user_text="你谁啊")          # 没闭环的一通电话
    open_report = eval_mod.compliance_report(sid_e2)
    check("质检分不是恒为 1 的摆设（未闭环会被扣分）",
          open_report["score"] < report["score"],
          ["闭环通话 score = %s，未闭环通话 score = %s"
           % (report["score"], open_report["score"])])
    end_call(sid_e2)
    end_call(sid_e)

    print()
    print("=" * 74)
    print("证据 9：语音链路还通不通，首字双口径是否真的在算")
    print("=" * 74)
    sid_f = start_call()["session_id"]
    handle_turn(sid_f, audio_bytes=b"\x00" * 3200)
    asr_f = payload_of(sid_f, "asr_result")
    check("语音输入有 vad_result，占位识别标 fake=true",
          "vad_result" in event_types(sid_f) and bool(asr_f) and asr_f.get("fake") is True,
          ["实际事件链：%s" % " → ".join(event_types(sid_f)),
           "asr_result.payload = %s" % json.dumps(asr_f, ensure_ascii=False)])

    # 模拟前端上报：真实运行时由浏览器的 utterance.onstart 触发
    report_client_metric(sid_f, 1, 1234, estimate=False)
    m = events.metrics()
    check("首字口径由浏览器上报，与服务端处理耗时分开统计",
          m["first_audio_samples"] == 1 and m["first_audio_p50_ms"] == 1234.0
          and m["processing_p50_ms"] > 0,
          ["metrics = %s" % json.dumps({k: m[k] for k in
                                    ("turns", "first_audio_samples", "first_audio_p50_ms",
                                     "processing_p50_ms", "gate_ms", "est_cost_cny")},
                                   ensure_ascii=False)])
    end_call(sid_f)

    print()
    print("=" * 74)
    print("证据 10：会话 token 与并发保护")
    print("=" * 74)
    started = start_call()
    sid_g, token = started["session_id"], started["read_token"]
    _, err_wrong, code_wrong = _auth(sid_g, "wrong-token")
    sess_ok, err_ok, _ = _auth(sid_g, token)
    check("错 token 被拒，正确 token 通过",
          code_wrong == 403 and err_ok is None and sess_ok is not None,
          ["错 token → %s" % json.dumps(err_wrong, ensure_ascii=False),
           "正确 token → 通过，会话存在 = %s" % (sess_ok is not None)])
    first = _acquire(sess_ok)
    second = _acquire(sess_ok)
    _release(sess_ok)
    check("同一会话并发第二轮被挡（409）", first and not second,
          ["第一次占住 = %s，第二次占住 = %s" % (first, second)])
    end_call(sid_g)

    print()
    print("=" * 74)
    print("证据 11：容量闸门到点会收尾，而不是一直说下去")
    print("=" * 74)
    orig_budget = config_loader.budget_config
    config_loader.budget_config = lambda: {"max_turns": 1}
    try:
        sid_h = start_call()["session_id"]
        handle_turn(sid_h, user_text="是我，8866")
        limited = handle_turn(sid_h, user_text="用着还行")
        codes = [p.get("code") for p in payloads(sid_h, "error")]
        check("超过轮次上限 → 写 error 事件并礼貌收尾",
              limited["next_stage"] == "DONE" and "BUDGET_MAX_TURNS" in codes,
              ["error 事件 code = %s" % codes,
               "实际回复：%s" % limited["reply_text"]])
        end_call(sid_h)
    finally:
        config_loader.budget_config = orig_budget

    print()
    print("=" * 74)
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("结果：%d/%d 通过" % (passed, total))
    if passed != total:
        print("有断言失败，说明代码与预期不符 —— 以这里为准，不要信文档。")
        return 1
    print("全部通过。这只证明上面这些行为是真的，不代表整个系统都完成了：")
    print("  仍然是占位的：真 ASR/TTS、流式与打断、语义级合规、模拟用户跑批。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
