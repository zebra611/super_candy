# -*- coding: utf-8 -*-
"""会话语言：中文 / 英文互切（11-12 用户要求）。

为什么不是"翻译层"：状态机、意图识别、赛题话术全是中文，硬翻一句
"yes" 既翻不出意图，也会把合规口径翻走。这里的做法是两段式：

    ① 英文短句 → 中文等价句（只换意图，不换业务），喂给现有中文状态机；
    ② 状态机照常算出中文回复，再按 (intent / stage) 换成英文话术播出。

所以英文模式下**业务流程一步不少**：确认公司 → 核身 → 讲事实 → 确认 → 回写。
英文话术是我们对照中文官方口径写的英文表述，不是官方英文原文（官方资料包
只有中文），材料名沿用商家后台的中文名称，便于客户在后台逐项对照。

切回中文的两种触发（用户明确要求）：
    - 句子里出现中文；
    - 明确说 "i can't understand english" / "can you speak chinese" 等。
"""
from __future__ import annotations

import re

_ZH = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")

#: 明确要求切回中文（句子本身可能是英文）
BACK_TO_ZH = (
    "i can't understand english", "i cant understand english",
    "i cannot understand english", "i don't understand english",
    "i dont understand english", "can you speak chinese",
    "could you speak chinese", "please speak chinese", "speak chinese",
    "no english", "in chinese please",
)

#: 明确要求用英语（此时即使上一轮是中文也切到英文）
ASK_EN = (
    "can you speak english", "do you speak english", "could you speak english",
    "speak english", "english please", "in english please",
)

#: 英文短句 -> 中文等价句。顺序即优先级：先长后短，避免 "no" 抢走 "no thanks"。
#: 只收**能明确对应到意图**的短句；长句保留原文，走通用英文承接。
_EN_INTENT = (
    (("hold on", "hold on hold on", "wait a moment", "one moment", "just a second"), "稍等"),
    (("what materials", "which documents", "what do i need", "what should i prepare",
      "what documents"), "材料"),
    (("what's this about", "what is this about", "what happened", "what for",
      "why are you calling"), "怎么了"),
    (("what do you mean", "i don't understand", "i dont understand", "sorry what"), "啥意思"),
    (("yes i am", "that's me", "thats me", "speaking", "yes speaking", "i am"), "是的，是我"),
    (("yes", "yeah", "yep", "yup", "sure", "ok", "okay", "right", "correct",
      "alright", "got it", "i understand"), "是的"),
    (("no thanks", "wrong number", "not me", "no", "nope", "nah"), "不是"),
    (("thank you", "thanks", "thanks a lot"), "清楚了"),
)

