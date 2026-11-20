# -*- coding: utf-8 -*-
"""创建 DashScope 热词表（paraformer-realtime-v2），把 vocabulary_id 填进配置。

为什么需要这个脚本：
    paraformer-realtime-v2 的热词不是直接传词表，而是先调云端"热词管理"接口
    建一张表（target_model 必须是 paraformer-realtime-v2），拿到 vocabulary_id，
    再在 run-task 的 parameters 里带这个 id。这一步是一次性运维动作，不做在通话链路上。

词源：
    直接读 config/scene.json 的 asr.hints（京东/资质/授权书/商标…），不另造词表——
    那份 hints 本来就是为热词维护的，只是 qwen3 HTTP 接口不认它（见 _hints_todo）。

用法：
    python scripts/setup_hotwords.py --dry-run        # 只打印分层统计+请求，不联网
    python scripts/setup_hotwords.py                  # 真建表（需 dashscope_api_key）
    python scripts/setup_hotwords.py --hints-only     # 只用手工 hints（旧行为）
    python scripts/setup_hotwords.py --words 京东 授权书 商标   # 覆盖词源

词源（默认）：asr.hints + config/mock/applications.json 的商家信息，分层权重、排除数字，
    按 (权重降序, 词长升序) 裁到 500（见 collect_merchant_words 文档）。

官方依据（2026-11-20 查证）：
    POST https://dashscope.aliyuncs.com/api/v1/services/audio/asr/customization
    {"model":"speech-biasing","input":{"action":"create_vocabulary",
     "target_model":"paraformer-realtime-v2","prefix":"...",
     "vocabulary":[{"text":"京东","weight":4,"lang":"zh"}]}}
    返回 output.vocabulary_id。v2 用 vocabulary_id；phrase_id 是 v1 的。
    单表最多 500 词，weight 1-5（推荐 4）。表状态要 DEPLOY/OK 才生效。
"""

import argparse
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader  # noqa: E402

_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/customization"
_TARGET_MODEL = "paraformer-realtime-v2"
_PREFIX = "supercandy"   # 仅限小写字母和数字，≤10 字符


def load_hints(scene_file="config/scene.json"):
    """从场景配置读热词词源（asr.hints）。"""
    path = scene_file if os.path.isabs(scene_file) else os.path.join(ROOT, scene_file)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return list((cfg.get("asr") or {}).get("hints") or [])


#: 商家字段分层权重。原则（用户 2026-11-20 要求）：
#:   - 排除所有数字信息（单号/手机号/后四位/带序号品牌）——热词是静态表，
#:     数字每次通话都不同，做成热词不仅没用，还会把其他数字发音强行拉偏。
#:   - 核心短专有词（品牌/简称/通用业务词）客户核身会口头报、日常对话碰不到 → 高权重。
#:   - 人名是核查字段（verify_by_name），要进表但降权——2-3 字姓名撞音严重，
#:     低权重能帮客户报名字时对上客户卡，又不至于把正常对话强行拉成人名。
#:   - 店铺全名/公司全称是长词，ASR 热词对长词效果弱、客户少完整念 → 低权重兜底。
_W_CORE_FIELDS = ("brand_name", "company_short_name")       # 核心短专有词 → w4
_W_NAME_FIELDS = ("contact_name", "legal_representative")    # 核查人名 → w2
_W_EXT_FIELDS = ("shop_name",)                               # 店铺全名兜底 → w2
                                                             # company_name 故意不收：客户极少完整念
                                                             # 公司全称，且长词热词 ASR 几乎不生效、
                                                             # 还会触发官方 vocabulary format invalid
_W_HINTS = 5                                                 # 通用业务词必保最高权重
_W_CORE = 4
_W_NAME = 2
_W_EXT = 2
_MAX_WORDS = 500                                             # DashScope 单表硬上限
_MAX_WORD_LEN = 20                                           # 单词长度上限：超长公司全称
                                                             # 官方报 vocabulary format invalid，
                                                             # 且 ASR 对长词热词本就几乎不生效
_VALID_WORD = re.compile(r"^[\u4e00-\u9fff]+$")              # 热词只收纯中文：官方不接受全角括号
                                                             # （）· 等符号，报 vocabulary format invalid；
                                                             # 且热词本就该是可朗读的词


