"""可学习策略层：把「遇到这种情况走哪条分支」从代码里挪到配置里。

为什么要单开这一层：
    自进化闭环能自动改的只有**配置**，不能改代码。话术替换改得了「怎么说」，
    改不了「说什么」——11-09 的实测里，重复最多的负面反馈都不是措辞问题：
        1. 客户说“信号不好，你再说一遍”，客服回“这个我这边确认不到”，
           连续四轮复读同一句（A01 第 4~7 轮）；
        2. 确认环节客户问“具体要补什么材料”，客服回“确认不到”，
           第二次直接把电话收了（A03/A06/A10/A14/A15）；
        3. 复述时把“授权链路不完整”降级成“材料问题”，具体原因丢了（A02 第 4 轮）。
    这三件事的共同点是**分支选错了**，不是哪句话不好听。把分支做成可配置的
    路由表，反馈闭环才有地方落笔；代码只提供一次通用查表，不写死任何一条路线。

默认值 = 加这一层之前的行为，逐字节不变：
    `evolution_policy` 缺省或全空时，dialogue 走原来那条路；本模块只做一次查表。
    这样 224 条 verify、88 条 run_eval、16 条对抗都不会因为“多了一层”而漂移，
    也让「改动到底带来了什么」可以归因到具体开关上。

可学习面（自进化只在这个集合里搜，不能往配置里随便塞键）：
    repair.<阶段>                   客户要求再说一遍时，是否用复述分支
    answer_routes.<阶段>.<问题类>   答不上来时，把这类问题路由到哪个话术键
    confirm_specific                复述时是否带具体原因与材料
"""

import config_loader

#: 要求重说/没听清的字面信号。只认这些，不做语义猜测：
#: 它们都是**修复请求**，正确动作是把上一段再说一遍，而不是“查不到”。
REPAIR_MARKS = (
    "再说一遍", "再说一次", "再讲一遍", "再讲一次", "重复一遍", "重复一下",
    "重说", "没听清", "听不清", "信号不好", "没太听清", "听不太清",
    # 「不是说了吗」曾经收进来过，评审时按实测撤回：
    # 这句话是**抱怨已经问过**，不是「请再说一遍」。判成修复请求后，客服会把
    # 整段驳回原因再念一遍，而客户这一轮往往是在给信息/暗示不想重复，状态机
    # 于是少走一轮确认计数，最后以 unknown 回写收线。16 通对抗画像实测：
    # 收进来是 14/16（A09/A14 误闭环），撤回后 16/16、负面反馈 6 条不变。
    # 另：句子以「我不是」开头时会命中 DENY_PERSON，判成「打错人」直接挂断，
    # 比回一句「确认不到」更糟。要支持这类说法，应当单独加「重复抱怨」分支，
    # 先接住再继续确认，不能用修复请求的复述话术顶替。
)

#: 问题分类的触发词。分类只用于**选路由**，不改变意图判定本身。
#: 顺序即优先级：费用/有效期这类"一句话就答得完"的先答；材料与原因次之；
#: 最后才是"你哪来的号码"这类信任问题——它常和"别是推销吧"绑在一句里，
#: 放在 fee 后面才不会把"这个收费吗，别是推销吧"误判成来源质疑。
_QUESTION_CLASSES = (
    ("fee", ("收费", "费用", "多少钱", "免费", "花钱", "要钱")),
    ("validity", ("多久", "多长时间", "什么时候", "有效期", "来得及")),
    ("material", ("补什么", "要补", "材料", "提供什么", "准备什么", "交什么")),
    ("reason", ("为什么", "为啥", "原因", "驳回", "没过", "没通过", "怎么回事")),
    ("provenance", ("哪来的", "哪来", "怎么有我", "怎么知道", "凭什么",
                    "怎么信", "你谁", "谁啊", "别是", "推销")),
)

#: 默认值：全空 = 未学习。新增键必须在这里登记，否则 current() 不认。
DEFAULTS = {
    "repair": {},
    "answer_routes": {},
    "confirm_specific": False,
    "greeting_company_alt": False,
}

#: 允许被自进化改写的开关名（白名单）。
LEARNABLE = ("repair", "answer_routes", "confirm_specific", "greeting_company_alt")

#: 路由只能指向 `reply_` 开头的话术键：不许指向 must_say、confirm_yes_words
#: 这类结构性字段——那等于让优化器改流程，不是改话术。
ROUTE_KEY_PREFIX = "reply_"


def current():
    """读出当前生效的策略（配置缺失时返回默认值）。

    每次调用都从 config_loader 取：场景切换 / 候选变体都靠换配置文件生效，
    策略不能自己缓存一份，否则子进程跑变体时会拿到上一份开关。
    """
    raw = (config_loader.load() or {}).get("evolution_policy") or {}
    out = {}
    for k, v in DEFAULTS.items():
        cur = raw.get(k, v)
        if isinstance(v, dict) and isinstance(cur, dict):
            out[k] = dict(cur)
        else:
            out[k] = bool(cur)
    return out


def enabled(name):
    """单个开关是否打开。未知名字一律 False（不抛异常，跑批不能因为配置少键挂掉）。"""
    return bool(current().get(name))


def repair_enabled(stage):
    return bool((current().get("repair") or {}).get(stage))


def is_repair_request(text):
    """客户是不是在要求“再说一遍”。

    注意不能只看“没听清”：CONFIRM 阶段“没明白”有自己的处理（先复述一次、
    第二次转专员），那条路已经修过；这里只接**带重说动作**的修复请求。
    """
    t = text or ""
    return any(w in t for w in REPAIR_MARKS)


def question_class(text):
    """把一句提问归到可路由的类别；归不了返回空串（不猜）。"""
    t = text or ""
    for name, marks in _QUESTION_CLASSES:
        if any(w in t for w in marks):
            return name
    return ""


def answer_route(stage, qclass):
    """查「这个阶段 + 这类问题」该用哪个话术键；没学过返回空串。"""
    if not stage or not qclass:
        return ""
    routes = current().get("answer_routes") or {}
    key = ((routes.get(stage) or {}).get(qclass)) or ""
    if not isinstance(key, str) or not key.startswith(ROUTE_KEY_PREFIX):
        return ""
    return key


def describe(raw=None):
    """人话描述策略，写进训练日志与接口返回。

    raw 用于描述**候选/训练后**的策略：此时它还没写进线上配置，
    不能走 current() 读盘，否则日志里写的永远是“全关”。
    """
    if raw is None:
        cur = current()
    else:
        cur = {}
        for k, v in DEFAULTS.items():
            got = raw.get(k, v)
            if isinstance(v, dict) and isinstance(got, dict):
                cur[k] = dict(got)
            else:
                cur[k] = bool(got)
    parts = []
    for st, on in sorted((cur.get("repair") or {}).items()):
        if on:
            parts.append("修复请求复述-%s" % st)
    for st, routes in sorted((cur.get("answer_routes") or {}).items()):
        for cls, key in sorted((routes or {}).items()):
            parts.append("%s/%s→%s" % (st, cls, key))
    if cur.get("confirm_specific"):
        parts.append("复述带具体原因与材料")
    if cur.get("greeting_company_alt"):
        parts.append("开场公司确认换问法")
    return "；".join(parts) or "全关（等于未学习前的行为）"
