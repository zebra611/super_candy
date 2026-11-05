"""流式识别适配层 —— DashScope paraformer-realtime-v2 的 WebSocket 客户端。

为什么是这个接口（选型结论，写在这里免得再查一遍）：
    qwen3-asr-flash 只有 HTTP 整段接口，没有流式端点；DashScope 家族里
    能"边说边出字"的是 paraformer-realtime-v2（讯飞系/阿里系的外呼场景
    也普遍用它）。所以流式链路换模型不是可选项，是唯一选项——
    qwen3-asr-flash 继续负责批式兜底，两者并存，由配置切换。

协议（DashScope 通用 WebSocket 推理协议，v1）：
    连上 wss://dashscope.aliyuncs.com/api-ws/v1/inference 后，
    客户端发三条指令（JSON text 帧）：
        run-task      开任务（声明模型 / pcm / 16kHz）
        continue-task 喂音频（base64，100ms 一片比较稳）
        finish-task   收尾
    服务端回：task-started / result-generated（半句与整句都在这）/ task-failed。

为什么一个通话只开一个任务、不按轮开：
    实时识别是连续流，句子级结果（final）一条条出。按轮开关任务要多两次
    往返，还容易在"finish 没回就来下一轮"时卡住。整通一个任务，
    打断时只丢弃"已攒未取"的句子，任务本身不动。

诚实声明（读代码的人必看）：
    这条链路没有密钥/没有网时**完全不启用**（voice.realtime_enabled()
    默认 False），WS 通话自动走批式兜底（整段识别，行为与 S1 一致）。
    本文件的可测部分是帧编解码与事件解析；连云端的部分只保证"失败时
    干净地退回批式"，不保证在没验证过的环境下一次连通——上真链路前
    必须先用真密钥跑一遍 verify 之外的连通检查。

负责人：角色 1（实时语音与终端）
"""

import base64
import json
import socket
import ssl
import struct
import threading
import time
import uuid

import config_loader
from modules import wsproto

_HOST = "dashscope.aliyuncs.com"
_PATH = "/api-ws/v1/inference"

# 喂给云端的最小片长。太小（每帧 20ms 就发）会产生海量小包，
# 太大（整轮再发）就失去流式意义。100ms 是官方示例的量级。
_FEED_CHUNK_MS = 100


