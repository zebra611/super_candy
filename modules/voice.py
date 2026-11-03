"""M2 实时语音网关 —— 采集、识别、合成都是真的；VAD 与打断仍是缺口。

职责（真实形态）：
    音频帧进来 → VAD 判断端点 → 流式 ASR 出文本；文本出去 → 流式 TTS 出音频；
    用户中途开口要能立刻掐断播报（barge-in）。

当前状态（每一条都经实测，不是照设计写的）：
    asr()        真识别，走 qwen3-asr-flash 的 HTTP 接口（整段上传，非流式）；
                 送识别前有 RMS 静音闸门，避免把静音识别成「嗯。」；
    tts()        真合成，走 qwen3-tts-flash，返回 OSS 音频链接给前端播；
                 合成失败回落到浏览器播报，不允许静默；
    vad_detect() 仍是占位：只判"有没有声音"，不判"说到哪算结束"，
                 所以现在是"松手即视为说完"；
    barge_in     做不到，原因见下面第 4 条，不是忘了写。

负责人：角色 1（实时语音与终端）

后续待补充（按优先级）：
    1. 流式 ASR：现在一轮说完整段上传，请求耗时会随音频增长且长尾不可控
       （实测一轮 1.9 秒的音频耗过 15.5 秒）；
    2. 流式 TTS：现在整句合成完才返回，首字要多等约 1.1 秒，逼近 2 秒门槛；
    3. VAD 换成 Silero 逐帧判定，替代现在的"有字节就算说完"；
    4. 打断：半双工下前端"松手才上传音频"，播报期间服务器收不到任何上行声音，
       既没有信号可检测、也没有通道可中断。要做得先把 S2 的 WebSocket
       双向通道建起来。contracts.EVENT_TYPES 里的 barge_in 是为此预留的事件位。
    （分帧与传输已完成，见 S1；S2 会把 HTTP 分片换成 WebSocket。）
"""

import base64
import http.client
import io
import json
import socket
import struct
import time
import urllib.error
import urllib.request
import wave

import config_loader

# 重试间隔（秒）。不要改成 0：对端刚掐断连接时立刻重连，大概率撞上同一个问题，
# 0.15 秒足够让对端把旧连接清掉，又短到不影响首字响应。
_RETRY_BACKOFF = 0.15
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
    """把一段音频转成文字。**这是全链路唯一需要替换的入口。**

    两条实现，由配置决定，调用方不需要知道用的是哪条：
        placeholder  占位：不看音频内容，永远返回同一句（没配密钥时用）
        dashscope    真识别：qwen3-asr-flash，走 HTTP，返回真文本

    为什么云端选"非实时"而不是"实时流式"：
        我们现在是半双工——用户说完一整句才送识别，本来就没有边听边出的需求。
        非实时接口是一次 HTTP POST，不需要 WebSocket、不需要 SDK、不引任何依赖，
        在沙箱里也能跑；实时接口要长连接，等 S2 做完再换更有价值。
        换的时候只改 config 的 model/endpoint，这个函数的签名不变。

    参数：
        audio_bytes  PCM16LE / 16kHz / 单声道原始字节（格式由 contracts 固定）
        hints        领域热词。当前 qwen3-asr-flash 的 HTTP 接口没有热词字段，
                     传了也不用；保留这个参数是为了将来换 Paraformer 时不用改上层
    返回：dict(text, confidence, latency_ms, fake, provider)
         出错时额外带 error 字段，且 fake=True——失败结果不能被算成真实识别
    """
    provider = config_loader.asr_provider()
    if provider == "placeholder":
        return _asr_placeholder()

    t0 = time.time()
    level = pcm_rms(audio_bytes)
    threshold = float(config_loader.asr_config().get("silence_rms", 0.003))
    if level < threshold:
        # 明显是静音：不发请求，直接当"没人说话"。
        # 省一次调用是顺带的，主要是避免模型对静音幻觉出一句"嗯。"
        # （实测确实会），那会让状态机以为用户开了口。
        # 这个门限是 S3 的临时替代品：真的 VAD 要判"说到哪结束"，不只是"有没有声"。
        return {
            "text": "",
            "confidence": None,
            "latency_ms": 0.0,
            "fake": False,
            "provider": provider,
            "rms": round(level, 5),
            "skipped": "silent",
        }

    try:
        text = _asr_dashscope(audio_bytes)
    except Exception as e:
        # 识别失败不让通话崩掉：返回空文本 + error，由 app.py 记一条 error 事件。
        # 外呼里因为网络抖一下就挂断，比识别错一句严重得多。
        return {
            "text": "",
            "confidence": None,
            "latency_ms": round((time.time() - t0) * 1000, 1),
            "fake": True,
            "provider": provider,
            "rms": round(level, 5),
            "error": str(e)[:300],
        }

    return {
        "text": text,
        "confidence": None,      # 该接口不返回置信度，不编一个假的填进去
        "latency_ms": round((time.time() - t0) * 1000, 1),
        "fake": False,           # 真识别结果，评测才会把它算进分数
        "provider": provider,
        "rms": round(level, 5),  # 记下音量，方便用真实录音标定 silence_rms
    }


