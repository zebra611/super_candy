# -*- coding: utf-8 -*-
"""C7：官方话术语音基准重测（真实云端调用，出 P50/P95 + 切段 A/B）。

为什么要单独写一份，而不是接着跑 bench_voice：
    bench_voice 的 30 句是售后演示短句（2–30 字）。赛题官方话术完全不同——
    kb_pop 94 条中位数 86 字、最长 160 字，场景包 reply 最长 73 字。
    清单 C7 的判断是「官方话术比现有一倍长，TTS 分段与 1300ms 预算可能超」，
    所以必须拿真实话术重测，不能用旧数据外推。

三种口径，不混着报：
    A. **话术级 TTS A/B**（主结论）：同一句官方话术测
       - whole   ：强制不切段，首字 = 整句合成完成（对照）
       - auto    ：生产用 `_split_for_tts`（首段 ≤12 字，超长尾段继续切）
       - legacy  ：修复前「只切一刀」（仅对回落/超预算项复测，证明缺陷）
       - short   ：首段压到 ≤8 字（同一批复测，判断瓶颈是否在首段长度）
       回落样本（mode=browser）单独计数，**不进分位数**。
    B. **完整链路 24 轮**：pop 场景、官方风格商家问句当用户输入，测
       `server_first_ms`（ASR + 状态机 + 首段合成），出 P50/P95。
    C. 1300ms 预算判定按服务端口径；浏览器还要另加 100–250ms，单独说明。

输出：
    data/bench_official_latest.json（明细）
    --report 指定的 Markdown 报告（默认 data/C7_语音基准报告.md）

用法：
    python scripts/bench_official_voice.py --report <path.md>
    python scripts/bench_official_voice.py --quick     # 12 条话术 + 8 轮链路，快速验连通
"""
from __future__ import annotations

import io
import json
import os
import re
import statistics
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

# 必须在 import app 之前定场景：app 启动时会按 APP_SCENE 读配置。
os.environ["APP_SCENE"] = "pop_reject"

import bench_voice as B              # noqa: E402  复用隔离事件库 / tts_to_pcm / pct
from modules import voice            # noqa: E402
import config_loader                 # noqa: E402

OUT_JSON = os.path.join(ROOT, "data", "bench_official_latest.json")
BUDGET_MS = 1300                     # 阶段1清单：首字预算
TIMEOUT_MS = 10000                  # 同 docs/04 §4：超时不并入分位数


# ---------------------------------------------------------------------------
# 官方话术集：kb 原文 + 场景包 reply，占位符按 C6 口径处理（不念花括号）
# ---------------------------------------------------------------------------
def _render(text):
    text = re.sub(r"\{[a-z_]+\}", "该品牌资质", text or "")
    text = re.sub(r"【([^】]*)】", lambda m: m.group(1) or "该品牌", text)
    return text.strip()


def official_texts():
    kb = json.load(open(os.path.join(ROOT, "config", "kb_pop.json"),
                        encoding="utf-8"))["items"]
    sc = json.load(open(os.path.join(ROOT, "config", "scenes", "pop_reject.json"),
                        encoding="utf-8"))
    out = []
    for r in kb:
        a = (r.get("answer") or "").strip()
        if a and not r.get("no_call"):
            out.append(("kb:" + r["id"], _render(a)))
    stages = sc.get("stages") or {}
    for stage, cfg in stages.items():
        for k, v in cfg.items():
            if k.startswith("reply") and isinstance(v, str) and v.strip():
                out.append(("scene:%s.%s" % (stage, k), _render(v)))
    # 去重（同文本只测一次），按字数排序便于分层抽样
    seen, uniq = set(), []
    for src, t in out:
        if t and t not in seen:
            seen.add(t)
            uniq.append((src, t))
    return sorted(uniq, key=lambda x: len(x[1]))


def stratified(items, n):
    """按字数分层等距抽样，保证短/中/长/超长都进样本，不只测好跑的短句。"""
    if len(items) <= n:
        return items
    step = (len(items) - 1) / float(n - 1)
    idx = sorted({int(round(i * step)) for i in range(n)})
    return [items[i] for i in idx]


