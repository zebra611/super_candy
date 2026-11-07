"""VAD 基准 —— 同一批语料上跑四个引擎，用数字回答"噪声还会不会触发"。

四个引擎（前两个是"改造前"的对照组，缺了它们就说不清改进了多少）：
    bytes     老 vad_detect 的行为：只要有字节就 started=True
              → 噪声误触率必然 100%，这是被替换掉的缺陷本身
    rms       老 asr() 的静音闸门：整段 rms ≥ silence_rms 就算有人
              → 稳态噪声（风扇、白噪）全部误触；瞬态噪声（键盘）可能漏
    spectral  纯标准库谱特征门（零依赖默认引擎）
    silero    ONNX 模型（装了 onnxruntime 才有）

判定口径（只看三件事，别混在一起说）：
    人声召回   12 条真人声里有多少条 started=True      —— 漏判会让系统"装没听见"
    噪声误触   9 类噪声里有多少条 started=True          —— 误判会把状态机推到"用户说话"
    混合召回   3 条"人声+噪声"里有多少条仍判出人声      —— 真实通话的常态

为什么噪声误触是**更严重**的错误：
    漏判的后果是"重问一次"，用户会再说一遍；
    误判的后果是拿噪声去识别，识别出乱码后进入核身失败计数，
    连续几次就把在线用户挂断了（C6-09 就是这么发生的）。

怎么用：
    python scripts/bench_vad.py              # 全部引擎，人声缺失则只跑噪声
    python scripts/bench_vad.py --json       # 机读输出（verify 用这个）
    python scripts/bench_vad.py --engine spectral --repeat 3   # 单引擎测耗时

语料由 scripts/gen_vad_corpus.py 生成，不进仓库（*.wav 已 gitignore）。
"""

import argparse
import json
import os
import sys
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader  # noqa: E402
from modules import silero_vad, vad_gate, voice  # noqa: E402

CORPUS = os.path.join(ROOT, "data", "vad_corpus")


def _read_pcm(path):
    with wave.open(path) as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    if sr != 16000:
        raise RuntimeError("%s 采样率 %d，语料必须是 16k" % (path, sr))
    return raw


def _list(dirname, root=None):
    d = os.path.join(root or CORPUS, dirname)
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".wav"))


def _run_bytes(pcm):
    """对照组：老实现。有字节就算说了话——这正是 Z6 要替换掉的东西。"""
    return {"started": bool(pcm), "speech_ms": int(len(pcm) / 32), "engine": "bytes"}


def _run_rms(pcm):
    """对照组：asr() 的静音闸门（整段 rms 阈值）。"""
    level = voice.pcm_rms(pcm)
    thr = float(config_loader.asr_config().get("silence_rms", 0.003))
    return {"started": level >= thr, "speech_ms": int(len(pcm) / 32) if level >= thr else 0,
            "engine": "rms", "rms": round(level, 4)}


ENGINES = {
    "bytes": _run_bytes,
    "rms": _run_rms,
    "spectral": lambda pcm: vad_gate.analyze(pcm, prefer="spectral"),
    "silero": lambda pcm: vad_gate.analyze(pcm, prefer="silero"),
}


def bench(engines, repeat=1, root=None):
    speech = _list("speech", root)
    noise = _list("noise", root)
    mixed = _list("mixed", root)
    if not speech:
        print("  [注意] 人声语料为空（没跑 gen_vad_corpus 或合成失败），"
              "本轮只能给噪声侧结论。")

    rows = {}
    for name in engines:
        fn = ENGINES[name]
        rows[name] = {"speech": [], "noise": [], "mixed": [], "cost": []}

        for path in speech + noise + mixed:
            kind = ("speech" if "/speech/" in path.replace("\\", "/")
                    else ("noise" if "/noise/" in path.replace("\\", "/") else "mixed"))
            pcm = _read_pcm(path)
            t0 = time.time()
            out = fn(pcm)
            dt = (time.time() - t0) * 1000
            rows[name][kind].append({
                "file": os.path.basename(path),
                "started": bool(out.get("started")),
                "speech_ms": int(out.get("speech_ms") or 0),
                "audio_ms": int(len(pcm) / 32),
                "ms": round(dt, 1),
            })
            if kind == "speech" or repeat > 1:
                rows[name]["cost"].append(dt)

    return rows, {"speech": len(speech), "noise": len(noise), "mixed": len(mixed)}


