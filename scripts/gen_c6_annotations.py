# -*- coding: utf-8 -*-
"""C6：给 config/kb_pop.json 打条目级官方数据噪声标注（确定性、可复算）。

原则：
  - **原文保留**，不静默改写官方话术；噪声只在条目上标注，处理动作显式可查；
  - 缺话术的条目（KB-POP-016/047）不能播出空回复 → 复用 C3 的 no_call
    检索隔离，转专员兜底；
  - `_meta.noise` 写全 9 类清单（7 类官方自曝 + 2 类我们实测发现），
    供答辩展示「我们发现并处理了官方数据的问题」。

跑法：python scripts/gen_c6_annotations.py（重复跑结果一致）
校验：python scripts/check_c6_annotations.py
"""
from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "config", "kb_pop.json")

CLASSES = [
    {"code": "C6-01", "name": "占位符两套体系",
     "official": "空的【】与【XX】【XXX】【XXXX】并存，来自不同编写批次",
     "risk": "把具名占位符当成运行时变量替换，会把话术结构念坏；删掉则语法残缺",
     "handling": "条目标注 placeholder_style；kb.render 只填空【】，具名占位符原样保留并进 missing，绝不删除或编造",
     "status": "已处理（C2 已实现，C6 逐条标注）"},
    {"code": "C6-02", "name": "关单标记混入追问列",
     "official": "特殊场景表『商家后续咨询什么问题』列混入 5 处『京麦驳回』标记",
     "risk": "把『京麦驳回』当商家问题会生成一条荒谬的追问剧本，电话里念站内指引",
     "handling": "提取阶段已剔除（证据：资料包提取.md 行 186-198）；14 条京麦类目在 kb 层打 no_call 检索隔离（C3）",
     "status": "已处理（提取过滤 + 检索隔离）"},
    {"code": "C6-03", "name": "话术结尾残缺 / 空话术",
     "official": "2 处话术结尾残缺，个别场景只有分类没有话术",
     "risk": "命中后播出空回复或半句话，是演示现场最刺耳的一类失败",
     "handling": "KB-POP-016 / KB-POP-047 的 answer 为空 → 打 no_call，检索层跳过、转专员；原文保留不补写",
     "status": "已处理（检索层隔离）"},
    {"code": "C6-04", "name": "话术与分类错配 / 主体品牌混淆",
     "official": "1 处话术与分类错配、1 处主体与品牌混淆",
     "risk": "按 category 路由时答非所问（无品牌话术挂在『授权书-文审』下）",
     "handling": "候选条目逐条标注，不强行改官方分类；路由以 patterns + 档案 reject_category 为准",
     "status": "已标注（保留原分类）"},
    {"code": "C6-05", "name": "三级分类只在组内首行（需向下填充）",
     "official": "68 条场景里只有 5 条能直接拿到一级菜单",
     "risk": "不做向下填充时 63 条场景失去业务域，五域路由全空",
     "handling": "场景汇总_结构化.json 已向下填充；条目 source_id 保留官方行号可溯源",
     "status": "已处理"},
    {"code": "C6-06", "name": "关单总数口径差 1",
     "official": "官方文字写电话 55 / 京麦 13，结构化数据实际 54 / 14",
     "risk": "答辩报数字与评委手里的官方说明对不上",
     "handling": "以结构化数据为准（54/14），并在 C6 清单里写出差异；C3 打标 14 条",
     "status": "已标注（口径以数据为准）"},
    {"code": "C6-07", "name": "追问剧本只有答案没有问句",
     "official": "特殊场景追问 17 条只有标准答案，缺触发词",
     "risk": "知识检索永远命中不了，商家一追问就落兜底话术",
     "handling": "S01-S17 按 5 类追问补触发词（10 条原有 + 7 条补），标注在 _meta.note",
     "status": "已处理"},
    {"code": "C6-08", "name": "版本未收敛（工号 / 批注未闭环）",
     "official": "『是否保留工号』被多次要求删除，外呼话术仍保留；1 条评审批注是未闭环反问",
     "risk": "把评审批注当话术播出去，或念出未定稿的工号",
     "handling": "本库 96 条逐条扫描，answer 中 0 条含『工号』（提取阶段已剔除）；官方特殊场景 QA 表仍保留『工号XXX号』，证据：资料包提取.md 行 186-198；批注列一律不入库",
     "status": "已核验（本库无残留，官方原文仍有，答辩时主动讲）"},
    {"code": "C6-09", "name": "ASR 噪声（我们实测发现，非官方自曝）",
     "official": "—",
     "risk": "咳嗽/旁人声被 ASR 识别成英文词或乱码，落进核身失败计数把在线用户挂断（11-06 真实事故 s_666ed5548f9c）",
     "handling": "纯英文/无中文无数字输入归入 evidence=noise；噪声与含糊分桶（不烧核身·含糊·追问次数）；无可核身信息的轮次不消耗追问机会；弱形式姓名抽取消要求首字为姓氏；NOISE_MAX=5 防死等；3 位尾号引导、挂断前完整重问。技术分析与三层防线见 docs/25_噪声与降噪.md",
     "status": "已处理 + 连续噪声实测 7/7 + 变异自测 2/2（scripts/test_noise_robustness.py）"},
]


def placeholder_style(answer):
    ph = re.findall(r"【([^】]*)】", answer or "")
    if not ph:
        return "none"
    has_blank = any(not x.strip() for x in ph)
    has_named = any(x.strip() for x in ph)
    if has_blank and has_named:
        return "mixed"
    return "blank" if has_blank else "named"


def main():
    with open(PATH, encoding="utf-8") as f:
        data = json.load(f)
    items = data["items"]
    counts = {}
    for r in items:
        answer = r.get("answer") or ""
        style = placeholder_style(answer)
        noise = []
        if style != "none":
            noise.append("C6-01")
            r["placeholder_style"] = style
        if not answer.strip():
            noise.extend(["C6-03", "C6-05"])
            r["no_call"] = True       # 空话术绝不播出，转专员兜底
        if "场景汇总" in str(r.get("source_id") or ""):
            if "C6-05" not in noise:
                noise.append("C6-05")
        if any(w in answer for w in ("当前申请类目不支持挂靠无品牌",
                                     "当前店铺类型不支持无品牌入驻")):
            noise.append("C6-04")
        if r["id"].startswith("KB-POP-S"):
            noise.append("C6-07")
        if "工号" in answer:
            noise.append("C6-08")
        if noise:
            r["noise"] = sorted(set(noise))
            for code in r["noise"]:
                counts[code] = counts.get(code, 0) + 1

    data.setdefault("_meta", {})["noise"] = {
        "version": "1.0",
        "annotated_by": "scripts/gen_c6_annotations.py",
        "source": "competition/官方资料包/资料包提取.md 六、数据特点与使用提示",
        "policy": "原文一字不改；逐条标注 + 检索层隔离 + 答辩展示，不静默清洗",
        "counts": counts,
        "classes": CLASSES,
    }
    body = json.dumps(data, ensure_ascii=False, indent=1)
    with open(PATH, "wb") as f:
        f.write((body + "\n").replace("\r\n", "\n").encode("utf-8"))
    print("C6 annotated %d items; noise counts: %s" % (len(items), counts))


if __name__ == "__main__":
    main()
