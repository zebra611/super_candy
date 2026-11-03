"""拿一段真人录音，验证云端识别到底认得准不准 —— 会花钱、会联网，所以单独一个脚本。

为什么不做进 verify.py：
    verify.py 要能离线、不花钱、不依赖密钥，随时能跑。
    识别准不准这件事只能拿真录音试，属于"人工主动验证"。

怎么用：
    python scripts/test_asr_file.py 你的录音.wav
    python scripts/test_asr_file.py 录音.mp3 也可能是 mp3 —— 只要云端能解

    没有现成录音？用手机录一段，或者直接在页面里点「按下说话」说一句，
    然后看事件库里 asr_result 的 text 字段。

它会：
    1. 读音频，转成契约要求的 PCM16 / 16kHz / 单声道（不满足就重采样）；
    2. 调一次真实识别，打印识别文本、耗时、音量；
    3. 打印"这句话里的数字有没有被转成阿拉伯数字"（ITN 是否生效）——
       身份核验要靠数字匹配，这一项不对，用户说尾号就对不上。

注意：识别按音频秒数计费，一次一两分钱。
"""

import os
import sys
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader  # noqa: E402
import contracts  # noqa: E402
from modules import voice  # noqa: E402


def read_wav_to_pcm(path):
    """读 WAV → 契约要求的 PCM16 / 16k / 单声道字节。

    为什么要重采样：云端按 WAV 头里的采样率解码。如果采样率标错
    （比如 44.1k 的音频标成 16k），返回的文字会"能出来但全是错的"，
    比直接报错更难查。所以这里按真实采样率重采样，不猜。
    """
    with wave.open(path, "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())

    if width != contracts.PCM_SAMPLE_WIDTH:
        raise SystemExit("只支持 16bit WAV，这段是 %dbit。先用 Audacity 或 ffmpeg 转一下。"
                         % (width * 8))

    # 下混成单声道
    if channels > 1:
        import struct
        n = len(frames) // (2 * channels)
        vals = struct.unpack("<%dh" % (n * channels), frames[:n * channels * 2])
        mono = [sum(vals[i * channels:(i + 1) * channels]) // channels for i in range(n)]
        frames = struct.pack("<%dh" % n, *mono)

    # 重采样到 16k（线性插值，够用；识别前不做高质量重采样）
    if rate != contracts.PCM_SAMPLE_RATE:
        import struct
        n = len(frames) // 2
        src = struct.unpack("<%dh" % n, frames[:n * 2])
        ratio = rate / float(contracts.PCM_SAMPLE_RATE)
        out_n = int(n / ratio)
        out = []
        for i in range(out_n):
            pos = i * ratio
            j = int(pos)
            f = pos - j
            a = src[j] if j < n else 0
            b = src[j + 1] if j + 1 < n else a
            out.append(int(a * (1 - f) + b * f))
        frames = struct.pack("<%dh" % out_n, *out)
        print("  重采样：%dHz → %dHz（%d → %d 采样）"
              % (rate, contracts.PCM_SAMPLE_RATE, n, out_n))

    return frames


def has_digits(text):
    return any("0" <= ch <= "9" for ch in text)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    if not os.path.exists(path):
        print("找不到文件：%s" % path)
        return 2

    print("=" * 68)
    print("识别一段真实录音")
    print("=" * 68)
    provider = config_loader.asr_provider()
    cfg = config_loader.asr_config()
    print("  provider：%s" % provider)
    print("  model   ：%s" % cfg.get("model"))
    print("  文件    ：%s" % path)
    if provider == "placeholder":
        print()
        print("  ⚠️ 现在走的是占位识别（没配密钥或 provider 写的 placeholder），")
        print("     跑出来只会得到那句固定的『（假识别）…』，不代表识别能力。")
        print("     配好 config/secrets.json 再来。")
        return 2

    pcm = read_wav_to_pcm(path)
    seconds = len(pcm) / 2.0 / contracts.PCM_SAMPLE_RATE
    print("  时长    ：%.2f 秒（≈ %.4f 元）" % (seconds, seconds * 0.00024))

    t0 = time.time()
    out = voice.asr(pcm)
    wall = (time.time() - t0) * 1000

    print()
    print("  识别结果：%r" % out.get("text"))
    print("  识别耗时：%.0fms（端到端 %.0fms）" % (out.get("latency_ms") or 0, wall))
    print("  音量 rms：%s（静音门限 %s）" % (out.get("rms"), cfg.get("silence_rms")))
    print("  fake    ：%s（false 表示这是真识别结果）" % out.get("fake"))
    if out.get("skipped") == "silent":
        print("  ⚠️ 被静音闸门挡掉了。如果你确实说了话，说明录音音量太低，")
        print("     把 config/scene.json 的 asr.silence_rms 调小（比如 0.001）。")
    if out.get("error"):
        print("  ❌ 出错：%s" % out["error"])

    text = out.get("text") or ""
    if text:
        print()
        print("  数字是否被转成阿拉伯数字（ITN）：%s" % ("是" if has_digits(text) else "否"))
        print("  → 如果录音里你说了尾号/订单号，这里应该是数字；否则身份核验会对不上，")
        print("    把 config/scene.json 的 asr.itn 设成 true。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
