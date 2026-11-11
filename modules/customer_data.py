"""把"顾客与客户的数据"编译成大模型能直接吃的档案卡。

**先讲清楚一件事：这不是微调。**
    用户说的"导入到大模型内"，工程上有两条路：
        (a) 把数据塞进 prompt / 检索上下文（长上下文注入）；
        (b) 拿数据去 fine-tune，写进模型权重。
    这个项目走 (a)，原因不是省事：
        - 档案是**每通电话不同**的（120 张申请单、12 个客户、16 笔订单），
          微调只能学到"句式"，学不到"这一单是谁、驳回原因是什么"；
        - 微调后的模型改一次数据就要重训一次，而自进化一轮要跑几十通；
        - 赛题要的是"可追溯"：哪句话依据哪条档案，prompt 注入能指出行号，
          权重里学到的指不出来。
    所以这里的产物是**结构化的档案卡**，运行时按通话取一张塞进上下文。
    效果上等于"模型把全部客户资料读了一遍"，只是读在上下文里，不在权重里。

数据来源（全部项目内已有，不新增采集）：
    config/mock/applications.json   120 条 POP 商家资质申请单（虚构）
    config/mock/customers.json      12 个售后客户
    config/mock/orders.json         16 笔订单
    eval/personas_*.json            394 条性格/诉求画像（10+17+320+12+12+23）
    config/kb_pop.json              68 条官方口径（客服侧事实源）
    config/mock/qa.json             27 条官方 QA 原话
    config/mock/catalog.json        68 条场景目录
    config/mock/rebuttals.json      52 条驳回话术

两个必须守住的边界（写在这里，改代码的人先看这两条）：
    1. **受知**：卡里的 `unknown` 是客户**不知道**的东西。LLM 扮演客户时
       只能从 `knows` 里取事实，`unknown` 里的（驳回码、审核员工号、银行账号）
       一律不许说出口。否则又回到"模拟器全知全能、随手报出正确信息"的假绿。
    2. **溯源**：每张卡都有 `source.file` + `source.record_no`。
       报告里出现任何一句话，都能回到是哪条档案生成的。

用法：
    python -m modules.customer_data            # 重新编译并打印统计
    python -m modules.customer_data --check    # 只校验现有文件是否最新
"""

import hashlib
import json
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT = os.path.join(_ROOT, "data", "customer_cards.json")

_SOURCES = (
    ("config/mock/applications.json", "items"),
    ("config/mock/customers.json", "items"),
    ("config/mock/orders.json", "items"),
    ("config/mock/qa.json", "items"),
    ("config/mock/catalog.json", "items"),
    ("config/mock/rebuttals.json", "items"),
    ("config/kb_pop.json", "items"),
)

_PERSONA_FILES = (
    ("eval/personas_sim.json", "基线 10 类"),
    ("eval/personas_temperament.json", "性格 12 类"),
    ("eval/personas_profession.json", "职业 12 类"),
    ("eval/personas_adversarial.json", "对抗 17 类"),
    ("eval/personas_jd.json", "京东人物库 320 条"),
    ("eval/personas_generated.json", "早期生成集"),
)

#: 客户**不知道**的东西。这张表是"受知"约束的唯一来源，别在别处另写一份。
#: 措辞刻意用抽象说法（"系统内部的驳回编号"而不是"驳回码 KB-POP-010"）：
#: 这段话是念给模型听的边界提示，不是给客户的知识——
#: 把具体字段名写进去，等于顺手教会模型这个字段存在。
_MERCHANT_UNKNOWN = (
    "系统内部的驳回编号",
    "审核专员的姓名与工号",
    "审核规则的具体条款号",
    "平台承诺的处理时限",
    "系统里这笔单的当前状态字段",
)
_CONSUMER_UNKNOWN = (
    "赔付规则的具体条款",
    "客服工单编号",
    "仓库/物流内部节点",
    "保修政策的官方条文",
)


