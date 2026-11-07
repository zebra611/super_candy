# -*- coding: utf-8 -*-
"""用本机真实麦克风录标定语料（真实设备频响 / 真实房间 / 真实 AEC 残压）。

为什么必须做这个：
    data/vad_corpus 里的 12 条人声是**云端 TTS 合成的**，9 类噪声是**我用 math
    拼的**。在这种数据上跑出 15/15，只能证明"判据不跟自己的语料打架"，
    不能证明"现场能用"。真正决定演示成败的是两条真实链路：
        · 扬声器放出来的声音经房间、经 AEC 之后，麦克风收到多少（会不会自打断）
        · 本机麦克风阵列的底噪与频响是多少（0.020 / 0.035 这两个阈值站不站得住）
    这两条只有真录才能回答。

录什么（都能自动跑，不需要人配合）：
    silence   纯房间底噪，不播放任何东西
    echo      扬声器播 TTS，麦克风同步录（真实回声路径）
    keytap    提示后由人去敲键盘（需要人配合，默认跳过）
    voice     提示后由人说话（需要人配合，默认跳过）

用法：
    python scripts/record_real_vad.py --list          # 看设备
    python scripts/record_real_vad.py                 # 录 silence + echo（全自动）
    python scripts/record_real_vad.py --with-human    # 再加 keytap + voice（要人配合）

输出：data/vad_real/<场景>.wav（16k / 单声道 / PCM16，与调参脚本口径一致）
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SR = 16000
WAVE_MAPPER = -1
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001
WAVE_IN_QUERY = 0x3F00


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wt.WORD),
                ("nChannels", wt.WORD),
                ("nSamplesPerSec", wt.DWORD),
                ("nAvgBytesPerSec", wt.DWORD),
                ("nBlockAlign", wt.WORD),
                ("wBitsPerSample", wt.WORD),
                ("cbSize", wt.WORD)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p),
                ("dwBufferLength", wt.DWORD),
                ("dwBytesRecorded", wt.DWORD),
                ("dwUser", ctypes.c_void_p),
                ("dwFlags", wt.DWORD),
                ("dwLoops", wt.DWORD),
                ("lpNext", ctypes.c_void_p),
                ("reserved", ctypes.c_void_p)]


winmm = ctypes.WinDLL("winmm", use_last_error=True)


def list_devices():
    n = winmm.waveInGetNumDevs()
    print("录音设备数：%d" % n)
    for i in range(n):
        caps = ctypes.create_string_buffer(128)
        rc = winmm.waveInGetDevCapsW(i, caps, ctypes.sizeof(caps))
        if rc == 0:
            # WAVEINCAPSW：wMid wPid vDriverVersion szPname[32]
            name = caps.raw[8:8 + 64].decode("utf-16-le", "ignore").split("\x00")[0]
            print("  [%d] %s" % (i, name))


def record(path, seconds, play_wav=None, device=WAVE_MAPPER):
    """录 seconds 秒到 path。play_wav 给定时，边录边放（用来采真实回声）。"""
    fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, 1, SR, SR * 2, 2, 16, 0)
    hwi = wt.HANDLE()
    rc = winmm.waveInOpen(ctypes.byref(hwi), wt.UINT(device), ctypes.byref(fmt), 0, 0,
                          CALLBACK_NULL)
    if rc != 0:
        raise RuntimeError("waveInOpen 失败 rc=%d（麦克风被占用或没有录音设备）" % rc)

    total = int(SR * 2 * seconds)
    buf = ctypes.create_string_buffer(total)
    hdr = WAVEHDR()
    hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
    hdr.dwBufferLength = total
    winmm.waveInPrepareHeader(hwi, ctypes.byref(hdr), ctypes.sizeof(hdr))
    winmm.waveInAddBuffer(hwi, ctypes.byref(hdr), ctypes.sizeof(hdr))
    winmm.waveInStart(hwi)

    t0 = time.time()
    if play_wav:
        import winsound
        winsound.PlaySound(play_wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
    # 等录满。不用固定 sleep：按已录字节数推进，慢一点也不会截断。
    while time.time() - t0 < seconds + 0.3:
        if hdr.dwBytesRecorded >= total:
            break
        time.sleep(0.05)
    time.sleep(0.1)

    winmm.waveInStop(hwi)
    winmm.waveInReset(hwi)
    winmm.waveInUnprepareHeader(hwi, ctypes.byref(hdr), ctypes.sizeof(hdr))
    winmm.waveInClose(hwi)

    n = min(hdr.dwBytesRecorded, total)
    data = buf.raw[:n]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data)
    return n


def rms_of(path):
    with wave.open(path) as w:
        raw = w.readframes(w.getnframes())
    if not raw:
        return 0.0
    s = 0
    n = len(raw) // 2
    for i in range(n):
        v = int.from_bytes(raw[2 * i:2 * i + 2], "little", signed=True) / 32768.0
        s += v * v
    return (s / n) ** 0.5


def _tts_wav(text, out_path):
    """用项目已有的云端 TTS 合成一条 16k wav，作为"客服播报"放出来。"""
    from scripts.check_realtime_asr import tts_to_pcm      # 复用已验证的实现
    ints = tts_to_pcm(text)
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(int(v).to_bytes(2, "little", signed=True) for v in ints))
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="列出录音设备")
    ap.add_argument("--with-human", action="store_true",
                    help="额外录 keytap/voice（需要人配合敲键盘、说话）")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "vad_real"))
    ap.add_argument("--device", type=int, default=WAVE_MAPPER)
    args = ap.parse_args()

    if args.list:
        list_devices()
        return 0

    tmp = os.path.join(args.out, "_tmp_agent.wav")
    os.makedirs(args.out, exist_ok=True)
    report = {}

    print("=" * 68)
    print("真实声学链路录音 → %s" % args.out)
    print("=" * 68)

    # ① 房间底噪：不播任何东西
    print("\n[1/2] 录房间底噪 %.0f 秒（保持安静，别说话）…" % args.seconds)
    p = os.path.join(args.out, "real_silence.wav")
    n = record(p, args.seconds, device=args.device)
    report["real_silence"] = {"bytes": n, "rms": round(rms_of(p), 5)}
    print("      → rms=%.4f  %s" % (report["real_silence"]["rms"], p))

    # ② 真实回声：扬声器播客服播报，麦克风同步录
    print("\n[2/2] 播放客服播报并同步录音（这条测的就是「会不会自己打断自己」）…")
    try:
        _tts_wav("您好，我是京东商家资质审核的工作人员，您这边提交的营业执照"
                 "经营范围与店铺主营类目不一致，需要您补充说明一下。", tmp)
        p2 = os.path.join(args.out, "real_echo.wav")
        n2 = record(p2, max(args.seconds, 12), play_wav=tmp, device=args.device)
        report["real_echo"] = {"bytes": n2, "rms": round(rms_of(p2), 5)}
        print("      → rms=%.4f  %s" % (report["real_echo"]["rms"], p2))
        if report["real_echo"]["rms"] < 0.001:
            print("      [!] 几乎没录到声音：可能在用耳机，或系统音量过低。")
            print("          这条数据不能用，先换成扬声器外放再跑一次。")
    except Exception as exc:
        print("      [跳过] %s" % str(exc)[:160])

    if args.with_human:
        print("\n[人工] 敲键盘 5 秒（机械/薄膜都行，正常力度）… 3 秒后开始")
        time.sleep(3)
        p3 = os.path.join(args.out, "real_keytap.wav")
        record(p3, 5.0, device=args.device)
        report["real_keytap"] = {"rms": round(rms_of(p3), 5)}
        print("      → rms=%.4f  %s" % (report["real_keytap"]["rms"], p3))

        print("\n[人工] 说一句话：「我这边已经上传了新的营业执照」 3 秒后开始")
        time.sleep(3)
        p4 = os.path.join(args.out, "real_voice.wav")
        record(p4, 6.0, device=args.device)
        report["real_voice"] = {"rms": round(rms_of(p4), 5)}
        print("      → rms=%.4f  %s" % (report["real_voice"]["rms"], p4))

    try:
        os.remove(tmp)
    except OSError:
        pass

    rp = os.path.join(args.out, "report.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n报告 → %s" % rp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
