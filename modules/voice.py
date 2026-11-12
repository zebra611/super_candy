"""M2 实时语音网关 —— 采集、识别、合成、端点检测都是真的。

职责（真实形态）：
    音频帧进来 → VAD 判断端点 → 流式 ASR 出文本；文本出去 → 流式 TTS 出音频；
    用户中途开口要能立刻掐断播报（barge-in）。

当前状态（每一条都经实测，不是照设计写的）：
    asr()        真识别，走 qwen3-asr-flash 的 HTTP 接口（整段上传，非流式）；
                 送识别前过 VAD（见 vad_detect），噪声不当人话送模型；
    tts()        真合成，走 qwen3-tts-flash，返回 OSS 音频链接给前端播；
                 合成失败回落到浏览器播报，不允许静默；
    vad_detect() Z6 已换掉"有字节就算说完"：逐帧判定 + 起止聚合，
                 引擎优先 Silero（装了 onnxruntime + 模型），否则纯标准库谱特征；
                 12 条真人声 / 9 类噪声 / 3 条混合语料上实测见 scripts/bench_vad.py；
    barge_in     前端三段式（疑似→识别→确认）已落地，服务端只记账。

负责人：角色 1（实时语音与终端）

S2 已落地（2026-11-05，本文件保留批式链路作为兜底）：
    - WS 双向通道见 ws_channel.py 与 /ws/call；音频帧不再走 HTTP 分片；
    - TTS 分段"就绪即推"替代前端轮询 /api/tts/rest（见 ws_channel._push_rest）；
    - 流式识别适配层 modules/realtime_asr.py（paraformer-realtime-v2），
      连通已实测通过（scripts/check_realtime_asr.py），默认由配置决定。

后续待补充（按优先级）：
    1. 说话人分离（环境里别人说话会被当成用户）——VAD 只能判"有没有人声"，
       判不出"是不是本人在说"，这一层现在没有；
    2. 真流式 TTS（现在是分段合成，不是首包流式）。
"""

import base64
import http.client
import io
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import wave

import config_loader

# 重试间隔（秒）。不要改成 0：对端刚掐断连接时立刻重连，大概率撞上同一个问题，
# 0.15 秒足够让对端把旧连接清掉，又短到不影响首字响应。
_RETRY_BACKOFF = 0.15

# 被限流（429）后的等待。比普通重试间隔长得多：配额恢复需要时间，
# 0.15 秒立刻重连基本还会撞上 429。
_THROTTLE_WAIT = 0.8

# 识别请求的两轮时限，每轮是 (socket 超时, 墙钟总上限)，单位秒。
# 为什么第一轮只给 2 秒：正常请求只要 400ms，有 5 倍余量；超了基本可判定撞上
# 云端长尾（实测同一段 2 秒音频，正常 398ms，最坏 5404ms，差 13 倍）。
# 提成模块级常量而不是写在函数里，是为了让自检能注入更短的值——
# 否则测一次闸门要真等 5 秒。
_ASR_TIMEOUT_PLAN = ((2.0, 2.0), (3.0, 3.0))


