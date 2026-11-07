"""打断/端点参数标定 —— 把 web/index.html 的 VAD 逻辑在 Python 里原样跑一遍。

为什么要有这个东西：
    docs/16 §4.2 那张参数表是**理论值**（"正常说话在 -30~-20dBFS 留足余量"），
    但演示现场是笔记本内置麦 + 外放音箱，回声残留、键盘、椅子声都会进来。
    "能量阈值对不对"这种事只有拿音频跑一遍才知道，靠看数字猜不算。

关键设计：**参数不写在脚本里，从 web/index.html 里读。**
    前端改了阈值、脚本没跟上，标定结果就会与现场行为脱节——
    这是最容易出的"文档/代码两张皮"。所以这里用正则把 `const VAD = {...}`
    解析出来，解析不到就直接报错（宁可不跑，也不要跑一套假参数）。

仿真什么：
    逐 20ms 帧流过前端同一套状态机（listening → user → thinking；
    agent → suspect → probing），判据也用前端同一套（rms + 过零率 + 谱倾斜）。

    唯一近似：前端 probing 阶段是真的调 /api/asr/probe 看"有没有实义文字"，
    这里用服务端 VAD（modules/vad_gate）代替——语义上都是"这段有没有人声"，
    但**不等于**"有没有实义内容"（"嗯"会被 ASR 判为无内容，VAD 判为人声）。
    所以本脚本给出的"误打断率"是**上界**（偏保守），报告里按上界写。

场景怎么来：
    人声/噪声都取 data/vad_corpus（scripts/gen_vad_corpus.py 生成）；
    回声按 AEC 残留 -26dB 合成（getUserMedia 的 echoCancellation 实测能压 20~30dB）。
    这些都是合成信号，**不能代替真实现场试麦**——脚本跑完仍要在演示机上实测一次。

怎么用：
    python scripts/tune_barge_vad.py                 # 用当前前端参数评估
    python scripts/tune_barge_vad.py --sweep         # 网格扫描，输出建议参数
    python scripts/tune_barge_vad.py --json          # 机读（verify 用）
"""

import argparse
import itertools
import json
import math
import os
import re
import struct
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modules import vad_gate  # noqa: E402

CORPUS = os.path.join(ROOT, "data", "vad_corpus")
INDEX = os.path.join(ROOT, "web", "index.html")
FRAME_MS = 20
FRAME_SAMPLES = 320


# ---------------------------------------------------------------------------
# 前端参数：从 index.html 里读，不在脚本里另存一份
# ---------------------------------------------------------------------------

def load_front_params(path=INDEX):
    with open(path, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"const VAD = \{(.*?)\n\};", html, re.S)
    if not m:
        raise RuntimeError("web/index.html 里找不到 `const VAD = {...}`，"
                           "拒绝用脚本里的默认值跑——那会标定出一套现场不生效的参数")
    body = m.group(1)
    out = {}
    for key, val in re.findall(r"(\w+):\s*([0-9.]+)", body):
        out[key] = float(val) if "." in val else int(val)
    need = ("speechRms", "silenceRms", "endpointFrames", "bargeRms",
            "bargeFrames", "probeSilenceFrames", "bargeMaxFrames")
    missing = [k for k in need if k not in out]
    if missing:
        raise RuntimeError("const VAD 缺字段：%s" % ", ".join(missing))
    return out


# ---------------------------------------------------------------------------
# 与前端等价的逐帧判据
# ---------------------------------------------------------------------------
# 这三个判据要和 web/index.html 的 analyzeFrame() 一字不差，否则标定无意义。
# 改动顺序固定：先改前端 → 再改这里 → 跑本脚本出数 → 更新 docs/16。

def frame_rms(pcm):
    s = 0.0
    for v in pcm:
        f = v / 32768.0
        s += f * f
    return math.sqrt(s / len(pcm)) if pcm else 0.0


def frame_zcr(pcm):
    if len(pcm) < 2:
        return 0.0
    c = 0
    prev = pcm[0]
    for v in pcm[1:]:
        if (v >= 0) != (prev >= 0):
            c += 1
        prev = v
    return c / float(len(pcm) - 1)


def goertzel(pcm, freq, sr=16000):
    coeff = 2.0 * math.cos(2.0 * math.pi * freq / sr)
    s0 = s1 = s2 = 0.0
    for v in pcm:
        s0 = v / 32768.0 + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return max(0.0, s1 * s1 + s2 * s2 - coeff * s1 * s2)


