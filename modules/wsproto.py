"""WebSocket 协议最小实现（RFC 6455）—— 只做通话需要的那一小块。

为什么自己写而不用库：
    项目铁律是"不引第三方依赖"（clone 下来就能跑、线上沙箱没有 pip）。
    而 S2 需要的 WebSocket 子集非常小：握手应答、text/binary 帧、
    ping/pong、close，加上客户端帧必须去掩码。总共一百多行，
    每一行都能离线断言（见 verify.py 证据 14），比引一个不确定能不能
    装上的包更可控。

刻意不支持的（用到再说，别提前造）：
    - 扩展头（permessage-deflate 等）：握手时我们不应答任何扩展；
    - 大于 2^16 的单帧其实支持（64 位长度），但服务端会拒绝超大帧；
    - 多路复用等花活。

帧格式速记（写测试时对照用）：
    byte0: [fin(1)][rsv(3)][opcode(4)]
    byte1: [mask(1)][payload_len(7)]  len==126 → 后跟 2 字节真实长度
                                     len==127 → 后跟 8 字节真实长度
    [2 字节掩码键（仅客户端帧）][payload（客户端帧需去掩码）]

负责人：角色 1（实时语音与终端）
"""

import base64
import hashlib
import struct

# RFC 6455 固定的握手魔串。所有 WebSocket 实现都必须用它算应答键，
# 改一个字母浏览器就会握手失败。
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# 帧操作码。只列用到的；0x0 是分片帧的"后续片段"。
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class FrameError(Exception):
    """帧不合法（长度超限、连接中途断掉、协议错）。调用方据此关连接。"""


def accept_key(client_key):
    """算握手应答键：base64(SHA1(client_key + GUID))。

    用 RFC 6455 第 1.3 节的官方示例作为断言锚点（verify.py 证据 14），
    算法抄错的话浏览器会直接拒绝建连，错得非常"响"，但也别等线上才响。
    """
    digest = hashlib.sha1((client_key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(opcode, payload=b"", fin=True, mask=False):
    """把一帧编码成字节串。

    mask：协议规定**客户端发给服务端**的帧必须带掩码，服务端发出的不带。
    我们两个方向都要用（服务端下发 + 实时识别的 WSS 客户端上行），
    所以这个开关是必须的，不是装饰。
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload 必须是 bytes")
    payload = bytes(payload)
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)

    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", b0, (0x80 if mask else 0x00) | n)
    elif n <= 0xFFFF:
        header = struct.pack("!BBH", b0, (0x80 if mask else 0x00) | 126, n)
    else:
        header = struct.pack("!BBQ", b0, (0x80 if mask else 0x00) | 127, n)

    if not mask:
        return header + payload

    # 掩码键随机生成。协议只要求"不可预测"，不要求密码学强度，
    # 但随手用 os.urandom 也不比写死一个数贵。
    key = struct.unpack("!I", _urandom(4))[0]
    masked = _mask_bytes(payload, key)
    return header + struct.pack("!I", key) + masked


def _mask_bytes(data, key):
    """异或掩码。掩码键按小端拆成 4 字节循环异或——RFC 原文就是这么绕的。"""
    kb = struct.pack("!I", key)
    out = bytearray(data)
    for i in range(len(out)):
        out[i] ^= kb[i & 3]
    return bytes(out)


def _urandom(n):
    import os
    return os.urandom(n)


class Reader:
    """从文件型对象（socket.makefile 或 rfile）里逐帧读。

    为什么按"精确 n 字节"读：TCP 是字节流没有消息边界，
    少读半个字节，下一帧的解析就全错位了——而且错位之后大概率
    不是报错而是"解析出乱码 JSON"，比崩溃难查得多。
    """

    def __init__(self, rfile, max_payload=8 * 1024 * 1024):
        self._rfile = rfile
        self._max = max_payload

    def _read_exact(self, n):
        data = self._rfile.read(n)
        if data is None or len(data) < n:
            raise FrameError("连接在读完 %d 字节前就断了" % n)
        return data

    def read_frame(self):
        """读一帧，返回 (fin, opcode, payload)。payload 已去掩码。

        只解析我们声明支持的字段；遇到 RSV 位非零（意味着对端想用扩展，
        而我们握手时没答应任何扩展）直接拒绝——带病运行只会更难查。
        """
        b1, b2 = self._read_exact(2)
        fin = bool(b1 & 0x80)
        rsv = b1 & 0x70
        opcode = b1 & 0x0F
        if rsv:
            raise FrameError("对端使用了未协商的扩展位（RSV != 0）")

        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exact(8))
        if length > self._max:
            raise FrameError("单帧 %d 字节，超过上限 %d" % (length, self._max))

        mask_key = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask_key is not None:
            payload = _mask_bytes(payload, struct.unpack("!I", mask_key)[0])
        return fin, opcode, payload

    def read_message(self):
        """读一条完整消息（自动拼分片），返回 (opcode, payload)。

        控制帧（ping/close）允许插在分片中间，这是协议明确允许的；
        所以不能在 read_frame 一层处理分片，得在这层攒。
        """
        frags = []
        op = None
        while True:
            fin, frame_op, payload = self.read_frame()
            if frame_op in (OP_CLOSE, OP_PING, OP_PONG):
                return frame_op, payload        # 控制帧原样上抛，调用方处理
            if frame_op == OP_CONT:
                if op is None:
                    raise FrameError("分片序列不能从 CONT 开始")
                frags.append(payload)
            else:
                if op is not None:
                    raise FrameError("上一条消息还没收完又来了新帧")
                op = frame_op
                frags = [payload]
            if fin:
                return op, b"".join(frags)
