"""标定说话人一致性门：先量"能不能分开"，再从分布里定阈值。

为什么必须先量再定阈值：
    拍脑袋写 0.6 / 0.25，上线后只有两种结果——谁都判"不是本人"，或谁都不判。
    阈值只能从**分数分布**里来：同人分布与异人分布重叠多少，重叠区就只能弃权。

协议（三段）：
    ① 成对可比性：同音色不同句子 vs 不同音色 → 分布 + EER；
    ② 真实用法模拟：每音色 3 条注册、留出 2 条当本人、其余音色全部当旁人 →
       看"误杀"（本人被判旁人）与"漏放"（旁人被判本人）；
    ③ 分时长看：短句（<2s）与长句分开算。短句是这套方案最弱的地方，
       必须单独暴露，不能靠平均值掩盖。

诚实的边界：
    · 语料是 **TTS 不同音色**，不是真人。只能证明"音色差异可被这套嵌入分开"，
      不能当真机 EER——真人还有口音/语速/房间差异，通常更难；
    · 每个音色 5 条，注册 3 条后只剩 2 条做本人测试，样本极小，
      FAR/FRR 只看数量级；
    · 同一批语料既标定阈值又评估，指标偏乐观（上界）。

用法：
    python scripts/gen_speaker_corpus.py
    python scripts/fetch_speaker_model.py
    python scripts/bench_speaker_gate.py
"""

import argparse
import itertools
import json
import os
import struct
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from modules import speaker_gate as sg  # noqa: E402

CORPUS = os.path.join(_ROOT, "data", "speaker_corpus")
CALIB = os.path.join(_ROOT, "data", "speaker_calib.json")


def _read_wav_pcm(path):
    with open(path, "rb") as f:
        data = f.read()
    i, pcm = 12, None
    while i + 8 <= len(data):
        cid = data[i:i + 4]
        size = struct.unpack("<I", data[i + 4:i + 8])[0]
        if cid == b"data":
            pcm = data[i + 8:i + 8 + size]
        i += 8 + size + (size & 1)
    return pcm or b""


def _load():
    with open(os.path.join(CORPUS, "manifest.json"), "r", encoding="utf-8") as f:
        man = json.load(f)
    items = []
    for it in man["items"]:
        p = os.path.join(_ROOT, it["path"])
        if not os.path.exists(p):
            continue
        emb, diag = sg.embed(_read_wav_pcm(p))
        if emb is None:
            print("  [跳过] %s %s：%s" % (it["voice"], it["id"], diag.get("reason")))
            continue
        items.append({"voice": it["voice"], "id": it["id"], "text": it["text"],
                      "ms": diag.get("ms", 0), "emb": emb})
    return items


def _eer(same, diff):
    """等错误率：扫阈值，取 FRR 与 FAR 最接近的点，EER=(FRR+FAR)/2。

    上一版把**阈值本身**当成 EER 打印出来了（0.996 显示成 99.6%）——
    这种"数字看着像结论"的 bug 最容易骗过自己，所以这里写清楚口径。
    """
    if not same or not diff:
        return None, None
    best = None
    for t in sorted(set(same + diff), reverse=True):
        frr = sum(1 for s in same if s < t) / float(len(same))
        far = sum(1 for d in diff if d >= t) / float(len(diff))
        gap = abs(frr - far)
        if best is None or gap < best[0]:
            best = (gap, t, frr, far, (frr + far) / 2.0)
    worst = min(
        ((max(sum(1 for s in same if s < t) / float(len(same)),
              sum(1 for d in diff if d >= t) / float(len(diff))), t)
         for t in sorted(set(same + diff), reverse=True)))
    return best[4], {"threshold": round(best[1], 3), "frr": round(best[2], 4),
                     "far": round(best[3], 4),
                     "min_worst_case": round(worst[0], 4),
                     "min_worst_threshold": round(worst[1], 3)}


