"""M2 实时语音网关 —— 哑链路里的"假实现"。

职责（真实形态）：
    音频帧进来 → VAD 判断端点 → 流式 ASR 出文本；文本出去 → 流式 TTS 出音频；
    用户中途开口要能立刻掐断播报（barge-in）。

当前状态：全部返回占位数据，但**接口签名已经是最终形态**，
后续替换内部实现时，上层（app.py / dialogue.py）一行都不用改。

负责人：角色 1（实时语音与终端）

后续待补充（按优先级）：
    1. 接真实流式 ASR（FunASR 或云 ASR），配领域热词表；
    2. 接真实流式 TTS，并把"首包到达时间"打进 latency_ms；
    3. VAD 换成真正的能量/模型判定，替代现在的"整段录完才算说完"；
    4. 打断：detect_barge_in 目前恒为 False，真实实现需要在播报期间持续监听。
"""

import time


def vad_detect(audio_bytes, sample_rate=16000):
    """判断这段音频里有没有人说话（端点检测）。

    哑链路实现：只要有音频字节就认为有人说了话，不做任何能量判断。
    真实实现要做的：
        - 按 20–30ms 分帧算能量/过零率，或直接上 VAD 模型；
        - 返回"起始时间"与"结束时间"，而不只是一个布尔值；
        - 播报期间持续检测，命中即触发 barge-in。

    返回：dict(started=bool, ended=bool, speech_ms=int)
    """
    has_audio = bool(audio_bytes) and len(audio_bytes) > 0
    return {
        "started": has_audio,
        "ended": has_audio,
        # 16kHz / 16bit 单声道 → 每字节 0.5 个采样点，这里只做粗略估算
        "speech_ms": int(len(audio_bytes) / 32) if has_audio else 0,
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
        audio_bytes  浏览器上传的音频原始字节（哑链路不解码）
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

    真实实现要换掉的：
        - 服务端流式合成，首包就往下推，不要等整句合成完；
        - 记录"首包耗时"写进事件，这是 P50 门槛的关键一段；
        - 支持音色/语速配置（来自 config/scene.json）。

    返回：dict(mode, text, latency_ms)
    """
    return {
        "mode": "browser",      # 前端据此决定用 speechSynthesis 播报
        "text": text,
        "latency_ms": 0.0,      # 骨架阶段无服务端合成，故为 0
    }


def detect_barge_in(is_playing):
    """播报过程中是否被用户打断。

    哑链路实现：恒为 False。
    真实实现：播报期间持续跑 vad_detect，命中说话即返回 True，
    上层收到后要立刻 cancel 播报并回退到"重新聆听"状态。
    """
    return False
