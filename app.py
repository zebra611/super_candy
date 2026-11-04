"""app.py —— 哑链路入口：一个只用标准库的 HTTP 服务。

跑起来：
    python app.py              # 默认 http://127.0.0.1:8000
    python app.py 9000         # 换端口

它做四件事：
    1. 把 web/index.html 发给浏览器（前端终端）；
    2. 提供 /api/* 接口，串起"假语音 → 规则编排 → 假工具 → 真事件流"；
    3. 把每一轮的时延、事件与成本估算写进 data/events.db，供质检与评测读取；
    4. 管住会话的边界：token、并发、TTL、容量与成本闸门。

刻意不做的事（骨架阶段）：
    - 不做 WebSocket / WebRTC（S1 先用 HTTP 分片承载音频流，S2 再换长连接）；
    - 不引入任何第三方库（FastAPI/uvicorn 等都先不上），保证 clone 下来就能跑。

后续待补充：
    - TODO S2：把 /api/audio/chunk 换成 /ws/call 的二进制帧，接收与 seq 校验逻辑复用；
    - TODO S2 之后才有"服务端主动下发"，打断（cancel 播报）要等它；
    - TODO 加简单的访问日志与错误日志文件（当前只打到控制台）。
"""

import base64
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import config_loader
import contracts
import events
from modules import dialogue, eval as eval_mod, tools, voice

_ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(_ROOT, "web")

# 单次上传音频的大小上限（字节）。防止误传大文件把内存打爆。
MAX_AUDIO_BYTES = 8 * 1024 * 1024

# 内存里的会话状态：session_id -> session dict。
# 进程重启即丢失——骨架阶段可以接受，真实实现要落库。
_SESSIONS = {}
# 只保护 _SESSIONS 这个字典本身（增删改查）。不保护对话处理：
# 同一会话的并发由 busy 标志挡住，不同会话本来就该并行。
_STORE_LOCK = threading.Lock()


class PayloadTooLarge(Exception):
    """请求体超过上限。单独一个异常是为了返回 413，而不是含糊的 400。"""


# ---------------------------------------------------------------------------
# 会话边界：token、并发、TTL、容量
# ---------------------------------------------------------------------------

def _purge_sessions():
    """清理过期会话：TTL 到期 + 超出上限时淘汰最久没动的。

    为什么必须做：浏览器关掉页面不会调 /api/call/end，会话就会一直留在内存里。
    骨架阶段不清理也能跑，但长期开演示或压测时会把内存慢慢吃满。
    正在处理中的会话（busy=True）不清理，否则会把别人正在说的那一轮丢掉。
    """
    cfg = config_loader.budget_config()
    ttl = float(cfg.get("session_ttl_sec", 1800))
    limit = int(cfg.get("max_sessions", 200))
    now = time.time()

    with _STORE_LOCK:
        for sid in [s for s, v in _SESSIONS.items()
                    if not v.get("busy") and now - v.get("last_active_ts", now) > ttl]:
            _SESSIONS.pop(sid, None)

        overflow = len(_SESSIONS) - limit
        if overflow > 0:
            idle = sorted((s for s, v in _SESSIONS.items() if not v.get("busy")),
                          key=lambda s: _SESSIONS[s].get("last_active_ts", 0))
            for sid in idle[:overflow]:
                _SESSIONS.pop(sid, None)


def _acquire(sess):
    """占住这个会话。已被占住就返回 False（调用方回 409）。

    为什么要有这个：同一通电话并行来两个 /api/turn（用户狂点、或前端 bug
    重复发送），两条请求会同时改状态机，导致跳环节、事件顺序错乱。
    宁可让第二个请求失败，也不要让状态机串台。
    """
    with _STORE_LOCK:
        if sess.get("busy"):
            return False
        sess["busy"] = True
        return True


def _release(sess):
    """释放会话并刷新活跃时间（TTL 从这里算）。"""
    with _STORE_LOCK:
        sess["busy"] = False
        sess["last_active_ts"] = time.time()


def _auth(session_id, token):
    """校验会话存在且 token 匹配，返回 (session, error_dict, http_code)。

    为什么用会话级 token 而不是全局密码：这通电话的数据只该给发起它的人看。
    全局密码一旦写进前端就等于公开；会话 token 只活在这一次通话里，
    别人捡到 session_id 也读不到姓名与尾号。用 compare_digest 是为了避免
    按字符比较泄露长度与位置信息（虽然这里风险很低，但成本几乎为零）。

    token 缺失时**一律拒绝**，不做"本地开发就放行"的例外——
    例外条件最容易在部署时忘记关。
    """
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return None, {"error": "UNKNOWN_SESSION", "session_id": session_id}, 400
    if not secrets.compare_digest(str(sess.get("read_token", "")), str(token or "")):
        return None, {"error": "FORBIDDEN", "detail": "缺少或错误的 token"}, 403
    return sess, None, 200


# ---------------------------------------------------------------------------
# 小工具：隐私打码、成本估算、闸门判断
# ---------------------------------------------------------------------------

