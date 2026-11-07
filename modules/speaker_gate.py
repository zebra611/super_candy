"""说话人一致性门 —— 判断"这句话是不是刚才那个人说的"（WeSpeaker ONNX 版）。

先把定位说清楚（这一节比代码重要）：
    它**不是**"声纹比对"。声纹比对要事先注册（"这个人的声音长这样"），
    而外呼是**我们先打过去**，打之前根本不知道接电话的是谁，没有可比模板。
    所以这里做的是**同一通电话内的说话人一致性**：
        把开场应答的那个人当作"主说话人"，后面的话跟他比。
    本质是"穷人版在线说话人聚类"，只回答"像不像刚才那位"。

它解决什么：
    · 同一间屋子里另一个人插话被当成用户回答；
    · 电视/广播里的人声被当成用户；
    · TTS 回放（客服自己的声音经扬声器回流）——回声音色与用户差得远，最容易判。
      但 AEC 已经在浏览器侧做了，这里只是第二道闸，不能替代 AEC。

它解决不了什么（必须写进文档）：
    · **短句**（"是我""尾号0086" 1~2 秒）——嵌入不稳，只能弃权；
    · **同性别近音色**——再好的模型也会掉点，本方案只是把上限抬高；
    · **第一句**——没有模板就没有基准，旁人抢在第一句堵不住；
    · **谁"应该"接电话**——只知道换人了，不知道换上来的是店主还是他老婆。

为什么最终用 ONNX 模型而不是自己算 MFCC：
    先用纯 numpy 的 MFCC+F0 做过一版，本机实测 **EER 24%（短句 30%）**，
    零误杀工作点上 99% 的旁人会被放过——等于没用（数据在 docs/33）。
    换成 WeSpeaker 中文说话人模型（CN-Celeb ResNet34，26MB ONNX）后重测，
    差距是数量级的。所以这里保留模型路径，手工特征那版作为"为什么不行"的
    证据留在文档里，不再进代码。

依赖策略（与 silero VAD 同一套）：
    onnxruntime + 模型都不是硬依赖。缺了 → available() 返回 False，
    调用方完全忽略本模块，主链路行为一字不变。
    模型不进仓库（*.onnx 已 gitignore），用 scripts/fetch_speaker_model.py 下。
"""

import json
import os

try:
    import numpy as np
except Exception:                       # pragma: no cover
    np = None

try:
    import onnxruntime as ort
except Exception:                       # pragma: no cover
    ort = None

SR = 16000
FRAME = 400          # 25ms
HOP = 160            # 10ms
NFFT = 512
N_MEL = 80           # WeSpeaker 用 80 维 fbank
LOW_FREQ = 20.0

MIN_MS = 1200        # 短于这个时长不出结论
MIN_ENROLL = 3       # 至少攒够 3 条才敢比

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REL = os.path.join("data", "models", "wespeaker_zh_cnceleb_resnet34.onnx")
EXPECTED_SHA256 = "f86cd6c509f331f0e20b07bd48d1b2eb7de54202643401c4e84695ac861a0e5a"
CALIB_PATH = os.path.join(_ROOT, "data", "speaker_calib.json")

_session = None
_session_err = ""
_mel_fb = None


def model_path():
    p = os.environ.get("SPEAKER_MODEL") or DEFAULT_REL
    return p if os.path.isabs(p) else os.path.join(_ROOT, p)


def _load_session():
    global _session, _session_err
    if _session is not None or _session_err:
        return _session
    if ort is None:
        _session_err = "没有 onnxruntime"
        return None
    p = model_path()
    if not os.path.exists(p):
        _session_err = "缺模型（先跑 scripts/fetch_speaker_model.py）"
        return None
    try:
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1        # 单线程够了：一条 2s 音频约 30ms
        so.log_severity_level = 3
        _session = ort.InferenceSession(p, sess_options=so,
                                        providers=["CPUExecutionProvider"])
    except Exception as e:                 # 模型截断/损坏都会走这里
        _session_err = "模型加载失败：%s" % str(e)[:120]
        return None
    return _session


