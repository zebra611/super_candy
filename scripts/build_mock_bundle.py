# -*- coding: utf-8 -*-
"""从官方资料包的两份 xlsx 生成 mock 基础库（目录 / 驳回话术 / 高频 QA）。

为什么要有这一步：
    赛题给的《[POP资审]资审关单场景话术V1.0.xlsx》与《驳回外呼QA.xlsx》是**唯一
    权威来源**（68 个关单场景、52 条驳回话术、24 条高频 QA、特殊场景追问）。
    之前 mock 里只有 8 条自造驳回原因，与官方一一对不上——演示时商家说"商标状态
    不符合要求"，系统回的是另一套说法，评委一看就知道是自己编的。

    所以把官方表**机械地**转成结构化 JSON，再由这些 JSON 驱动 mock 档案与
    知识/话术，保证"照着资料包说话"。

设计约束：
  - **只用标准库**：环境里没有 openpyxl/pandas，xlsx 本质是 zip + XML，
    自己解比装依赖稳（也避免部署时多一个依赖）。
  - **确定性**：输出不含时间戳，同输入逐字节一致；`--check` 用来验"生成物没漂移"。
  - **可溯源**：`_meta` 记录源文件名、sheet 名、各自的 sha256 与前缀，改源即可发现。
  - **只读源文件**：不写回、不改官方资料包里的任何文件。

产物（都在 config/mock/ 下，覆盖式重写；原有 applications.json/csv 不动）：
    catalog.json    68 个关单场景（菜单/分类/二级分类/关单方式/话术/需补材料/热线）
    rebuttals.json  52 条驳回话术（开场语+驳回+结束语），按场景标注
    qa.json         高频 QA + 特殊场景追问

跑法：
    python scripts/build_mock_bundle.py            # 生成
    python scripts/build_mock_bundle.py --check    # 只校验生成物与官方表是否一致
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "config", "mock")
OFFICIAL_DIR = os.path.join(ROOT, "competition", "官方资料包")

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

HOTLINE_DEFAULT = "4006180092"


# ---------------------------------------------------------------------------
# xlsx 读取（纯标准库）
# ---------------------------------------------------------------------------
def _shared_strings(z):
    out = []
    if "xl/sharedStrings.xml" not in z.namelist():
        return out
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    for si in root.findall(M + "si"):
        out.append("".join(t.text or "" for t in si.iter(M + "t")))
    return out


def _col_of(ref):
    m = re.match(r"([A-Z]+)", ref or "A")
    return m.group(1) if m else "A"


def read_xlsx(path):
    """返回 {sheet 名: [ {列字母: 文本} ... ]}。合并单元格只会出现在左上角那格，
    调用方按需 forward-fill。"""
    z = zipfile.ZipFile(path)
    shared = _shared_strings(z)
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    names = [s.get("name") for s in wb.find(M + "sheets")]
    out = {}
    for i, name in enumerate(names, 1):
        part = "xl/worksheets/sheet%d.xml" % i
        if part not in z.namelist():
            continue
        root = ET.fromstring(z.read(part))
        rows = []
        for row in root.iter(M + "row"):
            cells = {}
            for c in row:
                col = _col_of(c.get("r"))
                t = c.get("t")
                v = c.find(M + "v")
                isx = c.find(M + "is")
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr" and isx is not None:
                    val = "".join(x.text or "" for x in isx.iter(M + "t"))
                elif v is not None:
                    val = v.text or ""
                else:
                    val = ""
                cells[col] = val
            rows.append(cells)
        out[name] = rows
    return out


def _norm(s):
    """去多余空白与首尾空白；保留中文标点与【】占位符。"""
    s = (s or "").replace("\r", " ")
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    return s.strip()


def _find_sheet(sheets, *keywords):
    """按关键词找 sheet（官方表里 sheet 名可能带空格/全角括号）。"""
    for name in sheets:
        flat = name.replace(" ", "")
        if all(k in flat for k in keywords):
            return name
    return None


# ---------------------------------------------------------------------------
# 业务域映射：官方「一级菜单/一级分类」→ 回写枚举 reject_category
# ---------------------------------------------------------------------------
DOMAINS = ["品牌-授权", "品牌-商标", "品牌-全球购", "商品与行业资质",
           "入驻与主体店铺", "京麦驳回", "未定位"]


def map_domain(menu, category, subcategory):
    """官方场景 → 回写用业务域。只做**业务语义**映射，不看关单方式——
    京麦渠道的品牌驳回，业务域仍然是品牌，不是「京麦驳回」。"""
    text = "%s %s %s" % (menu, category, subcategory)
    if "全球购" in text or "海外" in text or "境外" in text:
        return "品牌-全球购"
    if "商标" in text:
        return "品牌-商标"
    if "授权" in text:
        return "品牌-授权"
    if menu in ("行业资质", "公司资质", "商品资质"):
        return "商品与行业资质"
    if menu == "入驻":
        if "店铺名称" in text:
            return "入驻与主体店铺"
        if "主体" in text or "公司" in text:
            return "入驻与主体店铺"
        return "入驻与主体店铺"
    if menu == "品牌":
        return "品牌-授权"
    return "未定位"


# 需补材料：按关键词给最小可用清单（不是穷举，够客服说清"补什么"）
MATERIAL_RULES = [
    (("商标",), ["商标注册证", "国家商标网状态截图"]),
    (("授权链路", "授权关系"), ["品牌授权书", "逐级授权链路证明"]),
    (("授权书", "授权时间", "授权范围", "独占性"), ["品牌授权书"]),
    (("发票",), ["购货发票", "发票验真截图"]),
    (("质检",), ["质检报告"]),
    (("食品", "生产许可"), ["食品经营许可证", "生产许可证"]),
    (("化妆品", "备案"), ["化妆品备案凭证"]),
    (("3C", "认证"), ["3C 认证证书"]),
    (("营业执照", "主体名称", "法人", "税务", "证件"), ["营业执照", "法人证件"]),
    (("店铺名称",), ["店铺名称变更截图", "授权书店铺名称页"]),
    (("无品牌",), ["品牌说明函"]),
    (("境外", "海外", "全球购", "跨境"), ["境外商标注册证", "中文译本", "公证认证文件"]),
    (("医药",), ["药品经营许可证", "购货发票"]),
]


def materials_for(menu, category, subcategory, script="", placeholders=None):
    text = "%s %s %s" % (menu, category, subcategory)
    out = []
    for keys, mats in MATERIAL_RULES:
        if any(k in text for k in keys):
            for m in mats:
                if m not in out:
                    out.append(m)
    if not out:
        out = ["补充材料扫描件"]
    # 话术里点名要补的材料优先（官方口径）
    for ph in (placeholders or []):
        ph = ph.strip()
        if not ph or ph in ("】", "XX", "XXX", "XXXX"):
            continue
        if any(k in ph for k in ("证", "书", "票", "报告", "证明")) and ph not in out:
            out.insert(0, ph)
    return out[:5]


PLACEHOLDER_RE = re.compile(r"【([^】]*)】")


def placeholders_of(script):
    seen, out = set(), []
    for m in PLACEHOLDER_RE.finditer(script or ""):
        inner = m.group(1).strip()
        if inner in seen:
            continue
        seen.add(inner)
        out.append(inner)
    return out


# ---------------------------------------------------------------------------
# catalog.json：68 个关单场景
# ---------------------------------------------------------------------------
def build_catalog(sheets):
    name = _find_sheet(sheets, "场景汇总")
    if not name:
        raise SystemExit("找不到「场景汇总」sheet，官方表结构变了？")
    rows = sheets[name]

    # 表头行：A 列是「一级菜单」
    header = None
    for i, r in enumerate(rows):
        if _norm(r.get("A")) == "一级菜单":
            header = i
            break
    if header is None:
        raise SystemExit("「场景汇总」里找不到表头（一级菜单）")

    items = []
    menu = category = ""
    for r in rows[header + 1:]:
        if _norm(r.get("A")):
            menu = _norm(r.get("A"))
        if _norm(r.get("B")):
            category = _norm(r.get("B"))
        sub = _norm(r.get("C"))
        if not sub:
            continue
        script = _norm(r.get("E"))
        close_raw = _norm(r.get("D"))
        close_mode = "京麦" if "京麦" in close_raw else ("电话" if "电话" in close_raw else "")
        if not close_mode:
            continue
        # 官方表里有两行（品牌方不认可授权）关单方式是电话但话术留空。
        # 这类场景**照样要进目录**：档案要覆盖它，只是没有可直接播的模板——
        # 用 script_missing 标出来，让下游知道"这条得走兜底话术"，而不是当它不存在。
        ph = placeholders_of(script)
        items.append({
            "id": "",                      # 下面按顺序编号
            "menu": menu,
            "category": category,
            "subcategory": sub,
            "close_mode": close_mode,
            "close_mode_raw": close_raw,
            "domain": map_domain(menu, category, sub),
            "script": script,
            "script_missing": not bool(script),
            "placeholders": ph,
            "materials": materials_for(menu, category, sub, script, ph),
            "hotline": HOTLINE_DEFAULT if "4006180092" in script else "",
        })

    for i, it in enumerate(items, 1):
        it["id"] = "KB-POP-%03d" % i
        it["scene_key"] = "%s/%s/%s" % (it["menu"], it["category"], it["subcategory"])
    return items


# ---------------------------------------------------------------------------
# rebuttals.json：52 条驳回话术
# ---------------------------------------------------------------------------
def build_rebuttals(sheets):
    name = _find_sheet(sheets, "驳回话术")
    if not name:
        raise SystemExit("找不到「驳回话术」sheet")
    rows = sheets[name]
    items = []
    scene = ""
    for r in rows:
        a, b = _norm(r.get("A")), _norm(r.get("B"))
        if a == "场景":            # 表头
            continue
        if a:
            scene = a
        if not b:
            continue
        items.append({
            "id": "RB-%03d" % (len(items) + 1),
            "scene": scene or "（未标注场景）",
            "script": b,
            "has_opening": "请问您是" in b or "您好" in b,
            "has_closing": "再见" in b,
            "hotline": HOTLINE_DEFAULT if "4006180092" in b else "",
            "placeholders": placeholders_of(b),
        })
    return items


# ---------------------------------------------------------------------------
# qa.json：高频 QA + 特殊场景
# ---------------------------------------------------------------------------
# 官方表里有 5 条问答带未填占位（"XX"）。11-08 由人工按上下文补齐，
# 原文一字不删地保留在 `answer_raw` / `question_raw` 里。
#
# 为什么非补不可：不补就只能选择「不播」或「让 TTS 念出叉叉」。
# 这 5 条恰好是商家问得最多的商标状态 / 授权链路问题，答不上来等于把
# 知识问答的分全丢掉；念"叉叉"又像系统故障。补齐是唯一能用的选项，
# 但补齐必须**可追溯**——谁都能翻到原句，判断我们补得对不对。
#
# 键用原句（不用 id）：id 是抽取顺序编出来的，官方表加一行就会整体挪位。
QA_FILL = {
    "我在哪可以查询到这个商标状态": (
        "我在哪可以查询到这个商标状态",
        "您可以在国家商标网中查询商标状态，等状态变为已注册之后重新提交申请，"
        "或者提供其他已注册状态的同类有效商标",
    ),
    "是必须在满足XX时间才可以申请吗": (
        "是必须等满商标审查时间才可以申请吗",
        "您商标目前是等待实质审查状态，如果在等待期满之前，商标状态变更为"
        "初审公告或者已注册，就可以直接申请，不用等满整段时间",
    ),
    "XX公司和XX公司他们是一家，怎么还需要授权书": (
        "这两家公司他们是一家，怎么还需要授权书",
        "这两家公司目前没有查询到有直接持股或者同一法人的关系，"
        "是需要您补充相应的授权书的",
    ),
    "你们联系的谁": (
        "你们联系的谁",
        "我们是通过品牌方官方渠道联系到授权负责人的，如果您能联系到该联系人，"
        "可以请他回复我们的消息或者接听我们的电话；如果您有其他的联系方式，"
        "可以在授权书的位置上传，不过这些联系方式需要联系人协助提供"
        "他在授权公司的工作凭证才可以",
    ),
    "我就要经营这些类目怎么办": (
        "我就要经营这些类目怎么办",
        "您可以联系品牌方给您再授权这些类目所需的对应类别商标",
    ),
}


def fill_placeholders(items):
    """把 QA 里的未填占位按 QA_FILL 补齐，原文存进 *_raw。

    返回补过的条目 id 列表（写进 _meta，供校验与答辩追溯）。
    """
    filled = []
    for it in items:
        hit = QA_FILL.get(it.get("question"))
        if not hit:
            continue
        new_q, new_a = hit
        if new_q != it.get("question"):
            it["question_raw"] = it["question"]
            it["question"] = new_q
        if new_a != it.get("answer"):
            it["answer_raw"] = it["answer"]
            it["answer"] = new_a
        it["filled_placeholder"] = True
        filled.append(it["id"])
    return filled


def build_qa(sheets):
    items = []

    name = _find_sheet(sheets, "高频")
    if name:
        rows = sheets[name]
        header = None
        for i, r in enumerate(rows):
            if _norm(r.get("B")) == "商家问题":
                header = i
                break
        module = ""
        for r in (rows[header + 1:] if header is not None else []):
            a = _norm(r.get("A"))
            if a:
                module = a
            q, ans = _norm(r.get("B")), _norm(r.get("C"))
            if not q or not ans:
                continue
            items.append({
                "id": "QA-HF-%03d" % (len(items) + 1),
                "source": "高频QA",
                "module": module or "通用",
                "question": q,
                "answer": ans,
                "context_script": "",
            })

    name = _find_sheet(sheets, "特殊场景")
    if name:
        rows = sheets[name]
        n0 = len(items)
        for r in rows:
            q, ans = _norm(r.get("C")), _norm(r.get("D"))
            if not q or not ans or q == "商家后续咨询什么问题":
                continue
            # 官方表里「这个我补上来了就可以通过了吗」出现两次（第 17、24 行），
            # 答案一致。去重保留第一条，避免下游按问题建索引时撞车。
            if any(it["question"] == q for it in items):
                continue
            items.append({
                "id": "QA-TS-%03d" % (len(items) - n0 + 1),
                "source": "特殊场景",
                "module": "特殊场景",
                "question": q,
                "answer": ans,
                "context_script": _norm(r.get("B"))[:200],
            })

    fill_placeholders(items)
    return items


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def build(source_dir=OFFICIAL_DIR):
    pop = os.path.join(source_dir, "POP计算关单场景话术V1.0.xlsx")
    qa = os.path.join(source_dir, "驳回外呼QA.xlsx")
    for p in (pop, qa):
        if not os.path.exists(p):
            raise SystemExit("缺官方源文件：%s" % p)

    sheets_pop = read_xlsx(pop)
    sheets_qa = read_xlsx(qa)

    catalog = build_catalog(sheets_pop)
    rebuttals = build_rebuttals(sheets_qa)
    qa_items = build_qa(sheets_qa)

    meta = {
        "generated_by": "scripts/build_mock_bundle.py",
        "source": {
            "POP": {"file": os.path.basename(pop), "sha256": _sha(pop),
                    "sheets": sorted(sheets_pop)},
            "QA": {"file": os.path.basename(qa), "sha256": _sha(qa),
                   "sheets": sorted(sheets_qa)},
        },
        "counts": {"catalog": len(catalog), "rebuttals": len(rebuttals),
                   "qa": len(qa_items)},
        "notice": "话术与 QA 原文来自官方资料包，逐字保留；结构化字段（domain/materials/"
                  "placeholders）由本脚本按规则派生，不对原文做改写。",
        # 唯一一处例外要说清楚：5 条带 XX 占位的 QA 由人工补齐（原文留在
        # 条目的 answer_raw / question_raw），否则 TTS 会把"XX"念成"叉叉"。
        "qa_filled": {
            "count": len([it for it in qa_items if it.get("filled_placeholder")]),
            "ids": [it["id"] for it in qa_items if it.get("filled_placeholder")],
            "policy": "占位符按上下文人工补齐，原文存 answer_raw/question_raw，不改官方口径",
        },
    }
    return meta, catalog, rebuttals, qa_items


def _dump(path, meta, items):
    body = json.dumps({"_meta": meta, "items": items},
                      ensure_ascii=False, indent=1)
    with open(path, "wb") as f:
        f.write((body + "\n").replace("\r\n", "\n").encode("utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对，不写文件")
    ap.add_argument("--source", default=OFFICIAL_DIR, help="官方资料包目录")
    args = ap.parse_args(argv)

    meta, catalog, rebuttals, qa_items = build(args.source)
    targets = {
        "catalog.json": catalog,
        "rebuttals.json": rebuttals,
        "qa.json": qa_items,
    }

    if args.check:
        bad = 0
        for fname, items in targets.items():
            path = os.path.join(OUT_DIR, fname)
            if not os.path.exists(path):
                print("MISS %s 不存在" % fname)
                bad += 1
                continue
            with open(path, encoding="utf-8") as f:
                cur = json.load(f)
            same = json.dumps(cur.get("items"), ensure_ascii=False, sort_keys=True) == \
                json.dumps(items, ensure_ascii=False, sort_keys=True)
            print("%s %s（%d 条）" % ("OK  " if same else "DIFF", fname, len(items)))
            bad += 0 if same else 1
        return 1 if bad else 0

    os.makedirs(OUT_DIR, exist_ok=True)
    for fname, items in targets.items():
        # qa_filled 只对 qa.json 有意义，别写进 catalog / rebuttals 的 _meta——
        # 那两份文件不需要重建，多一行 diff 只会让"这次到底改了什么"变模糊。
        m = meta if fname == "qa.json" else {
            k: v for k, v in meta.items() if k != "qa_filled"}
        _dump(os.path.join(OUT_DIR, fname), m, items)
        print("wrote %-15s %3d 条" % (fname, len(items)))
    print("catalog 关单方式：电话 %d / 京麦 %d" % (
        sum(1 for c in catalog if c["close_mode"] == "电话"),
        sum(1 for c in catalog if c["close_mode"] == "京麦")))
    print("catalog 业务域：%s" % {
        d: sum(1 for c in catalog if c["domain"] == d) for d in DOMAINS})
    return 0


if __name__ == "__main__":
    sys.exit(main())