def _asr_placeholder():
    """占位识别：不看音频，永远同一句。

    它的价值只有一条：让 app.py、dialogue.py、前端字幕、事件流先跑起来。
    千万别把这个结果当成"识别能力"——它连标点都是写死的。
    """
    t0 = time.time()
    return {
        "text": "（假识别）你好，我是客服，请问是张先生吗？",
        "confidence": 1.0,
        "latency_ms": round((time.time() - t0) * 1000, 1),
        "fake": True,
        "provider": "placeholder",
    }


def pcm_to_wav(pcm):
    """把裸 PCM 包成一个最小 WAV 容器。

    为什么必须包：云端接口要的是"音频文件"，不是裸采样流。
    有 WAV 头它才知道采样率与位宽；只发 PCM 字节，服务端只能靠猜，
    猜错就是"能返回文字但全是错的"，比直接报错更难查。
    用标准库 wave 写进内存，不落盘（避免在磁盘上留用户录音）。
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(contracts.PCM_CHANNELS)
        w.setsampwidth(contracts.PCM_SAMPLE_WIDTH)
        w.setframerate(contracts.PCM_SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def pcm_rms(pcm):
    """算这段 PCM 的均方根（0–1）。用来区分"真的在说话"和"只是麦克风开着"。

    为什么必须有这一步：云端识别对纯静音**会返回东西**。
    实测把 1 秒静音送进去，它返回「嗯。」——这会被当成用户说了话，
    推进状态机、还可能触发一次工具调用。
    与其在识别结果里过滤"嗯"（治不完），不如在送识别之前就判断有没有声音。
    """
    n = len(pcm) // contracts.PCM_SAMPLE_WIDTH
    if not n:
        return 0.0
    samples = struct.unpack("<%dh" % n, pcm[:n * contracts.PCM_SAMPLE_WIDTH])
    total = 0
    for v in samples:
        f = v / 32768.0
        total += f * f
    return (total / n) ** 0.5


def _asr_dashscope(pcm):
    """调用 qwen3-asr-flash（OpenAI 兼容的 chat/completions 接口），返回识别文本。

    请求形状来自官方文档：音频以 data URL 放在 input_audio.data 里，
    文本在 choices[0].message.content。

    为什么用 urllib 而不是装 SDK：
        一次 POST 就够，为它引一个包不划算；而且线上沙箱 installCmd 是空的，
        多一个依赖就多一处部署失败的可能。
    """
    cfg = config_loader.asr_config()
    key = config_loader.secret("dashscope_api_key")
    if not key:
        raise RuntimeError("未配置 dashscope_api_key")
    if len(pcm) < contracts.PCM_SAMPLE_RATE // 2:      # 不足 0.5 秒，识别没有意义
        return ""

    wav = pcm_to_wav(pcm)
    # 接口限制：base64 后不超过 10MB。超了直接报清楚，别等服务器返回一个含糊的错。
    if len(wav) * 4 // 3 > 9 * 1024 * 1024:
        raise RuntimeError("音频过长（%d 字节 PCM），请缩短单轮时长" % len(pcm))

    data_uri = "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")
    body = {
        "model": cfg.get("model") or "qwen3-asr-flash",
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": data_uri}}
        ]}],
        "stream": False,
        # ITN：把"八八六六"转成"8866"。身份核验要靠数字匹配，
        # 不转的话用户说数字我们一个字都对不上。
        "asr_options": {"enable_itn": bool(cfg.get("itn", True))},
    }
    req = urllib.request.Request(
        cfg.get("endpoint") or "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
        method="POST")
    timeout = float(cfg.get("timeout_sec", 5))
    # 超时必须短。实测有一轮 1.9 秒的音频（三轮里最短）让接口耗了 15.5 秒，
    # 原因不在音频长度，而是同步单次调用的长尾——这类慢请求重试只会更慢。
    # 所以策略是：宁可早点告诉用户"没听清，再说一遍"，也不让他干等。
    # 只对"连接被掐断/连不上"重试：那种失败通常几十毫秒就返回，重试不占用户时间。
    last_err = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _extract_text(json.loads(resp.read().decode("utf-8")))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:300]
            last_err = RuntimeError("识别接口返回 %d：%s" % (e.code, raw))
            if 400 <= e.code < 500:
                raise last_err          # 请求本身有问题，重试不会变好
        except urllib.error.URLError as e:
            # urlopen 把 socket 超时包在 URLError.reason 里，得拆开看
            if isinstance(e.reason, socket.timeout):
                raise RuntimeError("识别超时（%g 秒），这段音频没能出结果" % timeout)
            last_err = RuntimeError("连不上识别接口：%s" % e)
        except socket.timeout:
            raise RuntimeError("识别超时（%g 秒），这段音频没能出结果" % timeout)
        except (http.client.HTTPException, ConnectionError) as e:
            # 关键：对端收下请求却不给响应就断开，抛的是
            # http.client.RemoteDisconnected，它既不是 HTTPError 也不是 URLError。
            # 以前漏了这一支，重试逻辑形同虚设——断连直接逃到最外层回落。
            last_err = RuntimeError("识别连接被中断：%s" % e)
        if attempt == 1:
            time.sleep(_RETRY_BACKOFF)
    raise last_err


def _extract_text(payload):
    """从返回体里取文本。取不到就返回空串，绝不抛异常。

    为什么容忍空：用户按了说话但没出声，接口会返回空内容——
    这是正常情况（就是没人说话），不是错误。上层会把它当"沉默"处理。
    """
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):      # 少数情况下是分段结构
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict)).strip()
    return ""


def tts(text):
    """把回复文本变成语音。

    两条路：
        dashscope → 服务端合成，返回一个音频 URL，前端用 <audio> 播；
        browser   → 服务端不合成，前端用 speechSynthesis 念（占位/降级）。
    为什么保留降级：合成服务抖动时宁可声音机械一点，也不能突然没声——
    外呼里沉默比音色难听严重得多。

    音色与语速来自 config/scene.json 的 tts 段：换音色属于业务决策，不该改代码。

    返回：dict(mode, text, voice, rate, latency_ms[, audio_url, error])
    """
    cfg = config_loader.tts_config()
    provider = (cfg.get("provider") or "browser").strip()
    t0 = time.time()

    if provider == "dashscope":
        try:
            url = _tts_dashscope(text)
            return {
                "mode": "url",
                "text": text,
                # 这里必须是云端音色 ID。之前错填成浏览器音色名，排查时
                # 会以为"用的是 Xiaoxiao 却发出了 Cherry 的声音"而绕远路。
                "voice": cfg.get("voice_name", ""),
                "rate": float(cfg.get("rate", 1.0)),
                "audio_url": url,
                "provider": provider,
                "latency_ms": round((time.time() - t0) * 1000, 1),
            }
        except Exception as e:
            # 合成失败不掐断通话：退回浏览器播报，并把原因留痕
            return {
                "mode": "browser",
                "text": text,
                "voice": cfg.get("voice", ""),
                "rate": float(cfg.get("rate", 1.0)),
                "provider": "browser",
                "fallback_from": provider,
                "error": str(e)[:200],
                "latency_ms": round((time.time() - t0) * 1000, 1),
            }

    return {
        "mode": "browser",      # 前端据此决定用 speechSynthesis 播报
        "text": text,
        "voice": cfg.get("voice", ""),
        "rate": float(cfg.get("rate", 1.0)),
        "provider": "browser",
        "latency_ms": 0.0,      # 没走服务端合成，自然没有服务端耗时
    }


def _tts_dashscope(text):
    """调 qwen3-tts-flash 合成，返回可播放的音频 URL。

    为什么返回 URL 而不是自己下载转 base64：
        这条链路上音频只播一次，转一道手多一次读盘/编码，还让响应体从
        几百字节涨到几百 KB。OSS 链接 24 小时有效，当场播完全够。
    为什么要 language_type：模型是多语种的，不指定时英文单词会被念成中文字母。
    """
    cfg = config_loader.tts_config()
    key = config_loader.secret("dashscope_api_key")
    if not key:
        raise RuntimeError("未配置 dashscope_api_key")
    if not (text or "").strip():
        raise RuntimeError("空文本不合成")

    body = {
        "model": cfg.get("model") or "qwen3-tts-flash",
        "input": {
            "text": text,
            "voice": cfg.get("voice_name") or "Cherry",
            "language_type": cfg.get("language_type") or "Chinese",
        },
    }
    req = urllib.request.Request(
        cfg.get("endpoint") or
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
        method="POST")
    timeout = float(cfg.get("timeout_sec", 10))
    # 合成比识别更该重试：它是幂等的、纯函数式调用，重试不会产生副作用，
    # 而失败的表现是"客服突然不说话了"，比多等一会儿难看得多。
    last_err = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            url = ((payload.get("output") or {}).get("audio") or {}).get("url")
            if not url:
                raise RuntimeError("返回体里没有音频链接：%s"
                                   % json.dumps(payload, ensure_ascii=False)[:200])
            # OSS 默认给 http://，而我们是 https 页面。
            # 不换成 https 的话，浏览器会以"混合内容"为由直接拦掉播放，
            # 表现是"合成成功但一点声音都没有"，极难查。
            if url.startswith("http://"):
                url = "https://" + url[len("http://"):]
            return url
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:200]
            last_err = RuntimeError("合成接口返回 %d：%s" % (e.code, raw))
            if 400 <= e.code < 500:
                raise last_err
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                raise RuntimeError("合成超时（%g 秒）" % timeout)
            last_err = RuntimeError("连不上合成接口：%s" % e)
        except socket.timeout:
            raise RuntimeError("合成超时（%g 秒）" % timeout)
        except (http.client.HTTPException, ConnectionError) as e:
            # 实测真的撞上过：对端收下请求后直接断连，抛 RemoteDisconnected。
            # 它不是 HTTPError 也不是 URLError，漏掉这一支重试就白写了。
            last_err = RuntimeError("合成连接被中断：%s" % e)
        if attempt == 1:
            time.sleep(_RETRY_BACKOFF)
    raise last_err