def available():
    """(是否可用, 原因)。不可用时调用方完全忽略本模块。"""
    if np is None:
        return False, "没有 numpy"
    if _load_session() is None:
        return False, _session_err
    if not os.path.exists(CALIB_PATH):
        return False, "未标定（先跑 scripts/bench_speaker_gate.py）"
    return True, ""


def load_calibration():
    try:
        with open(CALIB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Kaldi 风格 fbank（80 维）：WeSpeaker 训练时吃的就是这个
# ---------------------------------------------------------------------------
def _mel_fb_matrix():
    """Kaldi mel：mel = 1127*ln(1+f/700)，三角滤波器，low=20Hz，high=Nyquist。"""
    global _mel_fb
    if _mel_fb is not None:
        return _mel_fb
    def hz2mel(f):
        return 1127.0 * np.log(1.0 + f / 700.0)
    def mel2hz(m):
        return 700.0 * (np.exp(m / 1127.0) - 1.0)
    lo, hi = hz2mel(LOW_FREQ), hz2mel(SR / 2.0)
    pts = mel2hz(np.linspace(lo, hi, N_MEL + 2))
    bins = np.floor((NFFT + 1) * pts / SR).astype(int)
    fb = np.zeros((N_MEL, NFFT // 2 + 1), dtype=np.float32)
    for m in range(N_MEL):
        l, c, r = bins[m], bins[m + 1], bins[m + 2]
        for k in range(l, c):
            if c > l:
                fb[m, k] = (k - l) / float(c - l)
        for k in range(c, r):
            if r > c:
                fb[m, k] = (r - k) / float(r - c)
    _mel_fb = fb
    return fb


def fbank(pcm, cmn=True):
    """PCM16@16k → (T, 80) fbank。返回 (feat, 诊断)。

    细节按 Kaldi 默认走：25ms/10ms、预加重 0.97、povey 窗、自然对数、
    snip_edges（不足一帧的尾巴丢掉）。`cmn` 是倒谱均值归一化：
    WeSpeaker 训练时做了逐句 CMN，推理不同步会系统性掉点，所以默认开。
    """
    if np is None or not pcm:
        return None, {"reason": "no_numpy_or_empty"}
    x = np.frombuffer(pcm, dtype="<i2")
    if x.size < FRAME * 2:
        return None, {"reason": "too_short"}
    x = x.astype(np.float32) / 32768.0

    n = 1 + (len(x) - FRAME) // HOP
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    frames = x[idx].copy()
    frames[:, 1:] -= 0.97 * frames[:, :-1]
    w = np.power(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(FRAME) / (FRAME - 1)), 0.85)
    frames *= w.astype(np.float32)

    power = np.abs(np.fft.rfft(frames, n=NFFT)) ** 2
    mel = power @ _mel_fb_matrix().T
    feat = np.log(np.maximum(mel, 1e-10)).astype(np.float32)
    if cmn:
        feat = feat - feat.mean(axis=0, keepdims=True)
    return feat, {"frames": int(n), "ms": int(len(x) / SR * 1000)}


def embed(pcm):
    """PCM16@16k → 256 维说话人嵌入（已 L2 归一化）。返回 (list, 诊断)。"""
    sess = _load_session()
    if sess is None:
        return None, {"reason": _session_err or "no_model"}
    feat, diag = fbank(pcm)
    if feat is None:
        return None, diag
    if feat.shape[0] < 40:                 # 少于 0.4s 的帧数，模型也给不出稳定结果
        return None, {"reason": "too_few_frames", "frames": int(feat.shape[0])}
    x = feat[None, :, :].astype(np.float32)
    try:
        out = sess.run(["embs"], {"feats": x})[0]
    except Exception as e:
        return None, {"reason": "infer_failed:%s" % str(e)[:80]}
    v = np.asarray(out, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    if n <= 1e-9:
        return None, {"reason": "zero_norm"}
    diag.update({"dim": int(v.size)})
    return (v / n).tolist(), diag


def cosine(a, b):
    if a is None or b is None:
        return 0.0
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    den = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(va @ vb / den) if den > 1e-9 else 0.0


# ---------------------------------------------------------------------------
# 在线档案
# ---------------------------------------------------------------------------
class SpeakerProfile(object):
    """一通电话里的"主说话人"档案：前几句注册，之后逐句比。

    注册只应是**确认属于本人**的段落（例如核身通过之后）。把旁人话注册进去，
    后面全乱——这不是模型问题，是调用方的纪律问题。
    """

    def __init__(self, min_enroll=MIN_ENROLL, min_ms=MIN_MS,
                 accept=None, reject=None):
        cal = load_calibration()
        self.accept_th = float(accept if accept is not None
                              else cal.get("accept_threshold", 0.60))
        self.reject_th = float(reject if reject is not None
                               else cal.get("reject_threshold", 0.25))
        self.min_enroll = min_enroll
        self.min_ms = min_ms
        self._sum = None
        self.count = 0

    @property
    def enrolled(self):
        return self.count >= self.min_enroll

    def centroid(self):
        if self._sum is None or self.count == 0:
            return None
        v = self._sum / float(self.count)
        n = float(np.linalg.norm(v))
        return (v / n).tolist() if n > 1e-9 else None

    def add_pcm(self, pcm, alpha=1.0):
        """把一句加进档案。alpha 是学习率：

            alpha=1.0  注册期用（还没中心，第一条直接当中心）
            alpha=0.25 判为 same 时用（正常更新）
            alpha=0.05 判为 uncertain 时用（慢速跟随，防漂移又不僵死）

        为什么要有 uncertain 这一档：同一人不同句子的相似度并不稳定
        （本机 p05=0.65、均值 0.76），如果只有"same 才更新"，
        中心会停在最像的那三句上，越用越偏；如果"uncertain 也全量更新"，
        旁人一句就能把中心拽走。慢速跟随是这两者之间唯一站得住的做法。
        """
        emb, diag = embed(pcm)
        if emb is None or diag.get("ms", 0) < self.min_ms:
            return False
        v = np.asarray(emb, dtype=np.float32)
        if self._sum is None or self.count == 0:
            self._sum = v.copy()
        else:
            a = max(0.0, min(1.0, float(alpha)))
            self._sum = (1.0 - a) * self._sum + a * v
        self.count += 1
        return True

    def decide(self, pcm):
        """出结论：same / other / uncertain / abstain。

        abstain 与 uncertain 必须分开：
            abstain   没条件判（太短、没注册、没模型/没标定）——调用方应完全忽略，
                      否则短句会被系统性判成旁人；
            uncertain 有条件判但分数在灰区——可以记日志，不该影响用户。
        只有 other 才当"非用户"，这是刻意的：错杀比漏放贵得多。
        """
        ok, why = available()
        if not ok:
            return {"decision": "abstain", "score": None, "reason": why}
        emb, diag = embed(pcm)
        if emb is None:
            return {"decision": "abstain", "score": None,
                    "reason": diag.get("reason", "no_embedding")}
        ms = diag.get("ms", 0)
        if ms < self.min_ms:
            return {"decision": "abstain", "score": None, "reason": "too_short", "ms": ms}
        if not self.enrolled:
            return {"decision": "abstain", "score": None, "reason": "enrolling",
                    "enrolled": self.count, "need": self.min_enroll, "ms": ms}

        s = cosine(self.centroid(), emb)
        if s >= self.accept_th:
            return {"decision": "same", "score": round(s, 3), "ms": ms,
                    "enrolled": self.count}
        if s <= self.reject_th:
            return {"decision": "other", "score": round(s, 3), "ms": ms,
                    "enrolled": self.count}
        return {"decision": "uncertain", "score": round(s, 3), "ms": ms,
                "enrolled": self.count}