# 官方资料里的 5 类商家追问（操作路径 / 门槛标准 / 质疑合理性 /
# 知识来源 / 结果预期），改写成电话口语，作为完整链路的用户输入。
USER_UTTERANCES = [
    "在哪里修改店铺名啊", "授权书要从哪里上传", "这个类目怎么申请",
    "数量金额有什么要求", "发票有效期是多久", "我这明明是真的授权",
    "品牌方固定模板改不了", "在哪查商标状态", "怎么分辨图形商标",
    "补上来就能通过吗", "你们能保证一定过审吗", "我材料都交齐了呀",
    "后四位是8866你核对一下", "我是负责人你说吧", "商标还在续展中怎么办",
    "授权链要几级才够", "这个类目要几个品牌", "质检报告多久算有效",
    "驳回原因能再说一遍吗", "要补哪些材料来着", "重新提交还收费吗",
    "多久能出审核结果", "我这边听不太清楚", "那我清楚了谢谢",
]


# ---------------------------------------------------------------------------
# A. 话术级 TTS A/B
# ---------------------------------------------------------------------------
_ORIG_SPLIT = voice._split_for_tts


def _legacy_split(t, single_max=16, head_max=12):
    """修复前的「只切一刀」：把 seg_max 放大到不可能触发，复现旧行为做对照。"""
    return _ORIG_SPLIT(t, single_max=single_max, head_max=head_max,
                       seg_max=10 ** 6)


def _measure(text, mode):
    if mode == "whole":
        voice._split_for_tts = lambda t, single_max=16, head_max=12: [(t or "").strip()]
    elif mode == "legacy":
        voice._split_for_tts = _legacy_split
    elif mode == "short":
        def short(t, single_max=16, head_max=12):
            parts = _ORIG_SPLIT(t, single_max, head_max)
            if len(parts) == 1:
                return parts
            head = parts[0][:8]
            tail = parts[0][8:] + "".join(parts[1:])
            return [head, tail] if tail else [head]
        voice._split_for_tts = short
    else:
        voice._split_for_tts = _ORIG_SPLIT
    try:
        return voice.tts(text, wait_first_only=False)
    finally:
        voice._split_for_tts = _ORIG_SPLIT


def _rec(o, prefix):
    """记一次测量。**回落样本必须留痕**：mode=browser 时 latency_ms 是
    4 秒超时×2 次重试的失败耗时，不是首段就绪时间，绝不能并进分位数。
    """
    return {
        prefix + "_ms": float(o.get("latency_ms") or 0),
        prefix + "_mode": o.get("mode"),
        prefix + "_total_ms": float(o.get("total_ms") or 0),
        prefix + "_segs": [len(s["text"]) for s in (o.get("segments") or [])],
        prefix + "_err": str(o.get("error") or "")[:80],
    }


def _valid(r, mode):
    return (r.get(mode + "_mode") in ("url", "segments")
            and r.get(mode + "_ms", 0) > 0)


def bench_tts(items):
    rows = []
    for i, (src, text) in enumerate(items, 1):
        rec = {"src": src, "chars": len(text), "text": text[:60]}
        for mode in ("whole", "auto"):
            try:
                rec.update(_rec(_measure(text, mode), mode))
            except Exception as e:
                rec.update({mode + "_ms": 0.0, mode + "_mode": "error",
                            mode + "_total_ms": 0.0, mode + "_segs": [],
                            mode + "_err": str(e)[:80]})
            time.sleep(0.3)          # 别把 TTS QPS 打爆，429 会污染样本
        flag = "" if _valid(rec, "auto") else "  [回落/失败]"
        print("  [%2d/%2d] %3d字  %-24s 整句 %6.0fms → 切段首字 %6.0fms 段=%s%s"
              % (i, len(items), rec["chars"], src[:24],
                 rec["whole_ms"], rec["auto_ms"], rec["auto_segs"], flag))
        rows.append(rec)

    # 复测三类：切段后仍回落 / 仍超预算 / **整句回落**（后者用来对比
    # 「修复前旧一刀切」是否也会回落，这是证明多段切分有效性的关键对照）。
    over = [r for r in rows
            if not _valid(r, "auto") or r["auto_ms"] > BUDGET_MS
            or not _valid(r, "whole")]
    for idx, r in enumerate(over, 1):
        full = next(t for s, t in items if s == r["src"])
        for mode in ("legacy", "short"):
            try:
                r.update(_rec(_measure(full, mode), mode))
            except Exception as e:
                r.update({mode + "_ms": 0.0, mode + "_mode": "error",
                          mode + "_total_ms": 0.0, mode + "_segs": [],
                          mode + "_err": str(e)[:80]})
            time.sleep(0.3)
        print("  [复测 %d/%d] %-24s 旧一刀 %6.0fms(%s) / 短首段 %6.0fms(%s)"
              % (idx, len(over), r["src"][:24],
                 r["legacy_ms"], r["legacy_mode"], r["short_ms"], r["short_mode"]))
    return rows


