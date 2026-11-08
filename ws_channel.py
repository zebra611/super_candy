"""S2 WebSocket 双向通道 —— 通话会话的协议层，与 HTTP 入口解耦。

为什么单独一个文件、还要"后端注入"：
    协议逻辑（收什么、推什么、什么顺序）和 HTTP 服务器（app.py 的
    BaseHTTPRequestHandler）必须分开，否则没法离线测试——总不能在
    verify.py 里真起一个服务器再真连一个 WebSocket 客户端。
    所以这里把"业务动作"抽象成 backend 的几个函数，由 app.py 在
    启动时注入自己的实现；verify.py 注入假实现。协议本身不依赖任何一方。

线路协议（text 帧一律 JSON，binary 帧一律 PCM16LE/16kHz/单声道）：

    前端 → 服务端：
        {"type":"start","session_id":...,"token":...}   鉴权并绑定会话
        binary                                           采集帧（替代 /api/audio/chunk）
        {"type":"turn"}                                  前端端点到了，执行一轮
        {"type":"barge_in","heard":"..."}                打断确认（替代 /api/call/barge_in）
        {"type":"ping"}                                  保活探针
    服务端 → 前端：
        {"type":"ready"}                                 绑定成功
        {"type":"turn_result",...}                       一轮的文字结果与指标（同 /api/turn 返回体，去掉 tts）
        {"type":"tts","turn_id":..,"seq":..,"text","audio_url"}   逐段合成好即推
        {"type":"tts_done","turn_id":..}                 这一段话的所有段都推完了
        {"type":"turn_end","turn_id":..,"latency":{...}} 服务端口径收尾
        {"type":"barge_ack","turn_id":..}                打断已记账、缓冲已清
        {"type":"partial","text":...}                    流式识别的半句字幕（仅 realtime 模式）
        {"type":"error","code":...,"detail":...}

与 HTTP 路线的分工（都有意保留）：
    WS 负责"音频帧上行 + 服务端主动下发"；文字输入仍走 /api/turn，
    质检/看板/运维全部不动。WS 建不起来时前端自动回落 HTTP 分片，
    行为和 S1 完全一致——S2 坏了不能把 S1 也带坏。

负责人：角色 1（实时语音与终端）
"""

import json
import threading
import time

from modules import voice
from modules import wsproto


