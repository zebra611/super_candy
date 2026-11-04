"""语音链路延迟基准：30 轮真实云端调用，出 P50 / P95。

为什么需要它：
    赛题硬门槛是首字 P50 <= 2000ms，但库里能拿出的真人通话只有几轮，
    样本量根本支撑不了结论。评委问"P50 多少、怎么测的"，答不上来就是硬伤。

它怎么测（以及这么测的代价）：
    没法让机器人拿麦克风说话，所以用 TTS 把人声合成出来，再原样喂给 ASR。
    这样走的是**完全相同的服务端链路**：真云端识别、真状态机、真云端合成。
    所以 asr_ms / tts_first_ms / total_ms 都是真实测量值。

    **但 first_audio_ms（浏览器口径）测不出来**——那个数只有真实浏览器
    播放音频时才能上报。本脚本给的是服务端口径，前端口径要靠人戴着耳机实测。
    两者不能混着报，混着报就是编数据。

    另外 TTS 合成的人声比真人录音"干净"：没有环境噪声、没有口音、
    没有停顿犹豫。所以识别耗时可能偏乐观，真实场景会更慢一点。

为什么用独立库：
    基准是可重复跑的，每次跑都产生新数据。写进 data/events.db 会把
    真人通话的统计淹掉（这个坑已经踩过一次，见 scripts/clean_events.py）。

用法：
    python scripts/bench_voice.py             # 30 轮，约 3 分钟
    python scripts/bench_voice.py --rounds 10 # 少跑几轮快速看趋势
"""

import io
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import urllib.request
import wave
from atexit import register as _atexit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import events  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="sc_bench_")
events.DATA_DIR = _tmp
events.DB_PATH = os.path.join(_tmp, "bench.db")
events.init_db()
_atexit(lambda: shutil.rmtree(_tmp, ignore_errors=True))

from app import end_call, handle_turn, start_call   # noqa: E402
from modules import voice                            # noqa: E402

OUT_JSON = os.path.join(_ROOT, "data", "bench_latest.json")


# 30 句模拟用户话术，长度从 2 字到 30 字。
# 刻意覆盖真实外呼里会遇到的类型：确认身份、拒绝、质疑、报号码、长句解释。
# 长度覆盖面很重要——实测合成耗时与字数近似线性，只测短句会得出虚高的成绩。
UTTERANCES = [
    # 极短（2-4 字）：真实电话里大量是这种应答
    "是我", "对", "不是", "嗯", "你谁啊",
    # 短（5-9 字）
    "我姓张", "用着还行", "这个要收钱吗", "那有效期多久", "行吧那就办",
    # 中（10-16 字）
    "我不需要谢谢", "我现在不方便", "你怎么知道我电话的", "什么意思没听懂",
    "我要退掉这个服务", "是不是诈骗电话", "我考虑一下再说",
    # 长（17-30 字）：带停顿、带解释、带情绪
    "我上次已经说过不要打了你们怎么还打",
    "这个延保服务具体保哪些东西你跟我说清楚",
    "你们这个到底是不是京东官方的我怎么确认",
    "我需要跟我爱人商量一下明天再回复你行吗",
    "钱是从哪里扣的我卡里怎么少了三十块",
    "那你帮我登记一下吧到时候有人联系我是吧",
    "我这会儿在开会你晚点再打过来可以吗",
    "我看到短信里说要收费了是不是真的",
    "之前那个客服答应给我退的到现在也没人管",
    "手机号后四位是8866你核对一下对不对",
    "我不是本人你们打错了不要再打了",
    "我确实买过但是不记得有延保这回事",
    "你先说一下你是哪个公司的工号多少",
    "这个钱要是扣了我怎么才能退回来",
]


def tts_to_pcm(text):
    """合成一句人声，下载并转成 16kHz 单声道 PCM16 字节。

    ASR 接受的是 16k/16bit/单声道；TTS 返回的是 24k 立体声 WAV，
    所以必须重采样——不转的话识别会得到一堆乱码，而且不会报错。

    注意返回形态有两种：短句是 mode=url（一个链接），长句会被切成
    两段、返回 mode=segments。这里只是给别人造一段输入音频，不需要
    低延迟，取第一段的链接就够了——但**必须处理这条分支**，
    否则所有 16 字以上的句子都会报"合成没返回链接"。
    """
    out = voice.tts(text)
    url = out.get("audio_url")
    if not url:
        segs = out.get("segments") or []
        if segs:
            url = segs[0].get("audio_url")
    if not url:
        raise RuntimeError("合成没返回链接：mode=%s err=%s"
                           % (out.get("mode"), out.get("error")))

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()

    w = wave.open(io.BytesIO(raw))
    ch, sr, nf = w.getnchannels(), w.getframerate(), w.getnframes()
    frames = w.readframes(nf)
    ints = struct.unpack("<%dh" % (len(frames) // 2), frames)
    if ch == 2:
        ints = ints[::2]                    # 立体声取左声道
        n = len(ints)
    else:
        n = len(ints)

    ratio = sr / 16000.0
    res, i = [], 0.0
    while int(i) < n:
        res.append(ints[int(i)])
        i += ratio
    return struct.pack("<%dh" % len(res), *res)


def pct(vals, q):
    """最近秩分位数。不插值——小样本插值会造出不存在的数。"""
    if not vals:
        return 0.0
    s = sorted(vals)
    return float(s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1))))) ])


