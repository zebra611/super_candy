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

import glob
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
from modules import attribution, user_sim, policy  # noqa: E402  L2：模拟用户与失败归因

# 自检默认不真的调云端合成：既慢又要花钱，还会让"没有密钥的队友"跑不过。
# TTS 的真实请求构造与失败回落，下面用本地假接口单独测（见证据 9 之后那段）。
_orig_tts_config = config_loader.tts_config
config_loader.tts_config = lambda: {
    "provider": "browser", "voice": "zh-CN-XiaoxiaoNeural", "rate": 1.05}

# nlg 同理必须强制关掉：自检断言的是确定性文本（must_say / 关键短语），
# 而 qwen-flash 改写会改措辞（实测把"同意或者不需要"改写成"同意或不需要"直接打红）。
# 演示机把 nlg.provider 打开是合法的本地选择，但不能让自检结果随它波动——
# 所以这里无条件压回 template，保证 verify 永远测的是规则层。见 docs/17 §3.4 的降级语义。
config_loader.nlg_config = lambda: {**config_loader.load().get("nlg", {}), "provider": "template"}

results = []
# "没被验证"的项单独记账：语料/密钥缺失时**不能**记成 PASS（那是假绿），
# 也不能让队友的 verify 直接红掉（他可能只是没跑生成器）。
# 所以列出来、不算进通过数，最后单独打印——看得见，就不算糊过去。
uncovered = []