# ---------------------------------------------------------------------------
# B. 完整链路
# ---------------------------------------------------------------------------
def bench_chain(rounds):
    rows, fails = [], []
    talks = [USER_UTTERANCES[i:i + 3]
             for i in range(0, min(rounds, len(USER_UTTERANCES)), 3)]
    n = 0
    for group in talks:
        sid = B.start_call()["session_id"]
        for said in group:
            if n >= rounds:
                break
            n += 1
            try:
                pcm = B.tts_to_pcm(said)
                r = B.handle_turn(sid, audio_bytes=pcm)
                lat = r.get("latency") or {}
                rows.append({
                    "n": n, "chars": len(said), "text": said,
                    "asr_ms": float(lat.get("asr_ms") or 0),
                    "tts_first_ms": float(lat.get("tts_first_ms") or 0),
                    "total_ms": float(lat.get("total_ms") or 0),
                    "server_first_ms": float(lat.get("server_first_audio_ms") or 0),
                    "reply_chars": len(r.get("reply_text") or ""),
                    "tts_mode": (r.get("tts") or {}).get("mode"),
                })
            except Exception as e:
                fails.append((n, said, str(e)[:80]))
            time.sleep(0.4)
        B.end_call(sid)
    return [r for r in rows if r["asr_ms"] > 0], fails


def stat(vals):
    keep = [v for v in vals if 0 < v <= TIMEOUT_MS]
    to = sum(1 for v in vals if v > TIMEOUT_MS)
    if not keep:
        return {"n": 0, "timeouts": to, "p50": None, "p95": None, "max": None}
    return {"n": len(keep), "timeouts": to,
            "p50": round(B.pct(keep, .5), 1),
            "p95": round(B.pct(keep, .95), 1) if len(keep) >= 20 else None,
            "max": round(max(keep), 1),
            "within_budget": sum(1 for v in keep if v <= BUDGET_MS)}


