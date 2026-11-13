"""客服的"表达增强层" —— 让坐席答到点上，而不是把骨架念一遍。

**它和 `nlg.py` 的分工（别混）**
    nlg.py      一句话、45 字、600ms，只换语气。它的位置在最热的链路上，
                所以宁可话说得朴素，也不能慢、不能错。
    agent_llm.py（本文件）
                允许 1-2 句、70 字、接受 2.5s。它**只在需要"讲清楚"的那一轮**
                才被调用：客户追问了、客户带着情绪、命中了官方口径。
                其余轮次仍然走 nlg 的快速改写，整通电话的时延不会被拖垮。

**为什么客服"不智能"其实是表达问题，不是知识问题**
    项目里能播的事实早就齐了：120 张申请单、68 条官方口径、27 条官方 QA、
    52 条驳回话术。问题在于过去只有两种出口：
        · 骨架话术（"您这个申请是品牌授权链路不完整，需要补充品牌授权书"）
        · 官方 QA 原话（逐字播报，书面到像念文件）
    客户问"我上个月就交过授权书了，怎么还让我补"，这两种都答不到点上——
    因为他问的是"我这一单"的事，而我们播的是"这一类"的话。

    本模块做的事很具体：把**这一单的档案**（哪家公司、什么品牌、哪个原因、
    要补哪几样）+ **客户刚说的那句话** 一起给模型，让它说一句"针对这个人"的话。
    业务判断权仍然在状态机：说什么事、推进到哪一步，这里一个字都不改。

**三道闸门（全部保留，一个都不能少）**
    1. 事实：只能使用骨架、本单档案、命中的官方口径里出现过的事实与数字；
    2. 实体：骨架里的公司名/申请单号必须原样保留（`nlg._entities_ok`）；
    3. 合规：禁语、承诺、金额、期限一律拦（`nlg._facts_ok` + 本文件的 `_banned`）。

**失败一律返回 None**，调用方退回骨架/快速改写。外呼里"话说得朴素"是
可接受的失败，"说了不该说的"不是。
"""

import json
import re

import config_loader
from modules import llm

#: 客户这句话里出现这些，说明他在"问"或者"不服"，值得多花 1-2 秒讲清楚。
_QUESTION_MARKS = ("？", "?", "为什么", "为啥", "怎么", "什么", "啥", "哪",
                   "多久", "多少", "收费", "要钱", "花钱", "材料", "补什么",
                   "凭什么", "能不能", "是不是", "有没有")
#: 情绪标记。带情绪时先接住，再讲正事——这是人工坐席和机器最大的差别。
_EMOTION_MARKS = ("你们", "又", "投诉", "烦", "催", "什么意思", "搞什么",
                  "说了几遍", "听不懂", "没用", "糊弄")

#: 硬禁语：任何情况下都不许由本层生成。合规检查在 app.py 还有一道，
#: 这里是"生成时就别往那个方向走"，两道不冲突。
_BANNED = ("保证", "百分百", "一定能", "肯定能", "绝对不会", "赔您", "补偿您",
           "退款", "私下", "内部渠道", "包过", "打点", "关系")

_SYSTEM = (
    "你是京东商家资质审核专员，正在给商家打电话。把【本轮要表达的意思】说成一句自然、"
    "贴着这个客户说的话讲的口语。\n"
    "铁律：\n"
    "1. 业务事实只能来自【本轮要表达的意思】【本单档案】【官方口径】，"
    "不得新增任何金额、期限、比例、政策条款、承诺。\n"
    "2. 客户刚说的话里有疑问或情绪，先用不超过 12 个字接住，再说本轮的正事；"
    "没疑问就直接说正事，别硬加客套。\n"
    "3. 不超过 70 个字，最多两句。不要书面语、不要括号、不要分条、不要引号、不要项目符号。\n"
    "4. 不要重复客户刚说过的话；不要追问客户已经答过的信息。\n"
    "5. 不要出现'保证''一定''百分百''退款''补偿'这类词。\n"
    "6. 不得用「您清楚…吗」「您明白…吗」「您懂…吗」「我说过了」这类把确认责任推给"
    "客户、像在考客户的说法，也不许把沟通不畅赖到客户身上（如「您没听清」）；要确认"
    "就保留「清楚了吗」，把主语放到自己这边。说坏消息先落到人身上再说事，别一开口"
    "就是「您这笔没通过」。\n"
    "7. 复述要办的事时，只讲接下来做什么，不许重讲驳回的原因和理由——原因上一轮"
    "已经说过了，再说一遍就是重复，客户会以为你没在听。\n"
    "8. 只输出这一句话，不要解释。\n"
)


def should_assist(user_text, kb=None, qa=None, stage="", flags=None):
    """这一轮值不值得多花 1-2 秒。**这个判断决定整通电话的时延**。

    判据刻意保守：
        · 命中官方口径（kb/qa）—— 有话可讲，最值得组织；
        · 客户这句话里有疑问/情绪 —— 不接住就变成"答非所问"；
        · 其余一律 false，走 nlg 的 600ms 快速改写。
    """
    if kb or qa:
        return True
    t = user_text or ""
    if any(m in t for m in _QUESTION_MARKS):
        return True
    if any(m in t for m in _EMOTION_MARKS):
        return True
    if (flags or {}).get("angry") or (flags or {}).get("complain"):
        return True
    return False


