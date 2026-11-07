"""生成 VAD 标定语料 —— 12 条真人声 + 9 类噪声 + 3 档混合。

为什么要有这个脚本：
    Z6 要回答的不是"VAD 能不能跑"，而是"**噪声片段还会不会触发 vad_result**"。
    没有语料，这句话就没法证伪——只能靠"我觉得 Silero 应该行"。
    所以先把"人声 / 噪声 / 人声+噪声"三类素材落成本地文件，后面
    `scripts/bench_vad.py` 在同一批文件上比较 rms / spectral / silero 三个引擎。

语料构成：
    人声  speech/s01..s12.wav
          12 条全部来自云端 TTS（真音色、有音节起伏），而非正弦调幅——
          合成"假人声"会把 Silero 和谱特征一起骗过去，标定出来的阈值不可信。
    噪声  noise/n01..n09.wav    九类演示现场最常见的声音，程序合成：
          白噪 / 粉噪 / 键盘 / 风扇 / 纸张摩擦 / 椅子 / 清嗓 / 关门 / 远处人声底噪
          合成噪声的局限要说清：它覆盖"谱形状与瞬态"这一类，
          但覆盖不了"真人咳嗽""旁人说话"的语义相似性——那部分靠
          真实通话回归（eval/adversarial_q1_regression.json）兜。
    混合  mixed/m01..m03.wav    人声 + 噪声（SNR ≈ 10 dB），验证"该响的时候还响"

产物不进仓库：`*.wav` 已在 .gitignore，语料是本地生成物（可复算）。
人声要花一次 TTS 的钱（12 条约几分钱），噪声合成零成本。

怎么用：
    python scripts/gen_vad_corpus.py              # 全量（人声缺失才合成）
    python scripts/gen_vad_corpus.py --force      # 强制重合成人声
    python scripts/gen_vad_corpus.py --noise-only # 只补噪声（不花钱）

为什么人声默认跳过已存在的文件：反复跑标定时不该重复计费。
"""

import argparse
import io
import math
import os
import random
import struct
import sys
import urllib.request
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader  # noqa: E402
from modules import voice  # noqa: E402

OUT_DIR = os.path.join(ROOT, "data", "vad_corpus")
SR = 16000

# 留出集：与调参集**不同的台词、不同的随机种子、不同的噪声幅度**。
# 为什么必须分开：阈值是在调参集上看着指标调出来的，同一批数据上 12/12、0/9
# 只能说明"拟合了这批语料"。留出集只用一次，数字才算数——
# 这条规矩和 eval 留出集（18 条，不得据此调参）是同一个道理。
HOLDOUT = {
    "dir": os.path.join(ROOT, "data", "vad_corpus_holdout"),
    "seed": 20261121,
    "amp_scale": 1.35,      # 更响的噪声：调参集上的阈值在这里会被顶穿
}

SPEECH_LINES_HOLDOUT = [
    ("h01", "您这边方便的话，我帮您把复核申请提交上去，大概明天就有结果。"),
    ("h02", "不好意思，刚才信号不太好，您再说一遍可以吗？"),
    ("h03", "我不是要投诉，我就是想知道到底还差什么材料。"),
    ("h04", "那我现在重新上传一遍营业执照，还需要别的吗？"),
    ("h05", "对，就是这个类目，没错。"),
    ("h06", "麻烦你帮我备注一下，我下午三点以后都在。"),
]

# 人声台词：一半是客服侧（长句、有停顿），一半是用户侧（短句、口语、带犹豫）。
# 为什么要分两侧：端点检测对"短促应答"和"连续长句"的容错完全不同，
# 只用长句标定会把 300ms 的"嗯，我知道"判成噪声。
SPEECH_LINES = [
    ("s01", "您好，我是京东客服中心的小采，请问是张伟先生吗？"),
    ("s02", "您提交的品牌授权资质被驳回了，原因是授权书已过期，需要重新上传。"),
    ("s03", "驳回原因有两项：一是商标注册证缺失，二是类目资质不匹配。"),
    ("s04", "材料补齐之后，我们会在一个工作日内完成复审，结果会以短信通知您。"),
    ("s05", "如果对驳回结果有异议，可以补充说明材料，我们会转交专员核实。"),
    ("s06", "关单方式有两种：电话回复或者京麦留言，您方便哪一种？"),
    ("s07", "你好，我是张伟，你说吧。"),
    ("s08", "等一下，我想问个问题。"),
    ("s09", "嗯，我知道了这个事情，那接下来怎么办？"),
    ("s10", "授权书没过期啊，我上个月才传的。"),
    ("s11", "能不能直接帮我改一下，我这边比较急。"),
    ("s12", "行，那我今天下午就传，麻烦你了。"),
]