LOW_BAND = (300, 500, 700)
HIGH_BAND = (1800, 2400, 3000)


def band_sum(pcm, freqs):
    return sum(goertzel(pcm, f) for f in freqs) / float(len(freqs))


def analyze_frame(pcm, p):
    """前端 analyzeFrame() 的等价实现：返回 (voiced, rms, zcr, tilt)。

    tilt 用**频段均值**而不是单点：之前用 goertzel(400)/goertzel(2000) 两个单点，
    20ms 窗分辨率只有 50Hz，等价于问"400Hz 这个精确频率上有没有能量"。
    女声基频在 200Hz 附近、400Hz 恰是谷，tilt 掉到 0.1 以下，真人被判成键盘。
    前端同步改成了三频点均值，这里必须一致，否则标定的和跑的不是一套判据。
    """
    rms = frame_rms(pcm)
    zcr = frame_zcr(pcm)
    tilt = band_sum(pcm, LOW_BAND) / (band_sum(pcm, HIGH_BAND) + 1e-12)
    # 位移门（onset）：起音那 20ms 常常只有半帧能量，光看 rms 会漏掉"停"这种单字。
    voiced = (rms >= p["speechRms"] and zcr <= p.get("zcrMax", 1.0)
              and tilt >= p.get("tiltMin", 0.0))
    return voiced, rms, zcr, tilt


# ---------------------------------------------------------------------------
# 语料拼接
# ---------------------------------------------------------------------------

def read_pcm(name):
    path = os.path.join(CORPUS, name) if os.sep not in name else name
    with wave.open(path) as w:
        return list(struct.unpack("<%dh" % (w.getnframes()),
                                  w.readframes(w.getnframes())))


def pick(kind, idx=0):
    d = os.path.join(CORPUS, kind)
    files = sorted(f for f in os.listdir(d) if f.endswith(".wav"))
    return read_pcm(os.path.join(d, files[idx % len(files)]))


def silence(ms):
    return [0] * int(16000 * ms / 1000)


def overlay(base, layer, at_ms, gain=1.0):
    """把 layer 叠到 base 的 at_ms 处（超出的部分截断）。"""
    out = list(base)
    start = int(16000 * at_ms / 1000)
    for i, v in enumerate(layer):
        j = start + i
        if j >= len(out):
            break
        out[j] = max(-32768, min(32767, out[j] + int(v * gain)))
    return out


# ---------------------------------------------------------------------------
# 状态机仿真（与前端 vadTick / agentTick 一致）
# ---------------------------------------------------------------------------

class Sim:
    def __init__(self, p):
        self.p = p
        self.phase = "listening"
        self.silent = 0
        self.onset = 0             # 开口确认计数（与前端 S.onsetHits 对应）
        self.barge_hits = 0
        self.barge_state = "idle"
        self.barge_buf = []
        self.events = []          # (时刻ms, 事件名)

    def tick(self, pcm, t_ms):
        voiced, rms, zcr, tilt = analyze_frame(pcm, self.p)
        p = self.p

        if self.phase == "agent":
            self._agent_tick(rms, pcm, t_ms)
            return
        if self.phase == "thinking":
            return
        if self.phase == "listening":
            # 与前端一致：连续 onsetFrames 帧"像人声"才认开口。
            # 键盘单帧很响但谱不对，这一条是专门挡它的。
            self.onset = self.onset + 1 if voiced else 0
            if self.onset >= p.get("onsetFrames", 1):
                self.phase = "user"
                self.silent = 0
                self.onset = 0
                self.events.append((t_ms, "user_start"))
            return
        # user：等端点。用**能量**判"还在说"，不能用 voiced——
        # voiced 含过零率与谱倾斜两个门，说到一半侧头/换字音就被当静音，
        # 700ms 一到把一句话切成两半。这句之前写成 `if voiced:`，是真 bug。
        if rms >= p.get("silenceRms", 0.010):
            self.silent = 0
        else:
            self.silent += 1
        if self.silent >= p["endpointFrames"]:
            self.phase = "thinking"
            self.events.append((t_ms, "endpoint"))

    def _agent_tick(self, rms, pcm, t_ms):
        p = self.p
        if self.barge_state == "probing":
            return
        if self.barge_state == "suspect":
            self.barge_buf.append(pcm)
            self.barge_hits += 1
            if rms >= p["speechRms"]:
                self.silent = 0
            else:
                self.silent += 1
            if (self.silent >= p["probeSilenceFrames"]
                    or self.barge_hits >= p["bargeMaxFrames"]):
                self.barge_state = "probing"
                self.events.append((t_ms, "probe"))
            return
        self.barge_hits = self.barge_hits + 1 if rms >= p["bargeRms"] else 0
        if self.barge_hits >= p["bargeFrames"]:
            self.barge_state = "suspect"
            self.barge_buf = []
            self.silent = 0
            self.barge_hits = 0
            self.events.append((t_ms, "suspect"))