class RealtimeAsr:
    """一条到 paraformer-realtime-v2 的长连接。坏了自己标记，不抛给通话。"""

    def __init__(self, sock, task_id):
        self._sock = sock
        self._task_id = task_id
        self._lock = threading.Lock()
        self._partials = ""       # 未取走的半句字幕
        self._finals = []         # 未取走的整句
        self._started = False
        self.dead = False
        self._last_error = ""
        # 喂音频的节流缓冲
        self._pending = bytearray()
        self._pending_ms = 0

    # ------------------------------------------------------------------
    # 建连
    # ------------------------------------------------------------------

    @classmethod
    def connect(cls, timeout=5.0):
        """建连 + 跑 run-task + 等 task-started。任何一步失败抛异常，
        由调用方决定回落批式——这里不做静默降级，原因要留给日志。"""
        key = config_loader.secret("dashscope_api_key")
        if not key:
            raise RuntimeError("未配置 dashscope_api_key")
        model = (config_loader.asr_config().get("realtime_model")
                 or "paraformer-realtime-v2")

        raw = socket.create_connection((_HOST, 443), timeout=timeout)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=_HOST)
        sock.settimeout(timeout)
        # 客户端帧必须带掩码（RFC 6455），encode_frame 的 mask=True 就是干这个的
        handshake = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Authorization: bearer %s\r\n"
            "\r\n" % (_PATH, _HOST, wsproto_handshake_key(), key)
        )
        sock.sendall(handshake.encode("ascii"))
        status, headers = _read_handshake_response(sock)
        if status != 101:
            sock.close()
            raise RuntimeError("实时识别握手被拒：HTTP %d" % status)
        accept = headers.get("sec-websocket-accept", "")
        # 校验应答键：服务器算错（或中间设备劫持）时在这里暴露，而不是之后出怪事
        if accept != wsproto.accept_key(_client_key_sent):
            sock.close()
            raise RuntimeError("实时识别握手应答键不匹配")

        task_id = uuid.uuid4().hex
        run_task = {
            "header": {"action": "run-task", "task_id": task_id,
                       "streaming": "out"},
            "payload": {
                "task_group": "audio", "task": "asr",
                "function": "recognition", "model": model,
                "parameters": {"format": "pcm", "sample_rate": 16000},
                "input": {},
            },
        }
        _send_json(sock, run_task)

        self = cls(sock, task_id)
        # 先注册再等启动：reader 线程一开跑就可能送来 task-started，
        # 晚注册会把它丢掉，然后白白超时。
        t = threading.Thread(target=self._reader_loop, daemon=True)
        t.start()
        deadline = time.time() + timeout
        while not self._started and not self.dead and time.time() < deadline:
            time.sleep(0.02)
        if not self._started:
            self.close()
            raise RuntimeError("实时识别任务未启动：%s"
                               % (self._last_error or "超时"))
        return self

    # ------------------------------------------------------------------
    # 喂音频 / 取结果
    # ------------------------------------------------------------------

    def feed(self, pcm):
        """攒够 ~100ms 才发一片。识别坏了就丢，不能让识别拖住通话。"""
        if self.dead:
            return
        with self._lock:
            self._pending.extend(pcm)
            self._pending_ms += len(pcm) * 1000 // (
                32000)          # 16kHz × 2 字节 = 每毫秒 32 字节
            if self._pending_ms < _FEED_CHUNK_MS:
                return
            chunk = bytes(self._pending)
            self._pending = bytearray()
            self._pending_ms = 0
        msg = {"header": {"action": "continue-task", "task_id": self._task_id,
                          "streaming": "in"},
               "payload": {"input": {"speech":
                                     base64.b64encode(chunk).decode("ascii")}}}
        try:
            _send_json(self._sock, msg)
        except Exception as exc:
            self.dead = True
            self._last_error = str(exc)[:120]

    def poll_partial(self):
        """取走半句字幕（有新内容才返回，避免前端每帧都重画）。"""
        with self._lock:
            if not self._partials:
                return ""
            text, self._partials = self._partials, ""
            return text

    def finish_and_collect(self, timeout=1.5):
        """等本段说完的整句。不结束任务（整通一个任务），只等结果落袋。"""
        deadline = time.time() + timeout
        while time.time() < deadline and not self.dead:
            with self._lock:
                if self._finals:
                    break
            time.sleep(0.03)
        with self._lock:
            text = "".join(self._finals)
            self._finals = []
            return text

    def restart(self):
        """丢弃已攒未取的结果（打断后调用）。任务连接保持不动。"""
        with self._lock:
            self._finals = []
            self._partials = ""
            self._pending = bytearray()
            self._pending_ms = 0

    def close(self):
        """礼貌收尾：发 finish-task 再关 socket。失败不打扰任何人。"""
        try:
            _send_json(self._sock, {
                "header": {"action": "finish-task", "task_id": self._task_id,
                           "streaming": "in"},
                "payload": {"input": {}}})
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        self.dead = True

    # ------------------------------------------------------------------
    # 收包线程
    # ------------------------------------------------------------------

    def _reader_loop(self):
        """死循环收服务端事件。所有异常都以"标记死亡"收场——
        识别挂了通话还得继续，绝不能让这个线程把进程带崩。"""
        try:
            reader = wsproto.Reader(self._sock, max_payload=4 * 1024 * 1024)
            while True:
                opcode, payload = reader.read_message()
                if opcode == wsproto.OP_CLOSE:
                    return
                if opcode != wsproto.OP_TEXT:
                    continue
                msg = json.loads(payload.decode("utf-8", "replace"))
                header = (msg.get("header") or {})
                event = header.get("event")
                if event == "task-started":
                    with self._lock:
                        self._started = True
                elif event == "result-generated":
                    self._on_result(msg)
                elif event == "task-failed":
                    with self._lock:
                        self.dead = True
                        self._last_error = json.dumps(msg, ensure_ascii=False)[:200]
                # task-finished：服务端主动收尾，之后不会再有结果
        except Exception:
            with self._lock:
                self.dead = True

    def _on_result(self, msg):
        sentence = ((msg.get("payload") or {}).get("output") or {}).get("sentence")
        if not sentence:
            return
        text = (sentence.get("text") or "").strip()
        if not text:
            return
        # end_time 存在 = 这句已经说完（final）；否则是半句（partial）
        with self._lock:
            if sentence.get("end_time") is not None:
                self._finals.append(text)
                self._partials = ""          # 整句落袋，半句自然作废
            else:
                self._partials = text


# ---------------------------------------------------------------------------
# 握手辅助
# ---------------------------------------------------------------------------

_client_key_sent = "candy-realtime-handshake-key"


def wsproto_handshake_key():
    """固定的 Sec-WebSocket-Key。

    为什么不随机：掩码帧的随机性由每帧掩码键承担（encode_frame 里），
    握手 Key 只要满足"base64 的 16 字节"即可。固定值让我们在抓包排查时
    一眼认出这是我们的连接。
    """
    import base64 as _b64
    return _b64.b64encode(_client_key_sent.encode("ascii")).decode("ascii")


def _read_handshake_response(sock, timeout=5.0):
    """读 HTTP 101 响应头。逐行读到空行为止，返回 (status, headers)。"""
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        piece = sock.recv(4096)
        if not piece:
            raise RuntimeError("握手响应中途断开")
        buf += piece
        if len(buf) > 64 * 1024:
            raise RuntimeError("握手响应过大")
    head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    parts = lines[0].split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 else 0
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers


def _send_json(sock, obj):
    """发一帧 JSON text（客户端方向，带掩码）。"""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(wsproto.encode_frame(wsproto.OP_TEXT, data, mask=True))
