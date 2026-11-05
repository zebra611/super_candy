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
import app as app_mod  # noqa: E402
from app import (_acquire, _audio_state, _auth, _has_interrupt_content,  # noqa: E402
                 _release, end_call, handle_turn, ingest_audio_chunk,
                 probe_speech, record_barge_in,
                 report_client_metric, start_call)
from modules import dialogue, eval as eval_mod, kb, nlg, tools, voice  # noqa: E402

# 自检默认不真的调云端合成：既慢又要花钱，还会让"没有密钥的队友"跑不过。
# TTS 的真实请求构造与失败回落，下面用本地假接口单独测（见证据 9 之后那段）。
_orig_tts_config = config_loader.tts_config
config_loader.tts_config = lambda: {
    "provider": "browser", "voice": "zh-CN-XiaoxiaoNeural", "rate": 1.05}

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

    # 拼错事件名曾经能静默写入：事件在库里，但质检和面板都看不见它，
    # 整条链缺一环却没人报错。现在写入前就拦。
    try:
        events.write_event("s_illegal", "turn_ended", {"oops": True})
        blocked = False
    except ValueError as e:
        blocked = True
        err_msg = str(e)[:80]
    check("写未登记的事件类型会直接抛错，而不是静默入库",
          blocked,
          ["尝试写入 'turn_ended'（正确名是 turn_end）",
           "结果：%s" % ("被拦截 → " + err_msg if blocked else "竟然写进去了")])

    print()
    print("=" * 74)
    print("证据 3：开场白先出声，文字输入这一轮写了什么，尾号有没有被打码")
    print("=" * 74)
    _started = start_call()
    sid_a = _started["session_id"]
    # 开场白必须把音频一起返回。曾经这里只把 voice.tts() 的返回值写进事件就丢了：
    # 服务端白花一秒多合成一段，前端拿不到，用户听到的第一句仍是浏览器机械音，
    # 而后面的句子是真声音——两种嗓音交替，比全程机械音更难解释。
    check("开场白连同音频一起返回（不是只写进事件）",
          bool(_started.get("opening"))
          and isinstance(_started.get("opening_tts"), dict)
          and _started["opening_tts"].get("text") == _started["opening"],
          ["opening = %r" % _started.get("opening"),
           "opening_tts = %s" % json.dumps(_started.get("opening_tts"), ensure_ascii=False)])
    _open_ev = [e for e in events.list_events(sid_a)
                if e["type"] == "agent_reply" and e["turn_id"] == 0]
    check("开场白作为第 0 轮落在事件链里",
          len(_open_ev) == 1 and _open_ev[0]["payload"].get("source") == "opening",
          ["turn 0 的 agent_reply 条数 = %d" % len(_open_ev),
           "source = %s" % (_open_ev[0]["payload"].get("source") if _open_ev else "无"),
           "事件链：%s" % " → ".join(event_types(sid_a))])
    out = handle_turn(sid_a, user_text="是我，尾号8866")
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
            hits["n"] += 1
            time.sleep(2)                    # 比注入的时限长，必定触发超时
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"x"}}]}')

        def log_message(self, *a):
            pass

    hits = {"n": 0}
    # 用 Threading 版：重试的第二个请求不该排在第一个后面
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowASR)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    orig_cfg, orig_secret = config_loader.asr_config, config_loader.secret
    orig_plan = voice._ASR_TIMEOUT_PLAN
    orig_env = os.environ.get("DASHSCOPE_API_KEY")
    # 队友机器上没配密钥也要能跑这条：请求打到本地假接口，key 不被校验
    os.environ["DASHSCOPE_API_KEY"] = "test-key-not-used"
    # 注入更短的时限，否则测一次闸门要真等 5 秒
    voice._ASR_TIMEOUT_PLAN = ((0.6, 0.6), (0.6, 0.6))
    config_loader.asr_config = lambda: {
        "provider": "dashscope", "model": "m",
        "silence_rms": 0.0,               # 关掉静音闸门，保证真的发出请求
        "endpoint": "http://127.0.0.1:%d/asr" % srv.server_address[1]}
    try:
        wave_pcm = b"".join(struct.pack("<h", int(8000 * math.sin(i / 8.0)))
                            for i in range(8000))        # 0.5 秒非静音
        t_slow = time.time()
        out_slow = voice.asr(wave_pcm)
        cost_slow = (time.time() - t_slow) * 1000
        check("识别超时由墙钟闸门截断，且超时会重试一次",
              "超时" in str(out_slow.get("error"))
              and cost_slow < 2000 and hits["n"] == 2,
              ["假接口要 2 秒，注入时限 0.6 秒 → 实际耗时 %.0f ms" % cost_slow,
               "请求次数 = %d（期望 2：首轮超时 + 重试）" % hits["n"],
               "error = %s" % out_slow.get("error"),
               "失败时 text 为空且 fake=True（不会被当成识别成功）：%r / %s"
               % (out_slow.get("text"), out_slow.get("fake"))])
    finally:
        config_loader.asr_config, config_loader.secret = orig_cfg, orig_secret
        voice._ASR_TIMEOUT_PLAN = orig_plan
        if orig_env is None:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        else:
            os.environ["DASHSCOPE_API_KEY"] = orig_env
        srv.shutdown()
        srv.server_close()

    # ---- TTS：真实请求构造 + 合成失败必须回落，不能静音 ----
    seen_tts = []

    # 先测切句（纯逻辑，不联网）。切分的意义是让第一段短到几百毫秒就能出声，
    # 所以这里盯的是"第一段有多短"，不是"分了几段"。
    _short = "好的。"
    _long = "实在抱歉，这边暂时核对不上，我稍后让专员联系您，感谢您的接听。"
    p_short, p_long = voice._split_for_tts(_short), voice._split_for_tts(_long)
    check("短句不切，长句切开头且首段足够短",
          p_short == [_short] and len(p_long) >= 2 and len(p_long[0]) <= 6
          and "".join(p_long) == _long,
          ["%r → %s" % (_short, p_short),
           "%d 字 → %s（首段 %d 字）" % (len(_long), p_long, len(p_long[0])),
           "切完拼回原句是否一致：%s" % ("".join(p_long) == _long)])

    class _FakeTTS(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen_tts.append(json.loads(self.rfile.read(n).decode("utf-8") or "{}"))
            if "fail" in self.path:
                body = b'{"message":"boom"}'
                self.send_response(500)
            else:
                body = json.dumps({"output": {"audio": {
                    "url": "https://example.com/a.mp3"}}}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv_tts = http.server.HTTPServer(("127.0.0.1", 0), _FakeTTS)
    threading.Thread(target=srv_tts.serve_forever, daemon=True).start()
    tts_base = "http://127.0.0.1:%d" % srv_tts.server_address[1]
    os.environ["DASHSCOPE_API_KEY"] = "test-key-not-used"
    orig_tts_key_env = os.environ.get("DASHSCOPE_API_KEY")

    def _tts_cfg(endpoint):
        return {"provider": "dashscope", "model": "qwen3-tts-flash",
                "endpoint": endpoint, "voice_name": "Cherry",
                "language_type": "Chinese", "timeout_sec": 3,
                "voice": "zh-CN-XiaoxiaoNeural", "rate": 1.05}

    try:
        config_loader.tts_config = lambda: _tts_cfg(tts_base + "/ok")
        out_tts = voice.tts("您好，我是京东客服中心的小采。")
        got = seen_tts[-1] if seen_tts else {}
        check("TTS 走云端时返回可播放链接，音色与文本都带对了",
              out_tts.get("mode") == "url"
              and out_tts.get("audio_url") == "https://example.com/a.mp3"
              and (got.get("input") or {}).get("voice") == "Cherry"
              and (got.get("input") or {}).get("text", "").startswith("您好"),
              ["mode = %s" % out_tts.get("mode"),
               "audio_url = %s" % out_tts.get("audio_url"),
               "请求体 input = %s" % json.dumps(got.get("input"), ensure_ascii=False),
               "服务端合成耗时 %s ms" % out_tts.get("latency_ms")])

        config_loader.tts_config = lambda: _tts_cfg(tts_base + "/fail")
        out_fb = voice.tts("测试一句")
        check("合成失败时回落到浏览器播报，而不是静音",
              out_fb.get("mode") == "browser"
              and out_fb.get("fallback_from") == "dashscope"
              and out_fb.get("text") == "测试一句" and out_fb.get("error"),
              ["mode = %s（回落自 %s）" % (out_fb.get("mode"), out_fb.get("fallback_from")),
               "保留文本可供浏览器念：%r" % out_fb.get("text"),
               "error = %s" % out_fb.get("error")])

        # 这条守着一个真实踩过的坑：对端收下请求却不给响应就断开时，
        # 抛的是 http.client.RemoteDisconnected——既不是 HTTPError 也不是
        # URLError。当初只捕获了后两者，导致"重试"形同虚设，断连直接回落成
        # 浏览器机械音。这里让第一次必然断连、第二次正常返回，验证重试真的生效。
        drops = {"n": 0}

        class _DropThenOK(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                drops["n"] += 1
                if drops["n"] == 1:
                    self.close_connection = True
                    try:
                        self.connection.close()     # 不回任何响应，直接断
                    except Exception:
                        pass
                    return
                body = json.dumps({"output": {"audio": {
                    "url": "https://example.com/retry.mp3"}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv_drop = http.server.HTTPServer(("127.0.0.1", 0), _DropThenOK)
        threading.Thread(target=srv_drop.serve_forever, daemon=True).start()
        try:
            config_loader.tts_config = lambda: _tts_cfg(
                "http://127.0.0.1:%d/drop" % srv_drop.server_address[1])
            out_retry = voice.tts("重试这一句")
            check("连接被对端掐断时会重试，而不是直接回落成机械音",
                  out_retry.get("mode") == "url"
                  and out_retry.get("audio_url") == "https://example.com/retry.mp3"
                  and drops["n"] == 2,
                  ["实际请求次数 = %d（期望 2：首次断连 + 重试成功）" % drops["n"],
                   "mode = %s" % out_retry.get("mode"),
                   "audio_url = %s" % out_retry.get("audio_url"),
                   "若这里只请求了 1 次，说明异常没被捕获、重试没生效"])
        finally:
            srv_drop.shutdown()
            srv_drop.server_close()

        # 分段必须真的只等首段：3 段那种切法会触发接口 QPS 限流（实测 429），
        # 所以规则是"只切一刀"。这里验证首段先回来、剩余段后补。
        seg_hits = {"n": 0}

        class _SegTTS(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seg_hits["n"] += 1
                time.sleep(0.35)
                body = json.dumps({"output": {"audio": {
                    "url": "https://example.com/seg%d.mp3" % seg_hits["n"]}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv_seg = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SegTTS)
        threading.Thread(target=srv_seg.serve_forever, daemon=True).start()
        try:
            config_loader.tts_config = lambda: _tts_cfg(
                "http://127.0.0.1:%d/seg" % srv_seg.server_address[1])
            out_seg = voice.tts(
                "实在抱歉，这边暂时核对不上，我稍后让专员联系您，感谢您的接听。",
                wait_first_only=True)
            segs = out_seg.get("segments") or []
            rest = voice.collect_rest(out_seg.get("_rest"))
            check("长回复只等首段就返回，剩余段后台补齐",
                  out_seg.get("mode") == "segments" and len(segs) == 1
                  and out_seg.get("rest_count") == 1 and len(rest) == 1
                  and seg_hits["n"] == 2
                  and out_seg.get("latency_ms", 9999) < 600,
                  ["首段返回 %s ms（假接口每段 350ms）" % out_seg.get("latency_ms"),
                   "首段 1 段 + 剩余 %s 段，接口共请求 %d 次"
                   % (out_seg.get("rest_count"), seg_hits["n"]),
                   "首段文本 = %s" % [s["text"] for s in segs],
                   "剩余文本 = %s" % [s["text"] for s in rest]])
        finally:
            srv_seg.shutdown()
            srv_seg.server_close()
    finally:
        config_loader.tts_config = _orig_tts_config
        if orig_tts_key_env is None:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        srv_tts.shutdown()
        srv_tts.server_close()

    print()
    print("=" * 74)
    print("证据 9b：打断要闭嘴、要记账、还要能接着聊")
    print("=" * 74)
    sid_x = start_call()["session_id"]
    r_x = handle_turn(sid_x, user_text="是我，尾号8866")
    tid_x = r_x["turn_id"]

    # 假装这一轮还有没播完的音频挂在那儿（真实运行时由 wait_first_only 留下）
    sess_x = app_mod._SESSIONS[sid_x]
    sess_x.setdefault("tts_rest", {})[tid_x] = ([], [None, "u2"], ["头段", "尾段"])
    before = bool(sess_x["tts_rest"].get(tid_x))
    res_b = record_barge_in(sid_x, tid_x, "user_speech", "等一下", 712)
    after = bool(sess_x["tts_rest"].get(tid_x))
    ev_b = [e for e in events.list_events(sid_x) if e["type"] == "barge_in"]
    heard = str(ev_b[-1].get("payload")) if ev_b else ""
    barge_lat = ev_b[-1].get("latency_ms") if ev_b else None
    check("打断：写 barge_in 事件，并丢掉这一轮没播完的音频",
          "barge_in" in event_types(sid_x) and before and not after
          and res_b.get("dropped_pending") is True,
          ["barge_in 在事件链里：%s" % ("barge_in" in event_types(sid_x)),
           "该轮待播音频：打断前 %s → 打断后 %s" % (before, after),
           "返回 dropped_pending = %s" % res_b.get("dropped_pending"),
           "事件链：%s" % " → ".join(event_types(sid_x))])
    check("打断事件里记下「为什么打断」（用户到底说了什么）",
          "等一下" in heard and "heard" in heard,
          ["barge_in.payload = %s" % heard[:160],
           "留存这句话才能复盘：用户是喊停、否定，还是噪声误判"])
    check("打断响应耗时落库（验收指标要取这个数）",
          barge_lat == 712,
          ["事件行 latency_ms = %s（前端报 712）" % barge_lat,
           "验收写「打断 ≤300ms 停声」，但那指的是感知延迟（音量压低），",
           "真正 stop 要等用户说完才能认出文字，约 0.7 秒——两个口径别混报"])

    # 打断不能把会话弄坏：接着还能正常来一轮
    r_x2 = handle_turn(sid_x, user_text="我姓张")
    check("打断之后仍能正常进入下一轮",
          bool(r_x2.get("reply_text")) and r_x2["turn_id"] > tid_x,
          ["上一轮 turn_id=%s，这一轮 turn_id=%s" % (tid_x, r_x2.get("turn_id")),
           "回复：%s" % str(r_x2.get("reply_text"))[:50]])
    end_call(sid_x)

    print()
    print("=" * 74)
    print("证据 9c：打断要先确认是「人话」，不能咳嗽一声就停")
    print("=" * 74)
    print("为什么单独测这条：原来的实现是能量一超阈值就停播，60ms 就够。")
    print("咳嗽、碰麦、椅子响都能把客服打断，而且打断了不可逆——")
    print("那半句话用户再也听不到，得重听一遍。")

    # 判定规则是纯函数，逐条列表验证，不依赖网络
    cases = [
        ("", False, "纯噪声，一个字都没识别出"),
        ("。，！", False, "只有标点"),
        ("嗯。", False, "随口应一声"),
        ("嗯嗯啊", False, "全是语气词"),
        ("啊", False, "单个语气词"),
        ("我", False, "单个字，不是打断意图"),
        ("停", True, "单个字但明确要求停"),
        ("不是", True, "两个字，有实义"),
        ("等一下", True, "最典型的插话"),
        ("喂你好", True, "打招呼式插话"),
        ("我我我", True, "结巴，但确实在说话"),
        # 以下三条来自真云识别的实测发现：ASR 遇到噪声会吐高频短语
        ("Thank you.", False, "白噪声实测识别成了这句，不能算插话"),
        ("うん。", False, "「嗯」被识别成日文假名"),
        ("OK", False, "纯英文：中文外呼场景下多半是幻觉"),
        ("好的OK", True, "中英混合，只要有汉字且有实义就算"),
    ]
    rows_c = []
    wrong = []
    for t, exp, why in cases:
        got = _has_interrupt_content(t)
        rows_c.append("  %-10r → %-5s （期望 %-5s）%s"
                      % (t, got, exp, why))
        if got != exp:
            wrong.append((t, exp, got))
    check("插话判定：语气词不算，实义文字才算",
          not wrong,
          rows_c + (["不符预期：%s" % wrong] if wrong else []))

    # probe 接口本身：走本地假识别服务，确认它把 text 翻成 has_content
    class _FixedASR(http.server.BaseHTTPRequestHandler):
        spoken = "等一下"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"choices": [{"message": {
                "content": _FixedASR.spoken}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv_p = http.server.HTTPServer(("127.0.0.1", 0), _FixedASR)
    threading.Thread(target=srv_p.serve_forever, daemon=True).start()
    orig_cfg_p, orig_sec_p = config_loader.asr_config, config_loader.secret
    orig_env_p = os.environ.get("DASHSCOPE_API_KEY")
    os.environ["DASHSCOPE_API_KEY"] = "test-key-not-used"   # 队友机器上也要能跑
    config_loader.asr_config = lambda: {
        "provider": "dashscope", "model": "m", "timeout_sec": 3,
        "silence_rms": 0.0,          # 关掉静音闸门，保证真的发出请求
        "endpoint": "http://127.0.0.1:%d/asr" % srv_p.server_address[1]}
    try:
        pcm_speech = b"".join(struct.pack("<h", int(9000 * math.sin(i / 6.0)))
                              for i in range(8000))          # 0.5 秒非静音

        _FixedASR.spoken = "等一下"
        out_yes = probe_speech(sid_x, pcm_speech)
        check("探针：识别出「等一下」→ 判定该打断",
              out_yes.get("text") == "等一下" and out_yes.get("has_content") is True,
              ["识别文本 = %r" % out_yes.get("text"),
               "has_content = %s" % out_yes.get("has_content"),
               "耗时 = %s ms" % out_yes.get("asr_ms")])

        _FixedASR.spoken = "嗯。"
        out_no = probe_speech(sid_x, pcm_speech)
        check("探针：只识别出「嗯」→ 判定不该打断（客服继续说）",
              out_no.get("text") == "嗯。" and out_no.get("has_content") is False,
              ["识别文本 = %r" % out_no.get("text"),
               "has_content = %s（False 才对，否则清嗓子就能打断客服）"
               % out_no.get("has_content")])

        out_empty = probe_speech(sid_x, b"")
        check("探针：没传音频时不报错，直接判不打断",
              out_empty.get("has_content") is False
              and out_empty.get("reason") == "empty_audio",
              ["has_content = %s，reason = %s"
               % (out_empty.get("has_content"), out_empty.get("reason"))])

        # 探针不能污染业务：它只识别，不该推进状态机、不该多出轮次
        n_before = len(event_types(sid_x))
        probe_speech(sid_x, pcm_speech)
        check("探针不写任何业务事件（不能推进状态机）",
              len(event_types(sid_x)) == n_before,
              ["探针前后事件条数：%d → %d" % (n_before, len(event_types(sid_x)))])
    finally:
        config_loader.asr_config, config_loader.secret = orig_cfg_p, orig_sec_p
        if orig_env_p is None:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        else:
            os.environ["DASHSCOPE_API_KEY"] = orig_env_p
        srv_p.shutdown()
        srv_p.server_close()

    print()
    print("=" * 74)
    print("证据 9d：话术改写的接缝（任务2 的落点）")
    print("=" * 74)
    print("契约见 docs/17 第 3 节。这里只守两件事：默认关着、出事事不抛异常。")
    print("为什么守这两条：改写层在外呼主链路上，它一抛异常整通电话就断；")
    print("而默认关着是为了队友 clone 下来没密钥也能跑、跑批也不会调 30 次模型。")

    base = "为了核对身份，请说一下手机号后四位。"
    out_default = nlg.say({}, base, "IDENTITY")
    check("改写默认关闭：原样返回骨架话术，行为与接入前一致",
          out_default == base,
          ["provider = %s" % config_loader.nlg_config().get("provider"),
           "输入 %r" % base,
           "输出 %r（必须一字不差）" % out_default])

    orig_nlg = config_loader.nlg_config
    config_loader.nlg_config = lambda: {"provider": "dashscope", "timeout_ms": 400}
    try:
        out_open = nlg.say({}, base, "IDENTITY")
    finally:
        config_loader.nlg_config = orig_nlg
    check("改写开启但实现未完成时退回骨架，不抛异常（降级不能断掉通话）",
          out_open == base,
          ["输出 %r" % out_open,
           "此刻 _rewrite() 还是空实现，返回 None → 上层退回骨架，这是预期行为"])

    check("空话术不炸（沉默/异常路径会走到）",
          nlg.say({}, "", "GREETING") == "" and nlg.say({}, None, "GREETING") == "",
          ["空串 → %r，None → %r"
           % (nlg.say({}, "", "GREETING"), nlg.say({}, None, "GREETING"))])

    # 兜底与挂断必须豁免改写。用"总是改写"的假函数来验，而不是读源码找字符串——
    # 读源码只能证明"有个 if"，证明不了"这个 if 真的起了作用"。
    orig_say = nlg.say
    nlg.say = lambda s, t, st="": "【被改写了】" + (t or "")
    try:
        s_fb = dialogue.new_session("s_fallback_probe")
        out_fb = dialogue.safe_fallback(s_fb)                 # intent=error
        out_end = dialogue.forced_end(
            dialogue.new_session("s_forced_probe"), "max_turns")
        out_norm = dialogue.respond(s_fb, "是我，尾号8866")     # 正常流程
    finally:
        nlg.say = orig_say
    check("兜底与挂断话术不走改写，正常流程走改写",
          "被改写了" not in out_fb["reply_text"]
          and "被改写了" not in out_end["reply_text"]
          and "被改写了" in out_norm["reply_text"],
          ["编排出错时：%r" % out_fb["reply_text"][:40],
           "强制收尾时：%r" % out_end["reply_text"][:40],
           "正常一轮：%r（这条应该被改写）" % out_norm["reply_text"][:40],
           "为什么：出错时要的是确定和简短，挂断话术不能被换成别的说法"])

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
    # 注意 expect_seq **不该**归零：连续通话时采集常开，seq 全局单调递增，
    # 一归零下一轮的每一片都会被判成"序号倒退"。要清的是音频内容，不是序号基准。
    check("一轮结束后缓冲被清空，但帧序号基准保留（连续采集不能倒回 0）",
          st_after["samples"] == 0 and st_after["frames"] == 0
          and len(st_after["buf"]) == 0 and st_after["expect_seq"] > 0,
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

    # ---- 证据 12：平台侧（M5/M8 合并进来之后必须守住的三条边界）----
    print()
    print("=" * 74)
    print("证据 13：运维接口准入、对外不吐绝对路径、归档搬丢不删数据")
    print("=" * 74)

    class _FakeReq:
        """只带 _is_loopback 用到的两个属性，够用就行。"""

        def __init__(self, headers=None, addr=("127.0.0.1", 0)):
            self.headers = headers or {}
            self.client_address = addr

    os.environ.pop("PORT", None)
    cases = [
        ("本机直连", _FakeReq({"Host": "127.0.0.1:8000"}), True),
        ("Host 是域名（隧道 / DNS 重绑定）", _FakeReq({"Host": "evil.example.com"}), False),
        ("带反代转发头", _FakeReq({"Host": "localhost", "X-Forwarded-For": "1.2.3.4"}), False),
        ("带 X-Real-IP", _FakeReq({"Host": "localhost", "X-Real-IP": "1.2.3.4"}), False),
        ("源地址不是本机", _FakeReq({"Host": "localhost"}, addr=("10.0.0.5", 0)), False),
    ]
    got = [(name, app_mod.Handler._is_loopback(req), exp) for name, req, exp in cases]
    os.environ["PORT"] = "8000"          # 托管环境：平台会注入 PORT
    hosted = app_mod.Handler._is_loopback(_FakeReq({"Host": "127.0.0.1:8000"}))
    os.environ.pop("PORT", None)
    check("运维接口不能只看源地址——线上反代下源地址就是 127.0.0.1",
          all(g == e for _, g, e in got) and hosted is False,
          ["%s → %s（期望 %s）" % (n, g, e) for n, g, e in got]
          + ["托管环境（PORT 存在）→ %s（期望 False）" % hosted,
             "只看源地址的话，线上 /api/sessions 不带凭证就能拿到会话列表"])

    scenes = config_loader.available_scenes()
    leaked = [x.get("path") for x in scenes if os.path.isabs(x.get("path") or "")]
    desc_p = config_loader.describe().get("path") or ""
    db_p = events.db_stats().get("db_path") or ""
    check("对外接口不吐服务器绝对路径（/api/scenes、describe、看板的库信息）",
          not leaked and not os.path.isabs(desc_p) and not os.path.isabs(db_p),
          ["场景清单里的绝对路径：%s" % (leaked or "无"),
           "describe().path = %s" % desc_p,
           "db_stats().db_path = %s" % db_p,
           "绝对路径会暴露开发机目录结构，对外只给相对路径或文件名"])

    sid_k = start_call()["session_id"]
    n_active = app_mod.active_session_count()
    end_call(sid_k)
    n_after = app_mod.active_session_count()
    check("能数出还在进行的通话数（切场景前要告诉调用方影响多少通）",
          n_active >= 1 and n_after == n_active - 1,
          ["开一通后 active=%d，挂断后 active=%d" % (n_active, n_after),
           "切场景会立刻改变进行中通话的下一句，不提示就会当场串台"])

    # 归档：搬出去的行必须真的在归档库里，而且失败时一行都不许从主库删
    conn = events._connect()
    old_ts = events.now_ms() - 40 * 86400 * 1000
    conn.execute("INSERT INTO events (session_id, turn_id, ts, type, payload, latency_ms, version)"
                 " VALUES (?,?,?,?,?,?,?)",
                 ("s_archive_probe", 0, old_ts, "turn_start", "{}", 0, 1))
    conn.commit()
    before_main = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    moved = events.archive_events(before_days=30)
    after_main = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    left = conn.execute("SELECT COUNT(*) FROM events WHERE session_id = 's_archive_probe'"
                        ).fetchone()[0]
    again = events.archive_events(before_days=30)
    check("归档是「搬走」不是「删掉」：校验通过才删，重复跑不撞主键",
          moved.get("moved", 0) >= 1 and left == 0
          and after_main == before_main - moved["moved"]
          and again.get("dry_run") is True,
          ["本次搬走 %d 行，主库 %d → %d" % (moved.get("moved", 0), before_main, after_main),
           "搬走的那条在主库里还剩 %d 条（期望 0）" % left,
           "再跑一次：moved=%s dry_run=%s（没有可搬的就该什么都不做）"
           % (again.get("moved"), again.get("dry_run"))])

    # ---- 证据 14：S2 WebSocket 双向通道（协议可离线验证的部分）----
    print()
    print("=" * 74)
    print("证据 14：S2 WebSocket——握手锚点、帧编解码、通话协议、流式识别接管")
    print("=" * 74)

    import io
    import modules.wsproto as wsproto            # noqa: E402
    import ws_channel                            # noqa: E402

    # 1) 握手应答键用 RFC 6455 官方示例做锚点：算法抄错的话浏览器直接拒连，
    #    但"拒连"会发生在别人的浏览器里，离线锚点让它在自检里就暴露。
    _k = wsproto.accept_key("dGhlIHNhbXBsZSBub25jZQ==")
    check("WS 握手应答键与 RFC 6455 官方示例一致",
          _k == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
          ["accept_key('dGhlIHNhbXBsZSBub25jZQ==') = %s" % _k,
           "RFC 6455 §1.3 期望值：s3pPLMBiTxaQ9kYGzzhZRbK+xOo="])

    # 2) 帧编解码：服务端下发（无掩码）、客户端上行（必须带掩码去掩码）、
    #    分片拼装。这三个是 WebSocket 全部"惊险动作"。
    fin, op, payload = wsproto.Reader(io.BytesIO(
        wsproto.encode_frame(wsproto.OP_TEXT, "你好".encode("utf-8")))).read_frame()
    check("服务端下发帧可被解码（文本、无掩码）",
          fin and op == wsproto.OP_TEXT and payload == "你好".encode("utf-8"),
          ["opcode=%d payload=%r" % (op, payload.decode("utf-8"))])
    fin, op, payload = wsproto.Reader(io.BytesIO(
        wsproto.encode_frame(wsproto.OP_BIN, b"\x01\x02\x03", mask=True))).read_frame()
    check("客户端上行帧去掩码后逐字节还原（二进制、带掩码）",
          fin and op == wsproto.OP_BIN and payload == b"\x01\x02\x03",
          ["opcode=%d payload=%r" % (op, payload)])
    op, payload = wsproto.Reader(io.BytesIO(
        wsproto.encode_frame(wsproto.OP_TEXT, b"he", fin=False)
        + wsproto.encode_frame(wsproto.OP_CONT, b"llo", fin=True))).read_message()
    check("分片帧自动拼回完整消息",
          op == wsproto.OP_TEXT and payload == b"hello",
          ["拼回后 = %r" % payload])

    # 3) 通话协议：假后端 + 假发送器走完整一轮。真起服务器再真连 WS 的测法
    #    会在"没装浏览器"的环境里跑不动，协议层测试必须不依赖任何 socket。
    class _FakeBackend:
        def __init__(self):
            self.ingested, self.turns, self.barges = [], [], []
            self.rest_map = {}
            self.turn_no = 0

        def auth(self, sid, token):
            return {"sid": sid} if token == "good" else None

        def attach(self, sess, ws):
            self.attached = (sess, ws)

        def detach(self, sess):
            self.detached = True

        def ingest(self, sess, pcm):
            self.ingested.append(pcm)

        def handle_turn(self, sid, audio_streamed=True, asr_override=None):
            self.turn_no += 1
            self.turns.append({"sid": sid, "override": asr_override})
            return {"turn_id": self.turn_no, "user_text": "喂",
                    "reply_text": "您好", "stage": "GREETING",
                    "next_stage": "VERIFY", "intent": "x", "tool_result": None,
                    "closed": False, "latency": {"total_ms": 1.0},
                    "tts": {"mode": "segments", "text": "您好",
                            "segments": [{"text": "您好",
                                          "audio_url": "https://x/0.mp3"}]}}

        def take_rest(self, sess, turn_id):
            return self.rest_map.pop(turn_id, None)

        def barge(self, sid, heard, latency_ms=None):
            self.barges.append((sid, heard, latency_ms))
            return 7

        def realtime_enabled(self):
            return False

    sent = []
    fake = _FakeBackend()
    wsess = ws_channel.WsCallSession(fake, send_text=sent.append)

    def _sent_types():
        out = []
        for s in sent:
            try:
                out.append(json.loads(s).get("type"))
            except ValueError:
                out.append("<坏 JSON>")
        return out

    wsess.on_text(json.dumps({"type": "turn"}))
    check("未绑定会话就发 turn 被拒绝（NOT_STARTED）",
          _sent_types() == ["error"],
          ["下行消息：%s" % _sent_types()])
    wsess.on_text(json.dumps({"type": "start", "session_id": "s1", "token": "bad"}))
    check("token 不对不绑定会话（AUTH_FAILED）",
          wsess.sess is None and _sent_types()[-1] == "error",
          ["下行消息：%s" % _sent_types()])
    sent.clear()
    wsess.on_text(json.dumps({"type": "start", "session_id": "s1", "token": "good"}))
    wsess.on_text(json.dumps({"type": "nonsense"}))
    wsess.on_binary(b"\x00\x01")
    wsess.on_text(json.dumps({"type": "turn"}))
    # nonsense 那条 error 是故意的：未知类型必须被点名拒绝，而不是被静默吞掉
    check("正常一轮的推送顺序：ready → error(未知类型) → turn_result → turn_end → tts(首段) → tts_done",
          _sent_types() == ["ready", "error", "turn_result", "turn_end", "tts", "tts_done"],
          ["下行顺序：%s" % _sent_types(),
           "首段地址 = %s" % next((json.loads(s).get("audio_url")
                                   for s in sent
                                   if json.loads(s).get("type") == "tts"), None)])
    check("音频帧进了会话缓冲通道（binary → backend.ingest）",
          fake.ingested == [b"\x00\x01"],
          ["收到 %d 帧" % len(fake.ingested)])

    # 剩余段：句柄就位后，推送线程应把 seq=1 推下来并以 tts_done 收尾——
    # 这就是"服务端主动下发"替代前端轮询 /api/tts/rest 的证据。
    sent.clear()
    urls = [None, "https://x/1.mp3"]           # 预填，模拟后台已合成完
    fake.rest_map[fake.turn_no + 1] = ([threading.Thread(target=lambda: None)],
                                       urls, ["您好", "请问"])
    wsess.on_text(json.dumps({"type": "turn"}))
    _deadline = time.time() + 2.0
    while "tts_done" not in _sent_types() and time.time() < _deadline:
        time.sleep(0.02)
    seqs = [json.loads(s).get("seq") for s in sent
            if json.loads(s).get("type") == "tts"]
    check("剩余段由服务端主动推送（不再让前端轮询）",
          seqs == [0, 1] and _sent_types()[-1] == "tts_done",
          ["tts 段序号：%s" % seqs, "下行顺序：%s" % _sent_types()])

    sent.clear()
    wsess.on_text(json.dumps({"type": "barge_in", "heard": "等一下",
                              "latency_ms": 210}))
    check("打断走协议事件并回执（后端收到 heard 与打断耗时，回 barge_ack）",
          fake.barges == [("s1", "等一下", 210)] and _sent_types() == ["barge_ack"],
          ["后端收到：%s" % fake.barges, "下行：%s" % _sent_types()])

    # 4) 流式识别接管：asr_override 必须以真识别的语义落事件（fake=False、
    #    source=asr、provider 带 realtime 字样），并且真的推进了状态机。
    _st2 = start_call()
    sid_w = _st2["session_id"]
    _sess2, _err2, _ = _auth(sid_w, _st2["read_token"])
    ingest_audio_chunk(_sess2, 0, b"\x00\x00" * 160)      # 10ms 静音占位
    _out2 = handle_turn(sid_w, audio_streamed=True, asr_override={
        "text": "是我，尾号8866", "provider": "paraformer-realtime",
        "latency_ms": 12.0})
    _asr2 = payload_of(sid_w, "asr_result")
    check("流式识别结果被接管：不再整段重识别，事件语义不变",
          bool(_asr2) and _asr2.get("provider") == "paraformer-realtime"
          and _asr2.get("source") == "asr" and _asr2.get("fake") is False,
          ["asr_result.payload = %s" % json.dumps(_asr2, ensure_ascii=False)])
    check("接管结果真的推进了对话（身份核验通过 → NEED）",
          _out2["next_stage"] == "NEED",
          ["next_stage = %s" % _out2["next_stage"],
           "入库文本 = %r（尾号应被打码）" % (_asr2 or {}).get("text")])
    end_call(sid_w)

    # ---- 证据 15：场景包与主场景的契约不许漂（P1/P2 回归）----
    # 起因：writeback_template 从扁平改成 required/optional、并新增 tools/knowledge 段时，
    # config/scenes/ 下两个包没人切过去跑，validate() 一声不吭；等演示现场切场景，
    # writeback_schema() 退化成"值表"，回写校验拿不到类型与枚举。
    print()
    print("=" * 74)
    print("证据 15：全场景契约一致；旧扁平写回、required 缺 default 都会被拦下")
    print("=" * 74)

    _rows = config_loader.validate_all()
    _bad = [(label, probs) for label, _path, probs in _rows if probs]
    check("主场景与 config/scenes/ 下每个场景包都通过校验",
          not _bad,
          ["校验了 %d 份：%s" % (len(_rows), ", ".join(l for l, _p, _x in _rows))]
          + ["%s: %s" % (l, "; ".join(ps[:2])) for l, ps in _bad])

    _cfg = json.loads(json.dumps(config_loader.load()))
    _flat = json.loads(json.dumps(_cfg))
    _flat["writeback_template"] = {"call_result": "connected", "need": "待填写"}
    _flat_probs = config_loader.validate(_flat)
    check("回写模板退回旧扁平写法会被拦下（不再静默退化）",
          any("旧扁平" in x for x in _flat_probs),
          ["validate() 报：%s" % (_flat_probs[0] if _flat_probs else "无（说明又静默了）")])

    _nodef = json.loads(json.dumps(_cfg))
    _nodef["writeback_template"]["required"]["need"].pop("default", None)
    _nodef_probs = config_loader.validate(_nodef)
    def _flat_keys(cfg):
        return set(config_loader._flatten_writeback(cfg.get("writeback_template") or {}).keys())
    _lost = sorted(_flat_keys(_cfg) - _flat_keys(_nodef))
    check("required 缺 default 会被拦下，且能指出摊平后会丢哪个字段",
          any("缺 default" in x for x in _nodef_probs) and "need" in _lost,
          ["validate() 报：%s" % next((x for x in _nodef_probs if "缺 default" in x), "无"),
           "摊平对比：缺 default 后少了 %s" % (_lost or "无")])

    # ---- 证据 16：任务2 七项能力的自测脚本 + 3 条直接断言 ----
    # 起因：七项能力（W1/W2/W3/W4/W5/W6/W7/T17）带来 7 个自测脚本、2500+ 行新代码，
    # 但 verify 里一条断言都没有——改坏 dialogue.py，verify 照样全绿。
    # 这里把 7 个脚本挂进总闸门，再补 3 条"最容易悄悄坏掉"的直接断言。
    print()
    print("=" * 74)
    print("证据 16：任务2 七项能力的自测脚本全部通过")
    print("=" * 74)

    for _suite in ("test_kb", "test_fallback", "test_intent", "test_slots",
                   "test_correction", "test_memory", "test_nlg"):
        _path = os.path.join(ROOT, "scripts", _suite + ".py")
        _run = subprocess.run([sys.executable, _path], cwd=ROOT,
                              capture_output=True, text=True, timeout=180)
        _out = ((_run.stdout or "") + (_run.stderr or "")).strip().splitlines()
        _last = _out[-1][:88] if _out else "(无输出)"
        check("%s.py 通过（失败时这个总闸门会红）" % _suite, _run.returncode == 0,
              ["退出码 %d；末行：%s" % (_run.returncode, _last)])

    # 三条直接断言：都是"看起来对、其实配置没生效"的典型，子测试里容易被绕过。
    _hit = kb.load().match("多久能上门")
    check("知识库短词抢占已被更长的词压住（问上门时长不会答成权益有效期）",
          (_hit or {}).get("id") == "KB-ONSITE-001",
          ["match('多久能上门') -> %s / 命中词 %s"
           % ((_hit or {}).get("id"), (_hit or {}).get("matched"))])

    _mem = dialogue.memory_brief({"memory": {
        "facts": {"need": "包装压坏了"},
        "recent": [{"user": "包装压坏了", "stage": "NEED", "intent": "need"}]}})
    check("记忆摘要能带出诉求（回溯与措辞层都依赖它）",
          "包装压坏了" in _mem, ["memory_brief -> %r" % _mem])

    _fb = dict((r, dialogue.fallback_for(r))
               for r in ("asr", "timeout", "business", "tool", "writeback"))
    check("五类兜底话术互不相同（按原因细分不是同一句话换个键名）",
          len(set(_fb.values())) == 5,
          ["%s -> %s" % (k, v[:24]) for k, v in _fb.items()])

    print()
    print("=" * 74)
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("结果：%d/%d 通过" % (passed, total))
    if passed != total:
        print("有断言失败，说明代码与预期不符 —— 以这里为准，不要信文档。")
        return 1
    print("全部通过。这只证明上面这些行为是真的，不代表整个系统都完成了：")
    print("  已经是真的：云端识别（qwen3-asr-flash）、云端合成（qwen3-tts-flash）、")
    print("            采集分帧上传、静音闸门、墙钟闸门、断连/429 重试、合成失败回落、")
    print("            长句切段只等首段、前端能量 VAD 自动提交、")
    print("            三段式打断（疑似压低音量→探针识别→认出文字才停）、LLM 改写接缝、")
    print("            S2 WebSocket 双向通道（音频帧上行 + TTS 分段主动下发 + barge_in 协议事件）、")
    print("            任务2 七项（知识库检索/兜底细分/置信度分级/槽位追问/改口/记忆/T17 指标）。")
    print("  仍然是占位的：语义级合规、模拟用户跑批、说话人分离（环境人声会被当用户）、")
    print("            nlg 改写默认关闭（provider=template；开启后受账号配额与\"期限表述\"规则影响）、")
    print("            流式识别默认关闭（asr.realtime=true 才启用，需真密钥验证连通）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
