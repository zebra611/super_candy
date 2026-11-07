# -*- coding: utf-8 -*-
"""读浏览器导出的校准 JSON（?vadcal=1），算出这台机器上站得住的阈值。

关键点：**先看约束有没有冲突，再谈建议值**。
    silence 不能被判成说话     → speechRms 要 > 静音的 P99
    voice   必须被判成说话     → speechRms 要 < 人声的 P25
    echo    不能触发打断       → bargeRms  要 > 回声的 P99
    voice   必须能触发打断     → bargeRms  要 < 人声的 P75
如果 echo 的 P99 比 voice 的 P25 还高，说明**外放音量下人声压不住回声**，
这时无论怎么调阈值都不对：调高就漏真插话，调低就自打断。
那种情况必须换手段（降外放音量 / 戴耳机 / 靠服务端回声否决），
脚本会直说，不会硬凑一组数字。

用法：
    python scripts/fit_vad_from_calib.py data/vad_calib/vadcal_xxx.json
"""

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def summarize(rows):
    by = {}
    for r in rows:
        by.setdefault(r["ph"], []).append(r)
    out = {}
    for ph, rs in by.items():
        rms = [r["rms"] for r in rs]
        out[ph] = {
            "frames": len(rs),
            "rms_p10": pct(rms, 0.10), "rms_p25": pct(rms, 0.25),
            "rms_p50": pct(rms, 0.50),
            "rms_p90": pct(rms, 0.90), "rms_p99": pct(rms, 0.99),
            "rms_max": max(rms),
            "zcr_p50": pct([r["zcr"] for r in rs], 0.50),
            "tilt_p50": pct([r["tilt"] for r in rs], 0.50),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="校准 JSON；省略则取 data/vad_calib 里最新的一份")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    path = args.path
    if not path:
        cand = sorted(glob.glob(os.path.join(ROOT, "data", "vad_calib", "*.json")))
        if not cand:
            print("没有校准文件。先在浏览器打开 http://127.0.0.1:8000/?vadcal=1 跑一遍，"
                  "把下载的 JSON 放进 data/vad_calib/")
            return 1
        path = cand[-1]

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("rows") or []
    if not rows:
        print("文件里没有帧数据：%s" % path)
        return 1

    S = summarize(rows)
    cur = (data.get("meta") or {}).get("front_params") or {}

    need = ("silence", "echo", "voice")
    missing = [k for k in need if k not in S]
    if missing:
        print("[!] 缺少阶段数据：%s —— 这份校准不完整，结论不可信" % ", ".join(missing))

    print("=" * 74)
    print("校准文件：%s" % os.path.basename(path))
    print("设备：%s" % ((data.get("meta") or {}).get("ua", "?"))[:80])
    print("=" * 74)
    print("%-9s %6s %7s %7s %7s %7s %7s" % ("阶段", "帧数", "p10", "p50", "p90", "p99", "max"))
    print("-" * 74)
    for ph in ("silence", "echo", "voice", "keytap"):
        if ph not in S:
            continue
        s = S[ph]
        print("%-9s %6d %7.4f %7.4f %7.4f %7.4f %7.4f"
              % (ph, s["frames"], s["rms_p10"], s["rms_p50"], s["rms_p90"],
                 s["rms_p99"], s["rms_max"]))

    verdict = []
    rec = {}

    # ---- 说话阈值：要高过静音，低过人声 ----
    if "silence" in S and "voice" in S:
        lo = S["silence"]["rms_p99"] * 1.3
        hi = S["voice"]["rms_p25"] * 0.8
        lo = max(lo, 0.004)
        if lo <= hi:
            rec["speechRms"] = round((lo + hi) / 2, 4)
            verdict.append("[OK] 说话阈值有解：静音 p99×1.3=%.4f ≤ 人声 p25×0.8=%.4f"
                           % (lo, hi))
        else:
            verdict.append("[冲突] 说话阈值无解：静音 p99×1.3=%.4f > 人声 p25×0.8=%.4f"
                           % (lo, hi))
            verdict.append("       → 底噪太高或人声太小。先降噪/靠近麦克风，别硬调阈值。")
            rec["speechRms"] = round(S["silence"]["rms_p99"] * 1.3, 4)

    # ---- 打断阈值：要高过回声，低过人声 ----
    if "echo" in S and "voice" in S:
        need_hi = S["echo"]["rms_p99"] * 1.15
        allow_hi = S["voice"]["rms_p50"]          # 人声一半的帧都要能触发
        if need_hi < allow_hi:
            rec["bargeRms"] = round(max(need_hi, (rec.get("speechRms") or 0.02) * 1.6), 4)
            verdict.append("[OK] 打断阈值有解：回声 p99×1.15=%.4f < 人声 p50=%.4f"
                           % (need_hi, allow_hi))
        else:
            verdict.append("[冲突] 打断阈值无解：回声 p99×1.15=%.4f ≥ 人声 p50=%.4f"
                           % (need_hi, allow_hi))
            verdict.append("       → 外放太响，回声压不过真人说话。可行的三条路：")
            verdict.append("         ① 演示时把外放音量调低（最省事）；")
            verdict.append("         ② 用耳机（但会失去\"外放共听\"的演示效果）；")
            verdict.append("         ③ 靠已有的服务端回声否决（app._is_self_echo）兜底。")
            verdict.append("       注意：这时**不要**为了压回声把 bargeRms 往死里调高，")
            verdict.append("       那样真人插话也会被打回来，等于把打断功能关掉。")

    if "keytap" in S and rec.get("speechRms"):
        k = S["keytap"]
        verdict.append("[参考] 键盘 p99=%.4f 对比说话阈值 %.4f → 键盘%s单独顶穿阈值"
                       % (k["rms_p99"], rec["speechRms"],
                          "会" if k["rms_p99"] >= rec["speechRms"] else "不会"))
        verdict.append("       顶穿时靠 zcr/tilt/onsetFrames 挡（键盘 zcr≈%.2f、tilt≈%.2f）"
                       % (k["zcr_p50"], k["tilt_p50"]))

    print()
    for v in verdict:
        print(v)

    print()
    print("-" * 74)
    print("前端当前值：speechRms=%.4f  bargeRms=%.4f  silenceRms=%.4f"
          % (cur.get("speechRms", 0), cur.get("bargeRms", 0), cur.get("silenceRms", 0)))
    print("建议值：" + json.dumps(rec, ensure_ascii=False))
    if rec and any(v.startswith("[冲突]") for v in verdict):
        print("       （有冲突时这里只是\"能算出来的那一半\"，别直接抄进前端）")

    if args.json:
        print(json.dumps({"summary": S, "recommend": rec,
                          "verdict": verdict}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
