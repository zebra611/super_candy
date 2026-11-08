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

import re

import config_loader
from modules import intent, kb, nlg, official_script, persona, tools

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
    if any(w in t for w in ANGRY_WORDS + _insult_extra):
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
    if (not end_reason and intent not in ("error", "empty")
            and not _freeze_identity and not _freeze_official):
        text = nlg.say(session, _fill_application(text, session["slots"].get("application")),
                       stage_now)
    else:
        # 挂断/拒绝/出错话术不走改写，但占位符仍要填（C5 统一兜底）。
        text = _fill_application(text, session["slots"].get("application"))

    # 性格适配放在措辞层**之后**：它只调形式（客套前缀/引导句），
    # 官方原话、核身问句、挂断话术一个字都不动——那三类要么必须逐字复述，
    # 要么指代必须固定，改写与细化都会把依据改没。
    # 核身问句不整体冻结：它要冻结的是**措辞层改写**（nlg，见上），
    # 而性格层只删句首"好的，"这类客套、不动指代与事实。
    # 官方原话 / 挂断 / 出错话术则全冻结，连形式都不动。
    _persona_freeze = (bool(end_reason) or intent in ("error", "empty")
                       or _freeze_official)
    text, _persona_applied = persona.adapt(
        session, text, stage_now, freeze=_persona_freeze)
    # 记的是**实际播出去**的那句（含适配后的引导语），下一轮判重复才准。
    session["last_reply_text"] = text

    if next_stage:
        session["stage"] = next_stage
    # 置信度默认取"这一轮用户话的识别置信度"（respond 里算好放进 session）。
    # 放在 session 里而不是每个调用点都传参，是因为出口太多（回写、兜底、转人工、
    # 收尾都要走 _reply），漏传一处就会出现"某些事件的 conf 永远是 0"这种假数据。
    if intent_conf is None:
        intent_conf = session.get("last_intent_conf", 0.0)
    out = {
        "reply_text": text,
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


def _official_line(session):
    """本通档案的官方驳回话术（已填槽、已去掉自报身份与热线尾句）。

    没有可用话术时返回空串——调用方退回自己的模板。
    结果缓存在 session 里：一轮里可能问两次，没必要每次重读 kb_pop。
    """
    if "official_line" not in session:
        got = official_script.script_for(_app_of(session))
        session["official_line"] = official_script.dialogue_text(got["script"]) if got else ""
        session["official_script_id"] = (got or {}).get("id", "")
    return session["official_line"] or ""


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
        intent = "consent"
    elif consent is False:
        key = "reply_declined"
        intent = "declined"
    else:
        key = "reply_unknown"
        intent = "ambiguous_writeback"

    text = cfg.get(key) or cfg.get("reply", "已为您登记。")
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
         "不好意思，我这边没太听清。您方便再说一下主要想了解什么吗？"),
        ("reply_unclear_need_2",
         "可能信号不太好。您是想了解产品使用，还是售后这方面的问题呢？"),
    ),
    "OFFER": (
        ("reply_unclear_offer",
         "我这边可能没讲清楚。您是想让我再讲一遍这个方案，还是有别的顾虑？"),
        ("reply_unclear_offer_2",
         "那我确认一下您的意思：您现在是同意，还是暂时不需要？简单回我一句就行。"),
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
    return _reply(session, config_loader.fallback_text(key, default), intent="unclear")


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
         "没关系，那方便说一下您的姓名吗？我核对一下。"),
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
    # 「就是晏青梧」是真人报姓名最常见的说法之一（前面常带"唉"等语气词）。
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
    if t[0] not in _SURNAMES:
        return ""
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
    if len(t) < 6 or not any(m in t for m in marks):
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
    if flags.get("digits"):
        _submit_identity_slot(session, "tail", flags["digits"])
        took.append("tail")
    app_id = _extract_application_id(text)
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
    if name:
        _submit_identity_slot(session, "name", name)
        took.append("name")
    return took


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


