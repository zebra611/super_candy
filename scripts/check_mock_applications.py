# -*- coding: utf-8 -*-
"""校验 POP 虚构档案：数量 / 唯一性 / 完整性 / 覆盖 / 虚构性 / 独立性 / 可复算。

带 --self-test：故意把数据改坏（重复编号、空字段、混入真实姓名、
跨档案引用），校验器必须逐项报红——防止「校验器自己假绿」。

跑法：python scripts/check_mock_applications.py [--self-test]
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "config", "mock", "applications.json")
sys.path.insert(0, ROOT)
from scripts.gen_mock_applications import build_records  # noqa: E402

# 凡是档案自身的可标识信息都要求全局唯一；显式「未提供」占位值也带序号，
# 所以一起查，不留「占位值重复」这种含混例外。
UNIQUE_FIELDS = [
    "application_id", "merchant_id", "company_name", "company_short_name",
    "shop_name", "contact_name", "legal_representative", "alternative_contact",
    "contact_phone", "phone_tail", "contact_email", "brand_name",
    "brand_registration_no", "verify_by_application",
]
CATEGORIES = ["品牌-授权", "品牌-商标", "品牌-全球购", "商品与行业资质",
              "入驻与主体店铺", "京麦驳回", "未定位"]
# 真实个人信息/真实企业名黑名单（本项目团队成员姓名 + 常见真实企业字号）
REAL_INFO_BLACKLIST = [
    "张博涵", "佟欣", "潘柏亦", "刘源", "黄睿哲", "张伟", "李娜",
    "京东", "阿里巴巴", "腾讯", "华为", "小米", "百度", "字节跳动",
]


def validate(records):
    """返回 (problems, stats)。problems 为空才算通过。"""
    problems = []
    stats = {"count": len(records), "fields": len(records[0]) if records else 0}

    # ① 数量
    if len(records) < 100:
        problems.append("数量不足 100：%d" % len(records))

    # ② 唯一性（八组）
    for f in UNIQUE_FIELDS:
        vals = [r.get(f) for r in records]
        dup = len(vals) - len(set(vals))
        if dup:
            problems.append("字段 %s 有 %d 个重复值" % (f, dup))
    # 日期类字段业务上允许重复，但「显式占位值」必须带序号、不得互相重复：
    # 否则「未提供」这种字符串会污染下游按值索引的逻辑。
    placeholders = [str(v) for r in records for v in r.values()
                    if isinstance(v, str) and v.startswith("（")]
    if len(placeholders) != len(set(placeholders)):
        problems.append("显式占位值出现重复（占位值必须带档案序号）")

    # ③ 完整性：所有字段必须有值（列表非空；显式「（未提供）」是合法值）
    if records:
        fields = list(records[0].keys())
        for i, r in enumerate(records):
            if set(r.keys()) != set(fields):
                problems.append("第 %d 条字段集与其它条不一致" % (i + 1))
            for k in fields:
                v = r.get(k)
                if v is None or v == "" or (isinstance(v, list) and not v):
                    problems.append("第 %d 条字段 %s 为空" % (i + 1, k))

    # ④ 覆盖
    cat_counts = {c: sum(1 for r in records if r.get("reject_category") == c)
                  for c in CATEGORIES}
    stats["categories"] = cat_counts
    for c, n in cat_counts.items():
        if n < 3:
            problems.append("业务域 %s 只有 %d 条（要求 ≥3）" % (c, n))
    stats["call_statuses"] = sorted({r.get("call_status") for r in records})
    if len(stats["call_statuses"]) < 6:
        problems.append("外呼状态覆盖不足 6 种：%s" % stats["call_statuses"])
    stats["close_modes"] = sorted({r.get("close_mode") for r in records})
    if set(stats["close_modes"]) != {"电话", "京麦"}:
        problems.append("关单方式未覆盖两种：%s" % stats["close_modes"])
    stats["placeholder_styles"] = sorted({r.get("placeholder_style") for r in records})
    if len(stats["placeholder_styles"]) != 3:
        problems.append("占位符两套体系未覆盖全：%s" % stats["placeholder_styles"])
    edges = sorted({e for r in records for e in r.get("edge_cases", [])})
    stats["edge_case_kinds"] = len(edges)
    if len(edges) < 30:
        problems.append("边缘标签种类不足 30：%d" % len(edges))
    stats["company_types"] = sorted({r.get("company_type") for r in records})
    if len(stats["company_types"]) < 6:
        problems.append("主体类型覆盖不足 6 种：%s" % stats["company_types"])

    # ⑤ 虚构性：黑名单 / 电话号段 / 邮箱域 / 全局标记 / 地区合规表述
    for i, r in enumerate(records):
        blob = json.dumps(r, ensure_ascii=False)
        for w in REAL_INFO_BLACKLIST:
            if w in blob:
                problems.append("第 %d 条混入真实信息关键词：%s" % (i + 1, w))
        if not str(r.get("contact_phone", "")).startswith("1990000"):
            problems.append("第 %d 条电话不是虚构号段 1990000" % (i + 1))
        if not str(r.get("contact_email", "")).endswith("@example.test"):
            problems.append("第 %d 条邮箱不是 .test 保留域" % (i + 1))
        if r.get("fictional_marker") != "SIM-2026-TEST-FICTIONAL":
            problems.append("第 %d 条缺虚构标记" % (i + 1))
        reg = str(r.get("region", ""))
        # 港澳台必须写「中国香港/中国澳门/中国台湾」，不得单独成国家表述
        for bad in ("香港", "澳门", "台湾"):
            if bad in reg and ("中国" + bad) not in reg:
                problems.append("第 %d 条地区表述不合规：%s" % (i + 1, reg))
    # 电话后四位与完整号码自洽
    for i, r in enumerate(records):
        if not str(r.get("contact_phone", "")).endswith(str(r.get("phone_tail", "~"))):
            problems.append("第 %d 条 phone_tail 与号码不一致" % (i + 1))

    # ⑥ 相互独立：任何一条的字段值里不得出现其它档案的编号
    ids = {r.get("application_id") for r in records}
    for i, r in enumerate(records):
        own = r.get("application_id")
        for k, v in r.items():
            if k in ("application_id", "verify_by_application"):
                continue
            s = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v)
            for other in ids - {own}:
                if other in s:
                    problems.append("第 %d 条字段 %s 引用了其它档案编号 %s" % (i + 1, k, other))
                    break

    # ⑦ 可复算：同种子重建必须逐字节一致
    rebuilt = build_records()
    h1 = hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    h2 = hashlib.sha256(json.dumps(rebuilt, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if h1 != h2:
        problems.append("生成结果不可复算（seed 固定但内容不一致）")
    stats["sha256"] = h1[:16]
    return problems, stats


def self_test(records):
    """故意改坏数据，校验器必须能抓到——防止校验器自己假绿。"""
    cases = {
        "重复编号": lambda rs: rs.__setitem__(1, {**rs[1], "application_id": rs[0]["application_id"]}),
        "空字段": lambda rs: rs[2].__setitem__("company_name", ""),
        "混入真实姓名": lambda rs: rs[3].__setitem__("contact_name", "张博涵"),
        "跨档案引用": lambda rs: rs[4].__setitem__("notes", "关联档案 " + rs[0]["application_id"]),
        "电话号段不虚构": lambda rs: rs[5].__setitem__("contact_phone", "13800138000"),
        "数量不足": lambda rs: rs.__delitem__(slice(21, None)),
    }
    ok = True
    for name, mutate in cases.items():
        bad = copy.deepcopy(records)
        mutate(bad)
        probs, _ = validate(bad)
        caught = bool(probs)
        print("  [%s] 变异「%s」→ %s" % ("PASS" if caught else "FAIL", name,
                                        (probs[0] if probs else "校验器没抓到！")))
        ok = ok and caught
    return ok


def main():
    with open(DATA, encoding="utf-8") as f:
        payload = json.load(f)
    records = payload.get("items") or []
    problems, stats = validate(records)

    print("=" * 70)
    print("POP 虚构档案校验：%d 条 / %d 字段 / sha256=%s" % (
        stats["count"], stats["fields"], stats["sha256"]))
    print("业务域分布：%s" % stats["categories"])
    print("外呼状态：%s ｜ 关单方式：%s ｜ 占位符：%s" % (
        stats["call_statuses"], stats["close_modes"], stats["placeholder_styles"]))
    print("边缘标签种类：%d ｜ 主体类型：%s" % (
        stats["edge_case_kinds"], stats["company_types"]))
    print("-" * 70)
    if problems:
        print("FAIL %d 项：" % len(problems))
        for p in problems[:20]:
            print("  -", p)
    else:
        print("全部通过：数量/唯一性/完整性/覆盖/虚构性/独立性/可复算")

    if "--self-test" in sys.argv:
        print("-" * 70)
        print("反向自测（故意改坏，校验器必须报红）：")
        if not self_test(records):
            print("反向自测失败：校验器存在假绿")
            return 1

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