def _has_digit(s):
    """含任意 Unicode 数字即视为数字信息，排除。用户明确要求排除单号/手机号等。"""
    return any(ch.isdigit() for ch in s)


def collect_merchant_words(hints, app_file="config/mock/applications.json",
                           max_words=_MAX_WORDS, include_names=True):
    """从商家申请数据 + 手写 hints 构造分层热词表（纯函数，可离线测）。

    返回 (words, stats)：
        words  = [(text, weight), ...]  已按权重降序、长度升序排序并裁到 max_words
        stats  = 计数明细，便于答辩时说明"为什么这几类进表、哪类被裁"

    关键取舍：
        - 数字字段天然被排除——本函数只读白名单文本字段，且逐词过 _has_digit；
          单号(application_id)/手机号(contact_phone)/后四位(phone_tail)等根本不在白名单里。
        - 500 上限：按 (权重降序, 词长升序, 字典序) 排，核心短词先进，长词最后被裁。
        - 同一词跨类别出现（如简称=品牌）只保留最高权重。
        - include_names=False：剔除人名层。实测 500 词表（含 240 人名）会把流式字幕
          首包拖到归零（终句仍准）；人名撞音风险也最高。演示要保流式效果时关掉。
    """
    path = app_file if os.path.isabs(app_file) else os.path.join(ROOT, app_file)
    with open(path, encoding="utf-8") as f:
        items = (json.load(f).get("items") or [])

    candidates = []  # (text, weight, layer)

    def add_one(v, weight, layer):
        """单值入候选：清洗、排除数字/占位符；含 | 的多值拆开逐个处理。"""
        v = (v or "").strip()
        if not v:
            return
        for part in v.split("|"):
            part = part.strip()
            if (part and not _has_digit(part) and not part.startswith("（未申报")
                    and len(part) <= _MAX_WORD_LEN and _VALID_WORD.match(part)):
                candidates.append((part, weight, layer))

    def add_field(field, weight, layer):
        for it in items:
            add_one(it.get(field), weight, layer)

    for fld in _W_CORE_FIELDS:
        add_field(fld, _W_CORE, "core")
    if include_names:
        for fld in _W_NAME_FIELDS:
            add_field(fld, _W_NAME, "name")
    for fld in _W_EXT_FIELDS:
        add_field(fld, _W_EXT, "ext")
    # 通用业务词权重最高：它们是话术里反复说、识别错代价最大的词，
    # 绝不能在 500 裁剪里被商家的 2 字品牌挤掉（上一版就踩了这个坑）。
    for h in (hints or []):
        add_one(h, _W_HINTS, "hints")

    # 去重：同词保留最高权重；权重相同则保留先出现的层
    best = {}
    for text, weight, layer in candidates:
        if text not in best or weight > best[text][0]:
            best[text] = (weight, layer)
    # 排序：权重降序 → 词长升序（短词 ASR 利用率高）→ 字典序（稳定可复现）
    ordered = sorted(best.items(), key=lambda kv: (-kv[1][0], len(kv[0]), kv[0]))
    kept = ordered[:max_words]

    stats = {
        "candidates": len(candidates),
        "unique": len(best),
        "in_table": len(kept),
        "dropped": len(best) - len(kept),
        "by_layer": {},
    }
    for _text, (_weight, layer) in kept:
        stats["by_layer"][layer] = stats["by_layer"].get(layer, 0) + 1

    return [(text, meta[0]) for text, meta in kept], stats


def build_request(words, prefix=_PREFIX, weight=4, lang="zh"):
    """构造 create_vocabulary 请求体（纯函数，可离线测）。

    words 元素可为：
        - str：用参数 weight（默认 4）——兼容旧调用与现有测试；
        - (text, per_word_weight)：用该词自己的权重（分层热词表用）。
    """
    vocab = []
    seen = set()
    for w in words:
        if isinstance(w, (tuple, list)):
            text = str(w[0]).strip()
            wgt = int(w[1])
        else:
            text = (w or "").strip()
            wgt = weight
        if not text or text in seen:
            continue
        seen.add(text)
        vocab.append({"text": text, "weight": wgt, "lang": lang})
    if not vocab:
        raise ValueError("热词表为空——先在 asr.hints 里配词，或用 --words 传")
    if len(vocab) > 500:
        raise ValueError("单张热词表最多 500 词，当前 %d" % len(vocab))
    if not prefix.isalnum() or prefix != prefix.lower() or len(prefix) > 10:
        raise ValueError("prefix 只能是小写字母+数字，且 ≤10 字符：%r" % prefix)
    return {
        "model": "speech-biasing",
        "input": {
            "action": "create_vocabulary",
            "target_model": _TARGET_MODEL,
            "prefix": prefix,
            "vocabulary": vocab,
        },
    }