def _mask_digits(text):
    """把连续 4 位以上的数字打码后再入库。

    用户的手机尾号是身份凭证，事件库拿来做分析就够了，不需要保留原文。
    注意：只影响**入库的事件**，对话逻辑用的仍是内存里的原文——
    如果连逻辑一起打码，身份核验会因为"查 **** 查不到"而永远失败。
    """
    if not config_loader.privacy_config().get("mask_digits_in_events", True):
        return text
    return re.sub(r"\d{4,}", lambda m: "*" * len(m.group()), text or "")


def _budget_reason(sess):
    """判断这通电话是否已经超出容量/时长闸门，返回原因（空串表示没超）。

    只在"到点"时收尾，不做中途打断：用户正在说话时突然掐掉是最差的体验。
    所以判断放在每一轮开始，超了就礼貌收尾，本轮不再走业务逻辑。
    """
    cfg = config_loader.budget_config()
    max_turns = int(cfg.get("max_turns", 0) or 0)
    if max_turns and sess["turn"] >= max_turns:
        return "max_turns"
    max_sec = float(cfg.get("max_call_seconds", 0) or 0)
    if max_sec and time.time() - sess["created_ts"] > max_sec:
        return "max_call_seconds"
    return ""


def _accrue_cost(sess, audio_bytes, reply_text):
    """按配置单价估算本轮成本，累到会话上，返回本轮增量（元）。

    这是估算，不是账单：用来在演示里回答"这通电话花了多少钱"，
    以及防止死循环把当天额度烧完。真实成本以云厂商账单为准。
    大模型按每轮约 1500 token 粗估——够用来告警，不适合用来对账。
    """
    cfg = config_loader.budget_config()
    usage = sess.setdefault("usage", {"asr_sec": 0.0, "tts_chars": 0,
                                      "turns": 0, "cost_cny": 0.0})
    # 16kHz / 16bit 单声道 ≈ 32 字节每毫秒
    asr_sec = len(audio_bytes) / 32000.0 if audio_bytes else 0.0
    tts_chars = len(reply_text or "")

    usage["asr_sec"] += asr_sec
    usage["tts_chars"] += tts_chars
    usage["turns"] += 1

    turn_cost = (asr_sec / 60.0 * float(cfg.get("price_per_min_asr", 0))
                 + tts_chars / 1000.0 * float(cfg.get("price_per_1k_chars_tts", 0))
                 + 1500 / 1000000.0 * float(cfg.get("price_per_1m_tokens", 0)))
    usage["cost_cny"] = round(usage["cost_cny"] + turn_cost, 6)

    warn = float(cfg.get("daily_warn_cny", 0) or 0)
    if warn and usage["cost_cny"] > warn and not sess.get("cost_warned"):
        sess["cost_warned"] = True
        print("[预算告警] 会话 %s 累计估算成本 %.4f 元，已超过 %.2f 元阈值"
              % (sess["session_id"], usage["cost_cny"], warn))
    return round(turn_cost, 6)


# ---------------------------------------------------------------------------
# 音频流接收（S1）：浏览器分帧上传 PCM
# ---------------------------------------------------------------------------
# 这一段是"1 号任务"在服务端的接缝：浏览器用 AudioWorklet 把麦克风切成
# 20ms / 320 采样点的 PCM16 帧，每 8 帧（160ms）打一个 HTTP 分片上来，
# 服务端按 seq 校验连续性并累积到会话缓冲，一轮结束时整段交给 ASR。
#
# 为什么现在不用 WebSocket：S1 的目标是先把"分帧 + 边采边发 + 帧号不跳"
# 这件事本身跑通并可验证。S2 会把这条 HTTP 分片换成 /ws/call 的二进制帧，
# **下面这三个函数（_audio_state / ingest_audio_chunk / flush_audio）原样复用**，
# 只有搬进搬出的通道变了。
#
# 已知边界（都是 S2 才解决的，别在这里花时间）：
#   - 每 160ms 一个 HTTP 请求，有 TCP 与头部开销，比不上长连接；
#   - 没有服务端主动下发的通道，所以做不了"服务端 cancel 播报"（打断要用它）；
#   - 半双工：一轮说完才交给 ASR，不是真的边识别边出字。

def _audio_state(sess):
    """取（或惰性创建）这个会话的音频缓冲状态。

    缓冲有上限：一轮说话累计不超过 MAX_AUDIO_BYTES，
    否则一个异常前端（或忘了 stop 的页面）就能把内存吃满。
    """
    st = sess.get("audio")
    if st is None:
        st = {
            "expect_seq": 0,     # 下一片应当出现的 seq，用来发现丢片
            "frames": 0,         # 已收帧数（20ms 一帧）
            "samples": 0,        # 已收采样点数（16k → 每帧 320）
            "gaps": 0,           # 序号不连续的次数
            "duplicates": 0,     # 重复分片的次数（seq 倒退）
            "buf": bytearray(),  # 累积的 PCM16LE 原始字节
            "started_ts": 0.0,
        }
        sess["audio"] = st
    return st


