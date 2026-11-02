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

    # ② VAD：判断有没有人说话（假的，只要收到音频就算说了）
    vad = voice.vad_detect(audio_bytes)
    events.write_event(session_id, "vad_result", vad, turn_id=turn_id)

    # ③ ASR：有声就走识别，没声就用打的字（文字模式用于无麦克风调试）
    if audio_bytes:
        asr_out = voice.asr(audio_bytes, hints=[])
        heard = asr_out["text"]
        asr_ms = asr_out["latency_ms"]
    else:
        heard = (user_text or "").strip() or "（空输入）"
        asr_ms = 0.0
    events.write_event(session_id, "asr_result",
                       {"text": heard, "latency_ms": asr_ms, "fake": True},
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
        events.write_event(session_id, "tool_result", tool_result,
                           turn_id=turn_id, latency_ms=tool_result.get("_ms", 0))
        # 工单真的建出来了，才算"结构化结果回写"完成——这是闭环的唯一硬证据
        if tc["name"] == "create_ticket" and tool_result.get("ticket_id"):
            events.write_event(session_id, "writeback",
                               {"ticket_id": tool_result["ticket_id"],
                                "created": tool_result.get("created")},
                               turn_id=turn_id)
            sess["closed"] = True

    # ⑥ 合规护栏：播出去之前先过滤（真实实现要重新生成合规表达，而不是打码）
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
        "tool_result": tool_result,
        "closed": sess["closed"],
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
    """启动服务。端口优先取命令行参数，其次环境变量 PORT，默认 8000。"""
    events.init_db()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("super_candy 哑链路已启动")
    print("  场景：%s" % config_loader.scene_name())
    print("  打开：http://127.0.0.1:%d" % port)
    print("  事件库：%s" % events.DB_PATH)
    print("  停止：Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
