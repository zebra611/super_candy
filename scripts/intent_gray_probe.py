# -*- coding: utf-8 -*-
"""S1 灰度决策探针：延迟预算 / 成本口径 / 降级路径三要素。

跑法：
    python scripts/intent_gray_probe.py                 # 有 Key 时实测云端 + 离线降级
    python scripts/intent_gray_probe.py --offline       # 只跑离线降级（CI / 无网）
    python scripts/intent_gray_probe.py --rounds 24     # 指定云端实测条数
    python scripts/intent_gray_probe.py --self-test     # 变异验证：降级断言必须能变红
    python scripts/intent_gray_probe.py --json data/intent_gray_probe_latest.json

**这个脚本不做什么**
    不开启模型、不改任何配置、不写 scene.json。它只产出决策依据，
    provider 由人按 docs/26 的闸门决定是否切。

**为什么延迟必须实测而不是引用旧数字**
    清单里写的「模型改写 401~560ms」是 11-05 用 nlg 改写测的，
    不是 intent 分类。两者 prompt 长度、max_tokens、调用位置都不同，
    拿它当 intent 的延迟预算属于偷换口径。这里按生产默认
    timeout_ms=800 逐条打真实接口，并明写失败/超时数。

**成本为什么给区间**
    qwen-flash 公开报价在不同官方页面口径略有差异
    （help.aliyun.com 模型页 0.15 元/百万输入、1.5 元/百万输出；
      计费规则页 0.0002 元/千输入、0.0015 元/千输出 = 0.2/1.5 元每百万）。
    这里按区间算，并注明以控制台账单为准，不假装精确。
"""

import argparse
import io
import json
import os
import statistics
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import config_loader                     # noqa: E402
from modules import dialogue, intent     # noqa: E402
from intent_bench import SAMPLES         # noqa: E402

# 元 / 百万 token。来源见模块 docstring，查证日期 2026-11-06。
PRICE_IN_LOW, PRICE_IN_HIGH = 0.15, 0.20
PRICE_OUT = 1.50
# C7 实测的单轮预算（docs/C7 报告），不是估算值。
CHAIN_BUDGET_MS = 1300
CHAIN_P50_MS = 813
CHAIN_P95_MS = 1114

CFG = {"provider": "dashscope", "model": "qwen-flash",
       "timeout_ms": intent._DEFAULT_TIMEOUT_MS}