def ingest_audio_chunk(sess, seq, pcm, sample_rate=contracts.PCM_SAMPLE_RATE):
    """收一片音频：校验格式与序号后追加到会话缓冲。

    参数：
        sess         会话字典（由调用方做鉴权后传入）
        seq          这一片的第一帧序号，从 0 开始由前端递增
        pcm          PCM16LE 单声道原始字节
        sample_rate  采样率，必须等于契约值，否则拒绝
    返回：dict(seq, bytes, received_frames, received_samples, gaps, duplicates)
    异常：ValueError（参数不合法，调用方转 400）；PayloadTooLarge（累计超限，转 413）
    """
    if sample_rate != contracts.PCM_SAMPLE_RATE:
        raise ValueError("采样率必须为 %d，收到 %d" % (contracts.PCM_SAMPLE_RATE, sample_rate))
    if not pcm:
        raise ValueError("空音频分片")
    if len(pcm) % contracts.PCM_SAMPLE_WIDTH:
        raise ValueError("PCM16 字节数必须为偶数，收到 %d" % len(pcm))

    st = _audio_state(sess)
    if len(st["buf"]) + len(pcm) > MAX_AUDIO_BYTES:
        raise PayloadTooLarge("本通电话累计音频超过 %d 字节" % MAX_AUDIO_BYTES)

    # 加锁的理由：HTTP 每个请求一个线程，两片并发到达时 seq 比较与 extend
    # 会交错，可能出现"先到的被当成重复丢掉"。这把锁只保护这几行，
    # 不涉及识别与对话，所以不会拖慢正常通话。
    with _STORE_LOCK:
        if seq < st["expect_seq"]:
            # 重复分片（重试或前端 bug）：重复追加会在音频里叠出一段回声，
            # 重复的采样还会让识别结果出现叠字。宁可丢弃并计数，也不盲信。
            st["duplicates"] += 1
        else:
            if seq > st["expect_seq"]:
                # 序号跳了：说明前面的分片丢了。不在这里补，但要记下来——
                # "帧号不跳"是 S1 的验收标准，掩盖丢帧等于验收造假。
                st["gaps"] += 1
            frames = len(pcm) // contracts.FRAME_BYTES
            st["buf"].extend(pcm)
            st["samples"] += len(pcm) // contracts.PCM_SAMPLE_WIDTH
            st["frames"] += frames
            st["expect_seq"] = seq + frames
            if not st["started_ts"]:
                st["started_ts"] = time.time()

        return {"seq": seq, "bytes": len(pcm),
                "received_frames": st["frames"], "received_samples": st["samples"],
                "gaps": st["gaps"], "duplicates": st["duplicates"]}


def flush_audio(sess, dropped=0):
    """把会话缓冲里的 PCM 取出来、清空，返回 (bytes, 统计)。

    每轮说话结束时调用一次。统计会写成 audio_stream 事件，
    它是"这一轮到底采到了多少声音、有没有丢片"的唯一证据。

    参数：
        dropped  前端因背压主动丢弃的帧数（前端自己报，服务端无法感知）
    """
    st = _audio_state(sess)
    pcm = bytes(st["buf"])
    stat = {
        "bytes": len(pcm),
        "samples": st["samples"],
        "frames": st["frames"],
        "duration_ms": round(st["samples"] * 1000.0 / contracts.PCM_SAMPLE_RATE, 1),
        "gaps": st["gaps"],
        "duplicates": st["duplicates"],
        "dropped_at_client": int(dropped or 0),
        "sample_rate": contracts.PCM_SAMPLE_RATE,
        "frame_ms": contracts.FRAME_MS,
    }
    st["buf"] = bytearray()
    st["expect_seq"] = 0
    st["frames"] = 0
    st["samples"] = 0
    st["gaps"] = 0
    st["duplicates"] = 0
    st["started_ts"] = 0.0
    return pcm, stat


# ---------------------------------------------------------------------------
# 业务流程：一轮对话的完整链路
# ---------------------------------------------------------------------------

