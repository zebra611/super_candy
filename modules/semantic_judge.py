# -*- coding: utf-8 -*-
"""S3 / L4 开放域语义合规：模板层之后的一道可选判定。

**为什么模板层不够**
    `dialogue.compliance_guard` 是词表命中（保证/百分百/绝对…）。换个说法说同样的
    承诺——「这事儿我给您打包票」「我拿人格担保」「出了问题算我的」——一个词都不命中。
    探针 `miss_open_domain` 就是这个边界：词表能挡住原话，挡不住同义改写。

**为什么默认不开**
    每轮回复都多一次模型调用，延迟直接压在首字预算上（S1 已实测 qwen-flash
    P95 477ms，而链路只剩 186ms）。所以默认 `provider=template`：
    直接返回模板层结果，**一个字节都不改**、零网络。

**降级原则**
    无密钥 / 超时 / 返回乱码 / 置信度不足 → 原样返回模板层结果。
    判定失败绝不放行，也绝不因为判定失败而误拦。

**审计**
    只有模型真的判了违规才产生 `audit` 字段，由 app 写成独立的
    `compliance_judge` 事件；默认路径不写任何新事件（老事件流逐字节不变）。
    审计里**不存原话全文**，只存 sha256 前 8 位 + 前 24 字，便于回溯又不留敏感内容。
"""

import hashlib
import json
import urllib.request

import config_loader

_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_DEFAULT_MODEL = "qwen-flash"
_DEFAULT_TIMEOUT_MS = 900
_MIN_CONF = 0.6

_SYSTEM = (
    "你是外呼客服话术的合规审核员。判断这句话是否踩以下红线：\n"
    "  promise  承诺结果（打包票、算我的、肯定过、我担保）\n"
    "  absolute 绝对化表述（一定、百分百、绝对没问题）\n"
    "  privacy  索要或泄露**第三方**的姓名/电话/住址\n"
    "  induce   诱导、制造虚假稀缺或利益\n"
    "  none     以上都不是\n"
    "重要边界：客服为核对本人身份，请对方提供**本人**的手机号后四位、姓名、\n"
    "申请单号或公司名，属于正常业务流程，**不算 privacy**；只有涉及他人信息才算。\n"
    "另外：**明确拒绝承诺**（\"我不能给您承诺\"\"不能保证结果\"\"以审核为准\"）\n"
    "是合规表述，不算 promise；不要因为出现\"承诺/保证\"就判违规。\n"
    "只输出一行 JSON：{\"category\": \"...\", \"conf\": 0.0~1.0, \"reason\": \"不超过20字\"}。"
    "不确定就输出 none，不要解释。"
)

_warned = set()


def _warn(tag, msg):
    if tag not in _warned:
        _warned.add(tag)
        print("[semantic_judge] %s" % msg)


def _conf():
    try:
        return (config_loader.load() or {}).get("compliance_judge") or {}
    except Exception:
        return {}


def _template(text):
    """模板层结论。import 延迟放在函数里，避免模块级循环依赖。"""
    from modules import dialogue
    return dialogue.compliance_guard(text)


def _ask(text, cfg, key):
    """让模型判一次；任何异常返回 None（调用方降级）。"""
    try:
        body = {
            "model": cfg.get("model") or _DEFAULT_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "待审话术：%s" % (text or "")},
            ],
            "temperature": 0.0,
            "max_tokens": 60,
            "stream": False,
        }
        req = urllib.request.Request(
            cfg.get("endpoint") or _ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"})
        timeout_ms = int(cfg.get("timeout_ms") or _DEFAULT_TIMEOUT_MS)
        with urllib.request.urlopen(req, timeout=max(0.2, timeout_ms / 1000.0)) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        raw = payload["choices"][0]["message"]["content"] or ""
    except Exception as exc:
        _warn("call_fail", "语义合规判定失败，降级模板层：%s" % str(exc)[:80])
        return None

    t = raw.strip()
    if "{" in t and "}" in t:
        t = t[t.find("{"):t.rfind("}") + 1]
    try:
        data = json.loads(t)
        cat = str(data.get("category") or "none").strip().lower()
        conf = float(data.get("conf"))
    except Exception:
        _warn("bad_out", "语义合规输出无法解析，降级模板层")
        return None
    if cat not in ("promise", "absolute", "privacy", "induce", "none"):
        return None
    if not (0.0 <= conf <= 1.0):
        return None
    return {"category": cat, "conf": conf,
            "reason": str(data.get("reason") or "")[:40]}


def judge(text, cfg=None, key=None):
    """返回与 `compliance_guard` 同形状的 dict（text/blocked/hits/replaced）。

    默认路径 = 模板层原样返回（同一个 dict 对象内容，逐字节一致）。
    只有「模板层没拦 + 显式开启 + 有密钥 + 模型判违规」才多出
    `via` / `reason` / `category` / `audit` 四个字段。
    """
    base = _template(text)
    if base.get("blocked"):
        return base

    try:
        cfg = cfg if cfg is not None else _conf()
        if str(cfg.get("provider") or "template").strip() != "dashscope":
            return base
        if key is None:
            key = config_loader.secret("dashscope_api_key")
        if not key:
            _warn("no_key", "未配置 dashscope_api_key，语义合规保持模板层")
            return base
        verdict = _ask(text, cfg, key)
        if not verdict or verdict["category"] == "none" or verdict["conf"] < _MIN_CONF:
            return base

        safe = config_loader.blocked_reply()
        # 兜底话术自己也不能违规，否则会出现「用来兜底的句子违规」。
        if any(w and w in safe for w in config_loader.forbidden_words()):
            safe = "这部分我需要请专员和您说明，可以吗？"
        body = (text or "")
        return {
            "text": safe, "blocked": True, "hits": [], "replaced": True,
            "via": "semantic", "category": verdict["category"],
            "reason": verdict["reason"],
            "audit": {
                "provider": "model",
                "model": cfg.get("model") or _DEFAULT_MODEL,
                "category": verdict["category"],
                "conf": round(verdict["conf"], 3),
                "reason": verdict["reason"],
                # 不存原话全文：审计要能回溯，但不必留全量业务内容。
                "text_sha8": hashlib.sha256(body.encode("utf-8")).hexdigest()[:8],
                "text_preview": body[:24],
                "text_len": len(body),
            },
        }
    except Exception as exc:
        _warn("judge_fail", "语义合规异常，降级模板层：%s" % str(exc)[:80])
        return base