def resolve_probe(sim, pcm_list):
    """仿真的 probe 阶段：用服务端 VAD 判"这段有没有人声"。

    前端的真实判据是 ASR 出来有没有实义文字（app._has_interrupt_content）。
    这里用 VAD 代替，得到的是**偏保守**的误打断率上界：
    "嗯"这种人声会被 VAD 判成有内容，真实链路会否决它。
    """
    merged = b"".join(struct.pack("<%dh" % len(f), *f) for f in pcm_list if f)
    if not merged:
        return False
    return bool(vad_gate.analyze(merged)["started"])


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

def build_scenarios():
    """返回 [(名字, 音频, 真值)]。

    真值只用于**打分**，不参与状态机——否则就是把答案喂给被测系统。
    """
    agent = pick("speech", 0)          # 客服播报（长句）
    user = pick("speech", 7)           # 用户短句"等一下，我想问个问题"
    scenes = []

    # 关键：这几个场景模拟的是**麦克风收到的信号**，不是喇叭放出来的信号。
    # 外放时麦克风收到的是 AEC 之后的回声（残压 20~30dB），原声不会进来。
    # 第一版把 agent 原声直接当麦克风信号，结果"外放回声"场景里 rms 高达 0.15，
    # 任何阈值都会被顶穿——测出来的是构造错误，不是参数问题。
    echo = [int(v * 0.05) for v in agent]      # -26dB 残压，AEC 实测区间中位

    # A 正常插话：播报 1.5s 后用户开口 → 必须判成打断
    a = overlay(silence(1500) + echo, user, 1500)
    scenes.append(("A_正常插话", a, {"barge": True, "at_ms": 1500}, "agent"))

    # B 外放回声：只有自己的回声，无人说话 → 不许打断自己
    b = silence(300) + echo
    scenes.append(("B_外放回声", b, {"barge": False}, "agent"))

    # C 键盘：播报期间敲键盘 → 不许打断
    kb = pick("noise", 2)
    c = overlay(silence(300) + echo, kb, 800)
    scenes.append(("C_键盘", c, {"barge": False}, "agent"))

    # D 清嗓：播报期间咳一声 → 按"不可逆的打断"原则，宁可多播半句也不许停
    cough = pick("noise", 6)
    d = overlay(silence(300) + echo, cough, 800)
    scenes.append(("D_清嗓", d, {"barge": False}, "agent"))

    # E 句中停顿：用户说两句、中间停 400ms → 只能切一轮，不能切成两轮
    e = (pick("speech", 8) + silence(400) + pick("speech", 9)
         + silence(900))
    scenes.append(("E_句中停顿", e, {"endpoints": 1}, "listening"))

    # F 连续长说：一直说不带长停顿 → 端点延迟不能无限拖
    f = pick("speech", 1) + silence(900)
    scenes.append(("F_连续长说", f, {"endpoints": 1}, "listening"))

    # G 全静音：没有任何输入 → 不许提交
    scenes.append(("G_静音", silence(3000), {"endpoints": 0}, "listening"))

    # H 说话中带键盘：真人说话 + 键盘声，仍应正常切一轮
    h = overlay(pick("speech", 10), kb, 600, gain=0.5) + silence(900)
    scenes.append(("H_说话含敲击", h, {"endpoints": 1}, "listening"))

    # 以下四条是"恶化场景"，专门用来把参数边界逼出来。
    # 只有常规场景时，192 组参数全过（实测），说明场景没有区分度——
    # 那样的标定等于没做。

    # I 弱 AEC：老笔记本 / 便宜音箱，回声只压到 -18dB
    echo2 = [int(v * 0.12) for v in agent]
    scenes.append(("I_弱AEC回声", silence(300) + echo2, {"barge": False}, "agent"))

    # J 小声说话：用户离麦远、音量只有正常 35% → 端点仍要能切出来
    quiet = [int(v * 0.35) for v in pick("speech", 3)] + silence(900)
    scenes.append(("J_小声说话", quiet, {"endpoints": 1}, "listening"))

    # K 回声 + 响亮键盘：两个都掺在一起，最容易误打断
    scenes.append(("K_回声加键盘", overlay(silence(300) + echo2, kb, 700, gain=2.0),
                   {"barge": False}, "agent"))

    # L 弱 AEC 下正常插话：既要压住回声，又不能把真人挡掉
    scenes.append(("L_弱AEC插话", overlay(silence(1500) + echo2,
                                          [int(v * 0.6) for v in user], 1500),
                   {"barge": True, "at_ms": 1500}, "agent"))

    # 以下三条是"边界探针"：参数在这个区间里才分得出高下。
    # 只有常规场景时 384 组参数全过（实测），说明场景没有区分度——
    # 那只能证明"参数不敏感"，不能证明"参数选得对"。这三条把边界逼出来。

    # M 极小声：只有正常音量 15%（rms 约 0.02），贴着 speechRms 走
    scenes.append(("M_极小声", [int(v * 0.15) for v in pick("speech", 5)] + silence(900),
                   {"endpoints": 1}, "listening"))

    # N 强回声：AEC 几乎失效（-12dB），常见于外放音量拉满 + 廉价麦
    echo3 = [int(v * 0.25) for v in agent]
    scenes.append(("N_强回声", silence(300) + echo3, {"barge": False}, "agent"))

    # O 爆响键盘：机械键盘贴着麦敲
    scenes.append(("O_爆响键盘", overlay(silence(300) + echo3, kb, 700, gain=4.0),
                   {"barge": False}, "agent"))
    return scenes


