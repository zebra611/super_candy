"""VAD 门面 —— 两层引擎，一套聚合规则，一个可解释的结论。

为什么需要这个文件（而不是直接在 voice.py 里塞 if）：
    Z6 的要求是"噪声片段不再触发 vad_result"。这句话有两个陷阱：
      1. 换引擎不难，难的是**证明换完之后噪声真的不触发了**；
      2. 更难的是一旦证明不了（模型装不上），还能不能给出可信的结论。
    所以这里把"逐帧判定"和"起止聚合"拆开：
      - 逐帧判定由引擎负责（silero 给概率 / spectral 给布尔），
      - 起止聚合**两条引擎共用同一套参数**。
    共用聚合是刻意的：否则比较两个引擎时，差异会混进参数差异里，
    报告上写的"Silero 更好"就无法归因。

引擎选择（自动，不需要调用方关心）：
    silero   装了 onnxruntime 且 data/models/silero_vad.onnx 在 → 模型判每一窗（32ms）
    spectral 否则 → 纯标准库谱特征门（零依赖，干净机器与 CI 都能跑）
    两者都输出"逐帧是否人声"，事件里用 `engine` 字段如实标注，
    降级原因写 `engine_note`——**不允许**在降级时假装自己是 Silero。

spectral 引擎到底看什么（不是能量阈值换皮）：
    语音与常见噪声的差别不在响度，而在三件事：
      ① 谐波结构：300~3000Hz 里有 5 个以上明显频点（元音共振峰），
         风扇只有 60/120/240 几个窄峰，键盘是宽带但只响 5~12ms；
      ② 周期性：浊音段自相关峰 > 0.4，摩擦声/白噪 < 0.2；
      ③ 过零率：语音多在 0.1~0.3，白噪与纸张摩擦 > 0.4。
    三条同时满足才判"语音帧"，再要求**连续 ≥96ms**才判"开口"——
    这一条专门用来挡键盘、椅子、关门这类瞬态：它们谱上像语音，
    但撑不过 96ms。
    已知挡不住的：远处人声、真人咳嗽（与语音同分布），
    这两类只能靠 ASR 语义层兜（C6 已做），谱特征层做不到就说做不到。

代价（实测，scripts/bench_vad.py 出数）：
    spectral  4 秒音频约 0.2~0.4 秒 CPU（纯 Python，无 numpy）
    silero    4 秒音频约 40ms，召回/误触都更好

负责人：角色 1（实时语音与终端）
"""

import math
import struct

import contracts

FRAME_MS = 20                    # spectral 的帧长
FRAME_SAMPLES = int(contracts.PCM_SAMPLE_RATE * FRAME_MS / 1000)   # 320

# 起止聚合参数（两条引擎共用）。取值理由都在 docs/16 §4.2 的同一张表里：
# 开口 96ms = 3 个 Silero 窗 / 5 个 20ms 帧，比键盘单次瞬态（5~12ms）长一个量级；
# 说完 300ms 比前端 700ms 短：服务端这一段音频已经由前端切好，
# 这里的"说完"只用于填 ended 字段，不影响轮次节奏。
MIN_SPEECH_MS = 96
HANGOVER_MS = 180
END_SILENCE_MS = 300

# spectral 判据。这些值在 data/vad_corpus 上标定，改动要重跑 bench_vad 并更新报告。
_ABS_FLOOR = 0.0035             # 绝对静音门（约 -49dBFS），低于它的帧连特征都不算
_FLOOR_RATIO = 2.6              # 相对本段噪声底（20 分位）的倍数
_ZCR_MAX = 0.30                 # 语音帧的过零率上限（白噪/摩擦 0.4+）
_PERIOD_MIN = 0.40              # 浊音自相关下限
_SPREAD_MIN = 5                 # 300~3000Hz 内 -30dB 以上的频点数
_TILT_MIN = 0.6                 # 低频(300~700) / 高频(1600~3000) 能量比；语音 >1，键盘 <0.2
_DECAY_RATIO = 0.78             # 连续几帧都掉到这个比例以下 = 冲击衰减，不是说话
_DECAY_RUN = 3
_PROBE_HZ = (300, 500, 700, 900, 1200, 1600, 2200, 3000)


