# -*- coding: utf-8 -*-
"""校验 config/mock/ 这一整套 mock 数据（不是只校验申请单）。

为什么单独一个校验器：
    mock 从"两个客户 + 120 条档案"扩成"目录 68 + 话术 52 + QA 28 + 档案 188 +
    客户 12 + 订单 16"之后，**文件之间的引用**成了新的出错面：
    档案里的 reject_code 指向不存在的场景、订单挂在不存在的客户上、
    目录说电话但档案标京麦……这些都不会让服务崩，只会让演示说错话。
    所以把"跨文件一致性"做成可复算的检查，挂进总闸门。

七类检查：
    ① 文件齐全   ② 官方目录完整（68/68、关单方式分布、业务域枚举）
    ③ 话术与 QA ④ 申请单覆盖与可回溯   ⑤ 合并池唯一性
    ⑥ 客户↔订单引用完整   ⑦ 可复算（重新生成逐字节一致）

跑法：python scripts/check_mock_bundle.py [--self-test]
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK = os.path.join(ROOT, "config", "mock")
sys.path.insert(0, ROOT)

from scripts.build_mock_bundle import build as build_bundle  # noqa: E402
from scripts.gen_mock_catalog_applications import build_catalog_records  # noqa: E402

DOMAINS = {"品牌-授权", "品牌-商标", "品牌-全球购", "商品与行业资质",
           "入驻与主体店铺", "京麦驳回", "未定位"}
UNIQUE_FIELDS = [
    "application_id", "merchant_id", "company_name", "company_short_name",
    "shop_name", "shop_id", "contact_name", "legal_representative",
    "contact_phone", "phone_tail", "contact_email", "brand_name",
    "credit_code", "id_card",
]
REQUIRED_FILES = [
    "catalog.json", "rebuttals.json", "qa.json",
    "applications.json", "applications.csv",
    "applications_catalog.json", "applications_catalog.csv",
    "customers.json", "orders.json",
]


def _read(fname):
    with open(os.path.join(MOCK, fname), encoding="utf-8") as f:
        return json.load(f)


def _items(fname):
    return _read(fname).get("items") or []


def _sha(obj):
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def validate(bundle=None):
    """返回 (problems, stats)。problems 为空才算通过。"""
    problems = []
    stats = {}

    # ① 文件齐全
    for f in REQUIRED_FILES:
        if not os.path.exists(os.path.join(MOCK, f)):
            problems.append("缺文件：config/mock/%s" % f)
    if problems:
        return problems, stats

    catalog = _items("catalog.json")
    rebuttals = _items("rebuttals.json")
    qa = _items("qa.json")
    legacy = _items("applications.json")
    newset = _items("applications_catalog.json")
    customers = _items("customers.json")
    orders = _items("orders.json")
    stats.update({"catalog": len(catalog), "rebuttals": len(rebuttals),
                  "qa": len(qa), "legacy": len(legacy), "catalog_apps": len(newset),
                  "customers": len(customers), "orders": len(orders),
                  "pool": len(legacy) + len(newset)})

    # ② 官方目录
    if len(catalog) != 68:
        problems.append("官方场景目录应为 68 条，实际 %d" % len(catalog))
    ids = [c.get("id") for c in catalog]
    if ids != ["KB-POP-%03d" % i for i in range(1, len(catalog) + 1)]:
        problems.append("场景 id 不连续/不按序：%s…" % ids[:3])
    modes = {"电话": 0, "京麦": 0}
    for c in catalog:
        m = c.get("close_mode")
        if m not in modes:
            problems.append("场景 %s 关单方式非法：%r" % (c.get("id"), m))
        else:
            modes[m] += 1
        if c.get("domain") not in DOMAINS:
            problems.append("场景 %s 业务域非法：%r" % (c.get("id"), c.get("domain")))
        if not (c.get("materials") or []):
            problems.append("场景 %s 没有需补材料" % c.get("id"))
        if not c.get("script") and not c.get("script_missing"):
            problems.append("场景 %s 既无话术也未标 script_missing" % c.get("id"))
    stats["close_modes"] = modes
    if modes != {"电话": 54, "京麦": 14}:
        problems.append("关单方式分布应为 电话54/京麦14，实际 %s" % modes)

    # ③ 话术与 QA
    if len(rebuttals) < 50:
        problems.append("驳回话术少于 50 条：%d" % len(rebuttals))
    for r in rebuttals:
        if not (r.get("script") or "").strip():
            problems.append("话术 %s 内容为空" % r.get("id"))
    if len(qa) < 20:
        problems.append("QA 少于 20 条：%d" % len(qa))
    qs = [q.get("question") for q in qa]
    if len(qs) != len(set(qs)):
        problems.append("QA 问题有重复")
    for q in qa:
        if not (q.get("answer") or "").strip():
            problems.append("QA %s 没有答案" % q.get("id"))

    # ④ 申请单：覆盖 + 可回溯 + 关单方式自洽
    covered = {r.get("catalog_id") for r in newset}
    missing = [c["id"] for c in catalog if c["id"] not in covered]
    if len(newset) != 68:
        problems.append("官方场景档案应为 68 条，实际 %d" % len(newset))
    if missing:
        problems.append("有 %d 个官方场景没有对应档案：%s" % (len(missing), missing[:5]))
    cat_by_id = {c["id"]: c for c in catalog}
    for r in newset:
        c = cat_by_id.get(r.get("catalog_id"))
        if not c:
            problems.append("档案 %s 的 catalog_id 不在目录中" % r.get("application_id"))
            continue
        if r.get("reject_code") != c["id"]:
            problems.append("档案 %s 的 reject_code 与 catalog_id 不一致"
                            % r.get("application_id"))
        if r.get("close_mode") != c["close_mode"]:
            problems.append("档案 %s 关单方式与官方场景不一致（%s vs %s）"
                            % (r.get("application_id"), r.get("close_mode"), c["close_mode"]))
        if r.get("reject_category") != c["domain"]:
            problems.append("档案 %s 业务域与目录映射不一致" % r.get("application_id"))
    pool = legacy + newset
    for r in pool:
        if r.get("reject_category") not in DOMAINS:
            problems.append("档案 %s 业务域非法" % r.get("application_id"))
        if r.get("close_mode") == "京麦" and r.get("call_status") != "无需外呼":
            problems.append("京麦档案 %s 的通话状态应为无需外呼" % r.get("application_id"))
        if r.get("close_mode") == "电话" and r.get("call_status") == "无需外呼":
            problems.append("电话档案 %s 不该标无需外呼" % r.get("application_id"))
        if not str(r.get("contact_phone", "")).endswith(str(r.get("phone_tail", "~"))):
            problems.append("档案 %s 尾号与号码不一致" % r.get("application_id"))
        if not str(r.get("contact_phone", "")).startswith("1990000"):
            problems.append("档案 %s 电话不是虚构号段" % r.get("application_id"))
        if not str(r.get("contact_email", "")).endswith("@example.test"):
            problems.append("档案 %s 邮箱不是 .test 保留域" % r.get("application_id"))
        if r.get("fictional_marker") != "SIM-2026-TEST-FICTIONAL":
            problems.append("档案 %s 缺虚构标记" % r.get("application_id"))

    # ⑤ 合并池唯一性
    for f in UNIQUE_FIELDS:
        vals = [r.get(f) for r in pool]
        dup = len(vals) - len(set(vals))
        if dup:
            problems.append("合并池字段 %s 有 %d 个重复" % (f, dup))

    # ⑥ 客户 ↔ 订单
    cids = [c.get("customer_id") for c in customers]
    if len(cids) != len(set(cids)):
        problems.append("客户编号有重复")
    tails = [c.get("phone_tail") for c in customers]
    if len(tails) != len(set(tails)):
        problems.append("客户尾号有重复（核身会撞车）")
    cset = set(cids)
    orphans = [o.get("order_id") for o in orders if o.get("customer_id") not in cset]
    if orphans:
        problems.append("订单挂在不存在的客户上：%s" % orphans[:3])
    for c in customers:
        if not any(o.get("customer_id") == c["customer_id"] for o in orders):
            problems.append("客户 %s 没有任何订单" % c["customer_id"])
    # 与原 Mock 业务服务同值（两边是同一套演示数据）
    by_id = {c["customer_id"]: c for c in customers}
    if by_id.get("C1001", {}).get("phone_tail") != "8866" or \
            by_id.get("C1002", {}).get("phone_tail") != "2143":
        problems.append("C1001/C1002 与 Mock 业务服务的同值约定被破坏")
    mine = [o for o in orders if o.get("customer_id") == "C1001"]
    latest = max(mine, key=lambda o: str(o.get("created_at") or "")) if mine else {}
    if latest.get("order_id") != "O20261101001":
        problems.append("C1001 最近一笔订单应为 O20261101001，实际 %r" % latest.get("order_id"))
    stats["customers"] = len(customers)
    stats["orders"] = len(orders)

    # ⑦ 可复算
    if bundle is None:
        _meta, cat2, rb2, qa2 = build_bundle()
        new2 = build_catalog_records()
    else:
        cat2, rb2, qa2, new2 = bundle
    for name, a, b in (("catalog", catalog, cat2), ("rebuttals", rebuttals, rb2),
                       ("qa", qa, qa2), ("applications_catalog", newset, new2)):
        if _sha(a) != _sha(b):
            problems.append("%s 不可复算（重新生成后内容不一致）" % name)
    stats["sha"] = {"catalog": _sha(catalog), "catalog_apps": _sha(newset),
                    "pool": _sha(pool)}
    return problems, stats


def self_test():
    """故意改坏数据，校验器必须逐项报红——防"校验器自己假绿"。"""
    catalog = _items("catalog.json")
    rebuttals = _items("rebuttals.json")
    qa = _items("qa.json")
    newset = _items("applications_catalog.json")
    customers = _items("customers.json")
    orders = _items("orders.json")
    legacy = _items("applications.json")

    def mutate_drop_scene():
        n = copy.deepcopy(newset); n.pop(3)
        return catalog, rebuttals, qa, n, customers, orders, legacy

    def mutate_bad_code():
        n = copy.deepcopy(newset)
        n[0]["reject_code"] = "KB-POP-999"
        return catalog, rebuttals, qa, n, customers, orders, legacy

    def mutate_close_mode():
        n = copy.deepcopy(newset)
        n[0]["close_mode"] = "京麦" if n[0]["close_mode"] == "电话" else "电话"
        return catalog, rebuttals, qa, n, customers, orders, legacy

    def mutate_dup_tail():
        n = copy.deepcopy(newset)
        n[1]["phone_tail"] = n[0]["phone_tail"]
        n[1]["contact_phone"] = "1990000" + n[0]["phone_tail"]
        return catalog, rebuttals, qa, n, customers, orders, legacy

    def mutate_orphan_order():
        o = copy.deepcopy(orders)
        o[0]["customer_id"] = "C9999"
        return catalog, rebuttals, qa, newset, customers, o, legacy

    def mutate_catalog_missing():
        c = copy.deepcopy(catalog); c.pop(0)
        return c, rebuttals, qa, newset, customers, orders, legacy

    cases = {
        "少一条场景档案": mutate_drop_scene,
        "reject_code 不可回溯": mutate_bad_code,
        "关单方式与官方不一致": mutate_close_mode,
        "合并池尾号重复": mutate_dup_tail,
        "订单挂在不存在的客户上": mutate_orphan_order,
        "官方目录缺一条": mutate_catalog_missing,
    }
    ok = True
    for name, make in cases.items():
        cat, rb, q, n, cu, od, lg = make()
        # 用文件级校验的同一套规则，但喂改坏后的数据
        probs = _validate_data(cat, rb, q, lg, n, cu, od)
        caught = bool(probs)
        print("  [%s] 变异「%s」→ %s" % ("PASS" if caught else "FAIL", name,
                                        probs[0] if probs else "校验器没抓到！"))
        ok = ok and caught
    return ok


def _validate_data(catalog, rebuttals, qa, legacy, newset, customers, orders):
    """把 validate() 里"数据层"的规则抽出来，供 self_test 直接喂内存对象。"""
    problems = []
    if len(catalog) != 68:
        problems.append("官方场景目录应为 68 条，实际 %d" % len(catalog))
    cat_by_id = {c["id"]: c for c in catalog}
    covered = {r.get("catalog_id") for r in newset}
    missing = [c["id"] for c in catalog if c["id"] not in covered]
    if missing:
        problems.append("有 %d 个官方场景没有对应档案" % len(missing))
    for r in newset:
        c = cat_by_id.get(r.get("catalog_id"))
        if not c:
            problems.append("档案 %s 的 catalog_id 不在目录中" % r.get("application_id"))
            continue
        if r.get("reject_code") != c["id"]:
            problems.append("档案 %s 的 reject_code 与 catalog_id 不一致"
                            % r.get("application_id"))
        if r.get("close_mode") != c["close_mode"]:
            problems.append("档案 %s 关单方式与官方场景不一致" % r.get("application_id"))
    pool = legacy + newset
    for f in UNIQUE_FIELDS:
        vals = [r.get(f) for r in pool]
        if len(vals) != len(set(vals)):
            problems.append("合并池字段 %s 有重复" % f)
    cset = {c["customer_id"] for c in customers}
    for o in orders:
        if o.get("customer_id") not in cset:
            problems.append("订单挂在不存在的客户上：%s" % o.get("order_id"))
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    problems, stats = validate()
    print("=" * 72)
    print("mock 数据包：目录 %s ｜ 话术 %s ｜ QA %s ｜ 档案 %s(旧)+%s(新)=%s ｜ 客户 %s ｜ 订单 %s"
          % (stats.get("catalog"), stats.get("rebuttals"), stats.get("qa"),
             stats.get("legacy"), stats.get("catalog_apps"), stats.get("pool"),
             stats.get("customers"), stats.get("orders")))
    if stats.get("close_modes"):
        print("关单方式：%s ｜ 场景 sha=%s ｜ 新增档案 sha=%s"
              % (stats["close_modes"], stats.get("sha", {}).get("catalog"),
                 stats.get("sha", {}).get("catalog_apps")))
    print("-" * 72)
    if problems:
        print("FAIL %d 项：" % len(problems))
        for p in problems[:20]:
            print("  -", p)
    else:
        print("全部通过：文件齐全/官方目录/话术与QA/覆盖与回溯/合并池唯一/客户订单引用/可复算")

    if args.self_test:
        print("-" * 72)
        print("反向自测（故意改坏，校验器必须报红）：")
        if not self_test():
            print("反向自测失败：校验器存在假绿")
            return 1
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