def summarize(rows, counts, root=None):
    print("=" * 78)
    print("VAD 基准：人声召回 / 噪声误触 / 混合召回")
    print("=" * 78)
    print("语料：人声 %d 条，噪声 %d 条，混合 %d 条\n"
          % (counts["speech"], counts["noise"], counts["mixed"]))

    head = "%-9s %10s %10s %10s %12s %10s" % (
        "引擎", "人声召回", "噪声误触", "混合召回", "人声speech_ms", "耗时均值")
    print(head)
    print("-" * 78)
    summary = {}
    for name, r in rows.items():
        sp = r["speech"]
        nz = r["noise"]
        mx = r["mixed"]
        sp_hit = sum(1 for x in sp if x["started"])
        nz_hit = sum(1 for x in nz if x["started"])
        mx_hit = sum(1 for x in mx if x["started"])
        cost = (sum(r["cost"]) / len(r["cost"])) if r["cost"] else 0.0
        ratio = (sum(x["speech_ms"] for x in sp) / max(1, sum(x["audio_ms"] for x in sp)))
        summary[name] = {
            "speech_hit": sp_hit, "speech_total": len(sp),
            "noise_hit": nz_hit, "noise_total": len(nz),
            "mixed_hit": mx_hit, "mixed_total": len(mx),
            "avg_ms": round(cost, 1),
            "speech_ms_ratio": round(ratio, 3),
        }
        print("%-9s %6d/%-3d %6d/%-3d %6d/%-3d %12.2f %8.1fms" % (
            name, sp_hit, len(sp), nz_hit, len(nz), mx_hit, len(mx), ratio, cost))

    # 逐条明细只打"判错"的那些：对的不用看，错的必须能定位到文件
    print("\n判错明细（空的就说明这一类全对）：")
    any_wrong = False
    for name, r in rows.items():
        for kind in ("speech", "noise", "mixed"):
            for x in r[kind]:
                wrong = (not x["started"]) if kind != "noise" else x["started"]
                if wrong:
                    any_wrong = True
                    print("  [%s] %-6s %-22s started=%s speech_ms=%d/%d"
                          % (name, kind, x["file"], x["started"],
                             x["speech_ms"], x["audio_ms"]))
    if not any_wrong:
        print("  （无）")

    print("\n噪声逐条（判成人声的会标 ←误触）：")
    for path in _list("noise", root):
        base = os.path.basename(path)
        flags = []
        for name, r in rows.items():
            x = next((y for y in r["noise"] if y["file"] == base), None)
            if x:
                flags.append("%s=%s" % (name, "Y" if x["started"] else "-"))
        print("  %-22s %s" % (base, "  ".join(flags)))

    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="只输出 JSON（verify 用）")
    ap.add_argument("--engine", action="append", help="只跑指定引擎，可重复")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "vad_bench_latest.json"))
    ap.add_argument("--corpus", default=CORPUS, help="语料目录（留出集用 data/vad_corpus_holdout）")
    args = ap.parse_args()

    engines = args.engine or ["bytes", "rms", "spectral", "silero"]
    if "silero" in engines and not silero_vad.available():
        print("  [注意] Silero 不可用：%s" % silero_vad.reason_unavailable())
        engines = [e for e in engines if e != "silero"]
    if not engines:
        print("没有可跑的引擎。")
        return 1

    rows, counts = bench(engines, repeat=args.repeat, root=args.corpus)
    if not args.json:
        summary = summarize(rows, counts, root=args.corpus)
    else:
        summary = {}
        for name, r in rows.items():
            summary[name] = {
                "speech_hit": sum(1 for x in r["speech"] if x["started"]),
                "speech_total": len(r["speech"]),
                "noise_hit": sum(1 for x in r["noise"] if x["started"]),
                "noise_total": len(r["noise"]),
                "mixed_hit": sum(1 for x in r["mixed"] if x["started"]),
                "mixed_total": len(r["mixed"]),
                "avg_ms": round(sum(r["cost"]) / max(1, len(r["cost"])), 1),
            }

    payload = {"counts": counts, "summary": summary,
               "detail": {name: {k: rows[name][k] for k in ("speech", "noise", "mixed")}
                          for name in rows}}
    try:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except OSError as exc:
        print("  [警告] 报告没写出去：%s" % exc)

    if args.json:
        print(json.dumps({"counts": counts, "summary": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