def analyze(pcm_bytes, sample_rate=contracts.PCM_SAMPLE_RATE, prefer="auto"):
    """判一段 PCM 里"有没有人说完了话"。

    返回字典（字段只增不减，老字段语义保持）：
        started     这段里有没有成句的人声（≥96ms 连续）
        ended       最后一段人声之后是否已经静下来（≥300ms 或直到音频结束）
        speech_ms   真正是人声的累计时长（不再是"整段长度"）
        samples     采样数（按契约位宽算，与 audio_stream 对得上）
        sample_rate 采样率
        engine      silero / spectral —— 结论由谁给出
        engine_note 降级原因（用 silero 时为空）
        speech_ratio speech_ms / 总时长
        frames      参与判定的帧数
    """
    pcm_bytes = pcm_bytes or b""
    samples = len(pcm_bytes) // contracts.PCM_SAMPLE_WIDTH if pcm_bytes else 0
    total_ms = int(samples * 1000 / sample_rate) if sample_rate else 0

    base = {
        "started": False,
        "ended": False,
        "speech_ms": 0,
        "samples": samples,
        "sample_rate": sample_rate,
        "engine": "none",
        "engine_note": "",
        "speech_ratio": 0.0,
        "frames": 0,
    }
    if not pcm_bytes:
        base["engine_note"] = "空音频"
        return base

    frame_flags = None
    engine = ""
    note = ""
    if prefer in ("auto", "silero"):
        from modules import silero_vad
        inst = silero_vad.load() if silero_vad.available() else None
        if inst is not None:
            try:
                probs = inst.probs(pcm_bytes, sample_rate=sample_rate)
            except Exception as exc:
                # 推理失败不能让这一轮没有 VAD 结论：降级并留原因。
                note = "silero 推理失败，降级谱特征：%s" % str(exc)[:120]
            else:
                engine = "silero"
                frame_flags = [(ms, p >= 0.5) for ms, p in
                               _windows(probs, 32)]
        else:
            from modules import silero_vad as sv
            note = "降级谱特征：%s" % (sv.reason_unavailable()
                                       or "Silero 不可用但未给出原因")
    if frame_flags is None:
        engine = "spectral"
        if prefer == "silero" and not note:
            note = "指定 silero 但不可用，降级谱特征"
        frame_flags = _spectral_frames(pcm_bytes, sample_rate)

    agg = _aggregate(frame_flags, total_ms)
    base.update(agg)
    base["engine"] = engine
    base["engine_note"] = note
    base["speech_ratio"] = round(agg["speech_ms"] / total_ms, 3) if total_ms else 0.0
    base["frames"] = len(frame_flags)
    return base


def _windows(probs, win_ms):
    """把 Silero 的逐窗概率摊成 (帧结束时刻ms, 是否人声)。"""
    return [((i + 1) * win_ms, bool(p >= 0.5)) for i, p in enumerate(probs)]


def _aggregate(frame_flags, total_ms):
    """把逐帧布尔聚合成 started / ended / speech_ms。

    hangover 的作用：语音里"京东"两个音节之间本来就有 80~150ms 的闭塞，
    不合并会把一句话切成一堆碎片，speech_ms 少算一半。
    """
    if not frame_flags:
        return {"started": False, "ended": False, "speech_ms": 0}

    step_ms = (frame_flags[1][0] - frame_flags[0][0]) if len(frame_flags) > 1 else FRAME_MS
    max_gap = max(1, int(HANGOVER_MS / max(1, step_ms)))

    segments = []
    cur_start = None
    last_true = None
    # 最长"连续"语音帧数：判"开口"只认它，不认 hangover 拼起来的段。
    # 为什么必须这样（实测踩出来的）：键盘每秒 6~9 次瞬态，单次只有 5~12ms，
    # 靠 hangover 能把 4 秒里的几十次敲击连成一段 1180ms 的"人声"。
    # 而真人说"京东"时，元音段本身连续 150ms 以上，不靠拼接也能过。
    best_run = 0
    run = 0
    for idx, (_, is_speech) in enumerate(frame_flags):
        if is_speech:
            run += 1
            best_run = max(best_run, run)
            if cur_start is None:
                cur_start = idx
            last_true = idx
        else:
            run = 0
            if cur_start is not None and last_true is not None and idx - last_true > max_gap:
                segments.append((cur_start, last_true))
                cur_start = None
                last_true = None
    if cur_start is not None and last_true is not None:
        segments.append((cur_start, last_true))

    speech_ms = sum(int((b - a + 1) * step_ms) for a, b in segments)
    started = (best_run * step_ms) >= MIN_SPEECH_MS
    if not segments:
        return {"started": False, "ended": True, "speech_ms": 0}

    tail_ms = total_ms - int((segments[-1][1] + 1) * step_ms)
    ended = tail_ms >= END_SILENCE_MS
    return {"started": bool(started), "ended": bool(ended), "speech_ms": int(speech_ms)}


# ---------------------------------------------------------------------------
# spectral 引擎：纯标准库
# ---------------------------------------------------------------------------

def _to_ints(pcm_bytes):
    n = len(pcm_bytes) // 2
    return struct.unpack("<%dh" % n, pcm_bytes[:n * 2])


