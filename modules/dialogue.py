"""M3 对话编排 —— 规则版状态机（第一版真实现）。

职责：
    听懂用户说了什么（规则意图）→ 按状态机推进六个环节 → 缺什么问什么
    → 需要业务数据就查 → 拿到明确意图后触发回写。

这一版是什么：
    用关键词规则做意图判断，**不调用大模型**。它的价值是：
    1. 让整条链路有真实的业务语义，不再"每轮推进一格"；
    2. 给角色 2 一个能跑、能测的基线，之后把 _classify 换成模型输出即可；
    3. 让角色 3 有东西可评（30 条种子用例能跑出真实分数）。

这一版不是什么：
    不处理同义改写、不理解上下文指代、不支持用户跳步。
    下面每个处理器的 "_todo" 就是角色 2 的开工清单。

设计约定（与 docs/02_状态机定义.md 一致）：
    - 状态机管流程，话术层管措辞；
    - 身份未确认时，绝不透露订单、金额、地址等任何个人信息；
    - "明确拒绝"必须回写（业务侧要知道结果），"要求挂断"不回写；
    - 含糊回答不算同意，二次追问仍含糊则记 unknown 并回写。
"""

import difflib
import re

import config_loader
from modules import i18n, intent, kb, nlg, official_script, persona, policy, tools

# ---------------------------------------------------------------------------
# 意图关键词表
# 维护说明：这些词是"规则版"的全部智能所在。角色 2 接入模型后，
# 这张表可以退化为兜底（模型置信度低时再用规则）。
# ---------------------------------------------------------------------------

# 精确匹配的肯定回答（避免"不好意思"里的"好"被误判成同意）
#
# 注意这里**没有"嗯"**。以前"嗯"/"嗯嗯"在表里，CONFIRM 阶段用户"嗯"一声就被判成
# 明确同意并直接回写——docs/02 写的是"含糊回答不算同意"，这条规则和文档是反的。
# 单个语气词既可能是同意，也可能只是"我还在听"，证据强度不够，所以挪进 PURE_FILLER，
# 由置信度分级判为含糊，走二次追问（见下方的 classify）。
YES_EXACT = {"是", "对", "好", "行", "可以", "是的", "对的",
             "好的", "行吧", "是啊", "同意", "要", "需要", "没问题"}
# 包含即算肯定（长句里带这些词）
#
# 补这几个是 11-06 对抗跑批暴露的：AD02「免费的是吧，那就要，要要」与
# AD03「不要钱啊，那办上吧」都是**嘴上说要、词表里没有**的说法，
# 结果被判 unknown，一路问到"您可以简单回我一句同意或者不需要"。
# 加词比改规则安全：它们是明确表态，不是含糊。
YES_CONTAINS = ("同意", "没问题", "那就办", "办吧", "可以可以", "就这样", "要了", "需要",
                "那我要", "就要", "我同意",
                "那就要", "就要了", "要要", "办上吧", "还是办", "就办", "那我办", "要要要",
                # T14 意向翻转：「先应一声，再表态」是拒绝后改主意最自然的语序
                # （"那行，我要""好，我要"）。11-06 L04 九轮长对话里，用户第 2 轮
                # 明确拒绝、第 8 轮说"那行，我要"，系统却在 DONE 里复读结束语——
                # 不是改口逻辑没写（_stage_done 早有），是这句压根没被判成肯定。
                # 刻意**不放裸词「我要」**：E03「我要投诉你们」、自由表达「我要退款」
                # 都是诉求不是同意，加裸词会把它们误判成同意并提前闭环。
                # 只收带承接词或带动作补语的确定说法。
                "那行，我要", "行，我要", "好，我要", "那好，我要",
                "我要办", "给我办", "办一个", "我要这个", "那我办吧",
                # Q1-D5：CONFIRM 复述后最自然的答复是「我确认」「就这么办」，
                # 原来都不在词表，conf 0.45 被判 ambiguous，还要再追问一遍才回写。
                # 刻意不加裸词「确认」——「我要确认一下账单」是提问不是同意。
                "我确认", "确认了", "确认办", "就这么办", "就这么定", "就按这个办")

NO_EXACT = {"不", "不要", "不用", "不了", "算了", "拒绝", "没兴趣", "不需要", "别打"}
NO_CONTAINS = ("不要", "不用", "不需要", "没兴趣", "算了", "别打", "拒绝", "不办", "不了")

# 「没听懂」既不是拒绝，更不是同意。11-08 实跑 S10：用户说「哎呀，我还是没听明白」，
# confirm_yes_words 里的「明白」被子串命中，旧的否定检查只找连续的「没明白」，
# 中间隔一个「听」就漏了 → 直接回写 consent=True 结束通话，业务侧收到
# 「客户同意」这个错误结论。这类说法必须显式拦在肯定判定之前。
NOT_CLEAR_MARKS = ("没听明白", "没明白", "没听懂", "没懂", "听不明白", "听不懂",
                   "没听清", "没听清楚", "不清楚", "不太清楚", "不太明白", "不明白",
                   "还是不明白", "再说一遍", "再讲一遍", "没太听懂", "没太懂",
                   "搞不懂", "没太明白")

# 否认本人 / 打错电话
DENY_PERSON = ("不是本人", "我不是", "打错", "他不在", "不是他", "你找错了")

# 要求结束通话（与"拒绝"不同：拒绝要回写，挂断不回写）
HANGUP_WORDS = ("挂了", "先挂", "忙着", "没空", "不聊了", "别打了", "不要再打")

# 情绪信号（骂/威胁这类**强情绪**）
ANGRY_WORDS = ("投诉", "骗子", "烦", "垃圾", "滚", "气死", "恶心", "骚扰")

#: 判情绪之前先扣掉的"礼貌说法"和"商品词"。
#:
#: 11-13 实测：客户一句「麻烦把材料清单发到我商家后台」，客服回
#: 「非常抱歉让您有这样的感受」——因为「烦」在 ANGRY_WORDS 里，而「麻烦」
#: 含「烦」。同一类误伤还有："我卖滚筒洗衣机的"（滚）、"垃圾袋"（垃圾），
#: 这两个词在这个赛题的类目里都是正经商品名。
#:
#: 做法是匹配情绪词之前先把这些片段扣掉，而不是删词表里的字：真骂人的
#: 「真麻烦」「别烦我」「滚筒滚出去」扣完还剩「真麻烦」「别我」「出去」，
#: 「烦」照样命中。宁可多一道归一，也不能让客户说句客气话就被道歉。
_POLITE_PREFIXES = ("麻烦您", "麻烦你", "麻烦帮", "麻烦把", "麻烦给", "麻烦发",
                    "麻烦再", "麻烦看", "麻烦说", "麻烦问", "麻烦转",
                    "麻烦确认", "麻烦提供", "麻烦告诉", "麻烦核对")
_ANGRY_FALSE_FRIENDS = ("滚筒", "垃圾袋", "垃圾桶", "垃圾处理器", "垃圾食品")


def _anger_text(text):
    """扣掉礼貌用语与商品词之后再看情绪。"""
    s = text or ""
    for w in _POLITE_PREFIXES + _ANGRY_FALSE_FRIENDS:
        s = s.replace(w, "")
    return s


# 抱怨/泄气信号（T11）。与 ANGRY_WORDS 刻意的区别：那些是"骂人"，
# 这些是"我说了也没用"——不是攻击客服，是**放弃沟通**。
# 不接住这句话的后果比挨骂更严重：用户下一句往往就是"算了，挂了吧"。
# 实测"那你还要我说啥"以前直接换来一句"您同意领取吗"，用户会觉得自己在自说自话。
COMPLAIN_WORDS = ("说啥", "让我说", "有啥用", "有什么用", "说了有用", "说了也没用",
                  "还要我说", "说了白说", "问这有啥", "白问了")

# T17-② 反问式不满：「干什么？」「你需要干什么？」「搞什么」「干嘛」。
# 11-06 真实通话 s_0af0a5f16695 里用户连问两句这种话，旧逻辑因为含"什么"
# 判成 ask 走知识库/继续追问，回复「没事，方便说下您的姓名吗」——火气零承接，
# 还用了「没事」这种敷衍开头，用户第二次问完就直接不给信息了。
# 它不是提问（用户并不想知道客服在做什么），是"你到底要干嘛"的不耐烦。
# 但「这个功能干什么用的」「你干什么工作」是真提问，所以下面按短句 +
# 排除词收窄，不放裸词进 COMPLAIN_WORDS。
RHETORICAL_DISPLEASURE = ("干什么", "干嘛", "搞什么", "整什么", "要干嘛", "想干嘛")
_RHETORICAL_EXCLUDE = ("用的", "工作", "怎么", "为什么", "啥用", "什么意思",
                       "这个", "那个", "按钮", "功能")


def _is_rhetorical_displeasure(text):
    core = _norm(text)
    if not core or len(core) > 10:
        return False
    if any(x in core for x in _RHETORICAL_EXCLUDE):
        return False
    return any(w in core for w in RHETORICAL_DISPLEASURE)


# Q1-D2：用户拒绝「提供身份信息」的几种说法，必须与「拒绝这份权益」分开：
#   "我凭什么告诉你"        → 拒绝核身 → 解释为什么核对 + 给替代路径
#   "要钱的话我就不要了"    → 不是拒绝核身，是在问收费 → 答 KB 后继续核对
# 只认明确的"不给信息"表达，不把裸「不要 / 不办」算进来——那两个在核身阶段
# 多半是在问"收不收费"，直接挂断就是 11-06 之前「要钱不？要钱我可不要」
# 被 refused_identity 一刀切掉的老毛病。
_REFUSE_IDENTITY = ("不报", "不说", "不告诉", "不想说", "不方便说",
                    "凭什么", "不提供", "别问", "不用核", "不核", "不验")


def _refuse_identity(text):
    core = _norm(text)
    return bool(core) and any(w in core for w in _REFUSE_IDENTITY)


# 「记不住」不是「听不清」，也不是「不愿给」——是第三种情况：人还在，凭据拿不出来。
# 11-13 自进化从 L2 基线里翻出 S05 第 6 轮：客户说「号码是店里注册的，我一时真
# 想不起来」，客服回「可能信号不太好，我先不打扰您了」并收线。前四轮客服都在给
# 替代路径（问姓名/单号/公司名），说得挺好；可 `noise_turns` 在后台一直累加，
# 到第 5 次就抢在替代路径前面挂断——两条判据打架，输的是能救回来的那通电话。
#
# 判据故意宽：这一条只用来**不累计噪声**，最坏结果也只是多问一次替代路径，
# 不会挂断。所以「忘了」「记不清」「想不起」这类一律算内容。
_CANT_REMEMBER = ("记不住", "记不得", "记不清", "想不起", "不记得", "没记住",
                  "忘了", "忘掉", "忘记了", "想不起来了")


def _cant_remember(text):
    core = _norm(text)
    return bool(core) and any(w in core for w in _CANT_REMEMBER)


# 「我不**会**报」跟「我不报」是同一件事，词表只收后者就会漏。
# 故意**不并进 `_REFUSE_IDENTITY`**：那个词表驱动"两次拒绝就收尾"，
# 误判的代价是提前挂断；这里只驱动"别把人说的话当链路噪声"，
# 误判的代价是多问一次替代路径。两处风险不对称，就不该共用一张表。
_DECLINES_INFO = ("不会报", "不给", "不能给", "不想给", "不愿意给",
                  "没法提供", "没办法提供", "不方便提供")


def _declines_info(text):
    core = _norm(text)
    return bool(core) and any(w in core for w in _DECLINES_INFO)

# "不要钱 / 不收钱"里的"不要"修饰的是"钱"，不是"这个服务"（T10 否定作用域）。
# 不区分的话，"不要钱啊，那办上吧"会被判成拒绝——把一个想办的客户记成"客户拒绝"，
# 业务侧拿着这张工单去做挽留，用户接到第二个电话会更火。
FREE_MARKS = ("不要钱", "不用钱", "不收钱", "不要费用", "不收费", "不要花钱", "不用花钱")

# 改口信号（T9）。只用在阶段**切换边界**：用户已经听过方案，又回头改诉求。
# "等等我改一下其实是屏幕碎了"以前被判 unknown，诉求仍是旧的，客服继续按旧诉求推销。
# 注意这里不含单独一个"不是"——它太常见（"不是本人""不是这个意思"），
# 一律当改口会把否认、拒绝全搅成改口。
CORRECTION_MARKS = ("改一下", "改口", "说错了", "我说错", "其实是", "更正",
                    "重新说", "等等我", "我说的是")

# 提问信号
# "到哪 / 哪了"是物流查询的高频问法（"这单到哪了"），不含它们就会被当成
# 一条诉求记下来并推进到方案沟通——用户明明在问，客服却在推销。
ASK_MARKS = ("吗", "？", "?", "多少", "多久", "怎么", "什么", "能不能", "是不是", "可以吗",
             "到哪", "哪了",
             # "在哪里改""去哪儿补"是赛题里最高频的一类追问（操作路径类），
             # 不含它们，一句真问题会被当成新诉求记进工单。
             "哪里", "在哪", "哪儿",
             # 11-08 接官方 QA 时补的两个疑问词：官方 27 条问法里有
             # "在职证明有哪些""如何上传联系方式"，这两个词都不在上面的表里，
             # 于是商家问出官方原话时**反倒判不成提问**，QA 永远接不上。
             # 只收这两个明确的疑问词，不收"有什么"——"有什么需要我配合"是问句，
             # 但"我有什么材料"常是陈述自己手上的东西，含糊的别往里加。
             "哪些", "如何")

# 要求转人工。**故意不含"投诉"**：投诉是情绪信号（见 ANGRY_WORDS），
# 要致歉承接后继续解决问题，不能一句"帮您转人工"就把电话挂掉。
# 评测用例 R04 就是"我要投诉你们"之后还要走完五个环节，含投诉会直接判负。
HANDOFF_WORDS = ("转人工", "找人工", "人工客服", "转客服", "转接人工",
                 "要真人", "真人接", "叫你们领导", "找你们领导",
                 # 口语说法：客户不会说"转人工"，说的是"转个人""找管事的"。
                 # 漏掉这两种，一句明确的转人工请求会被判成提问（含"能不能"），
                 # 于是客服继续按原流程问下去——这是最不该出错的漏判。
                 "转个人", "找你们负责", "找负责人", "叫负责人",
                 "叫你们领导", "上级")
# 注意：这里不能放裸词"负责人"——pop 场景开场问"您是贵公司的负责人吗"，
# 用户正常回答"是的，我是负责人"会 substring 命中被当成转人工直接挂机
# （11-06 17:18 pop 场景实测第一轮即中招）。转人工请求必须带动作词
# （找/叫/转/要），"负责人"单独出现是身份陈述不是请求。

# 弱肯定：嘴上答应了，但明显没拿定主意。
# "好吧""应该可以""差不多"这类回答如果直接当同意回写，业务侧会拿着一份
# 用户并不认账的工单去做后续动作，退订/投诉就从这儿来。所以给它们低于阈值的
# 置信度，在 CONFIRM 阶段触发一次追问，而不是直接落库。
WEAK_YES = ("好吧", "应该可以", "应该行", "应该吧", "差不多", "大概", "可能吧", "看看吧")

# ---------------------------------------------------------------------------
# 含糊输入的词表（T1：意图置信度分级）
#
# 为什么需要这一层：关键词规则只有"命中/没命中"两态，而真实通话里大量输入是
# 半句话——"你说是就是吧""嗯……""随便"。旧规则对这两种输入的处理都是错的，
# 但错法不同：
#     命中一个"嗯"  → 被当成同意，回写一个用户从没给过的结论（错在证据太弱）
#     一条都没命中  → 被当成诉求，直接推方案（错在用户会觉得客服在自说自话）
# 所以这里把"证据强度"显式化成 0~1 的置信度，再由各阶段决定低分要不要追问。
# ---------------------------------------------------------------------------

# 纯语气词：整句只有这些的时候，唯一的语义信息是"我还在听"，不是"我同意"。
PURE_FILLER = {"嗯", "嗯嗯", "啊", "哦", "喔", "呃", "额", "唉", "诶", "嘿", "哈"}

# 敷衍/含糊：说了等于没说。用**包含**匹配而不是整句匹配，
# 因为真实说法是"我也不太清楚""这个我还真不知道"，整句永远对不上。
VAGUE_MARKS = ("不太清楚", "不清楚", "不知道", "不晓得", "不记得", "说不好",
               "说不准", "不确定", "看情况", "考虑一下", "再想想", "让我想想",
               "你看着办", "你说了算", "你说是就是", "你定吧", "随便",
               "再说吧", "到时候再说", "无所谓", "都行", "都可以",
               # 连说两遍的"行吧行吧""行了行了"是不耐烦，不是同意。
               # 注意不能把单个"行吧"加进来——它在 YES_EXACT 里，是明确同意；
               # 也不能把"行吧"加进 YES_CONTAINS，否则"不行吧"会被判成同意。
               "行吧行吧", "行了行了", "知道了", "好知道了",
               # "没明白"家族：是"没听懂"不是确认。pop 场景确认词里有"明白"，
               # 不挡住它们的话，"我没明白"会被场景确认词误判成已清楚。
               "不明白", "没明白", "不太明白", "没听明白")

# 证据强度（0~1）。数字不是拍脑袋来的，是按"这句话能不能单独支撑一次回写"定的：
#   EXACT     整句就是一个标准答案，"同意"两个字不会再有别的解释
#   DIGIT     报出 4 位尾号，是强身份证据（但可能是别人的号，所以不给满分）
#   CONTAINS  长句里顺带带了标准答案，"行吧那你办吧"里"办吧"也可能是抱怨
#   WEAK      弱肯定，"好吧"经常等于"我不想争了"
#   VAGUE     命中含糊词表
#   NONE      一条规则都没命中：可能是自由表达（"我要退款"），也可能是没听清
CONF_EXACT = 0.95
CONF_DIGIT = 0.93
CONF_CONTAINS = 0.75
CONF_WEAK = 0.50
CONF_VAGUE = 0.25
CONF_NONE = 0.45

# 低于这条线的不算"明确同意"。只用在 CONFIRM——别处用它会误伤自由表达：
# "我要退款"一条规则都不命中（conf 0.45），但它显然该继续推进，不该被追问。
YES_MIN_CONF = 0.60

# 同一个阶段连续追问几次就换人。对应 T2"同一槽位连续追问 ≤2 次，第 3 次转兜底"。
CLARIFY_MAX = 2

# ---------------------------------------------------------------------------
# 兜底话术：按"为什么会兜底"分开
#
# 原来只有一句"抱歉，我这边刚才没处理好，麻烦您再说一遍好吗？"糊住所有情况，
# 结果是三种完全不同的失败对用户说同一句话：
#     识别没听清   —— 该请对方再说一遍
#     接口超时     —— 再说一遍也没用，该承诺回电
#     业务拒绝     —— 更不能让他重复，该换人处理
# 用户重复三遍仍听到"麻烦您再说一遍"，体验比直接挂断还差。
#
# 结构：reason -> (配置键, 内置默认话术)
#     配置键先查 `fallback` 段；`writeback` 额外兼容旧的 `stages.WRITEBACK` 段。
#     内置默认是为了配置没补齐时也能跑（4 号补配置不用改代码）。
# ---------------------------------------------------------------------------
FALLBACK_SCENES = {
    # 键名必须与 config/scene.json 里 fallback 段的**实际键名**逐字一致。
    # 这里踩过一个坑：原本写成 reply_asr / reply_timeout / reply_business / reply_fail，
    # 而配置里叫 reply_asr_fail / reply_tool_timeout / reply_business_reject，
    # 结果只有 tool 和 error 两条真正读了配置，其余四条一直用代码里的兜底句——
    # 改了配置话术一个字都不变，测试却全绿（因为"不同原因不同话术"是代码里就不同的）。
    # 新增原因时，先往三个场景包的 fallback 段加键，再来这里登记。
    "asr": ("reply_asr_fail",
            "不好意思，这边没听清，您再说一遍好吗？"),
    "error": ("reply",
              "抱歉，我这边刚才没处理好，麻烦您再说一遍好吗？"),
    "timeout": ("reply_tool_timeout",
                "我这边查询有点慢，稍后让专员给您回电说明，可以吗？"),
    "business": ("reply_business_reject",
                 "这个情况我这边处理不了，稍后让专员跟您说明，可以吗？"),
    "tool": ("reply_tool",
             "我这边查得有点慢，稍后让专员给您回电说明，可以吗？"),
    "writeback": ("reply_writeback",
                  "我这边登记时出了点问题，稍后会有专员联系您确认。感谢您的接听，再见。"),
}
_KEY_TO_REASON = dict((v[0], k) for k, v in FALLBACK_SCENES.items())


def new_session(session_id):
    """创建一次通话的内存状态。

    比骨架版多了几个计数器：身份核验尝试次数、确认问了几次、连续空输入次数。
    这些不是业务字段，但状态机靠它们决定"什么时候该放弃"。

    返回：dict
    """
    return {
        "session_id": session_id,
        "stage": "GREETING",
        "turn": 0,
        "slots": {
            "identity_confirmed": False,
            "need": "",
            "consent": "",      # true / false / unknown，未确认为空
            "customer": None,
            "order": None,
        },
        "closed": False,
        # 本通电话的外呼目标（外呼名单里那条申请单）：开场白要靠它报公司名。
        "outbound_target": None,
        # 商家是否已经确认"我就是这家公司"。赛题四段式：② 确认身份在③说明原因之前，
        # 没确认就把"你被驳回了"说出来，商家凭什么信这通电话。
        "company_confirmed": False,
        "identity_attempts": 0,
        "confirm_asks": 0,
        "empty_count": 0,
        # 因为"没听懂"追问了几次。到 CLARIFY_MAX 就转人工，不能让用户
        # 对着一句听不懂的话被反复问同一句（E04 那条用例骂的就是这个）。
        "clarify_asks": 0,
        # 身份核验拆成两个槽位分别追：尾号（唯一性高）和姓名。
        # 为什么要拆开：以前只记"问了几次"，问的是什么都丢了，
        # 于是永远是同一句"方便说一下您手机号后四位吗"——用户说了姓名
        # 也当没听见，只能靠再来一次。
        "identity_slots": {"tail": "", "name": ""},
        "slot_asks": {"tail": 0, "name": 0},
        "last_asked_slot": "",
        # 连续含糊次数（T12）。核验环节用户连说"嗯/哦"时用来决定什么时候收尾。
        "identity_vague": 0,
        # 带情绪的次数（T8）。只用于统计与话术轮换，不做转人工——
        # 见 ANGRY_HOLD 上关于"骂两句就转人工等于把电话关掉"的说明。
        "angry_holds": 0,
        # 通内指代（T6）：这通电话里提过的对象，键是类型、值是给人看的称呼。
        # 只认同一通电话内的指代，跨通话的"上次那个"不解析（那需要客户档案，
        # 查不到就硬答等于编造）。
        "referents": {},
        "last_referent": "",
        # 每个槽位被提交过的值（按顺序）。用来判"这是不是改口"——
        # 当前值查不到就会被清空，拿清空后的值比较会把中间那次改口弄丢。
        "slot_history": {"tail": [], "name": []},
        # 改口轨迹：[{slot, from, to, turn}]，同时会进 slots 与回写 payload
        "corrections": [],
        # 长会话记忆：跨环节事实 + 最近几轮原文（见 _remember / memory_brief）
        "memory": {"recent": [], "facts": {}},
        # 这一轮用户话的识别置信度。由 respond 写入，_reply 读取后落进事件。
        "last_intent_conf": 0.0,
        # 第几版回写。用户改口时递增，配合幂等键让"同一张工单被更新"而不是新开一张。
        "writeback_version": 0,
        # 官方 QA 播报记录（11-08）：播过哪几条、播了几条、哪几条命中了但没播。
        # 前两个用来去重与限额（逐字重念同一句像坏掉的机器人），
        # answer_blocked 只用于审计——"认出了这个问题，但答案里还有没填的占位"
        # （官方 5 条带 XX、kb_pop 96 条带【】），所以没念。
        "qa_answered": [],
        "qa_count": 0,
        "answer_blocked": [],
        # 已经逐字播过的答案原文。跨来源去重用：官方 QA 与 kb_pop 里有几条
        # 是同一句话（4 号照原文抄进 kb），只按 id 去重会漏掉这种情况。
        "spoken_answers": [],
    }


