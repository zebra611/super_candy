"""造"多说话人"语料：同一批商户话术，用 4 个不同音色各念一遍。

为什么需要它：
    现有的 data/vad_corpus/speech 是**同一个音色**合成的 12 条，
    在上面测"能不能分辨旁人"等于用一个人的两张照片测人脸识别——
    永远 100%，毫无意义。

为什么用 TTS 而不是真人：
    真人录音要人配合、要重录、没法复现；TTS 的**音色是可控变量**，
    换 voice_name 就换了一个"说话人"，标签天然正确。
    代价必须写清楚：这是**音色差异**，不是真实人声的全部差异
    （真人还有口音、语速习惯、房间混响）。所以本语料的结论只能回答
    "不同音色能不能被分开"，不能直接当成真机 EER。

输出：
    data/speaker_corpus/<voice>/<编号>.wav   16k / 单声道 / PCM16
    data/speaker_corpus/manifest.json         每条：voice / text / 时长 / 采样率

用法：
    python scripts/gen_speaker_corpus.py
    python scripts/gen_speaker_corpus.py --voices Cherry,Ethan
"""

import argparse
import json
import os
import struct
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import config_loader          # noqa: E402
from modules import voice     # noqa: E402

OUT_DIR = os.path.join(_ROOT, "data", "speaker_corpus")
SR = 16000

# 真实外呼里商家会说的话，长短都覆盖：
# 太短（"喂"）语音特征不足，太长（一段话）又掩盖了短句的真实困难。
TEXTS = [
    ("t1", "喂，你好，哪位？"),
    ("t2", "对，是我，你说吧。"),
    ("t3", "尾号是零零八六，你核对一下。"),
    ("t4", "我这个是品牌授权的材料没过是吧，那我需要补什么呢？"),
    ("t5", "行，我知道了，我这两天抽空去弄一下。"),
]

# 音色候选：qwen3-tts-flash 支持多个中文音色。失败的就跳过并记账，
# 不硬编码成"一定有 4 个"，否则换账号/换模型时会整条挂掉。
VOICES = ["Cherry", "Serena", "Ethan", "Chelsie", "Dylan"]


def _parse_wav(data):
    """极简 RIFF 解析：只取 fmt 与 data，返回 (pcm, sr, channels, width)。"""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("不是 WAV")
    i, fmt, pcm = 12, None, None
    while i + 8 <= len(data):
        cid = data[i:i + 4]
        size = struct.unpack("<I", data[i + 4:i + 8])[0]
        body = data[i + 8:i + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            pcm = body
        i += 8 + size + (size & 1)
    if not fmt or pcm is None:
        raise ValueError("WAV 缺 fmt/data")
    _, channels, sr, _, _, bits = fmt
    return pcm, sr, channels, bits // 8


def _to_mono16k(pcm, sr, channels, width):
    """转 16k 单声道 int16。合成音色通常是 24k，必须降到 16k 才能和主链路同口径。"""
    import numpy as np
    if width != 2:
        raise ValueError("只支持 16bit PCM，实际 %d 字节" % width)
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if channels > 1:
        a = a.reshape(-1, channels).mean(axis=1)
    if sr != SR:
        n = int(round(len(a) * SR / float(sr)))
        idx = np.linspace(0, len(a) - 1, n)
        a = np.interp(idx, np.arange(len(a)), a)
    return a.clip(-32768, 32767).astype("<i2").tobytes()


def _write_wav(path, pcm16k):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = len(pcm16k)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, SR, SR * 2, 2, 16))
        f.write(b"data" + struct.pack("<I", n) + pcm16k)


def _tts_wav(text, voice_name):
    """临时改配置里的音色，调现成的 voice.tts，拿 OSS 链接再下载成 WAV。"""
    orig = config_loader.tts_config

    def patched():
        d = dict(orig())
        d["voice_name"] = voice_name
        d["provider"] = "dashscope"
        return d

    config_loader.tts_config = patched
    try:
        out = voice.tts(text)
    finally:
        config_loader.tts_config = orig
    url = out.get("audio_url")
    if not url:
        segs = out.get("segments") or []
        url = segs[0].get("audio_url") if segs else None
    if not url:
        raise RuntimeError("TTS 没返回音频链接：%s" % json.dumps(out, ensure_ascii=False)[:160])
    return urllib.request.urlopen(url, timeout=20).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voices", default="", help="逗号分隔，默认试全部候选")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()
    voices = [v.strip() for v in (args.voices or "").split(",") if v.strip()] or VOICES

    os.makedirs(args.out, exist_ok=True)
    manifest = {"created": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "sample_rate": SR, "items": [], "failed_voices": []}

    for v in voices:
        ok = 0
        for tid, text in TEXTS:
            try:
                raw = _tts_wav(text, v)
                pcm, sr, ch, w = _parse_wav(raw)
                pcm16 = _to_mono16k(pcm, sr, ch, w)
                path = os.path.join(args.out, v, tid + ".wav")
                _write_wav(path, pcm16)
                dur = round(len(pcm16) / 2.0 / SR * 1000)
                manifest["items"].append({"voice": v, "id": tid, "text": text,
                                          "src_sr": sr, "ms": dur,
                                          "path": os.path.relpath(path, _ROOT).replace("\\", "/")})
                ok += 1
                print("  %-8s %s  %5d ms  %s" % (v, tid, dur, text[:16]))
            except Exception as e:
                print("  %-8s %s  失败：%s" % (v, tid, e))
        if not ok:
            manifest["failed_voices"].append(v)
            print("  [跳过] 音色 %s 一条都没成功（可能这个模型不支持该音色）" % v)

    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    vs = sorted({i["voice"] for i in manifest["items"]})
    print("\n完成：%d 条 / %d 个音色 %s" % (len(manifest["items"]), len(vs), vs))
    if len(vs) < 2:
        print("警告：少于 2 个音色，无法做说话人可分性测量。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
