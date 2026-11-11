# -*- coding: utf-8 -*-
"""模型客服：由模型决定「这一句说什么」，规则只留闸门。

## 为什么另起一层，而不是继续加厚 `nlg.assist`

`assist` 的输入是**已经定死的骨架**：

    好的，收到。咱们回到正题，您可以简单回我一句清楚了或者还有疑问。

客户刚说「这号我早报过三遍了」。骨架里根本没有「承认重复」这个选项，
模型再强也只能把这句话说得客气一点——它能改语气，改不了内容。

真正的问题是**说什么由规则定**。规则能枚举的形状有限，客户一换说法就露馅：
「刷不出来」不在障碍词表里、「你到底是谁」不含「你谁」、「是我公司」被当成
「是我本人」。这些不是靠补词表能补完的——词表永远差一条。

所以这一层的输入是「本环节目标 + 本单事实 + 最近对话 + 客户刚说」，
输出是一句自由组织的话。骨架只在模型失败时兜底。

## 三条不许越的边界

1. **事实只能来自本单档案 / 官方口径**。`nlg._facts_ok` 逐个数字卡：
   模型编一个金额、一个日期、一条政策，这一句就丢掉，退回骨架。
2. **流程不由模型改**。`next_stage` / 写工单 / 收线原因仍由规则状态机决定，
   模型只决定这一句怎么说。否则 88 条验收的闭环口径立刻失去意义。
3. **任何异常都退回骨架**。超时、返回空、带禁语、太短、和上一轮逐字相同，
   全部返回 None，调用方照旧播规则话术——外呼里没声比话说得难听严重得多。

## 和不做的部分

- 命中官方 QA 原话的轮次不走这里（`_freeze_official`）：官方原话必须逐字，
  转述就成了「我方口径」，被追问依据时站不住。
- 核身问句不走这里：指代必须固定（「预留手机号后四位」不能漂成「说个后四位」）。
- 这两类轮次仍由 `nlg`/骨架负责，占比不高，剩下的由这里接管。
"""

from __future__ import annotations

import io
import re

import config_loader

from . import llm, nlg

#: 模型说这些话等于越界：承诺通过/减免、索要完整证件、冒充官方背书。
_BANNED = ("保证通过", "一定能过", "包过", "百分百", "免除", "免费帮您办",
           "身份证号", "银行卡号", "密码", "验证码", "转账", "加微信", "私下",
           # 自曝无能。模型一不确定就爱说"这个我确实没法确认，稍后让专员回电"，
           # 而这一轮客户问的恰恰是本单档案里就有答案的事（号码哪来的、
           # 你们是谁）——说了这句，等于把"客服太傻"从规则层搬到了模型层。
           # 拦住它，退回骨架里那句准备好的正面回答。
           "确认不到", "没法确认", "无法确认", "查不到", "查不了", "不知道")

#: 各环节的目标。写死在这里而不是读 `_why`：`_why` 是给开发看的内部讨论
#: （"此前写……属于指代不明"），模型读了会把内部考虑说出来。
_GOALS = {
    "GREETING": "自报身份（京东资质审核），确认对方是不是这家公司的负责人，"
                "问出对方称呼。**不要**在这一步讲驳回原因。",
    "IDENTITY": "核对来电号码是不是商家申请时预留的，只核后四位；"
                "说明为什么要核对（防止把别人的资质情况讲错人）。",
    "NEED": "说明这笔申请没有通过审核，以及**具体原因**；先讲原因，不要一上来就念材料清单。",
    "OFFER": "说清要补哪几样材料、在商家后台重新提交；材料名要具体。"
             "不要承诺什么时候能审完、不要承诺一定通过。",
    "CONFIRM": "复述要点后确认商家听清楚了（问「清楚了吗」这一类），"
               "**不要**问「同意吗」（这一通不是营销，是通知）。",
}