def call_api(body, timeout=20):
    key = config_loader.secret("dashscope_api_key")
    if not key:
        raise RuntimeError("没有 dashscope_api_key，无法建表")
    req = urllib.request.Request(
        _ENDPOINT, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % key}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_vocabulary_id(response):
    """从返回里抠 vocabulary_id（兼容 output.vocabulary_id 与顶层两种）。"""
    out = (response or {}).get("output") or response or {}
    vid = out.get("vocabulary_id") or out.get("VocabularyId") or ""
    return str(vid).strip()


def main():
    ap = argparse.ArgumentParser(description="创建 paraformer-realtime-v2 热词表")
    ap.add_argument("--dry-run", action="store_true", help="只打印请求，不联网")
    ap.add_argument("--words", nargs="+", help="覆盖词源（默认读 asr.hints + 商家数据）")
    ap.add_argument("--hints-only", action="store_true", help="只用手工 hints，不读商家数据（旧行为）")
    ap.add_argument("--prefix", default=_PREFIX, help="热词表前缀（小写字母+数字，≤10）")
    ap.add_argument("--weight", type=int, default=4, help="默认权重 1-5（分层词用各自的权重）")
    ap.add_argument("--scene", default="config/scene.json", help="词源场景文件")
    ap.add_argument("--apps", default="config/mock/applications.json", help="商家数据文件")
    ap.add_argument("--max-words", type=int, default=_MAX_WORDS, help="热词上限（≤500）")
    ap.add_argument("--no-names", action="store_true", help="剔除人名（保流式首包效果，实测 500 词会拖慢）")
    args = ap.parse_args()

    stats = None
    if args.words:
        words = list(args.words)
    elif args.hints_only:
        words = load_hints(args.scene)
    else:
        words, stats = collect_merchant_words(load_hints(args.scene), args.apps,
                                              max_words=min(args.max_words, _MAX_WORDS),
                                              include_names=not args.no_names)
        print("分层统计：候选 %d → 去重 %d → 入表 %d（裁剪 %d）"
              % (stats["candidates"], stats["unique"], stats["in_table"], stats["dropped"]))
        print("  各层入表：%s" % "、".join("%s=%d" % (k, v)
                                       for k, v in sorted(stats["by_layer"].items())))
        by_w = {}
        for _t, w in words:
            by_w[w] = by_w.get(w, 0) + 1
        print("  权重分布：%s" % "、".join("w%d=%d" % (k, v) for k, v in sorted(by_w.items())))
    body = build_request(words, prefix=args.prefix, weight=args.weight)
    preview = "、".join(w["text"] for w in body["input"]["vocabulary"][:15])
    print("热词 %d 个（前 15）：%s%s" % (len(body["input"]["vocabulary"]), preview,
                                       "…" if len(body["input"]["vocabulary"]) > 15 else ""))

    if args.dry_run:
        print("\n--- dry-run：将提交的请求 ---")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        print("\n（未联网。真建表去掉 --dry-run）")
        return 0

    try:
        resp = call_api(body)
    except Exception as exc:
        print("建表失败：%s: %s" % (type(exc).__name__, exc))
        return 1
    vid = extract_vocabulary_id(resp)
    if not vid:
        print("建表返回里没找到 vocabulary_id，原始返回：%s"
              % json.dumps(resp, ensure_ascii=False)[:500])
        return 1
    print("\n建表成功！vocabulary_id = %s" % vid)
    print("下一步：把这个 id 填进四个场景配置的 asr.hotwords.vocabulary_id；")
    print("并确认热词表状态为 DEPLOY/OK（UNDEPLOYED 会被 ASR 静默忽略）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
