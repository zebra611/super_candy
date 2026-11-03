"""M2 实时语音网关 —— 哑链路里的"假实现"。

职责（真实形态）：
    音频帧进来 → VAD 判断端点 → 流式 ASR 出文本；文本出去 → 流式 TTS 出音频；
    用户中途开口要能立刻掐断播报（barge-in）。

当前状态：**采集侧已经是真的**（S1 完成：浏览器 AudioWorklet 16kHz/20ms 分帧上传），
下面三个函数仍是占位实现，但**接口签名已经是最终形态**，
后续替换内部实现时，上层（app.py / dialogue.py）一行都不用改。

负责人：角色 1（实时语音与终端）

后续待补充（按优先级）：
    1. 接真实流式 ASR（FunASR 或云 ASR），配领域热词表；现在 asr() 不看音频内容；
    2. 接真实流式 TTS，并把"首包到达时间"打进 latency_ms；
    3. VAD 换成 Silero 逐帧判定，替代现在的"有字节就算说完"；
    4. 打断：detect_barge_in 目前恒为 False，真实实现需要在播报期间持续监听。
    （分帧与传输已完成，见 S1；S2 会把 HTTP 分片换成 WebSocket。）
"""

import struct
import time

import config_loader
import contracts


def pcm_duration_ms(audio_bytes):
    """这段 PCM 有多少毫秒。

    口径统一走契约里的采样率与位宽，不要在别处再写一次 32000——
    "每毫秒 32 字节"这种常数一旦散落在多个文件里，改采样率就会漏改一处。
    """
    if not audio_bytes:
        return 0
    samples = len(audio_bytes) // contracts.PCM_SAMPLE_WIDTH
    return int(samples * 1000 / contracts.PCM_SAMPLE_RATE)


def pcm_to_samples(audio_bytes):
    """把 PCM16LE 字节解成 -1.0~1.0 的浮点采样序列。

    现在只有 VAD 和调试会用到；接实时 ASR 时不需要它（直接把字节推给云端），
    但做能量分析、波形回放、离线复算时必须先归一化。
    用 struct 而不是 numpy：全项目不引第三方依赖这条约束还没解除。
    """
    n = len(audio_bytes) // contracts.PCM_SAMPLE_WIDTH
    if not n:
        return []
    ints = struct.unpack("<%dh" % n, audio_bytes[:n * contracts.PCM_SAMPLE_WIDTH])
    return [v / 32768.0 for v in ints]


def vad_detect(audio_bytes, sample_rate=contracts.PCM_SAMPLE_RATE):
    """判断这段音频里有没有人说话（端点检测）。

    哑链路实现：只要有音频字节就认为有人说了话，不做任何能量判断。
    但**时长与采样数现在是真算的**——因为 S1 之后传上来的已经是 PCM16，
    不再是压缩后的 webm，字节数与时长有确定的换算关系，不需要再估。

    真实实现要做的（S3）：
        - 逐帧跑 Silero VAD，返回"起始帧 / 结束帧"，而不只是一个布尔值；
        - 播报期间持续检测，命中即触发 barge-in；
        - 加最短语音时长过滤（≥200ms），挡掉咳嗽与键盘声。

    返回：dict(started, ended, speech_ms, samples, sample_rate)
    """
    has_audio = bool(audio_bytes)
    samples = len(audio_bytes) // contracts.PCM_SAMPLE_WIDTH if has_audio else 0
    return {
        "started": has_audio,
        "ended": has_audio,
        "speech_ms": pcm_duration_ms(audio_bytes),
        "samples": samples,
        "sample_rate": sample_rate,
    }


def asr(audio_bytes, hints=None):
    """把一段音频转成文字。

    哑链路实现：**不看音频内容**，直接返回一句固定文本。
    这是整条链路里最明显的"假"，但它的价值在于：
    让 app.py、dialogue.py、前端字幕、事件流全部先跑起来。

    真实实现要换掉的：
        - 换成流式识别，边收边出中间结果（用于提前判断意图、压低首字延迟）；
        - 支持 hints（领域词表：人名、产品名、工单号）；
        - 返回字级时间戳，打断判定和字幕对齐都要用。

    参数：
        audio_bytes  PCM16LE / 16kHz / 单声道原始字节（S1 之后格式已固定）
        hints        领域热词列表，当前被忽略
    返回：dict(text, confidence, latency_ms, fake)
    """
    t0 = time.time()
    text = "（假识别）你好，我是客服，请问是张先生吗？"
    return {
        "text": text,
        "confidence": 1.0,
        "latency_ms": round((time.time() - t0) * 1000, 1),
        "fake": True,   # 标记这是占位结果，评测时要过滤掉，别把假的算进分数
    }


def tts(text):
    """把回复文本变成语音。

    哑链路实现：**服务端不合成**，只回一个"请前端自己念"的指令。
    前端用浏览器内置的 speechSynthesis 播报，所以链路里确实有声音，
    但不是我们自己的 TTS——这是有意为之，先不引入任何外部服务。

    音色与语速来自 config/scene.json 的 tts 段：换音色属于业务决策，
    不该改代码，所以哪怕现在只是传给浏览器，也走配置。

    真实实现要换掉的：
        - 服务端流式合成，首包就往下推，不要等整句合成完；
        - 记录"首包耗时"写进事件，这是 P50 门槛的关键一段。

    返回：dict(mode, text, voice, rate, latency_ms)
    """
    cfg = config_loader.tts_config()
    return {
        "mode": "browser",      # 前端据此决定用 speechSynthesis 播报
        "text": text,
        "voice": cfg.get("voice", ""),
        "rate": float(cfg.get("rate", 1.0)),
        "latency_ms": 0.0,      # 骨架阶段无服务端合成，故为 0
    }


def detect_barge_in(is_playing):
    """播报过程中是否被用户打断。

    哑链路实现：恒为 False。
    真实实现：播报期间持续跑 vad_detect，命中说话即返回 True，
    上层收到后要立刻 cancel 播报并回退到"重新聆听"状态。
    """
    return False