def handle_turn(session_id, user_text=None, audio_bytes=b"", audio_streamed=False,
                audio_dropped=0):
    """执行一轮对话：这是整个哑链路的核心，也是五个人将来各自替换的接缝。

    链路（每一步都写一条事件，方便事后归因）：
        ① 记本轮开始
        ② 闸门检查（轮次/时长超限就礼貌收尾，不再走业务）
        ③ VAD 判定（假）              ← 角色 1 负责替换
        ④ ASR 出文本（假）            ← 角色 1 负责替换
        ⑤ 编排生成回复（规则状态机）   ← 角色 2 负责替换
        ⑥ 需要就调业务工具（假数据）   ← 角色 2/4 负责替换
        ⑦ 合规护栏（命中整句替换）     ← 角色 3 负责替换
        ⑧ TTS 指令（前端播报）        ← 角色 1 负责替换
        ⑨ 记本轮结束、耗时分段与成本

    异常处理原则：单轮里任何一段抛异常，都记一条 error 事件并给一句兜底话术，
    **不让整通电话断掉**。外呼里中途崩掉，用户听到的是静音，比说错话更糟。

    参数：
        session_id      通话 ID
        user_text       文字模式下用户直接输入的内容（可空）
        audio_bytes     语音模式下的音频字节（PCM16 / 16k / 单声道）
        audio_streamed  为 True 表示音频已按帧上传过，本轮从会话缓冲取，
                        此时 audio_bytes 参数被忽略（前端不再整段上传）
        audio_dropped   前端因背压主动丢弃的帧数，仅用于记账
    返回：dict，前端需要的全部内容
    """
    t_start = time.time()
    sess = _SESSIONS[session_id]
    turn_id = sess["turn"] + 1

    # S1：音频若是按帧上来的，先把会话缓冲取出来。
    # 必须放在 turn_start 之前——否则 turn_start 里的 audio_bytes 会记成 0，
    # 而"这一轮有没有声音"正是它要回答的问题。
    audio_stat = None
    if audio_streamed:
        audio_bytes, audio_stat = flush_audio(sess, dropped=audio_dropped)

    events.write_event(session_id, "turn_start",
                       {"audio_bytes": len(audio_bytes), "text_input": bool(user_text),
                        "audio_streamed": bool(audio_streamed)},
                       turn_id=turn_id)

    if audio_stat is not None:
        # 单独记一条：这是 S1 的验收证据（帧数/采样数/序号缺口/客户端丢帧）
        events.write_event(session_id, "audio_stream", audio_stat, turn_id=turn_id)

    llm_ms = 0.0
    asr_ms = 0.0
    heard = ""
    tool_result = None

    forced = _budget_reason(sess)
    if forced:
        # 闸门收尾：这不是用户的意思，所以不写业务结论，只写一条 error 说明原因
        events.write_event(session_id, "error",
                           {"code": "BUDGET_" + forced.upper(), "stage": sess["stage"]},
                           turn_id=turn_id)
        source = "budget"
        agent_out = dialogue.forced_end(sess, forced)
    else:
        # ③ VAD：只有真的收到音频才做端点检测。
        # 文字输入不写 vad_result——否则 VAD 准确度统计里会混进一堆"没声音"的轮次。
        if audio_bytes:
            vad = voice.vad_detect(audio_bytes)
            events.write_event(session_id, "vad_result", vad, turn_id=turn_id)

        # ④ 三条输入路径分开处理，绝不能把"用户没说话"伪造成为一句话：
        #    有音频   → 真语音识别（fake 由识别实现自己声明）
        #    有文字   → 打字调试（不产生 fake，也不消耗识别时长）
        #    两者都空 → 用户沉默，记 no_answer，交给编排决定重问还是挂断
        is_fake = False
        provider = ""
        silence_reason = "empty_input"
        typed = (user_text or "").strip()
        if audio_bytes:
            asr_out = voice.asr(audio_bytes, hints=config_loader.asr_hints())
            heard = asr_out["text"]
            asr_ms = asr_out["latency_ms"]
            is_fake = bool(asr_out.get("fake", False))
            provider = asr_out.get("provider", "")
            if asr_out.get("error"):
                # 识别失败要留痕，否则用户抱怨"它总说听不清"时查不出原因
                events.write_event(session_id, "error",
                                   {"code": "ASR_FAILED", "detail": str(asr_out["error"])[:200],
                                    "provider": provider}, turn_id=turn_id)
            # 有音频但一个字都没识别出来 = 用户按了说话却没出声（或识别失败）。
            # 这不是"用户说了一句空话"，必须按沉默处理，
            # 否则会把空字符串当成有效输入推进状态机——那是缺陷 3 的老路。
            if heard.strip():
                source = "asr"
            else:
                source = "silence"
                silence_reason = "no_speech"
        elif typed:
            heard = typed
            source = "text"
        else:
            source = "silence"

        if source == "silence":
            events.write_event(session_id, "no_answer",
                               {"reason": silence_reason, "stage": sess["stage"]},
                               turn_id=turn_id)
        else:
            payload = {"text": _mask_digits(heard), "latency_ms": asr_ms,
                       "fake": is_fake, "source": source}
            if provider:
                payload["provider"] = provider
            events.write_event(session_id, "asr_result", payload,
                               turn_id=turn_id, latency_ms=asr_ms)

        # ⑤ 编排：吃用户话，吐回复（可能附带工具调用）
        t_llm = time.time()
        try:
            agent_out = dialogue.respond(sess, heard)   # 会就地推进 sess 的状态机
        except Exception as exc:
            events.write_event(session_id, "error",
                               {"code": "DIALOGUE_FAILED", "detail": str(exc)[:200],
                                "stage": sess["stage"]}, turn_id=turn_id)
            agent_out = dialogue.safe_fallback(sess)
        llm_ms = round((time.time() - t_llm) * 1000, 2)

        # ⑥ 工具：只有编排给出 tool_call 时才触发（走到回写时触发一次，改口再触发）
        tc = agent_out.get("tool_call")
        if tc:
            events.write_event(session_id, "tool_call", tc, turn_id=turn_id)
            try:
                tool_result = tools.dispatch(tc)
                # 写操作失败重试一次：幂等键不变，所以不会重复建单（幂等的用处就在这）
                if tc.get("name") == "create_ticket" and not (tool_result or {}).get("ticket_id"):
                    tool_result = tools.dispatch(tc)
                    tool_result["retried"] = True
            except Exception as exc:
                events.write_event(session_id, "error",
                                   {"code": "TOOL_FAILED", "detail": str(exc)[:200],
                                    "tool": tc.get("name")}, turn_id=turn_id)
                tool_result = {"error": "TOOL_FAILED", "_ms": 0.0}

            events.write_event(session_id, "tool_result", tool_result,
                               turn_id=turn_id, latency_ms=tool_result.get("_ms", 0))
            # 工单真的建出来了，才算"结构化结果回写"完成——这是闭环的唯一硬证据
            if tc.get("name") == "create_ticket" and tool_result.get("ticket_id"):
                events.write_event(session_id, "writeback",
                                   {"ticket_id": tool_result["ticket_id"],
                                    "created": tool_result.get("created"),
                                    "updated": tool_result.get("updated", False),
                                    "version": tool_result.get("version", 1),
                                    "retried": tool_result.get("retried", False)},
                                   turn_id=turn_id)
                sess["closed"] = True

    # ⑦ 合规护栏：播出去之前先过滤。
    # 回写失败时不能播"已为您登记"——那是虚假承诺，必须换成兜底话术。
    # 注意：这里要用 tool_call 判断，不能用 agent_out["stage"]，
    # 因为回写那一轮所处的阶段是 CONFIRM/OFFER，不是 WRITEBACK。
    tc = agent_out.get("tool_call") or {}
    if tc.get("name") == "create_ticket" and not (tool_result or {}).get("ticket_id"):
        agent_out["reply_text"] = dialogue.fallback_text("reply_fail")
    guard = dialogue.compliance_guard(agent_out["reply_text"])
    reply_text = guard["text"]

    events.write_event(session_id, "agent_reply",
                       {"text": reply_text, "stage": agent_out["stage"],
                        "next_stage": agent_out["next_stage"],
                        "intent": agent_out.get("intent", "unknown"),
                        "source": source,
                        "blocked": guard["blocked"], "hits": guard["hits"],
                        "replaced": guard["replaced"]},
                       turn_id=turn_id, latency_ms=llm_ms)

    # ⑧ TTS：只等第一段就返回，剩余段在后台合成、由前端播完首段后来取。
    # 为什么必须这样：分段并发如果仍然等全部段落合成完，响应时间不会变，
    # 用户照样要等 1.6 秒——分段就成了只让内部指标好看的花架子。
    tts_out = voice.tts(reply_text, wait_first_only=True)
    rest_handle = tts_out.pop("_rest", None)
    if rest_handle:
        sess.setdefault("tts_rest", {})[turn_id] = rest_handle
    events.write_event(session_id, "tts_start",
                       {k: v for k, v in tts_out.items() if not k.startswith("_")},
                       turn_id=turn_id)

    # ⑨ 收尾：处理耗时与"服务端下界首字"都写进 turn_end。
    # server_first_audio_ms 是"请求到达 → 首包就绪"，不含网络回程与浏览器起播，
    # 所以它是首字响应的下界，不是用户感知值；真正的 T4−T0 由前端上报。
    server_first_audio_ms = round((time.time() - t_start) * 1000, 2)
    total_ms = round((time.time() - t_start) * 1000, 2)
    turn_cost = _accrue_cost(sess, audio_bytes, reply_text)

    events.write_event(session_id, "turn_end",
                       {"stage": agent_out["next_stage"], "closed": sess["closed"],
                        "source": source, "processing_ms": total_ms,
                        "server_first_audio_ms": server_first_audio_ms,
                        "asr_ms": asr_ms, "llm_ms": llm_ms,
                        # tts_ms 是服务端真的等了多久（分段合成要等全部段落），
                        # 与 processing_ms 同一口径；tts_first_ms 才是"第一段可播"
                        # 的耗时，也就是用户实际开始听到声音的那一段。
                        # 两个数会差一倍以上，只看一个会误判。
                        "tts_ms": tts_out.get("total_ms", tts_out.get("latency_ms", 0)),
                        "tts_first_ms": tts_out.get("latency_ms", 0),
                        "tts_segments": len(tts_out.get("segments") or []),
                        "est_cost_cny": turn_cost},
                       turn_id=turn_id, latency_ms=total_ms)
    sess["turn"] = turn_id

    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "user_text": heard,
        "reply_text": reply_text,
        "stage": agent_out["stage"],
        "next_stage": agent_out["next_stage"],
        "intent": agent_out.get("intent", "unknown"),
        "tool_result": tool_result,
        "closed": sess["closed"],
        "slots": dict(sess["slots"]),
        "latency": {
            "asr_ms": asr_ms,
            "llm_ms": llm_ms,
            "tts_ms": tts_out.get("total_ms", tts_out.get("latency_ms", 0)),
            "tts_first_ms": tts_out.get("latency_ms", 0),
            "total_ms": total_ms,
            # 服务端下界，不是门槛口径；前端的实测值通过 /api/turn/metric 回传
            "server_first_audio_ms": server_first_audio_ms,
        },
        "tts": tts_out,
        "compliance": {"blocked": guard["blocked"], "hits": guard["hits"],
                       "replaced": guard["replaced"]},
        "usage": dict(sess.get("usage") or {}),
    }


