# -*- coding: utf-8 -*-
"""S2 语义召回评测：20 条改述 + 10 条无关问句，A/B 词检索与向量召回。

跑法：
    python scripts/test_kb_semantic.py                     # 有密钥时云端 A/B + 离线降级 + 变异
    python scripts/test_kb_semantic.py --offline           # CI / 无网：只验降级与变异
    python scripts/test_kb_semantic.py --json data/kb_semantic_latest.json

**口径（防假绿）**
- 「召回不低于词检索」不是把词检索结果换掉，而是**词检索命中原样保留，
  只在它答不上来时用语义补**。所以判定用 combined_correct >= word_correct，
  而不是拿语义版单独去比。
- 只看召回会假绿：把阈值调到 0，每条都能"命中"，但会把无关问句也乱答。
  因此同时统计 **10 条无关样本的误召数**，要求为 0。
- 20 条改述里至少 12 条必须词检索**答不上来**，否则样本没有区分度，脚本直接报 2。
- 阈值是在这同一批样本上标定的，报告必须写「开发集标定，非泛化准确率」。
"""

import argparse
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader                              # noqa: E402
from modules import kb, kb_semantic               # noqa: E402

KB_PATH = os.path.join(ROOT, "config", "kb.json")

# (改述问法, 期望命中的条目 id)。刻意避开该条目的原 patterns 关键词。
REWRITES = [
    ("会不会从我这儿扣银子", "KB-FEE-001"),
    ("能管到明年这时候吗", "KB-VALIDITY-001"),
    ("登记完是不是还得等审批", "KB-TRIGGER-001"),
    ("东西坏了你们负责哪一块", "KB-COVERAGE-001"),
    ("出毛病了该打给谁", "KB-REPAIR-001"),
    ("不想参与了这个能撤吗", "KB-CANCEL-001"),
    ("账户里少了一笔能给我查吗", "KB-REFUND-001"),
    ("你们从哪儿搞到我的手机号", "KB-PRIVACY-001"),
    ("干嘛非要核对那几个数字", "KB-IDENTITY-001"),
    ("又是来卖东西的吧", "KB-SALES-001"),
    ("给我张单据拿去单位报账行吗", "KB-INVOICE-001"),
    ("上次登记过一回会不会又记一遍", "KB-DUPLICATE-001"),
    ("我不想跟机器说话", "KB-TRANSFER-001"),
    ("我要去给你们写差评找报社", "KB-COMPLAINT-001"),
    ("一年之后是不是自动接着往下走", "KB-RENEW-001"),
    ("你们背后是哪家机构啊", "KB-OWNER-001"),
    ("夜里十点还有人接电话吗", "KB-HOURS-001"),
    ("技术员过来大概要等几天", "KB-ONSITE-001"),
    ("今天登记今天就能使吗", "KB-URGENT-001"),
    ("我只想留下其中一块行不行", "KB-PARTIAL-001"),
]

# 明显与业务无关、应当走 reply_unknown 的问句（误召回对照）。
IRRELEVANT = [
    "今天天气挺不错的",
    "你们中午吃的什么",
    "帮我念一下今天的日期",
    "这个声音是男声还是女声",
    "最近股票行情怎么样",
    "附近有停车场吗",
    "能不能给我唱首歌",
    "我这边手机信号不太好",
    "你说的是普通话吗",
    "隔壁老王的生日是哪天",
]

THRESHOLDS = [0.30, 0.40, 0.45, 0.50, 0.60, 0.65]
CFG = {"provider": "dashscope", "model": "text-embedding-v3",
       "threshold": kb_semantic._DEFAULT_THRESHOLD, "timeout_ms": 1500}
ALL_QUERIES = [q for q, _ in REWRITES] + IRRELEVANT


