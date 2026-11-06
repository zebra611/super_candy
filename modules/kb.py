# -*- coding: utf-8 -*-
"""知识条目库：让"回答业务问题"从写死话术变成可检索、可溯源、可校验的条目。

**为什么要这个文件**

外呼里用户问的问题只有一小类能预先写死（"要钱吗""能用多久"），
其余几十种问法（"什么时候生效""能退吗""你们是不是骗子"）现在全部掉进
`reply_unknown`——答不上来，等于把"知识问答"这个评分点让出去。

`config/scene.json` 里原来只有两处：`answers`（关键词表）和
`stages.OFFER.reply_ask_*`（答案正文）。**关键词和答案分居两地**，
每加一类问题要改两个地方，而且答案没有出处、没有有效期、匹配不分轻重——
"多久" 这种两字词只要出现在句子里就命中，用户问"多久能退款"也会被答成"有效期十二个月"。

这个模块把三件事一次解决：

1. **一条 = 一个 dict**：`patterns`（问法）+ `answer`（答法）+ `source_id`（出处）
   + `valid_from/valid_to`（有效期）+ `forbidden`（禁用语）写在一起，不再分居两处；
2. **匹配有分数**：命中的关键词越长权重越高，长词压短词，
   低于 `min_len` 的单字命中直接不算——宁可不答，也不能答错；
3. **可溯源**：返回结果带 `source_id`，赛后/答辩可以直接回答"这句话依据哪份资料"。

**为什么不用向量库**

`Dockerfile` 明确"无 requirements.txt，纯标准库"。引一个 embedding 模型要带
几百 MB 依赖 + 一次网络往返，而外呼场景全部答法都是短句、问题都是高频有限集，
关键词加权足够。真需要语义匹配时再换，接口不用动（`match()` 返回结构不变）。

**口径要说准**：这里是"词长加权"的规则检索，不是 TF-IDF / BM25——
打分只有一维（最长命中词×100 + 命中词长之和），没有词频、没有逆文档频率，
匹配方式是子串包含。对外不要写成"加权检索"就完事，被追问 IDF 时会答不上来。
这个选择在小规模知识库（几十条、高频问法）上是优点：零依赖、可解释、
每一步都能用 `explain()` 摊开给评委看。

**降级原则**

`config/kb.json` 不存在时，本模块**不报错**，`enabled()` 返回 False，
`match()` 返回 None，调用方退回原来的关键词表。这样这个模块可以先合进去，
等 4 号把内容文件落位再生效——不会出现"代码合了但服务起不来"。
"""

import json
import os
import re
import unicodedata
from datetime import date

# 条目必填字段。缺任何一个都视为配置错误，加载时就报，不要等到线上答错才发现。
REQUIRED_FIELDS = ("id", "patterns", "answer", "source_id")

# 可选字段及其默认值
DEFAULTS = {
    "category": "",
    "valid_from": None,
    "valid_to": None,
    "forbidden": [],
    "priority": 0,
    # no_call=True 的条目不进电话检索（如京麦驳回域：官方口径该场景无需外呼）。
    # 条目仍在库里（explain/统计可见），只是 match 永远不返回它——
    # 否则商家在电话里问"品牌"会被念一段"请在京麦后台操作"的站内指引。
    "no_call": False,
}

# 全项目共用的禁用语：这些词一旦出现在答案里就构成"承诺"，合规上不能播。
# 条目自己的 forbidden 是在此之上的追加，不是替代。
DEFAULT_FORBIDDEN = ("保证", "百分百", "100%", "绝对", "一定能", "包您")

_CACHE = {}
_CACHE_PATH = {}


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 官方话术里的占位符：【】、【XX】、【XXX】、【XXXX】。
# 资料包说明里明确提到"占位符有两套体系，说明来自不同编写批次"，
# 所以括号里是什么都接受（含空括号），一律按"括号内的名字"去找槽位值。
_PLACEHOLDER = re.compile(r"【([^】]*)】")


