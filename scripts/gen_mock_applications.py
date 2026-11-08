# -*- coding: utf-8 -*-
"""生成 POP 商家资质审核驳回「申请单 + 商家主体」拟真虚构档案（C5 mock 测试用）。

设计要点：
  - 确定性：random.Random(SEED) 固定种子，任何时候重跑逐字节一致；
  - 全虚构：字号/品牌/人名均为自造词，电话为 199-0000-XXXX 虚构不可拨号段，
    邮箱为 .test 保留域；不含任何真实个人信息（含本团队成员姓名）；
  - 全字段：每条档案 47 个字段全部有值（刻意留空的边缘场景用「（未提供）」
    这样的显式占位值表达，不用 null，方便下游无脑消费）；
  - 相互独立：不引用其它档案的编号，无父子/关联关系；
  - 互不重复：application_id / merchant_id / company_name / shop_name /
    contact_name / contact_phone / phone_tail / brand_name 八组全局唯一；
  - 覆盖：六个业务域 × 20 条，30+ 种边缘标签按配额铺开。

产物：config/mock/applications.json（主数据，UTF-8/LF）、applications.csv（平铺表格）。
跑法：python scripts/gen_mock_applications.py
校验：python scripts/check_mock_applications.py
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "config", "mock")
SEED = 20261106
N = 120  # ≥100，留 20 条余量
HOTLINE = "4006180092"

# ---------------------------------------------------------------------------
# 词池：全部为自造词 / 公开地名 / 虚构号段，不对应任何真实主体或自然人
# ---------------------------------------------------------------------------
SIZI = [  # 公司字号（自造双字词）
    "霁川", "砚舟", "星澈", "栖梧", "云杪", "澜序", "青砚", "闻樾", "南栀", "鹤汀",
    "松曜", "竹蹊", "澄屿", "锦笙", "月杳", "风禾", "鹿鸣", "雪堂", "知珩", "望舒",
    "清樾", "拾光", "茶蘼", "扶苏", "白榆", "木樨", "江蓠", "若华", "昭禾", "允舟",
    "景行", "静姝", "其琛", "维桢", "凯风", "蓼萧", "湛露", "菁莪", "庭柯", "初篁",
    "晚舟", "烟渚", "砚秋", "听澜", "疏桐", "寒山", "归雁", "长庚", "朝槿", "暮雪",
    "汀兰", "溪亭", "墨岚", "青岚", "碧落", "丹枫", "苍梧", "瑶光", "星野", "竹喧",
    "荷风", "松月", "云涛", "石泉", "霜华", "溪桥", "烟岚", "林樾", "川上", "山止",
    "川行", "水木", "云间", "风眠", "月栖", "烟汀", "归堂", "拾野", "半山", "川屿",
    "屿风", "山茶", "南风", "青禾", "白鹿", "云鹿", "月鹿", "松鹿", "栖鹿", "林泉",
    "云岑", "石屿", "霜禾", "霁月", "砚山", "听雪", "松风", "竹月", "溪云", "岚光",
    "青霭", "星澜", "澄川", "栖云", "闻溪", "知舟", "望川", "归鸿", "白苹", "红蓼",
    "紫苏", "青葙", "白芷", "杜若", "辛夷", "蘅芜", "菖蒲", "茯苓", "远志", "连翘",
    "沉香", "苏木", "降真", "龙脑", "芸香", "薄荷", "泽兰", "佩兰", "藿香", "苍术",
    "厚朴", "陈皮", "青黛", "紫草", "茜草", "地黄", "玄参", "麦冬", "天冬", "玉竹",
]

REGIONS = [
    "北京市", "上海市", "天津市", "重庆市", "河北省", "山西省", "辽宁省", "吉林省",
    "黑龙江省", "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省", "河南省",
    "湖北省", "湖南省", "广东省", "海南省", "四川省", "贵州省", "云南省", "陕西省",
    "甘肃省", "青海省", "内蒙古自治区", "广西壮族自治区", "西藏自治区",
    "宁夏回族自治区", "新疆维吾尔自治区", "中国香港", "中国澳门", "中国台湾",
]

# 注册地址的行政区 + 路名池：(市, 区/县, 道路)。
#
# 为什么要这张表：11-08 之前的地址是 `"%s创新大道%d号%d层"`——**188 条档案共用
# 一条"创新大道"**，测试页点「随机预览」连着点几次，看到的都是"XX省创新大道
# 760号18层"，一眼就是程序拼出来的。地址是预览卡片上最显眼的一行，假得太整齐
# 会让人先怀疑数据、再怀疑系统。
#
# 现在：省市/区县/路名都是真实行政区划与常见道路名（路名不是主体，不涉及真实企业），
# 门牌号与楼层仍由 seq 决定（虚构），所以既不撞真实地址，也不会重复成一模一样。
# 直辖市与港澳填 ""（市一级已在 region 里），分公司命名会退化成"…分公司"。
CITY_STREETS = {
    "北京市": [("", "海淀区", "中关村南大街"), ("", "朝阳区", "建国路"), ("", "大兴区", "科创十一街")],
    "上海市": [("", "浦东新区", "张江路"), ("", "徐汇区", "漕溪北路"), ("", "普陀区", "中江路")],
    "天津市": [("", "南开区", "卫津路"), ("", "河西区", "友谊路"), ("", "滨海新区", "第二大街")],
    "重庆市": [("", "渝北区", "金开大道"), ("", "江北区", "建新北路"), ("", "九龙坡区", "科园一路")],
    "河北省": [("石家庄市", "长安区", "中山东路"), ("保定市", "竞秀区", "朝阳北大街"),
               ("廊坊市", "广阳区", "新开路")],
    "山西省": [("太原市", "小店区", "长风街"), ("大同市", "平城区", "御河西路"),
               ("晋中市", "榆次区", "迎宾街")],
    "辽宁省": [("沈阳市", "和平区", "南京北街"), ("大连市", "中山区", "人民路"),
               ("鞍山市", "铁东区", "胜利南路")],
    "吉林省": [("长春市", "朝阳区", "人民大街"), ("吉林市", "船营区", "解放大路"),
               ("四平市", "铁西区", "中央西路")],
    "黑龙江省": [("哈尔滨市", "南岗区", "红军街"), ("大庆市", "萨尔图区", "纬二路"),
                 ("齐齐哈尔市", "龙沙区", "永安大街")],
    "江苏省": [("南京市", "建邺区", "江东中路"), ("苏州市", "姑苏区", "干将西路"),
               ("无锡市", "滨湖区", "太湖大道")],
    "浙江省": [("杭州市", "西湖区", "文三路"), ("宁波市", "鄞州区", "四明中路"),
               ("温州市", "鹿城区", "车站大道")],
    "安徽省": [("合肥市", "蜀山区", "长江西路"), ("芜湖市", "镜湖区", "北京中路"),
               ("蚌埠市", "蚌山区", "淮河路")],
    "福建省": [("福州市", "鼓楼区", "五四路"), ("厦门市", "思明区", "湖滨南路"),
               ("泉州市", "丰泽区", "田安北路")],
    "江西省": [("南昌市", "红谷滩区", "红谷中大道"), ("赣州市", "章贡区", "红旗大道"),
               ("九江市", "浔阳区", "长虹大道")],
    "山东省": [("济南市", "历下区", "经十路"), ("青岛市", "市南区", "香港中路"),
               ("烟台市", "芝罘区", "南大街")],
    "河南省": [("郑州市", "金水区", "农业路"), ("洛阳市", "洛龙区", "开元大道"),
               ("南阳市", "宛城区", "人民路")],
    "湖北省": [("武汉市", "江汉区", "建设大道"), ("宜昌市", "西陵区", "夷陵大道"),
               ("襄阳市", "樊城区", "长征路")],
    "湖南省": [("长沙市", "岳麓区", "麓谷大道"), ("株洲市", "天元区", "泰山路"),
               ("衡阳市", "蒸湘区", "船山大道")],
    "广东省": [("广州市", "天河区", "科韵路"), ("深圳市", "南山区", "科技南十二路"),
               ("东莞市", "南城区", "鸿福路")],
    "海南省": [("海口市", "龙华区", "滨海大道"), ("三亚市", "吉阳区", "迎宾路"),
               ("儋州市", "那大镇", "中兴大街")],
    "四川省": [("成都市", "高新区", "天府大道"), ("绵阳市", "涪城区", "临园路"),
               ("宜宾市", "翠屏区", "南岸西区")],
    "贵州省": [("贵阳市", "观山湖区", "长岭南路"), ("遵义市", "红花岗区", "中华南路"),
               ("六盘水市", "钟山区", "人民路")],
    "云南省": [("昆明市", "五华区", "东风西路"), ("曲靖市", "麒麟区", "南宁南路"),
               ("大理市", "下关镇", "建设路")],
    "陕西省": [("西安市", "雁塔区", "太白南路"), ("宝鸡市", "渭滨区", "经二路"),
               ("咸阳市", "秦都区", "人民西路")],
    "甘肃省": [("兰州市", "城关区", "东岗西路"), ("天水市", "秦州区", "民主路"),
               ("酒泉市", "肃州区", "解放路")],
    "青海省": [("西宁市", "城西区", "五四西路"), ("海东市", "乐都区", "碾伯大街"),
               ("格尔木市", "昆仑路", "昆仑路")],
    "内蒙古自治区": [("呼和浩特市", "新城区", "新华东街"), ("包头市", "昆都仑区", "钢铁大街"),
                     ("鄂尔多斯市", "东胜区", "天骄路")],
    "广西壮族自治区": [("南宁市", "青秀区", "民族大道"), ("柳州市", "城中区", "文昌路"),
                       ("桂林市", "象山区", "中山中路")],
    "西藏自治区": [("拉萨市", "城关区", "北京中路"), ("日喀则市", "桑珠孜区", "山东路"),
                   ("林芝市", "巴宜区", "八一南路")],
    "宁夏回族自治区": [("银川市", "金凤区", "北京中路"), ("石嘴山市", "大武口区", "朝阳西街"),
                       ("吴忠市", "利通区", "开元大道")],
    "新疆维吾尔自治区": [("乌鲁木齐市", "天山区", "人民路"), ("昌吉市", "宁边路", "延安北路"),
                         ("喀什市", "恰萨街道", "解放南路")],
    "中国香港": [("", "九龙城区", "马头围道"), ("", "观塘区", "开源道"), ("", "沙田区", "火炭路")],
    "中国澳门": [("", "花地玛堂区", "青洲大马路"), ("", "大堂区", "南湾大马路"),
                 ("", "氹仔", "基马拉斯大马路")],
    # 台北那条路原来写「南京东路」——"南京东路"里含"京东"两个字，
    # 会被虚构性校验的黑名单（真实企业关键词）误判，也确实是真实企业名的一部分。
    "中国台湾": [("", "台北市中山区", "信义路"), ("", "台中市西屯区", "台湾大道"),
                 ("", "高雄市前镇区", "中山二路")],
}

# 地址门牌之后的单元写法（轮换，避免 188 条都是"X层"）
ADDRESS_UNITS = ("%d层", "1号楼%d层", "A座%d层", "%d层东区")


def address_of(region, seq):
    """合成一条**虚构**注册地址：真实行政区与路名 + seq 决定的门牌/楼层。

    真实路名只是地理信息，不指向任何真实企业；门牌号由 seq 算出，不与真实地址重合。
    """
    spots = CITY_STREETS.get(region) or [("", "中心城区", "人民路")]
    city, district, road = spots[seq % len(spots)]
    no = 11 + (seq * 17) % 900
    unit = ADDRESS_UNITS[seq % len(ADDRESS_UNITS)] % (1 + seq % 20)
    return "%s%s%s%s%d号%s" % (region, city, district, road, no, unit)


def branch_city(region, seq):
    """分公司名里的城市部分（直辖市与港澳没有市级，返回空串）。"""
    spots = CITY_STREETS.get(region) or [("", "中心城区", "人民路")]
    return spots[seq % len(spots)][0]

COMPANY_TYPES = [
    "有限责任公司", "股份有限公司", "个体工商户", "合伙企业", "分公司",
    "个人独资企业", "外商投资企业", "港澳台投资企业",
]

SHOP_TYPES = ["旗舰店", "专卖店", "专营店", "普通店铺", "企业店铺"]

# 公司名里的**组织形式后缀**（"主体类型"另有一个 company_type 字段，不写进名字）。
#
# 之前直接拿 company_type 拼名字，出来的是「…商贸外商投资企业」「…商贸合伙企业」——
# 这是工商登记的「类型」标签，不是公司名的一部分。真实的叫法是
# 「…商贸有限公司（外商投资）」「…商贸合伙企业（有限合伙）」，名字里只保留组织形式。
# 改成这张映射后，company_type 字段的覆盖度不变（它仍是独立字段），名字先像真的。
LEGAL_SUFFIX = {
    "有限责任公司": "有限公司",
    "股份有限公司": "股份有限公司",
    "合伙企业": "合伙企业（有限合伙）",
    "个人独资企业": "中心",
    "外商投资企业": "有限公司",
    "港澳台投资企业": "有限公司",
}

SURNAMES = ["柏", "沈", "陆", "岑", "裴", "晏", "殷", "虞", "祁", "容",
            "闻", "阙", "甄", "郗", "扈", "竺", "童", "牧", "澹台", "钟离"]
GIVEN_NAMES = [
    "砚川", "见微", "听澜", "知珩", "疏影", "望舒", "允舟", "景行", "维桢", "静姝",
    "其琛", "鹿鸣", "采薇", "朝槿", "青梧", "白榆", "扶苏", "江蓠", "若华", "昭禾",
    "砚秋", "疏桐", "归雁", "长庚", "汀兰", "墨岚", "溪亭", "风眠", "月栖", "烟汀",
]
CONTACT_ROLES = ["法定代表人", "店铺负责人", "资质专员", "运营负责人", "品牌授权经办人", "客服主管"]

BRANDS = [  # 虚构品牌名（自造词）
    "栖光", "砚白", "松序", "鹿屿", "清禾", "南枝", "星杳", "闻川", "月见", "青梧",
    "扶摇", "知夏", "白露", "栖迟", "江月", "竹隐", "鹤归", "溪午", "林深", "霜见",
    "山止", "川行", "云旗", "瑶光", "兰泽", "庭柯", "初篁", "晚舟", "烟渚", "听澜",
    "寒山", "暮雪", "碧落", "丹枫", "苍梧", "星野", "竹喧", "荷风", "松月", "云涛",
    "霜华", "溪桥", "烟岚", "木樨", "江蓠", "若华", "允舟", "思远", "凯风", "蓼萧",
    "湛露", "菁莪", "采薇", "桐月", "霜序", "拾光", "归堂", "半山", "屿风", "山茶",
    "川屿", "青岚", "石泉", "林樾", "归鸿", "白苹", "紫苏", "青葙", "白芷", "杜若",
    "辛夷", "蘅芜", "菖蒲", "茯苓", "远志", "连翘", "沉香", "苏木", "芸香", "泽兰",
    "佩兰", "藿香", "青黛", "紫草", "地黄", "玄参", "麦冬", "玉竹", "云岑", "霁月",
    "砚山", "听雪", "松风", "竹月", "溪云", "岚光", "青霭", "星澜", "澄川", "栖云",
    "闻溪", "知舟", "望川", "石屿", "霜禾", "白鹿", "云鹿", "月鹿", "松鹿", "栖鹿",
    "川上", "水木", "云间", "风禾", "南风", "青禾", "烟岚", "庭柯", "初篁", "晚照",
]
# 池内去重并从字号池补齐（原表有「庭柯/初篁/烟岚」重复，会让两条档案撞同一个品牌名）
BRANDS = list(dict.fromkeys(BRANDS))
BRANDS += [w for w in SIZI if w not in BRANDS][:20]

CAT_L1 = ["家用电器", "食品饮料", "美妆个护", "服饰鞋包", "数码3C", "母婴玩具",
          "家居日用", "运动户外", "生鲜", "医药保健", "图书文娱", "汽车用品"]
CAT_L2 = ["厨房小电", "休闲零食", "护肤", "女装", "手机配件", "婴幼儿食品",
          "收纳整理", "健身器材", "进口水果", "膳食补充剂", "教辅", "车载电器"]
CAT_L3 = ["电饭煲", "坚果炒货", "面膜", "连衣裙", "数据线", "配方奶粉",
          "收纳箱", "瑜伽垫", "冷冻莓果", "维生素片", "中小学教辅", "行车记录仪"]

APPLICATION_TYPES = ["新增品牌", "新增类目", "主体信息变更", "品牌授权续期",
                     "资质补交", "店铺类型变更", "跨境资质备案", "商标信息更新"]

CATEGORIES = [  # 与 pop_reject.json reject_rules / writeback_template 五域枚举对齐
    "品牌-授权", "品牌-商标", "品牌-全球购", "商品与行业资质", "入驻与主体店铺", "京麦驳回",
]
# 六个业务域 + 「未定位」也必须有样本（框架 2.2 的兜底路径）
ALL_CATEGORIES = CATEGORIES + ["未定位"]

REJECT_REASONS = {
    "品牌-授权": [
        "授权链路不完整，缺少品牌方到一级经销商的逐级授权文件",
        "授权书有效期已过，需提供在有效期内的品牌授权书",
        "授权书公章与品牌持有人名称不一致，需重新出具",
        "授权范围未覆盖申请类目，需补充对应类目的授权证明",
    ],
    "品牌-商标": [
        "商标注册证状态异常，需在国家商标网核验状态后重新提交",
        "申请品牌与店铺经营品牌持有人不一致，需先停用原品牌",
        "商标类别与申请类目不匹配，需提供对应类别的有效商标",
        "商标转让证明缺失，无法确认品牌权利人",
    ],
    "品牌-全球购": [
        "境外主体资质文件未附中文译本，需补充盖章译本",
        "跨境品牌授权链路缺少海外采购凭证",
        "海外商标注册证明未完成公证认证",
        "全球购店铺主体与品牌授权主体不一致",
    ],
    "商品与行业资质": [
        "食品经营许可证已过期，需补交有效期内证件",
        "化妆品备案凭证与申请商品批号不一致",
        "3C 认证证书型号与申请商品型号不匹配",
        "质检报告出具机构不在认可名录内，需重新送检",
    ],
    "入驻与主体店铺": [
        "营业执照经营范围未覆盖申请类目",
        "主体名称与店铺认证信息不一致，需先变更主体资料",
        "个体工商户升级为公司后未同步更新入驻资料",
        "店铺类型与申请的资质类型不匹配",
    ],
    "京麦驳回": [
        "该驳回项需在京麦后台完成品牌停用后重新新增，无需电话外呼",
        "京麦侧资料校验未通过，按站内指引补充后重新提交",
        "京麦驳回场景：需在商家后台上传补充材料",
        "京麦渠道驳回，电话外呼不适用，走站内消息触达",
    ],
    "未定位": [
        "申请单原因标签缺失，待专员人工核实归档",
        "驳回原因未归入现有五域，需人工分诊后答复",
    ],
}

MATERIALS = {
    "品牌-授权": ["品牌授权书", "逐级授权链路证明", "授权方营业执照"],
    "品牌-商标": ["商标注册证", "国家商标网状态截图", "商标转让证明"],
    "品牌-全球购": ["境外主体证明", "中文译本", "海外采购凭证", "公证认证文件"],
    "商品与行业资质": ["行业许可证", "质检报告", "产品备案凭证", "认证证书"],
    "入驻与主体店铺": ["营业执照", "主体变更证明", "店铺认证截图"],
    "京麦驳回": ["京麦后台操作回执", "补充材料扫描件"],
    "未定位": ["申请单原始截图", "专员核实记录"],
}

CALL_STATUSES = ["待外呼", "已接通", "拒绝", "无应答", "无效号码", "已关单", "无需外呼"]
CALL_RESULTS = ["connected", "no_answer", "refused", "invalid", "not_applicable"]
CLOSE_MODES = ["电话", "京麦"]
PLACEHOLDER_STYLES = ["空【】占位符", "具名【XX】占位符", "无占位符"]
APP_STATUSES = ["待补充材料", "审核中", "已驳回", "已撤回", "已失效"]

# 边缘标签（≥30 种），按域循环铺开
EDGE_CASES = [
    "typical", "tail_leading_zero", "tail_repeated_digits", "tail_sequential",
    "long_company_name", "rare_char_company", "homophone_name_like",
    "no_alternative_contact", "multi_brand", "cross_category", "repeated_reject",
    "withdrawn_application", "expired_record", "jingmai_no_call", "hotline_fallback",
    "blank_placeholder", "named_placeholder", "overlong_reason",
    "identity_multi_path", "region_hongkong_china", "region_macao_china",
    "region_taiwan_china", "individual_business", "branch_company",
    "future_valid_window", "callback_needed", "merchant_refused", "no_answer",
    "invalid_number", "unlocated_category", "global_purchase", "missing_brand",
    "partial_tail_report", "similar_shop_name", "past_valid_window",
    "multi_reject_round", "no_recheck_deadline", "long_business_scope",
]

RARE_CHARS = ["彧", "翀", "甯", "燚", "犇", "垚", "淼", "叕", "雒", "郗"]


def _pick(rng, pool, i):
    """按序号取词，池用完时循环但加序号后缀（生成阶段会做唯一性兜底）。"""
    return pool[i % len(pool)]


# 地区 → 6 位行政区划代码（GB/T 2260 省级码，公开规范值；仅作合成骨架，
# 不对应任何真实主体，全部数据为虚构）。
REGION_CODE = {
    "北京市": 110000, "上海市": 310000, "天津市": 120000, "重庆市": 500000,
    "河北省": 130000, "山西省": 140000, "辽宁省": 210000, "吉林省": 220000,
    "黑龙江省": 230000, "江苏省": 320000, "浙江省": 330000, "安徽省": 340000,
    "福建省": 350000, "江西省": 360000, "山东省": 370000, "河南省": 410000,
    "湖北省": 420000, "湖南省": 430000, "广东省": 440000, "海南省": 460000,
    "四川省": 510000, "贵州省": 520000, "云南省": 530000, "陕西省": 610000,
    "甘肃省": 620000, "青海省": 630000, "内蒙古自治区": 150000,
    "广西壮族自治区": 450000, "西藏自治区": 540000, "宁夏回族自治区": 640000,
    "新疆维吾尔自治区": 650000, "中国香港": 810000, "中国澳门": 820000,
    "中国台湾": 710000,
}

# 身份证号（GB 11643-1999）与统一社会信用代码（GB 32100-2015）的**结构合法**
# 合成：仅用于联调 / 演示，内容为虚构，不对应任何真实自然人或企业。
# 校验位严格按国标算，保证"看起来合法、查不出破绽"，但整串是合成值。
_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CHECK = "10X98765432"
_USCC_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"   # 31 位，剔除 I/O/Z/S/V
_USCC_WEIGHTS = [1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28]


def make_id_card(region_code, seq):
    """合成 18 位、校验位合法的虚构身份证号（结构合法，内容为虚构）。"""
    year = 1965 + (seq * 7) % 30          # 1965~1994 之间
    month = 1 + (seq * 3) % 12
    day = 1 + (seq * 5) % 28
    body17 = "%06d%04d%02d%02d%03d" % (region_code % 1000000, year, month, day, seq % 1000)
    s = sum(int(c) * w for c, w in zip(body17, _ID_WEIGHTS)) % 11
    return body17 + _ID_CHECK[s]


def make_credit_code(region_code, seq):
    """合成 18 位、校验位合法的虚构统一社会信用代码（结构合法，内容为虚构）。"""
    h = hashlib.md5(("CC-%d" % seq).encode("utf-8")).hexdigest()
    org7 = "".join(_USCC_CHARS[int(h[i], 16) % 31] for i in range(7))
    # 末两位用 seq 的 base-31 编码，保证 120 条全局唯一（31^2 = 961 > 120）
    b = seq
    org2 = _USCC_CHARS[b % 31] + _USCC_CHARS[(b // 31) % 31]
    body17 = "91" + ("%06d" % (region_code % 1000000)) + org7 + org2
    s = sum(_USCC_CHARS.index(c) * w for c, w in zip(body17, _USCC_WEIGHTS)) % 31
    return body17 + _USCC_CHARS[(31 - s) % 31]


def build_records():
    rng = random.Random(SEED)

    # 打乱后的独立词池副本，保证 120 条内不重复
    sizi = list(SIZI); rng.shuffle(sizi)
    brands = list(BRANDS); rng.shuffle(brands)
    names = ["%s%s" % (s, g) for s in SURNAMES for g in GIVEN_NAMES]
    rng.shuffle(names)
    tails = ["%04d" % n for n in range(10000)]
    rng.shuffle(tails)
    tails = tails[:N]
    # 强制塞入几种特殊尾号（前导零 / 连号 / 顺子），且不破坏唯一性
    specials = ["0086", "1111", "1234", "0007", "9876"]
    for k, sp in enumerate(specials):
        if sp in tails:
            tails.remove(sp)
        tails.insert(k, sp)
    tails = tails[:N]

    records = []
    base_date = date(2026, 9, 1)
    edge_i = 0

    # 六域 × 20 条，「未定位」再从每域借 2 条改写（保证七个分类都有样本）
    for idx in range(N):
        seq = idx + 1
        cat = CATEGORIES[idx % len(CATEGORIES)]
        if idx >= N - 7:  # 最后 7 条切成未定位 / 各域兜底混合
            cat = "未定位" if idx % 2 == 0 else CATEGORIES[idx % len(CATEGORIES)]

        # 每域前 6 条典型，其余铺边缘标签（每条约 2 个，保证覆盖全部标签）
        edges = []
        if idx < 6 * len(CATEGORIES):
            if idx % 3 == 0:
                edges = ["typical"]
            else:
                edges = [EDGE_CASES[(edge_i % (len(EDGE_CASES) - 1)) + 1]]
                edge_i += 1
        else:
            edges = [EDGE_CASES[(edge_i % (len(EDGE_CASES) - 1)) + 1]]
            edge_i += 1
            if idx % 2 == 0:
                edges.append(EDGE_CASES[(edge_i % (len(EDGE_CASES) - 1)) + 1])
                edge_i += 1
        edges = list(dict.fromkeys(edges)) or ["typical"]

        company_short = sizi[idx]
        region = REGIONS[idx % len(REGIONS)]
        ctype = COMPANY_TYPES[idx % len(COMPANY_TYPES)]
        if "个体" in ctype:
            # 个体工商户的真名是「区县/市 + 字号 + 行业 + 经营部」，不带序号。
            # 原来写成「…经营部（001）」，序号直接漏在名字里，预览卡上一眼是生成的。
            company_name = "%s%s百货经营部" % (region, company_short)
        elif "分公司" in ctype:
            company_name = "%s%s云启科技有限公司%s分公司" % (
                region, company_short, branch_city(region, seq))
        else:
            company_name = "%s%s商贸%s" % (
                region, company_short, LEGAL_SUFFIX.get(ctype, "有限公司"))
        # 边缘：超长公司名 / 生僻字。
        # 这两类要保留"边缘"属性（长、生僻），但名字本身得是**能念出来的真名**——
        # 原来靠后缀「第097分公司」「第097分部」制造差异，序号出现在字号里，
        # 既不像真名，也让"公司名"这一列看起来像编号表。
        if "long_company_name" in edges:
            company_name = "%s%s云启嘉禾瑞景通达商贸产业链服务有限公司%s分公司" % (
                region, company_short, branch_city(region, seq))
        if "rare_char_company" in edges:
            company_name = "%s%s%s商贸有限公司" % (
                region, RARE_CHARS[idx % len(RARE_CHARS)], company_short)

        shop_name = "%s%s旗舰店" % (company_short, "官方" if seq % 2 else "品牌")
        if "similar_shop_name" in edges:
            # 「相似店名」这个边缘场景要的是"两店名字很像"，不是"名字后面挂号码"。
            # （二店）既自然，又能和主店区分开。
            shop_name = "%s官方旗舰店（二店）" % company_short
        brand = brands[idx]
        if "multi_brand" in edges:
            brand = "%s|%s" % (brand, brands[(idx + 37) % len(brands)])
        if "missing_brand" in edges:
            brand = "（未申报品牌·%03d）" % seq

        contact_name = names[idx]
        if "homophone_name_like" in edges:
            contact_name = contact_name[:-1] + "宴"  # 同音近形，不与其它档案重名
        phone_tail = tails[idx]
        phone = "1990000" + phone_tail
        email = "contact%03d@example.test" % seq

        # 核验补充信息（C5 联调用的"公司信息后四位"等）：全部结构合法、内容为虚构。
        # region_code 决定身份证 / 信用代码的地区段；seq 保证全局唯一。
        region_code = REGION_CODE.get(region, 100000)
        id_card = make_id_card(region_code, seq)
        credit_code = make_credit_code(region_code, seq)
        bat = (seq * 131 + 17) % 10000
        if bat == int(phone_tail):           # 避免与联系电话尾号撞值，方便对照
            bat = (bat + 1) % 10000
        bank_account_tail = "%04d" % bat

        # 店铺信息（补全：此前只有店铺名，缺店铺编号/类型/状态/开店时间，
        # 更没有"店铺编号后四位"这类可用于核对的数字——商家报"店铺信息后四位"
        # 时根本没有对应的字段可核，这是此前遗漏的最主要一项）。
        shop_id = "SHOP%08d" % (20260100 + seq)      # 全局唯一（含 seq）
        shop_id_tail = shop_id[-4:]
        shop_type = SHOP_TYPES[idx % len(SHOP_TYPES)]
        shop_status = "整改中" if ("withdrawn_application" in edges) else "正常经营"
        shop_open_date = (date(2016, 1, 1) + timedelta(days=(idx * 53) % 2200)).isoformat()
        # 公司注册地址（公司信息补全；虚构地址，不含任何真实主体名）。
        # 注册地址：真实行政区 + 常见路名 + 虚构门牌（见 CITY_STREETS 上方说明）。
        registered_address = address_of(region, seq)

        submitted = base_date + timedelta(days=(idx * 3) % 60)
        reject_at = submitted + timedelta(days=1 + idx % 7)
        deadline = reject_at + timedelta(days=7 + (idx * 2) % 23)
        valid_from = reject_at - timedelta(days=3)
        valid_to = deadline + timedelta(days=30)
        if "expired_record" in edges or "past_valid_window" in edges:
            valid_to = date(2026, 10, 15)
        if "future_valid_window" in edges:
            valid_from = date(2026, 12, 1)
            valid_to = date(2027, 1, 31)
        if "no_recheck_deadline" in edges:
            deadline_s = "（未设定截止日·%03d）" % seq
        else:
            deadline_s = deadline.isoformat()

        app_status = APP_STATUSES[idx % len(APP_STATUSES)]
        if "withdrawn_application" in edges:
            app_status = "已撤回"
        if cat == "未定位":
            app_status = "审核中"
        call_status = CALL_STATUSES[idx % len(CALL_STATUSES)]
        if cat == "京麦驳回" or "jingmai_no_call" in edges:
            call_status = "无需外呼"
        # 自相矛盾的一处，2026-11-08 修：CALL_STATUSES 轮转会把「无需外呼」发给
        # close_mode=电话 的档案（例如 idx=6、13…），于是数据出现"关单方式是电话、
        # 通话状态却是不用打"——外呼名单按 call_status 取，这种记录永远轮不到，
        # 但看板上会显示成"电话单却不用打"，归因时会被当成系统漏呼。
        if (cat != "京麦驳回" and "jingmai_no_call" not in edges
                and call_status == "无需外呼"):
            call_status = "待外呼"
        if "merchant_refused" in edges:
            call_status = "拒绝"
        if "no_answer" in edges:
            call_status = "无应答"
        if "invalid_number" in edges:
            call_status = "无效号码"
        call_result = {
            "待外呼": "not_applicable", "已接通": "connected", "拒绝": "refused",
            "无应答": "no_answer", "无效号码": "invalid", "已关单": "connected",
            "无需外呼": "not_applicable",
        }[call_status]
        close_mode = "京麦" if (cat == "京麦驳回" or "jingmai_no_call" in edges) else "电话"
        understood = [True, False, "unknown"][idx % 3]
        if cat == "京麦驳回" or call_status in ("待外呼", "无需外呼", "无应答", "无效号码"):
            understood = "unknown"

        reject_code = "KB-POP-%03d" % (1 + (idx * 7) % 96)
        reason = REJECT_REASONS[cat][idx % len(REJECT_REASONS[cat])]
        if "overlong_reason" in edges:
            reason = reason + "；" + "补充说明：申请材料中多项附件与系统登记信息不一致，需逐项核对后重新上传，并确保所有扫描件清晰可辨、加盖公章完整。" * 2
        if "blank_placeholder" in edges:
            placeholder = "空【】占位符"
        elif "named_placeholder" in edges:
            placeholder = "具名【XX】占位符"
        else:
            placeholder = PLACEHOLDER_STYLES[idx % len(PLACEHOLDER_STYLES)]

        materials = list(MATERIALS[cat])
        if "cross_category" in edges:
            materials.append("跨类目补充说明")

        cat_l1 = CAT_L1[idx % len(CAT_L1)]
        cat_l2 = CAT_L2[idx % len(CAT_L2)]
        cat_l3 = CAT_L3[idx % len(CAT_L3)]
        if "cross_category" in edges:
            cat_l1 = "%s/%s" % (cat_l1, CAT_L1[(idx + 5) % len(CAT_L1)])
        if "global_purchase" in edges:
            cat_l1 = "跨境" + cat_l1

        business_scope = "%s、%s的批发零售及网上销售；品牌管理；供应链技术服务" % (cat_l2, cat_l3)
        if "long_business_scope" in edges:
            business_scope = business_scope + "；" + "；".join(
                ["%s相关技术咨询与售后服务" % x for x in CAT_L2[:8]])

        rec = {
            # ① 标识
            "record_no": seq,
            "application_id": "QZ20261106%04d" % seq,
            "merchant_id": "M20261106%04d" % seq,
            "fictional_marker": "SIM-2026-TEST-FICTIONAL",
            # ② 主体基本信息
            "company_name": company_name,
            "company_short_name": company_short,
            "shop_name": shop_name,
            "shop_id": shop_id,
            "shop_id_tail": shop_id_tail,
            "shop_type": shop_type,
            "shop_status": shop_status,
            "shop_open_date": shop_open_date,
            "company_type": ctype,
            "region": region,
            "registered_capital_wan": 10 + (idx * 37) % 990,
            "established_date": (date(2016, 1, 1) + timedelta(days=(idx * 41) % 2500)).isoformat(),
            "legal_representative": names[120 + idx],
            "business_scope": business_scope,
            # ②-附 公司级核验信息（"公司信息后四位"等）
            "credit_code": credit_code,
            "credit_code_tail": credit_code[-4:],
            "bank_account_tail": bank_account_tail,
            "registered_address": registered_address,
            # ③ 联系人（全部虚构）
            "contact_name": contact_name,
            "contact_role": CONTACT_ROLES[idx % len(CONTACT_ROLES)],
            "contact_phone": phone,
            "phone_tail": phone_tail,
            "contact_email": email,
            # ③-附 联系人级核验信息（身份证号等）
            "id_card": id_card,
            "id_card_tail": id_card[-4:],
            "alternative_contact": ("（未提供备用联系人·%03d）" % seq)
                                   if "no_alternative_contact" in edges
                                   else "%s（%s）" % (names[240 + idx], "资质助理"),
            # ④ 申请单
            "submitted_at": submitted.isoformat(),
            "application_type": APPLICATION_TYPES[idx % len(APPLICATION_TYPES)],
            "brand_name": brand,
            # 商标注册号是纯数字（真实号段就是 8 位数字）。原来写成 "TM9xxxxxxx"，
            # 前缀像自编的编号，商家一眼就能看出这不是商标号。
            "brand_registration_no": "%08d" % (30000000 + seq * 7919),
            "category_l1": cat_l1,
            "category_l2": cat_l2,
            "category_l3": cat_l3,
            "target_channel": "全球购" if "global_purchase" in edges else ("京麦" if close_mode == "京麦" else "POP"),
            "application_status": app_status,
            # ⑤ 驳回信息
            "reject_code": reject_code,
            "reject_category": cat,
            "reject_reason": reason,
            "reject_at": reject_at.isoformat(),
            "reject_round": 3 if ("repeated_reject" in edges or "multi_reject_round" in edges) else 1 + idx % 2,
            "required_materials": materials,
            "recheck_deadline": deadline_s,
            # ⑥ 核身入口（C5 四条路径）
            "verify_by_name": contact_name,
            "verify_by_tail": phone_tail,
            "verify_by_company": company_name,
            "verify_by_application": "QZ20261106%04d" % seq,
            "verify_preferred": ["姓名", "后四位", "公司名", "申请单号"][idx % 4],
            # ⑦ 外呼与关单
            "close_mode": close_mode,
            "call_status": call_status,
            "call_result": call_result,
            "merchant_understood": understood,
            "callback_needed": True if ("callback_needed" in edges or call_status in ("无应答", "拒绝")) else False,
            "hotline": HOTLINE,
            "notes": "虚构演示数据；边缘标签：%s" % "、".join(edges),
            # ⑧ 窗口与标记
            "data_valid_from": valid_from.isoformat(),
            "data_valid_to": valid_to.isoformat(),
            "placeholder_style": placeholder,
            "edge_cases": edges,
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
    records = build_records()
    fields = list(records[0].keys())

    with open(os.path.join(OUT_DIR, "applications.json"), "wb") as f:
        body = json.dumps({"_meta": {
            "title": "POP 商家资质审核驳回申请单档案（虚构模拟数据）",
            "generated_by": "scripts/gen_mock_applications.py",
            "seed": SEED,
            "count": len(records),
            "fictional_notice": "全部数据为虚构，不对应任何真实企业、自然人、电话号码或邮箱。",
        }, "items": records}, ensure_ascii=False, indent=2)
        f.write((body + "\n").replace("\r\n", "\n").encode("utf-8"))

    # CSV 带 UTF-8 BOM，Excel 双击不乱码；列表字段用 | 分隔
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    for r in records:
        w.writerow({k: _csv_value(r[k]) for k in fields})
    with open(os.path.join(OUT_DIR, "applications.csv"), "wb") as f:
        f.write(b"\xef\xbb\xbf" + buf.getvalue().replace("\r\n", "\n").encode("utf-8"))

    print("generated %d records, %d fields -> %s" % (len(records), len(fields), OUT_DIR))


if __name__ == "__main__":
    main()
