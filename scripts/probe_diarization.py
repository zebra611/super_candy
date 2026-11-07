"""实测 paraformer 实时识别到底给不给 speaker_id（说话人分离能不能用）。

为什么必须先测再设计：
    「用音色把旁人说的话去掉」这件事，最省事的落点不是自己写声纹，
    而是让 ASR 直接告诉我们"这句是第几个人说的"。但这条能力**是否真的返回、
    返回的编号稳不稳、多长的句子才给得出来**，文档说了不算，得喂真音频看返回。

测法：
    用 data/speaker_corpus 里两个**不同音色**的句子拼成一段对话——
        A(长句) → 静音 700ms → B(长句) → 静音 700ms → A(短句)
    如果服务端真做了分离，B 那句的 speaker_id 应该和 A 不同，而最后那句 A 又要回到 A 的编号。
    如果全是同一个编号（或压根没有这个字段），那就是不支持，别再往这条路上投入。

用法：
    python scripts/probe_diarization.py
    python scripts/probe_diarization.py --voice-a Cherry --voice-b Ethan
"""

import argparse
import json
import os
import struct
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import config_loader              # noqa: E402
from modules import voice         # noqa: E402

CORPUS = os.path.join(_ROOT, "data", "speaker_corpus")
SR = 16000


def _pcm(path):
    with open(path, "rb") as f:
        data = f.read()
    i, out = 12, None
    while i + 8 <= len(data):
        cid = data[i:i + 4]
        size = struct.unpack("<I", data[i + 4:i + 8])[0]
        if cid == b"data":
            out = data[i + 8:i + 8 + size]
        i += 8 + size + (size & 1)
    return out or b""


def _silence(ms):
    return b"\x00\x00" * int(SR * ms / 1000.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice-a", default="Cherry")
    ap.add_argument("--voice-b", default="Ethan")
    ap.add_argument("--long-id", default="t4", help="用哪条当长句")
    ap.add_argument("--short-id", default="t1", help="用哪条当短句")
    args = ap.parse_args()

    a_long = os.path.join(CORPUS, args.voice_a, args.long_id + ".wav")
    b_long = os.path.join(CORPUS, args.voice_b, args.long_id + ".wav")
    a_short = os.path.join(CORPUS, args.voice_a, args.short_id + ".wav")
    for p in (a_long, b_long, a_short):
        if not os.path.exists(p):
            print("缺语料：%s（先跑 scripts/gen_speaker_corpus.py）" % p)
            return 2

    # 打开说话人分离（默认关，见 realtime_asr.connect 的注释）
    orig = config_loader.asr_config
    def patched():
        d = dict(orig())
        d["realtime"] = True
        d["speaker_diarization"] = True
        return d
    config_loader.asr_config = patched

    pcm = (_pcm(a_long) + _silence(700) + _pcm(b_long) + _silence(700) + _pcm(a_short)
           + _silence(1200))
    print("合成对话：%.1fs｜期望说话人顺序 A → B → A" % (len(pcm) / 2.0 / SR))

    inst = voice.open_realtime_asr()
    if inst is None:
        print("建连失败：%s" % (voice.last_realtime_error() or "未知"))
        config_loader.asr_config = orig
        return 2

    try:
        step = int(SR * 0.1) * 2          # 100ms
        for i in range(0, len(pcm), step):
            inst.feed(pcm[i:i + step])
            time.sleep(0.01)              # 比实时稍快，避免服务端积压
        time.sleep(1.6)                    # 等最后一句闭合
    finally:
        sents = inst.sentences()
        inst.close()
        config_loader.asr_config = orig

    print("\n返回的整句（%d 条）：" % len(sents))
    has_spk = False
    for s in sents:
        spk = s.get("speaker")
        if spk is not None:
            has_spk = True
        print("  sid=%s spk=%s %5s~%-5s %s"
              % (s.get("sid"), spk, s.get("begin_ms"), s.get("end_ms"), s.get("text")))

    if not sents:
        print("\n结论：一条整句都没回来——先确认 paraformer 实时链路本身可用。")
        return 2
    if not has_spk:
        print("\n结论：服务端**没有返回 speaker_id**，说话人分离不可用（这条能力要另找方案）。")
        return 1

    # 判定：B 段的编号应当与 A 段不同，且结尾 A 段回到 A 的编号
    ids = [s.get("speaker") for s in sents]
    print("\n说话人序列：%s" % ids)
    distinct = len(set(ids))
    if distinct >= 2:
        print("结论：**可用**。同一条连接里出现了 %d 个不同说话人编号，"
              "可以用它把旁人说的话单独标出来。" % distinct)
        return 0
    print("结论：返回了 speaker_id 但只有一个编号——等于没分离，不可用。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
