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
import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time

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
from app import (_acquire, _audio_state, _auth, _release, end_call,  # noqa: E402
                 handle_turn, ingest_audio_chunk, report_client_metric, start_call)
from modules import dialogue, eval as eval_mod, tools, voice  # noqa: E402

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
    # 这一段刻意把 provider 钉成 placeholder：
    # 自检要能离线跑、不花钱、也不依赖有没有配密钥。
    # 真实云端识别见 scripts/test_asr_file.py（那个是人工主动跑的）。
    orig_provider = config_loader.asr_provider
    config_loader.asr_provider = lambda: "placeholder"
    try:
        sid_f = start_call()["session_id"]
        handle_turn(sid_f, audio_bytes=b"\x00" * 3200)
        asr_f = payload_of(sid_f, "asr_result")
        check("占位识别路径仍可用（fake=true，不冒充真结果）",
              "vad_result" in event_types(sid_f) and bool(asr_f)
              and asr_f.get("fake") is True and asr_f.get("provider") == "placeholder",
              ["实际事件链：%s" % " → ".join(event_types(sid_f)),
               "asr_result.payload = %s" % json.dumps(asr_f, ensure_ascii=False)])

        # 静音闸门：provider 是云端，但音频是静音 → 必须在发请求之前就短路。
        # 这条断言不联网：真要联网就会超时变慢，说明闸门没生效。
        config_loader.asr_provider = lambda: "dashscope"
        t_gate = time.time()
        silent_out = voice.asr(b"\x00\x00" * 16000)
        gate_ms = (time.time() - t_gate) * 1000
        check("静音在发请求之前就被挡掉（云端对静音会幻觉出一句）",
              silent_out.get("skipped") == "silent" and silent_out["text"] == ""
              and gate_ms < 200,
              ["voice.asr(1 秒静音) → %s" % json.dumps(
                  {k: silent_out.get(k) for k in ("text", "skipped", "rms", "latency_ms")},
                  ensure_ascii=False),
               "耗时 %.1fms（若接近超时说明真的发了请求）" % gate_ms])

        sid_s = start_call()["session_id"]
        handle_turn(sid_s, audio_streamed=False, audio_bytes=b"\x00\x00" * 16000)
        types_s = event_types(sid_s)
        check("静音轮按沉默处理（no_answer，不伪造识别结果）",
              "no_answer" in types_s and "asr_result" not in types_s,
              ["实际事件链：%s" % " → ".join(types_s),
               "no_answer.reason = %s" % (payload_of(sid_s, "no_answer") or {}).get("reason")])
        end_call(sid_s)
    finally:
        config_loader.asr_provider = orig_provider

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

    # 上报事件必须把轮次写进事件列。只写 payload 会让面板上每条都显示
    # "turn 0"，实测时根本对不上是哪一轮慢——这个 bug 真的发生过。
    rep = [e for e in events.list_events(sid_f) if e["type"] == "latency_report"]
    check("latency_report 的 turn_id 落在事件列上（不是只塞进 payload）",
          len(rep) == 1 and rep[0]["turn_id"] == 1
          and payload_of(sid_f, "latency_report").get("turn_id") == 1,
          ["事件列 turn_id = %s" % (rep[0]["turn_id"] if rep else "无此事件"),
           "payload.turn_id = %s" % (payload_of(sid_f, "latency_report") or {}).get("turn_id")])
    end_call(sid_f)

    # 真实观测到的问题：一轮 1.9 秒的音频（三轮里最短）让接口耗了 15.5 秒。
    # 这条断言用本地假接口故意拖慢，确认超时会**按配置的时限**触发，
    # 而不是让用户对着话筒干等。它是负向测试——正常路径不会走到这里。
    import http.server

    class _SlowASR(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            time.sleep(3)                    # 比 timeout 长，必定触发超时
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"x"}}]}')

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _SlowASR)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    orig_cfg, orig_secret = config_loader.asr_config, config_loader.secret
    orig_env = os.environ.get("DASHSCOPE_API_KEY")
    # 队友机器上没配密钥也要能跑这条：请求打到本地假接口，key 不被校验
    os.environ["DASHSCOPE_API_KEY"] = "test-key-not-used"
    config_loader.asr_config = lambda: {
        "provider": "dashscope", "model": "m", "timeout_sec": 1,
        "silence_rms": 0.0,               # 关掉静音闸门，保证真的发出请求
        "endpoint": "http://127.0.0.1:%d/asr" % srv.server_address[1]}
    try:
        wave_pcm = b"".join(struct.pack("<h", int(8000 * math.sin(i / 8.0)))
                            for i in range(8000))        # 0.5 秒非静音
        t_slow = time.time()
        out_slow = voice.asr(wave_pcm)
        cost_slow = (time.time() - t_slow) * 1000
        check("识别超时按配置时限触发，不让用户干等",
              "超时" in str(out_slow.get("error")) and cost_slow < 2500,
              ["假接口拖 3 秒，配置超时 1 秒 → 实际耗时 %.0f ms" % cost_slow,
               "error = %s" % out_slow.get("error"),
               "失败时 text 为空且 fake=True（不会被当成识别成功）：%r / %s"
               % (out_slow.get("text"), out_slow.get("fake"))])
    finally:
        config_loader.asr_config, config_loader.secret = orig_cfg, orig_secret
        if orig_env is None:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        else:
            os.environ["DASHSCOPE_API_KEY"] = orig_env
        srv.shutdown()
        srv.server_close()

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
    print("证据 12：S1 采集分帧——10 秒音频是否真的变成 160000 个采样（帧号不跳）")
    print("=" * 74)
    check("契约里的音频格式常数自洽（16k / 16bit / 单声道 / 20ms）",
          contracts.PCM_SAMPLE_RATE == 16000 and contracts.FRAME_SAMPLES == 320
          and contracts.FRAME_BYTES == 640,
          ["PCM_SAMPLE_RATE=%d, FRAME_MS=%d, FRAME_SAMPLES=%d, FRAME_BYTES=%d"
           % (contracts.PCM_SAMPLE_RATE, contracts.FRAME_MS,
              contracts.FRAME_SAMPLES, contracts.FRAME_BYTES)])

    started_i = start_call()
    sid_i = started_i["session_id"]
    sess_i, _, _ = _auth(sid_i, started_i["read_token"])
    frame = b"\x00\x00" * contracts.FRAME_SAMPLES       # 一帧静音，只验证流量与计数
    last_chunk = None
    for chunk in range(63):                              # 63 片 × 8 帧 = 504 帧 ≈ 10.08 秒
        last_chunk = ingest_audio_chunk(sess_i, chunk * 8, frame * 8)
    check("10 秒音频 → 采样数与帧数按契约换算准确",
          last_chunk["received_samples"] == 504 * 320 and last_chunk["received_frames"] == 504,
          ["发了 504 帧（每帧 320 采样）",
           "服务端统计：%d 采样 / %d 帧 / 缺口 %d"
           % (last_chunk["received_samples"], last_chunk["received_frames"], last_chunk["gaps"]),
           "换算时长：%.2f 秒" % (last_chunk["received_samples"] / 16000.0)])

    gap = ingest_audio_chunk(sess_i, 900, frame * 8)      # 故意跳号
    dup = ingest_audio_chunk(sess_i, 0, frame * 8)        # 重复旧片
    check("序号跳了会被记下（不掩盖丢帧），重复分片不重复追加",
          gap["gaps"] == 1 and dup["duplicates"] == 1
          and dup["received_samples"] == last_chunk["received_samples"] + 8 * 320,
          ["跳号后 gaps = %d" % gap["gaps"],
           "重复旧片后 duplicates = %d，采样数是否被重复累加：%s"
           % (dup["duplicates"], "否（正确）" if dup["received_samples"] == last_chunk["received_samples"] + 8 * 320 else "是（错误）")])

    try:
        ingest_audio_chunk(sess_i, 999, frame, sample_rate=8000)
        bad_rate = "没报错（错误）"
    except ValueError as e:
        bad_rate = "拒绝：" + str(e)
    check("采样率不符契约时直接拒绝（防止 8k/16k 串号变噪音）", bad_rate.startswith("拒绝"),
          [bad_rate])

    # 整轮：帧流进来 → 一轮结束 → 音频流统计与识别一起落事件
    # 这一段验的是"音频有没有送到识别层"，不是识别准不准；
    # 固定成占位实现才能离线跑、不花钱。真实识别见 scripts/test_asr_file.py。
    orig_provider_i = config_loader.asr_provider
    config_loader.asr_provider = lambda: "placeholder"
    try:
        handle_turn(sid_i, audio_streamed=True, audio_dropped=3)
    finally:
        config_loader.asr_provider = orig_provider_i
    stat = payload_of(sid_i, "audio_stream")
    vad_i = payload_of(sid_i, "vad_result")
    turn_start_i = payload_of(sid_i, "turn_start")
    check("一轮结束时写了 audio_stream 事件，且时长/丢帧都对得上",
          bool(stat) and stat["duration_ms"] > 9000 and stat["dropped_at_client"] == 3,
          ["audio_stream.payload = %s" % json.dumps(stat, ensure_ascii=False)])
    check("turn_start 记下了真实音频字节数（不再是 0）",
          turn_start_i["audio_bytes"] > 0 and turn_start_i["audio_streamed"] is True,
          ["turn_start.payload = %s" % json.dumps(turn_start_i, ensure_ascii=False)])
    check("VAD 用的是真采样数，不是字节数拍脑袋",
          vad_i["samples"] == stat["samples"] and vad_i["speech_ms"] == int(stat["duration_ms"]),
          ["vad_result.payload = %s" % json.dumps(vad_i, ensure_ascii=False)])
    check("这一轮的识别来源标 source=asr（音频确实进了识别）",
          (payload_of(sid_i, "asr_result") or {}).get("source") == "asr",
          ["asr_result.payload = %s"
           % json.dumps(payload_of(sid_i, "asr_result"), ensure_ascii=False)])

    st_after = _audio_state(sess_i)
    check("一轮结束后缓冲被清空（下一轮不会串上这一轮的音频）",
          st_after["samples"] == 0 and st_after["frames"] == 0 and st_after["expect_seq"] == 0,
          ["清空后状态：samples=%d, frames=%d, expect_seq=%d, buf=%d 字节"
           % (st_after["samples"], st_after["frames"], st_after["expect_seq"],
              len(st_after["buf"]))])

    # 空缓冲但前端声称有流：应走"沉默"，不能伪造一句识别结果
    sid_j = start_call()["session_id"]
    silence_j = handle_turn(sid_j, audio_streamed=True)
    types_j = event_types(sid_j)
    check("声称有流但一帧都没到 → 按沉默处理，不伪造识别结果",
          "no_answer" in types_j and "asr_result" not in types_j,
          ["实际事件链：%s" % " → ".join(types_j),
           "实际回复：%s" % silence_j["reply_text"]])
    end_call(sid_j)
    end_call(sid_i)

    print()
    print("=" * 74)
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("结果：%d/%d 通过" % (passed, total))
    if passed != total:
        print("有断言失败，说明代码与预期不符 —— 以这里为准，不要信文档。")
        return 1
    print("全部通过。这只证明上面这些行为是真的，不代表整个系统都完成了：")
    print("  仍然是占位的：真 ASR/TTS、真 VAD、打断、语义级合规、模拟用户跑批。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