def _wav_bytes(ints, sr=SR):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<%dh" % len(ints), *ints))
    return buf.getvalue()


def _write(path, ints, sr=SR):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(_wav_bytes(ints, sr))


def _clip(v, lo=-32768, hi=32767):
    return lo if v < lo else (hi if v > hi else int(v))


# ---------------------------------------------------------------------------
# 人声：云端 TTS → 16k 单声道（口径与 check_realtime_asr.tts_to_pcm 一致）
# ---------------------------------------------------------------------------

def _tts_pcm(text):
    out = voice.tts(text)
    urls = []
    if out.get("audio_url"):
        urls.append(out["audio_url"])
    for seg in (out.get("segments") or []):
        u = seg.get("audio_url")
        if u and u not in urls:
            urls.append(u)
    if not urls:
        raise RuntimeError("合成没返回链接（mode=%s，error=%s）"
                           % (out.get("mode"), out.get("error")))

    ints_all = []
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
        if sr != SR:
            ratio = sr / float(SR)
            res, i = [], 0.0
            while int(i) < len(ints):
                res.append(ints[int(i)])
                i += ratio
            ints = res
        ints_all.extend(ints)
    return list(ints_all)


# ---------------------------------------------------------------------------
# 噪声：九类，程序合成
# ---------------------------------------------------------------------------
# 合成噪声只覆盖"谱形状 + 瞬态结构"这一类特征。真人咳嗽、旁人说话这类
# 与语音同分布的声音，合成不出来也不该硬造——所以本脚本只保证
# "谱形状不同的噪声"被覆盖，语义相似的那部分交给真实通话回归。
# ---------------------------------------------------------------------------

def _white(n, amp):
    return [random.uniform(-amp, amp) for _ in range(n)]


def _pink(n, amp):
    """粉噪：一阶低通串联近似 1/f（频谱斜率 -3dB/oct 的粗版）。

    为什么不用真 Voss 算法：标定只关心"谱能量向低频倾斜"这个形状，
    一阶滤波串联出来的斜率已经够用，而且没有状态复杂度。
    """
    out = []
    b = [0.0] * 5
    for _ in range(n):
        w = random.uniform(-1, 1)
        b[0] = 0.99886 * b[0] + w * 0.0555179
        b[1] = 0.99332 * b[1] + w * 0.0750759
        b[2] = 0.96900 * b[2] + w * 0.1538520
        b[3] = 0.86650 * b[3] + w * 0.3104856
        b[4] = 0.55000 * b[4] + w * 0.5329522
        out.append((sum(b) + random.uniform(-1, 1) * 0.5362) * amp * 0.11)
    return out


def _keyboard(n, amp):
    """键盘：每秒 6~9 次极短（5~12ms）宽带瞬态。检测它靠的是"瞬态太短"。"""
    out = [0.0] * n
    t = 0
    while t < n:
        dur = int(SR * random.uniform(0.005, 0.012))
        peak = amp * random.uniform(0.6, 1.0)
        for k in range(dur):
            if t + k >= n:
                break
            env = math.exp(-6.0 * k / dur)
            out[t + k] += random.uniform(-peak, peak) * env
        t += int(SR * random.uniform(0.09, 0.22))
    return out


def _fan(n, amp):
    """风扇：低频稳态（120Hz + 二三次谐波）+ 少量宽带底噪，幅度几乎不变。"""
    out = []
    for i in range(n):
        s = (math.sin(2 * math.pi * 120 * i / SR) * 0.6
             + math.sin(2 * math.pi * 240 * i / SR) * 0.25
             + math.sin(2 * math.pi * 60 * i / SR) * 0.3)
        out.append(s * amp + random.uniform(-amp, amp) * 0.12)
    return out


def _paper(n, amp):
    """纸张摩擦：带通噪声（2~6kHz）+ 随机慢包络，比语音更"沙"更平。"""
    out = []
    prev = 0.0
    env = 0.0
    for i in range(n):
        w = random.uniform(-1, 1)
        # 一阶高通（去掉低频）× 一阶低通（去掉最高频）≈ 带通
        hp = w - prev
        prev = w
        env = env * 0.999 + random.uniform(-1, 1) * 0.001
        out.append(hp * amp * (0.5 + 0.5 * min(1.0, abs(env) * 8)))
    return out


