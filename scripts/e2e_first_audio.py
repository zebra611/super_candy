# -*- coding: utf-8 -*-
"""端到端首字时延实测（可复现，真服务 + 真 WS + 真云端 ASR/TTS）。

回答一个问题：赛题"首字 P50 ≤ 2s"到底过没过？

口径（务必看清，别和别的数混）：
    T0   = 客户端推完最后一帧音频（≈客户说完那一刻），与前端 t0 对齐；
    T_tts= WS 收到首个 tts 消息（服务端首段音频 URL 就绪并回推）。
    本脚本测 T_tts − T0 = "说完 → 首段音频就绪"的端到端下界。
    它包含 ASR + 状态机/LLM + TTS 首段合成 + 服务端回推；
    **不含**浏览器下载音频 + 起播（前端 T4 还差这一段，约 100–300ms）。
    所以这是"下界"：若下界已逼近/超 2s，则真实 T4−T0 一定过不了；
    若下界明显低于 2s，余量留给下载+起播，才有资格说"可能过"。

    真正的 T4−T0 由浏览器 onplaying 上报（latency_report 事件），无头环境
    抓不到，本脚本不冒充它。

画像驱动：
    从 data/customer_cards.json 读不同 label 的画像，每轮绑定一张客户卡，
    卡里的公司/联系人决定状态机分支与回复长度——回复越长，TTS 首段压力越大，
    这正是时延测试要覆盖的差异。音频统一用 s07（客户说"你好，我是张伟"），
    因为语料库里只有这一句是真人客户话（其余 s01-s12 多为客服话术）。
    报告里逐轮记录 persona label / card_id，可追溯。

用法：
    python scripts/e2e_first_audio.py --rounds 20
    python scripts/e2e_first_audio.py --rounds 20 --wav data/vad_corpus/speech/s07.wav
"""
import argparse
import base64
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modules import wsproto  # noqa: E402  （仓库自带，check_ws_realtime.py 同款）


