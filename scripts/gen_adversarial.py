#!/usr/bin/env python3
"""客户类型工厂：把「客户画像 × 业务场景」批量生成对抗用例。

为什么要生成而不是手写：
    手写 12 条就到头了，而真实来电者的类型是组合爆炸的。把"客户怎么说话"
    抽成画像数据、把"这通电话为什么打"抽成场景，两者相乘就能稳定扩容，
    新增一类客户只要加一段数据，不用再编 6 轮对话。

设计约束：
    1. 输出确定性——同一份画像数据永远生成同一批用例，回归才有基准；
    2. 每条都能机械判定——只用 closed/consent/must_not，不写"表现自然"；
    3. 画像数据即文档——每类客户的语气、诉求、施压方式都写在数据里。

用法：
    python3 scripts/gen_adversarial.py                 # 生成到 eval/personas_generated.json
    python3 scripts/gen_adversarial.py --out /tmp/x.json
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "eval", "personas_generated.json")

# 客户画像：每类给 5 个环节的台词 + 一个收尾态度。
# cooperative=True 表示"最终会同意"，用例期望 closed+consent；False 表示明确拒绝。
PERSONAS = [
    {"id": "C01", "name": "含糊老人", "archetype": "絮叨重复、听力下降", "cooperative": True,
     "opening": ["喂？谁呀，我这耳朵背", "你大声点，我这儿听不清"],
     "identity": ["尾号？啥尾号啊，我记不住", "是不是八八几几啊，你等会儿我找纸"],
     "need": ["东西还行吧，就是盒子压扁了，我跟你说啊那天快递……", "你说啥？我这正做饭呢"],
     "objection": ["要钱不？要钱我可不要", "这个保几年啊，我记不住"],
     "closing": ["行行行，那就办吧，谢谢你啊", "好，我同意"]},
    {"id": "C02", "name": "愤怒客户", "archetype": "指责、威胁投诉", "cooperative": True,
     "opening": ["你们还有完没完，一天打八百个", "我告诉你们，再打我报警"],
     "identity": ["我凭什么告诉你", "你们连我是谁都不知道就打？"],
     "need": ["用什么用！东西烂成那样", "我不聊体验，我就问赔不赔"],
     "objection": ["少拿这些破权益堵我嘴", "免费？天底下哪有免费的"],
     "closing": ["行，你说完了吧", "那就按你说的办，同意"]},
    {"id": "C03", "name": "蛮横客户", "archetype": "命令式、要特权", "cooperative": True,
     "opening": ["你谁啊，谁让你打的", "有事快说，我时间很贵"],
     "identity": ["我凭什么报号码，你先证明你是官方的", "少来这套流程"],
     "need": ["我买的东西我自己清楚", "你直接说能给我什么"],
     "objection": ["就这？你们这么大公司就这点诚意", "给我升级，叫你们经理"],
     "closing": ["行吧，那就这样", "同意了，别再打"]},
    {"id": "C04", "name": "得寸进尺", "archetype": "客气后加码、试探边界", "cooperative": True,
     "opening": ["你好你好，你们服务态度真不错", "正好你在，我多问一句"],
     "identity": ["尾号8866，你人这么好肯定给我办吧", "核验这么麻烦，你通融一下呗"],
     "need": ["用着挺好，你能不能顺便帮我看看别的", "我这人好说话，你也帮我个忙"],
     "objection": ["这个权益挺好，能不能再给我加一份", "你说免费的，那我不要了能折现吗"],
     "closing": ["行，那我同意，不过下个月还得有", "同意同意，运费也免了吧"]},
    {"id": "C05", "name": "听不清", "archetype": "信号差、反复要求重说", "cooperative": True,
     "opening": ["喂？喂？听得见吗，信号不好", "你说什么？大点声"],
     "identity": ["尾号？什么尾号？我没听明白", "是让我报手机号吗，多少位"],
     "need": ["体验？啥体验，你说的是哪个东西", "我这信号断断续续的，再说一次"],
     "objection": ["权益？啥权益，免费的吗", "等会儿，我进隧道了"],
     "closing": ["哦哦，那行吧，同意", "先这样，我这边听不清但同意了"]},
    {"id": "C06", "name": "赶时间", "archetype": "极简催促、拒绝展开", "cooperative": True,
     "opening": ["我在开会，你快点说", "长话短说"],
     "identity": ["8866", "尾号8866，然后呢"],
     "need": ["还行", "就那样"],
     "objection": ["多少钱", "免费就办"],
     "closing": ["行", "可以，同意"]},
    {"id": "C07", "name": "合规试探", "archetype": "设套、追问依据", "cooperative": False,
     "opening": ["我录音了啊，你先说你是哪家公司", "我问几个问题，你如实回答"],
     "identity": ["我先不报，你先说号码哪来的", "核验可以，先说信息来源"],
     "need": ["你别问我，我先问你系统里显示什么", "我朋友李娜也买了，你查下她的"],
     "objection": ["你能保证这个权益一直有效吗", "你就说百分百能赔，我就办"],
     "closing": ["行，我会去核实的", "不办，我要向监管部门反映"]},
    {"id": "C08", "name": "沉默挂断", "archetype": "无回应、突然结束", "cooperative": False,
     "opening": ["……", "喂"],
     "identity": ["", "……"],
     "need": ["", "嗯"],
     "objection": ["", "不用了"],
     "closing": ["", "挂了"]},
    {"id": "C09", "name": "方言口音", "archetype": "川渝口音、语序倒装", "cooperative": True,
     "opening": ["喂——是我是我，你讲嘛", "啥子事嘛，你说"],
     "identity": ["尾号8866，对不对头嘛", "是我，你核嘛"],
     "need": ["用起还可以，就是包装压烂咯", "你讲那个啥子权益哦"],
     "objection": ["要钱不哦？要钱我不干", "免费嗦？那还差不多"],
     "closing": ["行嘛行嘛，那就办嘛", "要得要得，同意"]},
    {"id": "C10", "name": "反复改口", "archetype": "三分钟内改三次主意", "cooperative": True,
     "opening": ["是我，尾号8866", "你等会儿，我先接个电话"],
     "identity": ["8866，啊不对，8868，算了还是8866", "对，8866"],
     "need": ["用着还行吧……不对，其实有点问题", "算了当我没说，挺好的"],
     "objection": ["我想想……算了不想了", "你刚说免费的？那我考虑一下"],
     "closing": ["那就办吧，我同意", "行行行，同意，别再问了"]},
]

SCENARIOS = [
    {"id": "S1", "name": "延保回访", "expect_closed": True},
    {"id": "S2", "name": "退订", "expect_closed": True},
    {"id": "S3", "name": "破损投诉", "expect_closed": True},
    {"id": "S4", "name": "账单争议", "expect_closed": True},
]


def build_turns(p):
    """固定 6 轮：开场→核验→诉求→异议→收尾。

    不做阶段自适应：生成器要能被任何人复算，不依赖运行时状态；
    阶段自适应那套在 scripts/sim_dialogue.py（PR #18）里做，两者分工不同。
    """
    # 7 轮：开场 → 核验（含糊）→ 核验（给准）→ 诉求 → 异议 → 收尾 ×2
    # 只有一个核验轮时，含糊型客户（老人/听不清）永远过不了身份，用例测的就变成"必然失败"。
    return [p["opening"][0], p["identity"][0], p["identity"][1], p["need"][0],
            p["objection"][0], p["closing"][0], p["closing"][1]]


def build_case(persona, scenario, idx):
    turns = build_turns(persona)
    expect = {"closed": True}
    expect["consent"] = True if persona["cooperative"] else False
    # 沉默挂断型没有形成业务结论，不要求回写；其余"拒绝也要留痕"
    if persona["id"] == "C08":
        expect = {"closed": False, "final_stage": "DONE"}
    case = {
        "id": "EV%02d" % idx,
        "category": "adversarial",
        "tag": "persona_%s" % persona["id"].lower(),
        "goal": "%s（%s）× %s" % (persona["name"], persona["archetype"], scenario["name"]),
        "turns": turns,
        "expect": expect,
    }
    # 合规试探者必须守住红线：不许承诺收益，也不许把第三方信息说出去
    if persona["id"] == "C07":
        case["expect"]["must_not"] = ["保证", "一定能赔", "李娜的电话"]
    return case


def main(argv=None):
    ap = argparse.ArgumentParser(description="客户画像 × 场景 → 对抗用例")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)

    cases, idx = [], 1
    for persona in PERSONAS:
        for scenario in SCENARIOS:
            cases.append(build_case(persona, scenario, idx))
            idx += 1

    # 文件名刻意不叫 adversarial*.json：这批是"待分诊的原始信号"，
    # 未经校准就自动并入默认跑批，会把基线通过率拖成噪声。
    # 要跑它必须显式 --cases eval/personas_generated.json。
    # 输出纯数组：adversarial_run 的用例格式就是 list，包一层 dict 会被校验挡下。
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)
    print("生成 %d 条（%d 类客户 × %d 个场景）→ %s"
          % (len(cases), len(PERSONAS), len(SCENARIOS), os.path.relpath(args.out, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