#: 档案里**可以说**的字段。白名单，不是黑名单——
#: 身份证号、完整手机号、银行卡这些永远不进 prompt，进了就有被念出来的可能。
_FACT_FIELDS = (
    ("company_name", "公司名"),
    ("company_short_name", "公司简称"),
    ("contact_name", "联系人"),
    ("contact_role", "联系人职务"),
    ("brand_name", "品牌"),
    ("category_l1", "一级类目"),
    ("reject_reason", "驳回原因"),
    ("required_materials", "需补材料"),
    ("application_id", "申请单号"),
    ("phone_tail", "预留手机号后四位"),
    ("recheck_deadline", "补正截止日期"),
    ("application_type", "申请类型"),
)

_SYSTEM = (
    "你是京东商家资质审核的外呼坐席，正在给一位商家打电话。"
    "说话像真人：口语、简短、一句话说完一件事，不用书面语，不用分点。\n"
    "铁律：\n"
    "1. 只能使用【本单事实】里的信息和【官方口径】，不许编造金额、时限、材料名、政策条款；\n"
    "2. 不许承诺「一定能通过」「帮您免除」这类结果；\n"
    "3. 不许索要完整身份证号、银行卡号、密码、验证码；只核对手机号后四位；\n"
    "4. 不许重复上一轮已经说过的话，要接着客户刚说的那句往下说；\n"
    "4b. **核身通过之前**，绝对不能把客户的手机尾号、申请单号、姓名念出来"
    "（那正是要核的东西）；也不能说「对上了」——客户还没报；\n"
    "5. **只有客户本条消息里**报了核对信息（尾号/单号/姓名），才先用半句确认"
    "（比如「尾号9403，对上了」），再往下说；不要从档案里挑一个值假装是客户刚说的，"
    "也不要把同一句确认说第二遍；\n"
    "6. 不要每轮都问「清楚了吗」。客户在提问或抱怨时，先回答他的问题，不要追问表态；"
    "只有在你完整复述了要点、确实需要对方确认时才问；\n"
    "7. 同一个问题客户问第二遍，回答要比第一遍**短**，只补他没听清的那部分，"
    "不要再把整段材料清单念一遍；\n"
    "8. 客户问「怎么补」「补哪些」「在哪提交」「后台哪个入口」时，直接答："
    "驳回原因 + 要补哪几样 + 到商家后台的资质审核页重新提交。"
    "**不要反问**「您是想让我再讲一遍吗」，也不要说「确认不到」——"
    "这三件事档案里都写着；\n"
    "9. 客户问「你哪来的号码」「你是谁」「是不是骗子」时，就答：这是您提交申请时预留的"
    "联系方式，我们是京东官方审核，只用于核对这笔资质申请，不会外传。**不许说**"
    "「我确认不到」「没法确认」这类话——这些事档案里就有答案；\n"
    "10. 只输出你要说的那句话本身。不要解释、不要加引号、不要加「客服：」、不要分点。\n"
    "长度：一到两句，不超过 80 字。"
)


def enabled():
    """这一层开没开。配置 + 环境两道闸，任一关掉就不调模型。

    默认关是刻意的：队友 clone 下来没密钥也要能跑，自检与跑批也不能每跑
    一次调几十次模型（钱和时间都受不了）。

    环境闸有两道：
        AGENT_FREE=0  跑批/自检显式关闭（模型每次说的话不一样，验收要可复现）
        LLM_DISABLE=1 整个模型层关掉时，这一层自然也不该还去敲网络
    """
    import os
    if (os.environ.get("AGENT_FREE") or "").strip() == "0":
        return False
    if (os.environ.get("LLM_DISABLE") or "").strip() in ("1", "true", "yes"):
        return False
    try:
        # 用 load() 而不是场景文件：load() 已经做过主配置 + 场景包的合并，
        # 直接读文件会漏掉场景覆盖（这里踩过一次：写成 scene() 直接 AttributeError，
        # 被 except 吞成 False，表现是"开关打开了但一句都不生效"）。
        return bool((config_loader.load().get("agent") or {}).get("llm_reply"))
    except Exception:
        return False


_LINES_CACHE = {}