def _patch(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    return old


def _word_hit(q):
    r = kb.load().match(q)
    return (r or {}).get("id") or ""


def validate_samples():
    problems = []
    if len(REWRITES) != 20:
        problems.append("改述样本应为 20 条，实际 %d" % len(REWRITES))
    if len(IRRELEVANT) < 10:
        problems.append("无关对照样本不足 10 条")
    if len(set(ALL_QUERIES)) != len(ALL_QUERIES):
        problems.append("样本里有重复文本")
    ids = {it.get("id") for it in kb.load().items}
    for q, want in REWRITES:
        if want not in ids:
            problems.append("%r 的期望 id %s 不在知识库" % (q, want))
    miss = sum(1 for q, _ in REWRITES if not _word_hit(q))
    if miss < 12:
        problems.append("只有 %d 条改述词检索答不上来（需 ≥12），样本没区分度" % miss)
    bad_irr = [q for q in IRRELEVANT if _word_hit(q)]
    if bad_irr:
        problems.append("无关样本被词检索命中，样本设计错误：%s" % bad_irr)
    return problems, miss


class _FakeResp:
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


# --------------------------------------------------------------------------
# A. 云端 A/B
# --------------------------------------------------------------------------
def cloud_eval():
    key = config_loader.secret("dashscope_api_key")
    if not key:
        return {"available": False, "reason": "未配置 dashscope_api_key，云端未测"}
    kb_semantic.clear_cache()
    kbo = kb.load()
    index = kb_semantic._build_index(kbo, CFG, key)
    if not index:
        return {"available": False, "reason": "向量索引构建失败（调用异常），云端未测"}
    qvecs = kb_semantic._embed(ALL_QUERIES, CFG, key)
    if not qvecs or len(qvecs) != len(ALL_QUERIES):
        return {"available": False, "reason": "查询向量获取失败，云端未测"}

    bests = []
    for q in qvecs:
        bid, bs = max(((it.get("id", ""), kb_semantic._cos(q, v)) for it, v in index),
                      key=lambda t: t[1])
        bests.append({"id": bid, "cos": round(bs, 4)})
    word = [_word_hit(q) for q in ALL_QUERIES]
    word_correct = sum(1 for i, (_, want) in enumerate(REWRITES) if word[i] == want)

    grid = []
    for t in THRESHOLDS:
        correct = sum(1 for i, (_, want) in enumerate(REWRITES)
                      if bests[i]["cos"] >= t and bests[i]["id"] == want)
        wrong = sum(1 for i, (_, want) in enumerate(REWRITES)
                    if bests[i]["cos"] >= t and bests[i]["id"] != want)
        combined = sum(1 for i, (_, want) in enumerate(REWRITES)
                       if word[i] == want
                       or (bests[i]["cos"] >= t and bests[i]["id"] == want))
        false_positive = sum(1 for i in range(len(REWRITES), len(ALL_QUERIES))
                             if bests[i]["cos"] >= t)
        grid.append({"threshold": t, "semantic_correct": correct,
                     "semantic_wrong": wrong, "combined_correct": combined,
                     "false_positive": false_positive,
                     "pass": combined >= word_correct and false_positive == 0})

    usable = [g for g in grid if g["false_positive"] == 0]
    chosen = (max(usable, key=lambda g: g["combined_correct"])["threshold"]
              if usable else None)

    # 真实 kb.match 路径验证：确认不是"只在脚本里算了一遍"。
    # 这里**故意不传 threshold**，走生产默认 _DEFAULT_THRESHOLD，
    # 验的正是"有人直接开 semantic 但不配阈值"这条最常见的路径。
    path_check = {"available": False}
    default_cfg = {k: v for k, v in CFG.items() if k != "threshold"}
    if chosen is not None:
        idx = next((i for i, (q, want) in enumerate(REWRITES)
                    if not word[i] and bests[i]["cos"] >= chosen
                    and bests[i]["id"] == want), None)
        if idx is not None:
            p = _patch(kb_semantic, "_conf", lambda: default_cfg)
            try:
                got = kb.match(ALL_QUERIES[idx])
                path_check = {
                    "available": True, "query": ALL_QUERIES[idx],
                    "want": REWRITES[idx][1], "threshold": "生产默认",
                    "got_id": (got or {}).get("id"),
                    "via": (got or {}).get("via"),
                    "answer_from_kb": bool(got and got.get("answer") in
                                          [it["_answer"] for it in kbo.items]),
                    "pass": bool(got and got.get("id") == REWRITES[idx][1]
                                 and got.get("via") == "semantic"),
                }
            finally:
                _patch(kb_semantic, "_conf", p)

    return {"available": True, "model": CFG["model"], "index_items": len(index),
            "word_correct": word_correct,
            "word_miss_in_rewrites": sum(1 for i in range(len(REWRITES)) if not word[i]),
            "grid": grid, "chosen_threshold": chosen, "path_check": path_check,
            "per_query": [{"query": ALL_QUERIES[i], "word": word[i],
                           "semantic": bests[i]["id"], "cos": bests[i]["cos"]}
                          for i in range(len(ALL_QUERIES))]}


# --------------------------------------------------------------------------
# B. 降级路径（离线、确定性）
# --------------------------------------------------------------------------
def degradation_probe():
    kbo = kb.load()
    base = [kbo.match(q) for q in ALL_QUERIES]
    results = []

    def case(name, ok, detail=""):
        results.append({"name": name, "pass": bool(ok), "detail": detail})

    def same_as_word(tag, cfg_obj, patches=()):
        """在指定配置下逐字节比 30 条样本的 kb.match 与纯词检索。"""
        handles = [_patch(kb_semantic, "_conf", cfg_obj)]
        for obj, name, val in patches:
            handles.append((_patch(obj, name, val), obj, name))
        try:
            got = [kb.match(q) for q in ALL_QUERIES]
            return got == base, sum(1 for a, b in zip(got, base) if a != b)
        finally:
            for h, obj, name in reversed(handles[1:]):
                setattr(obj, name, h)
            setattr(kb_semantic, "_conf", handles[0])

    # D0 默认关闭且零网络调用
    calls = [0]
    orig = urllib.request.urlopen

    def counting(req, timeout=None):
        calls[0] += 1
        return orig(req, timeout=timeout)

    ok, diff = same_as_word("D0", lambda: {}, [(urllib.request, "urlopen", counting)])
    case("D0 默认关闭：零网络调用，30 条样本与词检索逐字节一致",
         ok and calls[0] == 0, "diff=%d calls=%d" % (diff, calls[0]))

    # D1 开启但无密钥
    ok, diff = same_as_word("D1", lambda: dict(CFG),
                            [(config_loader, "secret", lambda n, d="": "")])
    case("D1 开启但无密钥 → 自动降级回词检索", ok, "diff=%d" % diff)

    # D2 密钥读取抛异常
    def bad_secret(n, d=""):
        raise OSError("simulated secrets failure")

    ok, diff = same_as_word("D2", lambda: dict(CFG),
                            [(config_loader, "secret", bad_secret)])
    case("D2 密钥读取异常不外抛，降级回词检索", ok, "diff=%d" % diff)

    # D3 向量端点超时
    def boom(req, timeout=None):
        raise TimeoutError("simulated embedding timeout")

    ok, diff = same_as_word("D3", lambda: dict(CFG),
                            [(urllib.request, "urlopen", boom)])
    case("D3 向量调用超时 → 降级回词检索", ok, "diff=%d" % diff)

    # D4 返回乱码
    ok, diff = same_as_word("D4", lambda: dict(CFG),
                            [(urllib.request, "urlopen",
                              lambda req, timeout=None: _FakeResp(b"not json"))])
    case("D4 向量响应无法解析 → 降级回词检索", ok, "diff=%d" % diff)

    # D5 no_call 合规闸门：语义不能绕过京麦过滤
    fake = kb.KB({"items": [
        {"id": "JM-NOCALL", "patterns": ["京麦"], "answer": "请在京麦后台操作",
         "source_id": "x", "no_call": True},
        {"id": "OK", "patterns": ["品牌"], "answer": "正常话术", "source_id": "y"},
    ]}, path="(fake-nocall)")
    kb_semantic.clear_cache()
    p1 = _patch(kb_semantic, "_embed", lambda texts, c, k: [[1.0, 0.0] for _ in texts])
    try:
        got = kb_semantic.fallback("京麦后台怎么弄", fake, cfg=dict(CFG), key="fake")
        case("D5 no_call 条目不被语义召回（合规闸门）",
             got is not None and got.get("id") != "JM-NOCALL",
             "got=%r" % (got or {}).get("id"))
    finally:
        _patch(kb_semantic, "_embed", p1)
    kb_semantic.clear_cache()

    # D6 词检索命中时语义根本不介入
    kb_semantic.clear_cache()
    p1 = _patch(kb_semantic, "_conf", lambda: dict(CFG))
    p2 = _patch(kb_semantic, "_embed", lambda texts, c, k: [[1.0, 0.0] for _ in texts])
    try:
        got = kb.match("要钱不")
        raw = kbo.match("要钱不")
        case("D6 词检索命中时语义不介入（原有答案逐字节不变）",
             got == raw and not (got or {}).get("via"),
             "id=%r via=%r" % ((got or {}).get("id"), (got or {}).get("via")))
    finally:
        _patch(kb_semantic, "_embed", p2)
        _patch(kb_semantic, "_conf", p1)
    kb_semantic.clear_cache()

    # D7 反向：语义层确实能召回（防"压根没生效"假绿）
    kb_semantic.clear_cache()
    p1 = _patch(kb_semantic, "_embed", lambda texts, c, k: [[1.0, 0.0] for _ in texts])
    try:
        got = kb_semantic.fallback("会不会从我这儿扣银子", kbo,
                                   cfg=dict(CFG), key="fake")
        case("D7 反向：注入同向向量时语义层确实返回结果",
             bool(got) and got.get("via") == "semantic",
             "id=%r" % (got or {}).get("id"))
    finally:
        _patch(kb_semantic, "_embed", p1)
    kb_semantic.clear_cache()

    # D8 默认阈值必须是实测安全线（离线可验，防止有人只盯召回率把它调回去）
    case("D8 生产默认阈值 ≥0.60（0.45 时无关样本 7/10 被乱答）",
         kb_semantic._DEFAULT_THRESHOLD >= 0.60,
         "当前 %.2f" % kb_semantic._DEFAULT_THRESHOLD)

    return {"total": len(results), "passed": sum(1 for r in results if r["pass"]),
            "cases": results}


# --------------------------------------------------------------------------
# C. 变异验证：降级断言必须能变红
# --------------------------------------------------------------------------
def self_test():
    kbo = kb.load()
    out = []

    # M1：语义层在异常时仍返回假命中 → D0 的逐字节断言必须失效
    p1 = _patch(kb_semantic, "_conf", lambda: dict(CFG))
    p2 = _patch(kb_semantic, "fallback",
                lambda *a, **kw: {"id": "FAKE", "via": "semantic", "answer": "假"})
    try:
        changed = kb.match("今天天气挺不错的") != kbo.match("今天天气挺不错的")
        out.append({"name": "M1 语义层不降级时逐字节断言必须变红", "pass": changed})
    finally:
        _patch(kb_semantic, "fallback", p2)
        _patch(kb_semantic, "_conf", p1)

    # M2：把 no_call 条目塞进索引 → D5 合规断言必须失效
    fake = kb.KB({"items": [
        {"id": "JM-NOCALL", "patterns": ["京麦"], "answer": "请在京麦后台操作",
         "source_id": "x", "no_call": True},
        {"id": "OK", "patterns": ["品牌"], "answer": "正常话术", "source_id": "y"},
    ]}, path="(fake-mutation)")
    kb_semantic.clear_cache()
    p1 = _patch(kb_semantic, "_build_index",
                lambda kbo_, cfg, key, day=None: [(kbo_.items[0], [1.0, 0.0]),
                                                  (kbo_.items[1], [1.0, 0.0])])
    p2 = _patch(kb_semantic, "_embed", lambda texts, c, k: [[1.0, 0.0] for _ in texts])
    try:
        got = kb_semantic.fallback("京麦后台怎么弄", fake, cfg=dict(CFG), key="fake")
        changed = bool(got) and got.get("id") == "JM-NOCALL"
        out.append({"name": "M2 绕过 no_call 时合规断言必须变红", "pass": changed})
    finally:
        _patch(kb_semantic, "_embed", p2)
        _patch(kb_semantic, "_build_index", p1)
    kb_semantic.clear_cache()

    return {"total": len(out), "passed": sum(1 for r in out if r["pass"]),
            "cases": out}


# --------------------------------------------------------------------------
def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    kb.set_path(KB_PATH)
    problems, miss = validate_samples()
    if problems:
        print("样本集有问题：")
        for p in problems:
            print("  - " + p)
        return 2

    cloud = ({"available": False, "reason": "--offline 指定，云端未测"}
             if args.offline else cloud_eval())
    deg = degradation_probe()
    mut = self_test()

    print("=" * 78)
    print("S2 kb 语义召回评测（20 条改述 + 10 条无关对照）")
    print("=" * 78)
    print("样本区分度：20 条改述里 %d 条词检索答不上来；10 条无关样本词检索全部不命中" % miss)
    if cloud.get("available"):
        print("\nA. 云端 A/B（text-embedding-v3，索引 %d 条）" % cloud["index_items"])
        print("   词检索正确命中：%d/20" % cloud["word_correct"])
        print("   %-8s %-10s %-10s %-12s %-8s %s"
              % ("阈值", "语义正确", "语义错配", "复合正确", "无关误召", "判定"))
        for g in cloud["grid"]:
            print("   %-8.2f %-10d %-10d %-12d %-8d %s"
                  % (g["threshold"], g["semantic_correct"], g["semantic_wrong"],
                     g["combined_correct"], g["false_positive"],
                     "PASS" if g["pass"] else "FAIL"))
        print("   选定阈值：%s（误召为 0 中复合正确最高；开发集标定，非泛化准确率）"
              % cloud["chosen_threshold"])
        pc = cloud["path_check"]
        if pc.get("available"):
            print("   真实 kb.match 路径：%s → id=%s via=%s 答案取自知识库=%s [%s]"
                  % (pc["query"][:16], pc["got_id"], pc["via"],
                     pc["answer_from_kb"], "PASS" if pc["pass"] else "FAIL"))
    else:
        print("\nA. 云端 A/B：未测（%s）—— 不写成跑过了" % cloud.get("reason"))
    print("\nB. 降级路径：%d/%d 通过" % (deg["passed"], deg["total"]))
    for r in deg["cases"]:
        print("   [%s] %s %s" % ("PASS" if r["pass"] else "FAIL", r["name"], r["detail"]))
    print("\nC. 变异验证：%d/%d 断言能变红" % (mut["passed"], mut["total"]))
    for r in mut["cases"]:
        print("   [%s] %s" % ("PASS" if r["pass"] else "FAIL", r["name"]))

    payload = {"cloud": cloud, "degradation": deg, "self_test": mut,
               "sample_note": "阈值在同一批开发样本上标定，非泛化准确率"}
    if args.json:
        path = args.json if os.path.isabs(args.json) else os.path.join(ROOT, args.json)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print("\n结果已写入 %s" % path)

    ok = deg["passed"] == deg["total"] and mut["passed"] == mut["total"]
    if cloud.get("available"):
        ok = ok and any(g["pass"] for g in cloud["grid"]) \
            and cloud["path_check"].get("pass", False)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