def main():
    rounds = 30
    if "--rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--rounds") + 1])

    talks = [UTTERANCES[i:i + 3] for i in range(0, min(rounds, len(UTTERANCES)), 3)]

    print("语音链路基准：%d 通电话 / 最多 %d 轮" % (len(talks), rounds))
    print("说明：输入是 TTS 合成的人声，服务端链路与真人完全一致。")
    print("     首字口径为服务端口径；浏览器口径需真人戴耳机实测。\n")
    print("%-3s %-6s %-34s %8s %9s %9s" % ("#", "字数", "说的内容", "ASR", "首段TTS", "服务端总"))
    print("-" * 78)

    rows = []
    fails = []
    n = 0
    for t_i, group in enumerate(talks, 1):
        s = start_call()
        sid = s["session_id"]
        for said in group:
            if n >= rounds:
                break
            n += 1
            try:
                pcm = tts_to_pcm(said)
                dur = len(pcm) / 32000.0
                r = handle_turn(sid, audio_bytes=pcm)
                lat = r.get("latency") or {}
                row = {
                    "n": n, "talk": t_i, "chars": len(said), "text": said,
                    "audio_sec": round(dur, 2),
                    "asr_ms": float(lat.get("asr_ms") or 0),
                    "tts_first_ms": float(lat.get("tts_first_ms") or 0),
                    "total_ms": float(lat.get("total_ms") or 0),
                    "server_first_ms": float(lat.get("server_first_audio_ms") or 0),
                    "reply": r.get("reply_text"),
                    "stage": (r.get("stage") or ""),
                    "tts_mode": (r.get("tts") or {}).get("mode"),
                }
                rows.append(row)
                print("%-3d %-6d %-34s %7.0fms %8.0fms %8.0fms"
                      % (n, row["chars"], said[:16], row["asr_ms"],
                         row["tts_first_ms"], row["total_ms"]))
                if (r.get("tts") or {}).get("mode") == "browser":
                    print("      (!) 本轮合成回落成浏览器播报：%s"
                          % str((r.get("tts") or {}).get("error"))[:60])
            except Exception as e:
                fails.append((n, said, str(e)))
                print("%-3d 失败：%s（%s）" % (n, said[:14], str(e)[:60]))
        end_call(sid)
        time.sleep(0.4)                     # 别把接口 QPS 打爆，429 会污染样本

    ok = [r for r in rows if r["asr_ms"] > 0]
    if not ok:
        print("\n没有有效样本。检查密钥是否配好（config/secrets.json）。")
        return 1

    print("\n" + "=" * 78)
    print("有效样本 %d 轮，失败 %d 轮" % (len(ok), len(fails)))
    print("=" * 78)

    def line(name, key):
        v = [r[key] for r in ok]
        print("%-14s P50=%6.0fms  P95=%6.0fms  max=%6.0fms  min=%5.0fms"
              % (name, pct(v, .5), pct(v, .95), max(v), min(v)))

    line("识别 ASR", "asr_ms")
    line("首段合成", "tts_first_ms")
    line("服务端总响应", "total_ms")
    line("服务端首字下界", "server_first_ms")

    # 按字数分组，验证"合成耗时随字数线性增长"这个结论是否仍成立
    print("\n按字数分组（验证合成耗时的增长趋势）：")
    for lo, hi in ((0, 4), (5, 9), (10, 16), (17, 40)):
        g = [r for r in ok if lo <= r["chars"] <= hi]
        if g:
            print("  %2d-%2d 字  %2d 轮  首段TTS P50=%5.0fms  服务端总 P50=%5.0fms"
                  % (lo, hi, len(g),
                     pct([r["tts_first_ms"] for r in g], .5),
                     pct([r["total_ms"] for r in g], .5)))

    total_v = [r["total_ms"] for r in ok]
    pass_gate = sum(1 for v in total_v if v <= 2000)
    print("\n门槛判定（服务端口径，不含网络往返与浏览器播放启动）：")
    print("  %d/%d 轮 <= 2000ms，占 %.0f%%" % (pass_gate, len(ok), pass_gate * 100.0 / len(ok)))
    print("  注意：浏览器实测还要加一个网络往返 + 播放启动，约 100-250ms。")

    fallback = sum(1 for r in ok if r["tts_mode"] == "browser")
    if fallback:
        print("\n  (!) %d 轮合成回落成浏览器播报，这些轮的首字口径不含真实合成耗时。"
              % fallback)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rounds": len(ok),
            "failed": len(fails),
            "failed_detail": [{"n": n, "text": t, "error": e} for n, t, e in fails],
            "note": "输入为 TTS 合成人声；首字为服务端口径，非浏览器实测",
            "summary": {
                "asr_p50": pct([r["asr_ms"] for r in ok], .5),
                "asr_p95": pct([r["asr_ms"] for r in ok], .95),
                "tts_first_p50": pct([r["tts_first_ms"] for r in ok], .5),
                "tts_first_p95": pct([r["tts_first_ms"] for r in ok], .95),
                "total_p50": pct(total_v, .5),
                "total_p95": pct(total_v, .95),
                "within_gate": "%d/%d" % (pass_gate, len(ok)),
            },
            "rows": rows,
        }, f, ensure_ascii=False, indent=2)
    print("\n明细已写入 %s" % OUT_JSON)
    return 0


if __name__ == "__main__":
    sys.exit(main())
