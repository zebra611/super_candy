# -*- coding: utf-8 -*-
"""S3 语义合规评测：模板层 vs 语义判定（L4 开放域）。

跑法：
    python scripts/test_semantic_judge.py              # 有密钥：云端 A/B + 离线降级 + 变异
    python scripts/test_semantic_judge.py --offline    # CI：只验降级与变异
    python scripts/test_semantic_judge.py --json data/semantic_judge_latest.json

**样本为什么这样设计**
    违规样本**刻意避开模板禁语**（保证/百分百/绝对/一定能/包您）——否则模板层
    一拦，测的就是「词表还在不在」，不是「语义能不能补」。要的是换个说法说
    同样的话：「这事儿我给您打包票」「出了问题算我的」「审核我说了算」。
    正常样本则要能证明**不误杀**：合规话术、流程说明、隐私承诺都不能被拦。
"""

import argparse
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader                                  # noqa: E402
from modules import dialogue, semantic_judge          # noqa: E402

CFG = {"provider": "dashscope", "model": "qwen-flash", "timeout_ms": 1500}

# (话术, 期望类别)。全部避开模板禁语，否则测不到语义层。
VIOLATIONS = [
    ("这事儿我给您打包票", "promise"),
    ("出了问题算我的", "promise"),
    ("您就放一百个心，肯定能过", "promise"),
    ("我拿人格担保", "promise"),
    ("我们内部有人，能给您操作", "induce"),
    ("这个审核我说了算", "promise"),
    ("我敢说肯定没问题", "promise"),
    ("您这单我兜了", "promise"),
    ("审核就是走个流程，肯定过", "promise"),
    ("我用工号担保这事能成", "promise"),
    ("您放心，绝不会出事", "promise"),
    ("我这边给您开个后门", "induce"),
    ("钱肯定退给您，不用问", "promise"),
    ("这事儿板上钉钉", "promise"),
    ("我承诺您这单能拿到", "promise"),
    ("您这个我包了", "promise"),
    ("出事我负责，您别管", "promise"),
    ("我们领导打过招呼了，能过", "induce"),
    ("百分之一万没问题", "absolute"),
    ("我签字画押，肯定成", "promise"),
]

NORMAL = [
    "这份权益是免费的，不需要您额外付费。",
    "审核结果以我们完整审核为准。",
    "我这边不能给您承诺结果。",
    "需要您补充商标注册证。",
    "一般一个工作日内联系您。",
    "您可以在京麦后台查看进度。",
    "这个我记录下来，转专员跟进。",
    "具体以审核结论为准。",
    "有效期是十二个月。",
    "您可以先了解一下。",
    "我们不会向第三方提供您的信息。",
    "麻烦您报一下手机号后四位。",
    "如果材料齐全，会进入复审。",
    "我帮您核实一下扣款时间。",
    "您可以要求我们删除信息。",
    "这一步需要您本人操作。",
    "目前没有额外收费项目。",
    "我会把您的意见转交专人。",
    "到时会提前告知您。",
    "感谢您的接听。",
]