# 中文数字 → 阿拉伯数字，只用于"报尾号"这一种场景（见 classify 里的说明）。
_CN_DIGIT_CHARS = {"零": "0", "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
                   "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def classify(text):
    """把一句话拆成若干意图标记 + 一个置信度（规则版）。

    返回 dict：
        deny   否认本人 / 打错电话
        hangup 要求挂断
        angry  带情绪
        no     明确拒绝
        yes    明确肯定
        ask    在提问
        handoff 要求转人工（不含"投诉"——投诉要承接后继续处理）
        digits 文本里的 4 位数字（手机尾号）
        vague  这一句是含糊/敷衍（说了等于没说）
        conf   这次识别的置信度 0~1，写进事件供统计与回归
        evidence  置信度来自哪条证据（exact/contains/digit/weak/vague/none）

    _todo 角色 2 接入大模型后，模型输出 intent + 置信度，本函数退化为
    低置信度时的兜底——这也是为什么它先把 conf 这一个数字先说清，
    接模型时只需要换"从哪来"，不用改"怎么用"。
    """
    t = (text or "").strip()
    n = _norm(t)
    flags = {"deny": False, "hangup": False, "angry": False, "complain": False,
             "no": False, "yes": False, "ask": False, "handoff": False,
             "digits": "", "vague": False, "conf": CONF_NONE, "evidence": "none"}

    # 命中的强证据先都收起来，最后取最强的那个当置信度。
    # 为什么不"先命中先返回"：一句话经常同时有多种信号
    # （"你们天天打电话烦不烦"= 带情绪 + 在质问），
    # 主意图由 _intent_name 按固定优先级取，置信度取最强证据，
    # 两件事分开算，统计口径才稳定。
    strong = []

    if any(w in t for w in DENY_PERSON):
        flags["deny"] = True
        strong.append(("deny", CONF_EXACT))
    if any(w in t for w in HANGUP_WORDS):
        flags["hangup"] = True
        strong.append(("hangup", CONF_CONTAINS))
    # 情绪词 = 通用骂人词 + 场景包 compliance.categories.insult。
    # pop 场景的"爱审不审"这类行业特有攻击话不在通用表里，漏掉的话
    # 用户骂一句"爱审不审"会被当成正常推进——输入侧情绪分类和输出侧
    # 合规检查必须用同一份场景词表，不能各管各的。
    _insult_extra = tuple(w for w in (config_loader.compliance_categories()
                                      .get("insult") or [])
                          if w and w not in ANGRY_WORDS)
    _anger_src = _anger_text(t)
    if any(w in _anger_src for w in ANGRY_WORDS + _insult_extra):
        flags["angry"] = True
        strong.append(("angry", CONF_CONTAINS))
    # 抱怨单独一个标记，不并进 angry：angry 的处理是"暂停推进+致歉"，
    # complain 的处理是"致歉后照常往下走"——用户只是在泄气，不是要中断这通电话。
    # 反问式不满并入 complain（不新造标记）：它的处理同样是"先接住情绪"，
    # 区别在于 IDENTITY 阶段要改用「说明来意」的话术，见 _stage_identity。
    if any(w in t for w in COMPLAIN_WORDS) or _is_rhetorical_displeasure(t):
        flags["complain"] = True
        strong.append(("complain", CONF_CONTAINS))

    # 否定作用域（T10）：先说"是不是不要钱"这类**免费语境**，"不要"修饰的是钱。
    free_ctx = any(w in t for w in FREE_MARKS)
    no_spans = []
    no_pos = -1
    if n in NO_EXACT and not free_ctx:
        flags["no"] = True
        no_pos = max(t.find(n), 0)
        no_spans.append((no_pos, no_pos + len(n)))
        strong.append(("no", CONF_EXACT))
    elif not free_ctx:
        # 每个否定词的**每一次出现**都要记区间，不能只记第一次：
        # "不要了不要了"里第二个"要"落在第二段"不要"里，
        # 只记第一段会让它漏出来，把一句重复拒绝判成同意。
        for w in NO_CONTAINS:
            start = 0
            while True:
                i = t.find(w, start)
                if i < 0:
                    break
                no_spans.append((i, i + len(w)))
                no_pos = max(no_pos, i)
                start = i + 1
        if no_pos >= 0:
            flags["no"] = True
            strong.append(("no", CONF_CONTAINS))

    # 关键：命中否定词时不再是"直接不判肯定"，而是**只认否定词外面的肯定词**。
    #
    # 这一条以前写成 `if not flags["no"]:`，代价是把两种完全不同的句子并成了一类：
    #   "不要了"        —— "要"在"不要"里面，是拒绝（必须继续判成拒绝）
    #   "算了还是办吧"    —— "办吧"在"算了"后面，是改口（被判成了拒绝）
    # 判据是**位置**：肯定词落在否定词的字符区间内，才属于被否定。
    yes_pos = -1
    bare = _strip_filler(n)
    if n in YES_EXACT:
        yes_pos = max(t.find(n), 0)
        flags["yes"] = True
        strong.append(("yes", CONF_EXACT))
    # "好的嗯""对呀嗯"这类：语气词只是语气，去掉之后就是一句标准答案。
    # 以前这种整句在 YES_EXACT 里查不到、在 WEAK_YES 里也查不到，
    # 于是被判成 unknown —— 用户明明答应了，客服却问"您同意还是不需要"。
    # 置信度给 CONTAINS（不是 EXACT）：语气词的存在说明这句话不算干脆。
    elif bare and bare != n and bare in YES_EXACT:
        yes_pos = max(t.find(bare), 0)
        flags["yes"] = True
        strong.append(("yes", CONF_CONTAINS))
    elif _find_free(t, WEAK_YES, no_spans) >= 0:
        yes_pos = _find_free(t, WEAK_YES, no_spans)
        flags["yes"] = True
        strong.append(("yes", CONF_WEAK))
    elif _find_free(t, YES_CONTAINS, no_spans) >= 0:
        yes_pos = _find_free(t, YES_CONTAINS, no_spans)
        flags["yes"] = True
        strong.append(("yes", CONF_CONTAINS))

    # 同一句里既否定又肯定：**后说的那个赢**。
    # 依据是改口的自然语序——"算了还是办吧""不要了…那还是办吧"，
    # 更正永远在后面。以前两条都置 True，再由 _intent_name 的固定优先级
    # 判成"拒绝"，于是 AD07（先拒后要）最后落库的是拒绝，与用户最后一次表态相反。
    if flags["no"] and flags["yes"]:
        if yes_pos > no_pos:
            flags["no"] = False
            strong = [e for e in strong if e[0] != "no"]
        else:
            flags["yes"] = False
            strong = [e for e in strong if e[0] != "yes"]

    # 提问判定。这里有一个刻意的收窄：**用户已经给了明确决定时，光一个问号不算提问**。
    # 反例是 AD03「啊？不要钱啊，那办上吧」——开头的"啊？"是惊讶不是提问，
    # 以前它让整句判成 ask，于是客服去念订单金额，用户刚说"办上吧"又被晾在一边。
    _ask_hits = [m for m in ASK_MARKS if m in t]
    if _ask_hits:
        _punct_only = all(m in ("？", "?") for m in _ask_hits)
        if not (_punct_only and (flags["yes"] or flags["no"])):
            flags["ask"] = True
            strong.append(("ask", CONF_CONTAINS))
    # 同理："我不需要转人工"里的"转人工"是拒绝，不是要求
    if not flags["no"] and any(w in t for w in HANDOFF_WORDS):
        flags["handoff"] = True
        strong.append(("handoff", CONF_EXACT))

# 手机尾号：取**最后一个**孤立的 4 位数字段。
#    # 为什么是最后一个：改口几乎总是"不对，是XXXX"这个语序，先说的那个是错的。
    # 旧代码取第一个，于是"尾号1234，不对是4321"会把错的 1234 记下来，
    # C04 那条用例骂的就是这个。
    #
    # 为什么要"孤立"（前后不是数字）：8 位手机号 "12345678" 里也含 4 位数字段，
    # 不判边界的话会截出 "5678" 当成尾号去核验，属于凭空造数据。
    digits = ""
    for i in range(len(t) - 3):
        seg = t[i:i + 4]
        if not seg.isdigit():
            continue
        prev_ok = (i == 0) or not t[i - 1].isdigit()
        next_ok = (i + 4 >= len(t)) or not t[i + 4].isdigit()
        if prev_ok and next_ok:
            digits = seg
    # 中文数字报尾号："尾号是八八六六"。外呼里这比阿拉伯数字更常见，
    # 旧规则一个都认不出来，于是尾号收不进槽位、被反复追问。
    # 只在"正好连续 4 个中文数字"时成立：8 位连报（一二三四五六七八）不当尾号，
    # 那是完整号码，截后四位就是拿别人的号去核验。
    if not digits:
        run = ""
        for ch in t:
            if ch in _CN_DIGIT_CHARS:
                run += _CN_DIGIT_CHARS[ch]
            else:
                if len(run) == 4:
                    break
                run = ""
        if len(run) == 4:
            digits = run
    flags["digits"] = digits
    if digits:
        strong.append(("info", CONF_DIGIT))

    # 含糊判定：只有在**没有任何强证据**时才算含糊。
    # 为什么加这个前提——"我不知道这个保修多久"里带"不知道"，但它同时是提问
    # （"多久"）。按含糊处理会把一个真问题追问掉，用户会以为客服在打太极。
    # 反过来，"你说是就是吧"一条强规则都不命中，才是真正需要追问的那一类。
    # ASR 噪声单独归入：咳嗽/背景音被 qwen3-asr-flash 识别成 "Cough." 这类
    # 纯英文（没有任何中文或数字）的输入，不是用户在表达态度，是"没说出人话"。
    # 不挡住的话它会落到核身失败计数里——真实通话 11-06 里一声咳嗽就把人挂断。
    if t and not re.search(r"[\u4e00-\u9fff0-9]", t):
        flags["vague"] = True
        flags["conf"] = CONF_VAGUE
        flags["evidence"] = "noise"
    elif n in PURE_FILLER or (not strong and any(w in t for w in VAGUE_MARKS)):
        flags["vague"] = True
        flags["conf"] = CONF_VAGUE
        flags["evidence"] = "filler" if n in PURE_FILLER else "vague"
    elif strong:
        flags["conf"] = max(c for _, c in strong)
        flags["evidence"] = max(strong, key=lambda kv: kv[1])[0]
    else:
        flags["conf"] = CONF_NONE
        flags["evidence"] = "none"

    return flags


def _norm(text):
    """去掉首尾空白与标点，只用于"整句是否等于某个标准答案"这类比对。

    "嗯……" 和 "嗯" 必须判成同一件事，否则用户拖个长音就从"含糊"变成"没命中"。
    """
    return (text or "").strip().strip(" \t\r\n。，、！？!?,.~～…—-；;：:")


def _find_free(text, words, spans):
    """在 words 里找第一个**不在否定区间内**的出现位置，找不到返回 -1。

    spans 是 [(起, 止)] 的字符区间，来自这一句里命中的否定词。
    用途就一个：区分"不要了"（要埋在"不要"里）和"算了还是办吧"（办吧在后面）。
    """
    best = -1
    for w in words:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            if not any(a < i + len(w) and i < b for a, b in spans):
                best = max(best, i)
                break
            start = i + 1
    return best


# 句尾语气词。用户答"好的呀""行啊""可以啦"时，去掉这几个字才是真正的表态。
#
# 这里**故意不含"吧"**："好吧"是弱肯定（WEAK_YES，故意低于同意阈值），
# 去掉"吧"就变成"好"，而"好"在 YES_EXACT 里——一条把弱肯定抬成明确同意的规则，
# 正是这个文件开头反复强调要避免的错。同理不含"嘛/呗/咯"。
_FILLER_TAIL = "呀啊呢哦嗯啦哟哈"


def _strip_filler(text):
    """去掉语气词之后的"实质内容"。

    只用来判"这句话去掉语气词是不是一个标准答案"，不用于别的判断：
    "好的嗯"→"好的"是同意；但"嗯"去掉之后是空串，仍然是含糊，不能变成同意。
    """
    t = text or ""
    for w in PURE_FILLER:
        t = t.replace(w, "")
    return _norm(t.strip().strip(_FILLER_TAIL))


def _classify(text):
    """classify 的旧名字。其余模块与历史测试仍按这个名字调用，保留以免打断它们。"""
    return classify(text)


def _intent_name(flags):
    """把标记压成一个便于统计的意图名（写进事件的 intent 字段）。

    优先级顺序是**刻意**的：否认 > 挂断 > 转人工 > 带情绪 > 抱怨 > 拒绝 > 肯定 > 提问。
    越靠前的越"改变流程走向"，一句话同时命中多种时必须听最硬的那个
    （"不需要，别打了"是拒绝不是挂断，因为拒绝要回写留痕）。
    新增的 vague 排在最后：它只在没有别的信号时成立，理论上不会和上面冲突，
    放在这里是防止将来加词表时把含糊误盖掉一个明确意图。
    """
    for k in ("deny", "hangup", "handoff", "angry", "complain", "no", "yes", "ask"):
        if flags.get(k):
            return k
    if flags.get("digits"):
        return "info"
    return "unclear" if flags.get("vague") else "unknown"


#: 这些环节的话术不走模型改写（也不走 nlg）：求助步骤、升级承诺、抗议称呼的
#: 道歉、语言不通的说明、赶时间的短版——每一句都是**承诺**（发清单/回电/
#: 不再称呼），模型换个说法把承诺改丢，比说错话更糟。
_FREEZE_INTENTS = frozenset((
    "help_steps", "help_escalate", "name_protest",
    "language_barrier", "hurry_short", "explain_detail", "hotline_close",
    "scope_answer", "checklist_sent", "materials_only", "what_auth",
    "scam_reassure", "consent_checklist"))


def _strip_user_echo(text, user_text):
    """别鹦鹉学舌：不许拿客户本轮原话当自己的开场白。

    11-12 截图：客户问「怎么了」，回复「怎么了，得先确认下您是XX吗」——
    电话里听起来像反问挑衅。agent_free 早有这道闸（_strip_echo_open），
    但规则状态机 / 措辞层 / 性格层都不走它，所以统一在 _reply 出口再剥一次。
    只剥句首，句中提及客户原话不动。
    """
    if not text or not user_text:
        return text
    head = user_text.strip().rstrip("？！?!.。，,！？ ")
    if len(head) < 2 or not text.startswith(head):
        return text
    rest = text[len(head):].lstrip("，,、 ")
    return rest if len(rest) >= 4 else text


#: 用户要求（11-12 20:09）：**所有**收线/告别句之前都必须给热线。
#: 写在这一层而不是逐个改配置，是因为收尾出口太多（正常闭环、拒绝、挂断、
#: 转人工、语言不通、求助升级…），漏一处就等于那类任务没有热线。
_HOTLINE = "4006180092"
_HOTLINE_ZH = "如果还有问题，直接打电话联系 4006180092。"
_HOTLINE_EN = "If you still have questions, just call us on 4006180092."


def _with_hotline(text, session, next_stage=None):
    """在"再见"之前补一句热线；已经给过就不再重复。

    只动告别句：中间轮次（问材料、核身、解释）不挂热线，否则每轮都念一遍
    电话号，客户听到的就是复读机——11-09 那通连问三个问题被念了三遍热线
    的投诉刚修掉，不能又绕回去。
    """
    if not text or _HOTLINE in text:
        return text
    en = (session.get("lang") == "en") if session else False
    if en:
        if "Goodbye" not in text:
            return text
        return text.replace("Goodbye", _HOTLINE_EN + " Goodbye")
    if "再见" in text:
        idx = text.rfind("再见")
        # 前一句以逗号结尾时改成句号：否则会读出「感谢您的配合，如果还有问题…」
        # 这种一逗到底的长句，电话里听起来像没喘气。
        head = text[:idx].rstrip().rstrip("，,、;； ")
        if head and head[-1] not in "。！？!?":
            head += "。"
        return (head + _HOTLINE_ZH + text[idx:]).strip()
    # 有些收尾话术不说"再见"（如"那我不打扰您了"），DONE 轮也要给热线。
    if str(next_stage or "") == "DONE":
        return text.rstrip() + " " + _HOTLINE_ZH
    return text


def _reply(session, text, next_stage=None, tool_call=None, intent="unknown",
           end_reason="", stage_label=None, kb_hit=None, intent_conf=None,
           qa_hit=None):
    """统一的返回构造：把回复、阶段迁移、事件字段一次组装好。

    注意 stage 是"这一轮所处的阶段"（回复归属），next_stage 才是迁移后的阶段。
    stage_label 用于覆盖回复归属：回写那句话属于 WRITEBACK 环节，但这一轮
    是从 CONFIRM 直接跳过去的，不覆盖的话事件的 stage 会记成 CONFIRM，
    质检统计"回写环节说没说要说的那句话"时就查不到。

    kb_hit 只在命中知识条目时传入（含 id / source_id / score / matched）。
    这里的意义是**溯源**：赛后如果有人问"这句'有效期十二个月'依据是什么"，
    事件里能直接翻到条目编号和资料小节，而不是只有一串回复文本。

    qa_hit 是同一件事的另一条出处：命中的是**官方 QA 原话**（config/mock/qa.json）。
    两者分开记，是因为可信度不同——qa 逐字来自官方表格，kb 是我们自己组织的口径，
    混成一个字段后，答辩时说不清哪句是"官方原话"。
    """
    stage_now = stage_label or session["stage"]

    # 话术收口：所有回复都从这一个函数出去，所以 LLM 改写只在这里接一次
    # （契约见 docs/17 第 3.1 节）。默认 provider=template，nlg.say 原样返回，
    # 行为与接入前完全一致——verify 53/53 就是用来守这条的。
    #
    # 但下面三类不改写：
    #   intent=error/empty —— 编排出错、用户沉默时，要的是确定和简短，
    #                         不能引入"模型换个说法"的不确定性；
    #   end_reason 有值     —— 挂断/拒绝话术，"再见"不能被改写成"拜拜下次聊"；
    #   identity_* 且不是 identity_ok —— 核身**问句**（问的是"哪一个后四位"），
    #                         指代必须明确、可逐字复述，不能随模型每次改写漂移
    #                         （11-07 实测：模型会把"预留手机号后四位"压成
    #                         "说下…后四位数字"，反而更含糊）。identity_ok 是
    #                         核对结果陈述、不是问句，仍照常改写以保留口语自然度。
    _freeze_identity = (str(intent or "").startswith("identity_")
                        and intent != "identity_ok")
    # 官方原话（intent="qa"）同样冻结：`config/mock/qa.json` 里是审核员原话，
    # 让它过一遍改写，就等于"我们转述官方口径"——被追问依据时站不住。
    # 冻结的代价是句子偏书面（"可在单据中授权书位置上传"），
    # 这是刻意的取舍：宁可听起来像念文件，也不要听起来像猜的。
    #
    # 判据用 `qa_hit` 而不是只看 intent：GREETING 那句是"官方答案 + 确认公司"，
    # intent 记的是 greeting_confirm_company，但里面含官方原话——
    # 11-08 实测，按 intent 判会漏掉它，措辞层把整句压缩成
    # "麻烦您确认下，您是XX公司的负责人吗"，官方答案被丢掉了。
    _freeze_official = bool(qa_hit) or intent in ("qa", "official_script")
    # 先让**模型客服**说这一句（modules/agent_free）：它的输入是"本环节目标 +
    # 本单事实 + 最近对话"，不是已经定死的骨架——客户说"我早报过三遍了"，
    # 它可以直接认账，而骨架里压根没有这个选项。
    # 说不出来（超时/编数字/少说必答要素/和上一轮重复）就返回 None，
    # 下面照旧走骨架 + 措辞层，行为与接入前一致。
    _free = None
    # 注意：这里**不再排除 _freeze_identity**。
    # 核身问句以前整轮冻结，理由是"指代必须固定"；实测代价太大——一整通电话
    # 大部分轮次都在核身，冻结等于模型从头到尾不说话（11-11 截图那通 13 轮里
    # 模型只说 2 轮，用户因此问"你真的接入大模型了吗"）。
    # 现在的做法：让模型说，但由 agent_free 的内容闸确保它**确实问了**一样能
    # 核对的东西（后四位/尾号/姓名/单号），说不出来就退回骨架。
    # _freeze_identity 仍然作用于下面 nlg 那条改写路径：骨架自己那句不许改。
    if (not end_reason and intent not in ("error", "empty")
            and not _freeze_official and intent not in _FREEZE_INTENTS):
        try:
            from modules import agent_free
            _free = agent_free.reply(
                session,
                session.get("last_user_text") or "",
                session.get("last_flags") or {},
                stage_now,
                _fill_application(text, session["slots"].get("application")),
                must=agent_free._must_say(stage_now))
        except Exception as exc:          # 模型层任何故障都不能影响出话
            from modules import agent_free as _af
            _af._warn_once = getattr(_af, "_warn_once", None)
            _free = None
    if _free:
        text = _free
        session["agent_free_n"] = int(session.get("agent_free_n") or 0) + 1
    elif (not end_reason and intent not in ("error", "empty")
            and not _freeze_identity and not _freeze_official
            and intent not in _FREEZE_INTENTS):
        text = nlg.say(session, _fill_application(text, session["slots"].get("application")),
                       stage_now,
                       # 命中官方口径的那一轮才有东西可"讲清楚"；其余轮次
                       # agent_llm 会自己判不该走，仍走 600ms 快速改写。
                       kb=kb_hit, qa=qa_hit,
                       user_text=session.get("last_user_text") or "",
                       flags=session.get("last_flags") or {})
    else:
        # 挂断/拒绝/出错话术不走改写，但占位符仍要填（C5 统一兜底）。
        text = _fill_application(text, session["slots"].get("application"))

    # 不管这句是模型写的、还是措辞层改写出来的，都不许直呼客户姓名。
    # agent_free 内部有一道，但它只管自己生成的那一句；nlg 改写出来的句子
    # 它管不到——11-13 线上收线句就成了「马涛，这通电话就先到这里」。
    # 放在措辞层之后：改写会把整句重写一遍，之前剥掉的名字会被它加回来。
    # 只剥**句首称呼**（「马涛，…」「马涛先生，…」「马经理，…」），
    # 不管句子里的复述：客户自己报了姓名时，客服复述一次是承接他给的值
    # （verify 有一条专门守这个区别：只说"是我"不许带名字，说了"是我，马涛"
    # 才允许复述）。整句过滤会把后者一起删掉，那是过度。
    if session.get("outbound_target"):
        _nm = str((session["outbound_target"] or {}).get("contact_name") or "").strip()
        if len(_nm) >= 2:
            _forms = "|".join(re.escape(x) for x in sorted(
                {_nm, _nm + "先生", _nm + "女士", _nm + "经理",
                 _nm[0] + "先生", _nm[0] + "女士", _nm[0] + "经理", _nm[0] + "总"},
                key=len, reverse=True))
            # 「马涛您好，」「马涛你好，」也是称呼，别再迭代第四轮。
            text = re.sub(r"^\s*(?:%s)\s*(?:您好|你好|哈|啊)?\s*[，,、]\s*" % _forms,
                          "", text).lstrip("，,、 ")

    # 性格适配放在措辞层**之后**：它只调形式（客套前缀/引导句），
    # 官方原话、核身问句、挂断话术一个字都不动——那三类要么必须逐字复述，
    # 要么指代必须固定，改写与细化都会把依据改没。
    # 核身问句不整体冻结：它要冻结的是**措辞层改写**（nlg，见上），
    # 而性格层只删句首"好的，"这类客套、不动指代与事实。
    # 官方原话 / 挂断 / 出错话术则全冻结，连形式都不动。
    # 收尾那一轮（next_stage=DONE）也算挂断话术，必须一起冻结：
    # 11-09 线上 A01、11-10 对抗 A01/A17 都播成「…感谢您的配合，再见。
    # 我放慢一点说。」——客户已经说清楚了还要"放慢一点说"，逻辑上说不通，
    # 而且这是整通电话最后一句话，客户一定听见，比中间任何一轮都刺眼。
    # 旧判据只看 end_reason 与 intent：正常闭环的 end_reason 是空串（闭环不是
    # 异常结束），intent 又是 consent，两条都不命中，所以漏了。
    _persona_freeze = (bool(end_reason) or intent in ("error", "empty")
                       or _freeze_official or str(next_stage or "") == "DONE")
    text, _persona_applied = persona.adapt(
        session, text, stage_now, freeze=_persona_freeze)
    text = _strip_user_echo(text, session.get("last_user_text") or "")
    text = _with_hotline(text, session, next_stage)
    # 骨架复读：同一句话连着说两遍，客户听到的就是"它根本没在听"。
    # 11-11 截图那通：客户「你不是知道吗？」「到底要补啥」两轮，客服逐字
    # 回了两遍「您可以简单回我一句清楚了或者还有疑问就行」。
    # 模型层有跨轮去重（agent_free 第 4 道闸门），骨架没有——骨架是查表查出来的，
    # 同一个状态自然查同一句，连着两轮状态没动，就原样播两遍。
    if not _free and text and text == session.get("last_reply_text"):
        text = _skeleton_variant(session, text, stage_now, end_reason)
    # 记的是**实际播出去**的那句（含适配后的引导语），下一轮判重复才准。
    session["last_reply_text"] = text

    # 本轮播出的句子是否在问"清楚吗"类引导问题（句末问号才算——陈述里
    # 提到"清楚"不算）。下一轮客户答"不清楚"时，_is_help_seek 靠它区分
    # "在回答问题"和"没听清"；agent_free 与 persona 也用它避免同一句
    # 引导问句在一通电话里问第二遍。
    _asked = (any(w in text for w in _ADVANCE_ASK_MARKS)
              and text.rstrip().endswith(("？", "?")))
    session["last_advance_ask"] = _asked
    if _asked:
        session["advance_ever_asked"] = True
    # NEED 的解释轮播完，事实就算"已讲过"——之后客户赶时间只给短版，
    # 不再把驳回原因整段念第二遍。
    if str(next_stage or "") == "NEED":
        session["facts_spoken"] = True

    if next_stage:
        session["stage"] = next_stage
    # 置信度默认取"这一轮用户话的识别置信度"（respond 里算好放进 session）。
    # 放在 session 里而不是每个调用点都传参，是因为出口太多（回写、兜底、转人工、
    # 收尾都要走 _reply），漏传一处就会出现"某些事件的 conf 永远是 0"这种假数据。
    if intent_conf is None:
        intent_conf = session.get("last_intent_conf", 0.0)
    out = {
        "reply_text": text,
        # 这一句是模型组织的还是骨架话术。录制脚本靠它统计"模型接管了几轮"，
        # 质检也靠它回答"这句到底是谁说的"——没有这个字段，两种输出在
        # 事件里长得一模一样，出了事没法追。
        "reply_source": "model" if _free else "skeleton",
        "stage": stage_now,
        "next_stage": session["stage"],
        "tool_call": tool_call,
        "intent": intent,
        "intent_conf": round(float(intent_conf or 0.0), 2),
        # 本通客户被听成什么性格、这一句有没有真的按性格调过形式。
        # applied=False 是常态（规则表初始为空/中性客户/守卫拦下），
        # 记下来才能回答"为什么这句没做针对性处理"。
        "persona": dict(persona.summary(session), applied_this_turn=_persona_applied),
    }
    if end_reason:
        out["end_reason"] = end_reason
    if kb_hit:
        out["kb"] = {
            "id": kb_hit.get("id", ""),
            "category": kb_hit.get("category", ""),
            "source_id": kb_hit.get("source_id", ""),
            "score": kb_hit.get("score", 0),
            "matched": list(kb_hit.get("matched") or []),
        }
    if qa_hit:
        out["qa"] = {
            "id": qa_hit.get("id", ""),
            "source": qa_hit.get("source", ""),
            "match": qa_hit.get("match", ""),
            "score": qa_hit.get("score", 0),
        }
    return out


#: 骨架复读时的换说法前缀。只换开头，不改内容——骨架的内容是配置里的合规话术，
#: 不能为了"听起来不重复"去改它说的事。
_SKELETON_VARIANTS = ("我刚才可能没说清楚，", "换个说法，", "我再说一遍，")


def _skeleton_variant(session, text, stage, end_reason):
    """骨架要说的话和上一句逐字相同：换一个开头，别原样播两遍。

    收线话术与 DONE 轮不换：那是整通最后一句，改动只会有损。
    """
    if end_reason or str(stage or "") == "DONE":
        return text
    n = int(session.get("skeleton_variant_n", 0))
    session["skeleton_variant_n"] = n + 1
    # 原文常以「嗯，」「好的，」开头，直接拼会变成「换个说法，嗯，清单就在…」——
    # 11-13 截图里那串"换了说法但更别扭"的句子就是这么来的。先剥掉语气词。
    body = str(text or "").lstrip()
    for filler in ("嗯，", "嗯、", "嗯 ", "好的，", "好，", "行，", "对，"):
        if body.startswith(filler):
            body = body[len(filler):].lstrip()
            break
    return _SKELETON_VARIANTS[n % len(_SKELETON_VARIANTS)] + body


def _soothe(text, flags):
    """用户带情绪时，在话术前加一句安抚。文案来自配置，不写死。

    complain 也算：抱怨同样是"情绪没人接住"，只是强度低一档。
    区别在**接住之后怎么做**——angry 走 _angry_hold 暂停推进（下面），
    complain 只加一句致歉，后面照常回答或推进。
    """
    if flags.get("angry") or flags.get("complain"):
        return config_loader.apology_prefix() + text
    return text


# ---------------------------------------------------------------------------
# 带情绪时"暂停推进"（T8）
#
# 旧行为：ANGRY_WORDS 只在 _soothe 里被读，而 _soothe 只接在 GREETING/IDENTITY/
# NEED 的少数几个出口上。于是一路走到 OFFER/CONFIRM 之后，用户骂一句
# "你们这是骗子服务"，客服照样问"您同意领取这份权益吗"——这不是服务态度问题，
# 是**在用户已经明确表达不信任时继续要一个同意**，合规上也站不住。
#
# 现在的规则：NEED/OFFER/CONFIRM 命中情绪 → 致歉 + 承接，**阶段不动**。
# 不动阶段的理由和含糊追问一致：这一环的结论还没拿到，假装过了就是替用户表态。
#
# 不设"骂两次就转人工"：转人工是收尾动作，用户骂两句往往只是发泄，
# 一句"帮您转人工"就结束，等于把本来还能谈的一通电话关掉。
# 真正的兜底是 config.budget 的轮次/时长上限，那条对所有路径都生效。
# 话术取值：`fallback.<键>` → 内置默认，4 号补配置即可改口吻。
# ---------------------------------------------------------------------------
ANGRY_HOLD = {
    "IDENTITY": ("reply_soothe_identity",
                 "非常抱歉让您有这样的感受。您先消消气，我这边还在听着，您方便的时候再告诉我。"),
    "NEED": ("reply_soothe_need",
             "非常抱歉让您有这样的感受。您慢慢说，我这边听着，不着急。"),
    "OFFER": ("reply_soothe_offer",
              "非常抱歉让您有这样的感受。您先别急着定，有什么顾虑您跟我说，我再解释清楚。"),
    "CONFIRM": ("reply_soothe_confirm",
                "非常抱歉让您有这样的感受。您先不用急着答复，我把情况再说清楚，您听着不舒服随时打断我。"),
}


def _angry_hold(session, stage):
    """带情绪时的统一出口：致歉承接 + **不推进阶段**。

    与 _clarify 的区别：_clarify 是"没听懂"，_angry_hold 是"听懂了但用户在气头上"。
    两者都不推进，但话术完全不同——对生气的人说"我没听清"是火上浇油。
    """
    session["angry_holds"] = int(session.get("angry_holds", 0)) + 1
    key, default = ANGRY_HOLD.get(stage, ANGRY_HOLD["OFFER"])
    return _reply(session, config_loader.fallback_text(key) or default, intent="angry")


def _promise_block(session, text, flags):
    """求结果保证的统一拦截出口（合规红线），命中返回回复，未命中返回 None。

    商家/用户问"能不能保证通过"时，任何具体回答都容易被理解成变相承诺，
    必须先于知识库与 fee/validity 兜底拦截，统一走 reply_blocked 口径。
    词表来自场景包 compliance.categories.false_promise，代码不写死业务词。
    """
    _promise_words = (config_loader.compliance_categories()
                      .get("false_promise") or [])
    if any(w and w in text for w in _promise_words):
        return _reply(session, _soothe(config_loader.blocked_reply(), flags),
                      intent="compliance_block")
    return None


# ---------------------------------------------------------------------------
# 官方 QA 接入（2026-11-08）
#
# 接的是什么：官方资料包 27 条问答（11 条高频 + 16 条特殊场景），
# 由 `scripts/build_mock_bundle.py` 从两份官方 xlsx 抽成 `config/mock/qa.json`，
# 经 `tools.lookup_qa()`（适配器层）暴露出来。
#
# 为什么不直接用 `modules/kb.py` 那套：两者解决的不是同一个问题。
#   kb       = 我们自己组织的口径（patterns + answer + source_id），可以改口吻；
#   官方 QA  = 审核员原话，**逐字可复述**，才敢说"这是官方口径"。
# 商家问"在职证明有哪些"，念出官方原文和照我们的理解解释，可信度不是一回事。
# 所以两条库并存，官方优先——能复述原话就先复述原话。
#
# 四条硬约束（都落在下面的代码里，不是口头约定）：
#   1. 只播原话、不改写：所以 intent="qa" 在 `_reply` 里冻结措辞层（nlg）；
#   2. 答不上来就不答：查不到 / 只命中 2 字泛词 / 答案含未填占位，一律退回原路径；
#   3. 一通电话最多播 `QA_MAX_ANSWERS` 条、同一条只播一次——逐字重念同一句
#      比"这个我记下了"更像坏掉的机器人；
#   4. 接口故障等同"没查到"（适配器层的 read_op 语义），不让查询拖垮一通电话。
# ---------------------------------------------------------------------------
QA_MAX_ANSWERS = 4        # 单通电话最多播几条官方原话
QA_MAX_PER_QID = 1        # 同一条官方答案不重复播

# "这句能直接念吗"的判定：占位符一律不能念。
#   XX / ＸＸ —— 官方 27 条里 5 条答案的空位（"在商标状态变为XX后重新提交"）；
#   【】      —— kb_pop 里 96 条驳回话术的槽位标记（"您申请的【】品牌"）。
# TTS 会把它们念成"叉叉""左括号右括号"，听起来像系统故障；
# 而自己补词就是编造官方口径。这类一律不播，退回"记下来、稍后专员解答"。
UNREADABLE_MARKS = ("XX", "ＸＸ", "【", "】")


def _unreadable(text):
    """这条答案里还有没有不能念的占位符。"""
    return any(m in (text or "") for m in UNREADABLE_MARKS)


#: 句子"没说完"的判据。官方表格导出时，27 条 QA 全都没有句末标点
#: （"可在单据中授权书位置上传"），这不算残缺，补个句号就能念；
#: 真正残缺的是话说到一半断掉的那种（"商标虽然是注册状态，但是如果商标"）。
#:
#: 11-11 双端自对练实测：客户问"已注册的对应类商标具体指啥"，客服播了
#: QA-TS-004 的原文——**就是那半句**。客户听到"商标虽然是注册状态，但是如果商标"
#: 之后彻底懵了，后面全靠猜。这不是话术不好，是数据本身断在那儿。
#: 判据只看句尾，不猜语义，三条各自都有实测例子：
#:   1. 结尾是裸数字/量词（"…提供6个月"）
#:   2. 最后 8 个字里有关联词（"但是如果商标""您根据授权书"）
#:   3. 结尾是"的/了/和/或/与"这类必须接宾语的虚词
_DANGLING_WORDS = ("如果", "但是", "因为", "所以", "并且", "而且", "或者",
                   "以及", "根据", "按照", "才能", "需您", "请您", "建议您提供")
_TAIL_EMPTY = ("的", "了", "和", "或", "与", "跟", "把", "被", "在", "对")
_TAIL_QUANT = ("个月", "件", "吨", "天", "年", "次", "份")


def _truncated(text):
    """这句话是不是"说到一半就断了"。"""
    s = (text or "").strip()
    if not s:
        return True
    if s[-1] in "。！？!?…：:；;":
        return False
    tail = s[-8:]
    if any(w in tail for w in _DANGLING_WORDS):
        return True
    if s[-1] in _TAIL_EMPTY:
        return True
    # "…提供6个月" / "…累计≥200"：数字或量词收尾，后面明显还有话
    if re.search(r"\d+\s*(个月|件|吨|天|年|次|份)?$", s) and s[-1].isdigit():
        return True
    if any(s.endswith(q) for q in _TAIL_QUANT):
        return True
    return False


def _qa_lookup(session, text):
    """查官方 QA，返回 dict 或 None。

    dict 里的 `readable` 决定调用方能不能播：
        True   可以逐字播报（`answer` 有值）
        False  命中了但不能播（`blocked` 说明原因），调用方退回原路径

    返回 None 的三种情况都表示"当没这回事"：没命中、本条已播过、本通已超额。
    `blocked` 不进对话，只记进 `session["answer_blocked"]`——事后要能回答
    "这条官方答案为什么没念"，但没必要为了审计在电话里多说一句话。
    """
    q = (text or "").strip()
    if not q:
        return None
    try:
        res = tools.lookup_qa(q)
    except Exception:                    # 适配器故障不能拖垮对话
        return None
    if not isinstance(res, dict) or not res.get("found"):
        return None

    answer = (res.get("answer") or "").strip()
    qid = res.get("qid") or ""
    info = {"id": qid, "source": res.get("source") or "",
            "match": res.get("match") or "", "score": res.get("score", 0),
            "readable": True, "blocked": ""}

    # 太短的答案（"是的""不算"）脱离上下文就没信息量，播出去像敷衍。
    # 这类问题仍旧退回原路径（记下来、稍后专员解答），比硬答一句"是的"安全。
    if len(answer) < 4:
        info["readable"], info["blocked"] = False, "too_short"
        session.setdefault("answer_blocked", []).append(
            {"via": "qa", "id": qid, "source": info["source"],
             "reason": "too_short", "missing": []})
        return info

    # 官方 27 条里有 5 条答案带未填占位（"在商标状态变为XX后重新提交申请"）。
    # 逐字播报会让 TTS 念出"叉叉"，听起来像系统故障；自行补词就是编造官方口径。
    # 两害相权：不播。qid 留在 answer_blocked 里，谁补完这 5 条答案，
    # 谁就能一眼看出是哪几条被跳过。
    if _unreadable(answer):
        info["readable"], info["blocked"] = False, "placeholder"
        session.setdefault("answer_blocked", []).append(
            {"via": "qa", "id": qid, "source": info["source"],
             "reason": "placeholder", "missing": []})
        return info

    # 说到一半断掉的答案不播，理由和占位符一样：念半句比不答更糟。
    # 记进 answer_blocked，谁去补这份官方数据，谁就能在这里看到是哪几条。
    if _truncated(answer):
        info["readable"], info["blocked"] = False, "truncated"
        session.setdefault("answer_blocked", []).append(
            {"via": "qa", "id": qid, "source": info["source"],
             "reason": "truncated", "missing": []})
        return info
    # 官方表格导出的 27 条都没有句末标点。补一个句号是纯排版，
    # 不改一个字的内容，但念出来才像一句话，不像被打断。
    if answer and answer[-1] not in "。！？!?…":
        answer = answer + "。"

    if qid and list(session.get("qa_answered") or []).count(qid) >= QA_MAX_PER_QID:
        return None
    if int(session.get("qa_count") or 0) >= QA_MAX_ANSWERS:
        return None

    info["answer"] = answer
    session.setdefault("qa_answered", []).append(qid)
    # 记下**原话本身**（不只是 id）：`kb_pop.json` 里有几条条目和官方 QA 逐字相同
    # （4 号当时是照官方原文抄进去的）。去重只按 qid 记的话，第二次会从 kb 那边
    # 把同一句话原样再念一遍——用户听到的是一字不差的复读，去重等于没做。
    session.setdefault("spoken_answers", []).append(answer)
    session["qa_count"] = int(session.get("qa_count") or 0) + 1
    return info


def _app_of(session):
    """本通电话对应的那条档案（申请单优先，其次外呼目标）。"""
    return (session["slots"].get("application")
            or session.get("outbound_target") or {})


#: 官方话术与档案里"驳回原因"的最低词面重合度（2 字词交集占比）。
#:
#: 11-11 双端自对练实测出来的一条数据：97 条有官方话术的单里，话术与它自己的
#: reject_reason 的中位重合度只有 0.11，只有 19 条（20%）对得上。
#: 根因是 mock 档案的原因池比官方 68 条场景大得多，"海外公证认证""公章与持有人
#: 名称不一致"这类原因官方库里根本没有条目，`best_code` 只能硬挑一条最像的。
#: 后果很具体：客户问"海外采购凭证要哪几样"，客服念的是"商标状态为待审"，
#: 客户照着错的答案去准备材料。
#:
#: 所以加这道闸：对不上就不念官方原话，退回 `reply_application_detail`——
#: 那句话的原因和材料**都来自这张档案**，说的是这一单的真事。
#: 宁可没有"官方原话"，也不要念成别人家的事。
_OFFICIAL_MIN_COVER = 0.2


def _kw2(text):
    """中文 2 字词集合。不用分词器：这里只需要"有没有说到一块儿去"，不需要词性。"""
    cn = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    return {cn[i:i + 2] for i in range(len(cn) - 1)}


def _reason_covered(line, reason):
    """这条官方话术，讲的是不是档案里写的那条原因。"""
    r, s = _kw2(reason), _kw2(line)
    if not r or not s:
        return True
    return len(r & s) / float(len(r)) >= _OFFICIAL_MIN_COVER


def _official_line(session):
    """本通档案的官方驳回话术（已填槽、已去掉自报身份与热线尾句）。

    没有可用话术时返回空串——调用方退回自己的模板。
    结果缓存在 session 里：一轮里可能问两次，没必要每次重读 kb_pop。
    """
    if "official_line" not in session:
        app = _app_of(session)
        got = official_script.script_for(app)
        line = official_script.dialogue_text(got["script"]) if got else ""
        if line and not _reason_covered(line, app.get("reject_reason")):
            session.setdefault("answer_blocked", []).append(
                {"via": "script", "id": (got or {}).get("id", ""),
                 "reason": "off_reason", "missing": []})
            line = ""
        session["official_line"] = line
        session["official_script_id"] = (got or {}).get("id", "")
    return session["official_line"] or ""


def _kb_answers_question(hit, text, app):
    """命中的条目，答的是不是**客户这一句问的东西**。

    这一条是自对练挖出来的（11-11）：客户问"海外采购凭证具体要哪几样"
    （本单驳回原因是"海外商标注册证明未完成公证认证"），检索命中了
    `KB-POP-001`（讲"商标状态待审"），客服于是念"需您提供已注册状态的对应类商标"。
    客户听到的是另一件事的答案，还照着去准备材料——比说"我确认不到"糟得多。

    判据刻意不依赖分类体系（试过按 category 比，120 条档案的 reject_category
    与 kb 的 category 是两套分类，一条都对不上，等于把检索全关了）：
    只看**客户这句话里提到的本单材料词，在答案里有没有出现**。
        · 客户没提本单材料 → 不判（放行），避免误伤通用问题；
        · 客户提了材料 A，答案里连 A 都没有 → 这条答的不是他问的。
    """
    ans = str((hit or {}).get("answer") or "")
    if not ans:
        return False
    keys = []
    for m in ((app or {}).get("required_materials") or []):
        for seg in re.split(r"[、，,；;（）()]+", str(m)):
            seg = seg.strip()
            if len(seg) >= 3 and seg in (text or ""):
                keys.append(seg)
    if not keys:
        return True
    return any(k in ans for k in keys)


def _answer_official(session, text, flags):
    """先用官方原话答，再退回知识条目；都没命中返回 (None, None, None)。

    返回三元组 (answer_text, qa_hit, kb_hit)：`answer_text` 是要播的那句，
    后两个是溯源信息，原样传给 `_reply`，落进 agent_reply 事件。
    分开返回是因为两者的**可信度不同**：qa 是官方原话（不可改写），
    kb 是我们自己的口径（可改写）。调用方不该把两者混成一个字符串。

    知识条目这一路还多做一件事：**把没填的占位符挡在播报之外**。
    `kb_pop.json` 里 96 条驳回话术都是带【】槽位的模板（"您申请的【】品牌"），
    以前对话层调 `kb.match(text)` 不传 slots，模板原样念出来就是
    "您申请的括号括号品牌"——TTS 念成"左括号右括号"。这里做两条：
        - 传 slots 进去（C2 的渲染能力早就写好了，只是没人接）；
        - 渲染后仍 `missing` 的，整条不播（念半句模板比不答更糟）。
    """
    qa = _qa_lookup(session, text)
    if qa and qa.get("readable") and qa.get("answer"):
        return _soothe(qa["answer"], flags), qa, None
    hit = kb.match(text, slots=session.get("slots") or {}) if kb.enabled() else None
    if hit and hit.get("answer") and not _kb_answers_question(hit, text, _app_of(session)):
        # 命中，但答的不是他问的东西——不播。记下来，谁看报告谁去修检索。
        session.setdefault("answer_blocked", []).append(
            {"via": "kb", "id": hit.get("id", ""), "source": hit.get("source_id", ""),
             "reason": "off_question", "missing": []})
        return None, None, None
    if hit and hit.get("answer"):
        # kb_pop 的 96 条驳回话术都是带【】的模板。以前填不满就整条不播，
        # 等于商家问"为什么驳回"时客服只会说"我这边查不到"。
        # 现在先用**本通档案**把槽位填上（modules/official_script）：
        #   填满 → 播填好的句子；填不满 → 不播，记一条 answer_blocked。
        # 注意两条分支都必须 return：漏掉一个 return，就会把**没填的模板原文**
        # 送进 TTS（11-08 实测踩过，语音里念出"括号括号"就是这么来的）。
        filled, miss = official_script.render_script(
            hit["answer"], _app_of(session), hit.get("id"))
        if miss:
            session.setdefault("answer_blocked", []).append(
                {"via": "kb", "id": hit.get("id", ""), "source": hit.get("source_id", ""),
                 "reason": "placeholder", "missing": list(miss)})
            return None, None, None
        # 去掉官方话术的固定头尾（"尊敬的商家伙伴您好，我是京东资质审核专员…"
        # 与"如有疑问您可以电话联系 4006180092…"）。
        #
        # 11-11 双端自对练实测：客户在第 10 轮问"我这店就叫康舍旗舰店，哪来的原品牌"，
        # 客服把**整条驳回话术**念了出来——开头又自我介绍一遍，结尾还留热线，
        # 中间才是答案。客户听到的是"您好我是审核专员…如有疑问请拨打电话"，
        # 而他问的问题被夹在中间。开场白在第一轮已经说过，热线是挂断时才说的，
        # 这两段在这里都属于"场合不对"。
        # 删空说明这条话术除了头尾没内容，那就退回原文，不硬播半句。
        _body = official_script.dialogue_text(filled)
        if _body:
            filled = _body
        spoken = session.get("spoken_answers") or []
        if filled in spoken:
            # 和刚才播过的某句逐字相同：不念第二遍（理由见 _qa_lookup 末尾）。
            session.setdefault("answer_blocked", []).append(
                {"via": "kb", "id": hit.get("id", ""), "source": hit.get("source_id", ""),
                 "reason": "repeated", "missing": []})
            return None, None, None
        return _soothe(filled, flags), None, dict(hit, answer=filled, rendered=True)
    return None, None, None


def _finish(session, text, reason):
    """收尾：进入 DONE，不再推进，也不再发起新话题。"""
    return _reply(session, text, next_stage="DONE", intent=reason, end_reason=reason)


def _do_writeback(session, consent, changed=False):
    """执行回写：构造工具调用，把阶段推到 DONE。

    consent 只有三种取值，不能压缩成布尔：
        True      用户明确同意
        False     用户明确拒绝（拒绝也要回写，业务侧要知道这通电话的结果）
        "unknown" 问过一次仍含糊——宁可记"不确定"，也不能把"嗯"当成同意

    幂等键带版本号：第一次是 v1，用户改口后是 v2。
    同一个键重复调用不会新建（网络重试安全），版本递增则会更新同一张工单
    （`tools.create_ticket` 用 session_id 做业务主键）——这是"改口能纠正结论"的关键。

    参数：
        consent  本次回写的结论
        changed  是否是改口后的更正（决定用哪句播报话术）
    """
    cfg = config_loader.stage_config("WRITEBACK")

    # 客户已经说过"打错了、没这个人"：这一通不能落任何结论。
    # 这里是最后一道拦：不管从哪个分支调进来，都不许把错人的电话写成工单。
    if session.get("identity_denied"):
        return _finish(session,
                       config_loader.stage_config("GREETING").get("reply_deny", "")
                       or "抱歉打错了，那我不打扰您了，再见。", "not_me")

    # 改口轨迹要在覆盖 consent **之前**取：覆盖完就比不出"从什么改成什么"了
    old_consent = session["slots"].get("consent")
    if changed and old_consent not in ("", None) and old_consent != consent:
        _record_correction(session, "consent", old_consent, consent)

    session["slots"]["consent"] = consent

    version = int(session.get("writeback_version", 0)) + 1
    session["writeback_version"] = version

    content = config_loader.writeback_template()
    content["identity_confirmed"] = bool(session["slots"].get("identity_confirmed"))
    content["need"] = session["slots"].get("need") or "未获取"
    content["consent"] = consent
    content["writeback_version"] = version
    # 11-12：清单真发到后台了才写这两个字段（工具失败不写，绝不放假数据）。
    if session["slots"].get("checklist_sent"):
        content["checklist_sent"] = True
        content["checklist_id"] = session["slots"].get("checklist_id") or ""
    # 商家理解度：CONFIRM 闭环成功（consent=True）时必须同步为 true——
    # 以前只有 consent 一个字段，赛题回写要求的 merchant_understood
    # 永远停在默认 unknown，业务侧拿到的"商家是否清楚整改要求"全是猜的。
    # 三态同构：True→True / False→False / "unknown"→"unknown"。
    # 只在场景模板声明了这个键时才写（主场景模板没有它，payload 不添多余字段）。
    _need_text = content["need"]
    if not isinstance(_need_text, str):
        _need_text = str(_need_text)
    if "merchant_understood" in content:
        content["merchant_understood"] = consent
    if "reject_category" in content or "reject_summary" in content:
        # 驳回域分类（C1 框架 2.2 五域）：申请单级定位依赖 C5（mock 换业务），
        # 但"这通电话谈的是哪个域"从 need 文本就能判。规则来自场景包
        # reject_rules（关键词→域），代码不写死业务词；都匹配不上才是"未定位"。
        _reject_cat = "未定位"
        for _rule in (config_loader.load().get("reject_rules") or []):
            if any(k and k in _need_text for k in (_rule.get("keywords") or [])):
                _reject_cat = _rule.get("category") or "未定位"
                break
        if "reject_category" in content:
            content["reject_category"] = _reject_cat
        if "reject_summary" in content:
            content["reject_summary"] = _need_text[:200]
    # C5：核上申请单后，回写字段优先取档案真值——need 文本只是用户口述，
    # 用它推五域是兜底；档案里已有 reject_category / reject_reason / close_mode。
    _app = session["slots"].get("application") or {}
    if _app:
        if "reject_category" in content and _app.get("reject_category"):
            content["reject_category"] = _app["reject_category"]
        if "reject_summary" in content and _app.get("reject_reason"):
            content["reject_summary"] = str(_app["reject_reason"])[:200]
        if "close_mode" in content and _app.get("close_mode") in ("电话", "京麦"):
            content["close_mode"] = _app["close_mode"]
        if "call_result" in content and _app.get("call_result") in (
                "connected", "no_answer", "refused", "invalid"):
            content["call_result"] = _app["call_result"]
    if session["slots"].get("order"):
        content["order_id"] = session["slots"]["order"].get("order_id")
    # 改口轨迹跟着工单走：一个工单号从 v1 到 vN，改动过程留在同一个对象里。
    # 只在真改过口时才加这个字段——没改过的工单，payload 与以前逐字节一致，
    # 不给（将来的）回写校验器和下游系统添麻烦。
    if session.get("corrections"):
        content["corrections"] = list(session["corrections"])

    tool_call = {
        "name": "create_ticket",
        "args": {
            "session_id": session["session_id"],
            "content": content,
            "idempotency_key": "%s:WRITEBACK:v%d" % (session["session_id"], version),
            "version": version,
        },
    }

    if changed:
        key = "reply_changed" if consent is True else "reply_changed_declined"
        intent = "changed"
    elif consent is True:
        key = "reply"
        # 清单已发后台的收线句必须逐字播出：走 nlg 会被压成"后台能查到"，
        # "忘了直接翻后台清单"这句就不见了（11-12 复现实测）。
        intent = ("consent_checklist" if session["slots"].get("checklist_sent")
                  else "consent")
    elif consent is False:
        key = "reply_declined"
        intent = "declined"
    else:
        key = "reply_unknown"
        intent = "ambiguous_writeback"

    text = cfg.get(key) or cfg.get("reply", "已为您登记。")
    if consent is True and not changed and session["slots"].get("checklist_sent"):
        text = cfg.get("reply_checklist_done") or text
    return _reply(session, text, next_stage="DONE", tool_call=tool_call,
                  intent=intent, stage_label="WRITEBACK")


# ---------------------------------------------------------------------------
# 含糊时的澄清追问
#
# 触发条件是"这一轮没拿到有效信息"（flags["vague"]），处理方式是**追问一句、
# 阶段不动**。为什么不能推进：外呼里推进一格等于"这一环过了"，后面所有判断都
# 建立在它的结论上。用户说"你说是就是吧"，如果直接推到方案沟通，等于替他确认了
# 一个他从没确认的前提——业务侧最后拿到的那张工单，用户是不认账的。
#
# 但追问不能无限次：同一句话问三遍比挂断更伤体验（E04 用例骂的就是这个）。
# 所以到 CLARIFY_MAX 就转人工，把判断权交回给人。
#
# 话术取值顺序：`fallback.<键>` → 内置默认。这样 4 号往配置里补一句就能改口吻，
# 不用等代码；配置没补时也有话说，不会因为缺一个键就抛 KeyError。
# ---------------------------------------------------------------------------
CLARIFY_TEXTS = {
    "NEED": (
        ("reply_unclear_need",
         "不好意思，我这边没太听清。您是想问没通过的原因，还是想问要补哪些材料？"),
        ("reply_unclear_need_2",
         "我再说一遍重点：原因是{reason}，要补{materials}。您接着问就行。"),
    ),
    "OFFER": (
        ("reply_unclear_offer",
         "我这边可能没讲清楚。您是想让我再讲一遍这个方案，还是有别的顾虑？"),
        ("reply_unclear_offer_2",
         "我换种说法：要补{materials}，在商家后台重新提交。您是清楚怎么弄了，还是需要我把清单发到后台？"),
    ),
}


def _clarify(session, stage):
    """含糊输入的统一出口：第一次换句话问，第二次换更窄的选择题，第三次转人工。

    返回 _reply / _finish 的结果（**不推进阶段**，除了转人工那一支）。
    """
    session["clarify_asks"] = int(session.get("clarify_asks", 0)) + 1
    if session["clarify_asks"] > CLARIFY_MAX:
        # 问过 CLARIFY_MAX 次还听不懂，就别再问了。注意这里不回写：
        # 用户从没表达过业务结论，写一张"未同意"的工单是替用户做决定。
        return _handoff(session, "handoff_unclear")

    pairs = CLARIFY_TEXTS.get(stage) or CLARIFY_TEXTS["NEED"]
    key, default = pairs[min(session["clarify_asks"], len(pairs)) - 1]
    # 11-12 19:42：OFFER 里客户说「知道了 / 嗯」，不该再问"要不要把清单发到
    # 后台"——直接发，并告诉他以后不清楚/忘了直接翻后台清单。
    if stage == "OFFER" and session["slots"].get("application"):
        return _checklist_reply(session, stage, session.get("last_flags") or {})
    return _reply(session, config_loader.fallback_text(key, default), intent="unclear")


# ---------------------------------------------------------------------------
# 「求助」与「含糊」是两回事（11-11 晚上三张截图的共性根因）
#
# 客服问「这一步您清楚怎么补吗」，客户答「不清楚」——这不是没听清，
# 是在回答问题：答案是我不会。旧逻辑把这句话当含糊，走的路径是：
# 反问「您是想让我再讲一遍吗」（白耗一轮）→ 整段复述第三遍 →
# 收线「先不打扰您了」。客户从头到尾只想要一句"怎么操作"。
#
# 判据分两层：
#   - 前一轮客服的结尾是引导问句（您清楚怎么补吗/清楚了吗），此时
#     「不清楚/不知道/不会」是对问句的回答 → 求助；
#   - 或者话里带明确的"怎么办"信号（咋补/怎么弄/怎么操作）→ 无论上下文都是求助。
# 求助的处理是**直接给步骤**（一次），给完还不会就升级（发清单+专员回电），
# 全程不复述已说过的事实、不反问、不收线。
# ---------------------------------------------------------------------------
_HELP_ANSWER_MARKS = ("不清楚", "不知道", "不会弄", "不会弄", "搞不懂", "弄不明白",
                      "不了解", "没弄过")
_HOW_SEEK_MARKS = ("咋补", "怎么补", "怎么弄", "怎么操作", "咋弄", "怎么准备",
                   "怎么搞", "怎么办", "咋办")
_ADVANCE_ASK_MARKS = ("清楚怎么补吗", "清楚了吗", "明白了吗", "清楚要怎么准备",
                      "怎么补吗")


def _is_help_seek(session, text):
    """客户这句话是不是在求助（要操作指引），而不是没听清。

    早期版本要求"上一轮问过引导问句"才认——线上实测不成立：NEED 解释完
    事实后客户答"不清楚"，没有问句上下文，但意思同样是"我不会操作"。
    求助词本身就是有效回答，直接给步骤永远比反问"要不要再讲一遍"强。
    真正的没听清（"嗯""啊""啥"）不含这些词，仍走含糊梯子。

    **只在资审场景生效**（本单申请档案已定位）：营销/售后场景里
    "咋弄/怎么弄"是"怎么领取/怎么办理"这类合法提问，走提问链路；
    那边没有材料清单和后台入口，套资审的步骤话术就是答非所问
    （88 条评测 D05 抓过这条回归）。
    """
    if not (session["slots"].get("application") or {}):
        return False
    return _how_seek(text) or any(w in text for w in _HELP_ANSWER_MARKS)


def _how_seek(text):
    return any(w in text for w in _HOW_SEEK_MARKS)


def _help_reply(session, stage):
    """求助的统一出口：第一次给分步操作，第二次升级（发清单 + 专员回电）。

    不复述驳回原因——那是客户已经听过三遍的东西；步骤和升级路径才是
    他此刻要的。第二次之后仍不会，走 _handoff，但工单里带上
    callback_needed 与 merchant_understood=False，专员接手时有据可查。
    """
    n = int(session.get("help_count", 0)) + 1
    session["help_count"] = n
    cfg = config_loader.stage_config(stage) or {}
    if n <= 1:
        return _reply(session, cfg.get("reply_help_steps")
                      or "那我一步一步说：第一步，把{materials}都准备好；"
                         "第二步，登录商家后台，找到资质审核入口，把这些材料逐张上传；"
                         "第三步，重新提交，建议一周内完成就行。"
                         "您对哪一步还不清楚？我再细说这一步。",
                      intent="help_steps")
    # 第二次还不会：升级。写进 slots，闭环回写时这两项会带进工单。
    session["slots"]["callback_needed"] = True
    session["slots"]["merchant_understood"] = False
    if n == 2:
        return _reply(session, cfg.get("reply_help_escalate")
                      or "好，那我不再重复占用您时间：材料清单和操作步骤我稍后"
                         "发到您商家后台，专员会用官方号码给您回电，带着您一步步弄。"
                         "这单我先记成「需要专员协助」，可以吗？",
                      intent="help_escalate")
    # 升级都说了还是"不清楚"：不再原地打转。收尾，但把升级承诺说完整——
    # 这通电话的产出（发清单 + 专员回电）必须在最后一句里再次兑现。
    return _finish(
        session,
        cfg.get("reply_help_bye")
        or "那这单我先帮您记录到这，材料清单和操作步骤会发到您商家后台，"
           "稍后专员会用官方号码给您回电，带着您一步步弄。"
           + "如有疑问，您也可以拨打京东商家热线 4006180092。感谢您的接听，再见。",
        "help_escalated")


# ---------------------------------------------------------------------------
# 语言不通：客户说英文 / 明确表示听不懂中文
#
# 11-11 线上实录：客户问 "can you speak english?"，客服回「没太听清，
# 您方便再说一下主要想了解什么吗」；客户再说 "i can't understand CHinese"，
# 客服回「可能信号不太好。您是想了解产品使用，还是售后这方面的问题呢」——
# 场景都错乱了。宁可承认做不到，也不能装听不见。
# ---------------------------------------------------------------------------
_LANG_SEEK_MARKS = ("english", "chinese", "speak", "understand",
                    "language", "中文", "英文", "普通话")


def _is_foreign_lang(text):
    """这句话是不是在**对我们说**语言不通（而不是环境里的英文噪声）。

    只看"拉丁字母多"不够：嘈杂环境里 ASR 会吐出 "OK, thank you"
    "Yeah right" 这类英文环境音（C6 N03 用例），把它们当成"客户说英文"
    就会把一通正常电话挂成 language_barrier。判据加一层：带问号，或
    明确提到语言（english/中文/听不懂…）——客户是在**问我们**，才接话。
    """
    letters = len(re.findall(r"[A-Za-z]", text))
    if letters < 6:
        return False
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    if letters <= cjk * 2:
        return False
    low = text.lower()
    return ("？" in text or "?" in text
            or any(w in low for w in _LANG_SEEK_MARKS)
            or any(w in text for w in _LANG_SEEK_MARKS))


def _language_reply(session, stage):
    """语言不通的统一出口：第一次如实承认 + 用中文再说一遍；第二次转专员。"""
    n = int(session.get("lang_count", 0)) + 1
    session["lang_count"] = n
    cfg = config_loader.stage_config(stage) or {}
    if n <= 1:
        return _reply(session, cfg.get("reply_lang_barrier")
                      or "不好意思，我这边暂时只能用普通话为您服务。"
                         "我用中文再说一遍重点：原因是{reason}，要补{materials}，"
                         "在商家后台重新提交。",
                      intent="language_barrier")
    return _finish(
        session,
        cfg.get("reply_lang_bye")
        or "很抱歉，我暂时没办法用英文为您办理。我先把您的需求记下来，"
           "稍后会有专员与您联系。如有疑问，您也可以拨打京东商家热线 4006180092。感谢您的接听，再见。",
        "language_barrier")


# ---------------------------------------------------------------------------
# 客户抗议称呼方式：「不要喊我名字！」
#
# 11-11 线上实录：客户喊「不要喊我名字！」，被当成拒绝 → 直接收线回写。
# 客户抗议的是**称呼方式**，不是业务本身——正确动作是道歉、承诺不再称呼、
# 继续办正事，而不是把一个还在配合的客户挂掉。
# ---------------------------------------------------------------------------
_NAME_PROTEST_MARKS = ("不要喊", "别喊", "不要叫", "别叫", "不许喊", "少喊",
                       "不要提我", "别提我", "不要说我名字", "不要念名字",
                       "叫什么名字", "喊什么名字", "不要直呼")


def _is_name_protest(text):
    if not any(w in text for w in _NAME_PROTEST_MARKS):
        return False
    return any(w in text for w in ("名字", "姓名", "称呼"))


def _name_protest_reply(session, stage):
    """抗议称呼的统一出口：道歉 + 承诺不再称呼 + 回到当前环节的正事。"""
    session["no_name_call"] = True
    cfg = config_loader.stage_config(stage) or {}
    return _reply(session, cfg.get("reply_protest_next")
                  or "不好意思，是我不好，接下来我不再提您的姓名。咱们接着说正事。",
                  intent="name_protest")


# ---------------------------------------------------------------------------
# 英文报数：客户把 "4427" 说成 "four four two seven"
#
# ASR 原样给出英文单词，数字守卫把它当成"没报数"，客服只会让对方
# "再重复一遍"。整句都是数字词时直接换算成数字，下游所有匹配照常工作。
# ---------------------------------------------------------------------------
_EN_DIGITS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2",
              "three": "3", "four": "4", "five": "5", "six": "6",
              "seven": "7", "eight": "8", "nine": "9"}


