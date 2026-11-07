# -*- coding: utf-8 -*-
"""在**真实录音**上算前端 VAD 判据的实际分布——用来回答"阈值站不站得住"。

合成的语料里静音就是 0，回声就是我乘了个 0.05，所以怎么调都全过。
真录之后第一件事就是量：底噪到底多高、外放回声到底能到多少、
连续多少帧会顶穿 bargeRms（那才是"自己打断自己"的触发条件）。
"""

import argparse
import json
import os
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import tune_barge_vad as T          # 复用与前端一字不差的判据


def load(path):
    with wave.open(path) as w:
        assert w.getframerate() == 16000, "只吃 16k"
        raw = w.readframes(w.getnframes())
    return [int.from_bytes(raw[i:i + 2], "little", signed=True)
            for i in range(0, len(raw) - 1, 2)]


def longest_run(flags):
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


def analyze(path, p):
    pcm = load(path)
    n = 320                     # 20ms @16k
    rows = []
    for i in range(0, len(pcm) - n + 1, n):
        voiced, rms, zcr, tilt = T.analyze_frame(pcm[i:i + n], p)
        rows.append((rms, zcr, tilt, voiced))
    if not rows:
        return None
    rms = [r[0] for r in rows]
    over_speech = [r >= p["speechRms"] for r in rms]
    over_barge = [r >= p["bargeRms"] for r in rms]
    return {
        "file": os.path.basename(path),
        "frames": len(rows),
        "rms_p50": round(pct(rms, 0.50), 4),
        "rms_p90": round(pct(rms, 0.90), 4),
        "rms_max": round(max(rms), 4),
        "voiced_ratio": round(sum(1 for r in rows if r[3]) / len(rows), 3),
        "over_speech_ratio": round(sum(over_speech) / len(rows), 3),
        "over_barge_ratio": round(sum(over_barge) / len(rows), 3),
        "over_barge_run": longest_run(over_barge),
        # 前端触发"疑似打断"的条件：连续 bargeFrames 帧 >= bargeRms
        "would_barge": longest_run(over_barge) >= p["bargeFrames"],
        "zcr_p50": round(pct([r[1] for r in rows], 0.50), 3),
        "tilt_p50": round(pct([r[2] for r in rows], 0.50), 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "data", "vad_real"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    p = T.load_front_params()
    p.setdefault("zcrMax", 1.0)
    p.setdefault("tiltMin", 0.0)
    p.setdefault("onsetFrames", 1)

    files = sorted(f for f in os.listdir(args.dir)
                   if f.startswith("real_") and f.endswith(".wav")) \
        if os.path.isdir(args.dir) else []
    if not files:
        print("没有真实录音：先跑 python scripts/record_real_vad.py")
        return 1

    out = []
    if not args.json:
        print("当前前端参数：speechRms=%.3f bargeRms=%.3f silenceRms=%.3f "
              "endpoint=%d帧 zcrMax=%.2f tiltMin=%.2f"
              % (p["speechRms"], p["bargeRms"], p.get("silenceRms", 0),
                 p["endpointFrames"], p.get("zcrMax", 1.0), p.get("tiltMin", 0.0)))
        print("=" * 96)
        print("%-22s %6s %6s %6s %6s | %7s %7s %7s %6s"
              % ("文件", "rms50", "rms90", "rmsMax", "voiced", "≥speech", "≥barge", "最长连", "会打断"))
        print("-" * 96)
    for f in files:
        r = analyze(os.path.join(args.dir, f), p)
        if not r:
            continue
        out.append(r)
        if not args.json:
            print("%-22s %6.4f %6.4f %6.4f %6.2f | %7.2f %7.2f %7d %6s"
                  % (r["file"], r["rms_p50"], r["rms_p90"], r["rms_max"],
                     r["voiced_ratio"], r["over_speech_ratio"],
                     r["over_barge_ratio"], r["over_barge_run"],
                     "是" if r["would_barge"] else "否"))

    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
