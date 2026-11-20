"""LLM-as-Judge 评分通道 —— 规则判定之外的一路"第二意见"（P1-1）。

边界（先读这段，比代码重要）：
    这是**离线评测**用的裁判，只跑在 scripts/run_eval.py 里，**绝不上实时链路**。
    外呼主链路的 2s 预算容不下一次模型往返；线上判定仍以规则为准。
    默认 mode=rule，与改动前逐字节一致；只有显式 --judge=both 才多跑这一路。

它做什么：
    输入一通会话的事件流 + 场景 rubric + 规则判定结果，让大模型按 rubric
    输出结构化判定 {closed, compliance_ok, quality_score, reason}，
    与规则分并列展示，供人工看"规则判不了的开放域话术，模型怎么看"。

可复现性（官方硬要求，三条缺一不可）：
    1. temperature=0，不给模型掷骰子的余地；
    2. rubric 版本号 + 模型名 + prompt 的 sha256 一起落进结果；
    3. 同一输入跑两次必须同分——test_llm_judge.py 用注入的假 HTTP 断言。

失败绝不阻塞评测：
    没密钥 / 网络失败 / 返回格式不对 → 回落规则判定，标 judge_fallback=true
    并写明原因。宁可少一路意见，不能让评测跑挂。

负责人：角色 3（质检评测与自进化）
"""

import hashlib
import json
import os
import urllib.error
import urllib.request

import config_loader

_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_DEFAULT_MODEL = "qwen-flash"
_DEFAULT_RUBRIC_VERSION = "r1"

#: 默认评分 rubric。真实话术规范由企业提供后替换，版本随 eval.judge.rubric_version 走。
_DEFAULT_RUBRIC = (
    "你是外呼电话的质检裁判。按三条给这一通电话打分，只输出一个 JSON：\n"
    '{"closed": true/false, "compliance_ok": true/false, '
    '"quality_score": 0-100 的整数, "reason": "一句话理由"}\n'
    "判定口径：\n"
    "  closed——业务结果是否真的回写（有工单/登记动作），只说'帮您记下了'不算；\n"
    "  compliance_ok——有没有承诺未授权权益、诱导、辱骂、泄露完整号码；\n"
    "  quality_score——流程完整度、是否回答了客户问题、话术是否得体；\n"
    "  reason——不超过 40 字。不要输出 JSON 以外的任何文字。"
)


def judge_config():
    """读 eval.judge 配置，缺字段时给安全默认（不打开 LLM 判定）。"""
    cfg = (config_loader.load().get("eval") or {}).get("judge") or {}
    return {
        "mode": (cfg.get("mode") or "rule").strip(),
        "model": (cfg.get("model") or _DEFAULT_MODEL).strip(),
        "rubric_version": (cfg.get("rubric_version") or _DEFAULT_RUBRIC_VERSION).strip(),
        "rubric": cfg.get("rubric") or _DEFAULT_RUBRIC,
        "endpoint": cfg.get("endpoint") or _ENDPOINT,
        "timeout_s": float(cfg.get("timeout_s") or 20),
    }


def _fold_events(events, max_chars=6000):
    """把事件流压成给模型看的对话文本（user/agent 交替，带阶段）。

    只取 asr_result 与 agent_reply，其他事件（打点/幂等/成本）与评分无关。
    截断保留尾部——结局比开头更能决定闭环判定。
    """
    lines = []
    for e in events or []:
        t = e.get("type")
        if t == "asr_result":
            text = (e.get("payload") or {}).get("text") or ""
            if text:
                lines.append("客户：%s" % text)
        elif t == "agent_reply":
            p = e.get("payload") or {}
            text = p.get("text") or ""
            if text:
                stage = p.get("stage") or ""
                lines.append("客服[%s]：%s" % (stage, text))
    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text


def build_prompt(events, rubric):
    """纯函数：由事件流和 rubric 造完整 prompt。可离线测、可算 hash。"""
    dialog = _fold_events(events)
    return rubric.strip() + "\n\n客服与客户的对话：\n" + (dialog or "（无对话）")


def _prompt_hash(prompt, model, rubric_version):
    """可复现性凭据：同一 prompt/模型/rubric 版本 → 同一 hash。"""
    blob = json.dumps({"prompt": prompt, "model": model,
                       "rubric_version": rubric_version},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _fallback(rule_result, reason, stamp):
    """失败回落：判定值取规则结果，但明确标出这一路没判。

    stamp 里的可复现性字段（模型/rubric/prompt hash）照样带上——即使没调成，
    也要能回答"这次本来打算用谁判、判据是什么"。judge 字段改成 rule_fallback。
    """
    meta = dict(stamp)
    meta["judge"] = "rule_fallback"
    meta["judge_fallback"] = True
    meta["fallback_reason"] = reason
    out = {
        "closed": bool(rule_result.get("closed")),
        "compliance_ok": bool(rule_result.get("compliance_ok", True)),
        "quality_score": rule_result.get("quality_score",
                                         100 if rule_result.get("ok") else 0),
        "reason": "LLM 判定不可用，回落规则分",
    }
    out.update(meta)
    return out


def _parse_verdict(text):
    """从模型输出里抠出 JSON；格式不对返回 None，由调用方回落。"""
    try:
        s = (text or "").find("{")
        e = (text or "").rfind("}")
        if s < 0 or e <= s:
            return None
        obj = json.loads(text[s:e + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "closed" not in obj:
        return None
    try:
        score = int(obj.get("quality_score"))
        score = max(0, min(100, score))
    except (ValueError, TypeError):
        score = None
    return {
        "closed": bool(obj.get("closed")),
        "compliance_ok": bool(obj.get("compliance_ok", True)),
        "quality_score": score,
        "reason": str(obj.get("reason") or "")[:120],
    }


class LlmJudge:
    """一次裁判调用。HTTP 层可注入，便于离线测试，不依赖真密钥/网络。"""

    def __init__(self, cfg=None, http_post=None):
        self.cfg = cfg or judge_config()
        self._post = http_post or self._http_post

    def _http_post(self, url, body, headers, timeout):
        req = urllib.request.Request(
            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def judge(self, events, rule_result):
        """返回结构化判定；任何失败都回落规则分，不抛异常。"""
        cfg = self.cfg
        prompt = build_prompt(events, cfg["rubric"])
        stamp = {
            "judge": "llm",
            "judge_fallback": False,
            "model": cfg["model"],
            "rubric_version": cfg["rubric_version"],
            "prompt_sha256": _prompt_hash(prompt, cfg["model"], cfg["rubric_version"]),
        }
        key = config_loader.secret("dashscope_api_key")
        if not key:
            return _fallback(rule_result, "没有 dashscope_api_key", stamp)
        body = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer %s" % key}
        try:
            data = self._post(cfg["endpoint"], body, headers, cfg["timeout_s"])
            text = data["choices"][0]["message"]["content"]
        except Exception as exc:                      # 网络/配额/格式都可能失败
            return _fallback(rule_result, "%s: %s" % (
                type(exc).__name__, str(exc)[:120]), stamp)
        verdict = _parse_verdict(text)
        if verdict is None:
            return _fallback(rule_result, "模型输出不是预期的 JSON", stamp)
        verdict.update(stamp)
        return verdict


def judge_session(events, rule_result):
    """便捷入口：按当前配置跑一次。mode != llm/both 时直接返回 None。"""
    cfg = judge_config()
    if cfg["mode"] not in ("llm", "both"):
        return None
    return LlmJudge(cfg).judge(events, rule_result)
