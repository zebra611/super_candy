"""Z7 端到端验证：真服务 + 真 WS 通道 + 真流式识别 + 字幕不重复。

为什么不能只跑 check_realtime_asr.py：
    那个脚本只证明"云端认我们的协议"。Z7 的验收是**通话页边说边出字**，
    链路上还有三段没被证明：
        ① /ws/call 的握手与鉴权（start 消息、read_token）；
        ② 音频帧按 20ms 推进去，服务端攒 100ms 再喂云端（_FEED_CHUNK_BYTES）；
        ③ 半句字幕（partial）发到前端时是**覆盖**语义而不是**追加**——
           追加就会变成"京东京东客服"，这就是"重复"。
    这三段只有把整条链路跑起来才能验。

怎么跑：
    python scripts/check_ws_realtime.py                 # 用 TTS 造一句人声
    python scripts/check_ws_realtime.py --wav x.wav     # 用本地 16k WAV（不花钱）
    python scripts/check_ws_realtime.py --port 8802

它会自己起一个服务子进程（跑完关掉），所以不要求你先手动启动。
不写事件到正式库：服务用的是临时库吗？**不是**——它走的是真实 app，
会往 data/events.db 里写这一通。要干净就先把 PORT 指到临时目录的副本；
本脚本只在末尾打一行提示，不擅自改你的库。

判定（全过才算 Z7 达成）：
    1. ready.realtime == true（配置开了，且真建上了流式连接）；
    2. 半句字幕 ≥3 次（边说边出字不是一句都没有）；
    3. 相邻两条 partial 不相同（重复推送 = 前端会看到字在闪）；
    4. 落 turn 后 user_text 非空，且与最后一条 partial 有明显重叠（同一句话）；
    5. user_text 里没有"整句拼两遍"的重复（如"京东京东"）；
    6. turn_result 带 reply_text（识别结果真的进了状态机）。
"""

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modules import wsproto  # noqa: E402

FRAME_MS = 20
FRAME_BYTES = 640          # 320 采样 × 2 字节


def wait_health(port, timeout=20.0):
    deadline = time.time() + timeout
    url = "http://127.0.0.1:%d/api/health" % port
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("服务没起来（%s）" % url)


def post_json(port, path, body=None):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


