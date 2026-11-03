"""app.py —— 哑链路入口：一个只用标准库的 HTTP 服务。

跑起来：
    python app.py              # 默认 http://127.0.0.1:8000
    python app.py 9000         # 换端口

它做三件事：
    1. 把 web/index.html 发给浏览器（前端终端）；
    2. 提供 /api/* 接口，串起"假语音 → 假编排 → 假工具 → 真事件流"；
    3. 把每一轮的时延与事件写进 data/events.db，供质检与评测读取。

刻意不做的事（骨架阶段）：
    - 不做 WebSocket / WebRTC（真实链路才需要，先用 HTTP 半双工验证流程）；
    - 不做用户鉴权、不做并发保护、不做优雅停机；
    - 不引入任何第三方库（FastAPI/uvicorn 等都先不上），保证 clone 下来就能跑。

后续待补充：
    - TODO 换成 WebSocket 或 WebRTC 承载音频流（角色 1，对应赛题"WebRTC 网页端"）；
    - TODO turn 的音频改为流式上传，现在是一次性整段上传，无法做打断；
    - TODO 加简单的访问日志与错误日志文件（当前只打到控制台）。
"""

import base64
import json
import os
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
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 业务流程：一轮对话的完整链路
# ---------------------------------------------------------------------------

def handle_turn(session_id, user_text=None, audio_bytes=b""):
    """执行一轮对话：这是整个哑链路的核心，也是四个人将来各自替换的接缝。

    链路（每一步都写一条事件，方便事后归因）：
        ① 记本轮开始
        ② VAD 判定（假）              ← 角色 1 负责替换
        ③ ASR 出文本（假）            ← 角色 1 负责替换
        ④ 编排生成回复（假状态机）     ← 角色 2 负责替换
        ⑤ 需要就调业务工具（假数据）   ← 角色 2/4 负责替换
        ⑥ 合规护栏过滤回复            ← 角色 3 负责替换
        ⑦ TTS 指令（前端播报）        ← 角色 1 负责替换
        ⑧ 记本轮结束与总耗时

    参数：
        session_id   通话 ID
        user_text    文字模式下用户直接输入的内容（可空）
        audio_bytes  语音模式下的音频字节（哑链路不解码，只用于证明音频真的传上来了）
    返回：dict，前端需要的全部内容
    """
    t_start = time.time()
    sess = _SESSIONS[session_id]
    turn_id = sess["turn"] + 1

    events.write_event(session_id, "turn_start",
                       {"audio_bytes": len(audio_bytes), "text_input": bool(user_text)},
                       turn_id=turn_id)

    # ② VAD：只有真的收到音频才做端点检测。
    # 文字输入不写 vad_result——否则 VAD 准确度统计里会混进一堆"没声音"的轮次。
    if audio_bytes:
        vad = voice.vad_detect(audio_bytes)
        events.write_event(session_id, "vad_result", vad, turn_id=turn_id)

    # ③ 三条输入路径分开处理，绝不能把"用户没说话"伪造成为一句话：
    #    audio_bytes → 真语音识别
    #    user_text   → 打字调试（不产生 fake，也不消耗识别时长）
    #    两者都空     → 用户沉默，记一条 no_answer，交给编排决定重问还是挂断
    typed = (user_text or "").strip()
    silence = False
    if audio_bytes:
        asr_out = voice.asr(audio_bytes, hints=config_loader.asr_hints())
        heard = asr_out["text"]
        asr_ms = asr_out["latency_ms"]
        # fake 由识别实现自己声明：接上真 ASR 后它会返回 False，
        # 这里不再硬编码，否则评测会把真数据当成占位结果过滤掉。
        is_fake = bool(asr_out.get("fake", False))
        source = "asr"
    elif typed:
        heard = typed
        asr_ms = 0.0
        is_fake = False
        source = "text"
    else:
        heard = ""
        asr_ms = 0.0
        is_fake = False
        source = "silence"
        silence = True
        events.write_event(session_id, "no_answer",
                           {"reason": "empty_input", "stage": sess["stage"]},
                           turn_id=turn_id)

    if not silence:
        events.write_event(session_id, "asr_result",
                           {"text": heard, "latency_ms": asr_ms,
                            "fake": is_fake, "source": source},
                           turn_id=turn_id, latency_ms=asr_ms)

    # ④ 编排：吃用户话，吐回复（可能附带工具调用）
    t_llm = time.time()
    with _LOCK:
        agent_out = dialogue.respond(sess, heard)   # 会就地推进 sess 的状态机
    llm_ms = round((time.time() - t_llm) * 1000, 2)

    # ⑤ 工具：只有走到回写阶段才会触发（哑链路里第 6 轮触发一次）
    tool_result = None
    if agent_out.get("tool_call"):
        tc = agent_out["tool_call"]
        events.write_event(session_id, "tool_call", tc, turn_id=turn_id)
        tool_result = tools.dispatch(tc)
        # 写操作失败重试一次：幂等键不变，所以不会重复建单（幂等的用处就在这）
        if tc.get("name") == "create_ticket" and not (tool_result or {}).get("ticket_id"):
            tool_result = tools.dispatch(tc)
            tool_result["retried"] = True
        events.write_event(session_id, "tool_result", tool_result,
                           turn_id=turn_id, latency_ms=tool_result.get("_ms", 0))
        # 工单真的建出来了，才算"结构化结果回写"完成——这是闭环的唯一硬证据
        if tc["name"] == "create_ticket" and tool_result.get("ticket_id"):
            events.write_event(session_id, "writeback",
                               {"ticket_id": tool_result["ticket_id"],
                                "created": tool_result.get("created"),
                                "retried": tool_result.get("retried", False)},
                               turn_id=turn_id)
            sess["closed"] = True

    # ⑥ 合规护栏：播出去之前先过滤（真实实现要重新生成合规表达，而不是打码）
    # 回写失败时不能播"已为您登记"——那是虚假承诺，必须换成兜底话术。
    # 注意：这里要用 tool_call 判断，不能用 agent_out["stage"]，
    # 因为回写那一轮所处的阶段是 CONFIRM/OFFER，不是 WRITEBACK。
    _tc = agent_out.get("tool_call") or {}
    if _tc.get("name") == "create_ticket" and not (tool_result or {}).get("ticket_id"):
        agent_out["reply_text"] = dialogue.fallback_text("reply_fail")
    guard = dialogue.compliance_guard(agent_out["reply_text"])
    reply_text = guard["text"]

    events.write_event(session_id, "agent_reply",
                       {"text": reply_text, "stage": agent_out["stage"],
                        "next_stage": agent_out["next_stage"],
                        "blocked": guard["blocked"], "hits": guard["hits"]},
                       turn_id=turn_id, latency_ms=llm_ms)

    # ⑦ TTS：骨架阶段服务端不合成，只告诉前端"你自己念"（真实实现替换这里）
    tts_out = voice.tts(reply_text)
    events.write_event(session_id, "tts_start", tts_out, turn_id=turn_id)

    # ⑧ 收尾：总耗时写进 turn_end，metrics 的 P50/P95 就是从这里算的
    total_ms = round((time.time() - t_start) * 1000, 2)
    events.write_event(session_id, "turn_end",
                       {"stage": agent_out["next_stage"], "closed": sess["closed"]},
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
            "tts_ms": tts_out.get("latency_ms", 0),
            "total_ms": total_ms,
        },
        "tts": tts_out,
        "compliance": {"blocked": guard["blocked"], "hits": guard["hits"]},
    }