class WsCallSession:
    """一条 WebSocket 连接对应的通话会话。协议状态都在这里，socket 不在。

    backend 约定（app.py 与 verify.py 各注入一份）：
        auth(session_id, token)        -> sess 或 None
        attach(sess, ws) / detach(sess) -> 维护 sess 上的 ws 引用（打断广播用）
        ingest(sess, pcm)              -> 收一帧音频（内部自带 seq 推进）
        handle_turn(sid, audio_streamed, asr_override) -> /api/turn 同构 dict
        take_rest(sess, turn_id)       -> 取走剩余段句柄（拿走后 HTTP 轮询就取不到了）
        barge(sid, heard, latency_ms)  -> 记 barge_in + 清缓冲（latency 可为 None=未知）
        realtime_enabled()             -> 是否启用流式识别（partial 字幕来源）
    """

    # 单帧音频上限。20ms 一帧 16k 单声道只有 640 字节，正常流量差四个数量级；
    # 这个门是防"有人拿二进制帧当上传通道灌大文件"的。
    MAX_FRAME_BYTES = 512 * 1024

    def __init__(self, backend, send_text, close=None):
        self._backend = backend
        self._send_text = send_text          # str -> None，调用方保证线程安全
        self._close = close or (lambda: None)
        # 服务端主动收线的定时器：说完结束语后等一小段（留出打断/改口的窗口），
        # 期间用户再说话就取消（新一轮 _run_turn 会 cancel）。
        self._hangup_timer = None
        self._lock = threading.Lock()        # 下行串行化：读线程与推送线程共用
        self.sess = None
        self.session_id = None
        self.alive = True
        self._rt = None                      # RealtimeAsr（可选）
        self._turn_lock = threading.Lock()   # 一条连接同一时刻只跑一轮
        self.stats = {"frames": 0, "bytes": 0, "turns": 0,
                      "errors": 0, "opened_at": time.time()}

    # ------------------------------------------------------------------
    # 下行
    # ------------------------------------------------------------------

    def send_json(self, obj):
        """推一条 JSON。连接已死或对端读不动时静默放弃——通话不该被推送卡死。"""
        if not self.alive:
            return False
        try:
            data = json.dumps(obj, ensure_ascii=False)
        except (TypeError, ValueError):
            return False
        with self._lock:
            try:
                self._send_text(data)
                return True
            except Exception:
                self.alive = False
                return False

    # ------------------------------------------------------------------
    # 上行：binary
    # ------------------------------------------------------------------

    def on_binary(self, pcm):
        """收一帧采集音频。语义与 /api/audio/chunk 相同，只是没有 seq——
        单条 TCP 连接保证有序，乱序/缺口在这一层不存在，硬造一个 seq
        反而是假审计。TCP 断线重连由前端整体重开，不在这里补帧。"""
        if not self.sess:
            self.send_json({"type": "error", "code": "NOT_STARTED",
                            "detail": "先发 start 再发音频"})
            return
        if not pcm:
            return
        if len(pcm) > self.MAX_FRAME_BYTES:
            self.send_json({"type": "error", "code": "FRAME_TOO_LARGE",
                            "detail": "单帧 %d 字节超限" % len(pcm)})
            return
        try:
            self._backend.ingest(self.sess, pcm)
            self.stats["frames"] += 1
            self.stats["bytes"] += len(pcm)
        except Exception as exc:
            # 采样率不对这类错误在 HTTP 路线会变成 400；WS 里只能原样退回，
            # 让前端的日志能看到"为什么声音不对"。
            self.stats["errors"] += 1
            self.send_json({"type": "error", "code": "AUDIO_REJECTED",
                            "detail": str(exc)[:200]})

    # ------------------------------------------------------------------
    # 上行：text（控制消息）
    # ------------------------------------------------------------------

    def on_text(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            self.stats["errors"] += 1
            self.send_json({"type": "error", "code": "BAD_JSON",
                            "detail": "控制帧不是合法 JSON"})
            return
        mtype = (msg or {}).get("type")
        handler = {
            "start": self._on_start,
            "turn": self._on_turn,
            "barge_in": self._on_barge_in,
            "ping": self._on_ping,
            "end": self._on_end,
        }.get(mtype)
        if handler is None:
            self.send_json({"type": "error", "code": "UNKNOWN_TYPE",
                            "detail": "不认识的消息类型：%r" % mtype})
            return
        try:
            handler(msg)
        except Exception as exc:
            # 一条控制消息在业务层炸掉，不该赔上整条连接——回 error 并活着。
            # 不回不活着的后果：前端卡在"思考中"，用户看到的是"客服不理人"。
            self.stats["errors"] += 1
            print("  [ws] 控制消息 %r 处理失败：%s" % (mtype, str(exc)[:160]))
            self.send_json({"type": "error", "code": "INTERNAL",
                            "detail": str(exc)[:200]})

    def _on_start(self, msg):
        if self.sess:
            self.send_json({"type": "error", "code": "ALREADY_STARTED",
                            "detail": "这条连接已经绑定过会话"})
            return
        sess = self._backend.auth(msg.get("session_id"), msg.get("token"))
        if not sess:
            self.send_json({"type": "error", "code": "AUTH_FAILED",
                            "detail": "session_id 或 token 不对，先调 /api/call/start"})
            return
        self.sess = sess
        self.session_id = msg.get("session_id")
        self._backend.attach(sess, self)
        if self._backend.realtime_enabled():
            self._rt = voice.open_realtime_asr()
        # 建不上就把原因带出去：否则前端只看到"字幕没出来"，无从判断是
        # 没密钥、模型没开通、还是公司网拦了 WSS。这条字段只用于排障，不影响协议。
        self.send_json({"type": "ready", "session_id": self.session_id,
                        "realtime": bool(self._rt),
                        "realtime_error": ("" if self._rt
                                           else voice.last_realtime_error())})

    def _on_turn(self, msg):
        if not self.sess:
            self.send_json({"type": "error", "code": "NOT_STARTED",
                            "detail": "还没绑定会话"})
            return
        # 一条连接同一时刻只跑一轮：上一轮还没回来又来 turn，多半是前端
        # 状态乱了，直接拒绝比让两轮交错写事件流诚实得多。
        if not self._turn_lock.acquire(blocking=False):
            self.send_json({"type": "error", "code": "TURN_BUSY",
                            "detail": "上一轮还没处理完"})
            return
        try:
            self._run_turn()
        finally:
            self._turn_lock.release()

    def _run_turn(self):
        """跑一轮并把结果按协议推下去。TTS 分段是"就绪即推"，不再让前端来轮询。"""
        self._cancel_hangup()      # 用户又说话了：刚才的收线计划作废
        sid = self.session_id
        override = None
        if self._rt is not None:
            final = self._rt.finish_and_collect(timeout=1.5)
            if final and final.strip():
                # 流式识别出结果就不再整段重识别——省一次调用，也省 400ms。
                override = {"text": final, "provider": "paraformer-realtime"}
            self._rt.restart()

        out = self._backend.handle_turn(sid, audio_streamed=True,
                                        asr_override=override)
        if out.get("error"):
            self.send_json({"type": "error", "code": "TURN_FAILED",
                            "detail": str(out.get("detail", ""))[:200]})
            return

        self.stats["turns"] += 1
        turn_id = out["turn_id"]
        # turn_result 不带 tts：语音下发是独立消息流，前端按"可播就播"消费
        self.send_json({"type": "turn_result",
                        "turn_id": turn_id,
                        "user_text": out.get("user_text"),
                        "reply_text": out.get("reply_text"),
                        "stage": out.get("stage"),
                        "next_stage": out.get("next_stage"),
                        "intent": out.get("intent"),
                        "tool_result": out.get("tool_result"),
                        "closed": out.get("closed"),
                        "latency": out.get("latency")})
        self.send_json({"type": "turn_end", "turn_id": turn_id,
                        "latency": out.get("latency")})

        tts = out.get("tts") or {}
        self._push_tts(turn_id, tts)
        # 剩余段：拿走句柄，交给后台线程"好了就推"。
        # HTTP 路线是前端播完首段来轮询 /api/tts/rest；WS 路线方向反过来，
        # 服务端主动给——这正是 S2 要解决"半句干等"的那一半。
        rest = self._backend.take_rest(self.sess, turn_id)
        if rest:
            threading.Thread(target=self._push_rest, args=(turn_id, rest),
                             daemon=True).start()
        else:
            self.send_json({"type": "tts_done", "turn_id": turn_id})

        # 这一轮是"说完就结束"（终态/拒绝/挂断）：给一小段缓冲后主动收线。
        # 没有这一步，通话会一直挂着——前端不点挂断就永远不结束。
        if out.get("hangup"):
            self._schedule_hangup(out.get("end_reason") or "done")

    # ---- 服务端主动收线 ---------------------------------------------------
    AUTO_HANGUP_GRACE_S = 2.5      # 结束语说完到收线的缓冲，留出"等等我改一下"的窗口

    def _schedule_hangup(self, reason):
        self._cancel_hangup()
        timer = threading.Timer(self.AUTO_HANGUP_GRACE_S, self._auto_hangup, args=(reason,))
        timer.daemon = True
        self._hangup_timer = timer
        timer.start()

    def _cancel_hangup(self):
        if self._hangup_timer is not None:
            try:
                self._hangup_timer.cancel()
            except Exception:
                pass
            self._hangup_timer = None

    def _auto_hangup(self, reason):
        """到点收线：记一条 call_ended → 通知前端 → 关连接。

        顺序不能反：先记事件再关 socket，否则前端收到 call_ended 时
        服务端可能已经把会话清了，事后查不到这通电话怎么结束的。
        """
        try:
            self._backend.end_call(self.session_id)
        except Exception:
            pass
        try:
            self.send_json({"type": "call_ended", "reason": reason})
        except Exception:
            pass
        try:
            self.close()
        except Exception:
            pass

    def _push_tts(self, turn_id, tts):
        """把 tts 结果转成协议消息。browser 回落也推——前端要的是"知道该播什么"。"""
        if tts.get("mode") == "browser":
            self.send_json({"type": "tts", "turn_id": turn_id, "seq": 0,
                            "mode": "browser", "text": tts.get("text", "")})
            self.send_json({"type": "tts_done", "turn_id": turn_id})
            return
        segs = tts.get("segments") or []
        if tts.get("mode") == "url" and tts.get("audio_url"):
            segs = [{"text": tts.get("text", ""), "audio_url": tts["audio_url"]}]
        for i, seg in enumerate(segs):
            if not seg or not seg.get("audio_url"):
                continue
            self.send_json({"type": "tts", "turn_id": turn_id, "seq": i,
                            "mode": "url", "text": seg.get("text", ""),
                            "audio_url": seg["audio_url"]})

    def _push_rest(self, turn_id, rest):
        """后台推剩余段。voice 的句柄约定：([threads], urls, parts)。"""
        try:
            threads, urls, parts = rest
            for i in range(1, len(parts)):
                deadline = time.time() + 6.0     # 单段合成最长 ~2s，6s 是三倍余量
                while urls[i] is None and time.time() < deadline:
                    if not self.alive:
                        return
                    time.sleep(0.05)
                if urls[i] is None:
                    continue                     # 这段失败：跳过，别让后面段跟着堵死
                self.send_json({"type": "tts", "turn_id": turn_id, "seq": i,
                                "mode": "url", "text": parts[i],
                                "audio_url": urls[i]})
        finally:
            self.send_json({"type": "tts_done", "turn_id": turn_id})

    def _on_barge_in(self, msg):
        if not self.sess:
            self.send_json({"type": "error", "code": "NOT_STARTED",
                            "detail": "还没绑定会话"})
            return
        latency = msg.get("latency_ms")
        if not isinstance(latency, (int, float)):
            latency = None          # 前端没带就记"未知"，不编一个数
        turn_id = self._backend.barge(self.session_id,
                                      (msg.get("heard") or "").strip(),
                                      latency_ms=latency)
        # 打断时把正在攒的实时识别也作废：那些音频属于"被打断的半句话"，
        # 混进下一轮等于让下一轮听客服自己说过的话。
        if self._rt is not None:
            self._rt.restart()
        self.send_json({"type": "barge_ack", "turn_id": turn_id})

    def _on_ping(self, msg):
        self.send_json({"type": "pong", "ts": msg.get("ts")})

    def _on_end(self, msg):
        """前端挂断或重连：清引用但不强关 socket——close 交给读循环，
        让"谁先走"都不留半开连接。"""
        if self.sess:
            self._backend.detach(self.sess)
        self.sess = None
        self.session_id = None

    # ------------------------------------------------------------------
    # 实时识别（可选链路）
    # ------------------------------------------------------------------

    def feed_realtime(self, pcm):
        """把音频也喂给流式识别（如果启用）。批式兜底模式下是空操作。"""
        if self._rt is None or not self.sess:
            return
        self._rt.feed(pcm)
        partial = self._rt.poll_partial()
        if partial:
            self.send_json({"type": "partial", "text": partial})

    def close(self):
        """连接结束：清会话引用、关流式识别。幂等，读循环和上层都可调。"""
        if not self.alive:
            return
        self.alive = False
        if self.sess:
            self._backend.detach(self.sess)
            self.sess = None
        if self._rt is not None:
            self._rt.close()
            self._rt = None
        self._close()


def serve_loop(handler, backend):
    """HTTP 升级后的读写主循环，挂在 app.py 的 handler 线程里跑。

    handler 需要：self.rfile（读）、self.headers（握手）、
    self.send_ws_text(str)（下发）、self.close_ws()。
    这些胶水都在 app.py，本函数只关心协议。
    """
    sess = WsCallSession(
        backend,
        send_text=handler.send_ws_text,
        close=handler.close_ws,
    )
    reader = wsproto.Reader(handler.rfile)
    try:
        while sess.alive:
            opcode, payload = reader.read_message()
            if opcode == wsproto.OP_CLOSE:
                break
            if opcode == wsproto.OP_PING:
                handler.send_ws_frame(wsproto.OP_PONG, payload)
                continue
            if opcode == wsproto.OP_PONG:
                continue
            if opcode == wsproto.OP_TEXT:
                sess.on_text(payload.decode("utf-8", "replace"))
            elif opcode == wsproto.OP_BIN:
                sess.feed_realtime(payload)
                sess.on_binary(payload)
    except wsproto.FrameError as exc:
        sess.send_json({"type": "error", "code": "PROTOCOL",
                        "detail": str(exc)[:200]})
    except (ConnectionError, OSError, ValueError):
        pass          # 对端断开是常态（挂断、刷新页面），不是错误
    finally:
        sess.close()