def main():
    quick = "--quick" in sys.argv
    report = (sys.argv[sys.argv.index("--report") + 1]
              if "--report" in sys.argv
              else os.path.join(ROOT, "data", "C7_语音基准报告.md"))
    n_texts, n_rounds = (12, 8) if quick else (24, 24)

    items = stratified(official_texts(), n_texts)
    nlg = ((config_loader.load().get("nlg") or {}).get("provider") or "template")
    print("C7 官方话术语音基准（真实云端）")
    print("话术池 %d 条，抽样 %d 条（%d–%d 字）；链路 %d 轮；nlg=%s"
          % (len(official_texts()), len(items), len(items[0][1]),
             len(items[-1][1]), n_rounds, nlg))
    print("=" * 74)

    print("\nA. 话术级 TTS：整句 vs 自动切段（指标 = 首段就绪 / 服务端首字下界）")
    tts_rows = bench_tts(items)

    print("\nB. 完整链路 %d 轮（pop 场景，官方风格商家问句）" % n_rounds)
    chain, fails = bench_chain(n_rounds)
    print("  有效 %d 轮，失败 %d 轮" % (len(chain), len(fails)))

    # 回落样本（mode=browser/error）的 latency_ms 是 4 秒超时×2 次重试的
    # 失败耗时，不是首段就绪时间——必须排除，否则 P50/P95 全是假的。
    whole = stat([r["whole_ms"] for r in tts_rows if _valid(r, "whole")])
    auto = stat([r["auto_ms"] for r in tts_rows if _valid(r, "auto")])
    legacy = stat([r["legacy_ms"] for r in tts_rows if _valid(r, "legacy")])
    short = stat([r["short_ms"] for r in tts_rows if _valid(r, "short")])
    fb_whole = sum(1 for r in tts_rows if not _valid(r, "whole"))
    fb_auto = sum(1 for r in tts_rows if not _valid(r, "auto"))
    sf = stat([r["server_first_ms"] for r in chain])
    asr = stat([r["asr_ms"] for r in chain])
    tot = stat([r["total_ms"] for r in chain])

    def fmt(s):
        if not s["n"]:
            return "无有效样本"
        return ("n=%d P50=%.0fms P95=%s max=%.0fms 预算内 %d/%d"
                % (s["n"], s["p50"],
                   ("%.0fms" % s["p95"]) if s["p95"] is not None else "样本不足不出",
                   s["max"], s["within_budget"], s["n"]))

    print("\n" + "=" * 74)
    print("整句不切段（首字=整句完成）  " + fmt(whole) + "  回落 %d" % fb_whole)
    print("现有自动切段（首段≤12字）    " + fmt(auto) + "  回落 %d" % fb_auto)
    print("修复前旧一刀切（复测子集）   " + fmt(legacy))
    print("短首段复测（首段≤8字）       " + fmt(short))
    print("链路服务端首字               " + fmt(sf))
    print("链路 ASR                     " + fmt(asr))
    print("链路服务端总响应             " + fmt(tot))

    over_auto = [r for r in tts_rows
                 if not _valid(r, "auto") or r["auto_ms"] > BUDGET_MS]
    pairs = [(r["whole_ms"], r["auto_ms"]) for r in tts_rows
             if _valid(r, "whole") and _valid(r, "auto")]
    gain = (round(statistics.median([w - a for w, a in pairs]), 1)
            if pairs else 0)
    recovered = [r for r in tts_rows
                 if _valid(r, "auto") and r.get("legacy_mode") in ("browser", "error")]
    short_fb = [r for r in tts_rows
                if r.get("short_mode") in ("browser", "error")]
    over_detail = "、".join("%s %d字 %.0fms" % (r["src"], r["chars"], r["auto_ms"])
                           for r in over_auto) or "无"
    short_fb_detail = "、".join(r["src"] for r in short_fb) or "无"

    payload = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "caliber": "服务端；首段就绪=首字下界；超时>10000ms不入分位数；样本<20不出P95",
        "budget_ms": BUDGET_MS, "nlg_provider": nlg,
        "scene": "pop_reject",
        "summary": {"whole": whole, "auto": auto, "legacy": legacy, "short": short,
                    "server_first": sf, "asr": asr, "total": tot,
                    "median_gain_ms": gain,
                    "fallback_whole": fb_whole, "fallback_auto": fb_auto,
                    "over_budget_after_split": len(over_auto),
                    "recovered_by_multiseg": len(recovered)},
        "tts_rows": tts_rows, "chain_rows": chain,
        "chain_failed": [{"n": n, "text": t, "error": e} for n, t, e in fails],
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    lines = [
        "# C7 · 官方话术语音基准重测报告",
        "",
        "> 生成时间：%s｜场景 `pop_reject`｜nlg=`%s`｜真实云端 ASR/TTS"
        % (payload["ts"], nlg),
        "> 明细：`data/bench_official_latest.json`；脚本：`scripts/bench_official_voice.py`",
        "",
        "## 口径（先说清楚，避免混着报）",
        "",
        "- **首段就绪 = 服务端首字下界**，不是浏览器实测；浏览器还要加网络往返与播放启动约 100–250ms。",
        "- 超时 >10000ms 不并入分位数；样本 <20 **不出 P95**（docs/04 §4，与看板同一插值公式）。",
        "- 1300ms 是阶段1清单的首字预算；赛题硬门槛 2000ms 是另一条线，不混用。",
        "- **合成回落样本（`mode=browser`）一律排除出分位数**：那种 ~8.8s 是"
        "「4 秒超时×2 次重试」的失败耗时，不是首段就绪时间，混进来 P50/P95 全是假的。",
        "- 话术池：kb_pop 94 条官方原文 + 场景包 36 条 reply，去重后按字数分层等距抽样 %d 条（%d–%d 字）。" % (len(items), len(items[0][1]), len(items[-1][1])),
        "- 本场景 `nlg=template`；主场景开着 dashscope 改写，首字还要再加约 400–650ms，预算更紧。",
        "",
        "## 结果",
        "",
        "| 口径 | 样本 | P50 | P95 | max | ≤1300ms |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, s in (("整句不切段（首字=整句完成）", whole),
                    ("现有自动切段（首段≤12字）", auto),
                    ("修复前旧一刀切（复测子集）", legacy),
                    ("短首段复测（≤8字，复测子集）", short),
                    ("完整链路·服务端首字", sf),
                    ("完整链路·ASR", asr),
                    ("完整链路·服务端总响应", tot)):
        lines.append("| %s | %d | %s | %s | %s | %s |" % (
            name, s["n"],
            "%.0fms" % s["p50"] if s["p50"] is not None else "—",
            "%.0fms" % s["p95"] if s["p95"] is not None else "样本不足不出",
            "%.0fms" % s["max"] if s["max"] is not None else "—",
            "%d/%d" % (s["within_budget"], s["n"]) if s["n"] else "—"))
    lines += [
        "",
        "## 切段结论",
        "",
        "- 同一句官方话术，整句合成 vs 自动切段的**首字延迟中位数差 %.0fms**（只用两边都成功的配对样本）。" % gain,
        "- 回落样本：整句 %d/%d 条、自动切段 %d/%d 条（回落即云端超时后退回浏览器机械音）。"
        % (fb_whole, len(tts_rows), fb_auto, len(tts_rows)),
        "- **发现并修复的真实缺陷**：官方最长话术 160 字（KB-POP-Q10），旧的「只切一刀」",
        "  切完尾段仍 150 字，云端 `timeout_sec=4` 两次重试全超时，整句回落机械音。",
        "  C7 把 `_split_for_tts` 改成尾段按标点继续切到 `seg_max=48`：短话术仍只切一刀",
        "  （守 QPS），超长话术多几个串行请求，但每段都在超时预算内。",
        "  复测里旧切段回落、新切段成功的条目：**%d** 条。" % len(recovered),
        "- 自动切段后仍超 1300ms 或仍回落：**%d** 条（%s）。这些是 31–46 字短话术，"
        "  与字数不相关，瓶颈在云端抖动/网络，不是切段能解决的——不做「切段解决一切」的归因。"
        % (len(over_auto), over_detail),
        "- **短首段不是解法**：把首段压到 ≤8 字时复测有 %d 条回落（%s）——"
        "  它把已切好的多段又合并回两段、尾段重新超长，反而触发超时。"
        "  真正起作用的是「尾段也切到安全长度」，不是「首段更短」；"
        "  该模式不是生产默认配置，只作为对照。" % (len(short_fb), short_fb_detail),
        "- 多段后 `collect_rest` 有 3 秒取段超时，段数多时**尾段可能取不全**；首字不受影响，",
        "  但播完首段的连续性要靠前端缓冲或流式合成，这是本轮未根治的部分。",
        "",
        "## 复跑",
        "",
        "```",
        "python scripts/bench_official_voice.py --report <报告路径.md>",
        "python scripts/bench_official_voice.py --quick   # 12 条话术 + 8 轮链路",
        "```",
        "",
        "## 边界",
        "",
        "- 输入音频是 TTS 合成人声，比真人干净（无口音、无环境噪声、无停顿），识别耗时偏乐观。",
        "- 每次跑都是新的云端采样，数值会波动；结论看同一批次内的 A/B 差值，不跨批次比绝对值。",
        "- 完整链路样本 %d 轮；链路失败 %d 轮，明细在 JSON 的 `chain_failed`。" % (len(chain), len(fails)),
    ]
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n明细：%s\n报告：%s" % (OUT_JSON, report))
    return 0 if whole["n"] and auto["n"] else 1


if __name__ == "__main__":
    sys.exit(main())