def _stat(x):
    if not x:
        return {}
    x = sorted(x)
    return {"n": len(x), "min": round(x[0], 3), "p05": round(x[int(len(x) * .05)], 3),
            "mean": round(sum(x) / len(x), 3), "p95": round(x[int(len(x) * .95)], 3),
            "max": round(x[-1], 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(_ROOT, "data", "speaker_bench.json"))
    args = ap.parse_args()

    if not os.path.exists(os.path.join(CORPUS, "manifest.json")):
        print("没有语料，先跑：python scripts/gen_speaker_corpus.py")
        return 2
    ok, why = sg.available()
    if not ok and "未标定" not in why:
        print("说话人门不可用：%s" % why)
        return 2

    items = _load()
    voices = sorted({i["voice"] for i in items})
    print("语料：%d 条 / %d 个音色 %s" % (len(items), len(voices), voices))
    if len(voices) < 2:
        print("少于 2 个音色，无法测量")
        return 2

    # ---- ① 成对分布 ----
    same, diff = [], []
    for a, b in itertools.combinations(items, 2):
        s = sg.cosine(a["emb"], b["emb"])
        (same if a["voice"] == b["voice"] else diff).append(s)
    print("\n同人（同音色不同句子）：%s" % _stat(same))
    print("异人（不同音色）：      %s" % _stat(diff))
    eer, detail = _eer(same, diff)
    print("EER = %s（同人均值 %.3f / 异人均值 %.3f）"
          % ("%.1f%%" % (eer * 100) if eer is not None else "-",
             _stat(same).get("mean", 0), _stat(diff).get("mean", 0)))
    if detail:
        print("  等错误点：阈值 %.3f，FRR %.1f%%，FAR %.1f%%｜最坏侧最小 %.1f%%（阈值 %.3f）"
              % (detail["threshold"], detail["frr"] * 100, detail["far"] * 100,
                 detail["min_worst_case"] * 100, detail["min_worst_threshold"]))

    # ---- ③ 分时长 ----
    bucket = {}
    for label, lo, hi in (("短句<2.0s", 0, 2000), ("长句>=2.0s", 2000, 10 ** 9)):
        s_, d_ = [], []
        for a, b in itertools.combinations(items, 2):
            if not (lo <= a["ms"] < hi and lo <= b["ms"] < hi):
                continue
            v = sg.cosine(a["emb"], b["emb"])
            (s_ if a["voice"] == b["voice"] else d_).append(v)
        if s_ and d_:
            e_, _ = _eer(s_, d_)
            bucket[label] = {"same_n": len(s_), "diff_n": len(d_),
                             "same_mean": _stat(s_)["mean"],
                             "diff_mean": _stat(d_)["mean"],
                             "eer": None if e_ is None else round(e_, 4)}
            print("  %s：同人 n=%d(%.3f)｜异人 n=%d(%.3f)｜EER %s"
                  % (label, len(s_), _stat(s_)["mean"], len(d_), _stat(d_)["mean"],
                     "%.1f%%" % (e_ * 100) if e_ is not None else "-"))

    # ---- ② 真实用法 ----
    enroll_n = 3

    def centroid_of(v):
        mine = [i for i in items if i["voice"] == v][:enroll_n]
        s = np.zeros(len(mine[0]["emb"]), dtype=np.float32)
        for it in mine:
            s += np.asarray(it["emb"], dtype=np.float32)
        s /= len(mine)
        return (s / np.linalg.norm(s)).tolist()

    cents = {v: centroid_of(v) for v in voices}
    true_scores = [sg.cosine(cents[v], it["emb"])
                   for v in voices for it in [i for i in items if i["voice"] == v][enroll_n:]]
    imp_scores = [sg.cosine(cents[v], it["emb"])
                  for v in voices for it in items if it["voice"] != v]

    # 阈值口径：**先保证本人一条都不误杀**（FRR=0），在此前提下尽量多挡旁人。
    # 注意必须**从高往低**扫：判"旁人"的条件是 score <= reject_th，
    # 所以要尽量把 reject_th 抬高，抬到刚好低于本人最低分为止。
    # 上一版从低往高扫，第一个"不误杀"的点就是最小值，直接把 99% 的旁人放过去了。
    cands = sorted(set(true_scores + imp_scores), reverse=True)
    chosen = cands[-1]
    for t in cands:
        if sum(1 for s in true_scores if s <= t) == 0:
            chosen = t
            break
    frr_at = sum(1 for s in true_scores if s <= chosen) / float(len(true_scores))
    far_at = sum(1 for s in imp_scores if s > chosen) / float(len(imp_scores))

    print("\n真实用法模拟（注册 %d 条/音色；本人 %d 条，旁人 %d 条）："
          % (enroll_n, len(true_scores), len(imp_scores)))
    print("  本人得分：%s" % _stat(true_scores))
    print("  旁人得分：%s" % _stat(imp_scores))
    print("  选定 reject=%.3f → 误杀 %.1f%%（%d/%d）｜漏放 %.1f%%（%d/%d）"
          % (chosen, frr_at * 100,
             sum(1 for s in true_scores if s <= chosen), len(true_scores),
             far_at * 100, sum(1 for s in imp_scores if s > chosen), len(imp_scores)))

    # 音色两两相似度：谁和谁容易混，必须打出来。
    # 只说"EER 8%"没法指导上线——运营要知道"同性别近音色"到底差多少。
    pair = {}
    for i, va in enumerate(voices):
        for vb in voices[i + 1:]:
            s = [sg.cosine(a["emb"], b["emb"]) for a in items if a["voice"] == va
                 for b in items if b["voice"] == vb]
            pair["%s~%s" % (va, vb)] = round(sum(s) / len(s), 3)
    print("\n音色两两平均相似度（越高越容易混）：")
    for k, v in sorted(pair.items(), key=lambda kv: -kv[1]):
        print("  %-18s %.3f" % (k, v))

    # accept 阈值：比 reject 高一截，中间留给 uncertain。
    # 不用 EER 点当 accept：EER 点两侧各错一半，对"判成旁人"来说太激进。
    accept_th = round(min(0.92, chosen + 0.10), 3)
    payload = {
        "corpus": os.path.relpath(CORPUS, _ROOT).replace("\\", "/"),
        "n_items": len(items), "voices": voices, "dim": len(items[0]["emb"]),
        "same": _stat(same), "diff": _stat(diff),
        "eer": None if eer is None else round(eer, 4), "eer_detail": detail,
        "by_duration": bucket, "voice_pairs": pair,
        "protocol": {"enroll": enroll_n, "true_n": len(true_scores),
                     "impostor_n": len(imp_scores),
                     "false_reject_rate": round(frr_at, 4),
                     "false_accept_rate": round(far_at, 4)},
        "reject_threshold": chosen, "accept_threshold": accept_th,
        "note": "TTS 不同音色语料；只证明音色差异可分，真人更难，勿当真机 EER；"
                "标定与评估同批语料，指标偏乐观",
    }
    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    with open(args.json, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    calib = {"accept_threshold": accept_th, "reject_threshold": chosen,
             "eer": payload["eer"], "n_items": len(items), "dim": payload["dim"],
             "same_mean": payload["same"].get("mean"),
             "diff_mean": payload["diff"].get("mean"),
             "_note": payload["note"]}
    with open(CALIB, "w", encoding="utf-8", newline="\n") as f:
        json.dump(calib, f, ensure_ascii=False, indent=1)
    print("\n明细：%s\n标定：%s" % (os.path.relpath(args.json, _ROOT),
                                os.path.relpath(CALIB, _ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