def _read(rel):
    path = os.path.join(_ROOT, rel)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _sha(rel, n=10):
    path = os.path.join(_ROOT, rel)
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:n]
    except Exception:
        return ""


def _items(rel):
    data = _read(rel)
    if isinstance(data, list):
        return data
    return data.get("items") or []


def _short_materials(mats, limit=2):
    mats = [str(m) for m in (mats or []) if m]
    if not mats:
        return "材料"
    head = "、".join(mats[:limit])
    return head + ("等" if len(mats) > limit else "")


def _md(day):
    """2026-09-01 → 9 月 1 号。客户嘴里不会说"2026-09-01"。"""
    s = str(day or "")
    parts = s.split("-")
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return "%d 月 %d 号" % (int(parts[1]), int(parts[2]))
    return s or "前段时间"


def _merchant_card(app):
    """一张商家卡：身份 + 本单事实 + 客户自述 + 知识边界。

    `claim` 是**客户自己的说法**，不是档案原文。这一步很关键：
    让 LLM 照着档案念，它会念出"reject_code KB-POP-010"这种真人不会说的话；
    给它一段"我是谁、我遇到什么"的自述，它才会像人一样抱怨、追问、答非所问。
    """
    name = app.get("contact_name") or ""
    role = app.get("contact_role") or "联系人"
    company = app.get("company_name") or ""
    shop = app.get("shop_name") or ""
    brand = app.get("brand_name") or ""
    mats = app.get("required_materials") or []
    reject = app.get("reject_reason") or ""
    # 客户听到的驳回原因通常是"授权不对""材料不全"这种粗话，不是完整句子。
    reject_short = reject.split("，")[0][:22] if reject else "材料有问题"

    claim = (
        "我这边是{company}，{shop}这家店。"
        "{brand}这个牌子我是{when}提交的{atype}，前两天接到通知说{reject}，"
        "让补{mats}。店{n}年就开了，一直正常经营，我也不是第一次报品牌。"
    ).format(company=(app.get("company_short_name") or company), shop=shop,
             brand=brand or "新品牌", when=_md(app.get("submitted_at")),
             atype=(app.get("application_type") or "申请"),
             reject=reject_short, mats=_short_materials(mats),
             n=str(app.get("shop_open_date") or "")[:4] or "好几")

    knows = {
        "company_name": company,
        "company_short_name": app.get("company_short_name") or "",
        "shop_name": shop,
        "contact_name": name,
        "contact_role": role,
        "phone_tail": str(app.get("phone_tail") or ""),
        "application_id": app.get("application_id") or "",
        "brand_name": brand,
        "reject_reason_short": reject_short,
        "materials_short": _short_materials(mats, 3),
        "submitted_at": app.get("submitted_at") or "",
        "region": app.get("region") or "",
        "shop_open_year": str(app.get("shop_open_date") or "")[:4],
    }

    return {
        "card_id": "MC-%s" % (app.get("application_id") or app.get("record_no")),
        "kind": "merchant",
        "source": {"file": "config/mock/applications.json",
                   "record_no": app.get("record_no")},
        "who": {"name": name, "role": role, "company": company, "shop": shop,
                "region": app.get("region") or "", "phone_tail": str(app.get("phone_tail") or "")},
        "knows": knows,
        "claim": claim,
        "case": {
            "application_id": app.get("application_id") or "",
            "application_type": app.get("application_type") or "",
            "brand_name": brand,
            "reject_code": app.get("reject_code") or "",
            "reject_category": app.get("reject_category") or "",
            "reject_reason": reject,
            "required_materials": list(mats),
            "recheck_deadline": app.get("recheck_deadline") or "",
            "shop_type": app.get("shop_type") or "",
            "shop_status": app.get("shop_status") or "",
            "shop_open_date": app.get("shop_open_date") or "",
            "verify_preferred": app.get("verify_preferred") or "",
            # 渠道与状态：京麦单的官方口径就是"不外呼"，跑批必须先筛掉，
            # 否则会拿一通"根本不该打的电话"去算客服的失败（11-11 实测踩到）。
            "close_mode": app.get("close_mode") or "",
            "call_status": app.get("call_status") or "",
        },
        "unknown": list(_MERCHANT_UNKNOWN),
        "tags": ["pop", "merchant", (app.get("reject_category") or "").split("-")[0].lower()],
    }