def _rms(frame):
    if not frame:
        return 0.0
    s = 0.0
    for v in frame:
        f = v / 32768.0
        s += f * f
    return math.sqrt(s / len(frame))


def _zcr(frame):
    if len(frame) < 2:
        return 0.0
    c = 0
    prev = frame[0]
    for v in frame[1:]:
        if (v >= 0) != (prev >= 0):
            c += 1
        prev = v
    return c / float(len(frame) - 1)


def _goertzel_energy(frame, freq, sr):
    """单频点能量（Goertzel）。比 FFT 便宜：只要 8 个频点，不需要整谱。"""
    coeff = 2.0 * math.cos(2.0 * math.pi * freq / sr)
    s0 = s1 = s2 = 0.0
    for v in frame:
        s0 = v / 32768.0 + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def _spread(frame, sr):
    """300~3000Hz 内有几个频点高于"最响频点 -30dB"。

    语音的元音段在这个范围里有 5 个以上共振峰/谐波；
    风扇只有 120/240 几个窄峰；单音提示音只有 1 个。
    """
    energies = [_goertzel_energy(frame, f, sr) for f in _PROBE_HZ]
    peak = max(energies) or 1e-12
    thr = peak * 10 ** (-30 / 10.0)
    return sum(1 for e in energies if e >= thr)


def _periodicity(frame, sr):
    """归一化自相关峰（lag 40~160 ≈ 100~400Hz 基频）。

    为省时间把帧按 4 抽 1 再算：基频周期远大于 4 个采样，
    降采样只是把 lag 同比缩小，峰的位置与高度基本不变。
    """
    dec = 4
    x = frame[::dec]
    n = len(x)
    if n < 40:
        return 0.0
    mean = sum(x) / n
    x = [v - mean for v in x]
    e0 = sum(v * v for v in x)
    if e0 <= 1e-9:
        return 0.0
    lo, hi = int(40 / dec), int(160 / dec)
    best = 0.0
    for lag in range(max(1, lo), min(hi, n - 1) + 1):
        s = 0.0
        e1 = 0.0
        for i in range(n - lag):
            s += x[i] * x[i + lag]
            e1 += x[i + lag] * x[i + lag]
        if e1 <= 1e-9:
            continue
        r = s / math.sqrt(e0 * e1)
        if r > best:
            best = r
    return best


def _spectral_frames(pcm_bytes, sr):
    """逐 20ms 帧给出 (帧结束时刻ms, 是否语音)。"""
    ints = _to_ints(pcm_bytes)
    n_frames = (len(ints) + FRAME_SAMPLES - 1) // FRAME_SAMPLES
    if not n_frames:
        return []

    frames = []
    for i in range(n_frames):
        seg = ints[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES]
        if len(seg) < FRAME_SAMPLES:
            seg = list(seg) + [0] * (FRAME_SAMPLES - len(seg))
        frames.append((_rms(seg), _zcr(seg)))

    levels = sorted(r for r, _ in frames)
    floor = levels[int(len(levels) * 0.2)] if levels else 0.0
    thr = max(_ABS_FLOOR, floor * _FLOOR_RATIO)

    # 冲击衰减过滤：门被关上时，低频冲击的能量是"一上来最高、之后一路掉"。
    # 语音不会这样——音节之间有起伏，掉两帧就会回升或持平。
    # 只靠"连续 ≥96ms"挡不住：关门从 176 掉到 13 要 120ms，正好过了那条线。
    decay = [False] * n_frames
    run = 0
    for i in range(1, n_frames):
        if frames[i][0] < frames[i - 1][0] * _DECAY_RATIO and frames[i - 1][0] >= thr:
            run += 1
            if run >= _DECAY_RUN:
                for k in range(i - run, i + 1):
                    decay[k] = True
        else:
            run = 0

    out = []
    for i, (level, zcr) in enumerate(frames):
        is_speech = False
        if level >= thr and zcr <= _ZCR_MAX and not decay[i]:
            seg = ints[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES]
            if len(seg) < FRAME_SAMPLES:
                seg = list(seg) + [0] * (FRAME_SAMPLES - len(seg))
            spread = _spread(seg, sr)
            if spread >= _SPREAD_MIN:
                e = [_goertzel_energy(seg, f, sr) for f in _PROBE_HZ]
                tilt = sum(e[:3]) / (sum(e[3:]) + 1e-12)
                if tilt >= _TILT_MIN:
                    per = _periodicity(seg, sr)
                    # 浊音（元音）：周期 + 多频点；
                    # 清音（s/sh/f）：单靠周期不行，要求过零率很低但频点够散。
                    is_speech = (per >= _PERIOD_MIN) or (zcr <= 0.18)
        out.append((int((i + 1) * FRAME_MS), is_speech))
    return out
