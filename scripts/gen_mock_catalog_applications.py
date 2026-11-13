# -*- coding: utf-8 -*-
"""为官方 68 个关单场景各生成一条虚构申请单档案（config/mock/applications_catalog.json）。

为什么不复用原 120 条：
    原 `applications.json` 是**评测基线的一部分**——`verify` 的 C5 断言、C4 的 340 条
    对抗用例、`eval/seed_cases.json` 的 88 条，都按"尾号 / 申请单号"直接引用它。
    改它等于把基线一起改了，跑出来的分数就说不清是数据变了还是系统变了。

    所以做法是**并列新增**：原 120 条一个字节不动，这里按官方目录再生成 68 条，
    运行时由适配层合并成完整池（188 条）。这样既补齐覆盖，也不动任何既有引用。

设计要点：
  - 一条场景一条档案：68 个场景 = 68 条，`reject_code` 与目录 id 一一对应，可反查；
  - 全虚构：字号/品牌/人名自造，电话 199-0000-XXXX，邮箱 .test；
  - 与原集不撞车：字号池、姓名段、尾号段都与原集错开（生成时先读原集占用值）；
  - 确定性：固定种子，重跑逐字节一致；
  - 字段集 = 原集 62 字段 + 5 个溯源字段，字段顺序一致，下游可无差别消费。

跑法：python scripts/gen_mock_catalog_applications.py
校验：python scripts/check_mock_bundle.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import random
from datetime import date, timedelta

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.gen_mock_applications import (  # noqa: E402
    CAT_L1, CAT_L2, CAT_L3, CONTACT_ROLES, GIVEN_NAMES, REGION_CODE, REGIONS,
    SHOP_TYPES, COMPANY_TYPES, APPLICATION_TYPES, CALL_STATUSES,
    APP_STATUSES, make_credit_code, make_id_card, build_records,
    address_of, branch_city, LEGAL_SUFFIX,
    SIZI as LEGACY_SIZI, BRANDS as LEGACY_BRANDS,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "config", "mock")
CATALOG = os.path.join(OUT_DIR, "catalog.json")
LEGACY = os.path.join(OUT_DIR, "applications.json")
SEED = 20261201
HOTLINE = "4006180092"

# 已归档 → 运行状态：关单方式决定能不能打
CALL_RESULT_OF = {
    "待外呼": "not_applicable", "已接通": "connected", "拒绝": "refused",
    "无应答": "no_answer", "无效号码": "invalid", "已关单": "connected",
    "无需外呼": "not_applicable",
}

# 字号池：**与原集 SIZI 完全不重叠**（保证合并后 company_short_name 仍全局唯一，
# 而不用担心"字号相同 + 地区相同 + 类型相同"撞出同名公司）
CATALOG_SIZI = ["%s%s" % (h, t) for h in
                ["京", "海", "天", "恒", "诚", "惠", "联", "德", "瑞", "嘉", "盛", "广"]
                for t in ["华", "选", "通", "达", "源", "丰", "泰", "和", "信", "康"]][:68]

# 品牌池：同样与原集不重叠
CATALOG_BRANDS = ["%s%s" % (h, t) for h in
                 ["臻", "素", "朴", "原", "初", "真", "鲜", "悦", "柔", "净"]
                 for t in ["颜", "食", "居", "家", "选", "品", "源", "光", "语"]][:68]

# 上面两组与**原集**的字号/品牌池求差集（原来的手写池撞过「霁川」「蘅芜」，
# 撞了就违反合并池唯一性）。两边现在都改用常用字组合，第一组以「京/海/天…」
# 开头、第二组以「臻/素/朴…」开头，与原集的「华/佳/优…」「小/好/乐…」天然不撞。
CATALOG_SIZI = [w for w in dict.fromkeys(CATALOG_SIZI) if w not in set(LEGACY_SIZI)]
CATALOG_BRANDS = [w for w in dict.fromkeys(CATALOG_BRANDS) if w not in set(LEGACY_BRANDS)]
# 差集后不足 68 时用带序号的派生词补齐（仍是自造词，且与原集必然不重叠）
_PAD = ["京华序", "岑溪序", "岱屿序", "岫云序", "屿川序", "泊舟序", "涧雪序",
        "崖光序", "岚屿序", "葭芦序", "荻洲序", "蘋汀序"]
CATALOG_SIZI += [w for w in _PAD if w not in set(LEGACY_SIZI) and w not in CATALOG_SIZI]
CATALOG_BRANDS += [w for w in _PAD if w not in set(LEGACY_BRANDS) and w not in CATALOG_BRANDS]
if len(CATALOG_SIZI) < 68 or len(CATALOG_BRANDS) < 68:
    raise SystemExit("字号/品牌池差集后不足 68：%d / %d"
                     % (len(CATALOG_SIZI), len(CATALOG_BRANDS)))

# 姓氏池：与原集姓氏池（张/赵/刘/黄/徐…那一组）完全不重叠，
# 保证合并后 contact_name / legal_representative 不会撞名。
# 原来用的是复姓（欧阳/上官/司马…），念是念得出，但演示时"欧阳xx"总像武侠人物，
# 换成最常见的单姓更自然。
CATALOG_SURNAMES = [
    "钱", "孙", "李", "周", "吴", "郑", "王", "冯", "陈", "褚",
    "卫", "蒋", "韩", "杨", "朱", "秦", "尤", "许", "何", "吕",
]

# 场景专属边缘标签（让 notes/edge_cases 能看出这条是为哪个官方场景造的）
def _scene_edges(scene):
    edges = ["official_catalog"]
    sub = scene["subcategory"]
    if scene["close_mode"] == "京麦":
        edges.append("jingmai_no_call")
    if "无品牌" in sub:
        edges.append("missing_brand")
    if "全球购" in scene["menu"] or "境外" in sub:
        edges.append("global_purchase")
    if "店铺名称" in sub:
        edges.append("similar_shop_name")
    if scene["script_missing"]:
        edges.append("script_missing")
    if not scene["placeholders"]:
        edges.append("no_placeholder")
    return edges


def _reason_of(scene):
    """把官方二级分类 + 需补材料拼成一句可播报的驳回原因（不改写官方话术原文）。"""
    sub = scene["subcategory"].replace("\n", "")
    mats = scene["materials"][:2]
    if mats:
        return "%s，需补充%s后重新提交" % (sub, "、".join(mats))
    return sub


def _placeholders_style(scene):
    if not scene["placeholders"]:
        return "无占位符"
    if any(p == "" or p.upper() == "XX" for p in scene["placeholders"]):
        return "具名【XX】占位符"
    return "空【】占位符"


def build_catalog_records():
    with open(CATALOG, encoding="utf-8") as f:
        scenes = json.load(f).get("items") or []
    if len(scenes) != 68:
        raise SystemExit("目录条数不是 68（%d），先跑 build_mock_bundle.py" % len(scenes))

    # 原集占用值：尾号、电话、邮箱、公司名——用于避让，保证合并池唯一
    legacy = []
    if os.path.exists(LEGACY):
        with open(LEGACY, encoding="utf-8") as f:
            legacy = json.load(f).get("items") or []
    else:                                     # 原集被删时用生成器现算，行为一致
        legacy = build_records()
    used_tails = {r["phone_tail"] for r in legacy}
    used_companies = {r["company_name"] for r in legacy}
    used_shops = {r["shop_name"] for r in legacy}
    used_ids = {r["application_id"] for r in legacy}

    # 姓名池：原集的姓氏池被随机打乱后取前 360 个，无法安全复用索引，
    # 所以这里换一组**完全不重叠的复姓**，从源头避免合并后重名。
    names = ["%s%s" % (s, g) for s in CATALOG_SURNAMES for g in GIVEN_NAMES]
    names = list(dict.fromkeys(names))

    rng = random.Random(SEED)
    # 尾号：9000~9999 里挑未占用的，不足再向 8000 段扩
    pool = [t for t in ("%04d" % n for n in range(8000, 10000)) if t not in used_tails]
    rng.shuffle(pool)
    tails = pool[:len(scenes)]
    if len(tails) < len(scenes):
        raise SystemExit("可用尾号不足 %d 个" % len(scenes))

    records = []
    base_date = date(2026, 11, 1)
    for i, scene in enumerate(scenes):
        seq = i + 1
        sid = "QZ20261201%04d" % seq
        while sid in used_ids:                 # 理论上不会撞，防一手
            sid += "X"
        short = CATALOG_SIZI[i % len(CATALOG_SIZI)]
        region = REGIONS[(i * 3) % len(REGIONS)]
        ctype = COMPANY_TYPES[(i * 5) % len(COMPANY_TYPES)]
        # 公司名要像真名：字号 + 行业 + 组织形式。**不把场景序号写进名字**——
        # 之前是「…百货经营部（场景07）」，预览卡上"公司名称"一栏直接显示"场景07"，
        # 看上去像测试夹具，不像一家公司。68 条的字号池本来就互不重复，
        # 不靠序号也保得住全局唯一。
        if "个体" in ctype:
            company = "%s%s百货经营部" % (region, short)
        elif "分公司" in ctype:
            company = "%s%s云启科技有限公司%s分公司" % (
                region, short, branch_city(region, seq))
        else:
            company = "%s%s商贸%s" % (region, short, LEGAL_SUFFIX.get(ctype, "有限公司"))
        # 兜底：万一与历史重名（同字号同地区同类型），加一个分公司后缀区分
        if company in used_companies:
            company = "%s%s分公司" % (company, branch_city(region, seq))
        used_companies.add(company)

        shop = "%s官方旗舰店" % short
        if shop in used_shops:
            shop = "%s官方旗舰店（二店）" % short
        used_shops.add(shop)

        tail = tails[i]
        region_code = REGION_CODE.get(region, 100000)
        credit_code = make_credit_code(region_code, 1000 + seq)
        id_card = make_id_card(region_code, 1000 + seq)
        shop_id = "SHOP%08d" % (20261200 + seq)
        # 银行账户尾号：与电话尾号错开，方便测试者肉眼区分"哪个后四位"
        bat = (seq * 173 + 29) % 10000
        if "%04d" % bat == tail:
            bat = (bat + 1) % 10000

        submitted = base_date + timedelta(days=(i * 5) % 30)
        reject_at = submitted + timedelta(days=1 + i % 6)
        deadline = reject_at + timedelta(days=8 + (i * 3) % 20)

        # 关单方式决定通话状态：京麦的不外呼；电话的铺开各种状态（保证外呼名单有货）
        if scene["close_mode"] == "京麦":
            call_status = "无需外呼"
        else:
            call_status = CALL_STATUSES[i % (len(CALL_STATUSES) - 1)]   # 不含「无需外呼」
            if i % 11 == 0:
                call_status = "待外呼"          # 保证名单里有可打的

        understood = "unknown" if call_status in ("待外呼", "无应答", "无效号码") else [True, False][i % 2]

        reason = _reason_of(scene)
        rec = {
            "record_no": seq,
            "application_id": sid,
            "merchant_id": "M20261201%04d" % seq,
            "fictional_marker": "SIM-2026-TEST-FICTIONAL",
            "company_name": company,
            "company_short_name": short,
            "shop_name": shop,
            "shop_id": shop_id,
            "shop_id_tail": shop_id[-4:],
            "shop_type": SHOP_TYPES[i % len(SHOP_TYPES)],
            "shop_status": "正常经营",
            "shop_open_date": (date(2017, 3, 1) + timedelta(days=(i * 61) % 2100)).isoformat(),
            "company_type": ctype,
            "region": region,
            "registered_capital_wan": 20 + (i * 29) % 900,
            "established_date": (date(2016, 6, 1) + timedelta(days=(i * 47) % 2400)).isoformat(),
            "legal_representative": names[300 + i],
            "business_scope": "%s、%s的批发零售及网上销售；品牌管理" % (
                CAT_L2[i % len(CAT_L2)], CAT_L3[i % len(CAT_L3)]),
            "credit_code": credit_code,
            "credit_code_tail": credit_code[-4:],
            "bank_account_tail": "%04d" % bat,
            "registered_address": address_of(region, seq),
            "contact_name": names[100 + i],
            "contact_role": CONTACT_ROLES[i % len(CONTACT_ROLES)],
            "contact_phone": "1990000" + tail,
            "phone_tail": tail,
            "contact_email": "scene%03d@example.test" % seq,
            "id_card": id_card,
            "id_card_tail": id_card[-4:],
            "alternative_contact": "%s（资质助理）" % names[200 + i],
            "submitted_at": submitted.isoformat(),
            "application_type": APPLICATION_TYPES[i % len(APPLICATION_TYPES)],
            "brand_name": CATALOG_BRANDS[i % len(CATALOG_BRANDS)],
            "brand_registration_no": "%08d" % (95000000 + seq * 6151),
            "category_l1": CAT_L1[i % len(CAT_L1)],
            "category_l2": CAT_L2[i % len(CAT_L2)],
            "category_l3": CAT_L3[i % len(CAT_L3)],
            "target_channel": "京麦" if scene["close_mode"] == "京麦" else (
                "全球购" if scene["domain"] == "品牌-全球购" else "POP"),
            "application_status": APP_STATUSES[i % len(APP_STATUSES)],
            "reject_code": scene["id"],
            "reject_category": scene["domain"],
            "reject_reason": reason[:190],
            "reject_at": reject_at.isoformat(),
            "reject_round": 1 + i % 2,
            "required_materials": list(scene["materials"]),
            "recheck_deadline": (deadline.isoformat() if i % 13 else "（未设定截止日·%03d）" % seq),
            "verify_by_name": names[100 + i],
            "verify_by_tail": tail,
            "verify_by_company": company,
            "verify_by_application": sid,
            "verify_preferred": ["姓名", "后四位", "公司名", "申请单号"][i % 4],
            "close_mode": scene["close_mode"],
            "call_status": call_status,
            "call_result": CALL_RESULT_OF[call_status],
            "merchant_understood": understood,
            "callback_needed": call_status in ("无应答", "拒绝"),
            "hotline": HOTLINE,
            "notes": "官方场景目录 %s（%s）；话术模板%s" % (
                scene["id"], scene["scene_key"],
                "缺失，走兜底话术" if scene["script_missing"] else "见 official_script"),
            "data_valid_from": (reject_at - timedelta(days=3)).isoformat(),
            "data_valid_to": (deadline + timedelta(days=30)).isoformat(),
            "placeholder_style": _placeholders_style(scene),
            "edge_cases": _scene_edges(scene),
            # —— 溯源字段（原集没有；合并池里原集这几项为空字符串）——
            "catalog_id": scene["id"],
            "catalog_scene_key": scene["scene_key"],
            "official_script": scene["script"],
            "official_close_mode": scene["close_mode"],
            "reject_source": "官方场景目录",
        }
        records.append(rec)
    return records


def _csv_value(v):
    if isinstance(v, list):
        return "|".join(str(x) for x in v)
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    records = build_catalog_records()
    fields = list(records[0].keys())

    meta = {
        "title": "官方 68 个关单场景 × 各一条虚构申请单档案",
        "generated_by": "scripts/gen_mock_catalog_applications.py",
        "seed": SEED,
        "count": len(records),
        "source_catalog": "config/mock/catalog.json",
        "relation": "与原 applications.json（120 条）并列，运行时由 MockAdapter 合并为 188 条；"
                    "原文件不改动，评测基线因此保持稳定。",
        "fictional_notice": "全部数据为虚构，不对应任何真实企业、自然人、电话号码或邮箱。",
    }
    with open(os.path.join(OUT_DIR, "applications_catalog.json"), "wb") as f:
        body = json.dumps({"_meta": meta, "items": records}, ensure_ascii=False, indent=2)
        f.write((body + "\n").replace("\r\n", "\n").encode("utf-8"))

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    for r in records:
        w.writerow({k: _csv_value(r[k]) for k in fields})
    with open(os.path.join(OUT_DIR, "applications_catalog.csv"), "wb") as f:
        f.write(b"\xef\xbb\xbf" + buf.getvalue().replace("\r\n", "\n").encode("utf-8"))

    tel = sum(1 for r in records if r["close_mode"] == "电话")
    print("generated %d records, %d fields -> config/mock/applications_catalog.{json,csv}"
          % (len(records), len(fields)))
    print("关单方式：电话 %d / 京麦 %d ｜ 待外呼 %d 条"
          % (tel, len(records) - tel,
             sum(1 for r in records if r["call_status"] == "待外呼")))


if __name__ == "__main__":
    main()