def _post_with_deadline(req, socket_timeout, deadline_sec):
    """发 POST 并强制"墙钟"总时长上限，超时抛 socket.timeout。

    为什么单靠 urllib 的 timeout 不够（这是实测踩出来的）：
        它的 timeout 只作用于**单次 socket 操作**。一次请求要走
        DNS → 建连 → TLS 握手 → 发送 → 等响应 → 读数据，每一步都没超时，
        加起来却可以远超那个值。实测出现过 timeout 设为 5 秒、
        实际却耗了 5.4 秒的情况——等意识到时用户已经干等完了。

        所以这里另起一个线程跑请求，主线程只等 deadline_sec。
        到点就放弃，不等它。工作线程是 daemon，socket 超时会让它自己收尾，
        不会拖住进程。
    """
    box = {}

    def _work():
        try:
            with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
                box["body"] = resp.read()
        except BaseException as e:      # 线程里的异常必须兜住，否则会静默丢失
            box["err"] = e

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(deadline_sec)
    if th.is_alive():
        raise socket.timeout("墙钟超过 %.1f 秒未返回" % deadline_sec)
    if "err" in box:
        raise box["err"]
    return box["body"]

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

    两层结构（细节在 modules/vad_gate.py）：
        前端  `web/index.html` 逐帧（20ms）判定 + 静音 700ms 判"说完了"，
              它决定了"什么时候提交"，必须零延迟，所以在浏览器里做；
        服务端 本函数。它不参与节奏，负责两件事：
              ① 给事件链留一条可信的 `vad_result`（谁判的、判成什么）；
              ② 决定这段音频要不要送识别——噪声送进 ASR 会吐乱码，
                 乱码进核身失败计数会把在线用户挂断（C6-09 的真实事故）。

    为什么不能只判"有没有声音"（改造前的实现就是这样）：
        有字节就 started=True，等于没判。演示现场风扇、键盘、椅子都会触发，
        识别层再拿噪声去识别，整条链路看起来"在工作"，其实在制造错误。

    返回：dict(started, ended, speech_ms, samples, sample_rate,
               engine, engine_note, speech_ratio, frames)
        engine 如实标注是 silero 还是 spectral —— 降级了就要在事件里看得见。
    """
    from modules import vad_gate
    return vad_gate.analyze(audio_bytes, sample_rate=sample_rate)


def realtime_enabled():
    """是否启用流式识别（S2 的 partial 字幕来源）。

    默认关，配置 config/scene.json 的 asr.realtime=true 才开。为什么这么保守：
        流式链路用 paraformer-realtime-v2，计费与调用方式都和现在的
        qwen3-asr-flash 不同；没验证过密钥与网络的环境里贸然开启，
        风险不是"慢一点"而是"整通识别没了"。关着时 WS 通话自动走
        批式兜底（行为与 S1 完全一致），开了才有半句字幕与流式 final。
    """
    return bool(config_loader.asr_config().get("realtime", False))


_last_realtime_error = ""


def last_realtime_error():
    """最近一次流式识别建连失败的原因。空字符串表示没失败过。

    为什么要暴露出来：建连失败只打一行控制台，前端与 verify 都看不见，
    结果就是"配置开了却没字幕"，排查时只能猜。现在它进了 ready 消息，
    演示现场一眼能看出是没密钥、模型没开通、还是公司网拦了 WSS。
    """
    return _last_realtime_error


def open_realtime_asr():
    """建一条实时识别连接。失败返回 None（回落批式），不抛——
    调用方在 WS 建连路径上，抛异常会把整条通话通道带崩。"""
    global _last_realtime_error
    from modules import realtime_asr
    try:
        inst = realtime_asr.RealtimeAsr.connect()
        _last_realtime_error = ""
        return inst
    except Exception as exc:
        # 失败原因要能被看到：塞进事件流做不到（这里没有 session_id），
        # 至少打到控制台 + 记进 last_realtime_error（ready 消息会带出去），
        # 别让人以为"开了 realtime 没效果"是代码没生效。
        _last_realtime_error = str(exc)[:200]
        print("  [realtime-asr] 流式识别建连失败，回落批式：%s" % _last_realtime_error[:160])
        return None


def asr(audio_bytes, hints=None, vad=None):
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
        vad          已经算好的 vad_detect() 结果。传了就用它判"这段有没有人声"，
                     不再另算一次——调用方（app.handle_turn）本来就要写
                     vad_result 事件，同一段音频算两遍纯属浪费。
                     不传时退回到老的整段 rms 闸门（行为与改造前一致）。
    返回：dict(text, confidence, latency_ms, fake, provider)
         出错时额外带 error 字段，且 fake=True——失败结果不能被算成真实识别
    """
    provider = config_loader.asr_provider()
    if provider == "placeholder":
        return _asr_placeholder()

    t0 = time.time()
    level = pcm_rms(audio_bytes)
    threshold = float(config_loader.asr_config().get("silence_rms", 0.003))
    if vad is not None:
        # VAD 判"没有人声"才是真的没人说话；rms 只当兜底（VAD 引擎不可用时才会没有 vad）。
        # 这一条把风扇、键盘、椅子这类"有能量但没人说话"的片段挡在识别层之外——
        # 送进去只会换来一句幻觉文本，然后污染核身失败计数。
        no_speech = not vad.get("started")
        skip_reason = "vad_no_speech"
    else:
        no_speech = level < threshold
        skip_reason = "silent"
    if no_speech:
        # 明显是静音：不发请求，直接当"没人说话"。
        # 省一次调用是顺带的，主要是避免模型对静音幻觉出一句"嗯。"
        # （实测确实会），那会让状态机以为用户开了口。
        return {
            "text": "",
            "confidence": None,
            "latency_ms": 0.0,
            "fake": False,
            "provider": provider,
            "rms": round(level, 5),
            "skipped": skip_reason,
            "vad_engine": (vad or {}).get("engine", "rms"),
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
    # 实测长尾：两段几乎一样长的音频（2080ms 与 2040ms），一个 398ms 就回来，
    # 另一个耗了 5404ms——差 13 倍，与音频长度无关，是云端排队或抖动。
    # 对策不是"把超时调大让它等"，而是：短时限 → 超了就重试 → 都超才降级。
    plan = _ASR_TIMEOUT_PLAN
    last_err = None
    for attempt, (sock_to, deadline) in enumerate(plan, 1):
        try:
            raw = _post_with_deadline(req, sock_to, deadline)
            return _extract_text(json.loads(raw.decode("utf-8")))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:300]
            last_err = _friendly_http_error(e.code, raw, "识别")
            if e.code == 429:
                time.sleep(_THROTTLE_WAIT)   # 限流是瞬时的，等一下再试
            elif 400 <= e.code < 500:
                raise last_err                # 其他 4xx 是请求本身有问题，重试没用
        except urllib.error.URLError as e:
            # urlopen 把 socket 超时包在 URLError.reason 里，得拆开看
            if isinstance(e.reason, socket.timeout):
                last_err = RuntimeError("识别超时（%.0f 秒）" % deadline)
            else:
                last_err = RuntimeError("连不上识别接口：%s" % e)
        except socket.timeout:
            last_err = RuntimeError("识别超时（%.0f 秒）" % deadline)
        except (http.client.HTTPException, ConnectionError) as e:
            # 对端收下请求却不给响应就断开，抛的是 http.client.RemoteDisconnected，
            # 它既不是 HTTPError 也不是 URLError。漏了这一支，重试就是摆设。
            last_err = RuntimeError("识别连接被中断：%s" % e)
        if attempt < len(plan):
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