def _app_facts(session):
    """本单档案压成一段。**只放客服可以说的事实**，不放身份证明材料。"""
    slots = (session or {}).get("slots") or {}
    app = slots.get("application") or {}
    tgt = (session or {}).get("outbound_target") or {}
    rows = []
    for key, cn in (("company_name", "公司"), ("shop_name", "店铺"),
                    ("brand_name", "品牌"), ("application_id", "申请单号"),
                    ("application_type", "申请类型"), ("reject_category", "驳回类别"),
                    ("reject_reason", "驳回原因"), ("shop_type", "店铺类型"),
                    ("shop_open_date", "开店时间"), ("region", "地区")):
        val = app.get(key) or tgt.get(key)
        if val:
            rows.append("%s：%s" % (cn, val))
    mats = app.get("required_materials") or []
    if mats:
        rows.append("要补的材料：%s" % "、".join(str(m) for m in mats[:4]))
    if app.get("recheck_deadline"):
        rows.append("复核期限：%s" % app["recheck_deadline"])
    return "；".join(rows) or "（本单档案缺失，只用本轮要表达的意思）"


def _recent(session, limit=4):
    mem = ((session or {}).get("memory") or {}).get("recent") or []
    rows = []
    for r in mem[-limit:]:
        u = (r.get("user") or "").strip()
        a = (r.get("agent") or "").strip()
        if u:
            rows.append("客户：%s" % u[:40])
        if a:
            rows.append("你：%s" % a[:40])
    return "\n".join(rows) or "（无）"


def assist(session, base_text, stage="", kb=None, qa=None, user_text="",
           flags=None, max_chars=70, timeout=2.5):
    """返回增强后的一句话；任何失败返回 None（调用方退回骨架/改写）。"""
    if not base_text or not llm.available():
        return None
    if int((session or {}).get("assist_count") or 0) >= int(
            config_loader.nlg_config().get("assist_max") or 6):
        return None

    official = ""
    if kb and kb.get("answer"):
        official = str(kb["answer"])[:220]
    if qa and qa.get("answer"):
        official = str(qa["answer"])[:220]

    mood = "带情绪" if ((flags or {}).get("angry") or (flags or {}).get("complain")) else "平静"
    prompt = (
        "【当前环节】%s\n"
        "【本轮要表达的意思】%s\n"
        "【本单档案】%s\n"
        "【官方口径】%s\n"
        "【客户刚说】%s\n"
        "【客户情绪】%s\n"
        "【最近对话】\n%s"
        % (stage or "（未标注）", base_text, _app_facts(session),
           official or "（本轮没有命中官方口径）",
           (user_text or "（客户没说话）")[:80], mood, _recent(session))
    )
    meta = llm.chat([{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": prompt}],
                    temperature=0.55, max_tokens=180, timeout=timeout,
                    tag="agent_assist")
    if not meta.get("ok"):
        return None
    text = _clean(meta["text"], max_chars)
    if not text:
        return None

    # 闸门 1/2/3 —— 复用 nlg 里已经跑了几百次的判据，不另写一套。
    from modules import nlg
    sources = base_text + "\n" + _app_facts(session) + "\n" + official
    if not nlg._facts_ok(sources, text):
        return None
    if not nlg._entities_ok(base_text, text, session):
        return None
    if any(w in text for w in _BANNED):
        return None
    # 太短的话（"好的。"）等于把本轮信息丢了，退回骨架。
    if len(text) < 8:
        return None
    # 与骨架"说的不是一回事"的粗判：骨架里的关键动词至少要沾一个。
    if not _same_intent(base_text, text):
        return None

    session["assist_count"] = int(session.get("assist_count") or 0) + 1
    session.setdefault("assist_log", []).append(
        {"stage": stage, "base": base_text, "said": text,
         "ms": meta.get("ms"), "kb": (kb or {}).get("id"),
         "qa": (qa or {}).get("id")})
    return text


def _clean(text, max_chars):
    s = (text or "").strip()
    s = s.strip("\"'“”「」").strip()
    s = re.sub(r"^```.*?```$", "", s, flags=re.S).strip()
    s = re.sub(r"^(好的|嗯)[，,]\s*", "", s) if len(s) > 12 else s
    s = s.replace("\n", "，").replace("  ", " ").strip()
    if len(s) <= max_chars:
        return s
    # 只认句末标点。允许在「，；」处断会切出"…需补充品牌授权书、"这种半句，
    # 句尾挂着顿号，听的人以为断线（11-11 线上实录第 2 轮）。
    cut = s[:max_chars]
    for sep in ("。", "！", "？"):
        pos = cut.rfind(sep)
        if pos >= 6:
            return cut[:pos + 1]
    return ""


#: 骨架里的"动作词"。增强后至少要保留一个，否则就是把"让您补材料"说成了
#: "我们会关注"——话很好听，事没交代。
_VERBS = ("核对", "确认", "补充", "补", "提交", "上传", "驳回", "审核", "通过",
          "重新", "回电", "核实", "授权", "材料", "申请", "单号", "尾号", "姓名",
          "期限", "工作日", "清楚", "明白", "回执", "结果")


def _same_intent(base, text):
    hits = [v for v in _VERBS if v in (base or "")]
    if not hits:
        return True
    return any(v in (text or "") for v in hits)