def _lines():
    """读录制库（config/scenes/<场景>_lines.json）。读一次缓存住。

    这份文件是 `scripts/record_agent.py` 的产物：模型客服真跑过的话，
    按环节汇总。回放模式（AGENT_FREE=replay）从这里取话——
    **不调模型、可逐字节复现**，而说的仍然是模型组织过的人话。
    """
    if "d" in _LINES_CACHE:
        return _LINES_CACHE["d"]
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "mock", "agent_lines.json")
    data = {}
    try:
        with io.open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    # 录的可能不止一个场景：只取当前场景那批。
    scene = os.environ.get("APP_SCENE") or "pop_reject"
    if data.get("scene") and data["scene"] != scene:
        data = {}
    _LINES_CACHE["d"] = data
    return data


def _replay(stage, user_text):
    """在录制库里找最像这一轮的一条。找不到就返回 None（调用方退回骨架）。"""
    import difflib
    rows = (_lines().get("by_stage") or {}).get(stage or "") or []
    best, score = None, 0.0
    for r in rows:
        s = difflib.SequenceMatcher(None, str(r.get("user") or ""),
                                    user_text or "").ratio()
        if s > score:
            best, score = r, s
    # 门槛 0.55：太不像的宁可不回放。客户换了话题还硬拿上一句的回复，
    # 就是另一种"答非所问"。
    if best and score >= 0.55:
        return str(best.get("reply") or "") or None
    return None


def _facts(session):
    """把本单档案压成几行。只放白名单字段，没值的不占行。"""
    tgt = (session or {}).get("outbound_target") or {}
    lines = []
    for key, label in _FACT_FIELDS:
        val = tgt.get(key)
        if isinstance(val, (list, tuple)):
            val = "、".join(str(x) for x in val)
        val = str(val or "").strip()
        if not val or val in ("None", "null"):
            continue
        # 申请单号只提后六位：全号念出来又长又容易被听错。
        if key == "application_id" and len(val) > 8:
            val = "…" + val[-6:]
        lines.append("%s：%s" % (label, val))
    return "\n".join(lines) or "（这一通没有可用的档案事实，只做通用沟通）"


def _recent(session, limit=6):
    """最近几轮对话。没有就返回占位串，不让 prompt 里出现空白段。"""
    mem = (session or {}).get("memory") or {}
    rows = mem.get("recent") or []
    out = []
    for r in rows[-limit:]:
        u = str(r.get("user") or "").strip()
        a = str(r.get("agent") or "").strip()
        if u:
            out.append("客户：%s" % u[:60])
        if a:
            out.append("客服：%s" % a[:60])
    return "\n".join(out) or "（这是第一轮）"


def _must_say(stage):
    try:
        cfg = config_loader.stage_config(stage) or {}
    except Exception:
        return []
    return [str(w) for w in (cfg.get("must_say") or []) if w]