def _normalize_spoken_digits(text):
    """整句都是英文数字词时换算成数字（"four four two seven" → "4427"）。

    只处理**整句都是数字词**的情况：混着中文或其它词的句子不动，
    免得把正常英文（"can you speak english"）错误改写。
    """
    words = [w.strip("，,.!?？!；;、 ").lower() for w in text.split()]
    if len(words) >= 3 and all(w in _EN_DIGITS for w in words):
        return "".join(_EN_DIGITS[w] for w in words)
    return text


# 急着结束闲聊、只要干货的说法。facts_spoken 立起来之后命中即给短版，
# 不再把驳回原因整段念第二遍。
_HURRY_MARKS = ("时间有点急", "有点急", "赶时间", "长话短说", "简单说",
                "直接说重点", "说重点", "快点", "别啰嗦", "别绕弯子")

# 客户说"没听清 / 再说一遍 / 听不懂"：这不是新问题，是上一句没接住。
# 正确动作是**把同一件事讲得更细**（rich），而不是把那句驳回原因原样再念一遍——
# 念一样的他还是听不清（11-11 截图那通：客户连说"我根本没听清"，客服把同一段
# 又念了三遍，客户只好说"你别再重复了"）。
# 客户说"简洁 / 直说 / 别重复"：给**短版**并认账，不再整段重念。
_HEARING_GAP_MARKS = ("没听清", "没听清楚", "听不清", "听不到", "再说一遍", "再讲一遍",
                      "没听懂", "没太懂", "没太听懂", "搞不懂", "没明白", "听不明白",
                      "根本没听清", "没听进去", "听不进去", "没接住")
_CONCISE_MARKS = ("简洁", "直了", "直说", "简单点", "别重复", "别再重复", "不要重复",
                 "别念了", "别啰嗦", "挑重点", "说重点", "精炼", "别绕了")


def _hearing_gap(text):
    return any(w in (text or "") for w in _HEARING_GAP_MARKS)


def _concise_request(text):
    return any(w in (text or "") for w in _CONCISE_MARKS)


def _re_explain_defer(text):
    """带"没听清/别重复"但同时是在问一个**有专门答案**的具体问题
    （费用 / 有效期 / 怎么提交 / 入口）。这种不能抢成"重述驳回原因"，否则答非所问。
    返回 True 表示交给上层正常分支处理。"""
    if _asks_how_to_submit(text):
        return True
    _cls = policy.question_class(text)
    if _cls in ("provenance", "fee", "validity"):
        return True
    return False


# rich / short 的兜底（配置里没有时用，保证这个函数永远有话可回）。
_APP_DETAIL_RICH = ("我把这单从头说清楚，您别急。您这笔申请是{application_desc}，没有通过审核，"
                    "驳回原因是{reason}。要补的材料一共是这几样：{materials}，一样都不能少。"
                    "操作上分三步：先把材料备齐，再登录商家后台找到资质审核入口逐张上传，"
                    "最后重新提交，建议一周内完成就行。材料传上去以后，我们会等资料齐了做完整审核，"
                    "能不能通过要看审核结果，有问题会再跟您联系。要是哪一步想让人带着弄，"
                    "可以拨打京东商家热线 4006180092。您看是材料准备这块不清楚，还是后台上传这块不清楚？")
# 第二遍：换角度，只讲"接下来怎么办"，不再复述原因和材料清单。
# 依据官方资料：① 操作步骤口径（京麦后台-咨询单/资质审核入口-上传-重新提交）；
# ② 结果口径（高频QA「材料上传后，具体是否通过，我们会在审核您完整的材料后
#    才可以确定，如果有问题也会再次联系您的」）；③ 统一热线兜底 4006180092。
_APP_DETAIL_RICH_2 = ("那我换个讲法，只说接下来怎么办：材料备齐后登录商家后台，"
                      "找到这笔申请的资质审核入口，逐张上传，然后重新提交，"
                      "建议一周内完成。材料传上去以后我们会做完整审核，"
                      "能不能通过要看审核结果，有问题会再跟您联系。"
                      "您要是想让人带着弄，随时拨打京东商家热线 4006180092，"
                      "专员会一步步教您。现在先卡在哪一步了，您说一句，我单讲这一步。")
_APP_DETAIL_SHORT = ("好，不重复了：补{materials}，在商家后台重新提交。"
                     "有疑问随时拨 4006180092 找专员。您想单独问哪一样，我单说。")


# ---------------------------------------------------------------------------
# 「补齐到哪 / 这几步都要弄吗 / 还有疑问 / 你到底啥意思」不是没表态
#
# 11-12 线上实录：客户问「补齐到哪」，question_class 没有这一类，骨架直接
# 跳 CONFIRM 播「您看这样解释清楚了吗」；客户答「还有疑问」被算含糊，
# 「你到底啥意思」又撞上营销场景的「同意 / 不需要」词表，最后
# 「同意不需要啥」被判 declined 收线——全程没有一句在回答他的问题。
#
# 这类话的共性是：客户在**追问范围或表示没听懂**，不是含糊、不是拒绝。
# 正确动作是用本单事实正面回答（材料清单 + 三步 + 相对时限），
# 留在本阶段，不讨回应、不推进 CONFIRM、不记含糊、不收线。
# ---------------------------------------------------------------------------
_SCOPE_MARKS = ("补齐到哪", "补到哪", "补齐到什么", "补到什么", "都要弄吗",
                "都得弄吗", "都要做吗", "都得走完吗", "哪几步", "哪些步骤",
                "这几步都要", "具体要补哪", "补哪些")
# 只收「质疑 / 明确说还有问题」。**绝不收"说啥 / 没懂 / 听不懂"**：
# 那是信号不好没听清，走复述梯子；性格跑批 T05「听不清要重说的老师傅」
# 台词反复「啊？你说啥」，第一版收进来后 5 轮闭环被拖成 14 轮不闭环。
_DOUBT_MARKS = ("还有疑问", "有疑问", "啥意思", "什么意思", "到底啥",
                "到底什么")
_SCOPE_FALLBACK = ("这三样材料都要备齐，三步也都得走完：先把{materials}准备好，"
                   "再登录商家后台找到资质审核入口逐张上传，最后重新提交，"
                   "建议一周内完成。您对哪一步还不清楚？")


def _is_scope_question(session, text, flags):
    """客户在追问补齐范围 / 表示没听懂？返回 "scope"/"doubt"/None。

    场景闸：只在有申请单档案时生效（售后场景没有材料清单可讲）。
    表态闸：明确 yes/no/hangup/deny 不拦；但**质疑优先于 no**——
    「同意不需要啥」里带"不需要"，客户其实是在问"你说的啥"，不是拒绝。
    """
    if not session["slots"].get("application"):
        return None
    t = re.sub(r"[\s，,。.!！?？、…～~]", "", text or "")
    if not t or len(t) > 30:
        return None
    if any(w in t for w in _DOUBT_MARKS):
        return "doubt"
    # 「同意不需要啥」这种以"啥"收尾的短质疑句才收；「你说啥」不是质疑。
    if t.endswith("啥") and len(t) <= 8 and any(
            w in t for w in ("不需要", "同意", "到底")):
        return "doubt"
    if any(flags.get(k) for k in ("yes", "no", "hangup", "deny")):
        return None
    if any(w in t for w in _SCOPE_MARKS):
        return "scope"
    return None


def _scope_reply(session, stage, flags, kind="scope"):
    cfg = config_loader.stage_config(stage)
    # scope 与 doubt 分开计数：客户先问范围、再说"还有疑问"，是两件不同的事，
    # 共用一个计数器会让第二句直接跳到升级版本，答得像没听见第一句。
    nk = "scope_answer_n" if kind == "scope" else "doubt_answer_n"
    n = int(session.get(nk, 0)) + 1
    session[nk] = n
    key = "reply_scope" if kind == "scope" else "reply_doubt"
    # 连着追问不能整段复读：第二遍换角度只讲"卡在哪一步"，第三遍升级到专员。
    if n == 2:
        key += "_2"
    elif n >= 3:
        key += "_3"
    tpl = cfg.get(key) or _SCOPE_FALLBACK
    app = session["slots"].get("application")
    # 冻结：只要进改写层，材料清单 + 三步会被压成一句"您清楚了吗"，又变答非所问。
    return _reply(session, _fill_application(_soothe(tpl, flags), app),
                  intent="scope_answer")


# ---------------------------------------------------------------------------
# 11-12 19:42 / 19:46 两张长链：清单直接发后台 + 客户说清楚了就收线
#
# 实录：客户问「什么授权」「啥意思」，客服套「您想先问哪一步」；客户连说
# 「清楚了清楚了」「知道了！！！」「没有了！」「清楚了！」，客服还在复述
# 驳回原因、还在问「还有不明白的吗」，一通电话里同一段话念了四遍。
# 根因：这些明确表态只在 CONFIRM 被当闭环，NEED/OFFER 里全被算成含糊；
# 「清楚了」也从不触发"把清单落到后台"这个真实动作。
# ---------------------------------------------------------------------------
_DONE_ACK_MARKS = ("清楚了", "知道了", "明白了", "没有了", "不用了",
                   "不用再", "别说了", "别吵", "就这样", "行了", "足够了")
_DONE_ACK_NEG = ("不清楚", "没清楚", "不知道", "没明白", "不明白", "没懂", "还有")
_SCAM_MARKS = ("诈骗", "骗子", "骗人", "真假", "真的假的", "是不是官方", "是不是京东")
_WHAT_AUTH_MARKS = ("什么授权", "啥授权", "授权是什么", "什么叫授权", "授权指的")
_CHECKLIST_SENT_FALLBACK = ("好，清单我已经发到您商家后台了：{materials}。"
                            "后面哪一步不清楚、或者忘了要补什么，直接翻后台清单就行。")
_SCAM_FALLBACK = ("理解您谨慎。我们是京东官方资质审核，不会要您转账或报密码，"
                  "可以打京东商家热线 4006180092 核实。")
_AUTH_FALLBACK = ("这里说的授权，是品牌方到经销商、再到您这边的逐级授权文件。"
                  "您这单的问题是{reason}，要补{materials}。清单已经发到您后台了。")
_MATS_FALLBACK = ("您这单要补的就是{materials}，在商家后台资质审核页重新提交。"
                  "清单已经发到后台了，忘了随时翻。")


def _facts_told(session):
    return bool(session.get("facts_spoken") or session.get("official_line_spoken"))


#: 收线之后还允许继续处理的话——只有"改口"。
#: 客户在挂断前说"等等，我说错了""不是我本人"，业务事实变了，工单要出新版本；
#: 除此之外（"对""好的""嗯"）一律回一句"已经结束"。
_CORRECTION_MARKS = ("说错", "错了", "不对", "等等", "改一下", "改成", "重新说",
                     "搞错", "记错", "收回")
#: 收线之后的"表态"也算有效输入：客户在挂断前说"我同意""那就办吧"，
#: 是业务事实变了，得再写一版工单（课程用例 EV01 就长这样：先说不要钱，
#: 听到免费后改口说办）。这类句子不能按"电话已结束"挡掉。
_CONSENT_MARKS = ("同意", "办吧", "就这么办", "那就办", "可以办", "不要了",
                  "不需要了", "不用了", "拒绝")


def _looks_like_correction(text):
    t = text or ""
    return (any(w in t for w in _CORRECTION_MARKS)
            or any(w in t for w in _CONSENT_MARKS))


def _is_done_ack(session, text, flags):
    """客户**不耐烦地/明确地**说完了（清楚了清楚了 / 知道了！！！ / 没有了！）。

    平静的「好的，清楚了」「行，清楚了」不在这里收线：赛题四段式④要靠它们
    从 NEED 推进 OFFER、再进 CONFIRM 才算商家理解确认。逐句复盘里两条用例
    正是这个形状，第一版全收走会直接把闭环链打断（11-12 实测 29/29 → 27/29）。
    """
    if not _facts_told(session) or not session["slots"].get("application"):
        return False
    raw = text or ""
    t = re.sub(r"[\s，,。.!！?？、…～~0-9]", "", raw)
    if not t or len(t) > 12 or any(w in t for w in _DONE_ACK_NEG):
        return False
    if not any(w in t for w in _DONE_ACK_MARKS):
        return False
    _terminal = ("没有了", "不用了", "不用再", "别说了", "别吵", "就这样", "足够了")
    if any(w in t for w in _terminal):
        return True
    if "！" in raw or "!" in raw:
        return True
    return any(t.count(w) >= 2 for w in _DONE_ACK_MARKS)


def _is_scam_worry(text):
    return any(w in (text or "") for w in _SCAM_MARKS)


def _is_what_auth(text):
    t = re.sub(r"[\s，,。.!！?？、…～~]", "", text or "")
    return bool(t) and len(t) <= 16 and any(w in t for w in _WHAT_AUTH_MARKS)


def _is_material_choice(text):
    t = re.sub(r"[\s，,。.!！?？、…～~]", "", text or "")
    return bool(t) and len(t) <= 8 and "材料" in t and not any(
        w in t for w in ("什么", "哪些", "怎么", "为啥", "原因", "多少", "费用"))


def _wants_checklist(session, text, flags):
    """上一轮刚问过「要不要把清单发到后台」，这一轮客户说「需要 / 要」。"""
    prev = session.get("last_reply_text") or ""
    if not any(w in prev for w in ("清单发到后台", "需要我把清单", "把清单发到")):
        return False
    t = re.sub(r"[\s，,。.!！?？、…～~]", "", text or "")
    if (not t or len(t) > 10 or "不需要" in t or "不用" in t
            or flags.get("no") or flags.get("hangup")):
        return False
    return any(w in t for w in ("需要", "要", "发吧", "好", "行", "嗯", "对"))


def _checklist_tc(session):
    app = session["slots"].get("application") or {}
    return {"name": "send_checklist", "args": {
        "session_id": session.get("session_id") or "",
        "application_id": str(app.get("application_id") or ""),
        "materials": [str(x) for x in (app.get("required_materials") or [])],
    }}


def _ensure_checklist(session):
    """真的把清单写进后台（收线轮没有独立的工具事件，这里直接执行）。

    幂等：重复调用返回 already_sent=True，不会在后台留下两条清单。
    只按工具的真实结果记账，工具失败绝不置 checklist_sent=True。
    """
    if session["slots"].get("checklist_sent"):
        return
    res = tools.dispatch(_checklist_tc(session)) or {}
    session["slots"]["checklist_sent"] = bool(res.get("ok"))
    session["slots"]["checklist_id"] = res.get("checklist_id") or ""
    session["slots"]["checklist_error"] = res.get("error") or ""


def _checklist_reply(session, stage, flags):
    _ensure_checklist(session)
    cfg = config_loader.stage_config(stage)
    tpl = cfg.get("reply_checklist_sent") or _CHECKLIST_SENT_FALLBACK
    app = session["slots"].get("application") or {}
    # tool_call 仍交给 app.py 统一写事件（幂等，第二次返回 already_sent=True）；
    # 话术依据是 _ensure_checklist 的真实执行结果，不是"宣称已发"。
    return _reply(session, _fill_application(_soothe(tpl, flags), app),
                  intent="checklist_sent", tool_call=_checklist_tc(session))


def _short_affirm(text):
    t = re.sub(r"[\s，,。.!！?？、…～~]", "", text or "")
    return bool(t) and len(t) <= 8 and any(
        w in t for w in ("需要", "要", "好", "行", "嗯", "对",
                        "知道了", "清楚了", "明白")) and not any(
        w in t for w in ("不", "没", "别"))