def start_call():
    """开一通新电话：生成 session_id、初始化状态、记一条 call_started。"""
    session_id = "s_" + uuid.uuid4().hex[:12]
    with _LOCK:
        _SESSIONS[session_id] = dialogue.new_session(session_id)
    events.write_event(session_id, "call_started",
                       {"scene": config_loader.scene_name()})
    return {"session_id": session_id, "scene": config_loader.scene_name(),
            "stages": list(contracts.STAGES)}


def end_call(session_id):
    """挂断：记一条 call_ended，返回这通电话的简单结论（是否闭环）。"""
    sess = _SESSIONS.get(session_id)
    closed = bool(sess and sess.get("closed"))
    events.write_event(session_id, "call_ended", {"closed": closed})
    with _LOCK:
        _SESSIONS.pop(session_id, None)
    return {"session_id": session_id, "closed": closed}


# ---------------------------------------------------------------------------
# HTTP 层：路由与序列化（写得笨一点，方便看不懂框架的人直接读）
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    """所有请求的入口。路由用 if/elif 硬写，不引入路由器。"""

    server_version = "super_candy-skeleton/0.1"

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
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

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
            self._send_json({"ok": True, "scene": config_loader.scene_name()})
            return
        if path == "/api/metrics":
            self._send_json(events.metrics())
            return
        if path == "/api/events":
            sid = (qs.get("session_id") or [""])[0]
            if not sid:
                self._send_json({"error": "MISSING session_id"}, 400)
                return
            self._send_json({"session_id": sid, "events": events.list_events(sid)})
            return
        if path == "/api/sessions":
            self._send_json({"sessions": events.recent_sessions(limit=20)})
            return
        if path == "/api/report":
            sid = (qs.get("session_id") or [""])[0]
            if not sid:
                self._send_json({"error": "MISSING session_id"}, 400)
                return
            self._send_json({
                "transcript": eval_mod.transcribe_session(sid),
                "compliance": eval_mod.compliance_report(sid),
                "score": eval_mod.score_session(sid),
            })
            return

        self._send_json({"error": "NOT_FOUND", "path": path}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except Exception as e:
            self._send_json({"error": "BAD_JSON", "detail": str(e)}, 400)
            return

        if path == "/api/call/start":
            self._send_json(start_call())
            return

        if path == "/api/turn":
            sid = body.get("session_id") or ""
            if sid not in _SESSIONS:
                self._send_json({"error": "UNKNOWN_SESSION", "session_id": sid}, 400)
                return
            audio_bytes = b""
            if body.get("audio_b64"):
                try:
                    audio_bytes = base64.b64decode(body["audio_b64"])
                except Exception as e:
                    self._send_json({"error": "BAD_AUDIO", "detail": str(e)}, 400)
                    return
            try:
                out = handle_turn(sid, user_text=body.get("text"), audio_bytes=audio_bytes)
            except Exception as e:
                # 兜底：不能让一次异常把整通电话打断（赛题要求异常兜底，这里只是最低限度）
                self._send_json({"error": "TURN_FAILED", "detail": str(e)}, 500)
                return
            self._send_json(out)
            return

        if path == "/api/call/end":
            sid = body.get("session_id") or ""
            self._send_json(end_call(sid))
            return

        self._send_json({"error": "NOT_FOUND", "path": path}, 404)


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
