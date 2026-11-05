"""M3 的措辞层 —— 把骨架话术改写成自然口语。

负责人：2A 佟欣。完整契约见 docs/17_任务2接口契约.md 第 3 节。

**边界（别越界）**
    这个模块只决定「同一件事怎么说」，不决定「说哪件事」。
    走哪个环节、能不能推进、该不该收尾，全是 dialogue.py 里状态机的事。
    一旦这里开始判断"要不要核验身份"，就会出现模型自由发挥、
    跳过环节、每次说法还不一样——而且不可复现、查不出来。

**为什么是「改写」而不是「自由生成」**
    状态机先用一句现成骨架话术承载意图（"为了核对身份，请说一下手机号后四位"），
    这里只把它说得自然些。好处：
        - 业务正确性不依赖模型：模型抽风最多话说得别扭，不会漏环节；
        - 合规护栏仍然有效：禁语检查在 app.py 对最终文本做，换措辞也跑得掉；
        - 降级零成本：出错直接返回骨架，行为退回今天的样子；
        - 可回归：关掉改写，verify 的 73 条断言必须全过——这是唯一标准。

**当前状态（2026-11-05 实现）**
    `_rewrite()` 已按 docs/17 第 3.4 节实现，六种降级路径全在，全部返回骨架话术、不抛异常。
    `provider=template`（默认）时行为与接入前完全一致：verify 73/73、eval 30/30。
    离线自测见 `scripts/test_nlg.py`（14 条，不联网）。

**已知限制：骨架里有中文期限的环节，改写大概率被拦**
    实测 OFFER 环节骨架"有效期十二个月"被模型改成"有效期一年"，随后被
    `_facts_ok()` 按"金额/期限标志词"规则拦下（多出「年」），这一轮退回骨架。
    这是**刻意保守**，不是 bug：意思虽然对，但"十二个月"和"一年"是两种业务承诺
    的表述方式，改写层没有资格替业务改口径。代价是 OFFER 这一环的自然化收益接近 0。
    要提高命中率，应该改骨架话术本身（例如骨架就写"一年"），而不是放宽规则——
    放宽规则等于把编造业务事实的口子重新打开。

**实测：model 不能配 qwen-turbo**
    本账号对 `qwen-turbo` 持续返回 429 `limit_requests`（不是偶发限流，是没额度）。
    实测可用且最快的是 **`qwen-flash`**：283–553ms，5 个环节改写全部通过校验。
    `qwen-plus` 也可用但 734–748ms，超出 600ms 预算，不建议。
    `qwen3-flash` / `qwen3-max` / `qwen2.5-turbo` 在本账号是 404 model_not_found。
    → 所以 `config/scene.json` 的 `nlg.model` 必须改成 `qwen-flash`，
      `timeout_ms` 从 400 提到 600，否则开关打开后每次都会 429 降级，等于没开。
      （这是 4 号的地盘，走需求单，别在这里改配置。）

**佟欣要做的**
    1. ~~实现 _rewrite()~~ 已完成；
    2. 等 4 号改完 `nlg.model=qwen-flash` / `timeout_ms=600` 后，开开关跑
       eval（不低于 28/30）与 bench_voice（服务端 P50 记录在案）；
    3. 把 docs/17 第 3.5 节的验收表逐项过一遍。

**一个必须遵守的数**
    LLM 这一步的时间预算是 **400ms 硬上限**，不是参考值。
    但实测 `qwen-flash` 的 P50 在 400–460ms 之间，400ms 会拦掉约一半调用。
    建议按 600ms 配：服务端合计约 1.55s，真实首字约 1.75s，仍在 2000ms 门槛内。
"""

import json
import re
import sys
import urllib.error
import urllib.request

import config_loader

# 与 ASR/TTS 共用同一个 endpoint 与同一把 key（docs/15：一套 key 覆盖识别、合成、大模型）。
_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_DEFAULT_MODEL = "qwen-flash"     # 实测本账号 qwen-turbo 持续 429，见文件头
_DEFAULT_TIMEOUT_MS = 600         # 实测 qwen-flash P50 283-553ms，400ms 会拦掉一半
_DEFAULT_MAX_CHARS = 45

