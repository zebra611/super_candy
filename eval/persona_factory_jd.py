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
# 词表用空格分隔，**职业名内部不能有空格**：`.split()` 会把「UI 设计师」
# 切成「UI」「设计师」两个伪职业（初版 8 条画像是「我是做4S的」这种句子）。
PROFESSIONS = """快递员 外卖骑手 网约车司机 货车司机 公交司机 地铁站务 飞行员 空乘 铁路乘务
船员 渔民 农民 果农 菜贩 屠户 面点师 厨师 餐厅服务员 咖啡师 调酒师 茶艺师
酒店前台 客房服务员 导游 景区讲解员 旅行社计调 民宿房东 摄影师 摄像师 剪辑师
平面设计师 UI设计师 室内设计师 建筑师 结构工程师 土木工程师 造价师 监理 电工
水暖工 木工 焊工 油漆工 空调安装工 家电维修工 手机维修师 汽车维修工 汽修学徒 4S店销售
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
     "hassle": "平时没空盯物流",
     "guide": "先确认运单与签收状态，再说明查件时限与赔付标准，不承诺具体金额"},
    {"id": "S02", "name": "快递丢失", "need": "确认是否丢件并要求补发",
     "hassle": "平时没空盯物流",
     "guide": "区分商家发货与快递责任，给查件工单号，不替快递承诺时效"},
    {"id": "S03", "name": "错发商品", "need": "换回正确商品并承担运费",
     "hassle": "平时没空对单",
     "guide": "核对订单与实物照片，走换货流程，明确退回运费由谁承担"},
    {"id": "S04", "name": "漏发配件", "need": "补发缺失配件",
     "hassle": "平时没空清点装箱",
     "guide": "按装箱清单逐项核对，缺件补发不走退货，避免让用户整单退回"},
    {"id": "S05", "name": "商品破损", "need": "退款或换新",
     "hassle": "平时没空拆箱验货",
     "guide": "先致歉并索要照片，按破损程度给退换方案，不质疑用户人为损坏"},
    {"id": "S06", "name": "商家不发货", "need": "催发货或退款并追责",
     "hassle": "平时没空催商家",
     "guide": "查商家履约状态，说明平台介入规则与超时赔付，不替商家找借口"},
    {"id": "S07", "name": "退款未到账", "need": "查退款进度",
     "hassle": "平时没空对账",
     "guide": "区分「已退」与「到账」，说明银行/支付渠道时效，给出可查凭证"},
    {"id": "S08", "name": "发票问题", "need": "补开或重开发票",
     "hassle": "平时没空对账",
     "guide": "确认抬头与税号，说明电子发票重开时限，不承诺当天开出"},
    {"id": "S09", "name": "物流停滞", "need": "催促物流并给预计时间",
     "hassle": "平时没空盯物流",
     "guide": "给最新轨迹与催件记录，说明异常件处理时限，不做时效保证"},
    {"id": "S10", "name": "疑似假货/商家纠纷", "need": "鉴定真伪或维权",
     "hassle": "平时没空验货",
     "guide": "不判定真伪，引导走平台鉴定/举证流程，保留证据链"},
]

# 语气强度修饰
# 强度词必须是能独立成句的话：初版 1 档是「吧/呢」这种语气助词，拼出来是
# 「您好，吧，我有个…」；3 档的「今天必须」单独成句也不成立。
HEDGES = {1: ["", "啊", "呀"],
          2: ["说实话", "我跟你说", "您听我说"],
          3: ["我先说清楚", "今天必须给个说法", "别跟我扯别的"]}


def build_persona(idx):
    """按固定步长组合四个维度。步长与维度长度互质，保证长周期不重复。"""
    scenario = SCENARIOS[idx % len(SCENARIOS)]
    profession = PROFESSIONS[(idx * 7) % len(PROFESSIONS)]
    tone = TONES[(idx * 11) % len(TONES)]
    # 强度用 idx 的高位轮换，而不是 idx % 3：
    # 语气标签由 idx 唯一决定，若强度再取 idx % 3，两者被同一个数绑死，
    # 180 种组合会塌缩成 60 种。用 (idx // 60) 打散才能覆盖 60×3。
    intensity = INTENSITY[(idx // len(TONES)) % 3]
    # 态度不能只取 (idx*13)%30：那样四项的最小正周期都是 180，
    # idx 与 idx+180 产出**逐字节相同**（320 条里 140 条是副本，只改了 id）。
    # 加一个 idx//30 的相位后周期变 900，320 条两两不同。
    attitude = ATTITUDES[(idx * 13 + idx // len(ATTITUDES)) % len(ATTITUDES)]
    # 每 3 条里留 2 条普通话、1 条方言/外语，避免方言把样本带偏
    language = LANGUAGES[0] if idx % 3 else LANGUAGES[(idx * 17) % len(LANGUAGES)]

    hedge = HEDGES[intensity["level"]][idx % 3]
    m = language["markers"]
    # 强度词单独成句（原来直接拼在问候语后，读出来是「您好我不管，我有个…」）；
    # hassle 按场景取（原来固定写「没空盯物流」，发票/退款/假货照样说物流）；
    # 第 4 句不再把态度塞成定语（「我愤怒要求很简单」不是人话）。
    # 1 档是语气助词，直接粘在问候语后（您好啊）；2/3 档是独立短句，用逗号隔开。
    lead = (m[0] + hedge) if intensity["level"] == 1 else (m[0] + "，" + hedge)
    utterances = [
        "%s，我有个%s的事要问。" % (lead, scenario["name"]),
        "我是做%s的，%s，%s。" % (profession, scenario["hassle"], m[1]),
        "%s，你们到底怎么处理？" % m[2],
        "我挺%s的，要求很简单：%s。" % (attitude, scenario["need"]),
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


# ---------------------------------------------------------------------------
# 转成 sim_eval 能吃的画像：tone/attitude → traits，utterances → catchphrases
# ---------------------------------------------------------------------------
#: 态度 → 初始信任档。按四档归类而不是 30 个硬编码数字：改一档就整体平移，
#: 也方便解释「为什么是 0.12」——敌意/讽刺类先不信人，弱势类愿意配合。
_TRUST_BANDS = (
    (0.12, "愤怒 咄咄逼人 讽刺 不耐烦 敌意 迁怒"),
    (0.22, "怀疑 戒备 谨慎 犹豫 观望 试探"),
    (0.34, "委屈 失望 无助 认命 焦虑 自责 恳求 麻木"),
    (0.28, "礼貌 冷漠 敷衍 强势 讨好 疏离 配合 抵触 较真 宽容"),
)
_TRUST0 = {w: v for v, words in _TRUST_BANDS for w in words.split()}
if len(_TRUST0) != len(ATTITUDES) or {a for a in ATTITUDES} - set(_TRUST0):
    raise SystemExit("态度表与信任档不一致：信任档 %d 条，态度 %d 条" % (len(_TRUST0), len(ATTITUDES)))


def to_sim_persona(p):
    """把一条 JD 人物转成 `sim_eval --personas` 能直接吃的画像。

    映射规则全部显式（不给模型猜）：
        语气强度 level 1/2/3 → patience 6/4/2、verbosity 0.4/0.3/0.2
        态度四档             → trust0 0.12/0.22/0.28/0.34
        方言/外语            → hearing 0.9（模拟器只做"听不清时糊掉关键要素"，
                                不等于方言识别准确率，别当指标用）
        utterances           → catchphrases（原句直接当口头禅，不二次拼接）

    **局限**：`goal` 统一是 `confirm`、`expect.closed` 统一 True。这批人物的
    评价维度是"语气/态度/方言能不能把人问崩"，不是"拒绝型客户覆盖率"——
    要测拒绝必须另写画像，因为模拟器的拒绝是耐心/信任耗尽逼出来的，
    不是台词里写一句"我不要"就能演出来的。
    """
    level = int(p["tone"]["level"])
    return {
        "id": p["id"],
        "label": "%s·%s·%s" % (p["scenario"]["name"], p["profession"], p["tone"]["label"]),
        "goal": "confirm",
        "split": "train",
        "knows": {"phone_tail": True, "contact_name": True,
                  "company_name": True, "application_id": True},
        "willing": {"phone_tail": "on_ask", "contact_name": "on_ask"},
        "traits": {"hearing": 1.0 if p["language"]["code"] == "zh-CN" else 0.9,
                   "patience": {1: 6, 2: 4, 3: 2}[level],
                   "trust0": _TRUST0[p["attitude"]],
                   "trust_gate": round(0.2 + 0.1 * (level - 1), 2),
                   "verbosity": round(0.5 - 0.1 * level, 2)},
        "style": [p["language"]["markers"][0]],
        "catchphrases": list(p["utterances"]),
        "catchphrase_rate": round(0.4 + 0.15 * level, 2),
        "expect": {"closed": True},
        "_why": ("由 personas_jd.json 的 %s 转换：语气→耐心、态度→初始信任、方言→听清率。"
                 "goal 统一 confirm（售后客户要的是解决问题），不代表拒绝型客户的覆盖率。"
                 % p["id"]),
    }


def build_sim(items):
    return [to_sim_persona(p) for p in items]


def main(argv=None):
    ap = argparse.ArgumentParser(description="京东客服对抗人物工厂")
    ap.add_argument("--n", type=int, default=320)
    ap.add_argument("--out", default=os.path.join(ROOT, "eval", "personas_jd.json"))
    ap.add_argument("--sim-out", default="",
                    help="同时输出 sim_eval 可注入的画像（如 eval/personas_jd_sim.json）")
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
    # newline="\n" 不能省：Windows 文本模式会把 \n 写成 \r\n，重建一次 1.4 万行全变。
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("生成 %d 条 → %s" % (len(items), os.path.relpath(args.out, ROOT)))
    print("覆盖：职业 %d｜语气 %d｜态度 %d｜语言/方言 %d｜场景 %d"
          % (len({i["profession"] for i in items}), len({i["tone"]["label"] for i in items}),
             len({i["attitude"] for i in items}), len({i["language"]["code"] for i in items}),
             len({i["scenario"]["id"] for i in items})))
    print("唯一性：去 id 后 %d 条内容（应为 %d）"
          % (len({json.dumps({k: v for k, v in i.items() if k != "id"},
                             ensure_ascii=False, sort_keys=True) for i in items}), len(items)))

    if args.sim_out:
        sim = {"_comment": "由 eval/persona_factory_jd.py --sim-out 生成，供 sim_eval --personas 注入",
               "_version": "v1", "personas": build_sim(items)}
        with open(args.sim_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(sim, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print("可注入画像 %d 条 → %s" % (len(sim["personas"]),
                                       os.path.relpath(args.sim_out, ROOT)))
        print("  注意：本库场景是**京东售后**，仓库当前唯一可评测场景是 POP 资审。\n"
              "  直接注入跑出来的闭环率不能当客服水平看（客户在说售后、客服在说资审），\n"
              "  它的用途是「换一种说法/脾气/方言，客服还答不答得上」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
