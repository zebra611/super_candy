# -*- coding: utf-8 -*-
"""会话语言：中文 / 英文互切（11-12 用户要求）。

为什么不是"翻译层"：状态机、意图识别、赛题话术全是中文，硬翻一句
"yes" 既翻不出意图，也会把合规口径翻走。这里的做法是两段式：

    ① 英文短句 → 中文等价句（只换意图，不换业务），喂给现有中文状态机；
    ② 状态机照常算出中文回复，再按 (intent / stage) 换成英文话术播出。

所以英文模式下**业务流程一步不少**：确认公司 → 核身 → 讲事实 → 确认 → 回写。
英文话术是我们对照中文官方口径写的英文表述，不是官方英文原文（官方资料包
只有中文）。

11-12 20:07 用户截图："Your 荃汀品牌、运动户外类目 application was not approved
because 授权方联系不上，需补充品牌授权书后重新提交." —— 半中半英。原因是最初
把后台中文原值直接塞进了英文模板（{reason}/{materials}/{application_desc}）。
现在改成**除专有名词以外全英文**：

    - 品牌名、公司简称、店铺名是工商登记/后台里的专有名词，保留原文（译不了，
      客户也要照这个名字在后台对照）；
    - 类目、驳回原因、材料清单、步骤说明全部走下面的词表翻成英文，
      覆盖两个档案池（applications.json 120 条 + applications_catalog.json 68 条）
      里出现的全部取值。

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

#: 明确表示"我中文不行 / 请说英文"，**允许是用中文说的**。
#:
#: 11-13 用户要求：「直到用户使用完整的英语句子或者明确说我中文不好，才转换成英语」。
#: 用中文说"我中文不好"时，`detect` 里那条"句子里有中文就判中文"会抢先把它判成 zh，
#: 所以这两组必须在判中文之前先判。
EN_REQUEST_ZH = (
    "中文不好", "中文不太好", "中文不行", "中文很差",
    "不会中文", "不会说中文", "不懂中文", "听不懂中文", "中文听不太懂",
    "说英文", "说英语", "讲英文", "讲英语", "用英文", "用英语",
    "英文沟通", "英语沟通", "换英文", "换英语",
)
EN_REQUEST_EN = (
    "my chinese is not good", "my chinese is bad", "my chinese is poor",
    "i don't speak chinese", "i dont speak chinese",
    "i can't speak chinese", "i cant speak chinese",
    "i don't understand chinese", "i dont understand chinese",
    "no chinese",
)

#: 判断"这句是不是完整的英文句子"要用到的词：主语/代词/助动词。
#: 三个词里只要出现一个，基本就是在说话（"what is this" / "i need help"），
#: 而不是蹦了个单词（"ok" / "yes" / "thanks"）。
_SUBJECT_AUX = {
    "i", "you", "we", "they", "he", "she", "it", "my", "your", "our", "their",
    "is", "are", "was", "were", "am", "do", "does", "did", "can", "could",
    "would", "will", "should", "have", "has", "had", "not",
    "don't", "dont", "can't", "cant", "isn't", "aren't", "won't",
}

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

# ===========================================================================
# 后台字段的英文词表（类目 / 驳回原因 / 材料）
#
# 这些值来自 config/mock/applications.json（评测基线 120 条）与
# applications_catalog.json（官方 68 个关单场景），词表必须覆盖两边的全部取值，
# 否则就会漏出中文——漏一个词，整句又变成半中半英。
# ===========================================================================

#: 一级类目（含跨境与"裙装/生鲜"这类斜杠组合，组合值在 _category_en 里拆开翻）
CATEGORY_EN = {
    "家用电器": "Home Appliances",
    "家居日用": "Home & Daily Necessities",
    "运动户外": "Sports & Outdoors",
    "生鲜": "Fresh Food",
    "医药保健": "Medicine & Health",
    "图书文娱": "Books & Entertainment",
    "汽车用品": "Automotive Accessories",
    "食品饮料": "Food & Beverage",
    "母婴玩具": "Mother, Baby & Toys",
    "美妆个护": "Beauty & Personal Care",
    "服饰鞋包": "Apparel, Shoes & Bags",
    "数码3C": "Consumer Electronics (3C)",
    "跨境数码3C": "Cross-border Consumer Electronics",
    "跨境服饰鞋包": "Cross-border Apparel, Shoes & Bags",
    "跨境母婴玩具": "Cross-border Mother, Baby & Toys",
}

#: 需要补的材料（两个档案池的并集）
MATERIAL_EN = {
    "品牌授权书": "the brand authorization letter",
    "逐级授权链路证明": "proof of the step-by-step authorization chain",
    "授权方营业执照": "the authorizing party's business license",
    "商标注册证": "the trademark registration certificate",
    "国家商标网状态截图": "a screenshot of the trademark status from the national trademark office",
    "境外商标注册证": "the overseas trademark registration certificate",
    "商标转让证明": "the trademark assignment certificate",
    "中文译本": "a Chinese translation",
    "公证认证文件": "notarization and authentication documents",
    "境外主体证明": "proof of the overseas entity",
    "海外采购凭证": "overseas purchase receipts",
    "购货发票": "purchase invoices",
    "营业执照": "the business license",
    "主体变更证明": "proof of entity change",
    "法人证件": "the legal representative's ID document",
    "行业许可证": "the industry license",
    "药品经营许可证": "the pharmaceutical business license",
    "质检报告": "the quality inspection report",
    "产品备案凭证": "the product filing certificate",
    "认证证书": "the certification certificate",
    "店铺认证截图": "the shop verification screenshot",
    "店铺名称变更截图": "a screenshot of the shop name change",
    "授权书店铺名称页": "the page of the authorization letter that shows the shop name",
    "重新开具授权书": "a reissued authorization letter",
    "品牌说明函": "a brand statement letter",
    "京麦后台操作回执": "the Jingmai backend operation receipt",
    "补充材料扫描件": "scanned copies of the supplementary documents",
    "跨类目补充说明": "a cross-category supplementary statement",
    "申请单原始截图": "the original screenshot of the application",
    "专员核实记录": "the specialist's verification record",
    "您填写的身份证信息有误": "corrected ID information",
    "您应上传身份证，但您上传的是护照": "your ID card (a passport was uploaded instead)",
    "您选择法人证件类型是身份证，但您上传的是护照":
        "your ID card (the document type selected was ID card, but a passport was uploaded)",
}

#: 驳回原因主干（"…，需补充 X 后重新提交"里逗号前的部分）。
#: 写成能直接跟在 because 后面的从句；材料另由 {materials_en} 给，不在这里重复。
REASON_EN = {
    "不符合业务要求": "it doesn't meet the business requirements for this application",
    "个体工商户升级为公司后未同步更新入驻资料":
        "the onboarding information wasn't updated after the business was upgraded from a "
        "sole proprietorship to a company",
    "主体名称": "the entity name on the application doesn't match our records",
    "主体资质列严列异": "the entity's qualification status is flagged as abnormal in our system",
    "主体资质到期": "the entity's qualification has expired",
    "产品线不匹配": "the product line doesn't match the qualification submitted",
    "京麦侧资料校验未通过，按站内指引补充后重新提交":
        "the document check on the Jingmai side didn't pass",
    "京麦渠道驳回，电话外呼不适用，走站内消息触达":
        "this case was rejected through the Jingmai channel and is handled by on-site message "
        "rather than a call",
    "化妆品备案凭证与申请商品批号不一致":
        "the cosmetics filing certificate doesn't match the batch number of the product applied for",
    "医药类目链路不完整": "the authorization chain for the medicine category is incomplete",
    "品牌不一致": "the brand doesn't match the brand registered for your shop",
    "品牌方不认可授权（不属实、未授权等）":
        "the brand owner doesn't recognize this authorization (it's unverified or not granted)",
    "品牌方明确告知假授权": "the brand owner has confirmed the authorization is forged",
    "品牌混淆": "the brand is confusingly similar to an existing brand",
    "品牌类目不符": "the brand isn't registered for this category",
    "商标无效": "the trademark is invalid",
    "商标注册时间不符合（过期或卡控时间不符合）":
        "the trademark registration date doesn't meet our requirements "
        "(expired or outside the allowed window)",
    "商标注册证为境内": "the trademark registration certificate is domestic only",
    "商标状态不符合要求": "the trademark status doesn't meet our requirements",
    "商标转让证明缺失，无法确认品牌权利人":
        "the trademark assignment certificate is missing, so we can't confirm the brand owner",
    "图形商标": "it's a figurative trademark, which isn't accepted for this category",
    "境外主体资质文件未附中文译本":
        "the overseas entity's qualification documents don't include a Chinese translation",
    "店铺名称不符合授权书要求": "the shop name doesn't match the authorization letter",
    "店铺名称不符合规范": "the shop name doesn't meet our naming rules",
    "持有人不一致": "the brand owner doesn't match the applicant",
    "持有人不一致（商标网）":
        "the owner on the trademark office record doesn't match the applicant",
    "持有人不一致（经营品牌与新增品牌持有人）":
        "the owner of the brand applied for doesn't match the owner of the brands your shop runs",
    "授权书不符（链路/独占性/品牌）":
        "the authorization letter doesn't meet requirements "
        "(authorization chain, exclusivity or brand)",
    "授权书公章与品牌持有人名称不一致，需重新出具":
        "the seal on the authorization letter doesn't match the brand owner's name, "
        "so it must be reissued",
    "授权书内容不完整（包含盖章及个人身份证复印件、无独占性等问题）":
        "the authorization letter is incomplete (seal, copy of ID, exclusivity, etc.)",
    "授权书内容不完整（包含盖章及个人身份证复印件等问题）":
        "the authorization letter is incomplete (seal, copy of ID, etc.)",
    "授权方未回复": "the authorizing party hasn't replied to our verification",
    "授权方联系不上": "we couldn't reach the authorizing party",
    "授权时间到期": "the authorization has expired",
    "授权核实无果（无回复&无在职证明）":
        "the authorization couldn't be verified (no reply and no proof of employment)",
    "授权资质明显造假（公章造假/链路造假/ps）":
        "the authorization appears to be forged (seal, authorization chain or edited files)",
    "授权链路不完整": "the authorization chain is incomplete",
    "授权链路不完整，缺少品牌方到一级经销商的逐级授权文件":
        "the authorization chain is incomplete — the step-by-step authorization from the "
        "brand owner down to the first-tier distributor is missing",
    "无品牌": "no brand is registered for this application",
    "治理停用（临时管控、商品治理）": "the brand is temporarily suspended for a governance review",
    "法人证件信息错误": "the legal representative's ID information is incorrect",
    "法人证件类型": "the type of legal representative ID document isn't acceptable",
    "海外商标注册证明未完成公证认证":
        "the overseas trademark registration certificate hasn't been notarized and authenticated",
    "特殊事业部不支持申请（达达事业部、京喜通、京喜自营工厂店的不支持提交产品线）":
        "applications from this business unit aren't supported (Dada, Jingxitong and Jingxi "
        "factory stores can't submit product lines)",
    "申请单原因标签缺失，待专员人工核实归档":
        "the rejection reason wasn't tagged on this application, so a specialist needs to verify it",
    "申请品牌与店铺经营品牌持有人不一致，需先停用原品牌":
        "the brand applied for has a different owner than the brands your shop runs, "
        "so the original brand has to be deactivated first",
    "禁售": "the product is on the prohibited sales list",
    "税务信息不符": "the tax information doesn't match our records",
    "类似群不符合": "the similar trademark groups don't match the goods applied for",
    "类目超商标范围": "the category goes beyond the scope of the trademark",
    "缺少资质（公章/流水/品牌）":
        "required qualifications are missing (company seal, transaction records, brand documents)",
    "营业执照经营范围未覆盖申请类目":
        "the scope registered on your business license doesn't cover the category applied for",
    "证件信息不符": "the ID information doesn't match our records",
    "质检报告出具机构不在认可名录内，需重新送检":
        "the institution that issued the quality inspection report isn't on our approved list, "
        "so it needs to be re-tested",
    "资质上传有误（指缺失或者上传错误）":
        "the qualification documents were uploaded incorrectly (missing or wrong files)",
    "资质上传有误（指缺失或者错误）":
        "the qualification documents were uploaded incorrectly (missing or wrong files)",
    "资质不全": "the qualification documents are incomplete",
    "资质与类目不符": "the qualifications don't match the category applied for",
    "资质已过期": "the qualification has expired",
    "资质造假": "the qualification documents appear to be forged",
    "资质造假（公章造假/链路造假等）":
        "the qualification documents appear to be forged (company seal or authorization chain)",
}

#: 原因后面常被追加的"补充说明"段，只翻一次，不跟着原文重复两遍。
_REASON_NOTE_EN = (
    " In addition, several attachments don't match what's registered in our system, "
    "so please check them one by one, re-upload, and make sure every scan is clear and "
    "fully sealed")

_REASON_FALLBACK_EN = (
    "the documents for this application still have outstanding issues that our review "
    "couldn't clear")

#: 状态机 intent -> 英文话术。
#: 占位符由 i18n.fill_en 填：{subject_en}/{subject_cap_en}/{brand}/{category_en}/
#: {reason_en}/{materials_en}/{company_name}。
EN_REPLIES = {
    "greeting_confirm_company": (
        "Hello, this is JD merchant qualification review. Am I speaking with {company_name}?"),
    "identity_retry": (
        "To confirm this application, please tell me the last four digits of the "
        "phone number you registered."),
    "identity_ok": (
        "Thanks, that's confirmed. {subject_cap_en} was not approved because {reason_en}. "
        "You need to provide {materials_en}. Let me go through it with you."),
    "official_script": (
        "{subject_cap_en} was not approved because {reason_en}. "
        "You need to provide {materials_en}, then resubmit it in the merchant backend."),
    "unclear": (
        "Sorry, could you tell me whether you want the reason, or the list of documents?"),
    "materials_only": (
        "You need to provide {materials_en}. The checklist is already in your merchant "
        "backend, so you can check it anytime."),
    "what_auth": (
        "The authorization means the chain of documents from the brand owner to the "
        "distributor and then to you. In your case, {reason_en}, so you need {materials_en}. "
        "The checklist is in your merchant backend."),
    "scope_answer": (
        "All of these documents are required and all the steps must be done: prepare "
        "{materials_en}, upload them on the qualification review page of the merchant "
        "backend, then resubmit. Which step would you like me to explain?"),
    "checklist_sent": (
        "Done — I've sent the checklist to your merchant backend: {materials_en}. "
        "If a step is unclear, or you forget what to prepare, just check the checklist there."),
    # 所有收线句都带上热线（11-12 用户要求：再见前必须给 4006180092）
    "consent_checklist": (
        "Thanks. I've recorded the reason and the document requirements, and the checklist "
        "is in your merchant backend for you to check anytime. If you still have questions, "
        "just call us on 4006180092. Goodbye."),
    "consent": (
        "Thanks for your time. I've recorded the reason and the document requirements. "
        "If you still have questions, just call us on 4006180092. Goodbye."),
    "declined": (
        "Understood. I'll note this down and have a specialist contact you. If you still "
        "have questions, just call us on 4006180092. Goodbye."),
    "done": (
        "Thanks for your time. If you still have questions, just call us on 4006180092. "
        "Goodbye."),
    "help_steps": (
        "Here are the steps: first prepare {materials_en}; then log in to the merchant "
        "backend, find the qualification review page and upload them; finally resubmit. "
        "Which step is unclear?"),
    "hurry_short": (
        "In short: provide {materials_en} and resubmit in the merchant backend."),
    "scam_reassure": (
        "I understand your concern. This is JD's official qualification review. "
        "We will never ask you to transfer money or share passwords. You can verify "
        "this via the JD merchant hotline 4006180092."),
    "repair": (
        "Sorry, let me say that again: {subject_en} was not approved because {reason_en}; "
        "you need to provide {materials_en} and resubmit."),
    # 语言确认这一句不塞中文公司名：客户只是问"能不能说英语"，
    # 回一句纯英文的承接最自然；身份确认仍在下一轮按赛题②环报单位名。
    "lang_en": (
        "Sure, I can continue in English. Let's confirm which account we're calling about first."),
}

#: intent 没命中时按阶段兜底，保证英文模式下不冒出中文句子。
EN_BY_STAGE = {
    "GREETING": "Hello, this is JD merchant qualification review. Am I speaking with {company_name}?",
    "IDENTITY": "To confirm the application, please tell me the last four digits of the registered phone number.",
    "NEED": ("{subject_cap_en} was not approved because {reason_en}. "
             "You need to provide {materials_en} and resubmit in the merchant backend."),
    "OFFER": ("You need to provide {materials_en} and resubmit in the merchant backend. "
              "The checklist is in your merchant backend."),
    "CONFIRM": "Is everything clear? You can just say yes, or ask me a question.",
    "WRITEBACK": ("Thanks. I've recorded everything and the checklist is in your backend. "
                  "If you still have questions, just call us on 4006180092. Goodbye."),
    "DONE": ("Thanks for your time. If you still have questions, just call us on 4006180092. "
             "Goodbye."),
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


def _english_words(text):
    return re.findall(r"[A-Za-z][A-Za-z'\-]*", text or "")


def is_full_english(text, min_words=4):
    """这句算不算"在说英语"，而不是"蹦了个英文词"。

    11-13 用户要求：回答 ok / yes 时不要立刻切英文，得等到**完整的英语句子**。
    判据不是"有没有拉丁字母"（那样 "ok" 就够），而是**有没有成为一句句子**：

        ok / yes / ok thanks / yes i am        → 不算（这是回答，不是语种）
        what is this / i need help            → 算（三个词但带主语或助动词）
        i want to know why it was rejected    → 算（四个词以上）

    纯中文的句子直接返回 False；带中文的一律不按英文处理（混说的情况由
    "句中有中文 → zh" 那条兜着，这里只做正向判断）。
    """
    t = (text or "").strip()
    if not t or _ZH.search(t):
        return False
    words = _english_words(t)
    if len(words) >= min_words:
        return True
    if len(words) == 3 and any(w.lower() in _SUBJECT_AUX for w in words):
        return True
    return False


def detect(text, current="zh"):
    """判断这一轮该用哪种语言。返回 "zh" / "en"。

    顺序是有讲究的，每一条都对应一个真实踩过的坑（11-12 ~ 11-13）：

        ① 明确要求中文（"i can't understand english" / "请说中文"）→ zh
        ② 明确说中文不好 / 要求英文（可以是中文说的"我中文不好"）→ en
        ③ 句子里有中文 → zh
        ④ 明确问"你会说英语吗" → en
        ⑤ 能对上中文短句的英文回答（ok / yes / thanks / what materials）
           → **保持当前语言**。这是"回答"，不是"在说英语"。
        ⑥ 完整的英文句子 → en
        ⑦ 其余（数字、标点、单个词）→ 保持当前语言

    第 ⑤ 条是 11-13 用户明确提的：「回答 ok、yes 的时候不要立马切英文」。
    原来的判据是"有两个拉丁字母就切"，于是客户答一句 "ok"，客服下一句就整段
    改口说英语——客户只是应了一声，并没有要换语言。
    """
    t = (text or "").strip()
    low = _norm(t)
    if any(k in low for k in BACK_TO_ZH):
        return "zh"
    if any(k in t for k in EN_REQUEST_ZH) or any(k in low for k in EN_REQUEST_EN):
        return "en"
    if _ZH.search(t):
        return "zh"
    if any(k in low for k in ASK_EN):
        return "en"
    # 单号/编号不是"在说英语"：QZ202611060001 这类全大写+数字的串，
    # 按拉丁字母数判会整通电话切成英文（C5 四路径核身·申请单号就这么翻车的）。
    letters = "".join(_LATIN.findall(t))
    has_digit = any(ch.isdigit() for ch in t)
    looks_like_code = bool(has_digit and letters.isupper())
    if not looks_like_code and to_zh(t):
        return current or "zh"
    if not looks_like_code and is_full_english(t):
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


# ===========================================================================
# 后台字段 -> 英文
# ===========================================================================
_MATERIAL_FALLBACK = "the document required for this application"

#: 有些字段值本身会带"；补充说明：…"（档案里为压测重复拼接），只留一次说明。
_REASON_NOTE_RE = re.compile(r"[；;]?\s*补充说明\s*[：:]")


def _reason_head(raw):
    """取驳回原因主干 + 是否带"补充说明"。"""
    r = str(raw or "").strip()
    if not r:
        return "", False
    parts = _REASON_NOTE_RE.split(r, 1)
    head = parts[0].strip(" 。.")
    note = len(parts) > 1 and bool(parts[1].strip())
    # "…，需补充 X、Y 后重新提交" 里的材料另由 {materials_en} 给，这里只留问题本身
    if "，需补充" in head:
        head = head.split("，需补充")[0].strip(" 。，")
    return head, note


def reason_en(raw):
    """驳回原因 -> 英文从句（可直接跟在 because 后面）。"""
    head, note = _reason_head(raw)
    en = REASON_EN.get(head) or _REASON_FALLBACK_EN
    if not note:
        return en
    # 说明段单独成句：模板会在 {reason_en} 后面补句号，所以这里先断句。
    return en + "." + _REASON_NOTE_EN


def material_en(name):
    n = str(name or "").strip()
    if not n:
        return ""
    return MATERIAL_EN.get(n) or _MATERIAL_FALLBACK


def category_en(raw):
    """一级类目 -> 英文；斜杠组合按段翻（服饰鞋包/生鲜）。"""
    r = str(raw or "").strip()
    if not r:
        return ""
    out = []
    for part in (p.strip() for p in r.split("/")):
        if not part:
            continue
        en = CATEGORY_EN.get(part)
        if en:
            out.append(en)
    return " / ".join(out)


def _join_en(items):
    items = [x for x in items if x]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def subject_en(app):
    """句子主语：品牌名保留中文（工商/后台专有名词），其余英文。"""
    app = app or {}
    brand = str(app.get("brand_name") or "").strip()
    cat = category_en(app.get("category_l1"))
    if brand and cat:
        return "your application for the %s brand in the %s category" % (brand, cat)
    if brand:
        return "your application for the %s brand" % brand
    if cat:
        return "your application in the %s category" % cat
    return "your application"


def speaker_name(app=None, target=None):
    """英文里怎么称呼对方主体：公司简称 > 店铺名 > 公司全称，都保留原文。"""
    app = app or {}
    target = target or {}
    for d in (app, target):
        for k in ("company_short_name", "shop_name", "company_name"):
            v = str(d.get(k) or "").strip()
            if v:
                return v
    return "your company"


def fill_en(text, app=None, target=None):
    """英文出口统一填值：除品牌名/主体名外，其余一律英文。

    只填文本，不动 stage / intent / tool_call——业务流程与回写字段全走中文那套。
    顺手把中文占位符（{reason}/{materials}/{application_desc}）也换成英文值，
    这样哪个模板漏改都不会再漏出中文。
    """
    if not text:
        return text
    app = app or {}
    mats = app.get("required_materials") or []
    if isinstance(mats, str):
        mats = [m for m in re.split(r"[、,，;；]", mats) if m.strip()]
    mats_en = _join_en([material_en(m) for m in mats]) or "the required documents"
    subj = subject_en(app)
    who = speaker_name(app, target)
    cat = category_en(app.get("category_l1"))
    brand = str(app.get("brand_name") or "").strip()
    desc_en = subj
    repl = {
        "{subject_cap_en}": subj[:1].upper() + subj[1:],
        "{subject_en}": subj,
        "{reason_en}": reason_en(app.get("reject_reason")),
        "{materials_en}": mats_en,
        "{category_en}": cat or "the category applied for",
        "{brand}": brand or "your brand",
        "{company_name}": who,
        # 中文占位符的英文等价物（模板漏改时的兜底）
        "{application_desc}": desc_en,
        "{reason}": reason_en(app.get("reject_reason")),
        "{materials}": mats_en,
        "{category}": cat or "the category applied for",
        "{deadline}": "within one week",
        "{contact_role}": "the contact person",
    }
    for k in sorted(repl, key=len, reverse=True):
        text = text.replace(k, repl[k])
    return text


def closing_with_hotline(text, out=None):
    """英文收线句补热线（用户要求：再见前必须给 4006180092）。"""
    if not text or "4006180092" in text or "Goodbye" not in text:
        return text
    return text.replace(
        "Goodbye",
        "If you still have questions, just call us on 4006180092. Goodbye.")