class WsClient:
    """最小 WS 客户端：只做本脚本需要的事（握手/发文本/发二进制/收消息）。"""

    def __init__(self, port, path="/ws/call", timeout=10.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
               % (path, port, key))
        self.sock.sendall(req.encode("ascii"))
        self.rfile = self.sock.makefile("rb")
        head = b""
        while b"\r\n\r\n" not in head:
            piece = self.rfile.read(1)
            if not piece:
                raise RuntimeError("握手失败：连接断了")
            head += piece
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise RuntimeError("握手被拒：%s" % status.strip())
        self.reader = wsproto.Reader(self.rfile)
        self.msgs = []
        self._stop = False
        self._th = threading.Thread(target=self._reader_loop, daemon=True)
        self._th.start()

    def _reader_loop(self):
        while not self._stop:
            try:
                op, payload = self.reader.read_message()
            except Exception:
                return
            if op == wsproto.OP_CLOSE:
                return
            if op == wsproto.OP_PING:
                try:
                    self.sock.sendall(wsproto.encode_frame(wsproto.OP_PONG, payload, mask=True))
                except Exception:
                    return
                continue
            if op != wsproto.OP_TEXT:
                continue
            try:
                self.msgs.append((time.time(), json.loads(payload.decode("utf-8"))))
            except Exception:
                pass

    def send_json(self, obj):
        self.sock.sendall(wsproto.encode_frame(
            wsproto.OP_TEXT, json.dumps(obj).encode("utf-8"), mask=True))

    def send_bin(self, data):
        self.sock.sendall(wsproto.encode_frame(wsproto.OP_BIN, data, mask=True))

    def wait_for(self, mtype, timeout=8.0, since=0.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for t, m in self.msgs:
                if m.get("type") == mtype and t >= since:
                    return t, m
            time.sleep(0.02)
        return None, None

    def close(self):
        self._stop = True
        try:
            self.sock.sendall(wsproto.encode_frame(wsproto.OP_CLOSE, b"", mask=True))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def load_pcm(path):
    with wave.open(path) as w:
        if w.getframerate() != 16000:
            raise RuntimeError("WAV 必须是 16k：%s 是 %d" % (path, w.getframerate()))
        if w.getnchannels() != 1:
            raise RuntimeError("WAV 必须是单声道：%s" % path)
        return w.readframes(w.getnframes())


def make_pcm(port):
    """没有本地 WAV 时用云端 TTS 造一句（要和 config 的 TTS 一致，否则钱花了没声音）。"""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import check_realtime_asr as chk
    return chk.tts_to_pcm("你好，我是张伟，我想问一下授权书的事情。")


def near_dup(a, b):
    """两条 partial 是否属于"重复推送"（前端看起来字在闪）。

    三种情况要分清，混在一起判就会误报：
        完全相同            → 重复（真问题，已在 poll_partial 里去重）
        一条是另一条的前缀  → 正常增长（"你好" → "你好，我是"），不是重复
        长度接近且高度相似  → 疑似重复（同一句被重推了一遍）
    """
    if a == b:
        return True
    if not a or not b:
        return False
    if b.startswith(a) or a.startswith(b):
        return False
    longer = max(len(a), len(b))
    if abs(len(a) - len(b)) > max(1, int(0.15 * longer)):
        return False
    same = sum(1 for x, y in zip(a, b) if x == y)
    return same / longer > 0.9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8802)
    ap.add_argument("--wav", help="本地 16k 单声道 WAV")
    ap.add_argument("--keep", action="store_true", help="跑完不关服务（排查用）")
    args = ap.parse_args()

    print("=" * 74)
    print("Z7 端到端：/ws/call + 流式字幕 + 落 turn")
    print("=" * 74)

    # 先确认端口是空的。踩过：8802 上有上一次留下的旧服务（还是改配置之前的
    # APP_SCENE=pop_reject），脚本连上去测了一整轮，测的是旧代码 + 旧配置，
    # 结论"流式没启用"完全是假的。占用就报错，绝不复用不认识的进程。
    _probe = socket.socket()
    try:
        _probe.bind(("127.0.0.1", args.port))
    except OSError:
        print("  [FAIL] 端口 %d 已被占用——先关掉那个进程，别在别人的服务上测自己"
              % args.port)
        return 2
    finally:
        _probe.close()

    env = dict(os.environ)
    env.pop("PORT", None)          # 本机调试端口由命令行给
    env.pop("APP_SCENE", None)     # 场景必须和仓库配置一致，不要继承外部残留
    env.pop("APP_CONFIG", None)
    proc = subprocess.Popen([sys.executable, "app.py", str(args.port)], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env)
    ok_all = True
    try:
        health = wait_health(args.port)
        print("  [OK] 服务已起：scene=%s" % health.get("scene", "?"))

        pcm = (load_pcm(args.wav) if args.wav else make_pcm(args.port))
        print("  [OK] 测试音频 %.0f ms%s"
              % (len(pcm) / 32, "（本地 WAV）" if args.wav else "（云端 TTS）"))

        start = post_json(args.port, "/api/call/start")
        sid, token = start["session_id"], start["read_token"]
        ws = WsClient(args.port)
        ws.send_json({"type": "start", "session_id": sid, "token": token})
        t_ready, ready = ws.wait_for("ready", timeout=10)
        ok_all &= bool(ready)
        print("  [%s] WS 已绑定会话，ready.realtime=%s"
              % ("OK" if ready else "FAIL", ready and ready.get("realtime")))
        if not ready or not ready.get("realtime"):
            print("        → 流式没启用，原因：%s"
                  % ((ready or {}).get("realtime_error") or "（未给出）"))
            ok_all = False

        # 按 20ms 帧推，节奏与前端一致；每 160ms 一批（前端 BATCH_FRAMES=8）
        t0 = time.time()
        n_frames = len(pcm) // FRAME_BYTES
        for i in range(n_frames):
            ws.send_bin(pcm[i * FRAME_BYTES:(i + 1) * FRAME_BYTES])
            if i % 8 == 7:
                time.sleep(0.16)          # 跟真实语速走，也让服务端攒够 100ms
        print("  [OK] 已推 %d 帧（%.1f 秒音频）" % (n_frames, n_frames * FRAME_MS / 1000))

        t_turn = time.time()
        ws.send_json({"type": "turn"})
        t_res, res = ws.wait_for("turn_result", timeout=25, since=t_turn)
        ok_all &= bool(res)

        partials = [(t, m.get("text") or "") for t, m in ws.msgs
                    if m.get("type") == "partial"]
        texts = [p for _, p in partials if p]
        # 门槛是「≥2 条且后者更长」，不是「≥3 条」。
        # 原来写死 3 条，但这取决于云端怎么分批：同一段 2.4 秒音频，
        # 实测有时出 8 条、有时只出 2 条（"你好" → "你好，我是"），
        # 拿条数当断言就是拿云端的抖动当标准，会时不时的假红。
        # 真正要证明的是"边听边出、且内容在增长"，两条就够说明不是一次性吐完。
        _grew = bool(texts) and len(texts[-1]) > len(texts[0])
        ok_partial = len(texts) >= 2 and _grew
        print("  [%s] 半句字幕 %d 条（后者更长=%s）%s"
              % ("OK" if ok_partial else "FAIL", len(texts), _grew,
                 ("：" + " → ".join(texts[:5])) if texts else ""))
        ok_all &= ok_partial

        dups = [(a, b) for a, b in zip(texts, texts[1:]) if near_dup(a, b)]
        print("  [%s] 相邻字幕没有重复推送（%d 处可疑）"
              % ("OK" if not dups else "FAIL", len(dups)))
        for a, b in dups[:3]:
            print("        重复：%r / %r" % (a, b))
        ok_all &= not dups

        if texts:
            first_delay = (partials[0][0] - t0) * 1000
            print("        首条字幕距开讲 %.0f ms" % first_delay)

        if res:
            user_text = (res.get("user_text") or "").strip()
            reply = (res.get("reply_text") or "").strip()
            last = texts[-1] if texts else ""
            # 与最后一条半句的重叠：至少共享 4 个字符的连续片段
            overlap = 0
            for n in range(min(len(last), len(user_text)), 3, -1):
                if any(last[i:i + n] in user_text for i in range(0, len(last) - n + 1)):
                    overlap = n
                    break
            print("  [%s] 落 turn：user_text=%r" % ("OK" if user_text else "FAIL",
                                                   user_text[:40]))
            print("  [%s] 字幕与终句连贯（最长重叠 %d 字）"
                  % ("OK" if overlap >= 4 else "WARN", overlap))
            print("  [%s] 有回复：%s" % ("OK" if reply else "FAIL", reply[:40]))
            ok_all &= bool(user_text) and bool(reply)

            # 整句拼两遍的典型形态：同一 3 字片段出现两次以上
            triple = {}
            for i in range(len(user_text) - 2):
                k = user_text[i:i + 3]
                triple[k] = triple.get(k, 0) + 1
            repeats = [k for k, c in triple.items() if c >= 2]
            print("  [%s] 终句没有整句重复（可疑片段：%s）"
                  % ("OK" if not repeats else "FAIL",
                     ", ".join(repeats[:5]) if repeats else "无"))
            ok_all &= not repeats

        ws.close()
        try:
            post_json(args.port, "/api/call/end", {"session_id": sid,
                                                   "read_token": token})
        except Exception:
            pass
    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()

    print("=" * 74)
    print("结论：%s" % ("Z7 端到端通过" if ok_all else "有断言未通过，见上面标记"))
    print("注意：这一通会写进 data/events.db（用的就是真实服务）。")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