def _consumer_card(cust, orders):
    mine = [o for o in orders if o.get("customer_id") == cust.get("customer_id")]
    mine.sort(key=lambda o: str(o.get("created_at") or ""), reverse=True)
    last = mine[0] if mine else {}
    tags = [str(t) for t in (cust.get("tags") or [])]
    claim = (
        "我叫{name}，{city}的，{level}会员。"
        "{prod}是{when}买的，{amount}块，现在{status}。"
    ).format(name=cust.get("name") or "客户", city=cust.get("city") or "",
             level=cust.get("level") or "", prod=last.get("product") or "东西",
             when=_md(last.get("created_at")), amount=int(last.get("amount") or 0),
             status=last.get("status") or "在用")
    knows = {
        "customer_id": cust.get("customer_id") or "",
        "name": cust.get("name") or "",
        "phone_tail": str(cust.get("phone_tail") or ""),
        "level": cust.get("level") or "",
        "city": cust.get("city") or "",
        "tags": tags,
        "last_order_id": last.get("order_id") or "",
        "last_product": last.get("product") or "",
        "last_amount": last.get("amount") or 0,
        "last_status": last.get("status") or "",
        "last_created_at": last.get("created_at") or "",
        "order_count": len(mine),
    }
    return {
        "card_id": "CC-%s" % (cust.get("customer_id") or "?"),
        "kind": "consumer",
        "source": {"file": "config/mock/customers.json",
                   "record_no": cust.get("customer_id")},
        "who": {"name": cust.get("name") or "", "role": "客户",
                "company": "", "shop": "", "region": cust.get("city") or "",
                "phone_tail": str(cust.get("phone_tail") or "")},
        "knows": knows,
        "claim": claim,
        "case": {"orders": [
            {"order_id": o.get("order_id"), "product": o.get("product"),
             "amount": o.get("amount"), "status": o.get("status"),
             "created_at": o.get("created_at")}
            for o in mine[:3]]},
        "unknown": list(_CONSUMER_UNKNOWN),
        "tags": ["consumer"] + tags,
    }


def _persona_rows():
    """画像只取"扮演要用"的字段，不整条搬——JSON 会大一倍，而 90% 字段用不上。"""
    rows = []
    for rel, group in _PERSONA_FILES:
        data = _read(rel)
        if isinstance(data, list):
            rows += [{"id": p.get("id"), "source": rel, "group": group, **p} for p in data]
            continue
        items = data.get("personas")
        if not items and isinstance(data.get("items"), list):
            items = data["items"]
        for p in (items or []):
            if not isinstance(p, dict):
                continue
            row = {
                "id": p.get("id"),
                "label": p.get("label") or p.get("profession") or p.get("temper") or "",
                "goal": p.get("goal") or "",
                "temper": p.get("temper") or "",
                "split": p.get("split") or "train",
                "style": p.get("style") or [],
                "catchphrases": p.get("catchphrases") or [],
                "catchphrase_rate": p.get("catchphrase_rate"),
                "traits": p.get("traits") or {},
                "knows": p.get("knows") or {},
                "willing": p.get("willing") or {},
                "expect": p.get("expect") or {},
                "profession": p.get("profession") or "",
                "attitude": p.get("attitude") or "",
                "tone": p.get("tone") or {},
                "language": p.get("language") or {},
                "scenario": p.get("scenario") or {},
                "utterances": p.get("utterances") or [],
                "source": rel,
                "group": group,
            }
            rows.append(row)
    # id 重复的（不同文件用了同一套编号）加后缀，否则按 id 取画像会串
    seen = {}
    for row in rows:
        pid = str(row.get("id") or "?")
        seen[pid] = seen.get(pid, 0) + 1
        if seen[pid] > 1:
            row["id"] = "%s#%d" % (pid, seen[pid])
    return rows


