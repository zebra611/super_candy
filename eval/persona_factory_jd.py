#!/usr/bin/env python3
"""京东客服场景的对抗性人物（persona）工厂。

为什么用工厂而不是手写清单：
    「数百职业 × 数百语气 × 数十态度 × 多语言方言」是组合爆炸，手写到几十条就到头，
    而且改一个维度要全量重写。这里把四个维度拆成受控词表，按固定步长组合，
    生成过程完全确定：同样的输入永远得到同一批人物，回归才有基准。

四个维度：
    职业 160 种｜语气 60 种（×3 档强度 = 180 种组合）｜态度 30 种｜语言/方言 20 种
    场景 10 种（包裹丢失、错发、漏发、破损、商家纠纷、退款、发票、物流停滞、拒收、假货）

每条人物给出：场景、职业、语气、态度、语言/方言、典型表达、真实诉求、得体回复要点。
后两项是关键——没有它们，人物库只能"测出问题"，说不出"应该怎么答"。

用法：
    python3 eval/persona_factory_jd.py --n 320 --out eval/personas_jd.json
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# 维度一：职业（160 种，覆盖制造/服务/专业/农业/新兴行业）
# ---------------------------------------------------------------------------
PROFESSIONS = """快递员 外卖骑手 网约车司机 货车司机 公交司机 地铁站务 飞行员 空乘 铁路乘务
船员 渔民 农民 果农 菜贩 屠户 面点师 厨师 餐厅服务员 咖啡师 调酒师 茶艺师
酒店前台 客房服务员 导游 景区讲解员 旅行社计调 民宿房东 摄影师 摄像师 剪辑师
平面设计师 UI 设计师 室内设计师 建筑师 结构工程师 土木工程师 造价师 监理 电工
水暖工 木工 焊工 油漆工 空调安装工 家电维修工 手机维修师 汽车维修工 汽修学徒 4S 店销售
二手车评估师 保险代理人 银行柜员 信贷员 理财顾问 证券经纪人 会计 出纳 审计师
税务师 律师 法务 专利代理人 公证员 翻译 同声传译 编辑 记者 播音主持
新媒体运营 短视频博主 直播带货主播 电商运营 客服专员 呼叫中心班长 数据标注员 算法工程师 前端工程师 后端工程师
测试工程师 运维工程师 产品经理 项目经理 设计师助理 行政专员 人力资源 猎头 培训师 幼师
中小学教师 大学教授 研究员 实验室技术员 医生 护士 药师 兽医 心理咨询师 康复治疗师
健身教练 瑜伽老师 游泳教练 裁判员 运动员 舞蹈演员 歌手 乐手 话剧演员 相声演员
魔术师 化妆师 美发师 美甲师 美容师 裁缝 服装打版师 鞋匠 珠宝鉴定师 钟表维修师
眼镜验光师 花艺师 宠物医生 宠物美容师 家政阿姨 月嫂 育儿嫂 保洁员 保安 物业管家
房产中介 房屋租赁管家 装修工长 搬家师傅 废品回收员 再生资源分拣员 快递驿站老板 便利店店主 餐饮小老板 药店店主
服装店主 五金店老板 水果店老板 母婴店老板 数码店主 汽配店主 建材老板 农资经销商 化肥销售 种子销售
农机手 养殖户 兽医助理 乡村医生 村支书 社区网格员 社工 志愿者 消防员 警察
军人 海关关员 检疫员 邮政投递员 仓库管理员 叉车司机 分拣员 质检员 采购员 供应链专员
外贸业务员 报关员 单证员 展会策划 活动执行 婚礼策划 殡葬礼仪师 月老红娘 婚介顾问 家政中介""".split()

# ---------------------------------------------------------------------------
# 维度二：语气（60 种，配合三档强度形成 180 种组合）
# ---------------------------------------------------------------------------
TONES = """急促 缓慢 絮叨 简短 冷淡 热情 客气 敷衍 嘲讽 委屈
强硬 试探 犹豫 坚定 慌乱 平静 焦躁 撒娇 抱怨 指责
哀求 命令 商量 反问 自嘲 夸张 克制 啰嗦 断续 含混
阴阳怪气 一本正经 油腔滑调 低声下气 理直气壮 小心翼翼 咄咄逼人 慢条斯理 火急火燎 云淡风轻
絮絮叨叨 简明扼要 拐弯抹角 单刀直入 欲言又止 滔滔不绝 沉默寡言 强压怒火 半信半疑 将信将疑
不依不饶 得理不饶人 前倨后恭 先礼后兵 嬉皮笑脸 义正词严 语无伦次 气急败坏 好声好气 爱答不理""".split()

INTENSITY = [
    {"level": 1, "label": "轻微", "suffix": "语气还算平静"},
    {"level": 2, "label": "明显", "suffix": "情绪已经外露"},
    {"level": 3, "label": "激烈", "suffix": "已经压不住情绪"},
]

# ---------------------------------------------------------------------------
# 维度三：态度（30 种）
# ---------------------------------------------------------------------------
ATTITUDES = """愤怒 焦虑 冷漠 礼貌 咄咄逼人 委屈 怀疑 敷衍 恳求 讽刺
不耐烦 戒备 失望 无助 强势 谨慎 犹豫 认命 试探 讨好
敌意 疏离 配合 抵触 观望 自责 迁怒 较真 宽容 麻木""".split()

# ---------------------------------------------------------------------------
# 维度四：语言与方言（20 种）
# ---------------------------------------------------------------------------
LANGUAGES = [
    {"code": "zh-CN", "name": "普通话", "features": ["标准书面语混口语", "少方言词"],
     "markers": ["您好", "麻烦您", "谢谢"]},
    {"code": "zh-yue", "name": "粤语", "features": ["句末语气词多", "语序偶有倒装", "保留入声词"],
     "markers": ["唔该", "係咁先", "冇问题", "点算啊"]},
    {"code": "zh-minnan", "name": "闽南语", "features": ["文白异读", "称谓语前置", "语气词丰富"],
     "markers": ["歹势", "无要紧", "汝讲啥", "多谢汝"]},
    {"code": "zh-sichuan", "name": "四川话", "features": ["儿化与叠词多", "语速偏快", "爱用反问"],
     "markers": ["要得", "搞啥子", "莫得问题", "晓得咯"]},
    {"code": "zh-northeast", "name": "东北话", "features": ["儿化音重", "夸张程度副词", "自嘲式幽默"],
     "markers": ["咋整啊", "整不明白", "那可不", "得瑟啥"]},
    {"code": "zh-henan", "name": "河南话", "features": ["声调下沉", "常用「中」表肯定", "短句多"],
     "markers": ["中", "咋弄嘞", "恁说啥", "木事儿"]},
    {"code": "zh-hunan", "name": "湖南话", "features": ["语速快", "句尾上扬", "爱用「咯」「哒」"],
     "markers": ["咯", "要得哒", "莫子意思", "搞么子"]},
    {"code": "zh-shanghai", "name": "上海话", "features": ["连读多", "语气偏软", "常用「伐」结尾"],
     "markers": ["侬好", "哪能啦", "勿要", "谢谢侬"]},
    {"code": "zh-tianjin", "name": "天津话", "features": ["儿化重", "自带调侃", "语速中快"],
     "markers": ["嘛事儿", "介是嘛", "倍儿好", "您了"]},
    {"code": "zh-shaanxi", "name": "陕西话", "features": ["声调硬", "用「咋」「咧」", "语速偏慢"],
     "markers": ["咋咧", "嫑急", "这事嘛", "得是"]},
    {"code": "zh-hakka", "name": "客家话", "features": ["保留古音", "称谓在前", "语气克制"],
     "markers": ["多谢", "无相干", "𠊎知", "样般办"]},
    {"code": "zh-shandong", "name": "山东话", "features": ["儿化与「俺」", "语气直", "爱用反问"],
     "markers": ["俺", "咋回事儿", "中不中", "你说啥"]},
    {"code": "zh-hubei", "name": "湖北话", "features": ["语速快", "句末「撒」「唦」", "爱用短句"],
     "markers": ["么事撒", "搞么斯", "晓得唦", "莫慌"]},
    {"code": "zh-jiangxi", "name": "江西话", "features": ["声调多", "语气偏硬", "词序灵活"],
     "markers": ["做么子", "晓得", "莫急", "多谢"]},
    {"code": "zh-guangxi", "name": "广西白话", "features": ["句末「嘅」「咯」", "语速平缓", "用词古朴"],
     "markers": ["唔该晒", "係咁先", "得嘅", "冇事"]},
    {"code": "en", "name": "英语", "features": ["主谓宾完整", "礼貌用语前置", "常用降级请求"],
     "markers": ["Could you", "I need", "That's unacceptable", "Please check"]},
    {"code": "ja", "name": "日语", "features": ["敬语层级", "句末缓和", "避免直接拒绝"],
     "markers": ["すみません", "確認していただけますか", "困っています", "お願いします"]},
    {"code": "ko", "name": "韩语", "features": ["敬语终结词尾", "委婉表达", "强调委屈"],
     "markers": ["죄송한데요", "확인 부탁드려요", "너무 답답해요", "감사합니다"]},
    {"code": "ru", "name": "俄语", "features": ["语气直接", "句式较长", "情绪表达外放"],
     "markers": ["Извините", "Мне нужно", "Это неприемлемо", "Проверьте"]},
    {"code": "es", "name": "西班牙语", "features": ["语速快", "情绪浓", "敬语与亲昵混用"],
     "markers": ["Perdón", "Necesito", "No es aceptable", "Por favor"]},
]

# ---------------------------------------------------------------------------
# 场景（10 种，含诉求与得体回复要点）
# ---------------------------------------------------------------------------
SCENARIOS = [
    {"id": "S01", "name": "包裹丢失", "need": "找回包裹或按规则赔付",
     "guide": "先确认运单与签收状态，再说明查件时限与赔付标准，不承诺具体金额"},
    {"id": "S02", "name": "快递丢失", "need": "确认是否丢件并要求补发",
     "guide": "区分商家发货与快递责任，给查件工单号，不替快递承诺时效"},
    {"id": "S03", "name": "错发商品", "need": "换回正确商品并承担运费",
     "guide": "核对订单与实物照片，走换货流程，明确退回运费由谁承担"},
    {"id": "S04", "name": "漏发配件", "need": "补发缺失配件",
     "guide": "按装箱清单逐项核对，缺件补发不走退货，避免让用户整单退回"},
    {"id": "S05", "name": "商品破损", "need": "退款或换新",
     "guide": "先致歉并索要照片，按破损程度给退换方案，不质疑用户人为损坏"},
    {"id": "S06", "name": "商家不发货", "need": "催发货或退款并追责",
     "guide": "查商家履约状态，说明平台介入规则与超时赔付，不替商家找借口"},
    {"id": "S07", "name": "退款未到账", "need": "查退款进度",
     "guide": "区分「已退」与「到账」，说明银行/支付渠道时效，给出可查凭证"},
    {"id": "S08", "name": "发票问题", "need": "补开或重开发票",
     "guide": "确认抬头与税号，说明电子发票重开时限，不承诺当天开出"},
    {"id": "S09", "name": "物流停滞", "need": "催促物流并给预计时间",
     "guide": "给最新轨迹与催件记录，说明异常件处理时限，不做时效保证"},
    {"id": "S10", "name": "疑似假货/商家纠纷", "need": "鉴定真伪或维权",
     "guide": "不判定真伪，引导走平台鉴定/举证流程，保留证据链"},
]

# 语气强度修饰
HEDGES = {1: ["", "吧", "呢"], 2: ["说实话", "我跟你说", "你听我说"], 3: ["我不管", "今天必须", "别跟我扯"]}


def build_persona(idx):
    """按固定步长组合四个维度。步长与维度长度互质，保证长周期不重复。"""
    scenario = SCENARIOS[idx % len(SCENARIOS)]
    profession = PROFESSIONS[(idx * 7) % len(PROFESSIONS)]
    tone = TONES[(idx * 11) % len(TONES)]
    # 强度用 idx 的高位轮换，而不是 idx % 3：
    # 语气标签由 idx 唯一决定，若强度再取 idx % 3，两者被同一个数绑死，
    # 180 种组合会塌缩成 60 种。用 (idx // 60) 打散才能覆盖 60×3。
    intensity = INTENSITY[(idx // len(TONES)) % 3]
    attitude = ATTITUDES[(idx * 13) % len(ATTITUDES)]
    # 每 3 条里留 2 条普通话、1 条方言/外语，避免方言把样本带偏
    language = LANGUAGES[0] if idx % 3 else LANGUAGES[(idx * 17) % len(LANGUAGES)]

    hedge = HEDGES[intensity["level"]][idx % 3]
    m = language["markers"]
    utterances = [
        "%s%s，我有个%s的事要问。" % (m[0], hedge, scenario["name"]),
        "我是做%s的，平时没空盯物流，%s。" % (profession, m[1]),
        "%s，你们到底怎么处理？" % m[2],
        "我%s要求很简单：%s。" % (attitude, scenario["need"]),
    ]

    return {
        "id": "JD%04d" % (idx + 1),
        "scenario": {"id": scenario["id"], "name": scenario["name"],
                     "need": scenario["need"], "reply_guide": scenario["guide"]},
        "profession": profession,
        "tone": {"label": tone, "intensity": intensity["label"],
                 "level": intensity["level"], "note": intensity["suffix"]},
        "attitude": attitude,
        "language": {"code": language["code"], "name": language["name"],
                     "features": language["features"], "markers": language["markers"]},
        "utterances": utterances,
        "expect": {"closed": True, "consent": False},
        "tags": ["jd", scenario["id"].lower(), language["code"]],
    }


def build(n):
    return [build_persona(i) for i in range(n)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="京东客服对抗人物工厂")
    ap.add_argument("--n", type=int, default=320)
    ap.add_argument("--out", default=os.path.join(ROOT, "eval", "personas_jd.json"))
    args = ap.parse_args(argv)
    if args.n < 1:
        print("--n 至少为 1", file=sys.stderr)
        return 2

    items = build(args.n)
    payload = {
        "_comment": "由 eval/persona_factory_jd.py 生成，勿手改。四维组合：职业×语气×态度×语言/方言，"
                    "场景取自京东售后高频问题。每条含真实诉求与得体回复要点。",
        "_version": "v1",
        "personas": items,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("生成 %d 条 → %s" % (len(items), os.path.relpath(args.out, ROOT)))
    print("覆盖：职业 %d｜语气 %d｜态度 %d｜语言/方言 %d｜场景 %d"
          % (len({i["profession"] for i in items}), len({i["tone"]["label"] for i in items}),
             len({i["attitude"] for i in items}), len({i["language"]["code"] for i in items}),
             len({i["scenario"]["id"] for i in items})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
