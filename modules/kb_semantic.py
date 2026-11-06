# -*- coding: utf-8 -*-
"""S2 可选语义召回（kb 的外挂兜底层）。

**为什么不改 kb.py 的词长加权检索**

`kb.py` 顶部已经写了结论：几十条短句、高频问法，词长加权零依赖、可解释，
每步都能用 `explain()` 摊开。S2 不是要推翻它，而是补一个**只在词检索答不上来时**
才尝试的兜底：用户换个说法问同一件事，词表没覆盖到，就用向量召回找最像的条目。

    词检索命中  → 原样返回（**一个字节都不改**，原有 40+ 断言照旧成立）
    词检索未命中 → 配置开启且有密钥时，才做一次向量召回
    任何异常     → 返回 None，调用方走原来的 reply_unknown（= 自动降级回词检索）

**默认关闭**：`knowledge.semantic` 段不存在，或 `provider != dashscope`，
本模块不发任何网络请求。密钥缺失、超时、返回乱码、维度不一致，全部吞掉返回 None。

**边界（不许夸大）**
- 这是**召回**，不是生成：只会念知识库里已有的答案，不会编造新话术。
- `no_call`（京麦等无需外呼）与有效期窗口同样过滤，语义不能绕过合规闸门。
- 阈值是在开发样本上标定的，不等于真实用户分布上的泛化准确率；
  口径见 `scripts/test_kb_semantic.py` 与 `docs/27`。
- 纯标准库实现余弦相似度，不引 numpy / 向量数据库。
"""

import json
import math
import urllib.request

import config_loader

_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
_DEFAULT_MODEL = "text-embedding-v3"
# 默认阈值必须是**保守的安全线**，不是"看着差不多"的数。
# 11-06 真实云端 A/B（scripts/test_kb_semantic.py，20 条改述 + 10 条无关对照）：
#   0.45 → 无关样本 7/10 被乱答（"今天天气挺不错的"被答成权益生效话术）
#   0.60 → 无关样本 0/10，改述正确召回 10/20、错配 2
# 语义召回是"宁可不答也不能答错"的场景：一句错话会比不答更伤。
# 调低这个数必须同时重跑误召回对照，不许只盯着召回率。
_DEFAULT_THRESHOLD = 0.60
_DEFAULT_TIMEOUT_MS = 1500
_BATCH = 10                      # 兼容接口单批上限保守取 10

_INDEX_CACHE = {}
_warned = set()


def _warn(tag, msg):
    if tag not in _warned:
        _warned.add(tag)
        print("[kb_semantic] %s" % msg)


def _conf():
    try:
        return ((config_loader.load() or {}).get("knowledge") or {}).get("semantic") or {}
    except Exception:
        return {}


def _enabled(cfg):
    return str(cfg.get("provider") or "").strip() == "dashscope"


def _embed(texts, cfg, key):
    """批量取向量；任何异常返回 None（调用方据此降级）。"""
    try:
        model = cfg.get("model") or _DEFAULT_MODEL
        timeout_s = max(0.2, float(cfg.get("timeout_ms") or _DEFAULT_TIMEOUT_MS) / 1000.0)
        endpoint = cfg.get("endpoint") or _ENDPOINT
        out = []
        for i in range(0, len(texts), _BATCH):
            chunk = texts[i:i + _BATCH]
            body = json.dumps({"model": model, "input": chunk}).encode("utf-8")
            req = urllib.request.Request(
                endpoint, data=body, method="POST",
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            rows = sorted(payload.get("data") or [], key=lambda r: r.get("index", 0))
            vecs = [r.get("embedding") for r in rows]
            if len(vecs) != len(chunk) or any(not isinstance(v, list) or not v for v in vecs):
                return None
            out.extend(vecs)
        return out
    except Exception as exc:
        _warn("embed_fail", "向量调用失败，降级回词检索：%s" % str(exc)[:80])
        return None


def _doc_of(item):
    """索引文档 = 触发词 + 类目 + 答案前 60 字。

    只用已有字段，不额外维护一份语义描述——多一份就要多一处同步，
    迟早出现"词表改了向量没改"的漂移。
    """
    parts = [str(item.get("category") or "")]
    parts.extend(str(p) for p in (item.get("_patterns") or []))
    parts.append(str(item.get("_answer") or "")[:60])
    return " ".join(x for x in parts if x)


def _build_index(kb_obj, cfg, key, day=None):
    from datetime import date
    day = day or date.today()
    cache_key = (kb_obj.path, len(kb_obj.items), cfg.get("model") or _DEFAULT_MODEL,
                 day.isoformat())
    usable = []
    for item in kb_obj.items:
        # 语义召回同样受 no_call 与有效期约束，不能绕过合规闸门
        if item.get("no_call") or not kb_obj._in_window(item, day):
            continue
        doc = _doc_of(item)
        if doc.strip():
            usable.append((item, doc))
    if not usable:
        return None
    vecs = _embed([d for _, d in usable], cfg, key)
    if not vecs or len(vecs) != len(usable):
        return None
    dim = len(vecs[0])
    if any(len(v) != dim for v in vecs):
        _warn("dim", "向量维度不一致，降级回词检索")
        return None
    index = [(it, v) for (it, _), v in zip(usable, vecs)]
    _INDEX_CACHE[cache_key] = index
    return index


def _cos(a, b):
    if len(a) != len(b):
        return -1.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return -1.0
    return dot / math.sqrt(na * nb)


def fallback(text, kb_obj, slots=None, today=None, cfg=None, key=None):
    """词检索未命中后的语义兜底。返回与 KB.match 同形状的 dict，或 None。

    cfg / key 可由测试注入；生产不传，从 config_loader 读。
    **本函数绝不 raise**：任何失败都返回 None，由调用方退回词检索口径。
    """
    try:
        cfg = cfg if cfg is not None else _conf()
        if not _enabled(cfg) or not (text or "").strip():
            return None
        if key is None:
            key = config_loader.secret("dashscope_api_key")
        if not key:
            _warn("no_key", "未配置 dashscope_api_key，语义召回保持关闭")
            return None

        index = _build_index(kb_obj, cfg, key, day=today)
        if not index:
            return None
        qv = _embed([text], cfg, key)
        if not qv:
            return None
        q = qv[0]
        threshold = float(cfg.get("threshold") or _DEFAULT_THRESHOLD)
        best, best_score = None, -1.0
        for item, vec in index:
            s = _cos(q, vec)
            if s > best_score:
                best, best_score = item, s
        if best is None or best_score < threshold:
            return None

        from modules import kb as _kb
        try:
            answer, missing = _kb.render(best["_answer"], slots)
        except ImportError:
            import kb as _kb2
            answer, missing = _kb2.render(best["_answer"], slots)
        return {
            "id": best.get("id", ""),
            "category": best.get("category", ""),
            "answer": answer,
            "source_id": best.get("source_id", ""),
            "score": round(best_score, 4),
            "matched": ["semantic"],
            "missing": missing,
            "via": "semantic",
            "semantic_model": cfg.get("model") or _DEFAULT_MODEL,
        }
    except Exception as exc:
        _warn("fallback_fail", "语义兜底异常，降级回词检索：%s" % str(exc)[:80])
        return None


def clear_cache():
    """测试用：切场景/改数据后清索引，避免拿旧向量比新条目。"""
    _INDEX_CACHE.clear()