def _business_index():
    """客服侧的事实索引：只留"能播的话"和它的出处，不整条搬。

    注意这里**不是**给客户看的——客户不知道这些。它只进客服端 prompt。
    """
    idx = []
    for item in _items("config/kb_pop.json"):
        idx.append({"id": item.get("id"), "kind": "kb",
                    "q": " ".join([str(x) for x in (item.get("patterns") or [])][:4])
                         or (item.get("category") or ""),
                    "a": item.get("answer") or "",
                    "source": item.get("source_id") or ""})
    for item in _items("config/mock/qa.json"):
        idx.append({"id": item.get("id"), "kind": "qa",
                    "q": item.get("question") or item.get("question_raw") or "",
                    "a": item.get("answer") or item.get("answer_raw") or "",
                    "source": "官方QA"})
    return idx


def compile_all():
    apps = _items("config/mock/applications.json")
    custs = _items("config/mock/customers.json")
    orders = _items("config/mock/orders.json")

    cards = [_merchant_card(a) for a in apps if isinstance(a, dict)]
    cards += [_consumer_card(c, orders) for c in custs if isinstance(c, dict)]
    cards.sort(key=lambda c: c["card_id"])

    personas = _persona_rows()
    # 确定性配对：同一张卡永远配同一条画像。用 hash 而不是 random，
    # 否则重编译一次，昨天的报告和今天的报告就对不上人了。
    links = {}
    for card in cards:
        if not personas:
            break
        h = int(hashlib.sha256(card["card_id"].encode()).hexdigest()[:8], 16)
        links[card["card_id"]] = personas[h % len(personas)]["id"]

    idx = _business_index()
    sources = []
    for rel, _ in _SOURCES:
        rows = _items(rel)
        sources.append({"file": rel, "count": len(rows), "sha": _sha(rel)})

    payload = {
        "_meta": {
            "title": "客户档案卡（LLM 上下文注入用）",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generated_by": "modules/customer_data.py",
            "fictional_notice": "全部为虚构演示数据，不对应任何真实企业、自然人或号码。",
            "mode": "long_context_injection",
            "mode_note": "档案投进上下文，不写进权重。原因见模块文档。",
            "stats": {
                "merchant_cards": sum(1 for c in cards if c["kind"] == "merchant"),
                "consumer_cards": sum(1 for c in cards if c["kind"] == "consumer"),
                "cards": len(cards),
                "personas": len(personas),
                "business_facts": len(idx),
                "links": len(links),
            },
            "sources": sources,
        },
        "cards": cards,
        "personas": personas,
        "links": links,
        "business_index": idx,
    }
    return payload


_CACHE = {"mtime": None, "data": None}


def load(path=None):
    """读编译产物；文件不存在就现编译（不落盘），保证首次运行也能跑。"""
    p = path or _OUT
    try:
        m = os.path.getmtime(p)
    except OSError:
        return compile_all()
    if _CACHE["data"] is not None and _CACHE["mtime"] == m:
        return _CACHE["data"]
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return compile_all()
    _CACHE["data"], _CACHE["mtime"] = data, m
    return data


def card_by_id(card_id, path=None):
    for c in load(path).get("cards") or []:
        if c.get("card_id") == card_id:
            return c
    return None


def persona_by_id(pid, path=None):
    for p in load(path).get("personas") or []:
        if str(p.get("id")) == str(pid):
            return p
    return None


def pair_for(index=0, kind="merchant", path=None):
    """按序号取一张卡 + 它配对的画像。自对练用它保证每通电话"人不一样"。"""
    data = load(path)
    cards = [c for c in (data.get("cards") or []) if not kind or c.get("kind") == kind]
    if not cards:
        return None, None
    card = cards[int(index) % len(cards)]
    pid = (data.get("links") or {}).get(card["card_id"])
    return card, persona_by_id(pid, path)