def _chair(n, amp):
    """椅子：低频冲击 + 短摩擦，1 秒 1~2 次，冲击后 200ms 内衰减完。"""
    out = [0.0] * n
    t = 0
    while t < n:
        dur = int(SR * 0.2)
        for k in range(dur):
            if t + k >= n:
                break
            env = math.exp(-14.0 * k / dur)
            out[t + k] += (math.sin(2 * math.pi * 85 * k / SR) * 0.7
                           + random.uniform(-0.3, 0.3)) * amp * env
        t += int(SR * random.uniform(0.7, 1.1))
    return out


def _cough(n, amp):
    """清嗓/咳嗽：200ms 宽带噪声爆发，起振 20ms、衰减 180ms。

    为什么**不用**"780Hz 正弦 + 噪声"来合成（第一版就是这么写的，标定后改掉）：
    那样造出来的是一个元音"啊"，周期性 0.55、低频占比 10 倍——
    谱特征上与人声不可分，测出来"咳嗽误触"其实是语料造错了，
    不是算法不行。真实咳嗽以声门爆破的宽带噪声为主，周期性低。
    改语料是为了让它像真声音，不是为了刷指标；改完两个引擎都要重跑。
    """
    out = [0.0] * n
    t = int(SR * 0.3)
    dur = int(SR * 0.22)
    prev = 0.0
    for k in range(dur):
        if t + k >= n:
            break
        # 双相包络：20ms 快速起振 + 缓慢衰减（咳嗽的"咳"是爆发不是渐强）
        x = k / float(dur)
        env = (x / 0.09) if x < 0.09 else math.exp(-3.6 * (x - 0.09))
        w = random.uniform(-1, 1)
        hp = w - prev * 0.85          # 一阶高通：抬高高频，压掉低频轰鸣
        prev = w
        out[t + k] = hp * amp * env
    return out


def _door(n, amp):
    """关门：单次强低频冲击 + 混响尾巴（3 次指数衰减回声）。"""
    out = [0.0] * n
    t = int(SR * 0.4)
    dur = int(SR * 0.35)
    for k in range(dur):
        if t + k >= n:
            break
        env = math.exp(-9.0 * k / dur)
        out[t + k] += (math.sin(2 * math.pi * 70 * k / SR) * 0.8
                       + random.uniform(-0.4, 0.4)) * amp * env
    for d, g in ((int(SR * 0.12), 0.35), (int(SR * 0.26), 0.18), (int(SR * 0.41), 0.09)):
        for k in range(int(SR * 0.25)):
            if t + d + k >= n:
                break
            env = math.exp(-12.0 * k / (SR * 0.25))
            out[t + d + k] += random.uniform(-1, 1) * amp * g * env
    return out


def _ambient(n, amp):
    """远处人声底噪：多个低频调幅噪声叠加，像隔着一堵墙的交谈。

    这一类最难：它既有语音的调幅节奏，又没有清晰基频。
    单靠能量或谱形状都会误判，只能靠"周期性/清晰度"这一类特征挡。
    """
    out = [0.0] * n
    mods = [(random.uniform(2.0, 4.5), random.uniform(0, 6.28)) for _ in range(4)]
    for i in range(n):
        m = 0.0
        for f, ph in mods:
            m += max(0.0, math.sin(2 * math.pi * f * i / SR + ph))
        out.append(random.uniform(-1, 1) * amp * (0.25 + 0.75 * m / len(mods)))
    return out