def _ask_identity(session, slot, next_stage=None):
    """按槽位取话术追问，并记账（问了几次、上次问的是哪个）。

    next_stage 只在"从 GREETING 首次进入核验"时传 IDENTITY：
    那一句在事件里仍归属 GREETING（它是对开场白的回应），
    后续追问则不推进（next_stage=None，阶段本来就在 IDENTITY）。
    """
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
    if _verify_identity(session):
        identity_cfg = config_loader.stage_config("IDENTITY")
        return _reply(session, _soothe(identity_cfg.get("reply_ok", ""), flags),
                      next_stage="NEED", intent="identity_ok")
    # P4：上游掉线不能走「报了但核不上 → 换槽位再问」这条路。
    failed = _lookup_failed(session)
    if failed:
        return failed
    if took:
        # 报了但对不上：问另一个槽位。刚报过的那个再问一遍是最没信息量的一句
        # （用户会以为你没听见他说话）。
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
            _confirm_q = _fill_outbound(
                config_loader.stage_config("GREETING").get("reply_confirm_company")
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
    if flags.get("evidence") == "noise":
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
        return _reply(session,
                      _fill_application(_soothe(cfg.get("reply_ok", ""), flags), app),
                      next_stage="NEED", intent="identity_ok")

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
        session["noise_turns"] = int(session.get("noise_turns", 0)) + 1
        if session["noise_turns"] >= NOISE_MAX:
            return _finish(session,
                           _identity_text("reply_noise_final",
                                          "可能这边信号不太好，我先不打扰您了，稍后会有专员与您联系，再见。"),
                           "no_voice")

        # ① 明确拒绝提供身份信息（不报/不说/凭什么/不告诉）：
        #    不能追着要，也不能一句「再见」挂掉——先解释为什么核对，
        #    并给替代路径（订单号 / 姓名 / 尾号任一）。两次仍拒绝才收尾。
        if _refuse_identity(text):
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
            return _reply(
                session,
                _identity_text("reply_ask_identity",
                               "您问的这个问题我记下了，稍后专员给您详细解答。"
                               "这边先把身份核对一下，方便说一下手机号后四位吗？"),
                intent="identity_ask")

        # ③ 跑题 / 絮叨（有实义、既不是提问也不是拒绝）：中性承接 + 拉回话题。
        return _reply(
            session,
            _identity_text("reply_offtopic",
                           "嗯，您说的我记下了。咱们先把身份核对一下，方便说一下手机号后四位吗？"),
            intent="identity_offtopic")

    session["noise_turns"] = 0              # 人在回答了，噪声累计归零
    session["identity_attempts"] += 1

    # ③ 问最缺的那一个；两个都问满了就收尾
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


def _stage_need(session, text, flags):
    """NEED：先查业务数据，再问诉求。绝不能"没查就问"。

    顺序很关键：进入本阶段先调 query_order 拿真实订单，
    有订单就说订单细节（可溯源），没订单就走通用回访话术。
    """
    cfg = config_loader.stage_config("NEED")
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
                # 这时没有新的具体问题可答，正确动作是**推进到确认**，
                # 而不是用"换个语气再念一遍"制造进展的假象。
                if session.get("official_line_spoken"):
                    _cc = config_loader.stage_config("CONFIRM")
                    _after = _cc.get("reply_after_ask") or "您看这样解释清楚了吗？"
                    return _reply(session, _soothe(_after, flags),
                                  next_stage="CONFIRM", intent="confirm_after_ask")
                session["official_line_spoken"] = True
                return _reply(session, _soothe(_off, flags), intent="official_script")
            return _reply(session,
                          _fill_application(_soothe(cfg.get("reply_application_detail",
                                                           "您这笔申请的驳回原因和需补材料我这边都有记录，我再说一遍：{reason}，需要补充{materials}。"), flags), app),
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
    # 进了 OFFER 就说明"这份权益"已经被提过一次，后面所有"那个""这个"都能落到它身上
    _note_referent(session, "benefit", "这份权益")

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

    # 提问：官方 QA 原话 → 知识条目（带出处）→ 配置里的固定话术
    if flags["ask"]:
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
    return _reply(session, _soothe(confirm.get("reply", ""), flags),
                  next_stage="CONFIRM", intent=_intent_name(flags))


def _stage_confirm(session, text, flags):
    """CONFIRM：拿到明确答复。

    规则：明确肯定 → true；明确否定 → false；其他一律算含糊。
    含糊**先追问一次**（"您可以简单回我一句同意或者不需要"），
    第二次仍含糊才记 unknown——宁可记"不确定"，也不能把"嗯"当成同意。

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

    # 用户明确说没听懂：不回写同意、也不记拒绝，先把复述再讲一遍。
    # 第二次仍没听懂就转专员收线，**不产生回写事件**——回写会把
    # 「客户同意 / 拒绝」写进工单，而这通电话客户只是没听清，
    # 记成任何结论都是编的。也避免同一句追问反复念（agent_loop）。
    if any(w in text for w in NOT_CLEAR_MARKS):
        if session.get("reexplain_used"):
            return _finish(session,
                           cfg.get("reply_not_clear_handoff")
                           or "那我先不打扰您了，稍后专员会用官方号码再联系您，"
                              "需要补充的材料清单也会同步到商家后台，感谢您的接听，再见。",
                           "unclear")
        session["reexplain_used"] = True
        return _reply(session, cfg.get("reply", ""),
                      next_stage="CONFIRM", intent="ambiguous")

    # 场景确认词（pop："清楚了""明白了"）。CONFIRM 问的是什么由场景话术决定：
    # 主场景问"同意吗"，pop 问"清楚了吗"——通用 YES 词表覆盖不了所有场景问法
    # （"清楚了"不在任何肯定表里，"知道了"在通用含糊表里），于是商家明明说
    # 清楚了却被追问"您可以简单回我一句"。场景包用 confirm_yes_words 显式
    # 声明本场景的确认说法，命中即视同明确肯定；主场景不配这个键，行为不变。
    # 子串匹配要防否定变体："没明白"同样包含"明白"，必须先拦。
    _yes_marks = [w for w in (cfg.get("confirm_yes_words") or []) if w and w in text]

    def _negated(mark):
        # 只看确认词前面一小段：「没听明白」中间隔了字，旧的连续串匹配抓不到；
        # 但也不能扫全文，「不是，我清楚了」是先否认后肯定，扫全文会误伤。
        i = text.find(mark)
        return i >= 0 and any(neg in text[max(0, i - 4):i] for neg in ("没", "不", "别"))

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
    if flags["ask"]:
        blocked = _promise_block(session, text, flags)
        if blocked:
            return blocked
        _ans, _qa_hit, _kb_hit = _answer_official(session, text, flags)
        if _ans:
            _tail = (cfg.get("reply_after_ask")
                     or "您看这样解释清楚了吗？")
            return _reply(session, "%s %s" % (_ans, _tail),
                          intent="qa" if _qa_hit else "ask",
                          qa_hit=_qa_hit, kb_hit=_kb_hit)

    session["confirm_asks"] += 1
    if session["confirm_asks"] < 2:
        return _reply(session, _soothe(cfg.get("reply_again", cfg.get("reply", "")), flags),
                      intent="ambiguous")
    return _do_writeback(session, "unknown")


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
            return _reply(session, "%s %s" % (_ans, cfg.get("reply_ask") or cfg.get("reply", "")),
                          intent="qa" if _qa_hit else "ask",
                          qa_hit=_qa_hit, kb_hit=_kb_hit)
        return _reply(session, cfg.get("reply_ask") or cfg.get("reply", ""), intent="ask")

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

    规则：第一次重问一句，连续第二次仍无应答就收尾挂断。
    这条路径以前是死代码——app.py 把空输入替换成"（空输入）"这个非空字符串，
    于是永远走不到这里。现在 app.py 会如实传空串，并由它记 no_answer 事件。

    _todo 真实实现应区分两种情况：完全没声音（网络/静音）vs 有声音但 ASR 没识别出字。
    后者不该直接挂断，应该重播一次并提示"没听清"。
    """
    cfg = config_loader.silence_config()
    if session["empty_count"] >= 2:
        return _finish(session, cfg.get("reply_final", "看来信号不太好，稍后再联系您，再见。"), "silence")
    return _reply(session, cfg.get("reply", "您还在吗？"), intent="empty")


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
            or "好的，我帮您转接人工同事，稍后会有专人和您联系，感谢您的接听，再见。")
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

    商家在电话里一般只报字号（"川上"）或店名（"川上官方旗舰店"），
    很少逐字念全称（"北京市川上商贸有限责任公司"），所以三个都收，
    另外把全称的前若干字也算上（"北京市川上"）。
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
    """把开场白里的 {company_name} / {contact_role} 填成外呼目标的真值。

    为什么要有这一步：官方 52 条话术的固定格式是
    "您好，我是京东商家资质审核专员，请问您是（商家公司名/店铺名）…吗？"——
    **带公司名**。我们原来写的是"请问是贵公司的负责人吗？"，既没公司名，
    又让人无从判断这通电话是找谁的。商家凭什么相信一个陌生来电说"你被驳回了"？

    没有外呼目标时（自检、名单取空）原样返回，不留花括号给用户听。
    """
    if not target or not text:
        return text
    name = (target.get("company_name") or "").strip()
    short = (target.get("company_short_name") or "").strip()
    shop = (target.get("shop_name") or "").strip()
    role = (target.get("contact_role") or "").strip()
    who = name or shop or short
    out = text
    if who:
        out = out.replace("{company_name}", who)
    if role:
        out = out.replace("{contact_role}", role)
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
    session["turn"] += 1
    session["slots"]["last_user_text"] = text
    # 性格证据：只看这一句用户话的字面（不读 stage / intent / 画像标签）。
    # 放在所有分支之前，空输入与直接收尾的路径也能正确累积证据。
    persona.observe(session, text)

    if not text:
        session["empty_count"] += 1
        # 空输入没有"意图"可言。显式归零，避免上一轮的高置信度被这一轮复用，
        # 事件里出现"用户没说话但 conf=0.95"这种没法解释的数。
        session["last_intent_conf"] = 0.0
        out = _handle_empty(session)
        _remember(session, "", out)
        return out
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
    session["last_user_text"] = text
    session["last_flags"] = flags
    session["last_user_angry"] = bool(flags.get("angry"))

    # 全局：要求转人工。放在挂断判断之前——"我要真人"和"别打了"是两种相反的诉求，
    # 前者要承诺有人接手，后者要立刻结束；同时出现时以转人工为准（用户还在听）。
    # 原因码沿用 W7 定下的 "handoff"（用户主动要求），
    # 这里不改是因为 3 号的用例与看板已按这个值统计；新增的"听不懂"那条分支
    # 用 "handoff_unclear" 区分，两者在事件里能分开归因。
    if flags["handoff"] and session["stage"] not in ("WRITEBACK", "DONE"):
        out = _handoff(session)
    # 全局：要求挂断。注意与"明确拒绝"区分——两者同时出现时按拒绝处理，
    # 因为拒绝是业务结果，必须回写留痕（"不需要，别打了"属于这一类）。
    elif flags["hangup"] and not flags["no"] and session["stage"] not in ("WRITEBACK", "DONE"):
        out = _finish(session, config_loader.stage_config("GREETING").get("reply_hangup", ""), "hangup")
    else:
        # 记忆回溯放在挂断之后、阶段处理之前：它是"回答问题"，
        # 不该改变流程；也不该被某个阶段的处理器截走当成新诉求。
        out = _recall(session, text) if _is_recall_question(text) else None
        if out is None:
            handler = _HANDLERS.get(session["stage"], _stage_done)
            out = handler(session, text, flags)

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
