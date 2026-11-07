"""Silero VAD 的 ONNX 后端 —— **可选依赖**，缺了不影响主链路。

为什么是"可选"而不是写进 requirements：
    这个项目的硬约束是"队友 clone 下来就能跑、干净机器一条命令起得来"。
    onnxruntime 一个 wheel 就 15MB，为了一个端点检测把所有人的环境都拖重
    不划算。所以做成：装了 onnxruntime + 模型 → 用真 Silero；
    没装 → vad_gate 自动降级到纯标准库谱特征门，**行为可测、结果可解释**。
    降级不是"糊过去"：两条路径由同一批语料标定、同一个 bench 脚本比较，
    差异写在报告里（Silero 召回更高、谱特征零依赖）。

模型从哪来（不进仓库，*.onnx 已在 .gitignore）：
    python scripts/fetch_silero_vad.py
    下载 2.3MB 的 silero_vad.onnx 到 data/models/，带 sha256 校验。
    想换路径就设环境变量 SILERO_VAD_MODEL 或 config/scene.json 的 vad.model。

模型版本与接口（v5，2024-09 后的官方 onnx）：
    input [batch?, 512] float32   16kHz 下窗口固定 512 采样（32ms），无重叠
    state [2, batch, 128] float32 LSTM 状态，每窗递推
    sr    int64 标量              16000
    → output prob、stateN
    注意：这个模型**不自己判端点**，只给每窗"是人声的概率"；
    起止判定（连续几帧算开口、静音多久算说完）在本文件的 probs() 之上
    由 vad_gate 统一做，两条引擎共用同一套聚合规则，才能公平比较。

实测性能（本机 i7 / CPU EP）：0.33 ms/窗，1 秒音频 31 窗 ≈ 10ms，实时率约 1%。
负责人：角色 1（实时语音与终端）
"""

import os
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 16kHz 下的固定窗口。换 8k 要改成 256——但项目契约固定 16k（contracts.PCM_SAMPLE_RATE），
# 所以这里不做自适应，写死并在不合约时抛，免得静默算错。
WINDOW = 512
STATE_SHAPE = (2, 1, 128)

DEFAULT_REL = os.path.join("data", "models", "silero_vad.onnx")
EXPECTED_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
EXPECTED_BYTES = 2327524

_lock = threading.Lock()
_cache = {"tried": False, "inst": None, "error": ""}


def model_path():
    """模型文件路径：环境变量 > 默认 data/models/silero_vad.onnx。"""
    env = os.environ.get("SILERO_VAD_MODEL")
    if env:
        return env
    return os.path.join(_ROOT, DEFAULT_REL)


def available():
    """不加载模型，只回答"现在能不能用 Silero"。

    三个条件缺一不可：onnxruntime 装得上、模型文件在、文件大小对得上
    （大小不对多半是下载了 HTML 错误页被存成了 .onnx——这种情况
    onnxruntime 会抛一个很难懂的 INVALID_PROTOBUF，不如在这里先说清）。
    """
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False
    p = model_path()
    try:
        return os.path.isfile(p) and os.path.getsize(p) == EXPECTED_BYTES
    except OSError:
        return False


def reason_unavailable():
    """如实说明为什么用不了——降级原因必须能被写进事件与报告。"""
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return "onnxruntime 未安装"
    p = model_path()
    if not os.path.isfile(p):
        return "模型缺失：%s" % p
    size = os.path.getsize(p)
    if size != EXPECTED_BYTES:
        return "模型大小不符（%d ≠ %d）" % (size, EXPECTED_BYTES)
    return ""


def load():
    """加载并缓存一个推理实例。失败返回 None（原因留在 last_error()）。

    为什么要缓存：onnxruntime 建 session 要 100ms 级，每次 vad_detect 都建
    会把首字预算吃光；而 session 本身是线程安全的（run 可并发），
    所以一个进程一个实例就够。
    """
    with _lock:
        if _cache["tried"]:
            return _cache["inst"]
        _cache["tried"] = True
        _cache["error"] = ""
        why = reason_unavailable()
        if why:
            _cache["error"] = why
            return None
        try:
            inst = SileroVad(model_path())
            _cache["inst"] = inst
            return inst
        except Exception as exc:
            _cache["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            return None


def last_error():
    return _cache["error"]


def reset_cache():
    """测试用：忘掉已加载的实例（换模型路径、注入假 session 后要重新取）。"""
    with _lock:
        _cache["tried"] = False
        _cache["inst"] = None
        _cache["error"] = ""


class SileroVad:
    """一个可复用的推理 session。每次 analyze 都从零状态开始（一段音频一次判定）。"""

    def __init__(self, path):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        # 单线程足够：0.33ms/窗，多线程反而被 GIL 与线程调度拖慢；
        # 而且主链路是单通话串行处理，开多线程只会跟别的请求抢核。
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3          # 别把 INFO 级别的加载日志打到通话日志里
        self._sess = ort.InferenceSession(path, sess_options=opts,
                                          providers=["CPUExecutionProvider"])
        self._in = [i.name for i in self._sess.get_inputs()]

    def probs(self, pcm_bytes, sample_rate=16000):
        """把一段 PCM16LE 换成每窗的人声概率列表（长度 = ceil(采样数/512)）。

        末尾不足一窗要**补零**而不是丢弃：0→200ms 的短促应答（"嗯"）
        常常只有 5~6 个窗，丢尾窗可能把整句丢掉。
        补零的窗概率会偏低，这是可接受的保守方向（宁可漏判成静音，
        也不要把噪声判成人声——后者会把状态机推进到"用户说话"）。
        """
        import numpy as np

        if sample_rate != 16000:
            raise ValueError("Silero 后端只接 16k（契约 PCM_SAMPLE_RATE），收到 %d"
                             % sample_rate)
        if not pcm_bytes:
            return []

        data = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
        n_win = (len(data) + WINDOW - 1) // WINDOW
        pad = n_win * WINDOW - len(data)
        if pad:
            data = np.concatenate([data, np.zeros(pad, dtype=np.float32)])
        frames = data.reshape(n_win, WINDOW)

        state = np.zeros(STATE_SHAPE, dtype=np.float32)
        sr = np.array(16000, dtype=np.int64)
        # v5 的 ONNX 图要 576 个采样：64 个"上一窗尾巴"(context) + 本窗 512。
        # 只喂 512 也能跑（形状是动态的），但模型看到的是一段没有前文的孤立波形，
        # 概率会一直贴着 0——实测 12 条人声全部判成静音，排查了一轮才发现。
        # context 只在**同一段音频内部**递推；换一段音频必须归零，
        # 否则上一轮的话会渗进下一轮的判定。
        ctx = np.zeros((1, 64), dtype=np.float32)
        out = []
        for i in range(n_win):
            chunk = frames[i:i + 1]          # [1, 512]
            inp = np.concatenate([ctx, chunk], axis=1)   # [1, 576]
            res = self._sess.run(None, {"input": inp, "state": state, "sr": sr})
            out.append(float(res[0][0][0]))
            state = res[1]
            ctx = inp[:, -64:]
        return out