# --------------------------------------------------------------------------
# 工具：临时替换 / 百分位 / 假响应
# --------------------------------------------------------------------------
def _patch(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    return old


def _pct(values, q):
    """线性插值百分位；空列表返回 None（不允许悄悄写 0）。"""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return round(s[0], 1)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


class _FakeResp:
    """把真实响应体缓存下来：既让 _ask_model 读得到，又能取 usage。"""

    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass


# --------------------------------------------------------------------------
# A. 云端延迟 + token 用量实测
# --------------------------------------------------------------------------
def cloud_probe(rounds):
    key = config_loader.secret("dashscope_api_key")
    if not key:
        return {"available": False, "reason": "未配置 dashscope_api_key，云端未测"}

    usage_records, latencies, outcomes = [], [], []
    orig_urlopen = urllib.request.urlopen

    def wrapped(req, timeout=None):
        resp = orig_urlopen(req, timeout=timeout)
        body = resp.read()
        try:
            resp.close()
        except Exception:
            pass
        try:
            d = json.loads(body.decode("utf-8", "replace"))
            u = d.get("usage") or {}
            usage_records.append({
                "prompt": int(u.get("prompt_tokens") or 0),
                "completion": int(u.get("completion_tokens") or 0),
                "total": int(u.get("total_tokens") or 0),
            })
        except Exception:
            pass
        return _FakeResp(body)

    _patch(urllib.request, "urlopen", wrapped)
    try:
        for text, _, _ in SAMPLES[:rounds]:
            t0 = time.time()
            try:
                picked = intent._ask_model(text, CFG)
                ok = picked is not None
                label = picked[0] if picked else ""
                conf = round(picked[1], 3) if picked else None
            except Exception as exc:                 # 探针不该被单条异常打断
                ok, label, conf = False, "EXC:" + type(exc).__name__, None
            latencies.append(round((time.time() - t0) * 1000, 1))
            outcomes.append({"text": text, "ok": ok, "label": label, "conf": conf})
            time.sleep(0.12)                          # 别把 RPM 打爆污染样本
    finally:
        _patch(urllib.request, "urlopen", orig_urlopen)

    good = [x["ok"] for x in outcomes]
    prompt_avg = (round(statistics.mean(u["prompt"] for u in usage_records), 1)
                  if usage_records else None)
    completion_avg = (round(statistics.mean(u["completion"] for u in usage_records), 1)
                      if usage_records else None)
    return {
        "available": True,
        "model": CFG["model"], "timeout_ms": CFG["timeout_ms"],
        "rounds": len(outcomes),
        "success": sum(good), "failure": len(good) - sum(good),
        "latency_ms": {
            "p50": _pct(latencies, 0.50), "p95": _pct(latencies, 0.95),
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "all": latencies,
        },
        "usage": {"calls": len(usage_records),
                  "avg_prompt_tokens": prompt_avg,
                  "avg_completion_tokens": completion_avg},
        "outcomes": outcomes,
    }


# --------------------------------------------------------------------------
# B. 降级路径：全部离线、确定性、逐字节对比
# --------------------------------------------------------------------------
def degradation_probe():
    """每一条都断言「异常时 intent.classify 与规则版逐字节一致」。

    反向用例（D8）证明降级闸门没有把正常改写也堵死——否则
    全绿可能只是「模型根本没生效」的假绿。
    """
    text = "算了，不折腾了"
    base = dialogue.classify(text)
    results = []

    def case(name, ok, detail=""):
        results.append({"name": name, "pass": bool(ok), "detail": detail})

    # D0 provider=template 时一次网络都不发（默认配置就是它）
    calls = [0]
    orig_urlopen = urllib.request.urlopen

    def counting(req, timeout=None):
        calls[0] += 1
        return orig_urlopen(req, timeout=timeout)

    p1 = _patch(urllib.request, "urlopen", counting)
    p2 = _patch(intent, "_config", lambda: {"provider": "template"})
    try:
        got = intent.classify(text)
        case("D0 provider=template 零网络调用且逐字节等于规则版",
             calls[0] == 0 and got == base,
             "calls=%d equal=%s" % (calls[0], got == base))
    finally:
        _patch(intent, "_config", p2)
        _patch(urllib.request, "urlopen", p1)

    # D1 无 Key
    p1 = _patch(config_loader, "secret", lambda name, default=None: None)
    p2 = _patch(intent, "_config", lambda: CFG)
    try:
        got = intent.classify(text)
        case("D1 无 API Key → 退回规则版", got == base, "equal=%s" % (got == base))
    finally:
        _patch(intent, "_config", p2)
        _patch(config_loader, "secret", p1)

    # D2 网络超时
    def boom(req, timeout=None):
        raise TimeoutError("simulated timeout")

    p1 = _patch(urllib.request, "urlopen", boom)
    p2 = _patch(intent, "_config", lambda: CFG)
    try:
        got = intent.classify(text)
        case("D2 调用超时 → 退回规则版", got == base, "equal=%s" % (got == base))
    finally:
        _patch(intent, "_config", p2)
        _patch(urllib.request, "urlopen", p1)

    # D3 模型返回乱码
    p1 = _patch(urllib.request, "urlopen",
                lambda req, timeout=None: _FakeResp(b"not a json at all"))
    p2 = _patch(intent, "_config", lambda: CFG)
    try:
        got = intent.classify(text)
        case("D3 输出无法解析 → 退回规则版", got == base, "equal=%s" % (got == base))
    finally:
        _patch(intent, "_config", p2)
        _patch(urllib.request, "urlopen", p1)

    # D4 禁止模型改写的标签（deny / hangup / handoff / 未登记标签）
    p2 = _patch(intent, "_config", lambda: CFG)
    try:
        for label in ("handoff", "deny", "hangup", "info", "other"):
            p1 = _patch(intent, "_ask_model", lambda t, c, lb=label: (lb, 0.99))
            try:
                got = intent.classify(text)
                case("D4 禁止标签 %-8s → 退回规则版" % label, got == base,
                     "equal=%s" % (got == base))
            finally:
                _patch(intent, "_ask_model", p1)
    finally:
        _patch(intent, "_config", p2)

    # D5 低置信
    p1 = _patch(intent, "_ask_model", lambda t, c: ("yes", 0.31))
    p2 = _patch(intent, "_config", lambda: CFG)
    try:
        got = intent.classify(text)
        case("D5 置信度 0.31 < 0.70 → 退回规则版", got == base,
             "equal=%s" % (got == base))
    finally:
        _patch(intent, "_config", p2)
        _patch(intent, "_ask_model", p1)

    # D6 身份证据 digits 不许被模型碰
    p1 = _patch(intent, "_ask_model", lambda t, c: ("yes", 0.99))
    p2 = _patch(intent, "_config", lambda: CFG)
    try:
        t2 = "尾号8866"
        b2 = dialogue.classify(t2)
        got = intent.classify(t2)
        case("D6 digits 尾号不被模型改写（身份证据）",
             got.get("digits") == b2.get("digits"),
             "base=%r got=%r" % (b2.get("digits"), got.get("digits")))
    finally:
        _patch(intent, "_config", p2)
        _patch(intent, "_ask_model", p1)

    # D7 密钥读取本身抛异常（secrets 文件损坏/权限问题）时绝不向外抛。
    # 注意这不是人为给 _ask_model 塞异常：_ask_model 里 config_loader.secret
    # 在 try 之外，是生产代码真实可能漏异常的入口。
    def bad_secret(name, default=None):
        raise OSError("simulated secrets read failure")

    p1 = _patch(config_loader, "secret", bad_secret)
    p2 = _patch(intent, "_config", lambda: CFG)
    try:
        try:
            got = intent.classify(text)
            case("D7 密钥读取抛异常时 classify 不向外抛且退回规则版", got == base,
                 "equal=%s" % (got == base))
        except Exception as exc:
            case("D7 密钥读取抛异常时 classify 不向外抛且退回规则版", False,
                 "异常漏出：%s" % type(exc).__name__)
    finally:
        _patch(intent, "_config", p2)
        _patch(config_loader, "secret", p1)

    # D8 反向：合法高置信标签确实能改写（防「模型压根没生效」的假绿）
    p1 = _patch(intent, "_ask_model", lambda t, c: ("yes", 0.95))
    p2 = _patch(intent, "_config", lambda: CFG)
    try:
        got = intent.classify(text)
        case("D8 反向：合法高置信标签能改写（evidence=model）",
             got.get("evidence") == "model" and got.get("yes") is True,
             "evidence=%r yes=%r" % (got.get("evidence"), got.get("yes")))
    finally:
        _patch(intent, "_config", p2)
        _patch(intent, "_ask_model", p1)

    return {"total": len(results), "passed": sum(1 for r in results if r["pass"]),
            "cases": results}


# --------------------------------------------------------------------------
# C. 变异验证：降级断言必须能变红
# --------------------------------------------------------------------------
def self_test():
    text = "算了，不折腾了"
    base = dialogue.classify(text)
    out = []

    # M1：把禁止标签 handoff 放进可改写表，D4 的那条应该失效
    orig = intent._EDITABLE
    p1 = _patch(intent, "_EDITABLE", dict(orig, handoff=("handoff",)))
    p2 = _patch(intent, "_ask_model", lambda t, c: ("handoff", 0.99))
    p3 = _patch(intent, "_config", lambda: CFG)
    try:
        changed = intent.classify(text) != base
        out.append({"name": "M1 放开禁止标签后断言必须变红", "pass": changed})
    finally:
        _patch(intent, "_config", p3)
        _patch(intent, "_ask_model", p2)
        _patch(intent, "_EDITABLE", p1)

    # M2：把置信闸门调到 0，低置信应该能改写，D5 那条应该失效
    p1 = _patch(intent, "_MIN_MODEL_CONF", 0.0)
    p2 = _patch(intent, "_ask_model", lambda t, c: ("yes", 0.31))
    p3 = _patch(intent, "_config", lambda: CFG)
    try:
        changed = intent.classify(text) != base
        out.append({"name": "M2 放开置信闸门后断言必须变红", "pass": changed})
    finally:
        _patch(intent, "_config", p3)
        _patch(intent, "_ask_model", p2)
        _patch(intent, "_MIN_MODEL_CONF", p1)

    return {"total": len(out), "passed": sum(1 for r in out if r["pass"]),
            "cases": out}


# --------------------------------------------------------------------------
# D. 成本口径
# --------------------------------------------------------------------------
def cost_model(cloud, turns_per_call):
    u = cloud.get("usage") or {}
    pin, pout = u.get("avg_prompt_tokens"), u.get("avg_completion_tokens")
    if not cloud.get("available") or pin is None or pout is None:
        return {"available": False, "reason": "云端未测，token 用量无实测值，不编造成本"}
    per_low = (pin * PRICE_IN_LOW + pout * PRICE_OUT) / 1e6
    per_high = (pin * PRICE_IN_HIGH + pout * PRICE_OUT) / 1e6
    calls = turns_per_call
    return {
        "available": True,
        "avg_prompt_tokens": pin, "avg_completion_tokens": pout,
        "price_in_per_million": [PRICE_IN_LOW, PRICE_IN_HIGH],
        "price_out_per_million": PRICE_OUT,
        "price_note": "阿里云百炼公开报价（2026-11-06 查证），两个官方页面输入单价口径不同，按区间给；以控制台账单为准",
        "cny_per_call": [round(per_low, 8), round(per_high, 8)],
        "cny_per_conversation": [round(per_low * calls, 6), round(per_high * calls, 6)],
        "cny_per_1000_conversations": [round(per_low * calls * 1000, 4),
                                       round(per_high * calls * 1000, 4)],
        "turns_per_conversation": calls,
        "scope": "仅 intent 分类调用；不含 nlg 改写、ASR、TTS、embedding",
    }


def avg_turns():
    try:
        data = json.load(open(os.path.join(ROOT, "eval", "seed_cases.json"),
                             encoding="utf-8"))
        cases = data["cases"] if isinstance(data, dict) else data
        return round(statistics.mean(len(c.get("turns") or []) for c in cases), 1)
    except Exception:
        return 8.0


# --------------------------------------------------------------------------
def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    cloud = ({"available": False, "reason": "--offline 指定，云端未测"}
             if args.offline else cloud_probe(args.rounds))
    deg = degradation_probe()
    mut = self_test() if args.self_test else {"total": 0, "passed": 0, "cases": []}
    cost = cost_model(cloud, avg_turns())

    print("=" * 78)
    print("S1 intent 模型灰度决策探针")
    print("=" * 78)
    if cloud.get("available"):
        l = cloud["latency_ms"]
        print("A. 云端延迟（qwen-flash / timeout_ms=%d / %d 次真实调用）"
              % (CFG["timeout_ms"], cloud["rounds"]))
        print("   成功 %d / 失败 %d ｜ P50 %.0fms ｜ P95 %.0fms ｜ max %.0fms"
              % (cloud["success"], cloud["failure"], l["p50"], l["p95"], l["max"]))
        print("   实测 token：prompt 均值 %.1f ｜ completion 均值 %.1f"
              % (cloud["usage"]["avg_prompt_tokens"],
                 cloud["usage"]["avg_completion_tokens"]))
        headroom = CHAIN_BUDGET_MS - CHAIN_P95_MS
        print("   C7 链路 P95 %.0fms / 预算 %dms → 剩余 %dms；intent P95 %s 预算"
              % (CHAIN_P95_MS, CHAIN_BUDGET_MS, headroom,
                 "超出" if l["p95"] > headroom else "在"))
    else:
        print("A. 云端延迟：未测（%s）—— 不写成跑过了" % cloud.get("reason"))
    if cost.get("available"):
        print("B. 成本：单次 %.6f~%.6f 元 ｜ 单通(%.1f轮) %.4f~%.4f 元 ｜ 1000 通 %.2f~%.2f 元"
              % (cost["cny_per_call"][0], cost["cny_per_call"][1],
                 cost["turns_per_conversation"],
                 cost["cny_per_conversation"][0], cost["cny_per_conversation"][1],
                 cost["cny_per_1000_conversations"][0],
                 cost["cny_per_1000_conversations"][1]))
    else:
        print("B. 成本：未测（%s）" % cost.get("reason"))
    print("C. 降级路径：%d/%d 通过" % (deg["passed"], deg["total"]))
    for r in deg["cases"]:
        print("   [%s] %s %s" % ("PASS" if r["pass"] else "FAIL",
                                 r["name"], r["detail"]))
    if args.self_test:
        print("D. 变异验证：%d/%d 断言能变红" % (mut["passed"], mut["total"]))
        for r in mut["cases"]:
            print("   [%s] %s" % ("PASS" if r["pass"] else "FAIL", r["name"]))

    payload = {"cloud": cloud, "degradation": deg, "self_test": mut,
               "cost": cost, "chain_budget_ms": CHAIN_BUDGET_MS,
               "chain_p50_ms": CHAIN_P50_MS, "chain_p95_ms": CHAIN_P95_MS}
    if args.json:
        path = args.json if os.path.isabs(args.json) else os.path.join(ROOT, args.json)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print("\n结果已写入 %s" % path)

    ok = deg["passed"] == deg["total"]
    if args.self_test:
        ok = ok and mut["passed"] == mut["total"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