# system prompt 按 docs/17 第 3.3 节。第 2 条是防编造业务事实的主要护栏，
# 它不能替代 app.py 的合规检查，但能大幅降低"整句被替换"的概率。
_SYSTEM = (
    "你是京东客服外呼坐席，正在给一位客户打电话。\n"
    "规则：\n"
    "1. 只把给定的话术改写得更自然口语，不得改变它的意思、不得增加新的事实。\n"
    "2. 不得承诺、不得提及任何具体金额、日期、政策——这些只能来自给定的业务数据。\n"
    "3. 一次只说一件事，不超过 45 个字，不要用书面语和括号。\n"
    "4. 不要重复用户刚说过的话。\n"
    "5. 客户带情绪时，先简短致意再进入正题。\n"
    "6. 只输出改写后的那一句话，不要解释、不要加引号、不要分条。"
)

# 这些"具体事实"标志词，骨架里没有就别让模型说出来。
_FACT_MARKS = ("元", "块", "折", "%", "％", "月", "日", "号", "年", "小时", "天")

_warned = set()


def _warn_once(tag, msg):
    """同一种降级原因只提示一次，避免每轮刷屏（外呼一轮一条会淹掉日志）。"""
    if tag in _warned:
        return
    _warned.add(tag)
    sys.stderr.write("[nlg] %s\n" % msg)


def say(session, base_text, stage=""):
    """把骨架话术改写成自然口语。

    参数：
        session    对话状态（**只读**，禁止修改；slots 里的业务事实唯一来源）
        base_text  骨架话术，来自 config/scene.json 对应环节
        stage      当前环节名，用于 prompt

    返回：str
        改写后的话术。**任何异常都必须返回 base_text 本身**，
        绝不抛异常、绝不返回空串——外呼里突然没声比话说得难听严重得多。
    """
    if not base_text:
        return base_text or ""

    cfg = config_loader.nlg_config()
    if (cfg.get("provider") or "template").strip() != "dashscope":
        return base_text                       # 默认路径：原样返回，行为不变

    rewritten = _rewrite(session, base_text, stage, cfg)
    return rewritten if rewritten else base_text


def _rewrite(session, base_text, stage, cfg):
    """真正调模型的地方。返回 None 表示"这次不改写"，上层会退回 base_text。

    返回 None 的六种情况（按 docs/17 第 3.4 节，一个都不能漏）：
        1. 没有 API Key
        2. 请求超时（> cfg["timeout_ms"]）
        3. 返回空串或只有标点
        4. 返回字数与骨架差异超过 2 倍（模型跑题长篇大论）
        5. 返回里出现了骨架没有的具体数字/金额/日期（编造业务事实）
        6. 其他任何异常

    **这里绝不 raise。** 它在外呼主链路上，抛一次就是整通电话断掉。
    """
    key = config_loader.secret("dashscope_api_key")
    if not key:
        _warn_once("no_key", "未配置 dashscope_api_key，话术改写降级为骨架话术")
        return None

    model = cfg.get("model") or _DEFAULT_MODEL
    timeout_ms = _as_int(cfg.get("timeout_ms"), _DEFAULT_TIMEOUT_MS)
    max_chars = _as_int(cfg.get("max_chars"), _DEFAULT_MAX_CHARS)

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _user_prompt(session, base_text, stage)},
        ],
        "temperature": 0.3,      # 低温度：改写要稳，不要创意
        "max_tokens": 120,       # 45 字中文约 60-70 token，留一倍余量即可
        "stream": False,
    }
    req = urllib.request.Request(
        cfg.get("endpoint") or _ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
        method="POST")

    try:
        # 显式给 timeout，绝不用 urllib 的默认值（默认无超时，网络卡住会把电话挂死）
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000.0) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:                # 6 类里的 2 和 6：超时 / 4xx / 5xx / 解析失败
        _warn_once("call_fail", "改写请求失败，降级为骨架话术：%s" % _short(exc))
        return None

    text = _extract(payload)
    if not _has_word(text):                 # 6 类里的 3：空串或只有标点
        _warn_once("empty", "改写返回为空或只有标点，降级为骨架话术")
        return None

    text = text.strip().strip("\"'“”「」").strip()

    if not _facts_ok(base_text, text):      # 6 类里的 5：编造业务事实
        _warn_once("fact", "改写引入了骨架没有的具体数字/金额/日期，已丢弃")
        return None

    if not _length_ok(base_text, text):     # 6 类里的 4：跑题长篇大论
        _warn_once("drift", "改写字数与骨架相差过大，已丢弃")
        return None

    fitted = _fit(text, max_chars)
    if not fitted:
        _warn_once("fit", "改写超出字数上限且无法完整断句，退回骨架话术")
        return None
    return fitted