#: 状态机 intent -> 英文话术。占位符由 dialogue._fill_application /
#: _fill_outbound 填（{application_desc}/{reason}/{materials}/{company_name}）。
EN_REPLIES = {
    "greeting_confirm_company": (
        "Hello, this is JD merchant qualification review. Am I speaking with {company_name}?"),
    "identity_retry": (
        "To confirm this application, please tell me the last four digits of the "
        "phone number you registered."),
    "identity_ok": (
        "Thanks, that's confirmed. Your {application_desc} application was not approved "
        "because {reason}. You need to provide {materials}. Let me go through it with you."),
    "official_script": (
        "Your {application_desc} application was not approved because {reason}. "
        "Please provide {materials} and resubmit in the merchant backend."),
    "unclear": (
        "Sorry, could you tell me whether you want the reason, or the list of documents?"),
    "materials_only": (
        "You need to provide {materials}. The checklist is already in your merchant backend, "
        "so you can check it anytime."),
    "what_auth": (
        "The authorization means the chain of documents from the brand owner to the "
        "distributor and then to you. Your case: {reason}. You need {materials}. "
        "The checklist is in your merchant backend."),
    "scope_answer": (
        "All the documents are required and all three steps must be done: prepare "
        "{materials}, upload them in the merchant backend, then resubmit. "
        "Which step would you like me to explain?"),
    "checklist_sent": (
        "Done, I've sent the checklist to your merchant backend: {materials}. "
        "If a step is unclear or you forget what to prepare, just check the checklist there."),
    "consent_checklist": (
        "Thanks. I've recorded the reason and the document requirements, and the "
        "checklist is in your merchant backend for you to check anytime. Goodbye."),
    "consent": (
        "Thanks for your time. I've recorded the reason and the document requirements. Goodbye."),
    "declined": (
        "Understood. I'll note this down and have a specialist contact you. Goodbye."),
    "done": "Thanks for your time. Goodbye.",
    "help_steps": (
        "Here are the steps: first prepare {materials}; then log in to the merchant "
        "backend, find the qualification review page and upload them; finally resubmit. "
        "Which step is unclear?"),
    "hurry_short": (
        "In short: provide {materials} and resubmit in the merchant backend."),
    "scam_reassure": (
        "I understand your concern. This is JD's official qualification review. "
        "We will never ask you to transfer money or share passwords. You can verify "
        "this via the JD merchant hotline 4006180092."),
    "repair": (
        "Sorry, let me say that again: your {application_desc} application was not "
        "approved because {reason}; you need to provide {materials} and resubmit."),
    # 语言确认这一句不塞中文公司名：客户只是问"能不能说英语"，
    # 回一句纯英文的承接最自然；身份确认仍在下一轮按赛题②环报单位名。
    "lang_en": (
        "Sure, I can continue in English. Let's confirm which account we're calling about first."),
}

#: intent 没命中时按阶段兜底，保证英文模式下不冒出中文句子。
EN_BY_STAGE = {
    "GREETING": "Hello, this is JD merchant qualification review. Am I speaking with {company_name}?",
    "IDENTITY": "To confirm the application, please tell me the last four digits of the registered phone number.",
    "NEED": ("Your {application_desc} application was not approved because {reason}. "
             "You need to provide {materials} and resubmit in the merchant backend."),
    "OFFER": ("You need to provide {materials} and resubmit in the merchant backend. "
              "The checklist is in your merchant backend."),
    "CONFIRM": "Is everything clear? You can just say yes, or ask me a question.",
    "WRITEBACK": "Thanks. I've recorded everything and the checklist is in your backend. Goodbye.",
    "DONE": "Thanks for your time. Goodbye.",
}

EN_FALLBACK = (
    "Sorry, could you repeat that? If it's easier, a specialist can call you back, "
    "or you can reach the JD merchant hotline 4006180092.")

#: 切回中文时的第一句（不走英文渲染）
ZH_BACK = "好的，那咱们改用中文说。您想问哪一步，我直接讲给您听。"


def _norm(t):
    t = (t or "").strip().lower()
    t = re.sub(r"[^a-z0-9' ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def detect(text, current="zh"):
    """判断这一轮该用哪种语言。返回 "zh" / "en"。

    中文字符优先（"我听不懂 english" 也算中文）；纯英文短句且含拉丁字母才切 en；
    两者都不是（数字、标点）时保持当前语言，不让一次噪声把语言切走。
    """
    t = (text or "").strip()
    low = _norm(t)
    if any(k in low for k in BACK_TO_ZH) or _ZH.search(t):
        return "zh"
    if any(k in low for k in ASK_EN):
        return "en"
    if len(_LATIN.findall(t)) >= 2 and low:
        return "en"
    return current or "zh"


def wants_chinese(text):
    """英文句子里明确要求切回中文（不是"句子里带中文"）。"""
    return any(k in _norm(text) for k in BACK_TO_ZH)


def asks_english(text):
    return any(k in _norm(text) for k in ASK_EN)


def to_zh(text):
    """英文短句 -> 中文等价句；没匹配上返回 None（保留原文走通用承接）。"""
    t = _norm(text)
    if not t:
        return None
    for words, zh in _EN_INTENT:
        for w in words:
            if t == w or t.startswith(w + " ") or t.endswith(" " + w):
                return zh
    return None
