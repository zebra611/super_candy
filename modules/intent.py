"""意图识别层（T5）——规则版 + 可选的模型裁决。

**为什么要单独一个模块，而不是直接改 dialogue.classify**
    `dialogue.classify` 是"规则版"，40 多个测试与评测集都钉在它的具体取值上
    （含糊判 unclear、弱肯定 conf 0.50、尾号取最后一个……）。把模型塞进那个函数，
    等于让一次网络波动改掉这些取值，测试会变成"时灵时不灵"。
    所以这里做的是**外挂一层**：

        规则版先给出完整 flags  →  模型只在"高置信且标签合法"时改写主意图
                                →  任何异常都原样返回规则版结果

    默认 `provider=template`（不联网），行为与接入前逐字节一致。

**模型能改什么、不能改什么**
    能改：no / yes / ask / angry / complain / unclear 六个标签，以及它们对应的布尔位。
    不能改（一律以规则为准）：
        deny / hangup / handoff —— 否定本人、要求挂断、要求转人工都是**不可逆动作**，
            判错的代价是"把本人当陌生人挂掉"或"该转人工没转"。
            这三类交给人保底，不让模型碰。
        digits —— 尾号是身份证据，只认"孤立 4 位数字"这条硬规则。
    	  模型把"12345678"读成尾号 5678，就是把客户核验到别人账上。

**实测口径**
    `scripts/intent_bench.py` 用 25 条口语化改写样本对比两版：
        规则版  —— 关键词 + 位置规则
        模型版  —— 同一条样本交给 qwen-flash 判标签
    T5 的验收不是"模型更高"，而是"不低"：口径见 docs/24 与 PR 描述。
    没有 API Key 时 benchmark 只报规则版，并明确标注"模型未启用"，
    不允许把"没跑"写成"跑过了"。
"""

import json
import urllib.error
import urllib.request

import config_loader

_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_DEFAULT_MODEL = "qwen-flash"
_DEFAULT_TIMEOUT_MS = 800

# 模型被允许改写的标签，以及每个标签要置位的 flags 字段。
# 不在这张表里的标签一律忽略——模型说"handoff"也不会真的转人工。
_EDITABLE = {
    "yes": ("yes",),
    "no": ("no",),
    "ask": ("ask",),
    "angry": ("angry",),
    "complain": ("complain",),
    "unclear": ("vague",),
}

# 低于这个置信度就不采信模型：宁可留着规则版那条，也不要一个半信半疑的判断。
_MIN_MODEL_CONF = 0.70

_SYSTEM = (
    "你是外呼客服对话的意图标注器。给一句用户话，判断它属于哪一类。\n"
    "类别只能取以下之一：\n"
    "  yes      明确同意（同意、可以、办吧）\n"
    "  no       明确拒绝（不需要、算了、别打了）\n"
    "  ask      在提一个具体问题\n"
    "  angry    骂人或威胁投诉\n"
    "  complain 抱怨、泄气（说啥也没用、问这有啥）\n"
    "  unclear  含糊、敷衍、只有语气词（嗯、随便、你定吧）\n"
    "  info     报出数字信息（尾号、单号）\n"
    "  handoff  要求转人工\n"
    "  other    以上都不是\n"
    "只输出一行 JSON：{\"intent\": \"...\", \"conf\": 0.0~1.0}，不要解释。"
)

_warned = set()


def _warn_once(tag, msg):
    if tag in _warned:
        return
    _warned.add(tag)
    print("[intent] %s" % msg)


def _config():
    """读 `intent` 段配置；没有这一段就返回空 dict（走规则版）。"""
    try:
        scene = config_loader.scene()
        return (scene or {}).get("intent") or {}
    except Exception:
        return {}


def classify(text):
    """统一入口：返回与 `dialogue.classify` 同形状的 flags。

    保证：**永远返回一个可用的 dict**。模型超时、返回乱码、Key 缺失，
    都退回规则版结果，调用方不需要写 try。
    """
    from modules import dialogue          # 延迟导入，避免模块级循环依赖

    base = dialogue.classify(text)

    cfg = _config()
    if (cfg.get("provider") or "template").strip() != "dashscope":
        return base

    picked = _ask_model(text, cfg)
    if not picked:
        return base
    return _merge(base, picked)


def _ask_model(text, cfg):
    """调模型拿一个 (label, conf)；任何异常返回 None。这里绝不 raise。

    注意 try 的范围：**密钥读取、请求体构造、Request 构造**都在保护范围内。
    以前只包了 urlopen 那一段，`config_loader.secret` 读文件失败（secrets.json
    损坏/权限问题）会把 OSError 直接漏到 dialogue.respond，整轮通话崩掉，
    与模块顶部「调用方不需要写 try」的契约不符（S1 探针 D7 实测）。
    """
    try:
        key = config_loader.secret("dashscope_api_key")
    except Exception as exc:
        _warn_once("secret_fail", "密钥读取失败，意图识别保持规则版：%s" % str(exc)[:80])
        return None
    if not key:
        _warn_once("no_key", "未配置 dashscope_api_key，意图识别保持规则版")
        return None

    try:
        body = {
            "model": cfg.get("model") or _DEFAULT_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "用户：%s" % (text or "")},
            ],
            "temperature": 0.0,
            "max_tokens": 40,
            "stream": False,
        }
        req = urllib.request.Request(
            cfg.get("endpoint") or _ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            method="POST")
        timeout_ms = _as_int(cfg.get("timeout_ms"), _DEFAULT_TIMEOUT_MS)
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000.0) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        raw = payload["choices"][0]["message"]["content"] or ""
    except Exception as exc:                  # 超时 / 4xx / 5xx / 解析失败 / 构造失败
        _warn_once("call_fail", "意图模型调用失败，退回规则版：%s" % str(exc)[:80])
        return None

    picked = _parse(raw)
    if not picked:
        _warn_once("bad_out", "意图模型输出无法解析，退回规则版")
    return picked


def _parse(raw):
    """从模型输出里抠出 (label, conf)。认不出来返回 None。"""
    text = (raw or "").strip()
    if "{" in text and "}" in text:            # 容忍前后多一句解释
        text = text[text.find("{"):text.rfind("}") + 1]
    try:
        data = json.loads(text)
    except Exception:
        return None
    label = str(data.get("intent") or "").strip().lower()
    try:
        conf = float(data.get("conf"))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= conf <= 1.0):
        return None
    return label, conf


def _merge(base, picked):
    """把模型的判断合进规则版结果。不合法、不自信、不许改的，一律保留规则版。"""
    label, conf = picked
    if label not in _EDITABLE:
        return base
    if conf < _MIN_MODEL_CONF:
        return base

    out = dict(base)
    # 先把六个可改位清零，再按模型结论置位——否则会出现
    # "模型说 no、规则版的 yes 还留着"，最终按优先级仍判成 yes。
    for fields in _EDITABLE.values():
        for f in fields:
            out[f] = False

    for f in _EDITABLE[label]:
        out[f] = True

    out["conf"] = max(float(base.get("conf") or 0.0), conf)
    out["evidence"] = "model"
    out["model_intent"] = label          # 留痕：事后能看出这一轮是谁判的
    return out


def _as_int(value, default):
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return num if num > 0 else default