def _checklist_again_reply(session, stage, flags):
    """清单已发之后，客户又答一句短确认（对 / 好 / 知道了）。

    11-13 用户截图：「清单发完之后，客户每说一句"对"，客服就再念一遍"清单就在
    您后台"，通话说不到头」。旧实现是原地打转：阶段永远停在 NEED，客户怎么
    应答都换不来下一句，最后只能靠客户自己挂断——这就是截图里那串
    "对 / 对 / 对" 配五遍同样的话。

    现在最多接两次就收口：
        第 1 次：一句短确认 + 把四段式③问回去，并推进到 OFFER；
        第 2 次：换一句，直接问"现在方便提交吗"；
        第 3 次：不再磨，按"客户已经认可"回写收线（带热线）。
    """
    n = int(session.get("checklist_ack_n", 0)) + 1
    session["checklist_ack_n"] = n
    cfg = config_loader.stage_config(stage)
    app = session["slots"].get("application") or {}
    if n == 1:
        tpl = (cfg.get("reply_checklist_again")
               or "清单已经发到您商家后台了，直接翻后台清单就行。"
                  "这几样备齐后在商家后台重新提交，您清楚怎么提交吗？")
        return _reply(session, _fill_application(_soothe(tpl, flags), app),
                      next_stage="OFFER", intent="checklist_sent")
    if n == 2:
        tpl = (cfg.get("reply_checklist_submit")
               or "清单就在后台，照着传就行；传完重新提交，这单会自动进复核。"
                  "现在方便提交吗？")
        return _reply(session, _fill_application(_soothe(tpl, flags), app),
                      next_stage="CONFIRM", intent="checklist_sent")
    # 第三句还是一句"对"：客户已经用行动表态了，不再问，直接记账收线。
    return _do_writeback(session, True)


def _early_answer(session, text, flags):
    """所有阶段分支之前的统一早段入口。返回回复或 None（交回原阶段逻辑）。"""
    stage = session["stage"]
    if stage not in ("NEED", "OFFER", "CONFIRM"):
        return None
    app = session["slots"].get("application")
    if not app:
        return None
    cfg = config_loader.stage_config(stage)

    def say(key, fallback, intent):
        return _reply(session, _fill_application(cfg.get(key) or fallback, app),
                      intent=intent)

    if _is_scam_worry(text):
        return say("reply_scam_reassure", _SCAM_FALLBACK, "scam_reassure")
    if _is_what_auth(text):
        _ensure_checklist(session)
        return say("reply_what_auth", _AUTH_FALLBACK, "what_auth")
    if _is_material_choice(text):
        _ensure_checklist(session)
        return say("reply_materials_only", _MATS_FALLBACK, "materials_only")
    if _is_done_ack(session, text, flags):
        _ensure_checklist(session)
        return _do_writeback(session, True)
    # 清单已经发过，客户又回「知道了 / 需要 / 好」：给一句短确认并**往前推一格**。
    # 只在 NEED 里接：OFFER/CONFIRM 的"对"是四段式③④的表态，得走各自的阶段逻辑
    # 去推进/收线；在这里截住，就等于把客户已经点了头的话当成"知道了"，
    # 永远进不了下一步（11-13 截图那串"对/对/对"就是这么来的）。
    if (session["slots"].get("checklist_sent") and _short_affirm(text)
            and stage in ("NEED", "OFFER")):
        return _checklist_again_reply(session, stage, flags)
    if _wants_checklist(session, text, flags):
        return _checklist_reply(session, stage, flags)
    _scope = _is_scope_question(session, text, flags)
    if _scope and (_scope == "doubt" or session.get("official_line_spoken")):
        _reset_ambiguity(session, True)
        return _scope_reply(session, stage, flags, _scope)
    return None


def _re_explain(session, stage, text, flags):
    """客户又把驳回原因 / 要补材料拎出来说（没听清 or 让别重复）：决定给哪种版本。
    返回 _reply 结果；若不应由这里接（事实还没讲过 / 是别的具体问题）返回 None。"""
    # 场景闸：这套话术讲的是**资质申请单**的驳回原因与材料，只在真有申请单档案
    # 时成立。售后/召回场景没有这个档案，讲出来只会是"您提交的资质申请…
    # 原因是材料问题、要补对应材料"这种空壳话——11-12 Q2 课程自检实测：
    # 售后「听不清」画像 EV17-20 第 7 轮被这里抢成解释，闭环从 40/40 掉到 36/40。
    app = session["slots"].get("application")
    if not isinstance(app, dict) or not app:
        return None
    if not (session.get("facts_spoken") or session.get("official_line_spoken")):
        return None
    # 明确表态的句子一律不拦：「我这边听不清**但同意了**」是同意，不是没听清。
    # 词表永远差一条，语义优先：肯定/否定/挂断已由 classify 判出来，交给上层
    # 正常闭环，绝不能因为句里带"听不清"三个字就把客户的表态吞掉。
    if flags.get("yes") or flags.get("no") or flags.get("hangup") or flags.get("deny"):
        return None
    if not (_hearing_gap(text) or _concise_request(text)):
        return None
    if _re_explain_defer(text):
        return None
    cfg = config_loader.stage_config(stage)
    if _concise_request(text):
        tpl = cfg.get("reply_application_detail_short") or _APP_DETAIL_SHORT
        return _reply(session, _fill_application(_soothe(tpl, flags), app),
                      intent="hurry_short")
    # intent=explain_detail 走冻结：这句是"客户没听清"时的**增量解释**，
    # 里面按官方四段式把本单事实/材料清单/操作步骤/结果口径/热线逐项铺开。
    # 一旦进 nlg 改写层，模型会把它压成一句"原因是XX"（验证脚本实测：
    # 长解释被砍到只剩原因，材料清单和热线全丢），客户还是没听清，
    # 于是下一轮继续复读——等于白改。冻结它，逐字可复述。
    #
    # 第二遍起**换角度**：只讲"接下来怎么办"（操作步骤/结果口径/热线），
    # 不再把原因和材料清单从头念一遍。11-11 截图那通客户连说三次"没听清/
    # 别重复了"，客服三次都是同一段"品类+原因+三样材料"——详细不等于
    # 把同一段念得更长，得给新信息。
    _n = int(session.get("explain_detail_n", 0)) + 1
    session["explain_detail_n"] = _n
    if _n >= 2:
        tpl = cfg.get("reply_application_detail_rich_2") or _APP_DETAIL_RICH_2
    else:
        tpl = cfg.get("reply_application_detail_rich") or _APP_DETAIL_RICH
    return _reply(session, _fill_application(_soothe(tpl, flags), app),
                  intent="explain_detail")


# ---------------------------------------------------------------------------
# 身份核验的槽位驱动追问（T2）
#
# 旧实现只有一个计数器：问满两次就放弃。问句是配置里那一句固定的
# "方便说一下您手机号后四位吗"——用户说"我忘了尾号了"，下一句问的
# 还是同一件事，只是措辞换成问姓名（因为 reply_retry 恰好是问姓名，
# 属于"碰巧对了"，不是逻辑上知道该问什么）。
#
# 这里把身份拆成两个槽位，各自记"问过几次、拿到什么值"：
#     tail  手机尾号（唯一性高，优先问）
#     name  姓名（尾号的替代证据）
# 每轮先看用户给了什么，给了就查、查不到就清掉这个值（避免拿同一个错值
# 反复打接口），然后问**当前最缺、且还没问满**的那个槽位。
#
# 同一槽位最多问 SLOT_ASK_MAX 次，两遍都说不上来就收尾转专员——
# 追着问第三遍"您手机号后四位是多少"是外呼里最招投诉的动作。
# ---------------------------------------------------------------------------
SLOT_ASK_MAX = 2

IDENTITY_ASKS = {
    "tail": (
        ("reply_ask_tail",
         "好的，为了核对身份，方便说一下您手机号的尾号吗？就是后四位。"),
        ("reply_ask_tail_2",
         "那我换个方式核对：您手机号的最后四位是多少？说数字就行。"),
    ),
    "name": (
        ("reply_ask_name",
         "那方便说一下您的姓名吗？我核对一下。"),
        ("reply_ask_name_2",
         "您怎么称呼？说个姓也可以，我这边核对一下。"),
    ),
}

# C6：ASR 噪声 / 环境人声的独立容忍度。
# 与 IDENTITY_VAGUE_MAX 分开是必须的：含糊是**人在回答但答得不明确**，
# 噪声是**根本没人在说话**（咳嗽被识别成 "Cough."、旁人聊天、TTS 回声）。
# 把两者混进一个计数器，连续两声咳嗽就会按 unclear 挂断——人还在线，
# 电话已经断了，这正是 11-06 真实事故的形状（当时只暴露了第 1 声）。
# 连续 NOISE_MAX 轮纯噪声才收尾，是防死等的兜底；期间任何有效输入立即清零。
NOISE_MAX = 5

# 「记不住」给替代路径的次数上限。拒绝（identity_refusals 两次）和噪声
# （NOISE_MAX 五次）都有上限，只有"记不住"原来没有——客户一直想不起号码，
# 客服就一直回"不着急，您也可以说公司全名"，阶段永远停在 IDENTITY。
# 11-13 自进化把它报成 `agent_no_progress|IDENTITY|cant_remember`：
# 通了 6 轮还没结束，客户也挂不了。三次替代路径之后体面收尾，转专员。
ALT_MAX = 3

# 核验环节连续含糊的退出（T12）。
# 旧行为：用户连说"嗯/哦"，客服在尾号/姓名/姓氏之间轮换问法，问满 4 次才
# identity_failed，中间一句致歉都没有。用户体感是"机器在绕圈子"，
# 而真人坐席这时候一定会说"不好意思，信号是不是不太好"。
# 现在第二次含糊就收尾，end_reason 用 unclear（与"核对不上"区分归因）。
IDENTITY_VAGUE_MAX = 2
IDENTITY_VAGUE_ASKS = (
    ("reply_identity_unclear",
     "不好意思，我这边没太听清。您方便再说一次吗？不着急。"),
    ("reply_identity_unclear_final",
     "非常抱歉，可能信号不太好。我这边先记下来，稍后让专员与您联系，感谢您的接听。"),
)

# 从一句话里抽姓名时，这些字出现就说明它不是名字。
# 不这么滤，"真不记得"这种四个汉字会被当成一个姓名去查库。
_NAME_BLOCK = "不没忘知道是我你他她您们吗呢吧啊的对先那这"

# 中文姓名首字必须是姓氏。用于「整句就是一个 2~4 字中文词」这条弱形式：
# 带引导词（我姓/我叫/X先生）的强形式不需要它，引导词本身就是证据。
# 收《百家姓》常见单姓与复姓首字，并补齐 mock 档案用到的生僻姓
# （晏/祁/殷/郗/竺/容/扈/澹/甄/牧/阙/闻/裴/虞/岑/钟/柏/陆/童/沈）。
# 漏掉极生僻姓的代价只是多问一句"没听清"，不会挂断人——在安全侧。
_SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯管卢莫经房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉钮龚"
    "程嵇邢滑裴陆荣翁荀羊惠甄曲家封芮储靳汲邴糜松井段富巫乌焦巴弓"
    "牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘厉戎祖武符刘景詹束龙"
    "叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双"
    "闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍郤璩桑桂濮牛寿通边扈燕冀郏浦尚农"
    "温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘"
    "匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚简饶空"
    "曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公澹台钟离"
)

_NAME_PATTERNS = (
    re.compile(r"我姓([\u4e00-\u9fa5])"),
    re.compile(r"我叫([\u4e00-\u9fa5]{2,4})"),
    re.compile(r"我是([\u4e00-\u9fa5]{2,3})"),
    # 「就是马涛」是真人报姓名最常见的说法之一（前面常带"唉"等语气词）。
    # 11-08 性格跑批里 T06 连报两次都没被收进槽位，客服于是把同一句
    # 「您说个联系人姓名」念了三遍，耐心耗尽挂断——不是话术不好，是没听见。
    # 限定 2~4 字纯中文，且沿用 _NAME_BLOCK：像「就是不清楚」会被挡掉。
    re.compile(r"就是([\u4e00-\u9fa5]{2,4})"),
    re.compile(r"([\u4e00-\u9fa5]{1,3})(?:先生|女士)"),
)


def _identity_text(key, default):
    """身份环节的话术取值：阶段配置 → 兜底配置 → 内置默认。

    三级回落是为了"4 号补配置不用改代码"：现在内置默认就能跑，
    配置一补上就自动以配置为准。
    """
    return (config_loader.stage_config("IDENTITY").get(key)
            or config_loader.fallback_text(key)
            or default)


def _extract_name(text):
    """从一句话里抽一个可信的姓名候选，抽不到就返回空串。

    只认明确的表达方式（我姓X / 我叫XX / 我是XX / X先生），以及
    "整句就是一个 2~4 字中文词"这一种弱形式——后者是为了接住
    "对，张伟"这种补充回答。宁可抽不到，也不能把"真不记得"当名字。

    弱形式额外要求**首字是姓氏**：ASR 把咳嗽/环境音识别成"咕噜"这类
    两字乱码时，没有这条就会被当成姓名提交，白烧一次核身机会
    （C6 N04 实测）。这是中文姓名的语言事实，不是对样例过拟合；
    极生僻姓漏了只会走"没听清请再说一次"，在安全侧。
    """
    t = (text or "").strip()
    if not t:
        return ""
    for pat in _NAME_PATTERNS:
        m = pat.search(t)
        if not m:
            continue
        cand = m.group(1)
        if any(ch in _NAME_BLOCK for ch in cand):
            continue
        return cand
    for seg in re.split(r"[，,。、！？!?\s]+", t):
        seg = seg.strip()
        if (2 <= len(seg) <= 4 and seg[0] in _SURNAMES
                and all("\u4e00" <= ch <= "\u9fa5" for ch in seg)):
            if not any(ch in _NAME_BLOCK for ch in seg):
                return seg
    return ""


def _name_from_answer(session, text):
    """上一句问的是姓名时，把"裸答"的名字收进来（T7）。

    真实通话里没人会说"我叫刘雨儿"，只会说"刘雨儿"，或者"刘雨儿啊""是刘雨儿"。
    `_extract_name` 只认带引导词的写法（我姓/我叫/我是/X先生），这类裸答
    一个字都收不到，槽位永远是空的，于是两轮问满 → identity_failed 挂断。
    这不是边界情况：11-06 的真实通话就是这么把一位客户挂掉的。

    只在"上一句确实在问姓名"时启用。不加这个前提，用户随便回一句四字短语
    （"真不记得""我用着挺好"）都会被当成名字去查库——`_NAME_BLOCK` 挡得住一部分，
    挡不住"屏幕碎了"这种。
    """
    if (session.get("last_asked_slot") or "") != "name":
        return ""
    t = _strip_filler(_norm(text))
    if not (2 <= len(t) <= 4):
        return ""
    if any(ch.isdigit() for ch in t):
        return ""
    if not all("\u4e00" <= ch <= "\u9fa5" for ch in t):
        return ""
    # 不再要求首字是姓氏（11-12）：ASR 把"晏"听成"宴"时，"宴"不在姓氏表，
    # 整句名字就被丢掉，客户明明答了姓名却收到"报一下手机号后四位"。
    # 放宽的代价靠 _NAME_BLOCK 兜住——"不知道""记不清""真忘了"里都含挡词。
    # 前提仍是"上一句确实在问姓名"，不满足时这条函数根本不启用。
    if any(ch in _NAME_BLOCK for ch in t):
        return ""
    return t


def _record_correction(session, slot, old, new):
    """记一次改口：哪个槽位、从什么改成什么、第几轮改的。

    为什么要有这条轨迹：赛后如果有人问"用户说改口了，你们真的改了吗"，
    光看最终 slots 只能看到结果，看不到过程——而"多轮纠错"考的就是过程。
    轨迹同时进两个地方：
        ① session["slots"]["corrections"] —— /api/turn 与上报快照里能直接看到
        ② 回写 payload 的 corrections 字段 —— 工单自己带着改动过程

    不新造事件类型，是因为事件类型被 contracts.EVENT_TYPES 白名单管着
    （契约是禁改区）。真要做成独立事件，需要队长改契约，已在 PR 里提。
    """
    old = "" if old is None else old
    # 三个跳过条件，每个都对应一类假轨迹：
    #   old 为空   —— 第一次提交不是"改口"，记了会让每张工单都带一条空记录
    #   new 为空   —— 清空不是用户的表态
    #   old == new —— 重复说同一个值不是改口
    # 另外不能写 `if not new`：consent 的改口值就是 False，
    # 布尔 False 会被 `not` 判成"没值"，于是"同意→不要"这次改口就丢了。
    if old in ("", None) or new in ("", None) or old == new:
        return
    item = {"slot": slot, "from": old, "to": new, "turn": int(session.get("turn", 0))}
    session.setdefault("corrections", []).append(item)
    # 同步进 slots，让"只看最后一轮 slots"的调用方也能看到（/api/turn 就只看这个）
    session["slots"]["corrections"] = session["corrections"]


def _submit_identity_slot(session, slot, value):
    """提交一个槽位值：记历史、判是否改口、更新当前值。

    判改口用的是"上一个提交过的值"，不是"当前值"——因为查不到就会被清空，
    拿清空后的值比较，C03（李 → 张 → 张伟）的中间那次改口就丢了。
    """
    hist = session.setdefault("slot_history", {}).setdefault(slot, [])
    old = hist[-1] if hist else ""
    hist.append(value)
    _record_correction(session, slot, old, value)
    session["identity_slots"][slot] = value


def _short_digit_run(text):
    """这句是不是在报尾号但位数不够（2~3 位连续阿拉伯数字）。

    只认阿拉伯数字：中文数字报尾号的完整 4 位已在 classify 收，
    "三楼""第3个"这类 1 位数字不是在报尾号，不能拦。
    """
    t = _norm(text or "")
    best = run = 0
    for ch in t:
        if ch.isdigit():
            run += 1
            best = max(best, run)
        else:
            run = 0
    return 2 <= best < 4