def start_call():
    """开一通新电话：生成 session_id 与会话级 token、初始化状态、记一条 call_started。

    返回的 read_token 是这通电话的读取凭证：前端拿它查事件链与报告。
    它只存在于这次通话，不进日志、不进仓库。
    """
    _purge_sessions()

    session_id = "s_" + uuid.uuid4().hex[:12]
    sess = dialogue.new_session(session_id)
    sess["read_token"] = secrets.token_urlsafe(12)
    sess["created_ts"] = time.time()
    sess["last_active_ts"] = time.time()
    sess["busy"] = False
    sess["usage"] = {"asr_sec": 0.0, "tts_chars": 0, "turns": 0, "cost_cny": 0.0}

    with _STORE_LOCK:
        _SESSIONS[session_id] = sess

    events.write_event(session_id, "call_started",
                       {"scene": config_loader.scene_name()})

    # 外呼由客服先开场：自报身份是六环节的第一环，也是合规要求。
    # 以前开场白只在"回应用户第一句"时才会出现，用户抢先报尾号就被整段跳过。
    guard = dialogue.compliance_guard(dialogue.opening_line())
    opening = guard["text"]
    events.write_event(session_id, "agent_reply",
                       {"text": opening, "stage": "GREETING", "next_stage": "GREETING",
                        "intent": "opening", "source": "opening",
                        "blocked": guard["blocked"], "hits": guard["hits"],
                        "replaced": guard["replaced"]},
                       turn_id=0)
    # 开场白也要真合成。原来这里只把 voice.tts() 的返回值塞进事件就丢了，
    # 结果服务端花了 1 秒多、花了钱合成一段音频，前端却拿不到——
    # 用户听到的第一句永远是浏览器机械音，而后面的句子是真声音，非常割裂。
    # 同样只等首段：点"开始通话"后不该让用户干等整句 28 个字念完。
    opening_tts = voice.tts(opening, wait_first_only=True)
    rest_handle = opening_tts.pop("_rest", None)
    if rest_handle:
        sess.setdefault("tts_rest", {})[0] = rest_handle
    events.write_event(session_id, "tts_start",
                       {k: v for k, v in opening_tts.items() if not k.startswith("_")},
                       turn_id=0)

    return {"session_id": session_id, "read_token": sess["read_token"],
            "scene": config_loader.scene_name(),
            "stages": list(contracts.STAGES),
            "gate_ms": contracts.LATENCY_GATE_MS,
            "opening": opening,
            "opening_tts": opening_tts}