def run_scene(name, pcm, p, init_phase="listening"):
    sim = Sim(p)
    sim.phase = init_phase
    n = len(pcm) // FRAME_SAMPLES
    for i in range(n):
        seg = pcm[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES]
        sim.tick(seg, (i + 1) * FRAME_MS)
        # probe 到点就给结论（真实链路要约 300~400ms，这里直接判）
        if sim.barge_state == "probing":
            ok = resolve_probe(sim, sim.barge_buf)
            sim.events.append(((i + 1) * FRAME_MS,
                               "barge_confirm" if ok else "barge_dismiss"))
            if ok:
                sim.phase = "user"
                sim.barge_state = "idle"
            else:
                sim.barge_state = "idle"
                sim.barge_hits = 0
                sim.barge_buf = []
    return sim.events


def score(scenes, p):
    rows = []
    for name, pcm, truth, init in scenes:
        ev = run_scene(name, pcm, p, init)
        kinds = [e[1] for e in ev]
        row = {"scene": name, "events": [(t, k) for t, k in ev]}
        if "barge" in truth:
            got = "barge_confirm" in kinds
            row["ok"] = (got == truth["barge"])
            row["detail"] = ("判打断" if got else "未打断") + \
                            ("（应为%s）" % ("打断" if truth["barge"] else "不打断"))
            if truth["barge"] and "barge_confirm" in kinds:
                t0 = truth["at_ms"]
                t1 = [t for t, k in ev if k == "barge_confirm"][0]
                row["barge_delay_ms"] = t1 - t0
        else:
            n_ep = kinds.count("endpoint")
            row["ok"] = (n_ep == truth["endpoints"])
            row["detail"] = "端点 %d 次（应 %d 次）" % (n_ep, truth["endpoints"])
            if "endpoints" in truth and truth["endpoints"] > 0 and n_ep:
                row["endpoint_delay_ms"] = [t for t, k in ev if k == "endpoint"][0]
        rows.append(row)
    return rows


