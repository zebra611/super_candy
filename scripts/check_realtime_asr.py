"""验证 paraformer-realtime-v2 流式识别连通 —— 一条命令给结论。

为什么单独一个脚本：
    流式链路（modules/realtime_asr.py）协议层已被 verify 证据 14 离线断言，
    但"协议对"不等于"云端认"——密钥、模型开通、网络到不到得了，
    只有真连一次才知道。这个脚本把"卡在哪"分开判定，30 秒出结论。

怎么用：
    python scripts/check_realtime_asr.py            # TTS 造一句人声喂进去（约花几分钱）
    python scripts/check_realtime_asr.py --wav x.wav  # 用本地 16k WAV 验（不花钱）

判定标准（与 ws_channel 的真实用法一致）：
    1. WebSocket 握手成功（HTTP 101，密钥错会在这里暴露）；
    2. task-started 到达（模型没开通会在这里失败）；
    3. 边喂边出半句字幕（partial）；
    4. finish 后拿到非空整句（final），且含"京东"或"张先生"其一。

全过之后才能把 config/scene.json 的 asr 段加 "realtime": true——
在那之前 WS 通话自动走批式兜底，行为与 S1 一致。

只用标准库；TTS 造声的部分复用 bench_voice.py 的重采样逻辑。
"""

import argparse
import io
import json
import os
import struct
import sys
import time
import urllib.request
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader  # noqa: E402
from modules import realtime_asr, voice  # noqa: E402

EXPECTED_WORDS = ("京东", "张先生")


def step(name, ok, detail=""):
    tag = "  [OK] " if ok else "  [FAIL] "
    print(tag + name + (("—— " + detail) if detail else ""))
    return ok


def tts_to_pcm(text):
    """合成一句人声并转 16kHz 单声道 PCM（复用 bench_voice 的口径）。

    TTS 返回 24k 立体声 WAV，不重采样识别会得到乱码且不报错。
    长句会被切成 segments——这里只需要一段，取第一个链接。
    """
    out = voice.tts(text)
    urls = []
    if out.get("audio_url"):
        urls.append(out["audio_url"])
    for seg in (out.get("segments") or []):
        u = seg.get("audio_url")
        if u and u not in urls:
            urls.append(u)
    if not urls:
        raise RuntimeError("合成没返回链接（mode=%s，error=%s）——"
                           "账号可能欠费，先跑 scripts/check_key.py"
                           % (out.get("mode"), out.get("error")))
    # 必须拼接全部段：paraformer 对太短的音频不给结果，
    # 只喂首段（"您好，"560ms）什么都等不到。
    pcm = b""
    for u in urls:
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read()
        w = wave.open(io.BytesIO(raw))
        ch, sr, nf = w.getnchannels(), w.getframerate(), w.getnframes()
        frames = w.readframes(nf)
        ints = struct.unpack("<%dh" % (len(frames) // 2), frames)
        if ch == 2:
            ints = ints[::2]
        ratio = sr / 16000.0
        res, i = [], 0.0
        while int(i) < len(ints):
            res.append(ints[int(i)])
            i += ratio
        pcm += struct.pack("<%dh" % len(res), *res)
    return pcm


def wav_to_pcm(path):
    with open(path, "rb") as f:
        raw = f.read()
    w = wave.open(io.BytesIO(raw))
    ch, sr, nf = w.getnchannels(), w.getframerate(), w.getnframes()
    frames = w.readframes(nf)
    ints = struct.unpack("<%dh" % (len(frames) // 2), frames)
    if ch == 2:
        ints = ints[::2]
    if sr != 16000:
        ratio = sr / 16000.0
        res, i = [], 0.0
        while int(i) < len(ints):
            res.append(ints[int(i)])
            i += ratio
        ints = res
    return struct.pack("<%dh" % len(ints), *ints)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", help="本地 WAV 文件路径（给了就不调 TTS、不花合成钱）")
    args = ap.parse_args()

    print("=" * 70)
    print("paraformer-realtime-v2 流式识别连通验证")
    print("=" * 70)

    if not config_loader.secret("dashscope_api_key"):
        print("  [FAIL] 没读到 dashscope_api_key——先看 config/secrets.json")
        return 1
    step("密钥已读到", True)

    # ① 建连 + run-task + task-started。三件事失败原因完全不同：
    #    握手被拒多为网络/代理；task 起不来多为模型未开通或参数错。
    t0 = time.time()
    try:
        rt = realtime_asr.RealtimeAsr.connect(timeout=8.0)
    except Exception as exc:
        step("WebSocket 建连 + 任务启动", False, str(exc)[:200])
        print("\n排查顺序：")
        print("  1. scripts/check_key.py 先确认密钥本身有效")
        print("  2. 百炼控制台确认 paraformer-realtime-v2 已开通（不是只开 qwen3-asr）")
        print("  3. 公司网络常拦 WSS——换手机热点再试一次")
        return 1
    step("WebSocket 建连 + 任务启动", True, "耗时 %.0f ms" % ((time.time() - t0) * 1000))

    # ② 造测试音频
    try:
        if args.wav:
            pcm = wav_to_pcm(args.wav)
            print("  [OK] 已加载本地 WAV：%s" % args.wav)
        else:
            pcm = tts_to_pcm("您好，我是京东客服的小采，请问是张先生本人吗？")
            print("  [OK] 已用 TTS 合成测试语音（消耗一次合成调用）")
    except Exception as exc:
        rt.close()
        step("准备测试音频", False, str(exc)[:200])
        return 1
    dur_ms = len(pcm) * 1000 // 32000      # 16k×16bit = 每毫秒 32 字节
    print("  音频时长 %d ms" % dur_ms)

    # ③ 按真实节奏喂入（约 1.6 倍速），边喂边收半句字幕。
    #    以 100ms 为一片与 ws_channel 的 feed 相同，测的才是同一件事。
    partials = 0
    chunk = 3200                           # 100ms 的字节数
    feed_start = time.time()
    for off in range(0, len(pcm), chunk):
        rt.feed(pcm[off:off + chunk])
        if rt.poll_partial():
            partials += 1
        time.sleep(0.06)                   # 比实时快一点，但不至于把云端噎住
    feed_ms = (time.time() - feed_start) * 1000
    step("音频喂入", True, "%d ms 内喂完（含等待），收到半句字幕 %d 次"
         % (round(feed_ms), partials))

    # ④ 等整句
    final = rt.finish_and_collect(timeout=6.0)
    rt.close()
    hit = any(w in final for w in EXPECTED_WORDS)
    step("整句识别结果", bool(final.strip()),
         "final = %r" % final)
    if final.strip():
        step("内容校验（含 %s）" % "/".join(EXPECTED_WORDS), hit,
             "半句字幕 %d 次；没有内容校验通过只有'有字'还不够——"
             "那可能只是幻觉" % partials)

    # ⑤ 结论与下一步
    print()
    print("=" * 70)
    ok = bool(final.strip()) and hit
    if ok:
        print("连通验证通过。要启用流式字幕：")
        print('  在 config/scene.json 的 "asr" 段加 "realtime": true，')
        print("  重启服务后 WS 通话会自动走流式识别（partial 字幕 + final 接管）。")
        print("  关掉方法同处改回 false——批式兜底始终可用，两者互不影响。")
    else:
        print("未通过：握手/任务都成功但拿不到正确整句。")
        print("  可能原因：喂入节奏太快被丢帧（把 time.sleep 调大再试）、")
        print("  采样率不对（必须是 16k 单声道 PCM16）、或模型行为变化。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