def end_call(session_id):
    """挂断：记一条 call_ended，返回这通电话的简单结论（是否闭环）。

    用 pop 而不是标记删除：会话状态没有保留价值，内存里留着只会让 TTL 逻辑更复杂。
    """
    sess = _SESSIONS.get(session_id)
    closed = bool(sess and sess.get("closed"))
    usage = dict((sess or {}).get("usage") or {})
    events.write_event(session_id, "call_ended",
                       {"closed": closed, "est_cost_cny": usage.get("cost_cny", 0)})
    with _STORE_LOCK:
        _SESSIONS.pop(session_id, None)
    return {"session_id": session_id, "closed": closed, "usage": usage}


def report_client_metric(session_id, turn_id, first_audio_ms, estimate=False):
    """记录浏览器实测的首字响应（T4 − T0）。

    为什么必须由前端上报：只有浏览器知道自己什么时候真的出声。
    服务端能测的只是"请求到达 → 首包就绪"，那是下界，不是用户感知。
    也不做时钟换算——前端传的是 performance.now() 相减的**时间差**，
    所以客户端时钟不准也不会变成负数（docs/04 第 5 节那条提醒就是讲这个）。
    """
    value = float(first_audio_ms)
    if not (0 < value < 60000):
        raise ValueError("first_audio_ms 超出合理范围（0-60000ms）")

    # turn_id 必须同时写进事件列：面板和排查都按列取，只放 payload 里的活
    # 会出现"每轮都是 turn 0"的假象。payload 里也留一份，因为 metrics()
    # 是按 payload 配对浏览器值与 turn_end 的，两处必须同步。
    events.write_event(session_id, "latency_report",
                       {"turn_id": int(turn_id), "first_audio_ms": round(value, 1),
                        "estimate": bool(estimate)},
                       turn_id=int(turn_id))
    return {"ok": True, "turn_id": int(turn_id), "first_audio_ms": round(value, 1)}