def report(rows, p, title="当前参数"):
    print("=" * 78)
    print("%s：speechRms=%.3f bargeRms=%.3f endpoint=%d帧(%dms) zcrMax=%s tiltMin=%s"
          % (title, p["speechRms"], p["bargeRms"], p["endpointFrames"],
             p["endpointFrames"] * FRAME_MS, p.get("zcrMax", "关"),
             p.get("tiltMin", "关")))
    print("=" * 78)
    bad = 0
    for r in rows:
        flag = "OK  " if r["ok"] else "FAIL"
        if not r["ok"]:
            bad += 1
        extra = ""
        if "barge_delay_ms" in r:
            extra = "  打断延迟 %dms" % r["barge_delay_ms"]
        elif "endpoint_delay_ms" in r:
            extra = "  端点 %dms" % r["endpoint_delay_ms"]
        print("  [%s] %-12s %s%s" % (flag, r["scene"], r["detail"], extra))
    print("  → %d/%d 场景符合预期" % (len(rows) - bad, len(rows)))
    return bad


def sweep(scenes, base):
    """网格扫描：先保"不许误打断"，再在满足者里挑端点最快的。"""
    grid = {
        "speechRms": [0.012, 0.016, 0.020, 0.024],
        "bargeRms": [0.025, 0.035, 0.045],
        "endpointFrames": [25, 30, 35, 40],
        "zcrMax": [1.0, 0.45],
        "tiltMin": [0.0, 0.35],
        "onsetFrames": [1, 2],
    }
    print("\n" + "=" * 78)
    total_combos = 1
    for v in grid.values():
        total_combos *= len(v)
    print("网格扫描（%d 组）——先卡死误打断，再比端点延迟" % total_combos)
    print("=" * 78)
    best = []
    total = 0
    keys = ("speechRms", "bargeRms", "endpointFrames", "zcrMax", "tiltMin",
            "onsetFrames")
    for combo in itertools.product(*(grid[k] for k in keys)):
        p = dict(base)
        for k, v in zip(keys, combo):
            p[k] = v
        total += 1
        rows = score(scenes, p)
        missed = [r["scene"] for r in rows if not r["ok"]]
        ep = [r.get("endpoint_delay_ms") for r in rows if r.get("endpoint_delay_ms")]
        bd = [r.get("barge_delay_ms") for r in rows if r.get("barge_delay_ms")]
        best.append({
            "p": {k: p[k] for k in keys},
            "missed": missed,
            "endpoint_max_ms": max(ep) if ep else None,
            "barge_max_ms": max(bd) if bd else None,
        })
    ok = [b for b in best if not b["missed"]]
    ok.sort(key=lambda b: ((b["endpoint_max_ms"] or 99999), -(b["p"]["speechRms"])))
    print("全场景通过的组合 %d/%d 组。" % (len(ok), total))

    # 每个参数取值单独看通过率：这比"列前八名"有用——
    # 前八名会集中在同一角落，看不出哪个参数才是敏感的那个。
    print("\n各取值的通过率（能看出哪一维真正敏感）：")
    for key in ("speechRms", "bargeRms", "endpointFrames", "zcrMax", "tiltMin",
                "onsetFrames"):
        buckets = {}
        for b in best:
            v = b["p"][key]
            t = buckets.setdefault(v, [0, 0])
            t[1] += 1
            if not b["missed"]:
                t[0] += 1
        line = "  %-14s " % key + "  ".join(
            "%s→%d/%d" % (v, c[0], c[1]) for v, c in sorted(buckets.items()))
        print(line)

    print("\n前 6 名（端点延迟升序）：")
    for b in ok[:6]:
        print("  %s → 端点max %sms 打断max %sms"
              % (json.dumps(b["p"], sort_keys=True), b["endpoint_max_ms"],
                 b["barge_max_ms"]))
    if not ok:
        print("  没有一组能通过全部场景——说明场景之间冲突，要先放宽场景，"
              "而不是硬调参数。")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(CORPUS):
        print("语料缺失：先跑 python scripts/gen_vad_corpus.py")
        return 1
    p = load_front_params()
    # 前端目前没有这两个字段时，按"关"处理（等价于改造前的纯能量判定）
    p.setdefault("zcrMax", 1.0)
    p.setdefault("tiltMin", 0.0)
    p.setdefault("onsetFrames", 1)

    scenes = build_scenarios()
    rows = score(scenes, p)
    if args.json:
        print(json.dumps({"params": p, "rows": rows,
                          "pass": sum(1 for r in rows if r["ok"]),
                          "total": len(rows)}, ensure_ascii=False))
        return 0
    report(rows, p)
    if args.sweep:
        sweep(scenes, p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