def reply(session, user_text, flags, stage, base_text, official="", must=None):
    """返回模型组织的一句话；任何失败返回 None（调用方退回骨架）。

    `base_text` 只作为「本轮建议表达」进 prompt，不是必须照抄的内容——
    它是规则层算出来的"这一轮该表达什么意思"，模型可以换说法，但不能换成
    别的意思（换跑题了由必答要素与事实闸门拦下）。
    """
    if not base_text:
        return None

    import os
    # 回放模式：用录制下来的话，不调模型。演示可复现、跑批可验收。
    if (os.environ.get("AGENT_FREE") or "").strip() == "replay":
        got = _replay(stage, user_text)
        if not got:
            return None
        return _gates(session, got, base_text, stage, must, official, user_text)

    if not enabled() or not llm.available():
        return None

    # 这里原来是「骨架里含必答词就交回骨架」。**这条是错的**：
    # 必答词是"核对""重新提交""复述"这种业务常用词，几乎每条骨架都有，
    # 结果整通电话只有零星两三轮交给模型，客户听到的还是骨架。
    # 用户看完自己在页面上打的那通，问的是"你真的接入大模型了吗"——
    # 11-11 实测：13 轮里模型只说 2 轮。
    #
    # 正确做法不是"有硬要素就不让模型说"，而是**让模型说、再检查要素在不在**
    # （第 7 道闸门 _content_ok）。判据从"骨架含不含词"换成"输出有没有这件事"。
    must = _must_say(stage)
    prompt = (
        "【本环节】%s\n【本环节目标】%s\n【本环节必须说到】%s\n"
        "【本单事实】\n%s\n【官方口径】%s\n【最近对话】\n%s\n"
        "【客户刚说】%s\n【本轮建议表达（意思要对，说法可以改）】%s"
        % (stage or "（未知）",
           _GOALS.get(stage or "", "按电话的上下文自然回应，不要跑题。"),
           "、".join(must) or "（无硬性要求）",
           _facts(session),
           (official or "（本轮没有命中官方口径）")[:200],
           _recent(session),
           (user_text or "（客户没说话）")[:100],
           base_text[:160])
    )

    meta = llm.chat([{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": prompt}],
                    temperature=0.5, max_tokens=200, timeout=2.5,
                    tag="agent_free")
    if not meta.get("ok"):
        return None

    text = _clean(meta.get("text") or "", 80)
    session["agent_free_raw"] = text
    if not text:
        session["agent_free_reject"] = "empty"
        return None
    text = _drop_invented_ack(text, user_text)
    if not text:
        return None
    return _gates(session, text, base_text, stage, must, official, user_text)


def _drop_invented_ack(text, user_text):
    """砍掉"无中生有的确认"。

    模型很爱拿档案里的尾号当成"客户刚说的"来确认：连着四轮都以
    「尾号9403，对上了」开头，而客户那几轮根本没提尾号。相似度闸门拦不住
    ——整句并不相同，只有这个前缀相同。客户听到的却是同一句假确认。

    判据很窄：**第一个分句**里有 4 位数字，而客户这条消息里没有这个数字，
    就把这个分句砍掉。砍完不足 8 字就整句丢掉（退回骨架）。
    """
    t = (text or "").strip()
    head = re.split(r"[，。；！？]", t)[0]
    digits = re.findall(r"\d{4}", head)
    if not digits or any(d in (user_text or "") for d in digits):
        return t
    rest = t[len(head):].lstrip("，。；！？ ").strip()
    # 砍完可能剩一个孤零零的「对上了。」——那是同一句确认的残骸，
    # 留着比整句不说更怪（"对上了。页面打不开先别折腾"——对上什么了？）
    while rest and re.match(r"^(对上了|没错|对的|是的|对)[，。；！？]", rest):
        rest = re.sub(r"^(对上了|没错|对的|是的|对)[，。；！？]\s*", "", rest, count=1).strip()
    return rest if len(rest) >= 8 else ""


def _clean(text, max_chars=80):
    """洗掉模型爱加的东西：角色前缀、引号、换行、多余空白，超长按整句截。

    不做"尽力保留"：截不出一句完整的话就返回空串，让上层退回骨架。
    半句话（"……需要您补充品牌授权书、"）比骨架难听得多。
    """
    t = (text or "").strip()
    t = re.sub(r"^(客服|坐席|助理|回答|回复|话说)\s*[:：]\s*", "", t)
    t = t.strip("「」『』\"'“”‘’ \n\t")
    t = re.sub(r"\s+", " ", t.replace("\n", " ")).strip()
    if len(t) <= max_chars:
        return t
    out = ""
    for seg in re.split(r"(?<=[。！？])", t):
        if len(out) + len(seg) > max_chars:
            break
        out += seg
    return out.strip()


#: 每个环节"必须表达到的意思"。这是**语义**判据，不是逐字匹配——
#: 合规要求是"这一轮要说清是什么事"，不是"必须出现'复述'二字"。
def _bigrams(t):
    cn = re.sub(r"[^\u4e00-\u9fff]", "", t or "")
    return {cn[i:i + 2] for i in range(len(cn) - 1)}


def _leaks_secret(session, text):
    """核身还没过，就把档案里的核对值念出来——这是**嘴漏**。

    11-11 实测（截图那通）：客户还没报号，模型连着说
    「您预留的手机号后四位是9403，对上了」「申请时预留手机号后四位是9403」。
    两个问题叠在一起：
      1. 客户一个字都没确认，客服说"对上了"——无中生有的确认；
      2. **把核身答案念给了对方**。核身问的就是这个值，先念出来等于自己把门打开，
         之后谁接电话都能对上。
    这条闸不看句子结构，只看"核身通过前，档案里的尾号/单号有没有出现在话里"。
    """
    slots = (session or {}).get("slots") or {}
    if slots.get("identity_confirmed"):
        return False
    rec = (session or {}).get("outbound_target") or {}
    tail = re.sub(r"\D", "", str(rec.get("phone_tail") or ""))[-4:]
    app_id = re.sub(r"[^0-9A-Za-z]", "", str(rec.get("application_id") or ""))
    if tail and tail in (text or ""):
        return True
    # 单号后六位也算（模型爱说"申请单号后六位是060019"）。
    if len(app_id) >= 6 and app_id[-6:] in (text or ""):
        return True
    return False


def _same_topic(base, text):
    """说的是不是同一件事。

    以前这里调 `agent_llm._same_intent`：从骨架里挑出业务动词，要求输出命中
    同一个。实测太脆——骨架「…我按公司名核一遍，一样能确认是您这单」，
    模型说「…我核一下是不是您这单」：意思完全一样，但骨架里的动词是"确认"，
    模型用的是"核一下"，不命中，整句被丢。**这是判据挑词，不是判语义。**

    改成 2 字词重合度：同一件事的两种说法，重合度普遍在 0.2 以上；
    真换个话题（材料清单 → 收费标准）会掉到 0.1 以下。
    门槛 0.15 是留了余量：短句（"不着急，您直接说公司全名"）本身词少。
    """
    b, t = _bigrams(base), _bigrams(text)
    if not b or not t:
        return True
    return len(b & t) / float(len(b | t)) >= 0.15


_MATERIAL_FALLBACK = ("材料", "授权书", "译本", "公证", "营业执照", "商标", "证明")


def _materials(session):
    rec = (session or {}).get("outbound_target") or {}
    return [str(m) for m in (rec.get("required_materials") or []) if m]


def _content_ok(stage, text, session):
    """这一轮该说清的事，说清了没有。

    只守**合规要素**（自报身份、核身问句、驳回原因、材料、确认），不守措辞。
    缺了就让骨架说——骨架在这些轮次上是备好的合规话术，比模型漏说要安全。
    """
    t = text or ""
    if not t:
        return False
    if stage == "GREETING":
        # 自报身份：得说清是京东的资质审核，不能只说"您好"。
        return ("审核" in t) and ("京东" in t or "商家" in t or "资质" in t)
    if stage == "IDENTITY":
        # 核身：必须真的问了一样能核对的东西。
        return any(w in t for w in ("后四位", "尾号", "四位", "姓名",
                                    "公司名", "公司全名", "全名", "单号"))
    if stage == "NEED":
        # 驳回原因：得说出"为什么没过"。
        return any(w in t for w in ("驳回", "没有通过", "未通过", "没通过",
                                    "缺少", "不一致", "不通过", "原因"))
    if stage == "OFFER":
        # 方案：得说到材料或提交动作，不能空谈。
        mats = _materials(session)
        return any(m in t for m in mats) or any(
            w in t for w in _MATERIAL_FALLBACK + ("后台", "重新提交", "补齐"))
    if stage == "CONFIRM":
        # 确认：要么复述了材料/原因，要么明确请对方表态。
        mats = _materials(session)
        return (any(m in t for m in mats)
                or any(w in t for w in ("清楚", "确认", "同意", "明白", "材料", "不需要")))
    if stage in ("WRITEBACK", "DONE"):
        return any(w in t for w in ("记录", "登记", "短信", "再见", "后台", "同步"))
    return True


def _gates(session, text, base_text, stage, must, official, user_text=""):
    """七道闸门。任何一道不过就返回 None，让调用方退回骨架。

    这些判据都是 `nlg` 里已经跑了几百次的那几条，不另写一套——
    同一件事两套判据，迟早会分裂成两种口径。

    每道拒绝都写进 `session["agent_free_reject"]`（原因码）+ `agent_free_raw`
    （模型原话）。没有这两个字段，出问题只能看到"这一句是骨架"，看不出
    "模型说了什么、被哪道拦的"——11-11 用户问"你到底接入大模型了吗"，
    当时就是回答不了这个问题。
    """
    # 1) 事实闸：不许出现档案与官方口径之外的数字。
    # 客户这条消息本身也是事实来源：客户说"9670"，纠正当"您说的9670和
    # 预留的9403对不上"——这不是编数字，是把他刚说的话还回去。
    # 不把 user_text 算进来，模型只要一提客户报的号就被判"编造"，
    # 结果只能说一句含糊的"没关系，那方便说一下您的姓名吗"。
    sources = "\n".join([base_text, _facts(session), official or "", user_text or ""])
    if not nlg._facts_ok(sources, text):
        session["agent_free_reject"] = "facts"
        return None
    # 2) 禁语闸
    if any(w in text for w in _BANNED):
        session["agent_free_reject"] = "banned"
        return None
    # 3) 长度闸：太短等于把这一轮的信息丢了，太长外呼里听不下去。
    if not (8 <= len(text) <= 80):
        session["agent_free_reject"] = "length"
        return None
    # 4) 重复闸：最近几轮说过的话不许再说一遍。
    #    只比上一轮不够——第一版模型连着四轮都以「尾号9403，对上了」开头，
    #    每轮都不是"和上一轮逐字相同"，但客户听到的是同一句复读四遍。
    #    相似度过 0.75 就算说过（同一句话换个标点不算新话）。
    import difflib
    # 自己记一份"模型最近说过的句子"。原来只读 session["memory"]["recent"]，
    # 而 /api/turn 这条链路上 memory 没被维护，等于去重闸常年空转——
    # 11-11 实测第 11 轮和第 13 轮说了逐字相同的一句话，闸门没拦住。
    recent = [session.get("last_reply_text") or ""]
    recent.extend((session.get("agent_free_recent") or [])[-5:])
    for row in ((session.get("memory") or {}).get("recent") or [])[-5:]:
        recent.append(str(row.get("agent") or ""))
    for prev in recent:
        if prev and difflib.SequenceMatcher(None, prev, text).ratio() >= 0.75:
            session["agent_free_reject"] = "repeat"
            return None
    # 5) 跑题闸：这一轮要表达的意思，模型不能换成另一件事。
    #    不用 must_say 逐字比对（那是"卡词不卡意思"，第一版就是这么把
    #    整通 CONFIRM 拦光的）：改用骨架与输出的动词重合度，
    #    骨架说"接住页面打不开"，模型说"材料清单我同步到后台"——算同一件事；
    #    骨架说"确认材料"，模型开始讲收费标准——这就拦下。
    if not _same_topic(base_text, text):
        session["agent_free_reject"] = "intent"
        return None
    # 6) 结尾不许挂半句标点（TTS 会把"、"念成停顿，听起来像断线）。
    if re.search(r"[、，；]$", text):
        session["agent_free_reject"] = "punct"
        return None
    # 7) 内容闸：这一环节该说清的事（自报身份/核身问句/驳回原因/材料/确认）
    #    说清了没有。这是"硬要素"的正确落法——不靠骨架含不含词，
    #    只看说出来的这句话有没有这件事。
    # 8) 嘴漏闸：核身没过就不许念档案里的核对值（见 _leaks_secret）。
    if _leaks_secret(session, text):
        session["agent_free_reject"] = "secret"
        return None
    if not _content_ok(stage, text, session):
        session["agent_free_reject"] = "content"
        return None
    session["agent_free_reject"] = ""
    _hist = list(session.get("agent_free_recent") or [])
    _hist.append(text)
    session["agent_free_recent"] = _hist[-8:]
    return text