NOISES = [
    ("n01_white", "白噪声（空调出风口/气流）", _white, 0.06),
    ("n02_pink", "粉红噪声（机房底噪）", _pink, 0.10),
    ("n03_keyboard", "键盘敲击", _keyboard, 0.35),
    ("n04_fan", "风扇/机箱低频", _fan, 0.10),
    ("n05_paper", "纸张翻动/摩擦", _paper, 0.25),
    ("n06_chair", "椅子挪动", _chair, 0.30),
    ("n07_cough", "清嗓/咳嗽", _cough, 0.40),
    ("n08_door", "关门撞击", _door, 0.35),
    ("n09_ambient", "远处人声底噪", _ambient, 0.09),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已存在也重新合成人声（会重复计费）")
    ap.add_argument("--noise-only", action="store_true", help="只生成噪声，不调 TTS")
    ap.add_argument("--holdout", action="store_true",
                    help="生成留出集（不同台词/种子/噪声幅度）到 data/vad_corpus_holdout")
    ap.add_argument("--seconds", type=float, default=4.0, help="每条噪声时长（秒）")
    args = ap.parse_args()

    out_dir = HOLDOUT["dir"] if args.holdout else OUT_DIR
    lines = SPEECH_LINES_HOLDOUT if args.holdout else SPEECH_LINES
    seed = HOLDOUT["seed"] if args.holdout else 20261107
    amp_scale = HOLDOUT["amp_scale"] if args.holdout else 1.0

    random.seed(seed)              # 固定种子：噪声可复算，两次跑出来的文件应逐字节相同
    n = int(SR * args.seconds)

    print("=" * 70)
    print("VAD 标定语料生成 → %s%s" % (out_dir, "（留出集）" if args.holdout else ""))
    print("=" * 70)

    noise_pcm = {}
    for name, desc, fn, amp in NOISES:
        ints = [_clip(v * 32767) for v in fn(n, amp * amp_scale)]
        path = os.path.join(out_dir, "noise", name + ".wav")
        _write(path, ints)
        noise_pcm[name] = ints
        print("  [噪声] %-12s %-18s %d ms（幅度 ×%.2f）"
              % (name, desc, int(n * 1000 / SR), amp_scale))

    if args.noise_only:
        print("\n只生成了噪声（--noise-only）。人声语料沿用已有文件。")
        return 0

    if not config_loader.secret("dashscope_api_key"):
        print("\n[跳过人声] 没读到 dashscope_api_key——噪声已生成，人声语料缺失。")
        print("           没有密钥时 bench_vad 只能验噪声侧，人声召回无法证明。")
        return 1

    made = 0
    for name, text in lines:
        path = os.path.join(out_dir, "speech", name + ".wav")
        if os.path.exists(path) and not args.force:
            print("  [人声] %-5s 已存在，跳过" % name)
            continue
        try:
            ints = _tts_pcm(text)
        except Exception as exc:
            print("  [人声] %-5s 合成失败：%s" % (name, str(exc)[:120]))
            continue
        _write(path, ints)
        made += 1
        print("  [人声] %-5s %d ms  %s" % (name, int(len(ints) * 1000 / SR), text[:18]))
    print("\n人声：新合成 %d 条，目录 %s" % (made, os.path.join(out_dir, "speech")))

    # 混合：人声 + 噪声（按能量比对齐到 SNR≈10dB），验证"有噪声时人声仍要被判出来"
    if args.holdout:
        mixes = [("m01", "h01", "n04_fan"), ("m02", "h03", "n03_keyboard"),
                 ("m03", "h06", "n09_ambient")]
    else:
        mixes = [("m01", "s02", "n04_fan"), ("m02", "s08", "n03_keyboard"),
                 ("m03", "s10", "n09_ambient")]
    for mname, sname, nname in mixes:
        sp = os.path.join(out_dir, "speech", sname + ".wav")
        if not os.path.exists(sp):
            continue
        with wave.open(sp) as w:
            raw = w.readframes(w.getnframes())
        speech = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
        noise = noise_pcm.get(nname)
        if not noise:
            continue
        # 语音有效能量（取最响的 30% 帧均值）对齐噪声能量
        frame = 320
        energies = []
        for i in range(0, len(speech) - frame, frame):
            seg = speech[i:i + frame]
            energies.append(sum(v * v for v in seg) / frame)
        energies.sort()
        speech_ms = sum(energies[int(len(energies) * 0.7):]) / max(1, len(energies) * 3 // 10)
        noise_ms = sum(v * v for v in noise) / len(noise)
        gain = math.sqrt(speech_ms / max(1.0, noise_ms) / 10.0)   # 10^(10/20)
        out = []
        for i, v in enumerate(speech):
            nz = noise[i % len(noise)] * gain
            out.append(_clip(v + nz))
        path = os.path.join(out_dir, "mixed", mname + ".wav")
        _write(path, out)
        print("  [混合] %-4s = %s + %s（SNR≈10dB，噪声增益 %.2f）"
              % (mname, sname, nname, gain))
    return 0


if __name__ == "__main__":
    sys.exit(main())