class WsClient:
    """最小 WS 客户端：复用 check_ws_realtime.py 的握手/收发实现。"""

    def __init__(self, port, timeout=10.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ("GET /ws/call HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
               % (port, key))
        self.sock.sendall(req.encode("ascii"))
        self.rfile = self.sock.makefile("rb")
        head = b""
        while b"\r\n\r\n" not in head:
            p = self.rfile.read(1)
            if not p:
                raise RuntimeError("握手失败：连接断了")
            head += p
        if "101" not in head.split(b"\r\n", 1)[0].decode("latin-1"):
            raise RuntimeError("握手被拒")
        self.reader = wsproto.Reader(self.rfile)
        self.msgs = []
        self._stop = False
        threading.Thread(target=self._reader_loop, daemon=True).start()

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

    def wait_for(self, mtype, timeout=20.0, since=0.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for t, m in self.msgs:
                if m.get("type") == mtype and t >= since:
                    return t, m
            time.sleep(0.01)
        return None, None

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass


def wait_health(port, timeout=20.0):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port,
                                        timeout=2) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("服务没起来")


def post_json(port, path, body):
    import urllib.request
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def load_pcm(path):
    import wave
    with wave.open(path) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            raise ValueError("WAV 必须是 16k 单声道：%s" % path)
        return w.readframes(w.getnframes())


def pick_personas(n):
    """从不同 label 的画像里均匀取 n 个，避免全是同一类客户。"""
    data = json.load(open(os.path.join(ROOT, "data", "customer_cards.json"),
                          encoding="utf-8"))
    personas = data.get("personas") or []
    seen, picked = set(), []
    # 先按 label 去重，保证类型多样
    for p in personas:
        label = p.get("label") or p.get("id")
        if label and label not in seen:
            seen.add(label)
            picked.append(p)
    # 不够就补
    if len(picked) < n:
        picked += [p for p in personas if p not in picked]
    out = []
    for i in range(n):
        out.append(picked[i % len(picked)])
    return out


def pct(a, p):
    if not a:
        return None
    a = sorted(a)
    return a[min(len(a) - 1, int(len(a) * p / 100.0))]


def main():
    ap = argparse.ArgumentParser(description="端到端首字时延实测")
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--wav", default="data/vad_corpus/speech/s07.wav")
    ap.add_argument("--port", type=int, default=8890)
    args = ap.parse_args()

    wav = args.wav if os.path.isabs(args.wav) else os.path.join(ROOT, args.wav)
    pcm = load_pcm(wav)
    frame = 640  # 20ms @16k16bit
    personas = pick_personas(args.rounds)

    print("=" * 74)
    print("端到端首字时延实测：%d 轮" % args.rounds)
    print("口径B(对齐答辩)=请求到达→首段就绪；口径A(物理)=推完帧→首段就绪(含VAD等待)")
    print("均不含浏览器下载+起播；音频=%s" % os.path.basename(wav))
    print("=" * 74)

    env = dict(os.environ)
    env.pop("PORT", None)
    env.pop("APP_SCENE", None)
    env.pop("APP_CONFIG", None)
    proc = subprocess.Popen([sys.executable, "app.py", str(args.port)], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    rows = []
    try:
        health = wait_health(args.port)
        print("服务已起：scene=%s，realtime=%s" % (
            health.get("scene", "?"), health.get("realtime")))
        for i, persona in enumerate(personas):
            start = post_json(args.port, "/api/call/start", {})
            sid, token = start["session_id"], start["read_token"]
            ws = WsClient(args.port)
            ws.send_json({"type": "start", "session_id": sid, "token": token})
            _t, ready = ws.wait_for("ready", timeout=10)
            if not ready or not ready.get("realtime"):
                ws.close()
                post_json(args.port, "/api/call/end", {"session_id": sid,
                                                       "read_token": token})
                continue
            nframes = len(pcm) // frame
            for k in range(nframes):
                ws.send_bin(pcm[k * frame:(k + 1) * frame])
                if k % 8 == 7:
                    time.sleep(0.16)
            t_push_done = time.time()             # 推完最后一帧（客户物理说完）
            ws.send_json({"type": "turn"})
            t_tts, tts_msg = ws.wait_for("tts", timeout=20, since=t_push_done - 1)
            t_res, res = ws.wait_for("turn_result", timeout=20, since=t_push_done - 1)
            # 三个口径，绝不混报：
            #   A) 物理口径：推完帧 → 首段就绪（含 VAD 端点等待，约 700ms+）
            #   B) 服务端口径：请求到达 → 首段就绪（= turn_end.server_first_audio_ms，
            #      对齐答辩 T0=端点检测后、T4 前，是真正可比的"下界"）
            proc_ms = None
            try:
                turns = [m for _t, m in ws.msgs if m.get("type") == "turn_end"]
                if turns:
                    last = turns[-1]
                    lat = last.get("latency") or {}
                    proc_ms = (lat.get("server_first_audio_ms")
                               or last.get("server_first_audio_ms")
                               or lat.get("total_ms")
                               or last.get("processing_ms"))
                    proc_ms = round(float(proc_ms)) if proc_ms else None
            except Exception:
                pass
            delay_ms = round((t_tts - t_push_done) * 1000) if t_tts else None
            rtt_ms = round((t_res - t_push_done) * 1000) if t_res else None
            rows.append({
                "round": i + 1,
                "persona": persona.get("label") or persona.get("id", "?"),
                "persona_id": persona.get("id", ""),
                "physical_ms": delay_ms,          # A：推完帧→首段（含VAD等待）
                "server_first_ms": proc_ms,       # B：请求到达→首段（对齐答辩）
                "rtt_ms": rtt_ms,
                "tts_mode": (tts_msg or {}).get("mode", ""),
                "reply": ((res or {}).get("reply_text") or "")[:30],
            })
            print("  %2d. %-14s 服务端首字=%sms 物理口径=%sms mode=%s"
                  % (i + 1, rows[-1]["persona"][:14], proc_ms, delay_ms,
                     rows[-1]["tts_mode"]))
            ws.close()
            try:
                post_json(args.port, "/api/call/end", {"session_id": sid,
                                                       "read_token": token})
            except Exception:
                pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    server_v = [r["server_first_ms"] for r in rows if r["server_first_ms"] is not None]
    phys_v = [r["physical_ms"] for r in rows if r["physical_ms"] is not None]
    print("\n" + "=" * 74)
    if not server_v and not phys_v:
        print("无有效样本")
        return 2
    gate = 2000
    if server_v:
        print("【口径B·对齐答辩】请求到达 → 首段音频就绪，n=%d" % len(server_v))
        print("  P50 = %d ms   P95 = %d ms   min = %d   max = %d"
              % (pct(server_v, 50), pct(server_v, 95), min(server_v), max(server_v)))
        print("  门槛 2000ms：P50 %s（下界，真实 T4 还要加浏览器下载+起播）"
              % ("达标" if pct(server_v, 50) <= gate else "未达标"))
    if phys_v:
        print("【口径A·物理】推完帧 → 首段就绪（含 VAD 端点等待），n=%d" % len(phys_v))
        print("  P50 = %d ms   P95 = %d ms"
              % (pct(phys_v, 50), pct(phys_v, 95)))

    outdir = os.path.join(ROOT, "eval", "reports")
    os.makedirs(outdir, exist_ok=True)
    outpath = os.path.join(outdir, "first_audio_e2e.json")
    json.dump({
        "口径B_对齐答辩": "请求到达 → 首个 tts 音频就绪（server_first_audio_ms），不含浏览器下载+起播",
        "口径A_物理": "推完最后一帧 → 首个 tts 音频就绪（含 VAD 端点等待）",
        "wav": os.path.relpath(wav, ROOT),
        "rounds": args.rounds,
        "p50_server_ms": pct(server_v, 50),
        "p95_server_ms": pct(server_v, 95),
        "p50_physical_ms": pct(phys_v, 50),
        "p95_physical_ms": pct(phys_v, 95),
        "rows": rows,
    }, open(outpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("明细已写入 %s" % os.path.relpath(outpath, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