# ---------------------------------------------------------------------------
# HTTP 层：路由与序列化（写得笨一点，方便看不懂框架的人直接读）
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    """所有请求的入口。路由用 if/elif 硬写，不引入路由器。"""

    server_version = "super_candy-skeleton/0.2"

    # ---- 工具方法 ----

    def _send_json(self, obj, code=200):
        """统一的 JSON 响应。中文不转义，方便直接看。"""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        """把静态文件原样发出去。每次读盘，改完前端刷新即生效，不用重启。"""
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._send_json({"error": "NOT_FOUND", "path": path}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        """读请求体并解析 JSON。空体返回空 dict，解析失败抛 ValueError。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_AUDIO_BYTES * 2:   # base64 会膨胀约 1.33 倍，留余量
            raise PayloadTooLarge("请求体超过 %d 字节" % (MAX_AUDIO_BYTES * 2))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _read_raw(self):
        """读原始二进制请求体（音频分片用）。空体返回 b""，超限抛 PayloadTooLarge。

        为什么不复用 _read_json：音频是二进制，解码成 JSON 只会报 BAD_JSON，
        而且 base64 会把体积撑大 33%，分片上传的意义就没了。
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_AUDIO_BYTES:
            raise PayloadTooLarge("单个分片超过 %d 字节" % MAX_AUDIO_BYTES)
        return self.rfile.read(length)

    def _is_loopback(self):
        """请求是否来自本机。仅用于 /api/sessions 这类运维接口。"""
        return self.client_address[0] in ("127.0.0.1", "::1")

    def log_message(self, fmt, *args):
        """压掉默认的逐请求日志，只保留我们自己关心的输出。"""
        pass

    # ---- 路由 ----

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            # 预留：将来拆出 app.js / style.css 时走这里
            name = os.path.basename(path)
            ext = os.path.splitext(name)[1].lower()
            ctype = {".js": "text/javascript", ".css": "text/css",
                     ".png": "image/png", ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
            self._send_file(os.path.join(WEB_DIR, name), ctype + "; charset=utf-8")
            return
        if path == "/api/health":
            # 健康检查不带任何业务数据，故意不加 token：部署探针要能匿名探活
            self._send_json({"ok": True, "scene": config_loader.scene_name(),
                             "gate_ms": contracts.LATENCY_GATE_MS})
            return
        if path == "/api/metrics":
            # 聚合指标（分位数、计数、成本），不含任何个人信息，保持匿名可读
            self._send_json(events.metrics())
            return
        if path == "/api/events":
            sid = (qs.get("session_id") or [""])[0]
            token = (qs.get("token") or [""])[0]
            sess, err, code = _auth(sid, token)
            if err:
                self._send_json(err, code)
                return
            self._send_json({"session_id": sid, "events": events.list_events(sid)})
            return
        if path == "/api/sessions":
            # 会话列表会暴露别人的 session_id，只允许本机运维查看
            if not self._is_loopback():
                self._send_json({"error": "FORBIDDEN",
                                 "detail": "该接口只允许本机访问"}, 403)
                return
            self._send_json({"sessions": events.recent_sessions(limit=20)})
            return
        if path == "/api/report":
            sid = (qs.get("session_id") or [""])[0]
            token = (qs.get("token") or [""])[0]
            sess, err, code = _auth(sid, token)
            if err:
                self._send_json(err, code)
                return
            self._send_json({
                "transcript": eval_mod.transcribe_session(sid),
                "compliance": eval_mod.compliance_report(sid),
                "score": eval_mod.score_session(sid),
            })
            return

        # 取分段的剩余音频。前端在首段播完后调用，播报不需要等它。
        # 这里最多等 3 秒：等不到就返回已有部分，用户听到的是一句被截断的话，
        # 但那比"播报卡住不动"好——后者看起来像程序挂了。
        if path == "/api/tts/rest":
            sid = (qs.get("session_id") or [""])[0]
            token = (qs.get("token") or [""])[0]
            sess, err, code = _auth(sid, token)
            if err:
                self._send_json(err, code)
                return
            try:
                tid = int((qs.get("turn_id") or ["0"])[0])
            except ValueError:
                self._send_json({"error": "BAD_QUERY",
                                 "detail": "turn_id 必须是整数"}, 400)
                return
            handle = (sess.get("tts_rest") or {}).pop(tid, None)
            self._send_json({"segments": voice.collect_rest(handle) if handle else []})
            return

        self._send_json({"error": "NOT_FOUND", "path": path}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        # 音频分片走二进制体，必须在解析 JSON 之前分流，否则会被判成 BAD_JSON
        if path == "/api/audio/chunk":
            self._handle_audio_chunk()
            return

        try:
            body = self._read_json()
        except PayloadTooLarge as e:
            self._send_json({"error": "PAYLOAD_TOO_LARGE", "detail": str(e)}, 413)
            return
        except Exception as e:
            self._send_json({"error": "BAD_JSON", "detail": str(e)}, 400)
            return

        if path == "/api/call/start":
            self._send_json(start_call())
            return

        if path == "/api/turn":
            self._handle_turn(body)
            return

        if path == "/api/turn/metric":
            self._handle_metric(body)
            return

        if path == "/api/call/end":
            sid = body.get("session_id") or ""
            sess, err, code = _auth(sid, body.get("token"))
            if err:
                self._send_json(err, code)
                return
            self._send_json(end_call(sid))
            return

        self._send_json({"error": "NOT_FOUND", "path": path}, 404)

    def _handle_turn(self, body):
        """一轮对话：鉴权 → 占住会话 → 处理 → 释放。"""
        sid = body.get("session_id") or ""
        sess, err, code = _auth(sid, body.get("token"))
        if err:
            self._send_json(err, code)
            return

        # 两条音频来路，二选一：
        #   audio_streamed=True → 音频已按帧传过，从会话缓冲取（S1 之后前端走这条）
        #   audio_b64           → 整段上传（保留给脚本与旧客户端，不算废弃接口）
        streamed = bool(body.get("audio_streamed"))
        audio_bytes = b""
        if not streamed and body.get("audio_b64"):
            try:
                audio_bytes = base64.b64decode(body["audio_b64"])
            except Exception as e:
                self._send_json({"error": "BAD_AUDIO", "detail": str(e)}, 400)
                return
            if len(audio_bytes) > MAX_AUDIO_BYTES:
                self._send_json({"error": "AUDIO_TOO_LARGE",
                                 "detail": "单次音频上限 %d 字节" % MAX_AUDIO_BYTES}, 413)
                return

        if not _acquire(sess):
            # 同一会话上一轮还没处理完。宁可让这一条失败，也不要让状态机串台
            self._send_json({"error": "SESSION_BUSY",
                             "detail": "上一轮还在处理，请稍后重试"}, 409)
            return
        try:
            out = handle_turn(sid, user_text=body.get("text"), audio_bytes=audio_bytes,
                              audio_streamed=streamed,
                              audio_dropped=body.get("audio_dropped") or 0)
        except Exception as e:
            # 兜底：不能让一次异常把整通电话打断
            self._send_json({"error": "TURN_FAILED", "detail": str(e)}, 500)
            return
        finally:
            _release(sess)
        self._send_json(out)

    def _handle_audio_chunk(self):
        """收一片音频（S1）：鉴权 → 校验格式/序号 → 追加到会话缓冲。

        这个接口设计成"只进不出业务"：它不推进状态机、不产生回复、不写业务事件，
        只负责把音频攒起来。这样即使前端猛推帧，也不会打乱对话流程。

        参数从 query 走而不是 JSON 体：请求体整个留给二进制 PCM，
        省掉一次 base64 编解码（体积小 33%，也少一次 CPU 拷贝）。
        """
        qs = parse_qs(urlparse(self.path).query)
        sid = (qs.get("session_id") or [""])[0]
        sess, err, code = _auth(sid, (qs.get("token") or [""])[0])
        if err:
            self._send_json(err, code)
            return

        try:
            seq = int((qs.get("seq") or ["0"])[0])
            rate = int((qs.get("rate") or [str(contracts.PCM_SAMPLE_RATE)])[0])
            if seq < 0:
                raise ValueError("seq 不能为负")
            pcm = self._read_raw()
            out = ingest_audio_chunk(sess, seq, pcm, rate)
        except PayloadTooLarge as e:
            self._send_json({"error": "AUDIO_TOO_LARGE", "detail": str(e)}, 413)
            return
        except (TypeError, ValueError) as e:
            self._send_json({"error": "BAD_CHUNK", "detail": str(e)}, 400)
            return

        self._send_json(dict(out, ok=True))

    def _handle_metric(self, body):
        """前端上报首字响应（T4−T0）。上报失败不影响通话，所以错误码给得很明确。"""
        sid = body.get("session_id") or ""
        sess, err, code = _auth(sid, body.get("token"))
        if err:
            self._send_json(err, code)
            return
        try:
            out = report_client_metric(sid, body.get("turn_id"),
                                       body.get("first_audio_ms"),
                                       bool(body.get("estimate")))
        except (TypeError, ValueError) as e:
            self._send_json({"error": "BAD_METRIC", "detail": str(e)}, 400)
            return
        self._send_json(out)


def main():
    """启动服务。端口优先取命令行参数，其次环境变量 PORT，默认 8000。

    绑定地址的选择规则（部署到云端时必须监听 0.0.0.0，否则外部访问不到）：
        - 显式给了 HOST 环境变量 → 用它；
        - 否则如果存在 PORT 环境变量（说明跑在托管沙箱里）→ 0.0.0.0；
        - 否则（本地自己跑）→ 127.0.0.1，只对本机开放，更安全。

    本地跑会自动打开浏览器；部署环境不打开（没有显示器，也没必要）。
    不想自动打开就设 NO_BROWSER=1。
    """
    events.init_db()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8000"))
    deployed = "PORT" in os.environ
    host = os.environ.get("HOST") or ("0.0.0.0" if deployed else "127.0.0.1")

    server = ThreadingHTTPServer((host, port), Handler)

    url = "http://127.0.0.1:%d" % port
    print("super_candy 哑链路已启动")
    print("  场景：%s" % config_loader.scene_name())
    print("  监听：%s:%d" % (host, port))
    print("  本地地址：%s" % url)
    print("  事件库：%s" % events.DB_PATH)
    print("  首字门槛：%d ms（口径 T4−T0，由浏览器上报）" % contracts.LATENCY_GATE_MS)
    print("  停止：Ctrl+C")

    # 延后 1.5 秒再开浏览器：服务已经在监听，页面一打开就能拿到数据，
    # 不会出现"连不上服务"的假报错。用守护线程，不影响主线程阻塞。
    if not deployed and os.environ.get("NO_BROWSER") != "1":
        def _open():
            time.sleep(1.5)
            try:
                webbrowser.open(url)
            except Exception:
                pass   # 打不开浏览器不算错误，用户手动点地址即可
        threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