def _fill_application(text, app):
    """C5：把申请单档案的真值填进话术占位符（不写死业务内容）。

    场景包用 {application_desc}/{reason}/{materials}/{code} 声明需要哪些事实；
    主场景话术没有这些占位符时原样返回，行为逐字节不变。
    用 str.replace 而不是 str.format：话术里可能有别的大括号，format 会崩。
    **app 为空时也必须把占位符换掉**：没定位到申请单（主场景/查无申请）时
    绝不能把「{brand}」这种花括号原样念给客户听——用中性说法兜底。
    """
    if not text:
        return text
    app = app or {}
    brand = str(app.get("brand_name") or "")
    category = str(app.get("category_l1") or "")
    repl = {
        "{application_desc}": ("%s品牌、%s类目" % (brand, category))
                              if brand and category else "您提交的资质申请",
        "{brand}": brand or "该品牌",
        "{category}": category or "对应类目",
        "{reason}": str(app.get("reject_reason") or "材料问题"),
        "{materials}": "、".join(app.get("required_materials") or []) or "对应材料",
        "{code}": str(app.get("reject_code") or ""),
        # 档案里的 recheck_deadline 是建库时写死的绝对日期（2026-09/10），
        # 到 11 月早已过期；官方 56 条话术也没给具体时限。11-12 用户明确
        # 要求只说相对口径：这里不再取档案日期，任何残留占位符都只得到
        # 「一周内」，绝不把一个过去的日子念给商家听。
        # 公司名与岗位真值：语言不通/抗议称呼时重新发问需要（GREETING 的
        # 话术另外由 _fill_outbound 先填过，这里兜直接调 respond 的路径）。
        "{deadline}": "一周内",
        "{company_name}": str(app.get("company_name") or "贵公司"),
        "{contact_role}": str(app.get("contact_role") or "负责人"),
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def _extract_application_id(text):
    """C5：抽申请单编号（QZ + 12 位数字），四路径核身的第四条。

    号码里含连续数字，但不能当手机尾号收：classify 只认孤立 4 位段，
    而申请单号末尾是长数字串，硬截出来是拿别人的尾号去核验。
    """
    m = re.search(r"\b[A-Z]{2}\d{12}\b", _norm(text or ""))
    return m.group(0) if m else ""


def _extract_company(text):
    """C1/C5：抽裸报的公司名（T7 赛题版——pop 场景核身问的是公司/店铺信息）。

    只认带主体后缀的说法，且长度 ≥6：否则「我们公司」「这个品牌」这类口语
    会被当成公司名收进槽位，拿去查一遍接口又查不到，白烧一次核身机会。
    """
    t = _norm(text or "")
    marks = ("有限公司", "股份公司", "经营部", "分公司", "合伙企业", "商行", "商贸", "科技")
    # 门槛从 6 降到 4：语音里"s上市字号 + 行业"的简称很常见（"安食商贸"），
    # 卡 6 会把真实的简称整个丢掉，客户听到的还是"报一下尾号"。
    # 靠 marks（必须带主体后缀）挡住"我们公司""这个品牌"这类空指代。
    if len(t) < 4 or not any(m in t for m in marks):
        return ""
    if any(w in t for w in ("我们公司", "贵公司", "这个公司", "那家公司", "什么公司")):
        return ""
    # 剥掉开头的应答词/引导语，剩下的当作公司名候选
    for lead in ("我是", "这里是", "我们公司是", "公司是", "我这边是", "对，", "好的，"):
        if t.startswith(lead):
            t = t[len(lead):].strip("，,。. ")
            break
    return t if len(t) >= 4 else ""


def _take_identity_slots(session, text, flags):
    """从这一句里收身份槽位，返回这一轮提交了哪些槽位（列表）。

    返回"提交了哪些"是给调用方决定下一句问什么的：刚报了尾号又核不上，
    下一句该问姓名；再问尾号等于没听见用户说话。
    """
    took = []
    digits = _only_digits(flags.get("digits") or "")
    _runs = re.findall(r"\d+", str(text or ""))
    _longest = max(_runs, key=len) if _runs else ""
    app_id = _extract_application_id(text)
    # classify 只认"孤立 4 位数字段"（那是手机尾号的形状），6 位、8 位、
    # 11 位它一律返回空 digits。而核身要靠**位数**分流：
    #   4 位        → 预留手机号后四位
    #   6 位        → 申请单编号后六位
    #   7~11 位     → 客户在报整号/多报了几位，取末四位当尾号
    # 旧代码没有这一步：客户按提示报 060001（本单正确后六位）时，客服回的仍是
    # "需要您预留手机号的后四位"，客户以为自己说错了——而客服上一句刚说过
    # "报单号后六位也行"。报整号时同理，等于让人把号码说两遍。
    if not app_id and len(_longest) == 6:
        app_id = _longest
    if not digits and 7 <= len(_longest) <= 11:
        digits = _longest[-4:]
    if digits and not (app_id and app_id.endswith(digits)):
        _submit_identity_slot(session, "tail", digits)
        took.append("tail")
    if app_id:
        _submit_identity_slot(session, "application_id", app_id)
        took.append("application_id")
    company = _extract_company(text)
    if company:
        _submit_identity_slot(session, "company", company)
        took.append("company")
    name = _extract_name(text)
    if not name:
        # 带引导词的写法抽不到时，再看这是不是"上一句问了姓名、这一句裸答"（T7）
        name = _name_from_answer(session, text)
    if name and len(name) == 1:
        # 「我姓马」只报了**姓**，不是全名。这不是报错，所以不能说"对不上"；
        # 但也绝不能进 name 槽——`_matches_outbound` 的姓名比较是包含判断，
        # 单字"马"在"马涛"里必然命中，于是报个姓就核上（线上实测）。
        # 存进 surname 只用来判断方向：姓对就请他补全名，姓都不对才说不符。
        session["identity_surname"] = name
        name = ""
    if name:
        _submit_identity_slot(session, "name", name)
        took.append("name")
    return took


def _only_digits(v):
    return re.sub(r"\D", "", str(v or ""))


def _outbound_record(session):
    """这通电话**打给谁**。有意料外呼目标（带申请单号）才返回，否则 None。"""
    tgt = session.get("outbound_target") or {}
    return tgt if tgt.get("application_id") else None


def _char_gap(a, b):
    """两个等长中文字串有几个字不同。不等长直接判不可比（返回 99）。"""
    if len(a) != len(b):
        return 99
    return sum(1 for x, y in zip(a, b) if x != y)


def _name_level(rec_value, val):
    """姓名 / 公司名的匹配程度：exact / fuzzy / none。

    为什么不用拼音：项目零第三方依赖，标准库没有读音表；自己塞一张 3500 字
    拼音表既大又漏（生僻姓氏正好是重灾区）。而语音核身真正要解决的不是
    "算出读音"，是**别把同一个人判成错的**：

        马涛 → 马滔 / 马淘 / 马陶   ASR 同音错字，实际是同一个人

    这几种用"等长 + 只差一个字"就能覆盖（ASR 同音错几乎总是错一个位置，
    连着错两个字的概率很低，而且那种情况还有尾号这条路兜着）。

    代价是"张伟/张山"也会被判 fuzzy——所以 fuzzy **不直接核上**，
    而是复述确认一句（"您说的是张山吗？"），客户点头才记账。
    误判的成本是多问一句，不是把错的人放进来。
    """
    a = str(val or "").strip()
    b = str(rec_value or "").strip()
    if not a or not b:
        return "none"
    if a == b or a in b or b in a:
        return "exact"
    if abs(len(a) - len(b)) <= 1 and _char_gap(a, b) <= 1:
        return "fuzzy"
    # 公司名更长，容一位之差往往不够（"安食商贸"vs"安是商贸"是 1，
    # 但"北京安食商贸"vs"北京市安是商贸有限公司"编辑距离会超）；
    # 只对 ≥6 字的公司名放宽到"含相同字号且差异 ≤2"。
    if len(a) >= 6 and len(b) >= 6:
        core_a, core_b = _company_core(a), _company_core(b)
        if core_a and core_b and abs(len(core_a) - len(core_b)) <= 1 \
                and _char_gap(core_a, core_b) <= 2:
            return "fuzzy"
    return "none"


#: 公司名里不携带辨识信息的尾巴。"北京市安食商贸有限公司" 与 "川尚商贸"
#: 要能对上，就不能拿整串比——比的是字号。
_COMPANY_NOISE = ("股份有限公司", "有限责任公司", "有限公司", "分公司", "公司",
                  "商贸", "商行", "经营部", "科技", "电子商务", "贸易",
                  "北京市", "北京", "上海市", "上海", "天津市", "天津")


def _company_core(name):
    """把公司名剥成字号："北京市安食商贸有限公司" → "安食"。"""
    t = str(name or "").strip()
    for w in _COMPANY_NOISE:
        t = t.replace(w, "")
    return t.strip("（()）·— ")


def _matches_outbound(rec, slot, val):
    """客户报的这句话，是不是这一单的。

    只回答"确定是"（exact）。疑似（同音错字）走 _name_level 的另一条路——
    确认过才算，见 _verify_against_outbound。
    """
    v = str(val or "").strip()
    if not v:
        return False
    if slot == "tail":
        d = _only_digits(v)
        return bool(d) and d[-4:] == _only_digits(rec.get("phone_tail") or rec.get("verify_by_tail"))[-4:]
    if slot == "application_id":
        def _norm(x):
            return re.sub(r"[^0-9A-Za-z]", "", str(x or "")).upper()
        a, b = _norm(v), _norm(rec.get("application_id"))
        return bool(a and b) and (a == b or a.endswith(b) or b.endswith(a))
    if slot == "company":
        c = str(rec.get("company_name") or "")
        short = str(rec.get("company_short_name") or "")
        return bool(c) and (v in c or c in v or (short and short in v))
    if slot == "name":
        n = str(rec.get("contact_name") or "")
        # 单字永远不是姓名。缺这一条，"我姓晏" 会在 `v in n` 里一把过——
        # 线上实测（11-12 晚，发布后用 HTTP 入口跑通）：报一个姓就核上，
        # 接着把驳回原因全念了出来。等于知道姓就能过，核身形同虚设。
        # 这里只做兜底；「我姓X」在收槽位时就已改存 surname，不进 name 槽。
        return len(v) >= 2 and bool(n) and (v in n or n in v)
    return False


def _verify_against_outbound(session, rec, allow_customer=True):
    """外呼单的核身：**只跟这一单比**，不查库。

    11-11 线上实录（用户截图那通）：本单尾号是 9403，客户随口报了个 9673，
    `tools.query_application("9673")` 命中了**另一家商户**（青海梧丘），
    代码把 `slots.application` 直接换成了那家的档案。之后客服念的是
    "梧冈品牌、运动户外类目、需补境外商标注册证"——说的全是别人的事，
    而电话还打给甜源百货。客户听到的就是"你听不懂我说什么"，
    本质是**串单**，比听不懂严重得多：把 A 商家的驳回原因念给 B 听。

    外呼单本来就知道自己打给谁，核身只该回答"是不是这一单"，
    不该有"查出另一单"这个结果。查库只在先呼后找（没有外呼目标）时才需要。
    """
    tried = []
    for slot in ("application_id", "tail", "name", "company"):
        val = session["identity_slots"].get(slot)
        if not val:
            continue
        if _matches_outbound(rec, slot, val):
            session["slots"]["identity_confirmed"] = True
            # 仍然是这一单：写回去的必须是 outbound_target，不许是查出来的记录。
            session["slots"]["application"] = rec
            session["slots"]["customer"] = {
                "customer_id": rec.get("merchant_id"),
                "name": rec.get("contact_name"),
                "phone_tail": rec.get("phone_tail"),
            }
            session["slots"]["identity_via"] = slot
            session["identity_slots"][slot] = ""
            return rec
        # 姓名 / 公司名：同音错字很常见（ASR 把"马涛"听成"宴青梧"）。
        # 只差一个字时**不判错也不判对**——判错会把同一个人赶走（而且现在还会
        # 明确说"没核对上"，更伤）；判对则冒名的人报个近似名就过。
        # 交给下一句复述确认（"我这边听着是「宴青梧」，您说的是这几个字吗？"），
        # 客户点头才记账，记 identity_via=name:fuzzy 可追溯。
        if slot in ("name", "company"):
            _rec_val = rec.get("contact_name") if slot == "name" else rec.get("company_name")
            if _name_level(_rec_val, val) == "fuzzy":
                session["confirm_name"] = {"slot": slot, "heard": val, "asked": False}
                session["identity_slots"][slot] = ""
                return None
        # 没核对上：清掉这个值，下一轮重问。既不能"核上"，也不能去别家找。
        tried.append((slot, val))
        session["identity_slots"][slot] = ""
        session["identity_mismatch"] = slot

    # 本单对不上，再退回**客户库**查一次（只读）。
    # 售后主场景的客户打进来报 "8866"，核的是售后客户档案（张伟），
    # 跟外呼目标那条申请单不是一回事——这条路必须留着，否则主场景整通核不上
    # （verify 证据 14 当场红）。
    # 但**绝不查申请单库**：那正是串单的入口。客户报 9673 命中青海梧丘、
    # slots.application 被换成那家，然后念别人家的驳回原因——就是这么来的。
    #
    # 11-12 又补一条：**开了 application_mode 的赛题场景连客户库也不查。**
    # 两个业务域的 4 位尾号键空间是重叠的（120 份申请单 vs 12 位售后客户），
    # 线上实测报 "1234"（本单是 0086）时，客户库/申请单库里都有能命中的记录，
    # 于是这条回退把人核成了"别人"——见 allow_customer 的说明。
    if not allow_customer:
        return None
    for slot, val in tried:
        hit = tools.query_customer(val) or {}
        if hit.get("found"):
            session["slots"]["identity_confirmed"] = True
            session["slots"]["customer"] = hit.get("customer")
            session["slots"]["identity_via"] = "customer:%s" % slot
            return hit.get("customer")
    return None


def _verify_identity(session):
    """用当前槽位值查客户/申请单档案。核上返回档案，核不上返回 None。

    C5：pop 场景先走 query_application 四路径（姓名/后四位/公司名/申请单号），
    查不到再退回售后客户档案——主场景的「张伟/8866」不在申请单库里，
    行为与改造前完全一致。查不到就**清掉这个值**：不清的话下一轮会拿
    同一个错值再查一遍，表现就是"用户明明改口了，系统还按旧号核验"。
    """
    # 场景隔离：只有声明了 application_mode 的赛题场景才查申请单库。
    # 售后主场景仍走 query_customer，两个业务域共用 4 位尾号键空间，
    # 无条件查申请单会让主场景的「错误尾号 1234」意外命中归档数据。
    use_app = bool(config_loader.load().get("application_mode"))
    session.pop("lookup_error", None)
    # 外呼单：只跟本单比，绝不查库换单（见 _verify_against_outbound 的实录）。
    #
    if use_app and session.get("target_locked"):
        _ob = _outbound_record(session)
        if _ob:
            # 赛题场景不回退客户库：4 位尾号在两个业务域里是同一个键空间，
            # 回退等于给"报了个售后客户的尾号"开门（allow_customer=False）。
            return _verify_against_outbound(session, _ob, allow_customer=not use_app)
    for slot in ("tail", "name", "company", "application_id"):
        val = session["identity_slots"].get(slot)
        if not val:
            continue
        hit = tools.query_application(val) if use_app else {"found": False}
        # P4：查询类工具失败的形状是 {"error": "TIMEOUT", "found": False, ...}
        # （见 adapters/base.py 的 _fail）。只看 found 会把它当成"查无此人"，
        # 清掉槽位、换一个槽位再问——真实事故里 4 轮 4147ms 一直在问尾号，
        # 用户报了也没用，因为根本不是人的问题，是上游掉线。
        # 掉线时不重问、不回写，走兜底话术转专员。
        if hit.get("error"):
            session["lookup_error"] = hit["error"]
            return None
        if hit.get("found"):
            rec = hit.get("application") or {}
            session["slots"]["identity_confirmed"] = True
            session["slots"]["application"] = rec
            # 兼容形状：NEED 阶段的老调用点按 customer.customer_id 取主键
            session["slots"]["customer"] = {
                "customer_id": rec.get("merchant_id"),
                "name": rec.get("contact_name"),
                "phone_tail": rec.get("phone_tail"),
            }
            session["slots"]["identity_via"] = slot
            return rec
        hit_customer = tools.query_customer(val)
        if hit_customer.get("error"):
            session["lookup_error"] = hit_customer["error"]
            return None
        if hit_customer.get("found"):
            session["slots"]["identity_confirmed"] = True
            session["slots"]["customer"] = hit_customer.get("customer")
            session["slots"]["identity_via"] = slot
            return hit_customer.get("customer")
        session["identity_slots"][slot] = ""
    return None


def _lookup_failed(session):
    """P4：把「上游掉线」变成一次明确的收尾，而不是继续追问。

    返回 None 表示没出错，调用方照常往下走；否则返回一句兜底 + DONE。
    不回写工单——身份都没核上，写出去的业务结论没有意义。
    """
    code = session.pop("lookup_error", None)
    if not code:
        return None
    # 统一用 timeout 那句「稍后让专员回电」，不用 error 那句「麻烦再说一遍」：
    # 上游掉线时用户重复多少遍都查不到，请他再说一遍是纯消耗。
    # code 留在 slots 里供事件/工单归因（TIMEOUT 与 UPSTREAM 要能分开看）。
    session["slots"]["lookup_error"] = code
    return _finish(session, fallback_for("timeout"), "tool_error")


def _next_identity_slot(session, prefer=None):
    """下一句该问哪个槽位。两个都问满了返回 None（该收尾了）。

    排序键是"问得少的优先，其次优先问 prefer（没给就取和上一轮不同的那个）"——
    连问两遍同一件事（"您尾号多少" → "您尾号到底多少"）是明确该避免的。
    """
    asks = session.get("slot_asks") or {}
    last = session.get("last_asked_slot") or ""
    if not prefer:
        prefer = "name" if last == "tail" else "tail"
    order = sorted(("tail", "name"),
                   key=lambda s: (asks.get(s, 0), 0 if s == prefer else 1))
    for slot in order:
        if asks.get(slot, 0) < SLOT_ASK_MAX:
            return slot
    return None


#: 客户刚报的哪个值跟本单没核对上 → 对应一句**完整的**追问。
#:
#: 11-12 用户看完线上的要求（截图那通：「没关系，那方便说一下您的姓名吗？」）：
#:   「不应该说没关系，而是我并没有核对上，请您再确认一次并给我报电话号码后四位，
#:     其他两种也是同理。」
#:
#: 两条都对：① "没关系"把一次核身失败说轻了，客户听不出自己报错了；
#: ② 报错之后立刻换一个凭据问（"那说一下您的姓名"），等于告诉客户"你说的我不信"，
#: 而且新凭据他也未必记得。正确做法是先请他就**同一个值**核对再报一次，
#: 问满两次还不对，才换凭据。
#:
#: 所以这几句是整句替换，**不拼** IDENTITY_ASKS 的默认追问——那几句里带着
#: "没关系"，拼在一起就自相矛盾。也不透露正确值（念出来核身就白做了）。
_IDENTITY_MISMATCH = {
    "tail": ("reply_mismatch_tail",
             "我这边没有核对上，麻烦您再确认一次，报一下申请时预留手机号的后四位，"
             "说四个数字就行。"),
    "application_id": ("reply_mismatch_application_id",
                       "这个申请单编号我这边没有核对上，麻烦您再确认一次单号，"
                       "报后六位就行。"),
    "name": ("reply_mismatch_name",
             "这个姓名我这边没有核对上，麻烦您再确认一次申请单上的联系人姓名，"
             "重新报一次。"),
    "company": ("reply_mismatch_company",
                "这个公司名我这边没有核对上，麻烦您再确认一次申请单上的公司全名。"),
    # 客户只报了姓、而且这个姓都对不上时用。不说这句，他就以为"我姓X"已经够了。
    "surname": ("reply_mismatch_surname",
                "您说的这个姓跟申请单上的联系人对不上。"),
}

#: 同音字近似名要复述确认。ASR 把"晏"听成"宴"是常态，这时候既不能判对
#: （冒名的人报个近似名就过），也不能判错（是同一个人）。问一句最省事。
_CONFIRM_NAME_DEFAULT = "我这边听着是「{heard}」，您说的是这几个字吗？"


def _confirm_name_reply(session, next_stage=None):
    """把待确认的近似名念回给客户确认。"""
    pend = session.get("confirm_name") or {}
    heard = str(pend.get("heard") or "")
    text = (_identity_text("reply_confirm_name", _CONFIRM_NAME_DEFAULT)
            .replace("{heard}", heard))
    return _reply(session, text, next_stage=next_stage, intent="identity_confirm")


def _identity_ok_reply(session, app, flags=None, next_stage="NEED"):
    """核上那一刻的回复：精确核上 / 同音确认通过，共用这一句。

    抽出来是因为出口有三处（GREETING 首句就报号、IDENTITY 追问后报号、
    同音字复述确认通过）。三处各写一遍必然漂移——GREETING 那处以前就漏了
    `_fill_application`，话术里的「{application_desc}」会原样念出来。
    """
    cfg = config_loader.stage_config("IDENTITY")
    text = cfg.get("reply_ok", "")
    app = app if isinstance(app, dict) else {}
    # 售后主场景核上的是客户档案（没有 application_id），那种情况不填申请单槽位。
    if app.get("application_id"):
        text = _fill_application(text, app)
    return _reply(session, _soothe(text, flags or {}),
                  next_stage=next_stage, intent="identity_ok")


#: 客户对"您说的是 X 吗"的否定说法。必须排在肯定之前判：
#: "对不上" 以"对"开头、"是不是"以"是"开头，只看前缀会把否认读成确认。
_CONFIRM_NEG_HEADS = ("不是", "不对", "对不", "错了", "弄错", "不姓", "不是我", "不在这")
_CONFIRM_YES = ("对", "是", "是的", "对的", "没错", "嗯", "嗯对", "是我", "就是我",
                "是啊", "对呀", "正确", "是这", "对的没错")


def _confirm_name_answer(session, text, flags):
    """客户对「您说的是 X 吗」的回答。返回回复 dict；不是这一类时返回 None。

    为什么要在阶段分发**之前**判：这句"对/不是"在任何阶段都不是业务意图——
    落进 GREETING 会被当成"确认公司"，落进 IDENTITY 会被当成"拒绝核身"。
    """
    pend = session.get("confirm_name")
    if not pend or not pend.get("asked"):
        return None
    t = _norm(_strip_filler(str(text or ""))).strip("，,。.！!？? ")
    if any(t.startswith(w) for w in _CONFIRM_NEG_HEADS):
        no = True
    else:
        no = bool(flags.get("no") or flags.get("deny"))
    yes = (not no) and (bool(flags.get("yes")) or t in _CONFIRM_YES
                        or any(t.startswith(w) for w in ("对", "是", "没错", "嗯")))
    session.pop("confirm_name", None)
    if yes:
        rec = _outbound_record(session) or {}
        slot = pend.get("slot") or "name"
        session["slots"]["identity_confirmed"] = True
        if rec:
            session["slots"]["application"] = rec
            session["slots"]["customer"] = {
                "customer_id": rec.get("merchant_id"),
                "name": rec.get("contact_name"),
                "phone_tail": rec.get("phone_tail"),
            }
        # 同音确认通过的留痕：事后能分清"报准了"和"听成同音、客户点头了"。
        session["slots"]["identity_via"] = "%s:fuzzy" % slot
        return _identity_ok_reply(session, rec, flags)
    if no:
        # 客户否认了：别再说"没核对上"（他刚纠正过，再强调就是抬杠），
        # 直接给数字这条路——数字不受同音影响，是这通电话最硬的凭据。
        slot = "tail"
        session["last_asked_slot"] = slot
        session["slot_asks"][slot] = session["slot_asks"].get(slot, 0) + 1
        return _reply(session, _identity_text(
            "reply_after_name_denied",
            "明白，那我换个方式：麻烦您报一下申请时预留手机号的后四位，"
            "说四个数字就行。"), intent="identity_retry")
    return None


def _ask_identity(session, slot, next_stage=None):
    """按槽位取话术追问，并记账（问了几次、上次问的是哪个）。

    next_stage 只在"从 GREETING 首次进入核验"时传 IDENTITY：
    那一句在事件里仍归属 GREETING（它是对开场白的回应），
    后续追问则不推进（next_stage=None，阶段本来就在 IDENTITY）。
    """
    # ① 有待确认的近似名（同音字）：先问"您说的是 X 吗"，别急着判对错。
    pend = session.get("confirm_name")
    if pend and not pend.get("asked"):
        pend["asked"] = True
        session["confirm_name"] = pend
        session["last_asked_slot"] = pend.get("slot") or slot
        return _confirm_name_reply(session, next_stage=next_stage)

    # ② 客户只报了姓（"我姓晏"）：请他补全名。
    #    姓对 → 既不判对（一个姓核不了身）也不判错（他确实姓晏）；
    #    姓都不对 → 先说不符，再要全名（不然他补个名字也还是不对）。
    #    不论这一段本来要问哪个槽位：他刚刚报了姓，接着就该要全名。
    #    转头去问"报一下尾号"，等于把他刚说的话扔掉。
    if session.get("identity_surname"):
        sur = session.pop("identity_surname") or ""
        _rec = _outbound_record(session) or (session["slots"].get("application") or {})
        full = str(_rec.get("contact_name") or "")
        session["slot_asks"]["name"] = session["slot_asks"].get("name", 0) + 1
        session["last_asked_slot"] = "name"
        ask = _identity_text("reply_ask_full_name",
                             "好的，那麻烦说一下您的全名，我核对一下。")
        if full and not full.startswith(sur):
            wkey, wdefault = _IDENTITY_MISMATCH["surname"]
            return _reply(session, _identity_text(wkey, wdefault) + ask,
                          next_stage=next_stage, intent="identity_retry")
        return _reply(session, ask, next_stage=next_stage, intent="identity_retry")

    # ③ 刚报的值没核对上，而且**这一轮还要问他同一个值**：
    #    整句用「没有核对上 + 请再确认 + 重报这个值」，不拼默认追问
    #    （那几句带"没关系"，拼上就自相矛盾）。
    #    如果调用方已经决定换一个槽位问（同一个值问满了两次），就不能再用这句——
    #    客户刚被问"再报一次尾号"，下一句怎么变成"说一下姓名"了。
    wrong = session.pop("identity_mismatch", None)
    if wrong and wrong == slot and wrong in _IDENTITY_MISMATCH:
        key, default = _IDENTITY_MISMATCH[wrong]
        session["slot_asks"][slot] = session["slot_asks"].get(slot, 0) + 1
        session["last_asked_slot"] = slot
        return _reply(session, _identity_text(key, default), next_stage=next_stage,
                      intent="identity_retry")

    pairs = IDENTITY_ASKS[slot]
    idx = min(session["slot_asks"].get(slot, 0), len(pairs) - 1)
    session["slot_asks"][slot] = session["slot_asks"].get(slot, 0) + 1
    session["last_asked_slot"] = slot
    key, default = pairs[idx]
    return _reply(session, _identity_text(key, default), next_stage=next_stage,
                  intent="identity_retry")


# 指代词与"具体名词"两张小表（T3 的指代消解）。
#
# 只做一件事：判断"这句话除了指代，有没有实际信息"。
# "就是那个" 只有指代，没有名词 → 不能记成诉求，要追问到底指什么；
# "你刚说的那个权益" 有名词（权益）→ 是有效诉求，照常处理。
# 不追求理解"那个"具体指什么——那需要上下文推理，规则层做不准，
# 做不准的结果就是把一个含糊输入强行解释成一个具体诉求，比不解释更糟。
REFER_WORDS = ("这个", "那个", "它", "刚才", "刚说", "上面说", "你说的", "之前说")
# 动作词：出现在一句"带指代"的话里，说明用户在**要求做什么**，
# 不是在**指什么**。见 _looks_like_reference。
_REFER_VERBS = ("说", "讲", "介绍", "办", "同意", "要", "给", "弄", "看", "查", "改", "帮")
# 属性词：指代目标 + 属性词 = 在**问属性**，不是在指什么。
# "这个权益收费不""那个权益是免费的吧"都是真问题，用户想知道收不收费。
# 当成指代会回一句"您说的还是这份权益，我再讲一遍"——问题没答，
# 而且 OFFER 阶段不动，等于白白浪费一轮（D06/D10 就是栽在这里：
# 用户问完收费，下一句"办吧"只能推到 CONFIRM，进不了 DONE）。
# 注意不能用 CONCRETE_WORDS 代替：那里面有"权益""订单"，
# 而"那个权益"本身正是要算指代的。
_REFER_ATTR_WORDS = ("收费", "费用", "钱", "免费", "价格", "有效期", "多久",
                     "怎么用", "条件", "限制", "范围", "多少钱", "贵")
CONCRETE_WORDS = ("订单", "保修", "快递", "包装", "物流", "发货", "退款", "售后",
                  "质量", "权益", "费用", "收费", "钱", "有效期", "多久", "怎么用")


def _looks_like_reference(text, flags):
    """这句是不是"在指刚才那件事"，而不是在问一个新问题（T6）。

    比 `_is_reference_only` 宽一点，但宽得有边界：
        算：  "就是那个""那个权益""那个订单呢"   —— 指代 + 至多一个指代目标本身
        不算："这个权益多少钱""这个订单到哪了"   —— 有真问题，归提问分支
        不算："你刚说的那个权益挺好啊"          —— 太长，是评论不是指代
    判据是"有没有提问信号"而不是"有没有名词"：只按"有没有名词"判，
    "那个权益"会被推去确认（等于把一次指代当成同意）。
    """
    t = _norm(text)
    if not t or len(t) > 10:
        return False
    if flags.get("ask"):
        return False
    # 带动作词的不是指代，是**要求**。
    # "行，你说这个权益吧"里虽然有"这个权益"，但那是"你讲讲"，该推进到确认；
    # 把它当指代会卡在 OFFER 反复讲同一段，用户会觉得客服听不懂人话。
    # （这条是被 test_intent 的 _to_confirm 抓出来的：它一直是 CONFIRM 的入口句。）
    if any(w in t for w in _REFER_VERBS):
        return False
    # 带属性词的是**问属性**，不是指代。
    # 这里不能指望 flags["ask"]：口语里"收费不""要钱不""免费吧"是正反问句，
    # 没有"吗/呢/？"，ASK_MARKS 抓不到，于是这条检查是唯一的一道拦。
    if any(w in t for w in _REFER_ATTR_WORDS):
        return False
    if not any(w in t for w in REFER_WORDS):
        return False
    return True


def _is_reference_only(text):
    """这句是不是"只有指代、没有内容"。"""
    n = _norm(text)
    if not n or len(n) > 8:
        return False
    if any(w in n for w in CONCRETE_WORDS):
        return False
    return any(w in n for w in REFER_WORDS)


# ---------------------------------------------------------------------------
# 通内指代解析（T6）
#
# 上一版只会"识别指代然后追问"，不会解析。于是"就那个""那个权益"在 OFFER
# 阶段没有任何分支接它，直接落进默认路径推 CONFIRM——用户指了一下某样东西，
# 客服把这当成"同意"，比追问更糟。
#
# 这里只做**同一通电话内**的指代：解析目标是这通电话里刚提过的对象。
# 跨通话的"上次那个订单"一律不解析——那要去查客户档案，查不到还硬答就是编造。
# 也不做"三轮之前那个"这种复杂回指：规则层做不准，做不准时给一个 100% 错的
# 解析，比诚实地问一句"您是指哪件事"更伤人。
# ---------------------------------------------------------------------------
REFER_ORDER_WORDS = ("订单", "单号", "这单", "那单", "快递", "物流", "发货")
REFER_BENEFIT_WORDS = ("权益", "方案", "保障", "延保", "这个服务")


def _note_referent(session, kind, label):
    """记下这通电话里提过的对象，供后面的指代解析用。"""
    session.setdefault("referents", {})[kind] = label
    session["last_referent"] = kind


def _resolve_reference(session, text):
    """把一句指代落到具体对象上，返回 (kind, label)；解析不了返回 ("", "")。

    带名词的（"那个订单""刚才说的权益"）按名词选；
    光说"就是那个"的，用最近一次提到的对象——这正是"连续三轮指代"的场景。
    """
    t = text or ""
    kind = ""
    if any(w in t for w in REFER_ORDER_WORDS):
        kind = "order"
    elif any(w in t for w in REFER_BENEFIT_WORDS):
        kind = "benefit"
    referents = session.get("referents") or {}
    if kind and referents.get(kind):
        return kind, referents[kind]
    last = session.get("last_referent") or ""
    if last and referents.get(last):
        return last, referents[last]
    return "", ""


def _answer_reference(session, kind, label):
    """回答一个已解析的指代。阶段不动——用户只是在问"你说的那个是什么"。"""
    if kind == "order":
        order = session["slots"].get("order")
        tpl = config_loader.stage_config("NEED").get("reply_order_detail", "")
        if order and tpl:
            return _reply(session, tpl.format(amount=order.get("amount"),
                                              status=order.get("status")),
                          intent="reference")
    offer = config_loader.stage_config("OFFER").get("reply", "")
    return _reply(session, "您说的还是%s，我再讲一遍。%s" % (label or "这个", offer),
                  intent="reference")


# ---------------------------------------------------------------------------
# 长会话记忆（T4）
#
# 外呼一通电话可以聊到二三十轮。旧实现只保留 slots，而且 slots 会被后续轮次
# 覆盖（"need" 每轮都可能被改写），于是出现两类问题：
#     1. 用户第 2 轮说的诉求，到第 18 轮已经查不到了——问"我刚才说的什么"答不上；
#     2. 措辞层没有上下文可注入，prompt 里那句"最近两轮对话"一直是占位符。
#
# 这里做的是**最小可用的长期记忆**，不是"让模型记住一切"：
#     facts  跨环节不变的事实（首次诉求、客户称呼、订单号），只写一次不覆盖
#     recent 最近 MEMORY_RECENT 轮的原文，供措辞层注入
#
# 两个刻意的限制：
#     - 只留最近 6 轮原文：再多对 prompt 是噪声，对时延是负担；
#     - 存之前先过禁语过滤：记忆会进 prompt、也可能被复述出来，
#       一条带违规词的记忆等于把合规风险存进了上下文。
# ---------------------------------------------------------------------------
MEMORY_RECENT = 6
MEMORY_TEXT_MAX = 60

# "我刚才说的什么"这类问法的信号词。识别它不是为了炫技，
# 是因为长会话里用户真的会问——答不上来，"记得住"这个考察点就是零。
RECALL_MARKS = ("我刚才说", "我刚说", "我之前说", "我说过", "你还记得",
                "还记得我", "前面说", "刚才提", "我说的是")

# 只命中 RECALL_MARKS 还不够，必须同时有疑问特征才算"在问自己刚才说了什么"。
# 踩过的坑：M04 用例里用户澄清"我说的是快递"，命中「我说的是」被当成回溯问句，
# 于是既不复述也不记诉求，整通卡在 NEED——槽位完整率从 83.3% 掉到 81.5%。
# "我说的是快递"是**陈述澄清**，"我说的是什么来着"才是**回溯提问**，差别就在这几个字。
RECALL_HINTS = ("什么", "啥", "哪句", "哪一句", "来着", "吗")


def _safe_text(text, limit=MEMORY_TEXT_MAX):
    """记忆里存的文本：先删禁语，再截断。

    注意是**删词**不是整句丢弃：删掉违规词，剩下的仍然是有用的上下文；
    整句丢弃会让记忆出现空洞，反而更容易让模型瞎补。
    """
    t = (text or "").strip()
    for w in config_loader.forbidden_words():
        if w and w in t:
            t = t.replace(w, "")
    return t[:limit]


def _remember(session, user_text, out):
    """记一轮：最近原文 + 从 slots 里抽出来的长期事实。

    只在 respond 里调一次（所有出口都经过它）。挂在 _reply 上是不行的：
    _reply 一次调用可能被同一轮里多次触发，会把一轮记成好几轮。
    """
    mem = session.setdefault("memory", {})
    recent = mem.setdefault("recent", [])
    recent.append({
        "turn": int(session.get("turn", 0)),
        "user": _safe_text(user_text),
        "stage": (out or {}).get("stage", ""),
        "intent": (out or {}).get("intent", ""),
    })
    if len(recent) > MEMORY_RECENT:
        del recent[:-MEMORY_RECENT]          # 只留最近 N 轮

    facts = mem.setdefault("facts", {})
    slots = session.get("slots") or {}
    # 诉求只记**第一次**。后面的 need 可能是改口，也可能是"用户表示不需要"，
    # 那是结论不是诉求，覆盖掉就再也答不出"您最初说的是什么"。
    if slots.get("need") and not facts.get("need"):
        facts["need"] = _safe_text(slots["need"])
    customer = slots.get("customer") or {}
    if customer.get("name") and not facts.get("customer_name"):
        facts["customer_name"] = customer["name"]
    order = slots.get("order") or {}
    if order.get("order_id") and not facts.get("order_id"):
        facts["order_id"] = order["order_id"]


def memory_brief(session):
    """给措辞层（nlg）用的记忆摘要，没有可注入内容时返回空串。

    格式是"客户 / 订单 / 诉求 / 最近三句"的短句拼接，不写 JSON——
    prompt 里塞 JSON 会让模型开始照着格式输出结构化内容。
    """
    mem = session.get("memory") or {}
    facts = mem.get("facts") or {}
    parts = []
    if facts.get("customer_name"):
        parts.append("客户：%s" % facts["customer_name"])
    if facts.get("order_id"):
        parts.append("订单：%s" % facts["order_id"])
    if facts.get("need"):
        parts.append("诉求：%s" % facts["need"])
    recent = [r.get("user") for r in (mem.get("recent") or []) if r.get("user")]
    if recent:
        parts.append("最近用户说：%s" % "；".join(recent[-3:]))
    return " | ".join(parts)


def _is_recall_question(text):
    """这句是不是在问"我刚才说了什么"。

    两个条件同时成立才算：命中回溯词 + 有疑问特征。
    只看回溯词的版本会把"我说的是快递"这种澄清句吃掉（见 RECALL_HINTS 上的说明）。
    """
    t = text or ""
    if not any(w in t for w in RECALL_MARKS):
        return False
    return any(h in t for h in RECALL_HINTS)


def _recall(session, text):
    """从记忆里回溯用户说过的诉求。返回 None 表示答不了，交回正常流程。

    身份没核验**绝对不复述**：记忆里可能有姓名、订单号，
    在"还没确认对面是谁"的时候念出来，就是泄漏个人信息。
    """
    if not (session.get("slots") or {}).get("identity_confirmed"):
        return None
    facts = (session.get("memory") or {}).get("facts") or {}
    need = facts.get("need")
    if not need:
        # 兜底：只有提问、没有陈述的通话，facts.need 永远是空的。
        # 起因是 NEED 阶段的"提问"分支排在"记录诉求"之前——"包装压坏了，想问怎么处理"
        # 会整句被当成提问（含"怎么"），需求不落库。实测表现是回溯返回 None，
        # 落回正常流程后把上一句订单详情原样再念一遍。
        # 退回最近一条用户原文：回溯的意义是"复述用户说过的话"，
        # 有没有被归类成诉求，不影响这条记忆的有效性。
        recent = [r.get("user") for r in
                  ((session.get("memory") or {}).get("recent") or [])
                  if (r.get("user") or "").strip()]
        if recent:
            need = recent[-1]
            # 复述别人的原话，即使删过禁语也不带"这个我记着呢"的确认语气——
            # 那句话暗示"我把它当成您的诉求登记了"，而这条路恰恰没登记。
            return _reply(session, "您刚才提到的是：%s。这句我记着。" % need,
                          intent="recall")
    if not need:
        return None
    return _reply(session, "您刚才提到的是：%s。这个我记着呢。" % need, intent="recall")


# ---------------------------------------------------------------------------
# 各阶段的处理器
# 约定：每个处理器拿 (session, text, flags)，返回 _reply(...) 的结果
# ---------------------------------------------------------------------------

def _stage_greeting(session, text, flags):
    """GREETING：自报身份，确认是不是本人。

    如果用户在第一句里就把尾号报了，直接核验并进入 NEED——
    不能让用户"说了等于白说"，再问一遍是最招人烦的体验问题。

    _todo 真实实现要判"接电话的是不是本人"，现在只区分否认 / 挂断 / 带尾号 / 其它。
    """
    cfg = config_loader.stage_config("GREETING")
    if flags["deny"]:
        return _finish(session, cfg.get("reply_deny", "抱歉打错了，再见。"), "not_me")
    if flags["hangup"]:
        return _finish(session, cfg.get("reply_hangup", "抱歉打扰您了，再见。"), "hangup")

    # 用户这一句里报了尾号或姓名：当场核验，核上就直接跳到需求确认。
    # 为什么要收下再查而不是各写一段：两条路径的"报了但核不上"处理是反的
    # （报尾号要改问姓名、报姓名要改问尾号），写成两段必然有一边记不住。
    took = _take_identity_slots(session, text, flags)
    _app = _verify_identity(session)
    if _app:
        return _identity_ok_reply(session, _app, flags)
    # P4：上游掉线不能走「报了但核不上 → 换槽位再问」这条路。
    failed = _lookup_failed(session)
    if failed:
        return failed
    if took:
        # 报了但没核对上：先请他就**同一个值**再确认一次（用户 11-12 明确要求：
        # "我并没有核对上，请您再确认一次"）。立刻换凭据问"那说一下您的姓名"，
        # 等于告诉客户"你说的我不信"，而且新凭据他也未必记得。
        # 同一个槽位问满 SLOT_ASK_MAX 次之后才换。
        wrong = session.get("identity_mismatch") or ""
        if wrong in took and session["slot_asks"].get(wrong, 0) < SLOT_ASK_MAX:
            slot = wrong
        else:
            other = "name" if "tail" in took else "tail"
            slot = _next_identity_slot(session, prefer=other) or _next_identity_slot(session)
        if slot:
            return _ask_identity(session, slot, next_stage="IDENTITY")

    # ── 赛题四段式的第②环：确认「您是XX公司吗」─────────────────────────
    # 官方 52 条话术的固定格式是"您好，我是京东商家资质审核专员，请问您是
    # （商家公司名/店铺名）…吗？"，赛题数据说明写得更死：
    #   "确认对方身份（请问您是 XX 公司吗），**不能省，打错人是重大事故**"。
    #
    # 我们原来在开场白里就把"您提交的资质申请没有通过审核"说了出去——
    # 用户还没确认这通电话是找谁的，就先被告知被驳回了。商家凭什么信？
    # 更糟的是打错了人也要照样把驳回理由念一遍。
    #
    # 现在：用户确认公司之前，只回答问题 + 重复确认公司名，
    # **不进核身、不说驳回原因**。确认之后才按赛题往下走。
    _target = session.get("outbound_target")
    if _target and not session.get("company_confirmed"):
        if _mentions_target_company(text, _target) or _is_affirm_company(text):
            session["company_confirmed"] = True
        elif _is_deny_company(text):
            # 赛题原文："确认对方身份不能省，打错人是重大事故"。
            # 打错了就必须停，不能接着把驳回理由念给错的人听。
            return _finish(session, cfg.get("reply_deny", "抱歉打扰您了，再见。"),
                           "not_me")
        else:
            # 还没确认公司时用户提问：先答（官方原话优先），**但仍然要把
            # "您是XX公司吗"问回去**。赛题数据说明写死了这一环不能省，
            # 答了问题就不确认公司，等于为了体验丢掉最硬的一条合规要求。
            _prefix, _qa_hit, _kb_hit = "", None, None
            if flags.get("ask"):
                _ans, _qa_hit, _kb_hit = _answer_official(session, text, flags)
                _prefix = _ans or ""
                if not _prefix:
                    # 答不上来时至少要说明「为什么现在不能答」。
                    # 11-09 反馈：客户还没确认公司就先问"补什么材料"，
                    # 客服一句不解释、直接把"您是XX公司吗"再问一遍，
                    # 听起来像没听见。合规上确实不能讲，但必须把原因说出来。
                    _prefix = (config_loader.stage_config("GREETING")
                               .get("reply_before_confirm") or "")
            # 公司确认这一环不能省（赛题写死"打错人是重大事故"），但可以换问法。
            # 11-09 逐轮反馈：A07/A09 的客户在开场说"那号我一时真想不起来"，
            # 客服连续 3~4 轮逐字重复同一句"请问您是XX的法定代表人吗"。
            # 换问法不降低合规要求：第一次问全称确认，第二次缩成一句是/否，
            # 第三次给出"让负责人接听/稍后再打"的出口——仍然是同一道闸门。
            _n = int(session.get("company_ask_n", 0)) + 1
            session["company_ask_n"] = _n
            _keys = ("reply_confirm_company", "reply_confirm_company_2",
                     "reply_confirm_company_3")
            _key = _keys[0]
            if policy.enabled("greeting_company_alt"):
                if _n == 1:
                    _key = _keys[0]
                elif _n == 2:
                    _key = _keys[1]
                else:
                    # 第三次起在「一句是/否」与「让负责人接/稍后再打」之间交替，
                    # 不再逐字重复同一句；两条都不越过公司确认这道闸门。
                    _key = _keys[2] if _n % 2 else _keys[1]
            _confirm_q = _fill_outbound(
                config_loader.stage_config("GREETING").get(_key)
                or config_loader.stage_config("GREETING").get("reply_confirm_company")
                or cfg.get("reply", ""), _target)
            return _reply(session, ("%s %s" % (_prefix, _confirm_q)).strip(),
                          next_stage="GREETING", intent="greeting_confirm_company",
                          qa_hit=_qa_hit, kb_hit=_kb_hit)

    # Q1-D6：第一句就问「号码哪来的 / 我凭什么告诉你」——先正面回答，再核对。
    # 开场不答问题直接要尾号，是最容易被挂断的开场之一（Q1 分诊 Q1-R3）。
    # 回答仍要带核身问题，否则用户答完就沉默了。
    if flags.get("ask") or _refuse_identity(text):
        # 官方 QA 优先于知识条目：能复述原话就先复述原话（见 _answer_official）。
        _ans, _qa_hit, _kb_hit = _answer_official(session, text, flags)
        if _ans:
            identity_cfg = config_loader.stage_config("IDENTITY")
            ask = identity_cfg.get(
                "reply_ask_tail",
                "好的，为了核对身份，方便说一下您手机号的尾号吗？就是后四位。")
            # 手工记一次账：这句既答了问题也问了尾号，下一轮该按「问过 tail」算。
            session["slot_asks"]["tail"] = session["slot_asks"].get("tail", 0) + 1
            session["last_asked_slot"] = "tail"
            return _reply(session, "%s %s" % (_ans, ask),
                          next_stage="IDENTITY", intent="greeting_ask",
                          qa_hit=_qa_hit, kb_hit=_kb_hit)

    # 其它任何应答都往下走：核对身份。这里就用"问尾号"那一句——
    # 它是这一环的第一个问题，走同一套话术与记账，别让第一问和后续追问
    # 变成两套逻辑（以前就是这样：第一问读 stages.IDENTITY.reply，
    # 追问读 reply_retry，两边措辞对不上，也没有"问过几次"的记录）。
    return _ask_identity(session, "tail", next_stage="IDENTITY")


#: 核身最多耗几轮。超过就如实收尾，别把整通电话耗在"报一下尾号"上。
#: 11-11 截图那通：客户报错两次号，之后连着 8 轮"哎妈呀/弄不明白/那歇歇"，
#: 系统一直停在 IDENTITY 反复要尾号，直到轮次预算耗尽——客户听到的是
#: "它只会问这一句"，而不是"它在办事"。
_IDENTITY_MAX_TURNS = 8


def _stage_identity(session, text, flags):
    """IDENTITY：核对身份。这是全流程的安全关口。

    规则：
        1. 从这一句里尽量收槽位（尾号、姓名），收到就先拿去查；
        2. 查不到就把这个值清掉，绝不拿同一个错值反复打接口；
        3. 然后问**当前最缺、且还没问满**的那个槽位；
        4. 两个槽位都问满 SLOT_ASK_MAX 次还没核上，收尾转专员。

    _todo 真实实现要判"接电话的是不是本人"（声纹 / 关系人），
    现在只做到"信息对得上"。
    """
    # 兜底：绕太多轮还没核上就如实收尾，不把整通电话耗在"报一下尾号"上。
    # 11-11 截图那通：客户报错两次号，之后连着八轮"哎妈呀/弄不明白/那歇歇"，
    # 系统一直停在 IDENTITY 反复要尾号，直到轮次预算用完——客户听到的是
    # "它只会问这一句"，不是"它在办事"。
    session["identity_turns"] = int(session.get("identity_turns", 0)) + 1
    if (session["identity_turns"] > _IDENTITY_MAX_TURNS
            and not session["slots"].get("identity_confirmed")):
        # 不写工单——身份都没核上，写出去的业务结论没有意义（同 _lookup_failed）。
        return _finish(
            session,
            config_loader.stage_config("IDENTITY").get("reply_too_many_tries")
            or "咱们今天一直没对上，我不占用您时间了。稍后专员用官方号码联系您，"
               "您也可以直接打官方客服 4006180092 核实，再见。",
            "identity_failed")
    cfg = config_loader.stage_config("IDENTITY")

    if flags["deny"]:
        return _finish(session, config_loader.stage_config("GREETING").get("reply_deny", ""), "not_me")

    # 用户在核验环节要求挂断：直接结束，不回写（身份没确认，写回去没意义）。
    # Q1-D2：原来 `flags["no"]` 也走这里，导致「要钱不？要钱我可不要」被当成
    # 拒绝核身直接挂断——用户问的是收费，不是不肯报尾号。现在 `no` 交给下面
    # 的 `not took` 分支按内容分流：拒绝提供信息才解释+收尾，问收费先答再重问。
    if flags["hangup"]:
        return _finish(session, config_loader.stage_config("GREETING").get("reply_hangup", ""), "refused_identity")

    # 带情绪（T8）：核验环节挨骂，最不该做的就是"您手机号后四位是多少"接着问。
    # 用户会把这句话听成"我的不满你根本没接"。
    if flags.get("angry"):
        return _angry_hold(session, "IDENTITY")

    # T17-②：反问式不满（"干什么？""你需要干什么？"）必须**先说明来意**，
    # 不能像真实事故那样接着问姓名、更不能回一句"没事"敷衍。
    # 与 _angry_hold 的区别：骂人是暂停推进安抚；反问是不满但人还在听，
    # 把"我是谁、为什么打来"讲清楚通常就愿意配合了。
    # 只认反问式，普通 COMPLAIN_WORDS（"说了也没用"）仍走原来的 _soothe
    # 路径，不改变既有行为。不烧 slot_asks、不推进阶段、不回写。
    if (flags.get("complain") and not flags.get("angry")
            and _is_rhetorical_displeasure(text)):
        return _reply(
            session,
            _identity_text("reply_complain_hold",
                           "不好意思打扰您了，我是京东客服中心的小采，来电不是推销，"
                           "是想跟您核对一下信息。麻烦您配合一下，方便的话说一下手机号后四位。"),
            intent="complain")

    # C6：噪声与环境人声单独走一条通道，绝不落进含糊/核身失败的桶里。
    # classify 已把「无中文且无数字」的 ASR 输出标成 evidence=noise——
    # 咳嗽 Sneeze./Cough.、纯英文环境音、旁人说话被误当成用户发言，都属此类。
    # 这些不是用户态度，是链路噪声：不烧 identity_attempts、不累加
    # identity_vague、不轮换槽位、不推进阶段、不回写，只礼貌请对方重说。
    # noise_turns 只在「用户真的给出了可核身信息」时清零（见下方 ② 之后），
    # 否则连续几声咳嗽会被每轮清零掩掉，永远触发不了兜底。
    # 英文会话里的英文句子不算噪声：classify 的 noise 判据是"无中文且无数字"，
    # 它只认中文语料。11-13 改完语言判定后，客户说完整英文句才会切到 en，
    # 一旦把这种句子算成噪声，连着三句就把一个正常说英语的客户挂断
    # （实测：三句英文 → "信号不太好…再见"）。噪声这条路只对中文会话生效。
    if flags.get("evidence") == "noise" and session.get("lang") != "en":
        session["noise_turns"] = int(session.get("noise_turns", 0)) + 1
        if session["noise_turns"] >= NOISE_MAX:
            return _finish(session,
                           _identity_text("reply_noise_final",
                                          "可能这边信号不太好，我先不打扰您了，稍后会有专员与您联系，再见。"),
                           "no_voice")
        return _reply(session,
                      _identity_text("reply_noise",
                                     "不好意思，刚才有点杂音没听清，您方便再说一次吗？"),
                      intent="noise")

    # 连续含糊 → 退出机制（T12）。见 IDENTITY_VAGUE_MAX 上的说明。
    if flags.get("vague"):
        session["identity_vague"] = int(session.get("identity_vague", 0)) + 1
        key, default = IDENTITY_VAGUE_ASKS[
            1 if session["identity_vague"] >= IDENTITY_VAGUE_MAX else 0]
        text_ = _identity_text(key, default)
        if session["identity_vague"] >= IDENTITY_VAGUE_MAX:
            return _finish(session, text_, "unclear")
        return _reply(session, text_, intent="unclear")

    # 尾号报了一半（"111"=3 位）：引导补全，不算失败、不轮换槽位。
    # T17-①：真实通话里用户报 3 位尾号没接住，反而跳问别的——
    # 用户会觉得"我给了你又不认"，然后系统继续在槽位间横跳。
    # 用户明确在报数字（≥2 位但不足 4 位），最该做的是请他说完整。
    if not flags.get("digits") and _short_digit_run(text):
        return _reply(session,
                      _identity_text("reply_ask_tail_short",
                                     "您报的数字好像少了几位。麻烦说一下手机号的后四位，四个数字就行，不着急。"),
                      intent="identity_retry")

    # ① 收槽位（后说的覆盖先说的，并记一条改口轨迹）
    # 返回「这一轮提交了哪些」，C6 靠它区分「人在回答」和「只是有声音」。
    took = _take_identity_slots(session, text, flags)

    # ② 查。尾号优先：它唯一性比姓名高（同名太常见）
    if _verify_identity(session):
        session["noise_turns"] = 0
        app = session["slots"].get("application")
        # C3 余项：关单方式按申请单真值自动判定。京麦驳回/无需外呼的档案
        # 不该在电话里念站内指引，也不该继续推进六阶段——核上即礼貌收尾。
        if app and (app.get("close_mode") == "京麦"
                    or app.get("call_status") == "无需外呼"):
            return _finish(session,
                           _fill_application(cfg.get("reply_jingmai_no_call",
                                                     "您这笔申请走站内指引处理即可，就不占用您时间了，稍后会有站内消息同步，再见。"), app),
                           "jingmai_no_call")
        # C5：核上的那一刻就把「哪笔申请、哪个原因、补什么」说清楚，
        # 不再让用户再口述一遍业务域——这是申请单定位的真实落点。
        # 这一段**不用**官方话术：赛题四段式③要求"说明驳回原因与整改"，
        # 而官方话术是按"场景话术"写的（可能不提本单具体原因与需补材料）。
        # 官方原话留给"商家追问为什么"的场合（见 NEED 的提问分支），
        # 核身这一句保持"品牌 + 原因 + 补什么"的可溯源三件套。
        return _identity_ok_reply(session, app, flags)

    # P4：查询工具报 error 时不重问、不烧核身次数，直接兜底转专员。
    failed = _lookup_failed(session)
    if failed:
        return failed

    # C6：这一轮没给出任何可核身信息 —— 旁人说话、答非所问、中文环境
    # 人声被 ASR 当成用户发言。纯文本层**区分不了**「谁在说话」，但可以
    # 做到不误伤：它不是一次核身失败，不该消耗 asks 追问次数、不该轮换
    # 槽位、更不该把人挂断。真实嘈杂环境里三五句环境人声就会把追问机会
    # 耗光，用户一句话没说就被礼貌送走——与 11-06 咳嗽事故同构。
    # 真正的分离要在音频前端做（说话人分离/波束成形），见
    # competition/官方资料包/嘈杂人声降噪_C6.md；这里是文本层兜底。
    if not took:
        # 这一轮没给出可核身信息，但**不等于**没听清。Q1 分诊（24 条失败里
        # 20 条是这个根因）暴露：原来的写法把「提问 / 质疑 / 拒绝提供信息」
        # 全回成「有杂音没听清」，用户连问四轮都被敷衍，最后还被挂断。
        # 真噪声（evidence=noise）已在上面单独走了通道，这里按内容分三条路：
        # 客户在质疑 / 提问 / 拒绝 = **人在谈**，不是链路噪声。
        # 11-11 L2 基线 S05 实测：客户第 6 轮问「我凭什么告诉你，你先说你是
        # 哪个部门的」，只因前几轮没报尾号，累计到 NOISE_MAX 就在这一句回
        # 「可能信号不太好」并挂断——明明是人在质问，却说成链路噪声。
        #
        # 收紧后的口径：只有「不含任何实质内容」的轮才累计噪声计数。
        # 闸门没拆：纯噪声/答非所问连 5 轮仍走 no_voice 收线；
        # 明确拒绝核身仍由下面 ① 的 identity_refusals 两次收尾。
        # 真噪声（evidence=noise）走上面 2094 行那条独立通道，不受影响。
        _contentful = (flags.get("ask") or flags.get("no") or flags.get("complain")
                       or _refuse_identity(text) or _is_rhetorical_displeasure(text)
                       or _cant_remember(text) or _declines_info(text))
        if _contentful:
            session["noise_turns"] = 0
        else:
            session["noise_turns"] = int(session.get("noise_turns", 0)) + 1
            if session["noise_turns"] >= NOISE_MAX:
                return _finish(session,
                               _identity_text("reply_noise_final",
                                              "可能这边信号不太好，我先不打扰您了，稍后会有专员与您联系，再见。"),
                               "no_voice")

        # ① 明确拒绝提供身份信息（不报/不说/凭什么/不告诉，以及口语化的
        #    「我不会报 / 不给」）：不能追着要，也不能一句「再见」挂掉——
        #    先解释为什么核对，并给替代路径（订单号 / 姓名 / 尾号任一）。
        #    两次仍拒绝才收尾。
        #
        #    11-13 补 `_declines_info`：原来只把这类话当"不算噪声"，不进拒绝计数，
        #    于是客户连说六轮"号码我不会报"，客服六轮都在给替代路径，通话说不到头。
        #    从"五轮挂断"修成"无限循环"比原来更糟——拒绝必须有终点。
        if _refuse_identity(text) or _declines_info(text):
            session["identity_refusals"] = int(session.get("identity_refusals", 0)) + 1
            if session["identity_refusals"] >= 2:
                return _finish(
                    session,
                    _identity_text("reply_refuse_final",
                                   "理解您的顾虑，那我不勉强。稍后会有专员用官方号码联系您，您可以先核实身份。再见。"),
                    "refused_identity")
            return _reply(
                session,
                _identity_text("reply_refuse_identity",
                               "理解您谨慎，这边核对只是想确认是您本人接听。您不方便报尾号的话，"
                               "报一下订单号、或者您登记的姓名，任意一个都行。"),
                intent="identity_refuse")

        # ①' 客户记不住号码（"号码是店里注册的，我一时真想不起来"）：
        #     这不是拒绝，也不是噪声，是**凭据拿不出来**。继续给替代路径是对的，
        #     但必须有次数上限——否则客户一直记不住，客服就一直问，通话说不到头。
        #     三次之后体面收尾，转专员用官方号码联系（客户可以事后核实）。
        if _cant_remember(text):
            session["identity_alt_n"] = int(session.get("identity_alt_n", 0)) + 1
            if session["identity_alt_n"] >= ALT_MAX:
                return _finish(
                    session,
                    _identity_text("reply_alt_final",
                                   "那我把您的情况记下来，稍后会有专员用官方号码联系您，"
                                   "您可以先核实身份，再核对这笔申请。再见。"),
                    "identity_unverifiable")

        # ② 提问 / 质疑（"你先说号码哪来的"、"核验这么麻烦"）：先正面回答。
        #    官方 QA 原话优先，其次知识库（带出处），答不上也要给一句可核实的承诺，
        #    然后把话题拉回核身。绝不回「没听清」——那是把用户的问题当空气。
        _ans, _qa_hit, _kb_hit = _answer_official(session, text, flags)
        if _ans:
            return _reply(
                session,
                "%s 另外麻烦您报一下手机号后四位，我核对完马上给您办。" % _ans,
                intent="identity_ask", qa_hit=_qa_hit, kb_hit=_kb_hit)
        if flags.get("ask") or flags.get("no"):
            # 提问分两类（11-09 真实通话 s_32c93/s_a1fd 挖出来的）：
            #   「为什么驳回 / 要补什么材料」是实质问题——核身前不能答，等于把
            #     别人申请的信息讲给还没核验的来电人；但必须**说明为什么先核身份**。
            #     旧实现拿「号码是预留联系方式」把两类问题一起挡回去，用户连问两轮，
            #     客服两轮一字不差，等于把问题当空气。
            #   「你哪来的我号码 / 核验怎么这么麻烦」可以直接解释。
            if any(w in text for w in _RESULT_ASK_MARKS):
                n = int(session.get("identity_result_ask_n", 0)) + 1
                session["identity_result_ask_n"] = n
                # 两个键各自独立的默认文案：别的场景没配这两个键时也不能落成同一句，
                # 否则「不复读」这条在未配场景里立刻失效（自检实测踩到）。
                if n % 2 == 1:
                    return _reply(
                        session,
                        _identity_text(
                            "reply_ask_before_identity",
                            "您问的驳回原因和材料，我得先核对身份才能讲，不然不能把别人"
                            "申请里的信息说给您听。麻烦报一下申请时预留手机号的后四位，"
                            "或者联系人姓名，核对上我马上说给您。"),
                        intent="identity_ask")
                return _reply(
                    session,
                    _identity_text(
                        "reply_ask_before_identity_2",
                        "我知道您着急，但按规定这些得核完身份才能讲，一核对上我就把原因"
                        "和要补的材料说清楚。您报一下预留手机号后四位，或者联系人姓名就行。"),
                    intent="identity_ask")
            n2 = int(session.get("identity_ask_n", 0)) + 1
            session["identity_ask_n"] = n2
            # 循环交替而不是「第一次 A、之后永远 B」：问到第三轮又落回 B 的话，
            # 相邻两轮还是逐字相同（性格集 T10 连问三轮实测复读）。
            if n2 % 2 == 1:
                return _reply(
                    session,
                    _identity_text("reply_ask_identity",
                                   "您问的这个问题我记下了，稍后专员给您详细解答。"
                                   "这边先把身份核对一下，方便说一下手机号后四位吗？"),
                    intent="identity_ask")
            return _reply(
                session,
                _identity_text("reply_ask_identity_2",
                               "这个问题我记下了，专员会跟您解释得更细。咱们先把身份核对"
                               "一下——申请时预留手机号的后四位，四个数字就行。"),
                intent="identity_ask")

        # ②' 客户只报了姓（"我姓晏"）：这不是跑题，是答了一半。
        #     旧流程把它当噪声，落到下面那句"没关系，记不住号码也正常"——
        #     客户刚说了自己姓什么，客服却安慰他"记不住号码"，答非所问。
        #     注意这一段和 `_ask_identity` 开头那条**不是重复**：那一条只在
        #     "本段决定要再问一个槽位"时生效，而这里的情况是本轮什么都没收到、
        #     流程根本不打算再问（直接往跑题承接走）。少这一段，线上那句
        #     "我姓晏"就被当成噪声。
        if session.get("identity_surname"):
            _sn = "name"
            if session["slot_asks"].get(_sn, 0) < SLOT_ASK_MAX:
                return _ask_identity(session, _sn)

        # ③ 跑题 / 絮叨（有实义、既不是提问也不是拒绝）：中性承接 + 拉回话题。
        #
        # 同一句最多说一次。11-11 双端自对练实测：客户连着两轮说"我这会儿真想
        # 不起尾号，单号我也得查后台"，客服两轮**逐字相同**地把"没关系，记不住
        # 号码也正常……说联系人姓名也行"念了一遍，客户耐心掉到 0，
        # 整通卡在 IDENTITY 整整 14 轮没闭环。
        # 第二次必须换一条真有出路的话（稍后回电 / 换人接听），
        # 第三次还没进展就按"核不上"收尾转专员——继续磨只会把客户磨烦。
        _n = int(session.get("identity_offtopic_n", 0)) + 1
        session["identity_offtopic_n"] = _n
        # 三条话术轮换，**不挂断**：C6 的硬要求是"连续三轮噪声不挂断、可恢复"。
        # 第一版这里写成"第三次就 finish(identity_failed)"，直接踩红 C6——
        # 客户只是没报出尾号，不是骗子，挂断是我们的问题。
        # 真正该收尾的是"槽位问满次数仍核不上"，那条路在 _next_identity_slot 后面。
        _off_keys = ("reply_offtopic", "reply_offtopic_2", "reply_offtopic_3")
        _off_defaults = {
            "reply_offtopic": "嗯，您说的我记下了。咱们先把身份核对一下，方便说一下手机号后四位吗？",
            "reply_offtopic_2": "那这样，您查一下当初留的号码或者申请单号，我再给您打一次；"
                                "或者现在请店里另一位同事接听，报一下他的姓名也行。",
            "reply_offtopic_3": "不着急。您也可以直接说公司全名，我按公司名核一遍，"
                                "一样能确认是您这单。",
        }
        _off_key = _off_keys[0] if _n == 1 else _off_keys[1 if _n % 2 == 0 else 2]
        return _reply(session, _identity_text(_off_key, _off_defaults[_off_key]),
                      intent="identity_offtopic")

    session["noise_turns"] = 0              # 人在回答了，噪声累计归零
    session["identity_attempts"] += 1

    # ③ 问最缺的那一个；两个都问满了就收尾
    #    刚报错的那个优先再问一次（同一个值问满 SLOT_ASK_MAX 才换），
    #    与 _stage_greeting 的 took 分支同一口径。
    _wrong = session.get("identity_mismatch") or ""
    if _wrong and session["slot_asks"].get(_wrong, 0) < SLOT_ASK_MAX:
        slot = _wrong
    else:
        slot = _next_identity_slot(session)
    if slot is None:
        # T17-③：挂断前必须**完整重问一次**。真实事故里用户还在正常说话
        # 就被挂断（"人还在说话就挂断"），核不上≠不用再问——很多时候
        # 只是用户没听懂要什么。重问只给一次：这次之后仍核不上才是真的核不上。
        if not session.get("identity_reask"):
            session["identity_reask"] = True
            return _reply(session,
                          _identity_text("reply_reask",
                                         "实在不好意思，这边一直核对不上。麻烦您再报一次：手机号后四位，或者您的姓名，任意一个都可以，我这边再核对一次。"),
                          intent="identity_reask")
        return _finish(session, cfg.get("reply_fail", "这边核对不上，稍后让专员联系您。"),
                       "identity_failed")
    return _ask_identity(session, slot)


# 改口句里要剥掉的前缀。"等等我改一下其实是屏幕碎了" → "屏幕碎了"。
# 只剥前缀不剥中间：中间那些字往往就是新诉求本身的内容。
_CORRECTION_LEAD = ("等一下", "等等", "我改一下", "改一下", "改口", "更正", "重新说",
                    "说错了", "我说错", "其实", "不是", "是", "那个")


def _is_correction(text):
    """这句是不是"回头改诉求"（T9）。"""
    return any(w in (text or "") for w in CORRECTION_MARKS)


def _strip_correction_prefix(text):
    """把改口句的引导语剥掉，只留新的诉求内容。剥完是空的就返回空串。"""
    t = _norm(text)
    moved = True
    while moved and t:
        moved = False
        for w in _CORRECTION_LEAD:
            if t.startswith(w) and len(t) > len(w):
                t = t[len(w):].strip(" \t，,、。！？!?")
                moved = True
                break
    return t


def _apply_need_correction(session, text):
    """把边界改口落进 need 槽位，并重讲一遍方案（T9）。

    为什么必须重新讲方案：诉求从"包装压坏"改成"屏幕碎了"，针对的权益可能不同。
    继续按旧诉求往下推，等于用户说了新问题、客服还在答旧问题——
    对抗模拟 D4 的原话是"等等我改一下其实是屏幕碎了"，客服回的是"您同意领取吗"。
    """
    old = session["slots"].get("need") or ""
    new = _strip_correction_prefix(text)
    if not new or new == old:
        return None
    session["slots"]["need"] = new
    # 只记轨迹，不改 memory.facts["need"]——facts 记的是"用户最初说的什么"，
    # 那是回溯问句的答案来源，被改口覆盖掉就答不出"您一开始提到的是什么"。
    _record_correction(session, "need", old, new)
    offer = config_loader.stage_config("OFFER")
    return _reply(session, offer.get("reply", ""), next_stage="OFFER",
                  intent="corrected_need")


def _confirm_restatement(session, cfg):
    """确认环节的复述内容。

    默认仍是配置里那句通用复述（"因为材料问题被驳回"）。打开
    `confirm_specific` 后改用带 {reason}/{materials} 的版本——11-09 逐轮
    反馈里 A02 第 4 轮就是这么被标出来的：前面已经讲过"授权链路不完整"，
    复述时却降级成"材料问题"，客户听到的是"你根本没在听我这一单"。
    取不到档案时一律回落通用句，绝不为了具体而念出花括号。
    """
    app = session["slots"].get("application")
    if policy.enabled("confirm_specific") and app:
        tpl = cfg.get("reply_specific") or ""
        if tpl:
            return _fill_application(tpl, app)
    return cfg.get("reply", "")


def _repair_reply(session, cfg, stage, flags):
    """客户要求「再说一遍」时的回复：如实复述，而不是回一句「确认不到」。

    两条话术交替使用：同一句连说两次，客户听到的就是卡带（对抗器也会记 repeat）。
    两条都没有配置时返回空串，调用方回落原有分支——开关打开但场景没写话术，
    不能因此让客服哑火。
    """
    n = int(session.get("repair_n", 0))
    session["repair_n"] = n + 1
    keys = ("reply_repair", "reply_repair_2")
    tpl = cfg.get(keys[n % len(keys)]) or cfg.get("reply_repair") or ""
    if not tpl:
        return ""
    app = session["slots"].get("application")
    return _fill_application(_soothe(tpl, flags), app)


def _route_answer(session, cfg, stage, text, flags, tail=""):
    """答不上来时，按「阶段 + 问题类别」的路由表兜底；没学过返回 None。

    为什么不是又一个 if：路由是**学出来的**。自进化闭环从逐轮负反馈里看出
    “确认环节被问补什么材料，客服答确认不到”，把这条路由写进配置；
    下次复跑同一批客户，这条负面反馈就消失。代码只提供一次查表与填槽，
    不预置任何一条路线——预置了就等于把答案写死在代码里，闭环没得学。

    路由值必须指向 `reply_` 开头的话术键（见 policy.ROUTE_KEY_PREFIX）：
    优化器可以改「说什么」，不能借这个口子改 must_say 这类合规结构。
    """
    app = session["slots"].get("application")
    if not app:
        return None
    cls = policy.question_class(text)
    key = policy.answer_route(stage, cls)
    if not key or not cfg.get(key):
        return None
    # 同一类问题连续问两次时换一条说法，避免逐字复读（对抗器会记 repeat）。
    alt = key + "_2"
    n = session.setdefault("route_n", {})
    k = int(n.get(key, 0))
    n[key] = k + 1
    tpl = cfg.get(alt) if (k % 2 and cfg.get(alt)) else cfg.get(key)
    _txt = _fill_application(_soothe(tpl, flags), app)
    if tail:
        _txt = "%s %s" % (_txt, tail)
    return _reply(session, _txt, intent="ask")


def _stage_need(session, text, flags):

    """NEED：先查业务数据，再问诉求。绝不能"没查就问"。

    顺序很关键：进入本阶段先调 query_order 拿真实订单，
    有订单就说订单细节（可溯源），没订单就走通用回访话术。
    """
    cfg = config_loader.stage_config("NEED")
    # 客户说"你不是知道吗""我说过了"——元话语，在说"你在问废话"。
    # 必须先认账，不许落到 reply_unknown 回"这个我确认不到"。
    if _meta_repeat(text):
        return _meta_repeat_reply(session, cfg, flags, stay="NEED")
    app0 = session["slots"].get("application")

    # 只在第一次进入时查，避免每轮都打接口。
    # C5：已经定位到资质申请单时不再查售后订单——否则明明核上了申请单，
    # 却会因为订单库里没有这个商家 ID 而回一句"查不到订单"，自相矛盾。
    if not app0 and session["slots"].get("order") is None and not session["slots"].get("order_queried"):
        session["slots"]["order_queried"] = True
        cid = (session["slots"].get("customer") or {}).get("customer_id")
        if cid:
            res = tools.query_order(cid)
            if res.get("found"):
                session["slots"]["order"] = res["order"]
                # 记成可指代对象：后面用户说"就那个订单"，能落到这一单上
                _note_referent(session, "order", "您的订单")

    # 用户一上来就明确拒绝 → 直接去回写（拒绝也要留痕）
    if flags["no"]:
        session["slots"]["need"] = "用户表示不需要"
        return _do_writeback(session, False)

    # 求助（"不清楚/咋补"）先于含糊处理：这是对「您清楚怎么补吗」的**回答**，
    # 不是没听清——直接给步骤，不给就升级，绝不反问"要不要再讲一遍"。
    if _is_help_seek(session, text):
        return _help_reply(session, "NEED")

    # 客户说"没听清 / 再说一遍 / 听不懂"或"简洁 / 别重复 / 直说"，且事实已讲过：
    # 给**更详细**（rich）或**短版**（short）的解释，而不是把同一句驳回原因
    # 原样再念一遍——念一样的他还是听不清（11-11 截图那通：客户连说"我根本没听清"，
    # 客服把同一段又念了三遍，客户只好说"你别再重复了"）。放在 help_seek 之后、
    # 含糊/情绪/提问之前：真正的"不会操作"（不清楚/咋补）走求助给步骤，
    # 而"没听清/别重复"是上一句没接住，该换讲法而不是换话题。
    _re = _re_explain(session, "NEED", text, flags)
    if _re is not None:
        return _re

    # 客户赶时间（"我时间有点急/长话短说"）且事实已经讲过：给短版，
    # 不把驳回原因整段再念一遍。
    if session.get("facts_spoken") and any(w in text for w in _HURRY_MARKS):
        cfg_h = cfg
        return _reply(session, cfg_h.get("reply_hurry")
                      or "好，长话短说：补{materials}，在商家后台重新提交，"
                         "建议一周内完成就行。",
                      intent="hurry_short")

    # 含糊/敷衍 → 追问，不把"说了等于没说"记成诉求
    if flags.get("vague"):
        return _clarify(session, "NEED")

    # 带情绪（T8）→ 先接住，不推进。注意排在"提问"之前，但只在**不是在提问**时拦：
    # "你们这是骗子服务，那到底收不收费"这句里既有情绪又有真问题，
    # 把问题吞掉换成一句致歉，用户会认为客服在装听不见。
    if flags.get("angry") and not flags["ask"]:
        return _angry_hold(session, "NEED")

    # 只有指代没有内容（"就是那个""你刚说的那个"）→ 同样不能记成诉求。
    # 记下去的结果是工单的 need 字段写着一个"那个"，回写出去等于没写。
    if _is_reference_only(text):
        return _clarify(session, "NEED")

    # 客户拒绝再说身份信息：先松绑，再把话题拉回材料。
    if _refuses_info(text):
        _ri = _refuses_info_reply(session, cfg, flags)
        if _ri:
            return _ri

    # 客户说"单号记不住"：这不是提问，是求一条不用单号的路。
    if _cant_recall_order(text):
        _cr = _cant_recall_reply(session, cfg, flags)
        if _cr:
            return _cr

    # 客户说的是一堵墙（后台打不开、不会弄），不是知识问题：
    # 先接住墙，再谈材料。排在提问之前，因为「打不开」常和提问同句出现。
    _blk = _blocked_ui_reply(session, text, cfg, flags)
    if _blk:
        return _blk

    # 用户在提问 → 官方 QA 原话 / 查到的事实回答，仍停在本阶段
    if flags["ask"]:
        # 求结果保证（合规红线）在 NEED 阶段同样先拦，见 _promise_block。
        blocked = _promise_block(session, text, flags)
        if blocked:
            return blocked
        # 这里的顺序是「具体问题优先于通用事实」：
        # "在职证明有哪些"（官方 QA 能逐字答）必须先于"您这笔申请是…驳回原因是…"，
        # 否则用户问的是 A，听到的是 B——11-07 那批分诊里 20/24 条挂断都是这个形状。
        _ans, _qa_hit, _kb_hit = _answer_official(session, text, flags)
        if _ans:
            return _reply(session, _ans,
                          intent="qa" if _qa_hit else "ask",
                          qa_hit=_qa_hit, kb_hit=_kb_hit)
        app = session["slots"].get("application")
        if app:
            _off = _official_line(session)
            if _off:
                # 官方原话说一次就够。第二遍再逐字复读，客户只会听到卡带：
                # 11-08 性格跑批 T10 在 NEED 连听三遍同一句，第 9 轮直接挂断。
                if session.get("official_line_spoken"):
                    # 但"催一句清楚了"也不对。11-10 实测 A11/A16 第 4 轮：客户
                    # 第二次追问「那我具体要补什么材料」，客服换了「您看这样解释
                    # 清楚了吗？」并推进到确认——客户根本没拿到材料清单，也只好
                    # 回一句「清楚了」，电话闭环了，问题一个字没答。
                    # 客户再问，说明上一次没讲清（官方话术只说「补齐后重新提交」，
                    # 不列要补哪几样）。正确动作是**按问题类别给本单事实**，
                    # 两种说法轮换；只有确实没有对应话术时才退回旧的催确认。
                    _cls = policy.question_class(text)
                    _key = {"material": "reply_application_detail",
                            "reason": "reply_application_detail",
                            "provenance": "reply_provenance",
                            "fee": "reply_ask_fee",
                            "validity": "reply_ask_validity"}.get(_cls, "")
                    _n = int(session.get("need_detail_n", 0)) + 1
                    _alt = (cfg.get(_key + "_2") or "") if (_key and _n % 2 == 0) else ""
                    _tpl = _alt or ((cfg.get(_key) or "") if _key else "")
                    if _tpl:
                        session["need_detail_n"] = _n
                        return _reply(session,
                                      _fill_application(_soothe(_tpl, flags), app),
                                      intent="ask")
                    # 追问「补齐到哪」这一类已由 respond 层的 _scope 拦截正面答过，
                    # 能走到这里的都是本单事实也答不上的问题，仍按原路径回到确认；
                    # 性格跑批 T05（听不清的老师傅）的复述→闭环链依赖这一支。
                    _cc = config_loader.stage_config("CONFIRM")
                    _after = _cc.get("reply_after_ask") or "您看这样解释清楚了吗？"
                    return _reply(session, _soothe(_after, flags),
                                  next_stage="CONFIRM", intent="confirm_after_ask")
                session["official_line_spoken"] = True
                _line = _soothe(_off, flags)
                # 客户问「这几步都要弄吗」时，官方原话只说到"补齐后重新提交"，
                # 没正面回答"是不是都要做"。原话逐字保留，后面补一句本单事实，
                # 别让客户以为可以挑一步做（11-12 截图第 4 张）。
                if _is_scope_question(session, text, flags) == "scope":
                    _line += "。这几步都要走完，材料一样都不能少。"
                return _reply(session, _line, intent="official_script")
            # 客户第二次追问同一件事时换一条说法，不要一字不差再念一遍清单。
            # 11-11 双端自对练实测：客户连问两轮"具体要哪几样 / 找哪家机构"，
            # 客服两轮逐字相同地把"您这笔申请是…需要补充…"念了一遍，判 2 次复读。
            # 换说法不解决"答不到细节"（那要数据里有对应说明），但至少不再像卡带。
            _n = int(session.get("need_detail_n", 0)) + 1
            session["need_detail_n"] = _n
            _tpl = cfg.get(
                "reply_application_detail",
                "您这笔申请的驳回原因和需补材料我这边都有记录，我再说一遍："
                "{reason}，需要补充{materials}。")
            if _n % 2 == 0:
                _tpl = cfg.get("reply_application_detail_2") or _tpl
            return _reply(session, _fill_application(_soothe(_tpl, flags), app),
                          intent="ask")
        order = session["slots"].get("order")
        if order:
            tpl = cfg.get("reply_order_detail", "")
            return _reply(session, _soothe(tpl.format(amount=order.get("amount"),
                                                     status=order.get("status")), flags),
                          intent="ask")
        return _reply(session, _soothe(cfg.get("reply_no_order", cfg.get("reply", "")), flags),
                      intent="ask")

    # 正常表达诉求 → 记录并进入方案沟通
    session["slots"]["need"] = text
    offer = config_loader.stage_config("OFFER")
    return _reply(session,
                  _fill_application(_soothe(offer.get("reply", ""), flags),
                                    session["slots"].get("application")),
                  next_stage="OFFER", intent="need")


#: 客户在问"到底怎么补"。这是本环节的**基本问题**，答案就在本单档案里。
_HOW_TO_SUBMIT_MARKS = ("怎么补", "如何补", "怎么提交", "怎么上传", "在哪提交",
                        "哪里提交", "哪个入口", "在哪儿传", "怎么弄", "补哪些",
                        "补什么", "要补什么", "补哪几样", "怎么重新提交")


def _asks_how_to_submit(text):
    return any(w in (text or "") for w in _HOW_TO_SUBMIT_MARKS)


def _how_to_submit_reply(session, cfg, flags):
    """把本单的驳回原因、要补哪几样、去哪儿提交，一次说清。

    11-11 用户截图那通：客户连着问「怎么补」「你不是知道吗？」，
    客服回「这个我这边确认不到，稍后让专员给您回电」——这两句话问的
    恰恰是本环节唯一该答的事，而且答案就写在本单档案的
    `reject_reason` / `required_materials` 里。答不上来不是"库里没有"，
    是**把档案当成别人的事**。
    """
    app = _app_of(session)
    reason = str(app.get("reject_reason") or "").strip()
    mats = [str(m) for m in (app.get("required_materials") or []) if m]
    if not reason and not mats:
        return None
    _def = ("驳回原因是%s，要补的是%s。您在商家后台的资质审核页重新提交就行，"
            "不用跑线下。")
    tpl = cfg.get("reply_how_to_submit") or _def
    try:
        line = tpl % (reason or "材料不符合要求", "、".join(mats) or "对应材料")
    except Exception:
        line = _def % (reason or "材料不符合要求", "、".join(mats) or "对应材料")
    return _reply(session, _soothe(line, flags), intent="ask")


def _stage_offer(session, text, flags):
    """OFFER：讲清方案与权益。

    用户提问时走两段式：先查 `modules/kb.py`（命中就带出处播报），
    查不到才用配置里的固定话术 / `reply_unknown`。这样"答得上来"的范围
    由知识条目决定，而不是由代码里的 if 分支决定——加一类问题只改 json，不改代码。

    _todo 权益内容必须来自企业资料包。现在 `config/kb.json` 里是**占位内容**
    （`_meta.placeholder = true`），官方资料包公布后要逐条替换 answer 与 source_id，
    替换完把 placeholder 改成 false。内容是否替换过，`scripts/kb_check.py` 会提示。
    """
    cfg = config_loader.stage_config("OFFER")
    if _meta_repeat(text):
        return _meta_repeat_reply(session, cfg, flags, stay="OFFER")
    # 进了 OFFER 就说明"这份权益"已经被提过一次，后面所有"那个""这个"都能落到它身上
    _note_referent(session, "benefit", "这份权益")

    # 求助先于含糊：客户答"不清楚"是在说"我不会操作"，不是没听清。
    # 11-11 线上实录：客服问"这一步您清楚怎么补吗"，客户答"不清楚"，
    # 旧逻辑走含糊梯子，反问"您是想让我再讲一遍吗"，白耗一轮。
    if _is_help_seek(session, text):
        return _help_reply(session, "OFFER")

    # 客户说"没听清 / 再说一遍 / 听不懂"或"简洁 / 别重复 / 直说"，且事实已讲过：
    # 给更详细（rich）或短版（short）的解释，而不是把同一句驳回原因原样再念一遍。
    _re = _re_explain(session, "OFFER", text, flags)
    if _re is not None:
        return _re

    # 赶时间且事实已讲过 → 短版，不整段复述。
    if session.get("facts_spoken") and any(w in text for w in _HURRY_MARKS):
        return _reply(session, cfg.get("reply_hurry")
                      or "好，长话短说：补{materials}，在商家后台重新提交，"
                         "建议一周内完成就行。",
                      intent="hurry_short")

    # 含糊/敷衍先判。这里尤其危险：用户"嗯……"一声就被推进 CONFIRM，
    # 下一轮客服问"您同意吗"，用户会觉得刚说过的话没被听见。
    # 放在提问分支前面，是因为"啊？"这类只有语气词的输入虽然带问号，
    # 但语义是"我没听清"，不是"我要问一个具体问题"。
    if flags.get("vague"):
        return _clarify(session, "OFFER")

    # 改口（T9）：用户听过方案之后回头改诉求。
    # 必须先更新 need 再重讲方案，否则客服会一直按旧诉求推销——
    # "等等我改一下其实是屏幕碎了"以前被判 unknown，然后问"您同意领取吗"。
    if _is_correction(text):
        corrected = _apply_need_correction(session, text)
        if corrected:
            return corrected

    # 带情绪（T8）→ 致歉承接、不推 CONFIRM。
    # 这里以前是直接问"您同意领取这份权益吗"，用户刚骂完就收到一句要同意的话。
    if flags.get("angry") and not flags["ask"]:
        return _angry_hold(session, "OFFER")

    # 指代 → 落到具体对象上回答（T6），而不是推进确认。
    # 解析不了才退回追问。
    if _looks_like_reference(text, flags):
        kind, label = _resolve_reference(session, text)
        if kind:
            return _answer_reference(session, kind, label)
        if _is_reference_only(text):
            return _clarify(session, "OFFER")

    # 修复请求（"信号不好，你再说一遍"）先于提问处理。
    # 11-09 逐轮反馈：这句话被 classify 判成 ask（"说一遍"里带提问信号），
    # 于是走了下面的知识库分支，没命中就回"这个我这边确认不到"，
    # 客户只好一遍遍重复要求，客服一遍遍说确认不到（A01 第 4~7 轮、A04 第 7 轮）。
    # 正确动作是把刚才讲过的内容再说一遍——修复请求要的是复述，不是答案。
    #
    # 两个守卫（缺一个就会把别的路径搞坏）：
    #   1. 明确拒绝/挂断优先："没听清，不要了"要的是挂断，不是再听一遍；
    #      不加守卫时"我不是说了吗，不要了"会被复述分支拦走，整个会话不回写、
    #      继续留在 OFFER，该记的"拒绝"没记。
    #   2. 同一句里带实义提问不要抢："啊？我没听清，那到底要补什么"问的是材料，
    #      复述整段驳回原因等于不回答（打开开关前实测 no_answer 3→5）。
    if (policy.repair_enabled("OFFER") and policy.is_repair_request(text)
            and not (flags.get("no") or flags.get("deny") or flags.get("hangup"))
            and not policy.question_class(text)):
        _rep = _repair_reply(session, cfg, "OFFER", flags)
        if _rep:
            return _reply(session, _rep, intent="repair")

    # 拒绝再提供身份信息（同 NEED）。
    if _refuses_info(text):
        _ri = _refuses_info_reply(session, cfg, flags)
        if _ri:
            return _ri

    # 单号记不住（同上，NEED 里的说明）。
    if _cant_recall_order(text):
        _cr = _cant_recall_reply(session, cfg, flags)
        if _cr:
            return _cr

    # 后台打不开 / 不会弄：先接住墙（同上，NEED 里的说明）。
    _blk = _blocked_ui_reply(session, text, cfg, flags)
    if _blk:
        return _blk

    # 提问：官方 QA 原话 → 知识条目（带出处）→ 配置里的固定话术
    if flags["ask"]:
        # 先判一件事：客户是不是把**已经核过的信息**又报了一遍，然后反问
        # "这不都对得上吗"。
        #
        # 11-11 线上自对练实测（这一条是线上跑出来的，本地没复现）：
        #   客户：单号 QZ202611060002，9月4号交的，这不都对得上吗？
        #   客服：单号和时间我这边确认不到，稍后让专员给您回电说明，可以吗？
        #   客户：（又说一遍）
        #   客服：（逐字相同再说一遍）
        # 客户给的是这张单的真实单号，系统里明明有；回"确认不到"等于把他刚
        # 递上的证据扔回去，而且同一句说两遍。这不是知识问题，是没接住。
        # 正确动作：接住（单号对得上）→ 讲原因与材料（本来就在 slots 里）
        # → 第二次起直接推进确认，不再念第三遍。
        _echo = _echoed_slot(session, text)
        if _echo:
            app = session["slots"].get("application")
            if app:
                _n = int(session.get("echo_slot_n", 0)) + 1
                session["echo_slot_n"] = _n
                # 报的是哪一项就说哪一项。客户说"单号记不住、只记得尾号4656"时，
                # 回"您报的单号我这边对得上"是把他刚否认的事当成确认——
                # 线上实测发生过，客户后面每一轮都建在这个错前提上。
                _by_id = (_echo == "application_id")
                _ok_key = "reply_confirm_slot_ok" if _by_id else "reply_confirm_tail_ok"
                _ok2_key = "reply_confirm_slot_ok_2" if _by_id else "reply_confirm_tail_ok_2"
                _ok_def = ("您报的单号我这边对得上，就是这笔：{application_desc}。"
                           "驳回原因是{reason}，需要补充{materials}。") if _by_id else (
                           "尾号我这边对得上，就是这笔：{application_desc}。"
                           "单号不用您找了，我按尾号和公司名认的单。"
                           "驳回原因是{reason}，需要补充{materials}。")
                _ok2_def = ("对，单号没错，就是这笔。您按刚才说的把{materials}补齐、"
                            "在后台重新提交就行，我这边会盯着结果。") if _by_id else (
                            "尾号没错，就是这笔。单号不用您报，我这边已经认下了。"
                            "您按刚才说的把{materials}补齐、在后台重新提交就行。")
                if _n == 1:
                    return _reply(session, _fill_application(_soothe(
                        cfg.get(_ok_key, _ok_def), flags), app), intent="ask")
                return _reply(session, _fill_application(_soothe(
                    cfg.get(_ok2_key, _ok2_def), flags), app),
                    next_stage="CONFIRM", intent="ask")
        # 求结果保证（框架合规红线）必须先于知识库拦截，见 _promise_block。
        blocked = _promise_block(session, text, flags)
        if blocked:
            return blocked
        _ans, _qa_hit, _kb_hit = _answer_official(session, text, flags)
        if _ans:
            # 官方原话用 intent="qa"：_reply 会因此冻结措辞层，保证逐字可复述。
            # 知识条目仍走 intent="ask"（我们自己的口径，允许改写得更口语）。
            return _reply(session, _ans,
                          intent="qa" if _qa_hit else "ask",
                          qa_hit=_qa_hit, kb_hit=_kb_hit)
        # 老路径：knowledge 文件还没落位（或问题确实不在库里）时行为完全不变。
        # 保留它不是为了兼容，是因为「答不上来的问题」和「库里没写的问题」
        # 必须落到同一句 reply_unknown，不能让两种原因产生两种说法。
        if any(w in text for w in config_loader.answer_keywords("fee")):
            return _reply(session, _soothe(cfg.get("reply_ask_fee", ""), flags), intent="ask")
        if any(w in text for w in config_loader.answer_keywords("validity")):
            return _reply(session, _soothe(cfg.get("reply_ask_validity", ""), flags), intent="ask")
        # 答不上来的问题：先查学到的路由表（阶段 + 问题类别 → 话术键）。
        # 11-09 逐轮反馈：A14/A06 在 OFFER 被问“补什么材料/为什么驳回”，
        # 客服回“确认不到”，而这单的原因与材料就在 slots.application 里。
        # 路由表默认空 = 行为不变；自进化闭环依据反馈逐条学出来。
        _routed = _route_answer(session, cfg, "OFFER", text, flags)
        if _routed:
            return _routed
        # 「怎么补」是本环节的基本问题，档案里就有答案，不许落到「确认不到」。
        if _asks_how_to_submit(text):
            _how = _how_to_submit_reply(session, cfg, flags)
            if _how:
                return _how
        return _reply(session, _soothe(cfg.get("reply_unknown", ""), flags), intent="ask")

    # 明确拒绝 → 回写（拒绝也留痕）
    if flags["no"]:
        return _do_writeback(session, False)

    # 说了方案之后必须走确认，不能默认同意。
    # T13 复核（11-06）：曾尝试在这里对「明确肯定」直接回写以让 N206 四轮闭环，
    # 实测撞红 verify 证据 5 与 test_correction 共 5 条断言——CONFIRM 的
    # must_say「确认」是合规必答要素，跳过它等于为提分削合规，已撤回。
    # N206 的真实根因是用例轮数与规格不匹配（见 seed_cases.json N206._note），
    # 不是意图判定缺陷；T10 否定作用域对「不要钱啊，那办吧」判的是 yes，正确。
    confirm = config_loader.stage_config("CONFIRM")
    return _reply(session, _soothe(_confirm_restatement(session, confirm), flags),
                  next_stage="CONFIRM", intent=_intent_name(flags))


#: 核身前用户在问「结果/材料」：这类问题不能答（会把别人申请的信息讲给未核验的
#: 来电人），但必须说明为什么先核身份。真实通话 s_32c93/s_a1fd 里，用户连问
#: 「为什么会驳回」「那我要补什么材料」，客服两轮都回同一句「号码是预留联系方式」。
_RESULT_ASK_MARKS = ("为什么", "为啥", "驳回", "原因", "补什么", "材料", "怎么处理",
                     "怎么改", "整改", "什么情况", "没过", "没通过")


#: 说"我记不住"的信号。和报信息互斥：客户说"单号我真的记不住"时，
#: 句子里可能还带着尾号（"就记得尾号 4656"），那是**没报单号**的证据，不是报单号。
_NO_RECALL_MARKS = ("记不住", "想不起", "记不清", "忘了", "没记住", "没存", "没带", "记不得")


def _echoed_slot(session, text):
    """客户这一句里，把**本单已核过的哪一项**又报了一遍。

    返回 "application_id" / "phone_tail" / ""。

    为什么要分清是哪一项：11-11 线上实测，客户说
    "单号？我真记不住，号码 4656 的手机上也没存！"——只报了尾号、
    还明说了记不住单号，客服却回"您报的**单号**我这边对得上"。
    客户刚否认的事被当成确认，后面整通都建在这个错误前提上。

    判据两处收紧：
        · 命中"记不住/没存"等词 → 这一句不是在报信息，返回空；
        · 分开判单号与尾号，调用方按项选话术。
    姓名不查——"我是张三"和"我叫张三是吧"太容易混进普通问句。
    """
    t = text or ""
    if any(w in t for w in _NO_RECALL_MARKS):
        return ""
    app = _app_of(session) or {}
    v = str(app.get("application_id") or "")
    if len(v) >= 6 and (v in t or v[-6:] in t):
        return "application_id"
    v = str(app.get("phone_tail") or "")
    if len(v) >= 4 and (v in t or v[-4:] in t):
        return "phone_tail"
    return ""


#: 「打错人了」的判据。比 intent 的 DENY_PERSON 窄：那张表给开场用
#: （"我不是"就是拒接），这里用在**已经核过身**之后，误判的代价是把
#: 一通正常电话直接挂掉，所以"打错"必须同时出现人/电话/号码/号。
_DENY_ID_MARKS = ("打错", "没这个人", "没有这个人", "不是本人", "你找错",
                  "找错了人", "不是他", "他不在", "不认识这人")


def _denies_identity(text):
    """客户是不是在说"你们打错人了"。"""
    t = text or ""
    for m in _DENY_ID_MARKS:
        if m in t:
            if m == "打错" and not any(k in t for k in ("人", "电话", "号码", "号")):
                continue                     # "你们打错字了"不是打错人
            return True
    return False


#: 「后台用不了」的信号。客户说的不是知识问题，是一堵墙：
#: 再给他念一遍材料清单，等于对着打不开页面的人念清单。
_BLOCKED_UI_MARKS = ("打不开", "进不去", "登录不了", "上传不了", "传不上去",
                     "卡住", "找不到入口", "不会弄", "提交不了", "页面没反应",
                     # 11-11 线上 A15 第 11 轮：「后台卡得我连补传页面都刷不出来，
                     # 店里还一堆单等着发货呢！」——同一个意思，词表只收「打不开」，
                     # 这句一个字都没命中，客服继续问「清楚了吗」。客户在说一堵墙，
                     # 词表却只认墙上那一块砖。
                     "刷不出来", "刷不开", "刷新不了", "加载不出", "打不开页面",
                     "显示不出来", "白屏", "转圈",
                     # 11-11 线上：客户说「后台卡得我连订单都发不出去，
                     # 哪还有空找这些纸？」——"卡得"不在表里（只有"卡住"），
                     # "发不出去"也不在，于是没走障碍承接，落到"确认不到"兜底。
                     # 客户在说一堵墙，词表又一次只认墙上那一块砖。
                     "卡得", "卡到", "卡死", "发不出去", "上传不了", "传不上")


def _blocked_ui(text):
    return any(w in (text or "") for w in _BLOCKED_UI_MARKS)


#: 拒绝再提供身份信息。已经核过身之后客户说这个，是在"怕你再要"，不是在提问。
_REFUSE_MARKS = ("不说了", "不提供", "不告诉你", "凭什么", "不想说", "不给",
                 "别再问", "不聊这个")
_REFUSE_TOPICS = ("身份", "信息", "号码", "资料", "身份证", "证件")


def _refuses_info(text):
    t = text or ""
    return (any(w in t for w in _REFUSE_MARKS)
            and any(w in t for w in _REFUSE_TOPICS))


def _refuses_info_reply(session, cfg, flags):
    """客户不肯再说身份信息：告诉他"不用再说了"，把话题拉回材料。"""
    app = session["slots"].get("application")
    if not app:
        return None
    tpl = cfg.get("reply_no_more_info") or (
        "不用您再提供什么了，身份我这边已经核对过。"
        "您只要把{materials}补齐、在后台重新提交就行。")
    return _reply(session, _fill_application(_soothe(tpl, flags), app), intent="ask")


def _cant_recall_order(text):
    """客户在说"单号我记不住 / 找不到"。

    这种话里常带问号（"单号？我真记不住"），意图识别会判成提问；
    知识库没有这条，于是落到 reply_unknown——客户听到"这个我这边确认不到"，
    可他要的其实只是"不用单号行不行"。11-11 线上实录连踩四轮。
    """
    t = text or ""
    if not any(w in t for w in ("单号", "申请单", "编号")):
        return False
    return any(w in t for w in _NO_RECALL_MARKS + ("找不到", "查不到", "没找到"))


def _cant_recall_reply(session, cfg, flags):
    """把"单号记不住"接住：告诉他不用单号也能办，并把材料摆清楚。"""
    app = session["slots"].get("application")
    if not app:
        return None
    n = int(session.get("cant_order_n", 0)) + 1
    session["cant_order_n"] = n
    if n == 1:
        tpl = cfg.get("reply_no_order_needed") or (
            "单号不用您找了——尾号和公司名我这边已经核对过，就是这笔："
            "{application_desc}。驳回原因是{reason}，需要补充{materials}。")
    else:
        tpl = cfg.get("reply_no_order_needed_2") or (
            "真的不用单号，我按公司名和尾号认的单，错不了。"
            "您只要把{materials}补齐、在后台重新提交就行。")
    return _reply(session, _fill_application(_soothe(tpl, flags), app), intent="ask")


def _blocked_ui_reply(session, text, cfg, flags):
    """客户说"后台打不开 / 不会弄"：给一句能落地的，两次之后转专员。

    11-11 实测：客户说"后台审核我看材料页面都打不开，店里还一堆事等着呢"，
    客服把材料清单又念了一遍；下一轮还是这句，客服把整段驳回原因再念一遍。
    对着一堵墙念清单，是这个系统最"瞎"的一种表现。

    这里**不报具体入口路径**：那是产品事实，档案与知识库里没有就不能编
    （docs/28 的实体闸）。给的是不依赖内部页面的建议：先准备材料、
    换浏览器再试、把问题登记给专员。
    """
    if not _blocked_ui(text):
        return None
    n = int(session.get("blocked_ui_n", 0)) + 1
    session["blocked_ui_n"] = n
    if n == 1:
        # 客户在说一堵墙，不是在配合核身：这一轮不许再挂「您也可以对照单号核实」
        # 这类要信息型的后缀（11-11 线上 A15 第 11 轮就挂了，读起来像没听见）。
        session["fact_ack_turn"] = True
        return _reply(session, _soothe(cfg.get("reply_blocked_ui") or
                      "页面打不开先别折腾：清单我这边能同步到您后台，您先把材料准备齐；"
                      "换一个浏览器再试一次，通常就能进。", flags), intent="ask")
    # 第二次仍打不开：系统这边确实解决不了，再问「清楚了吗」就是空转。
    # 上一版这里说「我这条记下『后台打不开』，专员联系您时一并处理」，
    # 而实际**一张记录都没写**——只有第三次收线时才有 end_reason 可查。
    # 说了要做却没做，和「没得到答案却说知道了」是同一类毛病：都是嘴上
    # 比手上走得快。改成如实说 + 真收线：end_reason="blocked_ui" 就是
    # 那条「记下」，业务侧据此派单，比一句空承诺可查得多。
    return _finish(session, cfg.get("reply_blocked_ui_handoff") or
                   "后台的问题我这边确实处理不了，也不占用您时间了：材料您先备着，"
                   "我把这情况交给专员，稍后用官方号码联系您一起看，再见。",
                   "blocked_ui")

# 「客户还在谈」的信号：提问、质疑来电或费用、重复报已给过的核身信息。
# 这些都不是表态。旧逻辑把它们一并算成「含糊」，追问两次就回写 unknown——
# 客户还在问「你哪来的我号码」，工单却已经记了「不确定」并收线
# （11-08 对抗集 A03/A06/A07/A10 逐轮复现）。
_ENGAGED_MARKS = ("凭什么", "你谁", "哪来的", "怎么信", "别是", "推销",
                  "补什么", "怎么弄", "去哪", "第几遍",
                  "问下", "想问", "先问", "问一句", "我问问", "咨询一下")

#: 中文口语里"我要问个事"常常不带问号。「等等，我先问下收费」就是提问，
#: 但意图层靠问号与「吗/呢」识别，判成陈述 → 计入含糊 → 两次就收线。
#: 这张表只收**明确说要问**的动词短语，不放「吗/吧/啊」这类语气词：
#: 后者会把「可以吧」「行啊」这些表态句全变成提问，闸门就拆了。
_QUESTION_MARKS = ("问下", "想问", "先问", "问一句", "我问问", "咨询一下", "请问")


def _self_repeated(session, text):
    """客户是不是把上一句几乎原样又说了一遍。

    这不是「含糊表态」，是**客服上一句没接住**。11-11 课程 Gen3 消耗战：
    客户把「少拿这些破权益堵我嘴」连说四遍，旧逻辑逐轮计入含糊，
    第三轮转专员收线，后面那句「那就按你说的办，同意」直接被挂断
    （Gen3 8/12）。真人不会逐字复读，但一旦复读，正确反应是换个说法
    承接，不是把「还在抱怨」统计成「没表态」。

    长度下限 4 是刻意的：「嗯」「再说吧」这类**真含糊**单字/短句不进这条，
    否则连续「嗯嗯嗯」就永远触发不了转专员，闸门反而被拆了。
    """
    prev = (session.get("prev_user_text") or "").strip()
    cur = (text or "").strip()
    if len(prev) < 4 or len(cur) < 4:
        return False
    return difflib.SequenceMatcher(None, prev, cur).ratio() >= 0.85


def _self_repeat_reply(session, cfg, flags):
    """客户复读时换一种说法承接，留在 CONFIRM，不计任何含糊次数。"""
    lines = [
        cfg.get("reply_self_repeat")
        or "嗯，这句我听清了，不重复问您了。这个不是必须现在定，"
           "您什么时候想好了回我一句就行。",
        cfg.get("reply_self_repeat_2")
        or "我明白了，先不催您。您要是还有别的疑问，直接说，我一条一条答。",
        cfg.get("reply_self_repeat_3")
        or "收到，那我这边先不多说。您看还有哪点没讲清楚？",
    ]
    k = int(session.get("self_repeat_n", 0))
    session["self_repeat_n"] = k + 1
    return _reply(session, _soothe(lines[k % len(lines)], flags),
                  next_stage="CONFIRM", intent="ambiguous")


def _reset_ambiguity(session, also_engaged=False):
    """把「连续含糊」的观察窗口重开。

    confirm_asks 的语义是**连续**两轮含糊，不是一通电话里累计两次。
    客户中间问了问题、报了信息、说没听清、说后台打不开，都是实质互动；
    隔着这些轮次攒够两次就收线，等于客户明明还在谈就替他结束通话
    （11-11 回归 L02/L07：客户后面还有「同意」「算了还是不要了」没说出口）。
    """
    session["confirm_asks"] = 0
    if also_engaged:
        session["confirm_engaged"] = 0


def _still_engaged(session, text, flags):
    """客户这一轮还在主动沟通（而不是含糊表态）？只看字面，不猜意图。

    返回 ""（含糊）或原因分类：
        "fact" —— 在提供/重报实质信息（数字、已给过的尾号姓名、重申身份），
                  客服应当**接住**再继续确认；
        "talk" —— 在提问或质疑，客服应当先回应。
    调用方按分类给不同回复；空串才走「含糊计数」。
    """
    # 已经明确表态就不能算「还在谈」。「哦免费啊，那还是要吧」里带「免费」，
    # 旧词表把它当成质疑，转专员收线，整通电话一张工单都没有（L07 实测）。
    if flags.get("yes") or flags.get("no"):
        return ""
    # 判据顺序：**先看是不是在报本单事实，再看是不是在提问**。
    # 「田思远，记住了没？」带问号，旧顺序直接判成 talk，客服回
    # 「这个我这边确认不到」——客户明明在报姓名（slot_history 里就有），
    # 却被当成问了个答不上的知识题（11-11 线上 A15 第 7 轮）。
    # 挪到后面不会放过真质疑：「你哪来的我号码」不含任何档案值，
    # 走到下面仍是 talk。
    # 客户重申身份（"对，是我，你说"）：这是在推进核验，不是对确认问题的表态。
    # 旧判据把它计入含糊，隔着实质回合攒满两次就回写 unknown——客户明明
    # 还在积极配合（11-08 对抗集 A15 实测）。否定变体（"不是我"）必须先拦。
    i = text.find("是我")
    if i >= 0 and not any(neg in text[max(0, i - 2):i] for neg in ("不", "别", "没")):
        return "fact"
    # 反复报已经给过的尾号/姓名：客服没接住，不是客户没表态。
    # 用 slot_history 而不是当前值：核不上时当前值会被清空，历史不会。
    for values in (session.get("slot_history") or {}).values():
        for v in values or []:
            if len(str(v)) >= 2 and str(v) in text:
                return "fact"
    # 客户在报一串数字（尾号 4 位起、单号、完整号码）：这是在主动提供新信息，
    # 更不是含糊。旧判据只认「重新报已给过的值」，客户给一个**新**单号
    # （如对抗集 A15 在确认阶段报申请单号）就落不进来，两次之后被当成
    # 含糊回写 unknown 工单——客户明明还在积极配合（11-08 A15 实测）。
    if re.search(r"\d{4}", text):
        return "fact"
    if flags.get("ask"):
        return "talk"
    if any(w in text for w in _ENGAGED_MARKS):
        return "talk"
    return ""


#: 客户在**指客服重复**，不是在问新问题。
#: 11-11 线上 A15 第 9 轮：「我刚才不是说过了吗？」——这是元话语，
#: 客户在说"你刚才没听"。旧逻辑判不出提问/质疑，落到 engaged="talk" 的兜底，
#: 回「我这边确认不到，稍后让专员给您回电」——客户明明在抱怨重复，
#: 客服答"我确认不到"，等于把「你没听」听成了「你答不上」。
_META_REPEAT_MARKS = ("我刚才不是说了", "我刚不是说了", "刚才不是说过了",
                      "我刚说过了", "我说过了", "不是说了吗", "你刚问过了",
                      "又问一遍", "怎么又问", "重复问",
                      # 11-11 线上 A15 第 14 轮：「QZ202611060019，这号我早报过
                      # 三遍了！」——带具体遍数，是最明确的一种"你重复问"。
                      # 只收"报过/说过/问过 + 遍数"，不收光秃秃的"多少遍"
                      # （"这要等多少遍"不是这个意思）。
                      "报过三遍", "报过几遍", "说过三遍", "说过几遍",
                      "问过几遍", "问了三遍", "说了三遍",
                      # 11-11 线上复跑：NEED 里客户问完"怎么补"，客服答了，
                      # 客户接着说「你不是知道吗？」——意思是"这些你本来就该知道"，
                      # 旧词表没有这一条，落到 reply_unknown 回
                      # 「您说的这个我确认不到，稍后让专员给您回电」。
                      # 客户在说"你在问废话"，客服答"我不知道"，最刺人的一种答错。
                      "你不是知道", "你不知道吗", "你们不是知道", "还用问")


def _meta_repeat(text):
    return any(w in (text or "") for w in _META_REPEAT_MARKS)


def _meta_repeat_reply(session, cfg, flags, stay=None):
    """客户指出客服重复问：先认账，再**停止追问**。不许回「确认不到」。

    不能再挂「您也可以对照申请单编号核实」这类要信息型后缀——客户刚说
    "我说过了"，客服转头又要一遍单号，等于当场打自己脸。线上 A15 第 8 轮
    就挂了这条后缀，是这一轮逐句读出来的。
    """
    session["fact_ack_turn"] = True
    n = int(session.get("meta_repeat_n", 0)) + 1
    session["meta_repeat_n"] = n
    lines = [
        cfg.get("reply_meta_repeat")
        or "是我刚才没接住，抱歉。您说的我记下了，不重复问了。"
           "这几样材料您照着准备就行，哪一步卡住直接说。",
        cfg.get("reply_meta_repeat_2")
        or "确实问重了，我不问了。材料清单后台都能看到，您照着弄；"
           "有卡住的地方，您说一句我接着。",
    ]
    # stay：NEED/OFFER 里认账时不能顺手把阶段推到 CONFIRM——
    # 客户只是说"你在问废话"，事还没讲完，推到确认等于跳过解释。
    return _reply(session, _soothe(lines[(n - 1) % len(lines)], flags),
                  next_stage=stay or "CONFIRM", intent="ambiguous")


def _fact_ack_reply(session, text, cfg, flags):
    """客户在 CONFIRM 里报已核过的信息：先**接住他给的那个值**，再决定追不追问。

    11-11 线上 A15 第 5–13 轮：客户说「是我」「尾号9403，没错」
    「QZ202611060019，这单号我早背熟了」，客服四次回同一句
    「您简单回我一句清楚了或者还有疑问」——客户递了四次实质信息，
    一次都没被确认，九轮停在同一句话上。这不算「客户不表态」，
    是客服没接住：客户报什么都等于没听见。

    第一版骨架是「好的，收到。咱们回到正题，您可以简单回我一句清楚了」，
    经改写层压缩后只剩最后那半句追问，「收到」整段没了（线上第 9/10/12 轮
    实际播的就是「清楚了，还有其他疑问吗？」）。所以承接**必须带客户刚给的
    具体值**：改写时丢了就是事实错误，由 nlg 的数字守卫挡住。

    唯一的例外是**姓名**：客户只说「是我」时，姓名是我们档案里的资料，
    不是他刚给的值。拿它回一句「对，是您本人，马涛」，等于把联系人姓名
    念给一个还没确认是谁的人听——接电话的可能是同事、前台。所以只有
    客户自己把这名字说出来了，才复述；否则就停在「对，是您本人。」

    第 3 次起不再追问表态：问了三次还问不出答案，说明问法不对，再问就是逼。
    改成把清单落在后台、给一条退路，**仍然一张工单都不写**——
    客户没确认过，不能替他记「已沟通」，也不能替他记「拒绝」。
    """
    n = int(session.get("confirm_fact_n", 0)) + 1
    session["confirm_fact_n"] = n

    order = re.search(r"[A-Za-z]{2,4}\d{6,}", text or "")
    tail4 = re.search(r"(?<!\d)(\d{4})(?!\d)", (text or "").replace(" ", ""))
    who = "是我" in (text or "")

    if order:
        head = cfg.get("reply_fact_order") or "单号 %s 我记下了。" % order.group(0)
    elif tail4:
        head = cfg.get("reply_fact_tail") or "尾号 %s，对上了。" % tail4.group(1)
    elif who:
        # 「是我公司」和「是我本人」不是一回事，别一律回"是您本人"。
        # 档案在 outbound_target（pop 场景的 slots.application 是空的）。
        _tgt = session.get("outbound_target") or {}
        _co = _tgt.get("company_name") or ""
        _nm = _tgt.get("contact_name") or ""
        if ("公司" in (text or "") or "企业" in (text or "")) and _co:
            head = cfg.get("reply_fact_company") or "对，%s就是您公司。" % _co
        elif _nm and _nm in (text or ""):
            # 只有客户**自己刚报过这个名字**才复述。
            # 他说"是我"的时候，档案里的姓名是客服手上的资料，不是他给的值；
            # 拿它去回一句"对，是您本人，马涛"，等于把联系人姓名念给
            # 一个还没确认是谁的人听（接电话的可能是同事、前台）。
            head = cfg.get("reply_fact_self_named") or "对，是您本人，%s。" % _nm
        else:
            head = cfg.get("reply_fact_self") or "对，是您本人。"
    else:
        head = cfg.get("reply_fact_ack") or "好的，收到。"

    # 本轮客户刚递上信息：性格适配层不得再追加「要信息型」后缀，
    # 否则「尾号 9403 对上了……您也可以对照申请单编号核实」读起来像又要一遍。
    session["fact_ack_turn"] = True
    # 承接那半句交给 nlg 守：改写层不许把它压掉。
    # 线上第 6 轮骨架是「对，是您本人」，改写后只剩「您这边清楚这几样材料
    # 怎么准备吗」——客户确认了身份，客服一句都没认。数字/单号有专门的
    # 数字守卫，姓名和"是您公司"没有，靠这一条兜住。
    session["must_keep_open"] = re.sub(r"[，。；？！、\s]", "", head)

    if n >= 3:
        return _reply(session, _soothe(
            head + (cfg.get("reply_fact_hold")
                    or "清单在后台都能看到，您照着准备就行；哪一步卡住了直接说，"
                       "我陪您过一遍。"), flags),
            next_stage="CONFIRM", intent="ambiguous")

    asks = [cfg.get("reply_fact_ask_1")
            or "那这几样材料您清楚怎么准备吗？",
            cfg.get("reply_fact_ask_2")
            or "材料这块，您还有哪点不清楚？"]
    return _reply(session, _soothe(head + asks[(n - 1) % len(asks)], flags),
                  next_stage="CONFIRM", intent="ambiguous")


def _stage_confirm(session, text, flags):
    """CONFIRM：拿到明确答复。

    规则：明确肯定 → true；明确否定 → false；其他一律算含糊。
    含糊**先追问一次**（"您可以简单回我一句同意或者不需要"），
    **连续**第二次仍含糊就明说"我什么都没记"、留在 CONFIRM 等表态，
    连续第三次才转专员；全程**不写工单**——宁可不落结论，
    也不能把"嗯"当成同意（写 consent=unknown 也是替客户编结论）。
    连续是关键：中间隔着提问/报信息/说听不清，观察窗口必须重开，
    否则客户还在谈就被结束了（11-11 回归 L02/L07）。

    为什么追问这一次这么重要：犹豫的用户是外呼里最值钱的一类（还没决定），
    直接一句"那就不打扰您了"等于把本来能成的一单关掉，而且业务侧收到的
    是"客户拒绝"这个错误结论。以前这里写成 `confirm_asks >= 1`，
    导致 reply_again 是死代码、第一次含糊就挂断，这是已修的真实缺陷。

    另外这里多了一道置信度闸门：只有 conf ≥ YES_MIN_CONF 的肯定才算同意。
    "好吧""应该可以""差不多"这类弱肯定一律按含糊处理，追问一次再落库——
    它们听起来是答应，实质是"我不想跟你争了"，据此回写同意，
    后面退订和投诉就是从这里来的。
    """
    cfg = config_loader.stage_config("CONFIRM")

    # 修复请求（"信号不好，你再说一遍"）走复述，而不是走下面「没听懂」那条两次收线。
    # 位置必须在 NOT_CLEAR_MARKS 之前：那张表里有"再说一遍/没听清"，
    # 先命中就会把"请你再说一遍"当成"我听不懂"，第二次直接收线（A04 第 7 轮实测）。
    # 明确表态（"不要了，再说一遍"）仍由下面的拒绝分支回写，不被复述拦走。
    if (policy.repair_enabled("CONFIRM") and policy.is_repair_request(text)
            and not flags.get("no") and not flags.get("deny")):
        _rep = _repair_reply(session, cfg, "CONFIRM", flags)
        if _rep:
            _reset_ambiguity(session, True)
            return _reply(session, _rep, next_stage="CONFIRM", intent="repair")

    # 求助先于「没听懂」：CONFIRM 里答"不清楚"的客户是在说"操作上我不会"，
    # 给步骤 / 升级专员，而不是"您清楚了吗"问到第三次然后收线。
    # 11-11 线上实录：客户三次"不清楚"，客服复述了三遍全文后放弃——
    # 客户要的从来不是第四遍复述。
    # CONFIRM 不要求上一轮问过引导问句：这个环节的全部职责就是让客户
    # 弄清楚，客户在这里说"不清楚"永远是"需要帮助"，不是没听清。
    # 位置必须在 NOT_CLEAR_MARKS 之前——那张表里也有"不清楚"，先命中
    # 就会走"整段复述第二遍→转专员收线"的老路（本轮改的就是它）。
    # 判据统一走 _is_help_seek（含场景闸），这里不许用裸词表绕过：
    # 营销/售后场景的 CONFIRM 里"不清楚"仍走原含糊梯子。
    if _is_help_seek(session, text):
        _reset_ambiguity(session, True)
        return _help_reply(session, "CONFIRM")

    # 用户明确说没听懂：不回写同意、也不记拒绝，先把复述再讲一遍。
    # 第二次仍没听懂就转专员收线，**不产生回写事件**——回写会把
    # 「客户同意 / 拒绝」写进工单，而这通电话客户只是没听清，
    # 记成任何结论都是编的。也避免同一句追问反复念（agent_loop）。
    if any(w in text for w in NOT_CLEAR_MARKS):
        _reset_ambiguity(session, True)
        if session.get("reexplain_used"):
            return _finish(session,
                           cfg.get("reply_not_clear_handoff")
                           or "那我先不打扰您了，稍后专员会用官方号码再联系您，"
                              "需要补充的材料清单也会同步到商家后台。"
                           + "如有疑问，您也可以拨打京东商家热线 4006180092。感谢您的接听，再见。",
                           "unclear")
        session["reexplain_used"] = True
        return _reply(session, _confirm_restatement(session, cfg),
                      next_stage="CONFIRM", intent="ambiguous")

    # 拒绝再提供身份信息（同 NEED）。
    if _refuses_info(text):
        _ri = _refuses_info_reply(session, cfg, flags)
        if _ri:
            _reset_ambiguity(session, True)
            return _ri

    # 单号记不住（同上，NEED 里的说明）。
    if _cant_recall_order(text):
        _cr = _cant_recall_reply(session, cfg, flags)
        if _cr:
            _reset_ambiguity(session, True)
            return _cr

    # 后台打不开 / 不会弄：客户在说障碍，不是在表态。
    # 11-11 线上实录第 12 轮就踩在这里——客户说页面打不开，客服把材料清单
    # 整段念了一遍，客户第 13 轮直接说"打错了"。
    _blk = _blocked_ui_reply(session, text, cfg, flags)
    if _blk:
        _reset_ambiguity(session, True)
        return _blk

    # 场景确认词（pop："清楚了""明白了"）。CONFIRM 问的是什么由场景话术决定：
    # 主场景问"同意吗"，pop 问"清楚了吗"——通用 YES 词表覆盖不了所有场景问法
    # （"清楚了"不在任何肯定表里，"知道了"在通用含糊表里），于是商家明明说
    # 清楚了却被追问"您可以简单回我一句"。场景包用 confirm_yes_words 显式
    # 声明本场景的确认说法，命中即视同明确肯定；主场景不配这个键，行为不变。
    # 子串匹配要防否定变体："没明白"同样包含"明白"，必须先拦。
    _yes_marks = [w for w in (cfg.get("confirm_yes_words") or []) if w and w in text]

    def _negated(mark):
        # 只查确认词**附近**：「没听明白」中间隔了字，旧的连续串匹配抓不到；
        # 但也不能扫全文，「不是，我清楚了」是先否认后肯定，扫全文会误伤。
        i = text.find(mark)
        if i < 0:
            return False
        if any(neg in text[max(0, i - 4):i] for neg in ("没", "不", "别")):
            return True
        # 后置反问尾巴。11-11 线上 A15 第 7 轮：客户说
        # 「田思远，记住了没？」是**让客服记住姓名**，而 pop 场景的
        # confirm_yes_words 里有「记住了」，于是被当成"我清楚了"直接回写
        # 同意并收线——客户一个字都没确认。只看前面 4 字抓不到后置否定，
        # 这正是"没得到答案却说知道了"的镜像：客服没得到确认却当成确认。
        tail = text[i + len(mark): i + len(mark) + 3]
        return any(w in tail for w in ("没", "吗", "呢", "不", "？", "?"))

    if _yes_marks and not flags["no"] and not any(w in text for w in NO_CONTAINS) \
            and not any(_negated(w) for w in _yes_marks):
        return _do_writeback(session, True)

    if flags["yes"] and flags.get("conf", 1.0) >= YES_MIN_CONF:
        return _do_writeback(session, True)

    if flags["no"]:
        return _do_writeback(session, False)

    # 带情绪（T8）：不回"您可以简单回我一句同意或者不需要"。
    # 那句话在用户刚骂完之后读起来像"别废话，表态"——对抗模拟 N4 的原话。
    # 也不计 ambiguous：这一轮用户没有表态，是被情绪打断了，不该算"问过一次仍含糊"。
    if flags.get("angry"):
        return _angry_hold(session, "CONFIRM")

    # 提问不等于表态（11-08 接官方 QA 时补的一条）。
    # 旧行为：确认环节用户一问"这个会不会影响我店铺评分"，就落进下面的
    # confirm_asks += 1，问两轮记 unknown 转专员——商家问的是一个真问题，
    # 得到的回答却是"您简单回我一句清楚了或者还有疑问"。这在真实通话里
    # 是最典型的"客服装听不见"。
    #
    # 只在**真的答得上**时才拦（官方 QA 或知识条目命中）：答不上仍走原逻辑，
    # 否则任何带问号的句子都能免掉一次确认追问，等于把闸门拆了。
    # 拦截时 confirm_asks 不动——用户没有含糊，只是问了个问题。
    # 「等等，我先问下收费」不带问号，意图层判不出 ask；但它就是提问。
    # 不认的话会被算成含糊，隔着一轮又攒一次，客户后面还没说完就被收线。
    if flags["ask"] or any(w in text for w in _QUESTION_MARKS):
        blocked = _promise_block(session, text, flags)
        if blocked:
            _reset_ambiguity(session, True)
            return blocked
        _ans, _qa_hit, _kb_hit = _answer_official(session, text, flags)
        if _ans:
            _reset_ambiguity(session, True)     # 答上了 = 实质互动，窗口重开
            _tail = (cfg.get("reply_after_ask")
                     or "您看这样解释清楚了吗？")
            return _reply(session, "%s %s" % (_ans, _tail),
                          intent="qa" if _qa_hit else "ask",
                          qa_hit=_qa_hit, kb_hit=_kb_hit)

    # 答不上来的提问：查学到的路由表，用本单事实正面回答，再回到确认问题。
    # 11-09 逐轮反馈：A03/A06/A10/A14/A15 都在这里连问两次、客服两次“确认不到”、
    # 第二次直接收线；而这单的驳回原因与材料清单就在 slots.application 里。
    _routed = _route_answer(session, cfg, "CONFIRM", text, flags,
                            tail=(cfg.get("reply_after_ask")
                                  or "您看这样解释清楚了吗？"))
    if _routed:
        _reset_ambiguity(session, True)
        return _routed

    # 客户指出「你刚才问过了」：这不是提问，是在说客服没接住。
    # 必须在 engaged 判定之前——否则落到 talk 兜底，回「我这边确认不到」。
    if _meta_repeat(text):
        _reset_ambiguity(session, True)
        return _meta_repeat_reply(session, cfg, flags)

    # 客户还在提问 / 质疑 / 提供信息，而这一轮没有明确表态：这不是「含糊表态」。
    # 旧逻辑把它计入 confirm_asks，两次就回写 unknown 收线，等于替一个还没
    # 谈完的客户编结论。第一次如实回应并留在 CONFIRM；第二次转专员
    # 收线，全程不产生回写事件——工单里不该出现凭空猜出来的「不确定」。
    engaged = _still_engaged(session, text, flags)
    if engaged:
        # 客户拿回了实质内容：「连续两次含糊」的观察窗口重开。
        _reset_ambiguity(session, engaged == "fact")
        if engaged == "fact":
            # 客户在给信息（报单号、重申身份）：先复述他给的值，再问下一步。
            # 不计入任何升级计数——配合不该被「配合两次」送走；旧版两轮
            # 实质回合就转专员收线，客户正按客服建议报单号却被挂了
            # （11-08 A15 实测）。也不编「收到」之外的任何结论。
            return _fact_ack_reply(session, text, cfg, flags)
        # 提问/质疑且这一轮答不上：第一次如实说明，第二次转专员收线，
        # 全程不产生回写事件——工单里不该出现凭空猜出来的「不确定」。
        n = int(session.get("confirm_engaged", 0)) + 1
        session["confirm_engaged"] = n
        if n >= 2:
            return _finish(
                session,
                cfg.get("reply_engaged_handoff")
                or "您的问题我这边确认不到，先不打扰您了；稍后专员会用官方号码"
                   "再联系您，需要补充的材料清单也会同步到商家后台。"
                + "如有疑问，您也可以拨打京东商家热线 4006180092。感谢您的接听，再见。",
                "unresolved_question")
        return _reply(
            session,
            cfg.get("reply_unanswered")
            or "这个我这边确认不到，稍后让专员用官方号码给您回电说明，可以吗？",
            next_stage="CONFIRM", intent="ask")

    # 客户把同一句又说了一遍：先换个说法接住，不把它算成「含糊表态」。
    if _self_repeated(session, text):
        _reset_ambiguity(session, True)
        return _self_repeat_reply(session, cfg, flags)

    # 到这里才是真正「没表态也没在谈」的一轮。前面每个实质分支都会把
    # confirm_asks 清零，所以这里数的必然是**连续**含糊轮数。
    session["confirm_asks"] += 1
    if session["confirm_asks"] < 2:
        # 换着说：客户连着两轮"我一时真想不起来"，客服两轮逐字相同地
        # 念"您可以简单回我一句清楚了或者还有疑问就行"，读起来像卡带
        # （11-11 线上实录第 8、11 轮）。第二次换成更具体的问法。
        _k = int(session.get("confirm_again_n", 0))
        session["confirm_again_n"] = _k + 1
        _again = [cfg.get("reply_again", cfg.get("reply", "")),
                  cfg.get("reply_again_2")
                  or "不用您复述全部，就一句：这三样材料您清楚要怎么准备了吗？"]
        return _reply(session, _soothe(_again[_k % len(_again)], flags),
                      intent="ambiguous")
    # 连续两次含糊：**不写工单，也不结束通话**。
    #
    # 原来这里写 _do_writeback(session, "unknown")。11-11 线上实录：
    # 客户从头到尾只说"嗯"，客服也照样写了一张工单（consent=unknown）并收线，
    # 业务侧看到的是"这通电话已沟通"，而客户一个字都没确认——
    # "没得到答案却说知道了"换了个形式又出现一次，只不过这次写在工单里。
    #
    # 第一版修法改成"转专员收线"，又过头了：88 条评测的 L02/L07、课程 Gen3
    # 的消耗战用例，客户中间含糊两轮之后还有明确表态（"同意""算了不要了"），
    # 全被提前挂断（86/88、Gen3 8/12）。不替客户下结论 ≠ 替客户结束通话。
    # 正确的动作是：把"我什么都没记"说清楚，留在 CONFIRM 继续等；
    # 客户后面一句"那我要吧"仍能正常回写。连续第三次才交给人。
    if session["confirm_asks"] == 2:
        return _reply(
            session,
            _soothe(cfg.get("reply_ambiguous_hold")
                    or "没关系，您再想想。我这边先不记任何结论，"
                       "您别急，我不催您表态。您具体想问哪一步，我直接说给您听，"
                       "不用先回一句清楚了。", flags),
            next_stage="CONFIRM", intent="ambiguous")

    # 连续三次含糊：交给人，全程一张工单都不写。
    return _finish(
        session,
        cfg.get("reply_ambiguous_handoff")
        or "您还没拿准，我就不替您定了。稍后专员用官方号码再跟您确认一次。"
           + "如有疑问，您也可以拨打京东商家热线 4006180092。您先忙。", "unresolved_consent")


def _stage_writeback(session, text, flags):
    """WRITEBACK：兜底入口。

    正常情况下应当在 NEED/OFFER/CONFIRM 里直接调用 _do_writeback 完成，
    走到这里说明上一轮没有正常迁移，直接补一次回写。
    结论沿用已有槽位；没有就记 unknown（不知道结果比编一个结果安全）。
    """
    consent = session["slots"].get("consent")
    if consent not in (True, False, "unknown"):
        consent = "unknown"
    return _do_writeback(session, consent)


def _stage_done(session, text, flags):
    """DONE：终态。默认只说结束语，不再发起新话题（继续推销会拉低交互质量分）。

    但终态不等于"不再听"。两种输入必须处理，否则会出现两个真实缺陷：
        1. 用户改口（"等等，我不要了" / "那我要"）——不回写就更正，
           业务侧会一直拿着旧结论；
        2. 用户提问（"这个是免费的吧"）——不回答直接说再见，是最伤体验的一种。

    改口靠递增幂等键版本重写同一张工单，不新增阶段（不改状态机契约）。
    """
    cfg = config_loader.stage_config("DONE")
    current = session["slots"].get("consent")

    # 改口同样要过置信度闸门：终态里一句"好吧"如果被当成"我要了"，
    # 就会拿一个弱肯定去覆盖一张已经定稿的工单，比不回写更糟。
    yes_ok = flags["yes"] and flags.get("conf", 1.0) >= YES_MIN_CONF
    target = True if yes_ok else (False if flags["no"] else None)
    already_written = int(session.get("writeback_version", 0)) > 0
    # 注意这里不能要求 current 是布尔值：含糊回写记的是 "unknown"，
    # 用户之后说清"那我要"时必须能把它更正过来，否则工单永远停在"不确定"。
    if target is not None and already_written and current != target:
        return _do_writeback(session, target, changed=True)

    if flags["ask"]:
        # 终态里提问：先答（官方原话优先），再把热线与再见说出来。
        # 旧行为是直接念 reply_ask（"如有疑问请拨热线…再见"）——答非所问，
        # 但那句热线必须留着：用户挂了电话还想找人，这是唯一的兜底路径。
        _ans, _qa_hit, _kb_hit = _answer_official(session, text, flags)
        if _ans:
            # hotline_close 冻结：这句含官方热线号码与统一收线口径。11-12 实测
            # intent="ask" 会进改写层，把"您也可以拨打热线 / 感谢您的接听"改成
            # "欢迎拨打热线 / 感谢接听"——措辞一变，persona 的收线标记就匹配不上，
            # "我放慢一点说"又会挂回"再见"后面。官方口径逐字播，不交给模型润色。
            return _reply(session, "%s %s" % (_ans, cfg.get("reply_ask") or cfg.get("reply", "")),
                          intent="hotline_close", next_stage="DONE",
                          qa_hit=_qa_hit, kb_hit=_kb_hit)
        # 答不上时的兜底：第一次给热线，之后不再逐字复读同一句。
        # 真实通话 s_69aca7 里用户连问三个问题，客服把「如有疑问请拨热线」一字不差
        # 念了三遍（11-09 真实通话挖掘 real_loop3）——用户以为电话卡住了。
        n = int(session.get("done_ask_n", 0)) + 1
        session["done_ask_n"] = n
        if n == 1:
            # 这一句是收线话术：next_stage=DONE + hotline_close 双冻结，绝不挂
            # "我放慢一点说"这类尾巴——11-11 截图末轮"…感谢您的接听，再见。
            # 我放慢一点说。"就是这里没冻结、被 hard_of_hearing 后缀钻了空子。
            return _reply(session, cfg.get("reply_ask") or cfg.get("reply", ""),
                          intent="hotline_close", next_stage="DONE")
        return _reply(
            session,
            cfg.get("reply_ask_again")
            or "这个细节我这边说不全，专员回访时会跟您讲清楚；着急的话拨 4006180092 也能找到我们。",
            intent="hotline_close", next_stage="DONE")

    # 已经说过再见了：用户这句只是"再见""嗯""一个字"这种收尾词，不再重复整段。
    # 旧行为是每来一句就把整段结束语再念一遍（线上实测：说"再见"重复一次，
    # 说"一个字"又重复一次），用户会以为电话卡住了，而且永远挂不掉。
    if session.get("done_announced"):
        out = _reply(session, cfg.get("reply_short") or "好的，再见。",
                     intent="done", end_reason="done")
        out["hangup"] = True
        return out

    # 第一次进终态：说结束语，并标记"已告别"，同时请求服务端收线。
    session["done_announced"] = True
    out = _reply(session, cfg.get("reply", "感谢您的接听，再见。"),
                 intent="done", end_reason="done")
    out["hangup"] = True
    return out


_HANDLERS = {
    "GREETING": _stage_greeting,
    "IDENTITY": _stage_identity,
    "NEED": _stage_need,
    "OFFER": _stage_offer,
    "CONFIRM": _stage_confirm,
    "WRITEBACK": _stage_writeback,
    "DONE": _stage_done,
}


def _handle_empty(session):
    """用户这一轮没说话（沉默 / 空输入）。

    规则：连续第二次仍无应答就收尾挂断；此外**总沉默次数**也有上限。

    为什么加总次上限：连续计数 `empty_count` 在任意一句非空输入时归零，于是
    「沉默 → 说一个字 → 沉默」能无限循环，每次都是同一句「您还在吗」。真实通话
    s_a63565 里这句一字不差出现 4 次（11-09 真实通话挖掘 real_silence_repeat）：
    用户听到的是卡带，业务侧拿到的是 8 轮无效通话。

    _todo 真实实现应区分两种情况：完全没声音（网络/静音）vs 有声音但 ASR 没识别出字。
    后者不该直接挂断，应该重播一次并提示"没听清"。
    """
    cfg = config_loader.silence_config()
    # 已经收线了：再沉默不该把收尾长句再念一遍。真实链路里这时连接已断，
    # 但重放/回放会继续喂输入，同一句会被念两次（11-09 重放 s_a63565 复现）。
    if session.get("stage") == "DONE":
        out = _reply(session, cfg.get("reply_done") or "好的，再见。",
                     intent="empty", end_reason="silence")
        out["hangup"] = True
        return out
    total = int(session.get("silence_total", 0)) + 1
    session["silence_total"] = total
    if session["empty_count"] >= 2 or total >= 3:
        return _finish(session, cfg.get("reply_final", "看来信号不太好，稍后再联系您，再见。"), "silence")
    if total == 1:
        return _reply(session, cfg.get("reply", "您还在吗？"), intent="empty")
    # 第二次起换说法：同样的问题问两遍可以，一字不差地念两遍不行。
    return _reply(session,
                  cfg.get("reply_again")
                  or "我这边还是没听到声音，您方便时喂一声就行；实在不方便，我稍后让专员再打给您。",
                  intent="empty")


def forced_end(session, reason):
    """被容量/时长闸门强制收尾（不是用户的意思，所以不推进业务结论）。

    触发条件见 config.budget：轮次超限、单通时长超限。
    为什么要做这个：外呼是按时长计费的，没有上限的话一个死循环或一个
    不爱挂断的用户就能把当天额度烧完。先能拦，再谈优化。
    """
    cfg = config_loader.stage_config("DONE")
    key = {"max_turns": "reply_budget_turns",
           "max_call_seconds": "reply_budget_time"}.get(reason)
    text = (cfg.get(key) if key else "") or cfg.get("reply", "感谢您的接听，再见。")
    return _finish(session, text, reason)


def fallback_for(reason="error"):
    """按失败原因取兜底话术。reason 取 FALLBACK_SCENES 的键。

    取值顺序：`fallback.<键>` → （仅 writeback）`stages.WRITEBACK.<键>` → 内置默认。
    为什么 writeback 要单独多查一层：那批话术早就写在 `stages.WRITEBACK` 里了，
    直接搬到 `fallback` 段等于让 4 号改配置、让 3 号的用例重新对齐，收益为零。

    为什么别的 reason 不去查 stages：`WRITEBACK.reply` 是**回写成功**的话术，
    兜底时误拿到它就会对着用户说"已经为您登记完成"——这是最严重的一类事故。
    所以只对白名单里的键开放第二次查找。
    """
    key, default = FALLBACK_SCENES.get(reason, FALLBACK_SCENES["error"])
    text = config_loader.fallback_text(key)
    if text:
        return text
    if reason == "writeback":
        text = config_loader.stage_config("WRITEBACK").get(key)
        if text:
            return text
    return default


def fallback_key(reason="error"):
    """某个 reason 对应的配置键，供写需求和查配置时对照。"""
    return FALLBACK_SCENES.get(reason, FALLBACK_SCENES["error"])[0]


def _handoff(session, reason="handoff"):
    """转人工：礼貌收尾，并且**不下业务结论**。

    为什么不回写：转人工意味着"这通电话没谈完"，业务结论应该由人工给出。
    这时候写一张"未同意"的工单，等于替用户做了决定，人工接手时还会看到
    一条自相矛盾的记录。

    为什么走 `_finish` 而不是直接返回：`_finish` 会把阶段推到 DONE 并带上
    end_reason，`_reply` 见到 end_reason 就跳过话术改写——"再见"被模型
    改写成"拜拜下次聊"是外呼里很难解释的事故。

    reason 是给质检归因用的原因码（user_request / handoff_unclear / …）。
    话术不变，变的是事件里那一个字段——"为什么把这通电话交出去"必须能查。
    """
    cfg = config_loader.stage_config("DONE")
    text = (cfg.get("reply_handoff")
            or config_loader.fallback_text("reply_handoff")
            or "好的，我帮您转接人工同事，稍后会有专人和您联系，"
                   "如有疑问，您也可以拨打京东商家热线 4006180092。感谢您的接听，再见。")
    return _finish(session, text, reason)


def safe_fallback(session, reason="error"):
    """编排环节自己出错时的兜底回复：认错 + 按原因给不同处理，阶段不推进。

    不推进阶段是刻意的：这一轮没听懂，下一轮应该从同一个环节重来，
    而不是假装已经过了这一关（否则会漏掉必说话术与确认）。

    reason 只影响"说哪句"，不影响状态机——兜底永远不该改变业务进度。
    """
    return _reply(session, fallback_for(reason), intent="error")


# 确认"我就是这家公司"的肯定说法。只在**短句**上认，长句另作处理。
# 刻意不把"是"单独当万能词：先排除否定与打错，再看肯定词。
AFFIRM_COMPANY = ("是的", "是啊", "对啊", "对对", "没错", "正是", "就是",
                  "我就是", "是我", "对", "嗯", "是")


def _target_company_tokens(target):
    """外呼目标公司的可匹配标识。

    商家在电话里一般只报字号（"安食"）或店名（"安食官方旗舰店"），
    很少逐字念全称（"北京市安食商贸有限责任公司"），所以三个都收，
    另外把全称的前若干字也算上（"北京市安食"）。
    """
    if not target:
        return []
    out = []
    for key in ("company_short_name", "shop_name", "company_name"):
        v = str(target.get(key) or "").strip()
        if v and v not in out:
            out.append(v)
    full = str(target.get("company_name") or "").strip()
    if len(full) >= 6:
        head = full[:6]
        if head not in out:
            out.append(head)
    return out


def _mentions_target_company(text, target):
    """用户这句话里有没有提到目标公司。"""
    t = (text or "").strip()
    if not t:
        return False
    for token in _target_company_tokens(target):
        if len(token) >= 2 and token in t:
            return True
    return False


def _is_affirm_company(text):
    """用户是不是认了"我就是这家公司"。"""
    t = re.sub(r"[\s，,。.!！?？、…～~]", "", text or "")
    if not t or len(t) > 14:
        return False
    if any(w in t for w in ("不是", "不对", "没有", "打错", "找错", "不要", "别")):
        return False
    return any(w in t for w in AFFIRM_COMPANY)


def _is_deny_company(text):
    """用户否认"我就是这家公司"。

    只在**短句**上认：刚问完"您是XX公司的XX吗"，对方回一个"不是"就是否认。
    长句里的"不是"多半是别的意思（"不是我不想配合，是这个章我拿不到"），
    那种该继续谈而不是直接挂断——打错人挂断是对的，把话说清楚的人挂断是错的。
    """
    t = re.sub(r"[\s，,。.!！?？、…～~]", "", text or "")
    if not t or len(t) > 10:
        return False
    return any(w in t for w in ("不是", "不对", "没有", "打错", "找错", "我不是"))


def _fill_outbound(text, target):
    """把开场白里的 {company_name} 填成外呼目标的**店铺名/简称**。

    为什么要有这一步：官方 52 条话术的固定格式是
    "您好，我是京东商家资质审核专员，请问您是（商家公司名/店铺名）…吗？"——
    **要报出找的是哪家**。我们原来写的是"请问是贵公司的负责人吗？"，
    既没单位名，又让人无从判断这通电话是找谁的；商家凭什么相信一个陌生来电？

    11-12 改：原来优先报**工商登记全称**，线上实录念成"请问您是新疆维吾尔
    自治区溪亭商贸有限公司的品牌授权经办人吗？"——一长串行政区划 + 预设
    "品牌授权经办人"这顶身份帽子，对方只要回一句"是的"就被当成认过身份，
    等于没核身先替对方把"他是谁"定了。现在：
      1) 只确认**单位**（店铺名优先：官方允许"店铺名"，且短、听着自然），
         不再念工商全称；没有店铺名才退回全称，再退回简称。
      2) 话术里已删掉 {contact_role}，不再预设职务；"他到底是谁"交给下一环
         IDENTITY 报预留手机号后四位核身，核身通过前不讲申请单详情。
    赛题四段式第②环"确认对方身份（请问您是 XX 公司吗）不能省，打错人是重大
    事故"仍守住：模板保留 {company_name} 占位符，开场白仍带单位名与"请问您是"。

    没有外呼目标时（自检、名单取空）原样返回，不留花括号给用户听。
    """
    if not target or not text:
        return text
    name = (target.get("company_name") or "").strip()
    short = (target.get("company_short_name") or "").strip()
    shop = (target.get("shop_name") or "").strip()
    who = shop or name or short
    out = text
    if who:
        out = out.replace("{company_name}", who)
    # {contact_role} 已从所有话术里删除，这里留一个兜底：万一有旧场景仍带
    # 这个占位符，也要填掉，绝不把花括号念给客户听。
    out = out.replace("{contact_role}",
                      (target.get("contact_role") or "负责人").strip())
    return out if "{" not in out else text


def opening_line(target=None):
    """开场白：外呼是客服先说话，不是等用户先开口。

    为什么单独一个函数：外呼场景里"开场自报身份"是六环节的第一环，也是合规要求
    （必须说清是谁、是哪家公司）。以前它只作为"回应用户第一句"的素材存在，
    结果用户一上来就报尾号时，客服直接从"核对上了"开始，自报身份被整段跳过——
    既拿不到必说话术分，也不符合外呼的合规惯例。

    target 是这通电话的外呼目标（外呼名单里那条申请单），用来把公司名填进开场白。
    返回：str
    """
    raw = config_loader.stage_config("GREETING").get("reply", "您好。")
    return _fill_outbound(raw, target)


def _localize(session, out):
    """英文模式下把状态机算出的中文回复换成英文话术。

    只换**播出文本**，不动 stage / intent / tool_call：业务流程、回写字段、
    事件统计全部照旧，英文客户走的是同一套合规流程。
    """
    if session.get("lang") != "en" or not isinstance(out, dict):
        return out
    tpl = (i18n.EN_REPLIES.get(out.get("intent") or "")
           or i18n.EN_BY_STAGE.get(out.get("stage") or "")
           or i18n.EN_FALLBACK)
    # 11-12 20:07：不能把后台中文原值直接塞进英文模板（会变成
    # "Your 荃汀品牌、运动户外类目 application was not approved because…"）。
    # 统一走 fill_en：品牌名/主体名保留原文，类目、原因、材料全部英文。
    text = i18n.fill_en(tpl, session["slots"].get("application"),
                        session.get("outbound_target"))
    if "{" in text:                      # 占位没填干净就别播出花括号
        text = i18n.EN_FALLBACK
    out["reply_text"] = text
    out["lang"] = "en"
    return out


def respond(session, user_text):
    """核心入口：吃一句用户话，吐一句回复（可能附带一次工具调用）。

    全局规则（先于任何阶段判断）：
        - 空输入 → _handle_empty
        - 要求转人工 / 挂断 → 直接收尾，不回写
        - 问"我刚才说的什么" → 从记忆里回溯（身份已核验才允许，见 _recall）
    其余交给当前阶段的处理器。

    参数：
        session    new_session() 返回的 dict，会被就地修改
        user_text  用户这一轮说的话
    返回：dict(reply_text, stage, next_stage, tool_call, intent, intent_conf)
    """
    text = (user_text or "").strip()
    # 英文报数先换算（"four four two seven" → "4427"），再进意图识别与核身
    # 匹配——数字守卫看见的就已经是数字了。
    text = _normalize_spoken_digits(text)

    # ── 会话语言（11-12：客户说 yes / ok 不该毫无反应） ──────────────
    # 英文短句先归一成中文等价句喂给状态机，最后 _localize 再换成英文播出；
    # 这样意图、阶段、回写全走同一套中文逻辑，不需要第二套状态机。
    _prev_lang = session.get("lang") or "zh"
    _lang = i18n.detect(text, _prev_lang)
    session["lang"] = _lang
    # 不管当前是哪种语言，明确说 "i can't understand english" 一律切回中文；
    # 只判 _prev_lang == "en" 会让中文模式下说这句的人反而撞上"只能讲普通话"。
    if i18n.wants_chinese(text):
        _out = _reply(session, i18n.ZH_BACK, intent="lang_back")
        _remember(session, text, _out)
        return _out
    if i18n.asks_english(text):
        _out = _reply(session, _fill_outbound(i18n.EN_REPLIES["lang_en"],
                                              session.get("outbound_target")),
                      intent="lang_en")
        _remember(session, text, _localize(session, _out))
        return _localize(session, _out)
    # 英文短句归一成中文等价句，**两种语言模式下都做**。
    # 原来只在 en 模式下归一：11-13 改了判定之后，中文模式下的 "ok" 不再切英文，
    # 于是 "ok" 会原样进分类器，被当成没听懂——客户明明应了一声，客服却反问
    # "您刚才说什么"。归一之后，它就是一句中文的"是的"，流程照常往下走。
    _zh = i18n.to_zh(text)
    if _zh:
        text = _zh

    # 收线之后又来一句：不再走业务逻辑。
    #
    # 11-13 用户截图：「闭环了之后通话还能继续下去，很怪」。旧行为是每来一句就
    # 再回一遍告别话术（"感谢您的配合…再见"），页面也一直能发消息——客户会觉得
    # 这通电话没完没了。现在只回一句"已经结束 + 热线"，且**不再推进任何阶段**。
    # 唯一的例外是改口：客户在挂断前说"等等，我说错了"，那是业务事实变了，
    # 必须放回 `_do_writeback` 去改（见 test_correction 的 C03/C04）。
    if session.get("stage") == "DONE" and not _looks_like_correction(text):
        _n = int(session.get("after_done_n", 0))
        session["after_done_n"] = _n + 1
        # 三句轮换，都带热线、都不讲业务。为什么必须轮换：客户在收线后连问两句
        # 答不上的事，同一句话念两遍，听起来就是"这电话卡住了"（verify 有一条
        # 专门守它）。为什么不答问题：阶段已经 DONE，再答下去就是正文无限延长，
        # 而这正是用户 11-13 说的"闭环之后通话还能继续，很怪"。
        _after_done = (
            config_loader.fallback_text("reply_after_done")
            or "这通电话已经结束了。如果还有问题，直接打电话联系 4006180092。",
            "这单已经记录完成了，通话也结束了。后续有问题，拨 4006180092 找专员就行。",
            "电话这边已经结束了哈。还有事的话，打 4006180092 就能找到我们。",
        )
        return _reply(session, _after_done[_n % len(_after_done)],
                      next_stage="DONE", intent="done_again")

    session["turn"] += 1
    session["slots"]["last_user_text"] = text
    # 轮内标志先清：上一轮「客户刚给过信息」的状态不能带进这一轮。
    session.pop("fact_ack_turn", None)
    session.pop("must_keep_open", None)
    # 性格证据：只看这一句用户话的字面（不读 stage / intent / 画像标签）。
    # 放在所有分支之前，空输入与直接收尾的路径也能正确累积证据。
    persona.observe(session, text)

    if not text:
        session["empty_count"] += 1
        # 空输入没有"意图"可言。显式归零，避免上一轮的高置信度被这一轮复用，
        # 事件里出现"用户没说话但 conf=0.95"这种没法解释的数。
        session["last_intent_conf"] = 0.0
        out = _handle_empty(session)
        _remember(session, "", _localize(session, out))
        return _localize(session, out)
    session["empty_count"] = 0

    # 意图识别走 modules.intent（T5）：默认 provider=template，直接返回这里的
    # classify() 结果，行为逐字节不变；开了 dashscope 才让模型在六个可改标签上裁决，
    # deny/hangup/handoff/digits 永远以规则为准（见 intent.py 顶部说明）。
    flags = intent.classify(text)
    # 识别置信度落到 session，_reply 会把它带进每个事件。
    # 放在这里而不是让每个 handler 传参：出口有十几个，漏一处就是一个假数据点。
    session["last_intent_conf"] = flags.get("conf", 0.0)
    # 措辞层（nlg）要用的两个上下文：这一轮的原话与情绪。
    # 以前 nlg 的 prompt 里"客户刚说"永远读不到值（它读顶层字段，dialogue 写在 slots 里），
    # 于是每句话都像没听见用户——T3 修的就是这个。
    # 上一句客户原话单独留一份：last_user_text 是给措辞层读"本轮"的，
    # 而判「客户把同一句又说了一遍」需要的是上一轮。两者别混用。
    session["prev_user_text"] = session.get("last_user_text") or ""
    session["last_user_text"] = text
    session["last_flags"] = flags
    session["last_user_angry"] = bool(flags.get("angry"))

    # 同音字确认：「您说的是 X 吗」的回答。必须排在所有阶段判断之前——
    # 这里的"对 / 不是"不是业务意图，落进 GREETING 会被当成"确认公司"，
    # 落进 IDENTITY 会被当成"拒绝核身"。
    _confirmed = _confirm_name_answer(session, text, flags)
    if _confirmed:
        return _confirmed
    if session.get("confirm_name"):
        # 既没肯定也没否定（客户换了个说法）：清掉待确认，按新输入走。
        # 不清的话下一轮还会问"您说的是 X 吗"，变成复读。
        session.pop("confirm_name", None)

    # 全局：要求转人工。放在挂断判断之前——"我要真人"和"别打了"是两种相反的诉求，
    # 前者要承诺有人接手，后者要立刻结束；同时出现时以转人工为准（用户还在听）。
    # 原因码沿用 W7 定下的 "handoff"（用户主动要求），
    # 这里不改是因为 3 号的用例与看板已按这个值统计；新增的"听不懂"那条分支
    # 用 "handoff_unclear" 区分，两者在事件里能分开归因。
    # 全局：语言不通 / 抗议称呼。都排在挂断判断之前：
    #   - "i can't understand Chinese" 不含挂断词，但客户已经在说"我们没法
    #     沟通"，装听不见只会火上浇油；
    #   - "不要喊我名字！"带否定词，旧逻辑会落到各阶段的拒绝分支直接收线——
    #     客户抗议的是称呼方式，不是业务，挂断等于把还在配合的人赶走。
    # 两者都只在通话中的阶段生效（WRITEBACK/DONE 已经在收尾了，不再横生枝节）。
    _live_stage = session["stage"] not in ("WRITEBACK", "DONE")
    if _live_stage and session.get("lang") != "en" and _is_foreign_lang(text):
        out = _language_reply(session, session["stage"])
    elif _live_stage and _is_name_protest(text):
        out = _name_protest_reply(session, session["stage"])
    elif flags["handoff"] and session["stage"] not in ("WRITEBACK", "DONE"):
        out = _handoff(session)
    # 全局：要求挂断。注意与"明确拒绝"区分——两者同时出现时按拒绝处理，
    # 因为拒绝是业务结果，必须回写留痕（"不需要，别打了"属于这一类）。
    elif _denies_identity(text) and session["stage"] not in ("GREETING", "WRITEBACK", "DONE"):
        # 已经核过身，客户才说"打错了，没这个人"：必须停，而且**不许回写**。
        # 11-11 实测最严重的一条：客户在第 13 轮说"打错了，没这个人！"，
        # 客服回"好的，我先按现在的沟通情况记录…"并写了工单——把一通
        # 打错人的电话记成了"已沟通/不确定"。业务侧拿到的是一条假数据。
        session["identity_denied"] = True
        out = _finish(session,
                      config_loader.stage_config("GREETING").get("reply_deny", "")
                      or "抱歉打错了，那我不打扰您了，再见。", "not_me")
    elif flags["hangup"] and not flags["no"] and session["stage"] not in ("WRITEBACK", "DONE"):
        out = _finish(session, config_loader.stage_config("GREETING").get("reply_hangup", ""), "hangup")
    else:
        # 记忆回溯放在挂断之后、阶段处理之前：它是"回答问题"，
        # 不该改变流程；也不该被某个阶段的处理器截走当成新诉求。
        out = _recall(session, text) if _is_recall_question(text) else None
        # 11-12：追问范围 / 质疑 / 明确说清楚了 / 要清单，都必须在**所有阶段
        # 分支之前**处理。NEED 最前面的 flags["no"] 会先把质疑收成 declined，
        # CONFIRM 的含糊梯子会把「清楚了」再问一遍——放阶段里都拦不住。
        if out is None:
            out = _early_answer(session, text, flags)
        if out is None:
            handler = _HANDLERS.get(session["stage"], _stage_done)
            out = handler(session, text, flags)

    out = _localize(session, out)
    _remember(session, text, out)
    return out


def compliance_guard(text):
    """合规护栏：检查待播报的话术有没有踩红线。

    命中禁语的处理是**整句替换**，不是打码。原因：
        - 打码后的句子会被 TTS 念成"星星星星"，用户听到的是怪音；
        - 打码只遮住了关键词，句子的诱导语义还在（"我保证您***赚钱"）。
    所以命中即整句换成配置里的合规话术，并把命中词记进事件供质检归因。

    另外做一次自检：如果配置的合规话术里本身含禁语，就退回一句最保守的兜底，
    否则会出现"用来兜底的话自己违规"这种荒谬情况。

    _todo 真实实现要做语义级检测（换个说法说同样的话），并重新生成一句
    合规表达而不是整句换掉——现在换句会丢上下文，但至少不会播错。
    """
    out = text or ""
    hits = [w for w in config_loader.forbidden_words() if w and w in out]
    if not hits:
        return {"text": out, "blocked": False, "hits": [], "replaced": False}

    safe = config_loader.blocked_reply()
    if any(w and w in safe for w in config_loader.forbidden_words()):
        safe = "这部分我需要请专员和您说明，可以吗？"
    return {"text": safe, "blocked": True, "hits": hits, "replaced": True}


def fallback_text(key="reply_fail"):
    """按**配置键**取兜底话术（老接口，`app.py:525` 在用）。

    注意它吃的是"键"不是"原因"——`reply_fail` 是配置里已有的键名。
    新代码请用 `fallback_for(reason)`，按"为什么会兜底"来取，
    可读性更好，也不会把键名和原因混成一个概念。
    """
    reason = _KEY_TO_REASON.get(key)
    if reason:
        return fallback_for(reason)
    return config_loader.fallback_text(key) or ""