def render(answer, slots=None):
    """把话术里的【】占位符按槽位填充，返回 (文本, 没填上的占位符列表)。（C2）

    策略沿用赛题资料包给的顺序：**先原样念，后槽位填充**。
    填不上的占位符原样保留在句子里，而不是删掉或替换成"某某"——
        - 删掉会改变句子语法（"申请【XX】品牌的资质" → "申请品牌的资质"）；
        - 换成"某某"等于编一个具体对象出来，评委一问就穿。
    原样保留读起来别扭，但信息是诚实的；`missing` 会把它暴露出来，
    运营补一条槽位就能修好，不需要改代码。

    参数：
        answer  话术原文
        slots   可用槽位，例如 {"brand": "米家"}；None 或空 dict 等价于"全部没填"
    返回：
        (渲染后的文本, 没填上的占位符名字列表)
    """
    slots = slots or {}
    missing = []

    def _sub(m):
        name = (m.group(1) or "").strip()
        if name and str(slots.get(name, "")).strip():
            return str(slots[name])
        missing.append(name or "（空占位符）")
        return m.group(0)

    return _PLACEHOLDER.sub(_sub, answer or ""), missing


def _norm(text):
    """归一化：全角转半角、去标点、去空白、转小写。

    为什么要这么狠：ASR 出的文本标点很不稳定（"收费吗？" / "收费吗" /
    "收费 吗" 都会出现），不归一化的话关键词匹配率会掉得莫名其妙。
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    # NFKC 之后中文标点仍是全角，逐个去掉
    t = re.sub(r"[\s\u3000]+", "", t)
    t = re.sub(r"[，。！？、；：\"'（）《》【】…—·,.!?;:()\[\]{}<>/\\|@#$%^&*\-_=+`~]", "", t)
    return t


def _parse_date(v):
    """把 'YYYY-MM-DD' 解析成 date；空值/非法值一律当"无限制"。"""
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip())
    except ValueError:
        return None


def _default_path():
    """按当前生效的 scene 推断 kb.json 的位置。

    优先读 `config_loader.load()["knowledge"]["path"]`（4 号加的配置段）；
    没有就取 scene.json 同目录下的 `knowledge.file`（默认 kb.json）。
    这样切场景（`set_scene`）时知识库会跟着切，不用改代码。
    """
    try:
        import config_loader
        cfg = config_loader.load() or {}
        conf = cfg.get("knowledge") or {}
        if conf.get("path"):
            p = str(conf["path"])
            return p if os.path.isabs(p) else os.path.join(_repo_root(), p)
        fname = conf.get("file") or "kb.json"
        return os.path.join(os.path.dirname(config_loader.scene_path()), fname)
    except Exception:
        # config_loader 不可用（单测、脚本独立跑）时的兜底
        return os.path.join(_repo_root(), "config", "kb.json")


def _conf():
    """取 `scene.json` 的 `knowledge` 段。取不到就当空配置。"""
    try:
        import config_loader
        return (config_loader.load() or {}).get("knowledge") or {}
    except Exception:
        return {}


def _cfg_int(key, default):
    v = _conf().get(key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _min_len():
    """最短命中词长。低于它的命中不算数（单字关键词会把无关句子吃进来）。"""
    return max(1, _cfg_int("min_len", 2))


def _min_matches():
    """至少要命中几个关键词。

    配置里叫 `min_score`（4 号按需求单起的名字），语义是"命中几个词才算数"，
    不是分数阈值。分数只用于排序、不用于判门槛——门槛定在"命中几个词"上，
    调内容的人不用理解打分公式，也能判断自己写的问法够不够。
    """
    return max(1, _cfg_int("min_score", 1))


def _max_answer_chars():
    """答案长度上限（0 = 不限）。只用于校验告警，**不截断**：
    播报内容被程序悄悄改短，比超长更危险。"""
    return max(0, _cfg_int("max_answer_chars", 0))


class KB(object):
    """一份已加载的知识库。线程内缓存，按路径区分。"""

    def __init__(self, data, path="(memory)"):
        self.path = path
        self.raw = data or {}
        self.meta = self.raw.get("_meta") or {}
        self.items = []
        for it in (self.raw.get("items") or []):
            item = dict(DEFAULTS)
            item.update(it)
            item["_patterns"] = [_norm(p) for p in (item.get("patterns") or []) if _norm(p)]
            item["_answer"] = str(item.get("answer") or "")
            item["_from"] = _parse_date(item.get("valid_from"))
            item["_to"] = _parse_date(item.get("valid_to"))
            self.items.append(item)

    # ---------- 查询 ----------

    def _candidates(self, text, today=None):
        """给每个条目打分，返回按分数降序的候选列表（含未命中原因）。

        分数 = 最长命中词长度 × 100 + 命中词长度之和。
        为什么让"最长命中词"一票决定：用户问"多久能退款"，
        条目 A 命中"多久"(2)，条目 B 命中"退款"(2)、"退"(1)——
        两者最长命中都是 2，此时再看总和。而如果 B 有"退款流程"(4)，
        它就该赢，因为四个字的匹配比两个字的匹配更可能指的是同一件事。
        """
        t = _norm(text)
        day = today or date.today()
        out = []
        if not t:
            return out
        mn = _min_len()
        mm = _min_matches()
        for item in self.items:
            if item.get("no_call"):
                # 检索层过滤（京麦等无需外呼的条目）：带原因入候选，
                # explain 能看到"为什么不答"，match 只取 reason 为空的候选。
                out.append({
                    "id": item.get("id", ""),
                    "category": item.get("category", ""),
                    "answer": item["_answer"],
                    "source_id": item.get("source_id", ""),
                    "score": 0, "matched": [], "reason": "no_call",
                    "_priority": item.get("priority", 0),
                })
                continue
            matched = [p for p in item["_patterns"] if p in t]
            longest = max((len(p) for p in matched), default=0)
            score = longest * 100 + sum(len(p) for p in matched)
            reason = ""
            if not matched:
                reason = "no_pattern"
            elif longest < mn:
                reason = "too_short"
            elif len(matched) < mm:
                reason = "too_few"
            elif not self._in_window(item, day):
                reason = "expired"
            out.append({
                "id": item.get("id", ""),
                "category": item.get("category", ""),
                "answer": item["_answer"],
                "source_id": item.get("source_id", ""),
                "score": score,
                "matched": matched,
                "reason": reason,
                "_priority": item.get("priority", 0),
            })
        out.sort(key=lambda c: (c["reason"] != "", -c["score"], -c["_priority"], c["id"]))
        return out

    @staticmethod
    def _in_window(item, day):
        if item["_from"] and day < item["_from"]:
            return False
        if item["_to"] and day > item["_to"]:
            return False
        return True

    def match(self, text, today=None, slots=None):
        """命中就返回一条结果，命中不了返回 None。

        返回 dict：id / answer / source_id / score / matched / category / missing。
        调用方**只看有没有返回值**，不要自己再判阈值——阈值逻辑收在这里，
        否则每个调用点都要重复一遍判断，早晚会不一致。

        missing 是"这句答案里还有哪几个占位符没填上"（C2）。
        正常的知识条目没有占位符，它是空列表。
        """
        for c in self._candidates(text, today):
            if not c["reason"]:
                c.pop("_priority", None)
                c["answer"], c["missing"] = render(c["answer"], slots)
                return c
        return None

    def explain(self, text, top=3, today=None):
        """把打分过程摊开给人和测试看。

        用途有两个：调关键词时不用猜；评委问"你怎么保证不是瞎答"时
        可以直接展示"命中了哪个词、得分多少、为什么没命中"。
        """
        rows = self._candidates(text, today)[:max(1, top)]
        for r in rows:
            r.pop("_priority", None)
        return rows

    # ---------- 自检 ----------

    def validate(self):
        """返回问题列表；空列表代表这份知识库可以直接上线。

        检查项都是踩过的坑：
            - 必填字段缺失 → 运行时 KeyError 或答空
            - id 重复 → 统计口径对不上
            - patterns 为空 → 永远命中不了，等于白写
            - 答案含禁用语 → 合规上一票否决
            - valid_to 早于 valid_from → 永远不生效，且不报错
            - source_id 为空 → 溯源性为零，等于没有
        """
        problems = []
        seen = set()
        ids = set()
        for it in self.items:
            iid = it.get("id") or "(无 id)"
            for f in REQUIRED_FIELDS:
                if not it.get(f):
                    problems.append("%s：缺少必填字段 %s" % (iid, f))
            if iid in ids:
                problems.append("%s：id 重复" % iid)
            ids.add(iid)
            if not it["_patterns"]:
                problems.append("%s：patterns 为空或全是空白" % iid)
            for p in it["_patterns"]:
                seen.add(p)
            text = it["_answer"]
            for w in tuple(DEFAULT_FORBIDDEN) + tuple(it.get("forbidden") or []):
                if w and w in text:
                    problems.append("%s：答案含禁用语「%s」" % (iid, w))
            if it["_from"] and it["_to"] and it["_to"] < it["_from"]:
                problems.append("%s：valid_to 早于 valid_from，永远不会生效" % iid)
            if it["_from"] or it["_to"]:
                if not it["_from"]:
                    problems.append("%s：设了 valid_to 却没设 valid_from" % iid)
            cap = _max_answer_chars()
            if cap and len(text) > cap:
                problems.append("%s：答案 %d 字，超过 knowledge.max_answer_chars（%d）"
                                % (iid, len(text), cap))
        # 关键词被多条共用 → 排名靠"最长命中"兜不住，容易出现同分随机
        dup = {}
        for it in self.items:
            for p in it["_patterns"]:
                dup.setdefault(p, []).append(it.get("id", ""))
        for p, owners in dup.items():
            if len(owners) > 1:
                problems.append("关键词「%s」被多条共用：%s（命中会并列）" % (p, "、".join(owners)))
        return problems

    def conflicts(self):
        """找出"短词抢占"风险，返回告警列表（不阻断上线，但要有人看）。

        场景：条目 A 的关键词是「多久」（两字，够阈值），
        条目 B 的关键词是「多久能上门」。用户问"多久能上门"时，
        A 和 B 都会命中，B 因为词更长而胜出——这次没事；
        但如果用户说的是"多久上门"，B 也能命中，仍然没事；
        真正危险的是**用户说法只覆盖 A 的短词、却在问 B 的事**，
        比如"这个多久"落在上门语境里时，A 会安静地把答案答错。

        这个检查不能自动化解决，只能提示内容作者：看到告警就去补一条
        更长的同义说法，或者把 A 的短词换成更具体的词。

        实测踩过的例子：问"多久能上门"，被「多久」（有效期）抢走。
        """
        warns = []
        shorts = [(it.get("id", ""), p) for it in self.items
                  for p in it["_patterns"] if len(p) <= 2]
        for iid, p in shorts:
            for other in self.items:
                if other.get("id") == iid:
                    continue
                for q in other["_patterns"]:
                    if len(q) > len(p) and p in q:
                        warns.append("「%s」的「%s」被「%s」的「%s」包含"
                                     % (iid, p, other.get("id"), q))
        return sorted(set(warns))

    def stats(self):
        cats = {}
        sources = set()
        for it in self.items:
            cats[it.get("category") or "(未分类)"] = cats.get(it.get("category") or "(未分类)", 0) + 1
            if it.get("source_id"):
                sources.add(it["source_id"])
        return {
            "path": self.path,
            "items": len(self.items),
            "categories": cats,
            "sources": len(sources),
            "placeholder": bool(self.meta.get("placeholder")),
            "version": self.meta.get("version", ""),
        }

    def is_empty(self):
        return not self.items


# ---------- 模块级接口 ----------

def load(path=None, force=False):
    """加载知识库（带缓存）。文件不存在返回一个空 KB，不抛异常。

    路径优先级：显式参数 > `set_path()` 的override > 按 scene 推断。
    这里必须把 override 算进来——否则 `set_path()` 只在当次调用有效，
    下一次 `match()` 又悄悄读回默认文件，测试会"看起来通过但测的是别的库"。
    """
    p = os.path.abspath(path or _CACHE_PATH.get("override") or _default_path())
    if not force and p in _CACHE:
        return _CACHE[p]
    if not os.path.exists(p):
        kb = KB({"items": []}, path=p)
        _CACHE[p] = kb
        return kb
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    kb = KB(data, path=p)
    _CACHE[p] = kb
    return kb


def set_path(path):
    """切换知识库文件（测试、多场景切换用）。传 None 表示恢复默认推断。"""
    if path is None:
        _CACHE_PATH.pop("override", None)
        return load(force=True)
    _CACHE_PATH["override"] = path
    return load(path, force=True)


def current_path():
    return _CACHE_PATH.get("override") or _default_path()


def enabled():
    """知识库是否可用：配置没关，且文件里确实有内容。

    两个条件都要满足。只看文件会忽略 `knowledge.enabled = false`——
    那是运维的紧急开关（线上某条答案出事时要能一句话关掉，不用改代码）。
    """
    if _conf().get("enabled") is False:
        return False
    return not load().is_empty()


def match(text, today=None, slots=None):
    """便捷入口：命中返回 dict，否则 None。

    关掉知识库时直接返回 None，调用方自动退回旧的关键词话术——
    这样"关开关"和"删文件"两条路行为一致，运维不用记两种操作。

    slots 用于填充话术里的【】占位符（C2）。不传时占位符原样保留，
    与接入前逐字节一致——现有 kb.json 的答案里没有【】，所以默认路径不变。
    """
    if not enabled():
        return None
    return load().match(text, today=today, slots=slots)


def explain(text, top=3, today=None):
    return load().explain(text, top=top, today=today)


def stats():
    return load().stats()


def validate():
    return load().validate()