# ---------- 以下是实现细节，对外只有 say() 一个入口 ----------

def _as_int(value, default):
    """配置里写成字符串也不能崩（timeout_ms 被人手改成 "400" 是常事）。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return num if num > 0 else default


def _short(exc):
    text = str(exc) or exc.__class__.__name__
    return text[:80]


def _user_prompt(session, base_text, stage):
    """按 docs/17 第 3.3 节组织上下文。

    **只读 session，绝不写。** slots 是业务事实的唯一来源。
    现在 session 里还没有 last_user_text / last_user_angry / recent_turns
    三个字段（要 dialogue.py 加，那是队长的地盘），所以这里全部 .get 兜底：
    字段一加上，这段自动生效，不用再改。
    """
    state = session or {}
    slots = state.get("slots") or {}

    customer = slots.get("customer") or {}
    order = slots.get("order") or {}
    name = customer.get("name") if isinstance(customer, dict) else ""
    title = order.get("title") if isinstance(order, dict) else ""
    if not title and isinstance(order, dict):
        title = order.get("order_id") or ""

    last_user = state.get("last_user_text") or "（未知）"
    mood = "带情绪" if state.get("last_user_angry") else "平静"
    history = state.get("recent_turns") or []
    hist = "；".join(str(h) for h in list(history)[-2:]) or "（无）"

    # 注意：这里不放手机号、不放完整单号——prompt 不进事件库，但也没必要多传。
    return (
        "【当前环节】%s\n"
        "【本轮要表达的意思】%s\n"
        "【已知信息】姓名：%s；订单：%s\n"
        "【客户刚说】%s\n"
        "【客户情绪】%s\n"
        "【最近两轮对话】%s"
        % (stage or "（未标注）", base_text, name or "未知",
           title or "无", last_user, mood, hist)
    )


def _extract(payload):
    """兼容 OpenAI 兼容模式的标准返回形状。取不到就当空串。"""
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _has_word(text):
    """空串、纯标点、纯空白都算"没有内容"。"""
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text or ""))


def _facts_ok(base, text):
    """挡住模型编造业务事实（docs/17 第 3.3 节 system 第 2 条的运行时兜底）。

    两条规则：
        1. 3 位以上的数字（金额、单号、手机号、年份）不得是骨架里没有的；
           只查 3 位以上，是为了不误伤"后四位"这类正常表述。
        2. 金额/期限/日期标志词，骨架里没有就别加——"十二个月"改成"一年"
           虽然意思对，但换了一种业务承诺的表述方式，一律保守拦掉。
    """
    base_nums = set(re.findall(r"\d+(?:\.\d+)?", base))
    for num in re.findall(r"\d+(?:\.\d+)?", text):
        if len(num.replace(".", "")) >= 3 and num not in base_nums:
            return False

    for mark in _FACT_MARKS:
        if mark in text and mark not in base:
            return False
    return True


def _length_ok(base, text):
    """与骨架字数相差不能超过 2 倍（两边都算，含 10 字缓冲防短句误判）。

    短句场景很实在：骨架 12 字，模型答 3 字，也是跑题。
    """
    lb, lt = len(base), len(text)
    return not (lt > lb * 2 + 10 or lt * 2 + 10 < lb)


def _fit(text, max_chars):
    """超过 max_chars 就截断，优先断在标点上。

    为什么要截：TTS 耗时与字数近似线性（实测 3 字 344ms / 30 字 1684ms），
    模型多说 20 个字，TTS 就多花 0.5 秒，把省下的时延又还回去了。

    **说不完就返回 None（退回骨架），不返回半句话。**
    两种必须退的情况：
        - max_chars 内一个标点都没有（截出来是半句，用户听到会以为断线）；
        - 唯一能断的位置太靠前（只剩"好的，"三个字，等于把信息全丢了）。
    外呼里"话说得朴素"是可接受的失败，"话说一半"不是。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    cut = text[:max_chars]
    best = ""
    for sep in ("。", "！", "？", "，", "、", "；"):
        pos = cut.rfind(sep)
        if pos >= 0:
            cand = cut[:pos + 1]
            if len(cand) > len(best):
                best = cand
    if len(best) * 2 < max_chars:
        return None
    return best