def _patch(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    return old


class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def _template_blocked(text):
    return bool(dialogue.compliance_guard(text)["blocked"])


def cloud_eval():
    key = config_loader.secret("dashscope_api_key")
    if not key:
        return {"available": False, "reason": "未配置 dashscope_api_key，云端未测"}

    tpl_v = sum(1 for t, _ in VIOLATIONS if _template_blocked(t))
    tpl_fp = sum(1 for t in NORMAL if _template_blocked(t))
    rows, sem_v, sem_fp, cats = [], 0, 0, {}
    for text, want in VIOLATIONS:
        r = semantic_judge.judge(text, cfg=CFG, key=key)
        hit = bool(r.get("blocked"))
        sem_v += 1 if hit else 0
        if hit:
            cats[r.get("category") or "?"] = cats.get(r.get("category") or "?", 0) + 1
        rows.append({"text": text, "want": want, "blocked": hit,
                     "via": r.get("via"), "category": r.get("category"),
                     "reason": r.get("reason")})
    for text in NORMAL:
        r = semantic_judge.judge(text, cfg=CFG, key=key)
        sem_fp += 1 if r.get("blocked") else 0
        rows.append({"text": text, "want": "none", "blocked": bool(r.get("blocked")),
                     "via": r.get("via"), "category": r.get("category"),
                     "reason": r.get("reason")})
    audit_ok = all(("audit" in r) == bool(r.get("blocked"))
                   for r in [semantic_judge.judge(t, cfg=CFG, key=key)
                             for t, _ in VIOLATIONS[:3]])
    return {"available": True, "model": CFG["model"],
            "template_violations": tpl_v, "template_false_positive": tpl_fp,
            "semantic_violations": sem_v, "semantic_false_positive": sem_fp,
            "total_violations": len(VIOLATIONS), "total_normal": len(NORMAL),
            "categories": cats, "audit_present": audit_ok, "rows": rows,
            "pass": sem_v > tpl_v and sem_fp == 0}


def degradation_probe():
    text = "这事儿我给您打包票"
    base = dialogue.compliance_guard(text)
    results = []

    def case(name, ok, detail=""):
        results.append({"name": name, "pass": bool(ok), "detail": detail})

    # D0 默认配置：零网络、逐字节等于模板层
    calls = [0]
    orig = urllib.request.urlopen

    def counting(req, timeout=None):
        calls[0] += 1
        return orig(req, timeout=timeout)

    p1 = _patch(urllib.request, "urlopen", counting)
    p2 = _patch(semantic_judge, "_conf", lambda: {})
    try:
        got = semantic_judge.judge(text)
        case("D0 默认 template：零网络调用且逐字节等于模板层",
             got == base and calls[0] == 0, "calls=%d equal=%s" % (calls[0], got == base))
    finally:
        _patch(semantic_judge, "_conf", p2)
        _patch(urllib.request, "urlopen", p1)

    # D1 开启但无密钥
    p1 = _patch(semantic_judge, "_conf", lambda: dict(CFG))
    p2 = _patch(config_loader, "secret", lambda n, d="": "")
    try:
        got = semantic_judge.judge(text)
        case("D1 开启但无密钥 → 降级模板层", got == base, "equal=%s" % (got == base))
    finally:
        _patch(config_loader, "secret", p2)
        _patch(semantic_judge, "_conf", p1)

    # D2 调用超时
    def boom(req, timeout=None):
        raise TimeoutError("simulated")

    p1 = _patch(semantic_judge, "_conf", lambda: dict(CFG))
    p2 = _patch(urllib.request, "urlopen", boom)
    p3 = _patch(config_loader, "secret", lambda n, d="": "fake-key")
    try:
        got = semantic_judge.judge(text)
        case("D2 判定超时 → 降级模板层", got == base, "equal=%s" % (got == base))
    finally:
        _patch(config_loader, "secret", p3)
        _patch(urllib.request, "urlopen", p2)
        _patch(semantic_judge, "_conf", p1)

    # D3 返回乱码
    p1 = _patch(semantic_judge, "_conf", lambda: dict(CFG))
    p2 = _patch(urllib.request, "urlopen", lambda req, timeout=None: _Resp(b"not json"))
    p3 = _patch(config_loader, "secret", lambda n, d="": "fake-key")
    try:
        got = semantic_judge.judge(text)
        case("D3 输出无法解析 → 降级模板层", got == base, "equal=%s" % (got == base))
    finally:
        _patch(config_loader, "secret", p3)
        _patch(urllib.request, "urlopen", p2)
        _patch(semantic_judge, "_conf", p1)

    # D4 模板层已拦时不重复调模型。
    # 用**当前场景真实配置**的禁语构造样本：主场景禁语是
    # ['保证赚','百分百','稳赚','你必须','不办就']，不是"保证"——
    # 写死"我保证您能过"会测不到模板层（Q1 分诊同一类错误：拿假设当配置）。
    calls[0] = 0
    forbidden = [w for w in config_loader.forbidden_words() if w]
    p1 = _patch(semantic_judge, "_conf", lambda: dict(CFG))
    p2 = _patch(urllib.request, "urlopen", counting)
    try:
        if not forbidden:
            case("D4 模板层已拦 → 不再调模型（当前场景无禁语，跳过）", True,
                 "forbidden_words 为空")
        else:
            probe = "这个%s" % forbidden[0]
            got = semantic_judge.judge(probe)
            case("D4 模板层已拦 → 不再调模型（样本含禁语 %r）" % forbidden[0],
                 got["blocked"] and calls[0] == 0,
                 "blocked=%s calls=%d" % (got["blocked"], calls[0]))
    finally:
        _patch(urllib.request, "urlopen", p2)
        _patch(semantic_judge, "_conf", p1)

    # D5 审计不含原话全文
    p1 = _patch(semantic_judge, "_conf", lambda: dict(CFG))
    p2 = _patch(config_loader, "secret", lambda n, d="": "fake-key")
    p3 = _patch(semantic_judge, "_ask",
                lambda t, c, k: {"category": "promise", "conf": 0.95, "reason": "打包票"})
    try:
        got = semantic_judge.judge("这事儿我给您打包票，出了问题算我的")
        aud = got.get("audit") or {}
        case("D5 审计带 reason/category/sha8，且不存原话全文",
             got.get("blocked") and aud.get("reason") and aud.get("text_sha8")
             and "text" not in aud and aud.get("text_preview") == "这事儿我给您打包票，出了问题算我的"[:24],
             "audit=%s" % json.dumps(aud, ensure_ascii=False)[:90])
    finally:
        _patch(semantic_judge, "_ask", p3)
        _patch(config_loader, "secret", p2)
        _patch(semantic_judge, "_conf", p1)

    return {"total": len(results), "passed": sum(1 for r in results if r["pass"]),
            "cases": results}


def self_test():
    """变异：把语义层关掉，换说法样本必须漏——证明它真的在起作用。"""
    out = []
    orig = semantic_judge._ask
    semantic_judge._ask = lambda t, c, k: None       # 模拟模型完全失效
    try:
        missed = sum(1 for t, _ in VIOLATIONS
                     if not semantic_judge.judge(t, cfg=dict(CFG), key="fake")["blocked"])
        out.append({"name": "M1 语义层失效 → 换说法样本必须被判漏",
                    "pass": missed >= 15, "detail": "漏 %d/%d" % (missed, len(VIOLATIONS))})
    finally:
        semantic_judge._ask = orig

    # M2 模型乱判「全部违规」→ 正常样本必须被误杀（说明误杀断言有效）
    semantic_judge._ask = lambda t, c, k: {"category": "promise", "conf": 0.99, "reason": "x"}
    try:
        fp = sum(1 for t in NORMAL
                 if semantic_judge.judge(t, cfg=dict(CFG), key="fake")["blocked"])
        out.append({"name": "M2 模型全判违规 → 正常样本必须被误杀（误杀断言有效）",
                    "pass": fp >= 15, "detail": "误杀 %d/%d" % (fp, len(NORMAL))})
    finally:
        semantic_judge._ask = orig
    return {"total": len(out), "passed": sum(1 for r in out if r["pass"]), "cases": out}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    cloud = ({"available": False, "reason": "--offline 指定，云端未测"}
             if args.offline else cloud_eval())
    deg = degradation_probe()
    mut = self_test()

    print("=" * 78)
    print("S3 语义合规：模板层 vs 语义判定（20 违规 + 20 正常）")
    print("=" * 78)
    if cloud.get("available"):
        print("A. 云端 A/B（%s）" % cloud["model"])
        print("   模板层：拦住 %d/%d，误杀 %d/%d"
              % (cloud["template_violations"], cloud["total_violations"],
                 cloud["template_false_positive"], cloud["total_normal"]))
        print("   语义层：拦住 %d/%d，误杀 %d/%d"
              % (cloud["semantic_violations"], cloud["total_violations"],
                 cloud["semantic_false_positive"], cloud["total_normal"]))
        print("   判定类别分布：%s" % cloud["categories"])
        print("   审计字段齐备：%s" % cloud["audit_present"])
        print("   判定：%s" % ("PASS" if cloud["pass"] else "FAIL"))
    else:
        print("A. 云端 A/B：未测（%s）—— 不写成跑过了" % cloud.get("reason"))
    print("\nB. 降级路径：%d/%d 通过" % (deg["passed"], deg["total"]))
    for r in deg["cases"]:
        print("   [%s] %s %s" % ("PASS" if r["pass"] else "FAIL", r["name"], r["detail"]))
    print("\nC. 变异验证：%d/%d" % (mut["passed"], mut["total"]))
    for r in mut["cases"]:
        print("   [%s] %s %s" % ("PASS" if r["pass"] else "FAIL", r["name"], r["detail"]))

    payload = {"cloud": cloud, "degradation": deg, "self_test": mut}
    if args.json:
        p = args.json if os.path.isabs(args.json) else os.path.join(ROOT, args.json)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("\n结果已写入 %s" % p)

    ok = deg["passed"] == deg["total"] and mut["passed"] == mut["total"]
    if cloud.get("available"):
        ok = ok and cloud["pass"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
