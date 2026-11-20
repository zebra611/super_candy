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
    python scripts/setup_hotwords.py --dry-run        # 只打印将提交的请求，不联网
    python scripts/setup_hotwords.py                  # 真建表（需 dashscope_api_key）
    python scripts/setup_hotwords.py --words 京东 授权书 商标   # 覆盖词源

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


def build_request(words, prefix=_PREFIX, weight=4, lang="zh"):
    """构造 create_vocabulary 请求体（纯函数，可离线测）。"""
    vocab = []
    seen = set()
    for w in words:
        w = (w or "").strip()
        if not w or w in seen:
            continue
        seen.add(w)
        vocab.append({"text": w, "weight": weight, "lang": lang})
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
    ap.add_argument("--words", nargs="+", help="覆盖词源（默认读 asr.hints）")
    ap.add_argument("--prefix", default=_PREFIX, help="热词表前缀（小写字母+数字，≤10）")
    ap.add_argument("--weight", type=int, default=4, help="热词权重 1-5，默认 4")
    ap.add_argument("--scene", default="config/scene.json", help="词源场景文件")
    args = ap.parse_args()

    words = args.words if args.words else load_hints(args.scene)
    body = build_request(words, prefix=args.prefix, weight=args.weight)
    print("热词 %d 个：%s" % (len(body["input"]["vocabulary"]),
                            "、".join(w["text"] for w in body["input"]["vocabulary"])))

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