def mark_uncovered(name, reason):
    uncovered.append((name, reason))
    print("  [未覆盖] %s —— %s" % (name, reason))

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

    # 证据 5~8 这四段是**售后回访场景**的用例（台词是"用着还行""同意"，
    # 禁语是"保证您百分百赚钱"，必说话术也是售后那套）。默认场景换成赛题
    # POP 资审之后，它们的台词/阶段/禁语表全对不上——之前能过是因为默认场景
    # 恰好就是售后占位场景，等于测试绑死在"默认值"上。
    # 现在显式切到 service_outbound 跑，跑完切回默认（赛题场景）。
    _prev_scene = config_loader.scene_path()
    config_loader.set_scene("service_outbound")

    print()
    print("=" * 74)
    print("证据 5：含糊回答会不会被当成拒绝（售后回访场景）")
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
    print("证据 7：禁语命中后被整句替换，而不是打码（售后回访场景）")
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
    print("证据 8：质检报告能不能判必说话术与分类违规（售后回访场景）")
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

    # 切回默认场景（赛题 POP 资审）：后面所有断言都按它来。
    config_loader.set_scene(_prev_scene)

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

        # 回声否决：外放时麦克风收到客服自己的播报，ASR 会原样识别出这句话。
        # 不单独判的话它会被 _has_interrupt_content 当成"用户在插话"→ 直接停播，
        # 用户看到的就是"客服说一半突然不说了"。真实外放录音里回声最高到 0.21 rms，
        # 谱特征拦不住（回声本来就是人声），只能在文字层比对。
        _agent_line = ("您好，我是京东商家资质审核的工作人员，您这边提交的营业执照"
                       "经营范围与店铺主营类目不一致，需要您补充说明一下")
        _FixedASR.spoken = "我是京东商家资质审核的工作人员您这边提交的营业执照"
        out_echo = probe_speech(sid_x, pcm_speech, agent_text=_agent_line)
        check("探针：识别结果与自己正在播报的文本重合 → 判成回声，不许停播",
              out_echo.get("has_content") is False
              and out_echo.get("reason") == "echo_self",
              ["识别文本 = %r" % out_echo.get("text"),
               "has_content = %s，reason = %s"
               % (out_echo.get("has_content"), out_echo.get("reason"))])

        _FixedASR.spoken = "等一下你先别说了"
        out_user = probe_speech(sid_x, pcm_speech, agent_text=_agent_line)
        check("探针：同一段播报期间的真人插话仍要放行（回声判定不能一刀切）",
              out_user.get("has_content") is True,
              ["识别文本 = %r，reason = %s"
               % (out_user.get("text"), out_user.get("reason"))])

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
    # **kw 是必须的：nlg.say 现在多带了 kb/qa/user_text/flags（给表达增强层用），
    # 桩函数不接受就会 TypeError。这里刻意用 **kw 而不是把参数列全——
    # 以后接缝再加参数，这条证据不该因为"忘了改桩"而红。
    nlg.say = lambda s, t, st="", **kw: "【被改写了】" + (t or "")
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
    check("VAD 的采样数取自真实字节，且静音不再被算成人声",
          vad_i["samples"] == stat["samples"] and vad_i["started"] is False
          and vad_i["speech_ms"] == 0,
          ["audio_stream.samples = %d" % stat["samples"],
           "vad_result.payload = %s" % json.dumps(vad_i, ensure_ascii=False),
           "这 10.2 秒全是静音帧：改造前 speech_ms=10240（有字节就算说了话），"
           "现在 0 —— Z6 换掉的就是这个假判定"])
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
    # 挑一个**当前场景真实存在**的 required 字段，而不是写死 "need"：
    # 默认场景已换成赛题 POP 资审，它的写回必填项与售后场景不同，
    # 写死字段名会让这条断言在换场景时直接 KeyError 崩掉整段自检。
    _req = (_nodef.get("writeback_template") or {}).get("required") or {}
    _pick = "need" if "need" in _req else (sorted(_req.keys())[0] if _req else None)
    if _pick:
        _req[_pick].pop("default", None)
    _nodef_probs = config_loader.validate(_nodef)
    def _flat_keys(cfg):
        return set(config_loader._flatten_writeback(cfg.get("writeback_template") or {}).keys())
    _lost = sorted(_flat_keys(_cfg) - _flat_keys(_nodef))
    check("required 缺 default 会被拦下，且能指出摊平后会丢哪个字段",
          bool(_pick) and any("缺 default" in x for x in _nodef_probs)
          and _pick in _lost,
          ["拿来做试验的字段：%r" % _pick,
           "validate() 报：%s" % next((x for x in _nodef_probs if "缺 default" in x), "无"),
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
                   "test_correction", "test_memory", "test_nlg", "test_tools",
                   "test_user_sim"):
        _path = os.path.join(ROOT, "scripts", _suite + ".py")
        _run = subprocess.run([sys.executable, _path], cwd=ROOT,
                              capture_output=True, text=True, timeout=180)
        _out = ((_run.stdout or "") + (_run.stderr or "")).strip().splitlines()
        _last = _out[-1][:88] if _out else "(无输出)"
        check("%s.py 通过（失败时这个总闸门会红）" % _suite, _run.returncode == 0,
              ["退出码 %d；末行：%s" % (_run.returncode, _last)])

    # 三条直接断言：都是"看起来对、其实配置没生效"的典型，子测试里容易被绕过。
    # 这一条查的是**售后知识库**（"多久能上门" vs "权益有效期"），要显式切到售后场景。
    _kb_prev = config_loader.scene_path()
    config_loader.set_scene("service_outbound")
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

    # ---- 证据 17：闭环率的分母只算“应闭环”的用例（吸收自 #15） ----
    # 起因：跑批曾把「闭环条数 / 全部 30 条」当闭环率报出去（37%）。分母里含 14 条
    # 本来就不该闭环的（拒绝/诱导/缺槽位），等于把“该拒绝的也拒绝了”算成失败。
    # 口径与 #15 一致，只是本仓库 run_eval 的输出格式是「应闭环 1/1 = 100%｜误闭环 0/1」，
    # 断言按实际格式写，避免为了对齐字符串去改实现。
    print()
    print("=" * 74)
    print("证据 17：闭环率的分母只算“应闭环”的用例")
    print("=" * 74)

    _ev = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "run_eval.py"), "N01", "R01"],
        cwd=ROOT, capture_output=True, text=True, timeout=180)
    _ev_txt = (_ev.stdout or "") + (_ev.stderr or "")
    check("跑批按“应闭环/误闭环”两句报（N01 该闭环、R01 该不闭环 → 应闭环 1/1、误闭环 0）",
          ("闭环率：应闭环 1/1" in _ev_txt) and ("误闭环 0" in _ev_txt),
          [l.strip() for l in _ev_txt.splitlines() if "闭环率" in l]
          or ["输出里没有闭环率行（改坏了？）"])

    _seed = json.load(open(os.path.join(ROOT, "eval", "seed_cases.json"),
                           encoding="utf-8"))
    _seed = _seed if isinstance(_seed, list) else (_seed.get("cases") or [])
    _kinds = set((c.get("expect") or {}).get("closed") for c in _seed)
    check("用例集里三态齐全：该闭环 / 不该闭环 / 不作要求（只剩一态，分母就没意义了）",
          _kinds == {True, False, None},
          ["expect.closed 取值：%s" % sorted(str(k) for k in _kinds),
           "共 %d 条用例" % len(_seed)])

    # ---- 证据 18：R3 评测工具链的自测脚本挂进总闸门 ----
    # 起因：test_adversarial.py 与 test_eval_suite.py 是本轮新增的 43 条测试，
    # 但不在 verify 里——改了 run_eval / eval_suite 照样全绿，等于测试没进质量体系。
    print()
    print("=" * 74)
    print("证据 18：R3 评测工具链自测脚本全部通过")
    print("=" * 74)

    for _suite in ("test_adversarial", "test_eval_suite", "test_semantic_compliance"):
        _path = os.path.join(ROOT, "scripts", _suite + ".py")
        _run = subprocess.run([sys.executable, _path], cwd=ROOT,
                              capture_output=True, text=True, timeout=180)
        _out = ((_run.stdout or "") + (_run.stderr or "")).strip().splitlines()
        _last = _out[-1][:88] if _out else "(无输出)"
        check("%s.py 通过（失败时这个总闸门会红）" % _suite, _run.returncode == 0,
              ["退出码 %d；末行：%s" % (_run.returncode, _last)])

    # ---- 证据 19：任务 2 七项（T2-T12）的离线自测挂进总闸门 ----
    #
    # 为什么必须挂进来：T2-T12 改的是 dialogue.py / nlg.py，一共 200 多行。
    # 如果只写各自的自测脚本、不进 verify，那么任何人改这两个文件，
    # 只要没人想起来单独跑一遍，总闸门照样全绿——"改坏了没人知道"。
    # 这里的做法和证据 18 一致：跑脚本看退出码，再用几条直接断言钉住关键行为
    # （脚本能跑绿但行为没生效，是更隐蔽的一种失败）。
    print()
    print("=" * 74)
    print("证据 19：任务 2 七项（T2-T12）的离线自测全部通过")
    print("=" * 74)

    for _suite in ("test_intent", "test_slots", "test_correction",
                   "test_memory", "test_nlg", "test_kb", "test_fallback"):
        _path = os.path.join(ROOT, "scripts", _suite + ".py")
        _run = subprocess.run([sys.executable, _path], cwd=ROOT,
                              capture_output=True, text=True, timeout=180)
        _out = ((_run.stdout or "") + (_run.stderr or "")).strip().splitlines()
        _last = _out[-1][:88] if _out else "(无输出)"
        check("%s.py 通过（失败时这个总闸门会红）" % _suite, _run.returncode == 0,
              ["退出码 %d；末行：%s" % (_run.returncode, _last)])

    # 直接断言：只跑脚本不够，脚本自己也可能被改弱。
    from modules import dialogue as _dlg
    from modules import intent as _intent
    from modules import nlg as _nlg

    _base_offer = "权益有效期是十二个月，从登记当天开始算。"
    check("T2 期限同义换算放行（十二个月 == 一年），不把合法换算当篡改",
          _nlg._facts_ok(_base_offer, "这份权益有效期是一年，从登记当天算起。"),
          ["_facts_ok(十二个月, 一年) = %r" % _nlg._facts_ok(_base_offer, "这份权益有效期是一年，从登记当天算起。")])
    check("T2 换个数仍然拦（十二个月 → 三年），放行的边界没有被放宽成随便改",
          not _nlg._facts_ok(_base_offer, "这份权益有效期是三年。"),
          ["_facts_ok(十二个月, 三年) = %r" % _nlg._facts_ok(_base_offer, "这份权益有效期是三年。")])

    _p = _nlg._user_prompt({"memory": {"recent": [{"user": "包装压坏了"}]},
                            "slots": {"last_user_text": "用着还行"}}, "再来一句", "OFFER")
    check("T3 「最近两轮对话」注入的是真实上文，不是占位符",
          "包装压坏了" in _p and "【最近两轮对话】（无）" not in _p,
          ["prompt 片段：" + [ln for ln in _p.splitlines() if "最近两轮" in ln][0]])

    check("T10 混合句「好的嗯」判成肯定（以前判 unknown，用户答应了还被再问一遍）",
          _dlg.classify("好的嗯")["yes"] and _dlg.classify("好的嗯")["conf"] >= _dlg.YES_MIN_CONF,
          ["yes=%r conf=%r" % (_dlg.classify("好的嗯")["yes"], _dlg.classify("好的嗯")["conf"])])
    check("T10 否定作用域：「不要了」仍判拒绝（不能被'后说的赢'误伤）",
          _dlg.classify("不要了")["no"] and not _dlg.classify("不要了")["yes"],
          ["no=%r yes=%r" % (_dlg.classify("不要了")["no"], _dlg.classify("不要了")["yes"])])

    _s = _dlg.new_session("verify-t8")
    _dlg.respond(_s, "是我，尾号8866")
    _dlg.respond(_s, "包装压坏了")
    _out8 = _dlg.respond(_s, "你们这是骗子服务")
    check("T8 带情绪时暂停推进（OFFER 不再问'您同意领取吗'）",
          _out8["next_stage"] == "OFFER" and "同意领取" not in _out8["reply_text"],
          ["next_stage=%s" % _out8["next_stage"], "回复：" + _out8["reply_text"][:40]])

    _s9 = _dlg.new_session("verify-t9")
    _dlg.respond(_s9, "是我，尾号8866")
    _dlg.respond(_s9, "包装压坏了")
    _dlg.respond(_s9, "等等我改一下其实是屏幕碎了")
    check("T9 阶段边界的改口落进 need 槽位",
          _s9["slots"].get("need") == "屏幕碎了",
          ["need=%r" % _s9["slots"].get("need")])

    _s12 = _dlg.new_session("verify-t12")
    _dlg.respond(_s12, "是我")
    _dlg.respond(_s12, "嗯")
    _out12 = _dlg.respond(_s12, "哦")
    check("T12 连续含糊有退出机制（end_reason=unclear，不是无限轮换问法）",
          _out12.get("end_reason") == "unclear",
          ["end_reason=%r" % _out12.get("end_reason"), "回复：" + _out12["reply_text"][:40]])

    check("T5 意图识别可外挂模型且默认不联网（provider=template 时逐字节不变）",
          _intent.classify("同意")["yes"] and _intent.classify("同意")["evidence"] != "model",
          ["默认路径 evidence=%r" % _intent.classify("同意")["evidence"]])

    # ---- 证据 20：pop 场景（C1）——场景词表接入输入侧 / 求保证拦截 / 京麦过滤 / 占位符两套体系 ----
    # 起因：C1 施工后 pop_reject 场景只在 8802 端口实测过，verify 里一条断言都没有——
    # 场景包 compliance 词表被改坏、kb no_call 标记丢失、占位符处理回归，总闸门照样全绿。
    # ①②③ 在 monkeypatch 场景配置下做进程内全链路；④⑤ 直接读 kb_pop 验契约。
    print()
    print("=" * 74)
    print("证据 20：pop 场景 C1——输入侧情绪/求保证拦截/京麦过滤/占位符两套体系")
    print("=" * 74)

    from modules import kb as _kbmod

    _pop_cfg20 = json.load(open(os.path.join(ROOT, "config", "scenes", "pop_reject.json"),
                                encoding="utf-8"))
    _orig_cache20 = config_loader._cache
    config_loader._cache = _pop_cfg20
    try:
        _f20 = _dlg.classify("你们就是故意卡我们商家，爱审不审！")
        check("pop insult 词表接入输入情绪分类（'爱审不审'→angry，以前只有输出侧用它）",
              _f20["angry"],
              ["flags.angry=%r（通用 ANGRY_WORDS 不含'爱审不审'，命中说明场景词表已合并）"
               % _f20["angry"]])

        _s20 = _dlg.new_session("verify-pop-promise")
        _dlg.respond(_s20, "我是负责人")
        _dlg.respond(_s20, "尾号8866")
        _dlg.respond(_s20, "我申请的品牌授权没过")
        _out20 = _dlg.respond(_s20, "你能不能保证我这次重新提交一定能通过？")
        check("求结果保证统一走 reply_blocked 口径（合规红线：不许承诺通过）",
              "不能给您保证" in _out20["reply_text"],
              ["回复：" + _out20["reply_text"][:48]])

        _s21 = _dlg.new_session("verify-pop-insult")
        _dlg.respond(_s21, "我是负责人")
        _dlg.respond(_s21, "尾号8866")
        _dlg.respond(_s21, "我申请的品牌授权没过")
        _out21 = _dlg.respond(_s21, "你们就是故意卡我们商家，爱审不审！")
        check("pop 情绪词命中时致歉承接且阶段不动（不推 CONFIRM）",
              _out21["next_stage"] == "OFFER" and "抱歉" in _out21["reply_text"],
              ["next_stage=%s 回复：%s" % (_out21["next_stage"], _out21["reply_text"][:40])])

        # 场景确认词：pop 问"清楚了吗"，商家答"清楚了"必须闭环（通用 YES 表
        # 没有"清楚了"，以前被判含糊追问两次然后记 unknown）。
        _s22 = _dlg.new_session("verify-pop-confirm")
        _dlg.respond(_s22, "我是负责人")
        _dlg.respond(_s22, "尾号8866")
        _dlg.respond(_s22, "我申请的品牌授权没过")
        _dlg.respond(_s22, "行，清楚了，我这就去重新提交")
        _out22 = _dlg.respond(_s22, "对，清楚了")
        check("pop 场景确认词「清楚了」命中即闭环回写（不再追问两次记 unknown）",
              bool(_out22.get("closed")) or _out22["next_stage"] in ("WRITEBACK", "DONE"),
              ["closed=%r next=%s intent=%s"
               % (_out22.get("closed"), _out22["next_stage"], _out22.get("intent"))])

        # 回写字段完备性（赛题要求）：need 文本必须落进五域分类、
        # merchant_understood 必须跟 CONFIRM 结果同步（以前永远 unknown）。
        _wbc = ((_out22.get("tool_call") or {}).get("args") or {}).get("content") or {}
        check("pop 回写带五域分类（品牌授权→品牌-授权）且 merchant_understood 同步为 true",
              _wbc.get("reject_category") == "品牌-授权"
              and _wbc.get("merchant_understood") is True
              and _wbc.get("reject_summary"),
              ["reject_category=%r merchant_understood=%r summary=%r"
               % (_wbc.get("reject_category"), _wbc.get("merchant_understood"),
                  (_wbc.get("reject_summary") or "")[:24])])

        # T17 三洞回归（11-06 队长真实语音 s_666ed5548f9c 全复现）：
        # ① 3 位尾号引导补全不轮换槽位 ② 英文噪声走含糊通道不烧核身次数
        # ③ 挂断前完整重问一次
        check("T17-① 3 位尾号引导「请说四位」且不轮换槽位（以前跳问别的）",
              _dlg.classify("111")["digits"] == ""
              and not _dlg._short_digit_run("一") and _dlg._short_digit_run("111"),
              ["digits=%r short_run('111')=%r short_run('一')=%r"
               % (_dlg.classify("111")["digits"],
                  _dlg._short_digit_run("111"), _dlg._short_digit_run("一"))])
        check("T17-② ASR 噪声（咳嗽→'Cough.'）走含糊通道，不消耗核身失败",
              _dlg.classify("Cough.")["vague"]
              and _dlg.classify("Cough.")["evidence"] == "noise",
              ["vague=%r evidence=%r"
               % (_dlg.classify("Cough.")["vague"],
                  _dlg.classify("Cough.")["evidence"])])
        _s23 = _dlg.new_session("verify-t17-reask")
        _dlg.respond(_s23, "是我")
        _dlg.respond(_s23, "张三丰")          # name 第 1 问，核不上
        _dlg.respond(_s23, "李逵")            # tail 第 2 问，核不上
        _dlg.respond(_s23, "王五")            # name 第 2 问，核不上
        _out23a = _dlg.respond(_s23, "赵六")   # 槽位问满 → 应完整重问而不是挂断
        _out23b = _dlg.respond(_s23, "钱七")   # 重问后仍核不上 → 才挂断
        check("T17-③ 核不上先完整重问一次，再核不上才挂断（以前直接挂）",
              _out23a.get("intent") == "identity_reask"
              and "后四位" in _out23a["reply_text"]
              and _out23b.get("end_reason") == "identity_failed",
              ["第一轮 intent=%r 回复=%s" % (_out23a.get("intent"),
                                              _out23a["reply_text"][:36]),
               "第二轮 end_reason=%r" % _out23b.get("end_reason")])
    finally:
        config_loader._cache = _orig_cache20

    _kb_pop20 = _kbmod.load(os.path.join(ROOT, "config", "kb_pop.json"))
    _nocall_ids = {it.get("id") for it in (_kb_pop20.raw.get("items") or [])
                   if it.get("no_call")}
    _leak = []
    for _iid in sorted(x for x in _nocall_ids if x):
        _it20 = next(x for x in _kb_pop20.raw["items"] if x.get("id") == _iid)
        _probe = " ".join((_it20.get("patterns") or [])[:3])
        if not _probe:
            continue
        _hit20 = _kb_pop20.match(_probe)
        if _hit20 and _hit20.get("id") == _iid:
            _leak.append(_iid)
    check("京麦驳回域条目不进电话检索（no_call %d 条，泄漏 %s）"
          % (len(_nocall_ids), _leak or "无"),
          _nocall_ids and not _leak,
          ["no_call 条目：%s…" % ", ".join(sorted(x for x in _nocall_ids if x)[:6])])

    _r1, _m1 = _kbmod.render("您申请的【XXX类商标】品牌存在资质问题", None)
    _r2, _m2 = _kbmod.render("您申请的【】品牌存在资质问题", None)
    check("占位符两套体系：具名填不上原样保留、空【】记 missing（不删也不编造）",
          "【XXX类商标】" in _r1 and "（空占位符）" in " ".join(_m2),
          ["具名渲染：%r missing=%s" % (_r1[:34], _m1),
           "空渲染：%r missing=%s" % (_r2[:34], _m2)])

    # ---- 证据 21：C5 申请单接线 / C3 关单自动判定 / C1 裸报公司名收槽 ----
    # 起因：C5 之前核身只有 2 条售后假客户，赛题场景报姓名/公司名/申请单号
    # 一律核不上；场景话术也不带任何申请单真值。这里用 120 份虚构档案做
    # 进程内全链路，把四路径、精准告知、京麦收尾、回写真值一次钉死。
    print()
    print("=" * 74)
    print("证据 21：C5 申请单接线（四路径核身/精准告知/回写真值）与 C3 关单自动判定")
    print("=" * 74)

    _apps21 = json.load(open(os.path.join(ROOT, "config", "mock", "applications.json"),
                             encoding="utf-8")).get("items") or []
    _tel = next(r for r in _apps21
                if r["reject_category"] == "品牌-授权" and r["close_mode"] == "电话"
                and r["call_status"] not in ("无需外呼", "无效号码"))
    _jm = next(r for r in _apps21 if r["close_mode"] == "京麦")
    _pop21 = json.load(open(os.path.join(ROOT, "config", "scenes", "pop_reject.json"),
                            encoding="utf-8"))
    _orig21 = config_loader._cache
    config_loader._cache = _pop21
    try:
        for _field, _label in (("verify_by_name", "姓名"), ("verify_by_tail", "后四位"),
                               ("verify_by_company", "公司名"),
                               ("verify_by_application", "申请单号")):
            _s21 = _dlg.new_session("verify-c5-" + _field)
            _dlg.respond(_s21, "我是负责人")
            _o21 = _dlg.respond(_s21, _tel[_field])
            check("C5 四路径核身·%s → identity_ok 且播报品牌/驳回原因" % _label,
                  _o21["intent"] == "identity_ok"
                  and _tel["brand_name"] in _o21["reply_text"]
                  and str(_tel["reject_reason"])[:8] in _o21["reply_text"],
                  ["intent=%s 回复=%s" % (_o21["intent"], _o21["reply_text"][:52])])

        _s21c = _dlg.new_session("verify-c5-company-slot")
        _dlg.respond(_s21c, "我是负责人")
        _o21c = _dlg.respond(_s21c, _tel["verify_by_company"])
        check("C1/T7 赛题版：裸报公司名收进 company 槽位且不重复追问",
              bool(_s21c["identity_slots"].get("company"))
              and _o21c["intent"] == "identity_ok",
              ["company=%r intent=%s" % (_s21c["identity_slots"].get("company"),
                                         _o21c["intent"])])

        _s21j = _dlg.new_session("verify-c3-jingmai")
        _dlg.respond(_s21j, "我是负责人")
        _o21j = _dlg.respond(_s21j, _jm["verify_by_name"])
        check("C3 close_mode=京麦自动判定：核上即收尾，不进 NEED、不回写",
              _o21j.get("end_reason") == "jingmai_no_call"
              and _o21j["next_stage"] == "DONE" and not _o21j.get("tool_call"),
              ["end_reason=%r next=%s" % (_o21j.get("end_reason"),
                                          _o21j["next_stage"])])

        _s21w = _dlg.new_session("verify-c5-writeback")
        _dlg.respond(_s21w, "我是负责人")
        _dlg.respond(_s21w, _tel["verify_by_tail"])
        _dlg.respond(_s21w, "我申请的品牌授权没过")
        _dlg.respond(_s21w, "行，清楚了")
        _o21w = _dlg.respond(_s21w, "对，清楚了")
        _w21 = ((_o21w.get("tool_call") or {}).get("args") or {}).get("content") or {}
        check("C5 回写取档案真值（reject_category/close_mode/reject_summary）",
              bool(_w21) and _w21.get("reject_category") == _tel["reject_category"]
              and _w21.get("close_mode") == _tel["close_mode"]
              and str(_w21.get("reject_summary") or "")[:8] == str(_tel["reject_reason"])[:8],
              ["reject_category=%r close_mode=%r summary=%r"
               % (_w21.get("reject_category"), _w21.get("close_mode"),
                  (_w21.get("reject_summary") or "")[:20])])

        check("占位符不原样播出：档案为空时 _fill_application 换中性说法",
              "{" not in _dlg._fill_application("您提交的{application_desc}因{reason}被驳回，需补{materials}", None),
              ["neutral=%r" % _dlg._fill_application(
                  "您提交的{application_desc}因{reason}被驳回，需补{materials}", None)])
    finally:
        config_loader._cache = _orig21

    # 场景隔离：主场景不得查申请单库（两个业务域共用 4 位尾号键空间，
    # 无条件查会让售后场景的"错误尾号 1234"意外命中归档数据）。
    # 这条断言的语义是"**售后**场景不查申请单库"（两个业务域共用 4 位尾号键空间）。
    # 默认场景已换成赛题 POP 资审——那个场景**本来就要**查申请单库（C5 的正主），
    # 拿默认场景跑这条，测的就不是它想测的东西了。显式切到售后场景。
    _c5_prev = config_loader.scene_path()
    config_loader.set_scene("service_outbound")
    _orig_qa = _dlg.tools.query_application
    _qa_calls = []
    _dlg.tools.query_application = lambda kw: (_qa_calls.append(kw), {"found": False})[1]
    try:
        _s21i = _dlg.new_session("verify-c5-isolate")
        _dlg.respond(_s21i, "是我")
        _dlg.respond(_s21i, "1234")
    finally:
        _dlg.tools.query_application = _orig_qa
    check("C5 场景隔离：售后主场景完全不调 query_application",
          not _qa_calls and not config_loader.load().get("application_mode"),
          ["调用次数=%d 主场景 application_mode=%r"
           % (len(_qa_calls), config_loader.load().get("application_mode"))])
    config_loader.set_scene(_c5_prev)      # 切回默认（赛题 POP 资审）

    # 数据包校验脚本挂进总闸门：改坏任何一份档案，总闸门必须红。
    _ck = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                       "check_mock_applications.py")],
                         cwd=ROOT, capture_output=True, text=True, timeout=180)
    check("%d 份原集虚构档案七项校验通过（挂进总闸门）" % len(_apps21),
          _ck.returncode == 0,
          ["退出码 %d；末行：%s" % (_ck.returncode,
                                    ((_ck.stdout or "").strip().splitlines() or ["无输出"])[-1][:80])])

    # C4：340 条对抗用例的结构自检 + 变异验证（防「全绿是假绿」）。
    import glob as _glob
    _c4_files = sorted(_glob.glob(os.path.join(ROOT, "eval",
                                               "adversarial_c4_batch*.json")))
    _c4_all = []
    for _p in _c4_files:
        _c4_all.extend(json.load(open(_p, encoding="utf-8")))
    _c4_ids = [c["id"] for c in _c4_all]
    _c4_batch_ok = all(len(json.load(open(_p, encoding="utf-8"))) <= 40
                       for _p in _c4_files)
    check("C4 用例集：68 场景 × 5 追问 = 340 条，9 批每批 ≤40，ID 不重复",
          len(_c4_files) == 9 and len(_c4_all) == 340
          and len(set(_c4_ids)) == 340 and _c4_batch_ok,
          ["批次 %d，用例 %d，唯一 ID %d，最大批次 %d"
           % (len(_c4_files), len(_c4_all), len(set(_c4_ids)),
              max([len(json.load(open(_p, encoding="utf-8"))) for _p in _c4_files] or [0]))])

    _c4m = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "test_c4_mutation.py")],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    _c4m_out = ((_c4m.stdout or "") + (_c4m.stderr or "")).strip().splitlines()
    check("C4 变异验证：双层合规失效时结果预期类用例必须报红（全绿不是假绿）",
          _c4m.returncode == 0 and any("变异验证：PASS" in l for l in _c4m_out),
          ["退出码 %d；末行：%s" % (_c4m.returncode,
                                    (_c4m_out or ["无输出"])[-1][:80])])

    # ---- 证据 22：C6 官方数据噪声标注 + 嘈杂人声文本层防护 ----
    print()
    print("=" * 74)
    print("证据 22：C6 噪声清单逐条标注与嘈杂人声鲁棒性")
    print("=" * 74)

    _c6a = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "check_c6_annotations.py")],
                          cwd=ROOT, capture_output=True, text=True, timeout=180)
    _c6a_out = ((_c6a.stdout or "") + (_c6a.stderr or "")).strip().splitlines()
    check("C6 官方噪声逐条标注：覆盖/原文完整性/京麦隔离/可复算 + 反向自测",
          _c6a.returncode == 0 and any("全部通过" in l for l in _c6a_out),
          ["退出码 %d；末行：%s" % (_c6a.returncode,
                                    (_c6a_out or ["无输出"])[-1][:80])])

    _c6n = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "test_noise_robustness.py")],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    _c6n_out = ((_c6n.stdout or "") + (_c6n.stderr or "")).strip().splitlines()
    check("C6 连续 3 轮噪声不挂断/不烧核身/不误回写/可恢复（7 组 + 静音边界）",
          _c6n.returncode == 0 and any("噪声鲁棒性：7/7 通过" in l for l in _c6n_out),
          ["退出码 %d；末行：%s" % (_c6n.returncode,
                                    (_c6n_out or ["无输出"])[-1][:80])])

    _c6m = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "test_noise_robustness.py"),
                           "--self-test"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    _c6m_out = ((_c6m.stdout or "") + (_c6m.stderr or "")).strip().splitlines()
    check("C6 噪声探针变异自测：防护失效时必须报红（全绿不是假绿）",
          _c6m.returncode == 0 and any("C6 变异自测：2/2 通过" in l for l in _c6m_out),
          ["退出码 %d；末行：%s" % (_c6m.returncode,
                                    (_c6m_out or ["无输出"])[-1][:80])])

    # ---- 证据 23：S1 intent 灰度决策（降级路径 / 默认不开 / 变异自测） ----
    print()
    print("=" * 74)
    print("证据 23：S1 intent 模型灰度——降级路径与默认关闭")
    print("=" * 74)

    _s1 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                       "intent_gray_probe.py"),
                          "--offline", "--self-test"],
                         cwd=ROOT, capture_output=True, text=True, timeout=180,
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    _s1_out = ((_s1.stdout or "") + (_s1.stderr or "")).strip().splitlines()
    check("S1 降级路径 13/13（含密钥读取异常不外抛）且变异 2/2 能报红",
          _s1.returncode == 0
          and any("13/13 通过" in l for l in _s1_out)
          and any("2/2 断言能变红" in l for l in _s1_out),
          ["退出码 %d；末行：%s" % (_s1.returncode,
                                    (_s1_out or ["无输出"])[-1][:80])])

    with open(os.path.join(ROOT, "config", "scene.json"), encoding="utf-8") as _f:
        _main_scene = json.load(_f)
    _intent_cfg = _main_scene.get("intent") or {}
    check("S1 默认仍 provider=template（决策文档要求不擅自开启模型）",
          str(_intent_cfg.get("provider") or "template").strip() != "dashscope",
          ["intent 段：%r" % _intent_cfg])

    # ---- 证据 24：S2 kb 语义召回（降级 / 默认关闭 / 阈值安全线） ----
    print()
    print("=" * 74)
    print("证据 24：S2 kb 语义召回外挂层——降级路径与默认关闭")
    print("=" * 74)

    _s2 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                       "test_kb_semantic.py"),
                          "--offline"],
                         cwd=ROOT, capture_output=True, text=True, timeout=300,
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    _s2_out = ((_s2.stdout or "") + (_s2.stderr or "")).strip().splitlines()
    check("S2 降级 9/9（无密钥/超时/乱码/no_call/默认阈值安全线）+ 变异 2/2",
          _s2.returncode == 0
          and any("9/9 通过" in l for l in _s2_out)
          and any("2/2 断言能变红" in l for l in _s2_out),
          ["退出码 %d；末行：%s" % (_s2.returncode,
                                    (_s2_out or ["无输出"])[-1][:80])])

    with open(os.path.join(ROOT, "config", "scenes", "pop_reject.json"),
              encoding="utf-8") as _f:
        _pop_scene = json.load(_f)
    _sem_main = (_main_scene.get("knowledge") or {}).get("semantic") or {}
    _sem_pop = (_pop_scene.get("knowledge") or {}).get("semantic") or {}
    check("S2 默认关闭：两个场景都未开启 semantic（docs/27 开启建议）",
          str(_sem_main.get("provider") or "").strip() != "dashscope"
          and str(_sem_pop.get("provider") or "").strip() != "dashscope",
          ["main=%r pop=%r" % (_sem_main, _sem_pop)])

    from modules import kb_semantic as _kbs
    check("S2 生产默认阈值 ≥0.60（0.45 实测无关样本 7/10 被乱答）",
          _kbs._DEFAULT_THRESHOLD >= 0.60,
          ["当前 %.2f" % _kbs._DEFAULT_THRESHOLD])

    # ---- 证据 25：T17 / P4 回归 + 故障用例隔离 + T15 答辩口径 ----
    print()
    print("=" * 74)
    print("证据 25：智能化线 T17/P4 回归与答辩口径")
    print("=" * 74)

    _ENV = dict(os.environ, PYTHONIOENCODING="utf-8")

    _t17 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "adversarial_run.py"),
                           "--cases", os.path.join(ROOT, "eval",
                                                   "adversarial_t17_realcall.json"),
                           "--rounds", "1", "--no-report"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300,
                          env=_ENV)
    _t17_out = ((_t17.stdout or "") + (_t17.stderr or "")).strip().splitlines()
    check("T17 真实通话 6 条对抗用例全部通过（中间轮承接也断言，不是只看闭环）",
          _t17.returncode == 0 and any("通过 6/6" in l for l in _t17_out),
          ["退出码 %d；末行：%s" % (_t17.returncode,
                                    (_t17_out or ["无输出"])[-1][:80])])

    _t17m = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                         "test_t17_mutation.py")],
                           cwd=ROOT, capture_output=True, text=True, timeout=300,
                           env=_ENV)
    _t17m_out = ((_t17m.stdout or "") + (_t17m.stderr or "")).strip().splitlines()
    check("T17 变异 3/3：反问承接/3位尾号引导/词表边界失效时必须报红",
          _t17m.returncode == 0
          and any("T17 变异验证：3/3 通过" in l for l in _t17m_out),
          ["退出码 %d；末行：%s" % (_t17m.returncode,
                                    (_t17m_out or ["无输出"])[-1][:80])])

    _p4 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                       "test_p4_timeout.py")],
                         cwd=ROOT, capture_output=True, text=True, timeout=300,
                         env=_ENV)
    _p4_out = ((_p4.stdout or "") + (_p4.stderr or "")).strip().splitlines()
    check("P4 故障注入 3/3 + 变异（关掉 _lookup_failed 必须报红）",
          _p4.returncode == 0
          and any("P4 故障注入基线：3/3 通过" in l for l in _p4_out)
          and any("P4 变异验证：PASS" in l for l in _p4_out),
          ["退出码 %d；末行：%s" % (_p4.returncode,
                                    (_p4_out or ["无输出"])[-1][:80])])

    _eval_dir = os.path.join(ROOT, "eval")
    _auto = [n for n in os.listdir(_eval_dir)
             if n.startswith("adversarial") and n.endswith(".json")]
    check("需故障注入的 P4 用例已隔离，不污染默认 adversarial*.json 跑批",
          not any("p4" in n.lower() for n in _auto)
          and os.path.exists(os.path.join(_eval_dir, "fault_cases",
                                          "p4_tool_error.json")),
          ["自动跑批文件：%s" % ", ".join(sorted(_auto))])

    _ib = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                       "intent_bench.py"),
                          "--no-model"],
                         cwd=ROOT, capture_output=True, text=True, timeout=180,
                         env=_ENV)
    _ib_out = ((_ib.stdout or "") + (_ib.stderr or "")).strip().splitlines()
    check("T15 intent_bench 样本条数以 SAMPLES 为准（25 条）且离线基准可跑",
          _ib.returncode == 0
          and any("25 条口语化样本" in l for l in _ib_out),
          ["退出码 %d；末行：%s" % (_ib.returncode,
                                    (_ib_out or ["无输出"])[-1][:80])])

    with open(os.path.join(ROOT, "docs", "21_答辩提纲与演示脚本.md"),
              encoding="utf-8") as _f:
        _d21 = _f.read()
    check("T15 答辩口径写明「自拟样本 ≠ 泛化准确率」且不再念 86/88",
          "自证覆盖" in _d21 and "不是泛化准确率" in _d21
          and "25 条" in _d21 and "86/88" not in _d21,
          ["docs/21 长度 %d 字" % len(_d21)])

    # ---- 证据 26：质检进化线 Q1/Q2/Q3/Q4/Q5/S3 ----
    print()
    print("=" * 74)
    print("证据 26：质检进化线（分诊转正/课程进化/严格模式/双场景/语义合规）")
    print("=" * 74)

    _qc1 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "adversarial_run.py"),
                           "--cases", os.path.join(ROOT, "eval",
                                                   "adversarial_q1_regression.json"),
                           "--rounds", "1", "--no-report"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300,
                          env=_ENV)
    _qc1_out = ((_qc1.stdout or "") + (_qc1.stderr or "")).strip().splitlines()
    check("Q1 分诊转正 6 条回归用例全过（拒绝核身给替代路径/收费非拒绝/确认词闭环）",
          _qc1.returncode == 0 and any("通过 6/6" in l for l in _qc1_out),
          ["退出码 %d；末行：%s" % (_qc1.returncode,
                                    (_qc1_out or ["无输出"])[-1][:80])])

    # Q4 起画像生成物不进仓库：CI / 新克隆环境里没有这个文件。
    # 所以这里先按生成器复算一次（固定顺序、只用标准库、毫秒级），
    # 否则 Q2 课程与 Q4 断言会在干净环境里直接红——那不是缺陷，是没准备输入。
    _personas_path = os.path.join(ROOT, "eval", "personas_generated.json")
    if not os.path.exists(_personas_path):
        subprocess.run([sys.executable,
                        os.path.join(ROOT, "scripts", "gen_adversarial.py")],
                       cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env=_ENV)

    _qc2 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "course_evolution.py"),
                           "--self-test",
                           "--report", os.path.join(ROOT, "eval", "reports",
                                                    "_verify_course.md"),
                           "--json", os.path.join(ROOT, "eval", "reports",
                                                  "_verify_course.json")],
                          cwd=ROOT, capture_output=True, text=True, timeout=600,
                          env=_ENV)
    _qc2_out = ((_qc2.stdout or "") + (_qc2.stderr or "")).strip().splitlines()
    check("Q2 三代课程可复算，且变异能抓到注入缺陷（课程不是摆设）",
          _qc2.returncode == 0
          and any("Gen1 全矩阵" in l for l in _qc2_out)
          and any("已抓到" in l for l in _qc2_out),
          ["退出码 %d；末行：%s" % (_qc2.returncode,
                                    (_qc2_out or ["无输出"])[-1][:80])])

    _qc3 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "run_eval.py"),
                           "--cases", os.path.join(ROOT, "eval", "fault_cases",
                                                   "p4_tool_error.json"),
                           "--strict"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300,
                          env=_ENV)
    check("Q3 --strict：有用例失败时 exit 1（接 CI 不会静默绿）",
          _qc3.returncode == 1,
          ["退出码 %d（期望 1）" % _qc3.returncode])

    _qc3b = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                         "run_eval.py"),
                            "--cases", os.path.join(ROOT, "eval",
                                                    "adversarial_q1_regression.json"),
                            "--strict"],
                           cwd=ROOT, capture_output=True, text=True, timeout=300,
                           env=_ENV)
    check("Q3 --strict：全通过时 exit 0（现有调用方不传参行为不变）",
          _qc3b.returncode == 0,
          ["退出码 %d（期望 0）" % _qc3b.returncode])

    # Q4：不调 git（PowerShell/CI 环境里 git 未必在 PATH，调了会 FileNotFoundError
    # 把整个 verify 崩掉，而不是报一条红）。用 .gitignore + 本地可复算来判。
    _personas = os.path.join(ROOT, "eval", "personas_generated.json")
    try:
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as _gf:
            _ignored = "/eval/personas_generated.json" in _gf.read()
    except Exception:
        _ignored = False
    check("Q4 画像生成物已出仓库（.gitignore 命中、本地保留可复算）",
          _ignored and os.path.exists(_personas),
          ["ignored=%s 本地存在=%s" % (_ignored, os.path.exists(_personas))])

    _rep_main = os.path.join(ROOT, "docs", "eval_reports", "eval_main_1106.md")
    _rep_pop = os.path.join(ROOT, "docs", "eval_reports", "eval_pop_reject_1106.md")
    _ok_rep = False
    if os.path.exists(_rep_main) and os.path.exists(_rep_pop):
        with open(_rep_main, encoding="utf-8") as f:
            _m = f.read()
        with open(_rep_pop, encoding="utf-8") as f:
            _p = f.read()
        _ok_rep = "88/88" in _m and "340/340" in _p
    check("Q5 双场景两份报告分开存档（售后 88/88、pop 340/340）", _ok_rep,
          ["main=%s pop=%s" % (os.path.exists(_rep_main), os.path.exists(_rep_pop))])

    _s3 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                       "test_semantic_judge.py"),
                          "--offline"],
                         cwd=ROOT, capture_output=True, text=True, timeout=300,
                         env=_ENV)
    _s3_out = ((_s3.stdout or "") + (_s3.stderr or "")).strip().splitlines()
    check("S3 语义合规离线降级 6/6 + 变异 2/2（默认走模板层）",
          _s3.returncode == 0
          and any("6/6 通过" in l for l in _s3_out)
          and any("2/2" in l for l in _s3_out),
          ["退出码 %d；末行：%s" % (_s3.returncode,
                                    (_s3_out or ["无输出"])[-1][:80])])

    print()
    print("=" * 74)
    print("证据 27：Z6 端点检测——噪声不再触发 vad_result")
    print("=" * 74)

    from modules import silero_vad, vad_gate

    corpus = os.path.join(ROOT, "data", "vad_corpus")

    def _corpus_pcm(path):
        import wave
        with wave.open(path) as w:
            return w.readframes(w.getnframes())

    def _corpus_files(kind):
        d = os.path.join(corpus, kind)
        if not os.path.isdir(d):
            return []
        return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".wav"))

    noise_files = _corpus_files("noise")
    speech_files = _corpus_files("speech")

    # ① 噪声侧：九类都必须判成"没人说话"。
    #    这是 Z6 的核心验收——改造前 vad_detect 是 bool(audio_bytes)，
    #    噪声 9/9 全部 started=True（见 scripts/bench_vad.py 的 bytes 行）。
    if not noise_files:
        mark_uncovered("Z6 噪声不误触",
                       "语料缺失，跑 python scripts/gen_vad_corpus.py --noise-only")
    else:
        default_engine = vad_gate.analyze(_corpus_pcm(noise_files[0]))["engine"]
        hits = []
        for p in noise_files:
            out = vad_gate.analyze(_corpus_pcm(p))
            if out["started"]:
                hits.append("%s(speech_ms=%d)" % (os.path.basename(p), out["speech_ms"]))
        check("Z6 九类噪声片段全部不判为人声（默认引擎=%s）" % default_engine,
              not hits,
              ["命中（不该）：%s" % ", ".join(hits) if hits else "9/9 未触发",
               "对照：改造前 bool(audio_bytes) 是 9/9 全触发"])

        # ② 纯标准库引擎单独再验一遍：干净机器没装 onnxruntime 时走的就是它。
        sp_hits = []
        for p in noise_files:
            out = vad_gate.analyze(_corpus_pcm(p), prefer="spectral")
            if out["started"]:
                sp_hits.append(os.path.basename(p))
        check("Z6 降级引擎（spectral，零依赖）同样挡得住九类噪声",
              not sp_hits,
              ["误触：%s" % ", ".join(sp_hits) if sp_hits else "9/9 未触发",
               "这一条保证没装 onnxruntime 的机器也不会把噪声当人声"])

    # ③ 人声侧：真人声必须判出来，否则"不误触"可以靠"永远说没人"作弊。
    if not speech_files:
        mark_uncovered("Z6 人声召回",
                       "人声语料缺失（合成需要 dashscope_api_key）："
                       "python scripts/gen_vad_corpus.py")
    else:
        miss = []
        for p in speech_files:
            out = vad_gate.analyze(_corpus_pcm(p), prefer="spectral")
            if not out["started"]:
                miss.append(os.path.basename(p))
        check("Z6 真人声 %d 条全部判出人声（spectral 引擎）"
              % len(speech_files), not miss,
              ["漏判：%s" % ", ".join(miss) if miss else "全中",
               "语料含 6 条长句客服话术 + 6 条口语短句（含「嗯，我知道了」这种短应答）"])

    # ④ 降级必须如实标注：不能在没装模型时装成 Silero。
    _orig_avail = silero_vad.available
    silero_vad.available = lambda: False
    silero_vad.reset_cache()
    try:
        sample = noise_files[0] if noise_files else speech_files[0] if speech_files else None
        if sample:
            down = vad_gate.analyze(_corpus_pcm(sample), prefer="silero")
        else:
            down = vad_gate.analyze(b"\x00\x00" * 8000, prefer="silero")
    finally:
        silero_vad.available = _orig_avail
        silero_vad.reset_cache()
    check("Z6 引擎降级如实标注（不装模型时不冒充 Silero）",
          down["engine"] == "spectral" and bool(down["engine_note"]),
          ["engine=%s note=%s" % (down["engine"], down["engine_note"][:60])])

    # ⑤ 变异：把谱判据整条换成"能量门"（等于退回改造前的行为），噪声必须被误触。
    #    为什么不用"把某个阈值改小"来变异：单条阈值放松后被别的判据挡住了（实测 0/9），
    #    那只能证明"多重判据有冗余"，不能证明"这条断言抓得住退化"。
    #    整条换掉才等价于"有人把 Z6 改回去了"。
    if noise_files:
        _orig_frames = vad_gate._spectral_frames

        def _energy_only(pcm, sr):
            ints = vad_gate._to_ints(pcm)
            n = (len(ints) + vad_gate.FRAME_SAMPLES - 1) // vad_gate.FRAME_SAMPLES
            out = []
            for i in range(n):
                seg = ints[i * vad_gate.FRAME_SAMPLES:(i + 1) * vad_gate.FRAME_SAMPLES]
                out.append((int((i + 1) * vad_gate.FRAME_MS),
                            vad_gate._rms(seg) >= 0.004))
            return out

        vad_gate._spectral_frames = _energy_only
        try:
            broken = [os.path.basename(p) for p in noise_files
                      if vad_gate.analyze(_corpus_pcm(p), prefer="spectral")["started"]]
        finally:
            vad_gate._spectral_frames = _orig_frames
        check("Z6 变异验证：把谱判据换回能量门后噪声必须被误触（否则前面的全绿是假绿）",
              len(broken) >= 3,
              ["换成能量门后误触 %d/%d 类：%s"
               % (len(broken), len(noise_files), ", ".join(broken[:6]))])

    # ⑥ 参数口径一致：docs/16 表里的"开口/说完"毫秒数必须等于代码常量。
    #    这条专门防"改了代码没改文档"——答辩时念的是文档。
    try:
        with open(os.path.join(ROOT, "docs", "16_实时通话与打断.md"),
                  encoding="utf-8") as f:
            _d16 = f.read()
    except OSError:
        _d16 = ""
    check("Z6 文档参数与代码常量一致（开口 %dms / 说完 %dms）"
          % (vad_gate.MIN_SPEECH_MS, vad_gate.END_SILENCE_MS),
          ("%dms" % vad_gate.MIN_SPEECH_MS) in _d16
          and ("%dms" % vad_gate.END_SILENCE_MS) in _d16,
          ["docs/16 里能找到对应数字=%s（改了常量不改文档，这条会红）"
           % bool(_d16)])

    print()
    print("=" * 74)
    print("证据 28：Z7 实时字幕——默认开启 + 去重 + 端到端")
    print("=" * 74)

    # ⓪-b 赛题四段式的第②环：确认「您是XX公司吗」必须在第③环（说明驳回原因）之前。
    #    赛题数据说明原文："确认对方身份（请问您是 XX 公司吗），**不能省，打错人是重大事故**"。
    #    官方 52 条话术的固定格式就是"您好，我是京东商家资质审核专员，请问您是
    #    （商家公司名/店铺名）…吗？"——带公司名。我们原来既没公司名，
    #    又在开场白里先把"没通过审核"说了出去。
    try:
        with open(os.path.join(ROOT, "config", "scenes", "pop_reject.json"),
                  encoding="utf-8") as f:
            _pop = json.load(f)
    except Exception:
        _pop = {}
    _g = (_pop.get("stages") or {}).get("GREETING") or {}
    _open_reply = _g.get("reply") or ""
    check("赛题第②环：开场白按官方格式报出公司名，且不提前说驳回",
          "{company_name}" in _open_reply and "驳回" not in _open_reply
          and "资质审核" in _open_reply,
          ["GREETING.reply = %r" % _open_reply,
           "含公司名占位符=%s，提前说驳回=%s"
           % ("{company_name}" in _open_reply, "驳回" in _open_reply)])

    # ⓪-c 真跑一通：开场白里出现真实公司名；用户确认公司之前不能说驳回原因。
    #     显式切到 pop_reject 跑，别指望"主进程当前恰好是哪个场景"——
    #     verify 里前面几段证据会切场景，靠默认值等于把断言绑在执行顺序上。
    _q4_prev = config_loader.scene_path()
    config_loader.set_scene("pop_reject")
    _sid_o = start_call()["session_id"]
    _open_txt = payload_of(_sid_o, "agent_reply") or {}
    _open_line = (_open_txt.get("text") or "")
    _tgt = (_pop.get("_scene_meta") or {})
    check("赛题第②环：开场白填的是外呼名单里的真实公司名",
          bool(_open_line) and ("公司" in _open_line or "店" in _open_line)
          and "请问您是" in _open_line,
          ["开场白 = %r" % _open_line[:60]])

    _r1 = handle_turn(_sid_o, user_text="怎么了")
    _r2 = handle_turn(_sid_o, user_text="不是")
    check("赛题第②环：用户确认公司前不进核身；否认身份立即收尾",
          "后四位" not in (_r1.get("reply_text") or "")
          and "没有通过审核" not in (_r1.get("reply_text") or "")
          and _r2.get("next_stage") == "DONE",
          ["第一轮回复：%r（不应出现核身/驳回原因）" % (_r1.get("reply_text") or "")[:60],
           "否认后 next_stage = %s（应为 DONE）" % _r2.get("next_stage")])
    end_call(_sid_o)
    config_loader.set_scene(_q4_prev)      # 切回默认场景

    # ⓪-d nlg 实体保护：改写把公司名改没了必须丢弃（实测踩过：
    #     "北京市川上商贸有限责任公司" 被改写成 "这家公司"，六道守卫一道没拦）。
    try:
        from modules import nlg as _nlg
        _sess = {"outbound_target": {"company_name": "北京市川上商贸有限责任公司",
                                     "company_short_name": "川上",
                                     "shop_name": "川上官方旗舰店",
                                     "application_id": "QZ202611060001",
                                     "brand_name": "闻樾",
                                     "phone_tail": "0086"}}
        _base = "请问您是北京市川上商贸有限责任公司的法定代表人吗？"
        check("nlg 实体保护：公司名被改写掉时必须丢弃改写",
              _nlg._entities_ok(_base, "请问您是这家公司的法定代表人吗？", _sess) is False
              and _nlg._entities_ok(_base, "请问您是北京市川上商贸有限责任公司的法定代表人吗？",
                                    _sess) is True,
              ["改掉公司名 → 判为不合格；保留公司名 → 判为合格"])
    except Exception as _exc:
        mark_uncovered("nlg 实体保护", str(_exc)[:80])

    # ⓪ 默认场景必须是**赛题场景**，不是占位场景。
    #    实测踩过：直接 python app.py 不带 APP_SCENE 时跑的是 config/scene.json，
    #    而它长期是"服务类外呼（占位场景，等官方公布后替换）"——官方早就公布了
    #    （11 月初那两份 xlsx），演示/线上却还在跑占位场景，开场白还是
    #    "我是京东客服中心的小采，请问是张先生本人吗？"。评审看到的是错的题。
    #    这条断言把"默认跑什么"钉死。
    try:
        with open(os.path.join(ROOT, "config", "scene.json"), encoding="utf-8") as f:
            _dflt = json.load(f)
    except Exception:
        _dflt = {}
    _dflt_name = _dflt.get("scene_name") or ""
    _dflt_ok = ("POP" in _dflt_name
                and bool(_dflt.get("application_mode"))
                and bool(_dflt.get("reject_rules")))
    check("Z7 默认场景（config/scene.json）就是赛题 POP 资审场景，不是占位场景",
          _dflt_ok,
          ["scene_name = %r" % _dflt_name,
           "application_mode = %s，reject_rules %d 条"
           % (_dflt.get("application_mode"), len(_dflt.get("reject_rules") or [])),
           "占位场景已挪到 config/scenes/service_outbound.json，"
           "需要时用 APP_SCENE=service_outbound 切"])

    # ① **所有**场景包都要开，不是"开一个算数"。
    #    实测踩过两次：先只改 config/scene.json → ready.realtime 仍 false（场景包整体
    #    覆盖 asr 段）；改完 pop 又漏了 service_outbound / retail_recall —— 售后主场景
    #    照样一条字幕都没有，而 health 看起来一切正常。所以这里**不写死文件名**，
    #    而是扫 config/scenes/ 下所有包：以后新增场景包，漏配就会直接报红。
    _rt_flags = {}
    for rel in ["config/scene.json"] + [
            os.path.join("config", "scenes", os.path.basename(f))
            for f in sorted(glob.glob(os.path.join(ROOT, "config", "scenes", "*.json")))]:
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                _rt_flags[rel] = bool((json.load(f).get("asr") or {}).get("realtime"))
        except Exception:
            _rt_flags[rel] = False
    _rt_missing = [k for k, v in _rt_flags.items() if not v]
    check("Z7 主配置与**全部**场景包都开了流式识别（扫 config/scenes/，不是挑几个看）",
          not _rt_missing,
          [json.dumps(_rt_flags, ensure_ascii=False),
           "缺 realtime 的：%s" % (", ".join(_rt_missing) or "无")])

    # ② 半句去重：同一段文本只推一次，正常增长照常推。离线断言，不花钱。
    from modules import realtime_asr as _rtmod

    _inst = _rtmod.RealtimeAsr(None, "t")

    def _push_sentence(text, end=False, sid=1):
        _inst._on_result({"payload": {"output": {"sentence": {
            "sentence_id": sid, "text": text, "sentence_end": end}}}})

    _push_sentence("你好")
    _p1 = _inst.poll_partial()
    _p2 = _inst.poll_partial()           # 云端把同一句又推了一次
    _push_sentence("你好，我是")
    _p3 = _inst.poll_partial()
    check("Z7 半句字幕不重复推送（同一文本只给一次，增长照常给）",
          _p1 == "你好" and _p2 == "" and _p3 == "你好，我是",
          ["第 1 次=%r 第 2 次（重复）=%r 增长后=%r" % (_p1, _p2, _p3)])

    # ③ 前端在落 turn 后要清空字幕，否则同一句会在日志与字幕上各出现一次
    try:
        with open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8") as f:
            _html = f.read()
    except OSError:
        _html = ""
    check("Z7 落 turn 后清空半句字幕（同一句不出现两遍）",
          'liveSubtitle").textContent = ""' in _html,
          ["web/index.html 命中=%s" % bool(_html)])

    # ④ 端到端：真起服务 + 真 WS + 真流式。没有语料或密钥就明说"没验"，不装绿。
    _z7_wav = os.path.join(ROOT, "data", "vad_corpus", "speech", "s07.wav")

    def _real_dashscope_key():
        """拿真实密钥，**跳过本进程里的测试注入值**。

        前面几条断言为了离线跑，把 DASHSCOPE_API_KEY 设成 "test-key-not-used"
        并且没有恢复（它只影响本进程）。这里如果直接读环境变量，就会拿着这个
        假密钥去连云端，得到 401，然后误判成"流式链路坏了"——
        实测踩过一次：verify 里红，单独跑脚本绿，差的就是这个注入值。
        """
        env_val = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
        if env_val and env_val != "test-key-not-used":
            return env_val
        try:
            with open(os.path.join(ROOT, "config", "secrets.json"),
                      encoding="utf-8") as f:
                return (json.load(f).get("dashscope_api_key") or "").strip()
        except Exception:
            return ""

    _z7_env = {k: v for k, v in _ENV.items()
               if k not in ("DASHSCOPE_API_KEY", "SC_DASHSCOPE_API_KEY")}
    if not os.path.exists(_z7_wav) or not _real_dashscope_key():
        mark_uncovered("Z7 端到端（WS → 流式字幕 → 落 turn）",
                       "需要 data/vad_corpus/speech/s07.wav 与 dashscope_api_key")
    else:
        _z7 = subprocess.run([sys.executable,
                              os.path.join(ROOT, "scripts", "check_ws_realtime.py"),
                              "--wav", _z7_wav, "--port", "8807"],
                             cwd=ROOT, capture_output=True, text=True, timeout=180,
                             env=_z7_env)
        _z7_out = ((_z7.stdout or "") + (_z7.stderr or "")).strip().splitlines()
        _z7_txt = "\n".join(_z7_out)
        check("Z7 端到端：流式字幕 ≥3 条、无重复、终句连贯、有回复",
              _z7.returncode == 0 and "Z7 端到端通过" in _z7_txt,
              ["退出码 %d" % _z7.returncode]
              + [l for l in _z7_out if "字幕" in l or "realtime" in l
                 or "原因" in l][:5])

    print()
    print("=" * 74)
    print("证据 29：V1 打断/端点参数——前端与仿真同源，场景全过，边界有据")
    print("=" * 74)

    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import tune_barge_vad as _tune

    # ① 仿真必须"从 index.html 读参数"，不能自己存一份。读不到就报错不跑——
    #    这是防"前端改了、标定脚本没跟"的唯一办法。
    try:
        _fp = _tune.load_front_params()
        _fp_ok, _fp_err = True, ""
    except Exception as exc:
        _fp = {}
        _fp_ok, _fp_err = False, str(exc)[:120]
    check("V1 标定脚本能从前端解析 const VAD（参数不另存一份）",
          _fp_ok and _fp.get("speechRms") and _fp.get("bargeRms"),
          ["解析到 %s" % json.dumps({k: _fp.get(k) for k in
                                     ("speechRms", "bargeRms", "endpointFrames",
                                      "zcrMax", "tiltMin", "onsetFrames")},
                                    ensure_ascii=False) if _fp_ok else _fp_err])

    # ② 15 个场景全过（正常插话/外放回声/弱 AEC/键盘/清嗓/小声/停顿/长说/静音/
    #    说话含敲击/极小声/强回声/爆响键盘）。这是"打断不会误伤自己、也不会漏人"的直接证据。
    if _fp_ok and os.path.isdir(os.path.join(ROOT, "data", "vad_corpus")):
        _v1 = subprocess.run([sys.executable,
                              os.path.join(ROOT, "scripts", "tune_barge_vad.py"),
                              "--json"],
                             cwd=ROOT, capture_output=True, text=True, timeout=600,
                             env=_ENV)
        try:
            _v1j = json.loads((_v1.stdout or "").strip().splitlines()[-1])
        except Exception:
            _v1j = {}
        _tot = _v1j.get("total", 0)
        _pass = _v1j.get("pass", 0)
        check("V1 十五个场景全部符合预期（外放不自打断、真人能打断、小声能切句）",
              _tot >= 15 and _pass == _tot,
              ["%d/%d 通过；退出码 %d" % (_pass, _tot, _v1.returncode),
               "场景含：A正常插话 B外放回声 C键盘 D清嗓 E句中停顿 F连续长说 "
               "G静音 H说话含敲击 I弱AEC L弱AEC插话 M极小声 N强回声 O爆响键盘"])
    else:
        mark_uncovered("V1 十五场景（打断/端点）", "缺前端参数或语料")

    # ③ 参数边界：栅格扫描的结论要能被钉住。
    #    speechRms 调到 0.024 时极小声场景整段漏判（扫描 96 组里 32 组失败）；
    #    bargeRms 调到 0.025 时强回声场景自己打断自己（128 组里 32 组失败）。
    #    这条断言不是"禁止改参数"，而是"改了要重跑扫描并把数字更新到 docs/16"。
    if _fp_ok:
        # 注意措辞：这条约束的是**合成场景**里的安全区，不是真机安全区。
        # 真机上底噪 rms 峰值能到 0.035（docs/16 §4.8），所以它挡不住"静音误开口"，
        # 那一层靠浏览器的 AEC/NS/AGC 与 ?vadcal=1 现场标定，不能靠这条断言担保。
        check("V1 参数落在合成场景安全区内（speechRms ≤0.020、bargeRms ≥0.035）",
              _fp.get("speechRms", 1) <= 0.020 and _fp.get("bargeRms", 0) >= 0.035,
              ["speechRms=%s（>0.020 漏极小声） bargeRms=%s（<0.035 强回声自打断）"
               % (_fp.get("speechRms"), _fp.get("bargeRms")),
               "口径：合成语料；真机另见 docs/16 §4.8 与 ?vadcal=1"])

    # ④ 前端判据与标定脚本必须同源：常量名在两边都要有，否则改一边就漂移。
    try:
        with open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8") as f:
            _html_v1 = f.read()
    except OSError:
        _html_v1 = ""
    # 三条防回退断言：都是真实缺陷修完之后加的，改回去必须报红。
    #   ① 端点只该看能量。用 voiced 数静音会把一句话切成两半（说到一半换字音就"静音"）。
    #   ② 谱倾斜必须用频段均值。单点 Goertzel 等价于问"这个精确频率有没有能量"，
    #      女声基频 200Hz、400Hz 恰是谷，真人会被判成键盘。
    #   ③ 参数定义了就必须被用到。silenceRms 曾经只定义不用——它就是 ① 那条 bug 的痕迹。
    import re as _re_v1
    _i0 = _html_v1.find('if (S.phase === "user") {')
    _i1 = _html_v1.find("if (S.silentFrames >= VAD.endpointFrames)")
    # 注释里有 "f.voiced"（就是记录这条 bug 的那句），必须先把注释行剔掉，
    # 否则断言会被自己的注释挡住——这种"断言看的是字面而不是代码"是假绿的另一个来源。
    _ep_body = "\n".join(_ln for _ln in
                          (_html_v1[_i0:_i1] if 0 <= _i0 < _i1 else "").splitlines()
                          if not _ln.strip().startswith("//"))
    check("V1 端点用能量判「还在说」，不用 voiced（否则一句话被切两半）",
          "silenceRms" in _ep_body and "f.voiced" not in _ep_body,
          ["端点分支里出现 silenceRms=%s，出现 f.voiced=%s"
           % ("silenceRms" in _ep_body, "f.voiced" in _ep_body)])
    check("V1 谱倾斜用三频点均值，不是单点 Goertzel",
          "LOW_BAND" in _html_v1 and "HIGH_BAND" in _html_v1 and "bandSum" in _html_v1,
          ["LOW_BAND/HIGH_BAND/bandSum 都在=%s"
           % all(k in _html_v1 for k in ("LOW_BAND", "HIGH_BAND", "bandSum"))])
    check("V1 silenceRms 不是死参数（定义了就一定被用到）",
          len(_re_v1.findall(r"silenceRms", _html_v1)) >= 2,
          ["silenceRms 出现 %d 次（<2 说明只定义没用）"
           % len(_re_v1.findall(r"silenceRms", _html_v1))])

    check("V1 前端与仿真判据同源（analyzeFrame + zcrMax/tiltMin/onsetFrames 都在）",
          all(k in _html_v1 for k in ("analyzeFrame", "zcrMax", "tiltMin",
                                      "onsetFrames")),
          ["web/index.html 命中四项=%s"
           % all(k in _html_v1 for k in ("analyzeFrame", "zcrMax", "tiltMin",
                                         "onsetFrames"))])

    print()
    print("=" * 74)
    print("证据 30：L2 自进化——模拟用户受知/受听/受耐心，归因不冤枉系统，版本可回滚")
    print("=" * 74)

    # 30.1 受知：画像不知道尾号，问三次都不能吐出正确值。
    # 这条是整个 L2 的地基：模拟器一旦会编事实，聚类出来的"失败"就全是自证的。
    _sim_co = {"company_name": "测试商贸有限责任公司", "contact_role": "法定代表人",
               "contact_name": "张三", "phone_tail": "8642",
               "application_id": "QZ202611060099"}
    _sim_p = {"id": "V30", "goal": "confirm",
              "knows": {"phone_tail": False, "contact_name": True},
              "willing": {"contact_name": "on_ask"},
              "traits": {"hearing": 1.0, "patience": 6, "trust0": 0.5,
                         "trust_gate": 0.2, "verbosity": 0.3}, "style": []}
    _ask = "好的，核对申请单需要您预留手机号的后四位，说四个数字就行。"
    _s1 = user_sim.UserSim(_sim_p, _sim_co, seed="v30")
    _outs = [_s1.say(_ask) for _ in range(3)]
    _said = " ".join(o["text"] for o in _outs)
    check("模拟用户不会编造它不知道的尾号（问三次也不出现 8642）",
          "8642" not in _said and "八六四二" not in _said,
          ["三次回答：%s" % " / ".join(o["text"] for o in _outs),
           "首轮动作=%s（应为 cant_remember）" % _outs[0]["action"]])

    # 30.2 受知（正向）：知道就必须报出档案里的那个值，不能是另一套写死的数字。
    _s2 = user_sim.UserSim(dict(_sim_p, knows={"phone_tail": True, "contact_name": True}),
                           _sim_co, seed="v30b")
    _o2 = _s2.say(_ask)
    check("模拟用户报出的尾号来自本通档案（8642），不是写死的",
          _o2["action"] == "answer_tail" and
          ("8642" in _o2["text"] or "八六四二" in _o2["text"]),
          ["动作=%s；原话=%s" % (_o2["action"], _o2["text"])])

    # 30.3 受听：开场白只确认身份，没要信息 → 不能主动交信息。
    _s3 = user_sim.UserSim(_sim_p, _sim_co, seed="v30c")
    _o3 = _s3.say("您好，我是京东商家资质审核专员，请问您是测试商贸有限责任公司的法定代表人吗？")
    check("没被问到就不主动交信息（开场只确认身份）",
          _o3["action"] not in user_sim.GAVE_FACT_ACTIONS and "8642" not in _o3["text"],
          ["动作=%s；原话=%s" % (_o3["action"], _o3["text"])])

    # 30.4 受听：听不清只错一次，且必须留下证据（否则归因会算成客服的错）。
    _s4 = user_sim.UserSim(dict(_sim_p, traits={"hearing": 0.0, "patience": 6,
                                                "trust0": 0.5, "trust_gate": 0.2}),
                           _sim_co, seed="v30d")
    _o4a, _o4b = _s4.say(_ask), _s4.say(_ask)
    check("听不清只错一次，且记下听错了哪个词",
          _o4a["action"] == "mishear" and _o4b["action"] != "mishear" and bool(_o4a["missed"]),
          ["第一次=%s（missed=%s）｜第二次=%s"
           % (_o4a["action"], _o4a["missed"], _o4b["action"])])

    # 30.5 受耐心：挂断由耐心预算触发，不是脚本里写死的。
    _s5 = user_sim.UserSim(dict(_sim_p, traits={"hearing": 1.0, "patience": 0,
                                                "trust0": 0.5, "trust_gate": 0.2}),
                           _sim_co, seed="v30e")
    _o5 = _s5.say("您好，我是京东商家资质审核专员。")
    check("耐心耗尽才挂断（patience=0 → hangup 且 ended=True）",
          _o5["action"] == "hangup" and _s5.ended,
          ["动作=%s；ended=%s" % (_o5["action"], _s5.ended)])

    # 30.6 归因：用户提问被回"有杂音没听清" → 必须判成系统缺陷，不能当噪声放过。
    _t_noise = [{"user": {"action": "ask_material", "text": "那我具体要补什么材料？"},
                 "agent": {"reply_text": "不好意思，这边有点杂音没听清，您再说一次。",
                           "stage": "IDENTITY", "next_stage": "IDENTITY",
                           "intent": "noise", "slots": {}}}]
    _codes = [d["code"] for d in attribution.analyse_turns(_t_noise)]
    check("用户提问被当成「有杂音没听清」→ 判为系统缺陷",
          attribution.AGENT_NOISE_ON_QUESTION in _codes,
          ["缺陷码=%s；用户原话=%s" % (_codes, _t_noise[0]["user"]["text"])])

    # 30.7 归因：用户报了尾号、客服同一轮又问一遍 → 槽位丢失。
    _t_slot = [{"user": {"action": "answer_tail", "text": "后四位是 8642"},
                "agent": {"reply_text": _ask, "stage": "IDENTITY",
                          "next_stage": "IDENTITY", "intent": "identity_retry",
                          "slots": {}}}]
    _codes2 = [d["code"] for d in attribution.analyse_turns(_t_slot)]
    check("用户已报尾号、客服同轮又问同一条 → 判为槽位丢失",
          attribution.AGENT_SLOT_LOSS in _codes2,
          ["缺陷码=%s；客服原话=%s" % (_codes2, _ask[:32])])

    # 30.8 归因纪律：画像自己没这条信息 → 责任在用例，不许记到系统头上。
    _sess = {"id": "V30X", "closed": False, "blocked_by_missing_fact": ["phone_tail"],
             "turns": [{"user": {"action": "cant_remember", "text": "我记不住"},
                       "agent": {"reply_text": "您再想想？", "stage": "IDENTITY",
                                 "next_stage": "IDENTITY", "intent": "unclear",
                                 "slots": {}}}]}
    _res = attribution.analyse_session(_sess, {"goal": "confirm", "expect": {"closed": True}})
    check("用例自己缺信息时不冤枉系统（fault=case 而不是 agent）",
          _res["fault"] == "case",
          ["fault=%s（缺的字段：%s）" % (_res["fault"], _sess["blocked_by_missing_fact"])])

    # 30.8a 误闭环要能被证实，不能只记 unexplained：期望是"不该闭环"，
    # 实际产生了回写，就是客服擅自回写（工单里多一条"商家承诺整改"）。
    _close_turns = [{"user": {"action": "reask_already_told", "text": "我刚不是说了吗"},
                     "agent": {"reply_text": "好的，我记一下。", "stage": "CONFIRM",
                               "next_stage": "WRITEBACK", "intent": "consent",
                               "slots": {}}}]
    _res = attribution.analyse_session(
        {"id": "V30C", "closed": True, "blocked_by_missing_fact": [], "turns": _close_turns},
        {"goal": "offtopic", "expect": {"closed": False}})
    _codes = [d["code"] for d in _res["defects"]]
    check("期望不闭环却回写了 → 记 agent_premature_close（不再只记 unexplained）",
          attribution.AGENT_PREMATURE_CLOSE in _codes and _res["fault"] == "agent",
          ["fault=%s；缺陷码=%s" % (_res["fault"], _codes)])

    # 30.8b 但客户最后明确说"清楚了"时不能扣帽子：可能是画像被说服了，
    # 那是用例期望该改。判据宁可漏，不可冤枉。
    _consent_turns = [{"user": {"action": "confirm", "text": "清楚了，我按你说的弄"},
                       "agent": {"reply_text": "好的，都记下了。", "stage": "CONFIRM",
                                 "next_stage": "WRITEBACK", "intent": "consent",
                                 "slots": {}}}]
    _res = attribution.analyse_session(
        {"id": "V30D", "closed": True, "blocked_by_missing_fact": [], "turns": _consent_turns},
        {"goal": "offtopic", "expect": {"closed": False}})
    check("客户最后明确表态同意的回写不算「擅自回写」（用例期望待复核）",
          attribution.AGENT_PREMATURE_CLOSE not in [d["code"] for d in _res["defects"]],
          ["fault=%s；缺陷码=%s" % (_res["fault"],
                                    [d["code"] for d in _res["defects"]])])

    # 30.8c 客户要重听、客服答"确认不到/转专员"：闭环了也记，不许藏在通过里。
    _repair_turns = [{"user": {"action": "mishear", "text": "啊？信号不好，你再说一遍"},
                      "agent": {"reply_text": "这个我这边确认不到，稍后让专员给您回电说明。",
                                "stage": "OFFER", "next_stage": "OFFER",
                                "intent": "ask", "slots": {}}}]
    _codes = [d["code"] for d in attribution.analyse_turns(_repair_turns)]
    check("客户要重说、客服回「确认不到/转专员」→ 记 agent_repair_ignored",
          attribution.AGENT_REPAIR_IGNORED in _codes, ["缺陷码=%s" % _codes])

    # 30.8d 性格层会在句尾挂「说联系人姓名也行」，不能被当成"又在要姓名"。
    _suffix_turn = [{"user": {"action": "answer_name", "text": "我叫晏青梧"},
                     "agent": {"reply_text": "核对上了，感谢配合。说联系人姓名也行。",
                               "stage": "IDENTITY", "next_stage": "NEED",
                               "intent": "ack", "slots": {}}}]
    _codes = [d["code"] for d in attribution.analyse_turns(_suffix_turn)]
    check("「核对上了…说联系人姓名也行」不算槽位丢失（性格后缀假阳）",
          attribution.AGENT_SLOT_LOSS not in _codes, ["缺陷码=%s" % _codes])

    # 30.8e 对抗集必须有"听不清就要重说"这类客户，否则 repair.* 只能靠轨迹判断。
    _pp = json.load(open(os.path.join(ROOT, "eval", "personas_adversarial.json"),
                         encoding="utf-8"))
    _a17 = next((x for x in _pp.get("personas", []) if x.get("id") == "A17"), None)
    check("对抗画像含「听不清就要重说」（traits.mishear_every≥2）",
          bool(_a17) and int((_a17.get("traits") or {}).get("mishear_every") or 0) >= 2,
          ["A17 traits=%s" % (_a17 and _a17.get("traits"))])

    # 30.8f 「确认不到」+ 问的是本单事实（号码哪来的）→ 判没回答。
    # 客服话里带「官方号码」会命中 ask_why 的关键词，旧判据因此把它算成答了来源质疑。
    from modules import feedback as _fb
    _jt = [{"user": {"action": "ask_why", "text": "你哪来的我号码，怎么信你"},
            "agent": {"reply_text": "这个我这边确认不到，稍后让专员用官方号码给您回电说明。",
                      "stage": "CONFIRM", "next_stage": "CONFIRM", "intent": "ask"}}]
    _j = _fb.judge_turn(_jt, 0)
    check("「确认不到」+ 问号码来源 → 判没回答（不被「官方号码」蒙过）",
          "no_answer" in _j["codes"],
          ["极性=%s 码=%s" % (_j["polarity"], _j["codes"])])

    # 30.8g 但客户问的是画外问题（后台哪个入口）时，答不上来后体面收线是对的，
    # 不能横扫成缺陷——否则优化器会去追一个改不动、也不该改的数。
    _jt2 = [{"user": {"action": "ask_why", "text": "这个得去后台哪个入口弄"},
             "agent": {"reply_text": "您问的入口我这边确认不到，稍后专员会用官方号码联系您。",
                       "stage": "CONFIRM", "next_stage": "DONE", "intent": "ask"}}]
    _j2 = _fb.judge_turn(_jt2, 0)
    check("「确认不到」+ 画外问题（后台入口）→ 不横扫成缺陷",
          "no_answer" not in _j2["codes"],
          ["极性=%s 码=%s" % (_j2["polarity"], _j2["codes"])])

    # 30.8h NEED 阶段第二次追问必须正面答，不能改口去催"清楚了"。
    # 11-10 实测 A11/A16 第 4 轮：客户没拿到材料清单，被催一句就回"清楚了"，
    # 电话闭环了，问题一个字没答——这类"配合式确认"最容易被当成好数据。
    # 前面的证据把场景切走过，这里显式切到 pop 场景再切回，否则读到的是
    # 售后场景的 NEED（它没有 reply_application_detail，会测到别的分支）。
    _cl = dialogue.config_loader
    _prev_scene = _cl.scene_path()
    _ap = (tools.get_application("QZ202611060001").get("application") or {})
    if _ap.get("company_name"):
        try:
            _cl.set_scene("pop_reject")
            _sN = dialogue.new_session("V30N")
            _sN["stage"] = "NEED"
            _sN["slots"]["identity_confirmed"] = True
            _sN["slots"]["application"] = _ap
            _sN["official_line_spoken"] = True
            _q1 = "那我具体要补什么材料"
            dialogue._stage_need(_sN, _q1, dialogue.classify(_q1))   # 第一次：官方话术
            _q2 = "急，那我具体要补什么材料"
            _oN = dialogue._stage_need(_sN, _q2, dialogue.classify(_q2))
            _xN = _oN.get("reply_text") or ""
            _mat = (_ap.get("required_materials") or [""])[0]
            check("NEED 第二次追问材料 → 正面给清单（不再催「清楚了」）",
                  bool(_mat) and _mat in _xN and "解释清楚了吗" not in _xN
                  and (_oN.get("next_stage") or "") != "CONFIRM",
                  ["回复=%s｜next=%s" % (_xN[:40], _oN.get("next_stage"))])
            _q3 = "你哪来的我号码，怎么信你"
            _oN2 = dialogue._stage_need(_sN, _q3, dialogue.classify(_q3))
            _xN2 = _oN2.get("reply_text") or ""
            check("NEED 第二次被质疑号码来源 → 答来源，不回「确认不到」",
                  ("预留" in _xN2 or "申请时留" in _xN2 or "审核" in _xN2)
                  and "确认不到" not in _xN2,
                  ["回复=%s" % _xN2[:40]])
        finally:
            _cl.set_scene(_prev_scene)
    else:
        mark_uncovered("NEED 二次追问", "取不到档案，无法真跑")

    # 30.9 版本库：上线过的版本必须留快照，且补丁只能落在话术上。
    _vs_path = os.path.join(ROOT, "config", "evolution", "versions.json")
    if not os.path.exists(_vs_path):
        mark_uncovered("L2 版本库", "没有 config/evolution/versions.json（还没跑过 evolve.py）")
    else:
        with open(_vs_path, "r", encoding="utf-8") as _f:
            _vs = json.load(_f)
        _act = next((h for h in _vs.get("history", []) if h["version"] == _vs.get("active")), {})
        _patched = [p["path"] for h in _vs.get("history", [])
                    for p in ((h.get("candidate") or {}).get("patches") or [])]
        check("进化版本库有生效版本，且补丁只改话术（stages.*）",
              bool(_act) and all(p.startswith("stages.") for p in _patched),
              ["active=%s 状态=%s｜补丁路径=%s"
               % (_vs.get("active"), _act.get("status"), sorted(set(_patched)))])
        _snap_ok = all(os.path.exists(os.path.join(ROOT, "config", "evolution", "versions",
                                                   h["version"], "scene.json"))
                       for h in _vs.get("history", []) if h.get("status") != "rejected")
        check("每个有效版本都有可回滚的场景快照",
              _snap_ok,
              ["版本：%s" % [h["version"] for h in _vs.get("history", [])]])

    # 30.10 两份同场景文件不许漂移：改一份不改另一份，演示时就是"改了没生效"。
    _sc_main = os.path.join(ROOT, "config", "scene.json")
    _sc_pkg = os.path.join(ROOT, "config", "scenes", "pop_reject.json")
    if os.path.exists(_sc_main) and os.path.exists(_sc_pkg):
        with open(_sc_main, "r", encoding="utf-8") as _f:
            _a = json.load(_f).get("stages") or {}
        with open(_sc_pkg, "r", encoding="utf-8") as _f:
            _b = json.load(_f).get("stages") or {}
        _diff = sorted(k for k in set(_a) | set(_b) if _a.get(k) != _b.get(k))
        check("默认场景与 pop_reject 场景包的话术不漂移",
              not _diff,
              ["不一致的阶段：%s" % (_diff or "无")])
    else:
        mark_uncovered("双场景一致性", "缺 config/scene.json 或 scenes/pop_reject.json")

    print()
    print("=" * 74)
    print("证据 31：说话人一致性门——同人/旁人可区分，影子模式不改行为")
    print("=" * 74)

    from modules import speaker_gate as spk_mod

    check("speaker_gate 事件已进契约（不是调用处临时造的名字）",
          "speaker_gate" in contracts.EVENT_TYPES,
          ["事件全集里命中=%s" % ("speaker_gate" in contracts.EVENT_TYPES)])

    _ok_spk, _why_spk = spk_mod.available()
    _corpus = os.path.join(ROOT, "data", "speaker_corpus")
    # 显式切到赛题场景：说话人门的开关写在场景包里，
    # 前面几条断言把场景切走了，这里不切回来就会读到"没这个配置"而静默跳过。
    _spk_scene_prev = config_loader.scene_path()
    try:
        config_loader.set_scene("pop_reject")
    except FileNotFoundError:
        pass
    if not _ok_spk:
        # 缺 numpy / onnxruntime / 模型 / 标定，都属"没条件验"，不能记成 PASS。
        mark_uncovered("说话人一致性门", _why_spk)
    elif not os.path.isdir(_corpus):
        mark_uncovered("说话人一致性门", "没有 data/speaker_corpus（先跑 gen_speaker_corpus.py）")
    else:
        def _spk_pcm(rel):
            with open(os.path.join(_corpus, rel), "rb") as _f:
                _d = _f.read()
            _i, _out = 12, None
            while _i + 8 <= len(_d):
                _cid = _d[_i:_i + 4]
                _sz = struct.unpack("<I", _d[_i + 4:_i + 8])[0]
                if _cid == b"data":
                    _out = _d[_i + 8:_i + 8 + _sz]
                _i += 8 + _sz + (_sz & 1)
            return _out or b""

        _sess_spk = {}
        for _tid in ("t1", "t2", "t3"):          # 注册 3 条
            app_mod._speaker_probe(_sess_spk, _spk_pcm("Serena/%s.wav" % _tid), "asr")
        _prof = _sess_spk.get("speaker_profile")
        _same = app_mod._speaker_probe(_sess_spk, _spk_pcm("Serena/t4.wav"), "asr")
        _other = app_mod._speaker_probe(_sess_spk, _spk_pcm("Ethan/t4.wav"), "asr")
        _other2 = app_mod._speaker_probe(_sess_spk, _spk_pcm("Cherry/t4.wav"), "asr")

        check("同一人的新句子不会被判成旁人",
              _same.get("decision") in ("same", "uncertain"),
              ["判定=%s 相似度=%s（注册 %d 条）"
               % (_same.get("decision"), _same.get("score"),
                  _prof.count if _prof else 0)])
        check("换一个人说话会被判成 other（跨性别）",
              _other.get("decision") == "other",
              ["Ethan 判定=%s 相似度=%s" % (_other.get("decision"), _other.get("score"))])
        check("换一个同性别的相近音色也能判成 other",
              _other2.get("decision") == "other",
              ["Cherry 判定=%s 相似度=%s（最像的一对音色）"
               % (_other2.get("decision"), _other2.get("score"))])
        check("文字输入不走说话人门（没有音频就没有结论）",
              app_mod._speaker_probe({}, _spk_pcm("Serena/t1.wav"), "text") is None,
              ["source=text 时返回 None"])

    config_loader.set_scene(_spk_scene_prev)

    # ---- 证据 32：mock 数据包（官方 68 场景全覆盖 / 跨文件引用自洽）----
    # 起因：mock 原来只有 2 个客户 + 120 条自造驳回原因，与官方资料包的
    # 68 个关单场景对不上——演示时"说商标状态不符合要求"，系统回的是另一套说法。
    # 现在把官方表机械转成目录/话术/QA，并按目录补 68 条档案，运行时合并成 188 条。
    print()
    print("=" * 74)
    print("证据 32：mock 数据包——官方 68 场景全覆盖，合并池 188 条，跨文件引用自洽")
    print("=" * 74)

    _mb = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                       "check_mock_bundle.py")],
                         cwd=ROOT, capture_output=True, text=True, timeout=180)
    check("mock 数据包七类校验通过（文件/目录/话术QA/覆盖回溯/唯一/引用/可复算）",
          _mb.returncode == 0,
          ["退出码 %d；末行：%s" % (_mb.returncode,
                                    ((_mb.stdout or "").strip().splitlines() or ["无输出"])[-1][:80])])

    _cat = tools.reject_catalog()
    _modes = {}
    for _it in (_cat.get("items") or []):
        _modes[_it.get("close_mode")] = _modes.get(_it.get("close_mode"), 0) + 1
    check("官方关单场景 68 条，电话 54 / 京麦 14（与官方表一致）",
          _cat.get("count") == 68 and _modes == {"电话": 54, "京麦": 14},
          ["count=%s modes=%s" % (_cat.get("count"), _modes)])

    from modules.adapters.mock import _load_applications as _load_pool
    _pool = _load_pool()
    _legacy_ids = {r["application_id"] for r in
                   json.load(open(os.path.join(ROOT, "config", "mock",
                                               "applications.json"),
                                  encoding="utf-8")).get("items") or []}
    _catalog_ids = {r.get("catalog_id") for r in _pool if r.get("catalog_id")}
    check("合并池 = 原集 120 + 官方场景 68 = 188，且原集顺序在前（名单取第一条不变）",
          len(_pool) == 188 and _pool[0]["application_id"] in _legacy_ids
          and len(_catalog_ids) == 68,
          ["池=%d 首条=%s 覆盖场景=%d"
           % (len(_pool), _pool[0]["application_id"], len(_catalog_ids))])

    _dup = {f: len([r[f] for r in _pool]) - len({r[f] for r in _pool})
            for f in ("application_id", "phone_tail", "company_name", "contact_phone")}
    check("合并池四组关键字段全局唯一（单号/尾号/公司名/电话）",
          not any(_dup.values()), ["重复计数=%s" % _dup])

    _ord = tools.query_order("C1001")
    _cus = tools.query_customer("8866")
    check("客户/订单来自完整 mock（12 客户·16 订单），订单取最近一笔",
          _cus.get("found") and _cus["customer"]["customer_id"] == "C1001"
          and (_ord.get("order") or {}).get("order_id") == "O20261101001",
          ["客户=%s 订单=%s"
           % (_cus.get("customer", {}).get("customer_id") if _cus.get("found") else None,
              (_ord.get("order") or {}).get("order_id"))])

    _sc = tools.reject_script("KB-POP-001")
    _qa_hit = tools.lookup_qa("我在哪可以查询到这个商标状态")
    _qa_miss = tools.lookup_qa("今天天气不错")
    check("驳回话术按场景 id 可取；QA 命中给官方原话，不相关就返回未找到（不编）",
          _sc.get("found") and _sc["scene"]["id"] == "KB-POP-001"
          and _qa_hit.get("found") and _qa_hit.get("source") == "特殊场景"
          and not _qa_miss.get("found") and not _qa_miss.get("answer"),
          ["script=%s QA命中=%s 未命中=%s"
           % (_sc.get("found"), _qa_hit.get("qid"), _qa_miss.get("found"))])

    from modules.adapters.real import RealAdapter as _RealAd
    _base_default = _RealAd({})
    check("真实适配器没接这几张表时返回默认空结果，不抛异常（不会拖垮一通电话）",
          _base_default.reject_catalog() == {"found": False, "items": [], "count": 0}
          and _base_default.reject_script("x")["found"] is False
          and _base_default.lookup_qa("x")["found"] is False,
          ["默认返回可用"])

    # ---- 证据 33：对抗跑批按场景分批 + 收尾话术不带补救语（11-09）----
    # 起因是 11-09 线上实测（train API）暴露的两件事，都不在单条用例的射程里：
    #
    # 1) 对抗跑批把 340 条 C4（POP 关单场景）用例混进默认售后场景跑，全部卡在
    #    IDENTITY，跑出 31/374 = 8.3%；同一批数据切到 pop_reject 是 340/340。
    #    这个数字会直接喂给 CI 卡口（--fail-under），错一次就是"系统看起来很烂"。
    #    它属于**跑批工具的属性**，任何一条用例都测不出来，只能单独守。
    #
    # 2) A01 第 9 轮客户已经说"清楚了，我按你说的弄"，客服回
    #    "…感谢您的配合，再见。我放慢一点说。"——补救语挂到了告别语上。
    #    只有"客户被听成听不清 + 这一轮正好收尾"同时成立才出现，
    #    普通用例不会踩到这个组合。
    print()
    print("=" * 74)
    print("证据 33：对抗跑批按场景分批，收尾话术不带补救语")
    print("=" * 74)

    _si = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                       "test_scene_isolation.py")],
                         cwd=ROOT, capture_output=True, text=True, timeout=180)
    check("场景隔离 / 收尾话术 / 修复请求边界（12 条自测）",
          _si.returncode == 0,
          ["退出码 %d；末行：%s" % (_si.returncode,
                                    ((_si.stdout or "").strip().splitlines() or ["无输出"])[-1][:80])])

    # 修复请求的边界：只收带重说动作的说法。
    # 「不是说了吗」曾经被收进来，评审实测把对抗集从 16/16 拉到 14/16
    # （A09/A14 复述打断确认计数 → unknown 误闭环），已撤回；这条守住别再加回去。
    _repair_ok = (policy.is_repair_request("信号不好，你再说一遍")
                  and not policy.is_repair_request("我刚不是说了吗，就那几个数"))
    check("修复请求只认重说动作：「不是说了吗」是抱怨不是修复（已被 16/16→14/16 实测否掉）",
          _repair_ok,
          ["classic=%r plead=%r" % (policy.is_repair_request("信号不好，你再说一遍"),
                                    policy.is_repair_request("我刚不是说了吗，就那几个数"))])

    # ---- 官方 QA 接进对话（11-08）----------------------------------------
    # 上面那条验的是"适配器查得到"，这三条验的是"查到了之后在对话里怎么用"：
    # 该逐字播的原话播了没有、该闭嘴的占位答案闭嘴了没有、提问有没有被当成表态。
    _ds = dialogue.new_session("verify-qa-offer")
    _ds["stage"] = "OFFER"
    _ds["slots"]["identity_confirmed"] = True
    _dq = dialogue.respond(_ds, "在职证明有哪些")
    _dq2 = dialogue.respond(_ds, "在职证明有哪些")
    check("官方 QA 接进对话：OFFER 提问逐字播原话、带溯源、不推进阶段；同一句不复读",
          _dq["intent"] == "qa" and (_dq.get("qa") or {}).get("id") == "QA-HF-003"
          and "企微认证" in _dq["reply_text"]
          and _dq["next_stage"] == "OFFER"
          and not _dq2.get("qa")
          and "企微认证" not in _dq2["reply_text"],
          ["intent=%s qa=%s 阶段=%s 二次qa=%s"
           % (_dq["intent"], (_dq.get("qa") or {}).get("id"),
              _dq["next_stage"], _dq2.get("qa"))])

    _dc = dialogue.new_session("verify-qa-confirm")
    _dc["stage"] = "CONFIRM"
    _dc["slots"]["identity_confirmed"] = True
    _dcq = dialogue.respond(_dc, "发票必须要盖开店公司公章吗")
    check("确认环节提问不计入含糊次数，答完把确认问题放回去（提问不是表态）",
          _dc["confirm_asks"] == 0
          and (_dcq.get("qa") or {}).get("id") == "QA-HF-007"
          and "清楚了吗" in _dcq["reply_text"]
          and _dcq["next_stage"] == "CONFIRM",
          ["confirm_asks=%d qa=%s"
           % (_dc["confirm_asks"], (_dcq.get("qa") or {}).get("id"))])

    _dp = dialogue.new_session("verify-qa-placeholder")
    _dp["stage"] = "OFFER"
    _dp["slots"]["identity_confirmed"] = True
    _dph = dialogue.respond(_dp, "我在哪可以查询到这个商标状态")
    _bad = ("XX", "【", "】")
    import json as _json
    with open(os.path.join(ROOT, "config", "mock", "qa.json"), encoding="utf-8") as _f:
        _qa_items = (_json.load(_f).get("items") or [])
    _with_xx = [it["id"] for it in _qa_items if "XX" in (it.get("answer") or "")]
    _filled = [it for it in _qa_items if it.get("filled_placeholder")]
    check("官方 27 条 QA 答案里没有未填占位（5 条已补齐、原话留 answer_raw），补齐后可逐字播",
          not _with_xx and len(_filled) == 5
          and all(it.get("answer_raw") for it in _filled)
          and (_dph.get("qa") or {}).get("id") == "QA-TS-003"
          and "国家商标网" in _dph["reply_text"]
          and not any(m in _dph["reply_text"] for m in _bad),
          ["仍有占位=%s 补齐=%d 条 实际播报=%s"
           % (_with_xx or "无", len(_filled), (_dph.get("qa") or {}).get("id"))])

    # ---- 性格识别与针对性回复（11-08）：识别不可进化、事实不可改 ----
    from modules import persona as _pm
    check("性格识别：强信号一次定性、弱信号两次，报尾号不误判成话少",
          _pm._classify("你哪来的我号码") == "skeptical"
          and _pm._classify("你快点说") == "impatient"
          and _pm._classify("4316") is None
          and _pm._classify("嗯，明白了") == "quiet"
          and (lambda x: (_pm.observe(x, "嗯"), x["persona_code"])[1] == "neutral")({})
          and (lambda x: (_pm.observe(x, "你再说一遍"), x["persona_code"])[1]
               == "hard_of_hearing")({}),
          ["8 类词表 + 弱信号阈值"])

    _ps = {"slots": {"application": {"company_name": "北京市柏庐商贸有限公司"}}}
    _ps.update({"persona_code": "impatient", "persona_conf": 1.0})
    _base = "北京市柏庐商贸有限公司的申请单编号 20261101001 请核对。"
    _pn, _pa = _pm.adapt(_ps, _base, "NEED")
    check("针对性回复只改形式：公司名/单号/禁语/占位一律不被改掉",
          "北京市柏庐商贸有限公司" in _pn and "20261101001" in _pn
          and not any(m in _pn for m in ("【", "】", "XX"))
          and not any(w in _pn for w in config_loader.forbidden_words()),
          ["原句=%s 适配后=%s" % (_base[:20], _pn[:34])])
    _pf, _pfa = _pm.adapt(dict(_ps, last_reply_text=""),
                          "好的，官方口径如下。", "NEED", freeze=True)
    check("官方原话冻结：不删客套、不追加引导，逐字保留",
          not _pfa and _pf == "好的，官方口径如下。",
          ["freeze=True 时仅重复引导可用"])

    # 收尾那一轮不加任何性格后缀：11-10 实测播成「…感谢您的配合，再见。
    # 我放慢一点说。」——告别语后面挂补救语，客户听到的最后一句就是它。
    _done_s = dict(_ps, persona_code="hard_of_hearing", persona_conf=1.0,
                   slots={}, stage="WRITEBACK")
    _done_out = dialogue._reply(_done_s, "感谢您的配合，再见。",
                                next_stage="DONE", intent="consent")
    _done_txt = _done_out.get("reply_text") or ""
    _wb_out = dialogue._reply(dict(_ps, persona_code="hard_of_hearing", persona_conf=1.0,
                                   slots={}, stage="CONFIRM"),
                              "感谢您的配合，再见。", next_stage="WRITEBACK",
                              intent="consent")
    _wb_txt = _wb_out.get("reply_text") or ""
    check("收尾话术（next_stage=DONE）不加性格后缀（对照组 WRITEBACK 仍会加）",
          "放慢" not in _done_txt and "回到这笔申请" not in _done_txt
          and ("放慢" in _wb_txt or "回到这笔申请" in _wb_txt),
          ["DONE=%s｜WRITEBACK=%s" % (_done_txt[:24], _wb_txt[:24])])

    _pt = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                       "test_persona.py")],
                         cwd=ROOT, capture_output=True, text=True, timeout=240)
    _pd1 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "evolve.py"), "doctor", "--deep"],
                          cwd=ROOT, capture_output=True, text=True, timeout=240)
    _pd2 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "evolve_persona.py"), "doctor", "--deep"],
                          cwd=ROOT, capture_output=True, text=True, timeout=180)
    check("性格单测（含真跑识别一致率）+ 两条进化链体检全部通过",
          _pt.returncode == 0 and _pd1.returncode == 0 and _pd2.returncode == 0,
          ["test_persona=%d doctor(L2)=%d doctor(性格)=%d"
           % (_pt.returncode, _pd1.returncode, _pd2.returncode)])

    # ---- 官方话术填槽（11-08）：模板不许把【】/XX 播出去 ----
    _osq = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                       "check_official_scripts.py"),
                           "--self-test"],
                          cwd=ROOT, capture_output=True, text=True, timeout=180)
    check("官方话术填槽：电话单都说得出话术、京麦单不外呼、无残留占位（含 3 种反向自测）",
          _osq.returncode == 0,
          ["退出码 %d；末行：%s" % (_osq.returncode,
                                    ((_osq.stdout or "").strip().splitlines() or ["无输出"])[-1][:90])])

    from modules import official_script as _osmod
    _phone = next(a for a in _pool if a.get("close_mode") == "电话")
    _jmapp = next(a for a in _pool if a.get("close_mode") == "京麦")
    _scr = _osmod.script_for(_phone) or {}
    _dneed = dialogue.new_session("verify-official-need")
    _dneed["stage"] = "NEED"
    _dneed["slots"]["identity_confirmed"] = True
    _dneed["slots"]["application"] = _phone
    _dout = dialogue.respond(_dneed, "为什么会驳回")
    check("对话里讲驳回原因用官方话术（槽位已按档案填好），京麦单不配电话话术",
          bool(_scr.get("script")) and not _scr.get("missing")
          and not any(m in _scr["script"] for m in ("【", "】", "XX"))
          and _osmod.script_for(_jmapp) is None
          and not any(m in _dout["reply_text"] for m in ("【", "】", "XX")),
          ["话术=%s 命中=%s 对话=%s"
           % (_scr.get("id"), _dout.get("intent"), _dout["reply_text"][:40])])

    import io as _io
    import contextlib as _ctx
    from scripts import check_mock_bundle as _mbmod
    _buf = _io.StringIO()
    with _ctx.redirect_stdout(_buf):
        _self_ok = _mbmod.self_test()
    check("数据包校验器的反向自测：6 种改坏都能被抓到（校验器不假绿）",
          _self_ok, ["变异用例全部报红" if _self_ok else "有变异没被抓到"])

    # ---- 「没听明白」不是同意（11-08 S10 真实缺陷，附反向断言） ----
    # 显式切到赛题场景：confirm_yes_words（含「明白」）写在 pop_reject 场景包里，
    # 不切回来就会在主场景测，测不到这次修的否定变体。
    _nc_prev_scene = config_loader.scene_path()
    config_loader.set_scene("pop_reject")
    _nc = dialogue.new_session("verify-not-clear")
    _nc["stage"] = "CONFIRM"
    _nc["slots"]["identity_confirmed"] = True
    _r1 = dialogue.respond(_nc, "哎呀，我还是没听明白")
    _r2 = dialogue.respond(_nc, "哎呀，这我搞不懂")
    check("客户说没听明白：不回写同意，复述一次后转专员且无回写事件",
          _r1["next_stage"] == "CONFIRM"
          and not _r1.get("end_reason")
          and "清楚了吗" in _r1["reply_text"]
          and _nc["slots"].get("consent") in ("", None)
          and bool(_r2.get("end_reason"))
          and _nc["slots"].get("consent") in ("", None),
          ["一轮=%s 二轮=%s consent=%r"
           % (_r1["next_stage"], _r2.get("end_reason"), _nc["slots"].get("consent"))])

    _yc = dialogue.new_session("verify-yes-clear")
    _yc["stage"] = "CONFIRM"
    _yc["slots"]["identity_confirmed"] = True
    _ry = dialogue.respond(_yc, "我明白了，清楚了")
    check("反向：真说清楚了仍按同意回写（修复没把肯定也挡掉）",
          _ry["intent"] == "consent" and _yc["slots"].get("consent") is True,
          ["intent=%s consent=%r" % (_ry["intent"], _yc["slots"].get("consent"))])

    # 客户还在提问/质疑/重复给信息时，不许把「没表态」记成 unknown 回写收线
    # （11-08 对抗集 A03/A06/A07/A10 逐轮复现的真实缺陷）。
    _eg = dialogue.new_session("verify-engaged")
    _eg["stage"] = "CONFIRM"
    _eg["slots"]["identity_confirmed"] = True
    _eg["slot_history"]["tail"] = ["0086"]
    _e1 = dialogue.respond(_eg, "那我具体要补什么材料")
    _e2 = dialogue.respond(_eg, "你哪来的我号码，怎么信你")
    check("客户还在提问：两次也不回写 unknown，转专员收线",
          not _e1.get("end_reason")
          and bool(_e2.get("end_reason"))
          and _eg["slots"].get("consent") in ("", None),
          ["一轮 end_reason=%r 二轮 end_reason=%r consent=%r"
           % (_e1.get("end_reason"), _e2.get("end_reason"),
              _eg["slots"].get("consent"))])
    config_loader.set_scene(_nc_prev_scene)

    # 反向（L07 十轮改口用例）：明确表态优先于「还在谈」判定。
    # 端到端由 run_eval 的 L07 守（88/88）；这里直接测纯函数，是刻意做的
    # 变异靶子——把这条排除规则删掉，本断言必须当场报红。
    _lf = dialogue.new_session("verify-free-yes")
    _lf["stage"] = "CONFIRM"
    _lf["slots"]["identity_confirmed"] = True
    _yes = {"ask": True, "yes": True}
    _no = {"ask": True, "yes": False}
    _a = dialogue._still_engaged(_lf, "那还是要吧", _yes)
    _b = dialogue._still_engaged(_lf, "那还是要吧", _no)
    check("反向：明确表态优先于「还在谈」判定（L07 不回归）",
          not _a and _b,
          ["yes=True → %s（应 False）；yes=False → %s（应 True）" % (_a, _b)])

    # 客户在确认阶段报**新**单号（对抗集 A15 的形状）：必须接住并留在
    # CONFIRM，不许被当成含糊攒两次回写 unknown 工单。
    _of = dialogue.new_session("verify-order-fact")
    _of["stage"] = "CONFIRM"
    _of["slots"]["identity_confirmed"] = True
    _o1 = dialogue.respond(_of, "单号后六位QZ202611060001")
    check("客户报新单号：接住并重问确认，不产生回写",
          not _o1.get("end_reason") and _o1["next_stage"] == "CONFIRM"
          and _of["slots"].get("consent") in ("", None)
          and ("收到" in _o1["reply_text"] or "记下" in _o1["reply_text"]),
          ["next_stage=%s consent=%r reply=%s"
           % (_o1["next_stage"], _of["slots"].get("consent"),
              _o1["reply_text"][:30])])
    _o2 = dialogue.respond(_of, "我刚不是说了吗，单号就是QZ202611060001")
    check("连续报信息：接住话术交替不复读，且不追加「要信息型」后缀",
          not _o2.get("end_reason")
          and _o2["reply_text"][:12] != _o1["reply_text"][:12]
          and "对照申请单编号核实" not in _o2["reply_text"],
          ["两轮主干是否相同=%s 后缀是否漏拦=%s"
           % (_o2["reply_text"][:12] == _o1["reply_text"][:12],
              "对照申请单编号核实" in _o2["reply_text"])])

    # 性格适配层守卫：客户刚递上信息的那一轮，「要信息型」后缀不许追加
    # （11-08 A15：skeptical 的 suffix 紧跟在客户报完单号之后，被对抗器
    # 判成 slot_loss）。反向：没递信息时不拦，suffix 照常追加。
    _pf = dialogue.new_session("verify-persona-factack")
    _pf["persona_code"] = "skeptical"
    _base = "好的，收到。咱们回到正题，您可以简单回我一句清楚了或者还有疑问。"
    _pf["fact_ack_turn"] = True
    _g1, _a1 = _pm.adapt(_pf, _base, "CONFIRM")
    _pf.pop("fact_ack_turn")
    _g2, _a2 = _pm.adapt(_pf, _base, "CONFIRM")
    check("适配层：客户刚给信息时不追加要信息型后缀（反向：平时照常追加）",
          "对照申请单编号核实" not in _g1 and "对照申请单编号核实" in _g2,
          ["拦后=%r 放后=%r" % (_g1[-16:], _g2[-16:])])

    # ---- 真实通话挖掘出来的三处缺陷（11-09，证据来自 data/events.db） ----
    # 1) 核身前用户问「为什么驳回 / 要补什么材料」：不能答（会泄露别人申请信息），
    #    但必须说明为什么先核身份；旧实现拿「号码是预留联系方式」挡回去，
    #    用户连问两轮、客服两轮一字不差（真实会话 s_32c93 / s_a1fd）。
    _id = dialogue.new_session("verify-before-identity-ask")
    _id["stage"] = "IDENTITY"
    _id["slots"]["identity_confirmed"] = False
    _i1 = dialogue.respond(_id, "为什么会驳回")
    _i2 = dialogue.respond(_id, "那我要补什么材料")
    check("核身前问驳回原因/材料：说明为什么先核身份，且两轮不复读",
          "核对身份" in _i1["reply_text"] and "才能讲" in _i1["reply_text"]
          and _i1["reply_text"] != _i2["reply_text"]
          and "号码是商家入驻时预留的联系方式" not in _i1["reply_text"],
          ["一轮=%s" % _i1["reply_text"][:26], "二轮=%s" % _i2["reply_text"][:26]])

    # 2) 沉默：交错的沉默（沉默→说话→沉默）不许每次念同一句，总次数到上限收尾。
    #    真实会话 s_a63565 里「您还在吗」一字不差出现 4 次。
    _sm = dialogue.new_session("verify-silence-vary")
    _sm["stage"] = "IDENTITY"
    _s1 = dialogue.respond(_sm, "")
    dialogue.respond(_sm, "在")
    _s2 = dialogue.respond(_sm, "")
    dialogue.respond(_sm, "嗯")
    _s3 = dialogue.respond(_sm, "")
    check("交错沉默：第二次换说法，第三次总次数到上限收尾",
          _s1["reply_text"] != _s2["reply_text"]
          and bool(_s3.get("end_reason")) and _s3["next_stage"] == "DONE",
          ["一轮=%s" % _s1["reply_text"][:18], "二轮=%s" % _s2["reply_text"][:18],
           "三轮 end=%r" % _s3.get("end_reason")])

    # 3) 终态里连续提问答不上：不许把「如有疑问请拨热线」逐字念三遍
    #    （真实会话 s_69aca7 里念了三遍，用户以为电话卡住）。
    _dn = dialogue.new_session("verify-done-ask-vary")
    _dn["stage"] = "DONE"
    _dn["slots"]["consent"] = "unknown"
    _dn["writeback_version"] = 1
    _d1 = dialogue.respond(_dn, "你们公司在几楼？")
    _d2 = dialogue.respond(_dn, "你们食堂几点开饭？")
    check("终态连续答不上的提问：第二句换说法，不逐字复读热线话术",
          _d1["reply_text"] != _d2["reply_text"],
          ["一句=%s" % _d1["reply_text"][:20], "二句=%s" % _d2["reply_text"][:20]])

    # 4) 真实审计器的反向自测（造缺陷必须抓到、干净通话必须不报），
    #    与 scripts/mine_calls.py check 同一套判据，接进全量自检防退化。
    import subprocess as _sp
    _mr = _sp.run([sys.executable, os.path.join("scripts", "mine_calls.py"), "check"],
                  cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  capture_output=True, text=True, timeout=120)
    check("真实通话审计器自测（11 项）全过", _mr.returncode == 0,
          [(_mr.stdout or "").strip().splitlines()[-1:] or ["无输出"]][0])

    # ---- PR #42 收线覆盖（合并审查后补的守卫） ----
    import ws_channel as _wsc
    check("PR #42：挂断/拒绝/转专员等所有收尾出口都会触发服务端收线",
          _wsc.should_auto_hangup({"hangup": True})
          and _wsc.should_auto_hangup({"end_reason": "hangup"})
          and _wsc.should_auto_hangup({"end_reason": "unclear"})
          and not _wsc.should_auto_hangup({"reply_text": "好的，我核对一下"}),
          ["终态/挂断/转专员=true，普通轮=false"])

    class _FakeEndBackend:
        def detach(self, sess):
            pass

        def end_call(self, sid):
            pass

    _wsx = _wsc.WsCallSession(_FakeEndBackend(), send_text=lambda _t: None,
                              close=lambda: None)
    _wsx._schedule_hangup("done")
    _timer_set = _wsx._hangup_timer is not None
    _wsx.close()
    check("PR #42：连接关闭时撤销收线定时器，避免重复记 call_ended",
          _timer_set and _wsx._hangup_timer is None,
          ["关闭前=%s 关闭后=%s" % (_timer_set, _wsx._hangup_timer)])

    _adv = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "adversarial_evolve.py"), "check"],
                          cwd=ROOT, capture_output=True, text=True, timeout=180)
    check("对抗闭环反向自测：确定性/不编事实/闸门第六道/静态校验/接口约束全过",
          _adv.returncode == 0,
          ["退出码 %d；末行：%s"
           % (_adv.returncode,
              ((_adv.stdout or "").strip().splitlines() or ["无输出"])[-1][:80])])

    # ---- 逐轮反馈闭环（11-09）：判据、四道验收、日志结构、隔离 ----
    # 单独一条断言，理由和对抗链一样：自测里既有"必须退回"的反例，也有一次
    # 真跑两轮的迷你训练；只要判据被放宽或验收被跳过，这里就会红。
    _se = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                       "self_evolve.py"), "check"],
                         cwd=ROOT, capture_output=True, text=True, timeout=420)
    _se_last = ((_se.stdout or "").strip().splitlines() or ["无输出"])[-1][:80]
    check("逐轮反馈闭环自测：判据/四道验收/日志结构/不写线上配置全过",
          _se.returncode == 0, ["退出码 %d；末行：%s" % (_se.returncode, _se_last)])

    # 两份场景文件必须同源：只改一份是这台机器最容易踩的坑。
    # 比整份相等更严——整份不等可能只差 nlg.provider（刻意的），
    # 所以只比"线上真正会读到"的两块：stages 与 evolution_policy。
    _sc_a = json.load(open(os.path.join(ROOT, "config", "scenes", "pop_reject.json"),
                           encoding="utf-8"))
    _sc_b = json.load(open(os.path.join(ROOT, "config", "scene.json"), encoding="utf-8"))
    _drift = [k for k in ("stages", "evolution_policy")
              if _sc_a.get(k) != _sc_b.get(k)]
    check("两份场景文件的 stages / evolution_policy 一致（只改一份等于没改）",
          not _drift, ["不一致：%s" % "、".join(_drift) if _drift else "一致"])

    # ---- 双击入口（11-08 晚）：.bat 里混进一个中文字符，cmd 会吞掉后续命令 ----
    # 真踩过：把「自进化.bat」写进注释后，整份批处理被逐行拆碎执行。
    def _bat_bad(raw):
        return raw.startswith(b"\xef\xbb\xbf") or any(x > 127 for x in raw)

    _bats = sorted(f for f in os.listdir(ROOT) if f.lower().endswith(".bat"))
    _bad_bats = [f for f in _bats
                 if _bat_bad(open(os.path.join(ROOT, f), "rb").read())]
    _probe_ok = (_bat_bad("rem 中文".encode("utf-8"))
                 and not _bat_bad(b"rem ascii\n"))
    check("Windows 双击入口：所有 .bat 纯 ASCII 无 BOM（含中文探针反向自测）",
          bool(_bats) and not _bad_bats and _probe_ok,
          ["检查 %d 个；异常：%s；探针=%s"
           % (len(_bats), "、".join(_bad_bats) or "无",
              "能抓到" if _probe_ok else "失效")])

    # ---- 证据 34：三批新数据/模板的自测，以及它们各自最容易烂掉的地方 ----
    # 职业画像 12 条、京东人物库 320 条、双端界面模板，三个都自带 unittest，
    # 但都不在 verify 里——改坏数据或生成器，总闸门照样全绿。
    # 仓库自己写过这条规矩（证据 16 / 18：「不在 verify 里等于测试没进质量体系」）。
    print()
    print("=" * 74)
    print("证据 34：职业画像 / 京东人物库 / 双端界面模板的自测全部通过")
    print("=" * 74)

    for _suite in ("test_personas_profession", "test_persona_jd", "test_ui_template"):
        _path = os.path.join(ROOT, "scripts", _suite + ".py")
        _run = subprocess.run([sys.executable, _path], cwd=ROOT,
                              capture_output=True, text=True, timeout=300)
        _out = ((_run.stdout or "") + (_run.stderr or "")).strip().splitlines()
        check("%s.py 通过（数据/生成器/模板被改坏时这里会红）" % _suite,
              _run.returncode == 0,
              ["退出码 %d；末行：%s" % (_run.returncode, (_out or ["(无输出)"])[-1][:80])])

    # 职业画像：目标与期望必须自洽，而且要有留出集。
    # J07 初版 goal=refuse 却 expect.closed=true，客户第 1 轮说「不需要」→ 必败，
    # 归因 sim——数据错误穿上「客服不行」的外衣，改话术永远救不回来。
    _pro = json.load(open(os.path.join(ROOT, "eval", "personas_profession.json"),
                          encoding="utf-8"))["personas"]
    _contra = [p["id"] for p in _pro
               if p["goal"] in ("refuse", "ask") and p["expect"]["closed"]]
    _hold = sum(1 for p in _pro if p["split"] == "holdout")
    check("职业画像：goal=refuse/ask 不期望闭环，且有留出集（12 条全 train 等于没验收）",
          not _contra and _hold >= 1,
          ["矛盾 %s｜holdout %d 条" % (_contra or "无", _hold)])

    # 京东人物库：320 条必须两两不同。
    # 初版四个维度周期都是 180，idx 与 idx+180 逐字节相同（140 条副本只改了 id），
    # 文档却写「全量唯一」——答辩现场一复算就穿。
    _jd = json.load(open(os.path.join(ROOT, "eval", "personas_jd.json"),
                         encoding="utf-8"))["personas"]
    _keys = set()
    for _p in _jd:
        _keys.add(json.dumps({k: v for k, v in _p.items() if k != "id"},
                             ensure_ascii=False, sort_keys=True))
    _frag = [p["profession"] for p in _jd if p["profession"].isascii()]
    check("京东人物库：去 id 后两两不同，职业无「4S/UI」这类被空格切开的碎片",
          len(_keys) == len(_jd) and not _frag,
          ["唯一 %d/%d｜碎片 %s" % (len(_keys), len(_jd), sorted(set(_frag)) or "无")])

    # 文档编号不能撞：PR47 曾在 25 号上与《25_噪声与降噪》撞车，合并后同一句
    # 「看 25 号文档」会指到错的，而且没人会注意到。
    import re as _re
    _nums = {}
    for _f in os.listdir(os.path.join(ROOT, "docs")):
        _m = _re.match(r"^(\d+)_", _f)
        if _m:
            _nums.setdefault(_m.group(1), []).append(_f)
    _dup = {k: v for k, v in _nums.items() if len(v) > 1}
    check("docs 文档编号不撞号", not _dup,
          ["重号：%s" % _dup if _dup else "共 %d 个编号，无重号" % len(_nums)])

    print()
    print("=" * 74)
    print("证据 35：客户数据注入 / 模型客户 / 客服表达层（全离线，不联网）")
    print("=" * 74)
    print("守什么：四道闸真的拦得住（编造数字、跨动作族、出戏、禁语、丢公司名）、")
    print("        模型不可用时真的静默降级、档案卡的「客户不知道什么」没漏进简报。")
    print("为什么不在这里真调模型：自检每次改代码都要跑，一旦联网，红绿就跟着配额走，")
    print("        那种红没有诊断价值。真实效果由 scripts/selfplay.py 出报告（docs/40）。")

    _env_off = dict(os.environ)
    _env_off["LLM_DISABLE"] = "1"
    _env_off.pop("LLM_MAX_CALLS", None)

    _r35 = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "test_llm_layer.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=240, env=_env_off)
    _last35 = ((_r35.stderr or "").strip().splitlines() or [""])[-1]
    check("大模型接入层离线自测全过（闸门 / 降级 / 受知边界）",
          _r35.returncode == 0 and _last35.startswith("OK"),
          ["退出码 %d；末行：%s" % (_r35.returncode, _last35[:80])])

    _r36 = subprocess.run(
        [sys.executable, "-m", "modules.customer_data", "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    check("档案卡与源数据一致（改了 mock 数据却没重编译，这里会红）",
          _r36.returncode == 0,
          ["退出码 %d；%s" % (_r36.returncode, (_r36.stdout or "").strip()[:120])])

    # 这一条是"没有 key 的队友 clone 下来能不能跑"的实证：
    # LLM_DISABLE=1 + 骨架话术，一通电话必须能从头走到尾。
    _r37 = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "selfplay.py"),
         "--calls", "1", "--assist", "off", "--nlg", "template", "--quiet"],
        cwd=ROOT, capture_output=True, text=True, timeout=180, env=_env_off)
    check("模型被关掉时自对练仍能跑完一通（客户自动退回规则层，不抛异常）",
          _r37.returncode == 0 and "闭环" in (_r37.stdout or ""),
          ["退出码 %d；%s" % (_r37.returncode,
                             ((_r37.stdout or "") or (_r37.stderr or ""))[-200:])])

    # ---- 训练回放与"AI 用户"合并（11-11）----
    # 用户的原话是"把 AI 用户和训练回放结合，不要再单设一个另外的面"。
    # 合并之后有三件事会悄悄退化，普通用例都测不到：
    #   ① 画像又变成随机抽商户——客户报的号码和客服手上的档案对不上；
    #   ② 首页又长出一块新面板，两个入口跑同一件事、数字各说各的；
    #   ③ 模型一关，这块面板直接白屏（它现在是线上公开链接的主功能区）。
    _tp = app_mod._train_personas()
    _tp_ok = bool(_tp) and all(p.get("merchant") for p in _tp)
    check("训练回放：每个对抗画像都绑定确定的一家商户（复跑只差「改了什么」）",
          _tp_ok, ["%d 条画像；缺失商户：%s" % (
              len(_tp), [p.get("id") for p in _tp if not p.get("merchant")][:5])])

    _html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    check("首页只有一块「训练回放」，第二个自对练面板已合并掉",
          "trainUser" in _html and "selfplayCard" not in _html
          and "/api/selfplay" not in _html,
          ["trainUser=%s selfplayCard=%s api=%s" % (
              "trainUser" in _html, "selfplayCard" in _html,
              "/api/selfplay" in _html)])

    from scripts import adversarial_evolve as _adv_v
    _p01 = [p for p in _adv_v.load_personas() if p["id"] == "A01"][0]
    _train_out = app_mod._train_run(_p01, user="rule")     # 规则版，不联网
    check("模型关掉时「训练回放」仍能跑完一场（客户退回规则层，逐轮轨迹完整）",
          bool(_train_out.get("turns")) and _train_out.get("user_mode") == "rule"
          and all((t.get("user") or {}).get("text") for t in _train_out["turns"])
          and _train_out.get("card_id"),
          ["轮数 %d；mode=%s；card=%s；end_by=%s" % (
              len(_train_out.get("turns") or []), _train_out.get("user_mode"),
              _train_out.get("card_id"), _train_out.get("end_by"))])

    print()
    print("=" * 74)
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("结果：%d/%d 通过" % (passed, total))
    if uncovered:
        # 未覆盖不是通过。单独打出来，避免"没验"被读成"验过了"。
        print("另：%d 项**未被验证**（不计入通过数，也不算失败）：" % len(uncovered))
        for _n, _r in uncovered:
            print("    - %s：%s" % (_n, _r))
    if passed != total:
        print("有断言失败，说明代码与预期不符 —— 以这里为准，不要信文档。")
        return 1
    print("全部通过。这只证明上面这些行为是真的，不代表整个系统都完成了：")
    print("  已经是真的：云端识别（qwen3-asr-flash）、云端合成（qwen3-tts-flash）、")
    print("            采集分帧上传、墙钟闸门、断连/429 重试、合成失败回落、")
    print("            长句切段只等首段、三段式打断（疑似压低音量→探针识别→认出文字才停）、")
    print("            服务端端点检测（Z6：Silero / 谱特征双引擎，九类噪声 0/9 触发）、")
    print("            实时字幕（Z7：paraformer-realtime-v2 默认开启，半句覆盖不重复）、")
    print("            前端谱特征 VAD + 40ms 开口确认（V1 参数在 15 场景上标定）、")
    print("            LLM 改写接缝、S2 WebSocket 双向通道、")
    print("            任务2 七项（知识库检索/兜底细分/置信度分级/槽位追问/改口/记忆/T17 指标）、")
    print("            L2 自进化（模拟用户受知/受听/受耐心、失败归因三分类、")
    print("                     话术候选双跑闸门、版本快照与指针回滚）、")
    print("            mock 数据包（官方 68 关单场景目录/52 条话术/27 条 QA、")
    print("                     188 条档案、12 客户/16 订单，跨文件一致性可复算）。")
    print("  仍然是占位的：语义级合规（S3 能力已接、默认关闭）、")
    print("            说话人分离（**影子模式已接**：WeSpeaker 嵌入判“是不是刚才那位”，")
    print("            只在事件里记录、不拦截；没接 enforce，因为还没有真人双人语料，见 docs/33）、")
    print("            nlg 改写默认关闭（provider=template；开启后受账号配额与\"期限表述\"规则影响）、")
    print("            真流式 TTS（现在是分段合成，不是首包流式）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
