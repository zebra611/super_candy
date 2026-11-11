"""通义千问统一调用层 —— 客服自进化里"会思考的那部分"。

为什么单独一个模块：
    在这次改造之前，全项目只有 `modules/nlg.py` 里一份 `_rewrite()` 在调模型，
    它被写死成"只改写一句话、45 字、600ms"，因为它的位置在外呼主链路上。
    但"把客户数据喂给模型、让模型自己对话"需要另一套完全不同的参数：
    长 prompt、多轮上下文、要 JSON、要能等 5-10 秒。

    两套需求混在一个函数里，最后一定是主链路被拖慢、离线跑批被超时打断。
    所以这里放"能等"的那一套；nlg 继续管"不能等"的那一套，互不影响。

**这里绝不 raise。** 所有失败都落成 `{"ok": False, "error": ...}`。
理由和 nlg 一样：调用方是训练脚本和看板，一次网络抖动不该把整轮训练打断。

三道总闸（都可以用环境变量关掉，方便离线跑测试）：
    LLM_DISABLE=1      全局禁用，chat() 立刻返回 error，调用方走规则回退
    LLM_MAX_CALLS=200  本次进程最多调多少次，超了直接拒（训练跑飞时兜底）
    LLM_MODEL=qwen-plus  覆盖默认模型

用量统计：每次调用都记 模型/耗时/输入输出 token/成功与否，`stats()` 给报告用。
这不是"顺便记一下"——自进化的训练日志要能回答"这一轮到底花了多少次模型调用、
哪几次降级了"，没有这层统计就只能靠猜。
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import config_loader

_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
#: 训练用 plus：比 flash 会"想"，比 max 便宜快。实测 466ms 首字（空载）。
_DEFAULT_MODEL = "qwen-plus"

_LOCK = threading.Lock()
_STATS = {"calls": 0, "ok": 0, "fail": 0, "ms": 0, "prompt_tokens": 0,
          "completion_tokens": 0, "retries": 0, "blocked": 0}
_RECENT = []            # 最近若干次调用摘要，给看板/报告展示"真的调了"
_RECENT_MAX = 200


def _enabled():
    return (os.environ.get("LLM_DISABLE") or "").strip() not in ("1", "true", "yes")


def _max_calls():
    try:
        return max(0, int(os.environ.get("LLM_MAX_CALLS") or 0))
    except ValueError:
        return 0


def _model(default=None):
    return (os.environ.get("LLM_MODEL") or "").strip() or default or _DEFAULT_MODEL


def available():
    """能不能调：有 key、没被禁用、没超预算。调用方据此选"LLM 路径 / 规则回退"。"""
    if not _enabled():
        return False
    if not config_loader.secret("dashscope_api_key"):
        return False
    cap = _max_calls()
    if cap and _STATS["calls"] >= cap:
        return False
    return True


def stats():
    """当前进程的累计用量（深拷贝，调用方改了也不影响内部）。"""
    with _LOCK:
        out = dict(_STATS)
        out["recent"] = list(_RECENT[-20:])
        out["model"] = _model()
    return out


def reset_stats():
    with _LOCK:
        for k in _STATS:
            _STATS[k] = 0
        del _RECENT[:]


def chat(messages, model=None, temperature=0.7, max_tokens=600,
         timeout=30.0, tag="", retries=1):
    """调一次对话。返回 dict，字段稳定：

        ok        bool    是否拿到可用文本
        text      str     模型输出（ok=False 时是空串）
        model     str     实际用的模型
        ms        int     本次耗时（含重试）
        usage     dict    prompt_tokens / completion_tokens（拿不到就是 0）
        retries   int     重试次数
        error     str     失败原因（ok=False 时非空）
    """
    if not _enabled():
        return _fail("disabled", model)
    cap = _max_calls()
    if cap and _STATS["calls"] >= cap:
        with _LOCK:
            _STATS["blocked"] += 1
        return _fail("budget_exhausted(%d)" % cap, model)

    key = config_loader.secret("dashscope_api_key")
    if not key:
        return _fail("no_key", model)

    use_model = _model(model)
    body = {
        "model": use_model,
        "messages": list(messages or []),
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    started = time.time()
    last_err = ""
    tries = 0

    for attempt in range(max(0, int(retries)) + 1):
        tries = attempt
        req = urllib.request.Request(
            _ENDPOINT, data=payload,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            text = _extract(data)
            usage = data.get("usage") or {}
            ms = int((time.time() - started) * 1000)
            if not text:
                last_err = "empty"
                _record(tag, use_model, ms, 0, 0, False, last_err, tries)
                return _fail("empty", use_model, ms, tries)
            _record(tag, use_model, ms, int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0), True, "", tries)
            return {"ok": True, "text": text.strip(), "model": use_model, "ms": ms,
                    "usage": {"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                              "completion_tokens": int(usage.get("completion_tokens") or 0)},
                    "retries": tries, "error": ""}
        except Exception as exc:                       # 超时 / 4xx / 5xx / 解析失败
            last_err = _short(exc)
            # 只有"重试可能有用"的错才重试：限流、5xx、网络。4xx 参数错重试也白搭。
            if attempt < int(retries) and _retryable(exc):
                time.sleep(0.6 * (attempt + 1))
                with _LOCK:
                    _STATS["retries"] += 1
                continue
            break

    ms = int((time.time() - started) * 1000)
    _record(tag, use_model, ms, 0, 0, False, last_err, tries)
    return _fail(last_err, use_model, ms, tries)


def chat_json(messages, schema="{}", model=None, temperature=0.4,
              max_tokens=700, timeout=40.0, tag="", retries=1):
    """要一个 JSON 对象回来。解析失败时**不当场报错**，返回 (None, meta)。

    为什么不用 `response_format={"type":"json_object"}`：
        dashscope 兼容模式对这个参数的支持按模型/版本变，配错的表现是 400，
        而 400 在调用层看起来跟"模型不听话"一样，排查成本高。
        这里改成"prompt 里写清楚 + 代码里抠第一个平衡花括号"，行为可预期。
    """
    msg = list(messages or [])
    hint = ("\n\n只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释。"
            "字段如下：%s" % (schema or "{}"))
    if msg:
        msg[-1] = dict(msg[-1])
        msg[-1]["content"] = (msg[-1].get("content") or "") + hint
    meta = chat(msg, model=model, temperature=temperature, max_tokens=max_tokens,
                timeout=timeout, tag=tag, retries=retries)
    if not meta.get("ok"):
        return None, meta
    obj = extract_json(meta["text"])
    if obj is None:
        meta = dict(meta)
        meta["ok"] = False
        meta["error"] = "bad_json"
    return obj, meta


def extract_json(text):
    """从模型输出里抠出第一个完整的 JSON 对象。抠不到返回 None。

    不用 `json.loads(text)` 硬解：模型很爱在 JSON 前面加一句"好的，以下是结果："，
    或者是 ```json 包裹。这些都是"能用"，直接判失败会把 80% 的有效输出丢掉。
    """
    s = (text or "").strip()
    if not s:
        return None
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, (dict, list)) else None
    except Exception:
        pass
    depth = 0
    start = -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    start = -1
    return None


# ---------- 内部 ----------

def _extract(payload):
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _retryable(exc):
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code == 429 or code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def _short(exc):
    text = str(exc) or exc.__class__.__name__
    code = getattr(exc, "code", None)
    return ("HTTP %s %s" % (code, text))[:120] if code else text[:120]


def _record(tag, model, ms, ptok, ctok, ok, error, retries):
    with _LOCK:
        _STATS["calls"] += 1
        _STATS["ok" if ok else "fail"] += 1
        _STATS["ms"] += ms
        _STATS["prompt_tokens"] += ptok
        _STATS["completion_tokens"] += ctok
        _RECENT.append({"tag": tag, "model": model, "ms": ms, "ok": bool(ok),
                        "error": error, "prompt_tokens": ptok,
                        "completion_tokens": ctok, "retries": retries,
                        "at": time.strftime("%H:%M:%S")})
        if len(_RECENT) > _RECENT_MAX:
            del _RECENT[:len(_RECENT) - _RECENT_MAX]


def _fail(error, model, ms=0, retries=0):
    return {"ok": False, "text": "", "model": _model(model), "ms": ms,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "retries": retries, "error": error}