def brief(card, max_chars=900):
    """把一张卡压成 prompt 里的一段。**只给客户自己知道的部分**。

    为什么不直接把 JSON 丢进去：JSON 里的 `case.reject_code`、`unknown`
    会被模型当成"我知道的事"顺口说出来。压成自然语言段落，边界才守得住。
    """
    if not card:
        return ""
    k = card.get("knows") or {}
    who = card.get("who") or {}
    lines = []
    if card.get("kind") == "merchant":
        lines.append("你是%s，%s的%s。" % (
            who.get("name") or "商家联系人", who.get("company") or "一家公司",
            who.get("role") or "联系人"))
        lines.append("你的店叫「%s」，在%s，%s年开的。" % (
            who.get("shop") or k.get("shop_name") or "店铺",
            who.get("region") or "本地", k.get("shop_open_year") or "好几"))
        lines.append("这次是为「%s」的%s申请，%s提交的。" % (
            k.get("brand_name") or "新品牌", "品牌资质", _md(k.get("submitted_at"))))
        lines.append("你收到的通知只说：%s，要补%s。" % (
            k.get("reject_reason_short") or "材料有问题",
            k.get("materials_short") or "材料"))
        lines.append("你能报出的信息：公司全名「%s」、店铺「%s」、你叫「%s」、预留手机尾号 %s、申请单号 %s。" % (
            k.get("company_name") or "", k.get("shop_name") or "",
            k.get("contact_name") or "", k.get("phone_tail") or "（记不清）",
            k.get("application_id") or "（记不全）"))
    else:
        lines.append("你是%s，%s的%s会员。" % (
            who.get("name") or "客户", who.get("region") or "本地", k.get("level") or ""))
        lines.append("最近一笔是%s，%s买的，%s块，现在%s。" % (
            k.get("last_product") or "东西", _md(k.get("last_created_at")) or "前段时间",
            int(k.get("last_amount") or 0), k.get("last_status") or "在用"))
        lines.append("你能报出的信息：姓名「%s」、预留手机尾号 %s、订单号 %s。" % (
            k.get("name") or "", k.get("phone_tail") or "", k.get("last_order_id") or ""))
        if k.get("tags"):
            lines.append("你的情况标签：%s。" % "、".join(k["tags"]))

    unknown = card.get("unknown") or []
    if unknown:
        lines.append("你**不知道**的：%s。被问到就说不清楚、记不住，绝不编。" % "、".join(unknown))
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    # 超长就保住"能报的信息"和"不知道的"两段——这两段决定受知边界。
    keep = [ln for ln in lines if ln.startswith("你能报出") or ln.startswith("你**不知道**")]
    return "\n".join(keep + lines[:2])[:max_chars]


def write(path=None):
    p = path or _OUT
    os.makedirs(os.path.dirname(p), exist_ok=True)
    payload = compile_all()
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload


def main(argv):
    if "--check" in argv:
        fresh = compile_all()
        try:
            with open(_OUT, "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            # 编译产物在 .gitignore 里（`/data/` 是运行时目录）。
            # 队友 clone 下来一定没有这个文件，此时 load() 会现编译，
            # 行为是对的——所以"没编译"不算闸门失败，只提示一句。
            print("未编译（data/customer_cards.json 不存在）；运行时 load() 会现编译")
            return 0
        same = (json.dumps(fresh.get("cards"), sort_keys=True, ensure_ascii=False)
                == json.dumps(old.get("cards"), sort_keys=True, ensure_ascii=False))
        print("档案卡与源数据一致" if same else "档案卡已过期，请重新编译")
        return 0 if same else 1

    payload = write()
    s = payload["_meta"]["stats"]
    print("已写入 data/customer_cards.json（%d 张卡）" % s["cards"])
    for k, v in s.items():
        print("  %-18s %s" % (k, v))
    for src in payload["_meta"]["sources"]:
        print("  %-34s %4d 条  sha=%s" % (src["file"], src["count"], src["sha"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