def _split_for_tts(text, single_max=16, head_max=12, seg_max=48, max_segs=12):
    """把回复切段，让第一段尽量短、且**每一段都在云端超时预算内**。

    为什么要切（实测数据）：
        合成耗时随字数近似线性——3 字 344ms、14 字 694ms、24 字 1056ms、
        30 字 1684ms。整句合成意味着"第一个字要等最后一个字念完"。

    切在哪：
        优先在逗号/句号后切；**英文按词边界（空格）切**，不能把 "application"
        切成 "applica|tion"——中文没有空格，标点就是唯一可切点，英文则必须
        先找空格。第一段不少于 3 字（"好，"这种一字段没意义）。

    段数与上限（11-12 20:37 实测后改）：
        英文长句 415 字被切成 `[229, 48, 4, 48, 44, 42]`——**首段 229 字**，
        因为前 229 字里一个标点都没有（英文逗号在 229 字之后），而旧逻辑
        "找不到符合 head_max 的标点就取第一个标点"直接把它当首段：
        首字 3507ms，整轮 8.5 秒。两个毛病一起修：
          ① 首段找不到近处的标点时，拉丁文回退到最近空格，仍找不到才硬切；
          ② 尾段留下 `<4` 字的碎段（实测出现过一个只有 4 字的请求）
             合并回上一段，白扔一次请求；
          ③ 段数上限 `max_segs`：切太碎会被接口按请求数限流（429），
             超限时把最短的相邻两段合并到均衡，而不是任由它切出十几段。

    拼接无损：`"".join(parts) == text`，verify.py 有断言盯着这一条。
    """
    text = (text or "").strip()
    if len(text) <= single_max:
        return [text]

    punct = "，。！？；,.!?;"
    is_latin = sum(1 for ch in text if ord(ch) < 128) > len(text) * 0.5

    def _cut_at(seg, limit, floor):
        """在 seg[:limit] 里找一个 >=floor 的切点（返回切后长度，找不到返回 0）。"""
        best = 0
        for i, ch in enumerate(seg[:limit], 1):
            if ch in punct or (is_latin and ch == " "):
                if i >= floor:
                    best = i
        return best

    head_end = 0
    for i, ch in enumerate(text[:head_max], 1):
        if (ch in punct or (is_latin and ch == " ")) and 3 <= i <= head_max:
            head_end = i
            break
    if not head_end:
        # 前面 head_max 字里没有任何可切点：拉丁文先在更宽一点的范围找空格
        head_end = _cut_at(text, head_max, 3) or head_max

    head, tail = text[:head_end], text[head_end:]
    if not tail:
        return [head]

    parts = [head]
    while len(tail) > seg_max:
        cut = _cut_at(tail, seg_max, seg_max // 2) or seg_max
        parts.append(tail[:cut])
        tail = tail[cut:]
    if tail:
        # 碎段合并：只剩十来个字（"resubmit it."）时别单独发一次请求——
        # 一个请求 0.5 秒起，为 8 个字开一次接口不划算，也更容易撞限流。
        if parts and len(tail) <= 10 and len(parts) > 1:
            parts[-1] = parts[-1] + tail
        else:
            parts.append(tail)

    # 段数上限：请求数是限流口径，切太碎会 429。超限就把最短的相邻两段合并。
    # **绝不合并首段**：首段就是首字延迟本身，第一版没排除它，实测把
    # 5 字的 "Your " 和后面 41 字并成了 46 字，首字又回到 900ms+。
    while len(parts) > max_segs and len(parts) > 2:
        i = min(range(1, len(parts) - 1),
                key=lambda k: len(parts[k]) + len(parts[k + 1]))
        parts[i:i + 2] = [parts[i] + parts[i + 1]]
    return parts


def _tts_dashscope_multi(text, wait_first_only=False):
    """把文本切段后**串行**合成，第一段合成完就可以返回。

    为什么是串行，不是并发（这是踩过坑之后改的）：
        一开始写成并发，结果把接口的 QPS 配额打爆了——
        `Throttling.RateQuota`，429。一轮回复本来只要 1 个请求，
        切成 3 段就变成 3 个，而限流是按请求数算的。
        更糟的是 429 属于 4xx，会被判成"请求有问题不重试"，直接回落成浏览器机械音。

        串行其实不亏：第一段本来就排在最前面，合成完立刻返回，
        首字依然是几百毫秒；剩下的段在后台一段接一段跑，
        用户还在听第一段时它们就陆续好了。

    返回 (首段列表, 首段就绪耗时ms, 剩余句柄)。
    """
    parts = _split_for_tts(text)
    if len(parts) <= 1:
        return [{"text": parts[0], "audio_url": _tts_dashscope(parts[0])}], None, None

    t0 = time.time()
    urls = [None] * len(parts)
    errors = []

    urls[0] = _tts_dashscope(parts[0])          # 首段必等
    head_ms = round((time.time() - t0) * 1000, 1)
    head = [{"text": parts[0], "audio_url": urls[0]}]

    def _rest_worker():
        for i in range(1, len(parts)):
            try:
                urls[i] = _tts_dashscope(parts[i])
            except Exception as e:              # 线程里必须兜住
                errors.append((i, e))

    if wait_first_only:
        rest_thread = threading.Thread(target=_rest_worker, daemon=True)
        rest_thread.start()
        return head, head_ms, ([rest_thread], urls, parts)

    _rest_worker()
    if errors:
        raise errors[0][1]
    return ([{"text": p, "audio_url": u} for p, u in zip(parts, urls)],
            head_ms, ([], urls, parts))


def collect_rest(handle, timeout=3.0):
    """取出剩余段（前端播完首段后调）。等不到就跳过，不让用户卡住。"""
    if not handle:
        return []
    threads, urls, parts = handle
    for th in threads:
        th.join(timeout)
    return [{"text": p, "audio_url": u}
            for p, u in zip(parts[1:], urls[1:]) if u]


def tts(text, wait_first_only=False):
    """把回复文本变成语音。

    两条路：
        dashscope → 服务端合成，返回音频 URL（单段或分段），前端用 <audio> 播；
        browser   → 服务端不合成，前端用 speechSynthesis 念（占位/降级）。
    为什么保留降级：合成服务抖动时宁可声音机械一点，也不能突然没声——
    外呼里沉默比音色难听严重得多。

    wait_first_only=True 时只合成并返回第一段，剩余段落后台继续，
    由 app 层暂存、前端播完首段后再来取（见 collect_rest）。

    音色与语速来自 config/scene.json 的 tts 段：换音色属于业务决策，不该改代码。

    返回：dict(mode, text, voice, rate, latency_ms[, audio_url | segments, _rest])
    """
    cfg = config_loader.tts_config()
    provider = (cfg.get("provider") or "browser").strip()
    t0 = time.time()

    if provider == "dashscope":
        try:
            segs, first_ms, handle = _tts_dashscope_multi(text, wait_first_only)
            common = {
                "text": text,
                # 这里必须是云端音色 ID。之前错填成浏览器音色名，排查时
                # 会以为"用的是 Xiaoxiao 却发出了 Cherry 的声音"而绕远路。
                "voice": cfg.get("voice_name", ""),
                "rate": float(cfg.get("rate", 1.0)),
                "provider": provider,
            }
            if len(segs) == 1 and handle is None:
                return dict(common, mode="url", audio_url=segs[0]["audio_url"],
                            latency_ms=round((time.time() - t0) * 1000, 1))

            out = dict(common, mode="segments", segments=segs,
                       latency_ms=first_ms or round((time.time() - t0) * 1000, 1))
            if handle is not None:
                out["rest_count"] = len(handle[2]) - 1
                out["_rest"] = handle       # 下划线开头＝内部字段，app 取走后不下发
            if not wait_first_only:
                out["total_ms"] = round((time.time() - t0) * 1000, 1)
            return out
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


def _friendly_http_error(code, raw, what):
    """把接口返回的错误翻成"看了就知道该干什么"的话。

    400 不都是"请求写错了"。实测撞过两种完全不同的 400：
        - 账号欠费（Arrearage / overdue-payment）
        - 参数不合法
    只报"返回 400"会让人去翻代码，而真正该做的是去充值。
    演示前最可能撞的就是欠费，所以这一支必须单独说清楚。

    what：识别 / 合成
    """
    low = raw.lower()
    if code == 400 and ("arrearage" in low or "overdue" in low
                        or "good standing" in low):
        return RuntimeError(
            "%s不可用：阿里云账户欠费或免费额度已用尽。"
            "去百炼控制台充值后自动恢复，代码不用改。原始返回：%s"
            % (what, raw[:120]))
    if code in (401, 403):
        return RuntimeError(
            "%s鉴权失败（%d）：API Key 无效、被禁用或权限不足。"
            "检查 config/secrets.json 里的 dashscope_api_key。原始返回：%s"
            % (what, code, raw[:120]))
    return RuntimeError("%s接口返回 %d：%s" % (what, code, raw))


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

    # 语言跟着文本走：配置里是 Chinese，英文回复若仍按中文合成，
    # 会把英文单词念成中文字母（11-12 英文互切后发现）。ASCII 占多数即按英文。
    _lang = cfg.get("language_type") or "Chinese"
    _ascii = sum(1 for ch in (text or "") if ord(ch) < 128)
    _cjk = sum(1 for ch in (text or "") if "\u4e00" <= ch <= "\u9fff")
    if _ascii > _cjk * 3:
        _lang = "English"
    body = {
        "model": cfg.get("model") or "qwen3-tts-flash",
        "input": {
            "text": text,
            "voice": cfg.get("voice_name") or "Cherry",
            "language_type": _lang,
        },
    }
    req = urllib.request.Request(
        cfg.get("endpoint") or
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
        method="POST")
    timeout = float(cfg.get("timeout_sec", 4))
    # 合成比识别更该重试：它是幂等的、纯函数式调用，重试不会产生副作用，
    # 而失败的表现是"客服突然不说话了"，比多等一会儿难看得多。
    # 同样加墙钟闸门：socket timeout 约束不了总时长（见 _post_with_deadline）。
    last_err = None
    for attempt in (1, 2):
        try:
            raw = _post_with_deadline(req, timeout, timeout + 0.5)
            payload = json.loads(raw.decode("utf-8"))
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
            last_err = _friendly_http_error(e.code, raw, "合成")
            if e.code == 429:
                # 实测被限流过：分段合成把 QPS 配额打爆，接口直接 429。
                # 它属于 4xx 但和"请求写错了"完全不同——等一会儿就能过。
                time.sleep(_THROTTLE_WAIT)
            elif 400 <= e.code < 500:
                raise last_err
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                last_err = RuntimeError("合成超时（%g 秒）" % timeout)
            else:
                last_err = RuntimeError("连不上合成接口：%s" % e)
        except socket.timeout:
            last_err = RuntimeError("合成超时（%g 秒）" % timeout)
        except (http.client.HTTPException, ConnectionError) as e:
            # 实测真的撞上过：对端收下请求后直接断连，抛 RemoteDisconnected。
            # 它不是 HTTPError 也不是 URLError，漏掉这一支重试就白写了。
            last_err = RuntimeError("合成连接被中断：%s" % e)
        if attempt == 1:
            time.sleep(_RETRY_BACKOFF)
    raise last_err
