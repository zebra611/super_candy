# -*- coding: utf-8 -*-
"""官方驳回话术的槽位填充：把 `config/kb_pop.json` 里带【】的模板变成能念的句子。

**为什么要有这个文件**

`kb_pop.json` 的 96 条来自官方资料包，是审核员原话，但其中 80 条带【】占位：
38 条全是**空占位**（"您申请的【】品牌"）、16 条混用、10 条是具名占位。
以前对话层只做两件事：命中就念，或者填不满就整条不播。前者会让 TTS 念出
"括号括号""叉叉"，后者等于把官方话术全丢掉——商家问"为什么驳回"，
客服只会说"我这边暂时查不到"。

这个模块做第三件事：**用本通档案的真值把槽位填上**，填不满才不播。

**三条边界**

1. 只填档案里真有的东西（品牌/店铺/公司/类目/需补材料），
   档案没有的字段（SKU、商标类似群）一律判"填不满"，不猜。
2. 具名占位不都当槽位：像【授权时间】【重新开具授权书】【该品牌暂不招商/该品牌为邀请制】
   括号里写的是**要说的话**（甚至二选一），不是字段名。这类去括号原样保留，
   含"/"的取第一个选项——这是官方表的书写习惯，不是数据缺失。
3. 填完还要过一遍可播检查：残留【】/XX 视为失败，退回"不播"，
   由调用方走"记下来、稍后专员解答"，绝不把半句话送去 TTS。

**和 `modules/kb.py` 的关系**

kb 负责"哪句话该用"（词长加权检索），本模块负责"这句话怎么念"（槽位渲染）。
两者不互相依赖：kb 命中后把 answer 交给 render_script()，渲染失败就不播。
"""

import json
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KB_PATH = os.path.join(_ROOT, "config", "kb_pop.json")

_PLACEHOLDER = re.compile(r"【([^】]*)】")
# 渲染后仍出现这些，说明没填干净（"XX" 是官方表里的另一套占位写法）
_RESIDUAL = ("【", "】", "XX")

_CACHE = {"items": None, "mtime": None}

# 官方追问剧本里有 5 条答案带 "XX" 占位（kb_pop 的 S 系列）。
# 它们和 `config/mock/qa.json` 里的特殊场景问答是同一句话，补齐文案必须一致——
# 同一个问题从 QA 通道问和从知识库通道问，听到的不能是两个版本。
# 原文仍在 kb_pop.json 里做审计（C6 的原则是"原文保留"），这里只做**播报替身**。
SCRIPT_OVERRIDES = {
    "KB-POP-S03": "您可以在国家商标网中查询商标状态，等状态变为已注册之后重新提交申请，"
                  "或者提供其他已注册状态的同类有效商标",
    "KB-POP-S05": "您商标目前是等待实质审查状态，如果在等待期满之前，商标状态变更为"
                  "初审公告或者已注册，就可以直接申请，不用等满整段时间",
    "KB-POP-S12": "这两家公司目前没有查询到有直接持股或者同一法人的关系，"
                  "是需要您补充相应的授权书的",
    "KB-POP-S14": "我们是通过品牌方官方渠道联系到授权负责人的，如果您能联系到该联系人，"
                  "可以请他回复我们的消息或者接听我们的电话；如果您有其他的联系方式，"
                  "可以在授权书的位置上传，不过这些联系方式需要联系人协助提供"
                  "他在授权公司的工作凭证才可以",
    "KB-POP-S17": "您可以联系品牌方给您再授权这些类目所需的对应类别商标",
}

# 档案大类 → 官方话术分类。
# 这两个是不同维度的标签：档案说"这单属于哪个业务域"，kb_pop 说"这句话术是
# 哪个审核环节的"。不映射的话，legacy 那 120 条只能按序号硬取话术，
# 会出现"品牌-商标"的单子念一段"公司主体资质"的话术。
CATEGORY_TO_MODULES = {
    "品牌-商标": ("商标注册证", "新增产品线"),
    "品牌-授权": ("授权书-文审", "授权书-验真", "授权范围审核"),
    "品牌-全球购": ("（全球购）",),
    "入驻与主体店铺": ("公司主体", "店铺名称", "授权书-文审", "授权范围审核"),
    "商品与行业资质": ("商品资质", "行业资质"),
}


# 档案里没有对应字段、因此永远填不满的模板：
#   026 要"正确类似群"（商标分类明细，档案只到三级类目名）
#   067/068 要 SKU（商品资质场景，档案是店铺级申请单）
# 这三条不是"不会填"，是"数据里没有"——列出来是为了让候选池跳过它们，
# 而不是每次运行时反复尝试再失败。
UNFILLABLE_IDS = ("KB-POP-026", "KB-POP-067", "KB-POP-068")


def _load():
    """读 kb_pop.json，按 mtime 缓存（改完配置不用重启进程）。"""
    mtime = os.path.getmtime(_KB_PATH)
    if _CACHE["items"] is None or _CACHE["mtime"] != mtime:
        with open(_KB_PATH, encoding="utf-8") as f:
            _CACHE["items"] = json.load(f).get("items") or []
        _CACHE["mtime"] = mtime
    return _CACHE["items"]


def slot_view(app):
    """把档案字段翻成话术里的中文槽位名。

    只给**命名占位**用（`kb.render` 走这条路）；空占位没有名字，
    必须靠上下文规则（见 render_script）。
    """
    app = app or {}
    mats = [str(m) for m in (app.get("required_materials") or []) if str(m).strip()]
    return {
        "品牌": app.get("brand_name") or "",
        "店铺名称": app.get("shop_name") or "",
        "公司名称": app.get("company_name") or "",
        "公司": app.get("company_name") or "",
        "类目": app.get("category_l3") or app.get("category_l2") or "",
        "材料": mats[0] if mats else "",
        "截止日期": app.get("recheck_deadline") or "",
        "申请单号": app.get("application_id") or "",
    }


def _is_blank(name):
    """这个占位是不是"没写名字的空槽"（【】）。"""
    return not (name or "").strip()


def _pick_option(name):
    """具名占位里含"/"的，是官方写的二选一，取第一项。"""
    return name.split("/")[0].strip()


def _blank_value(before, after, app, used_mats):
    """空【】该填什么：看它前后各几个字。

    每条规则都对应上面注释里的一种模板形态；匹配不上的返回 None（判填不满）。
    宁可漏答一句，也不要把"类目：正确类似群："这种东西念给商家听。
    """
    brand = (app.get("brand_name") or "").strip()
    mats = [str(m) for m in (app.get("required_materials") or []) if str(m).strip()]

    def _mat(i=0):
        """按出现顺序取需补材料，避免一句里两个空槽填成同一项。"""
        if not mats:
            return None
        k = min(used_mats[0], len(mats) - 1)
        used_mats[0] += 1
        return mats[k]

    # 官方表里"您申请的【】品牌"出现在 30+ 条里，是最主要的形态
    if "品牌" in after[:2] and brand:
        return brand
    # 商标状态有两处，意思相反，不能填同一个词：
    #   "因商标状态为【】故不符合要求" → 当前状态不合规（待审）
    #   "需您提供【】状态的【】类商标" → 要补的状态（已注册）
    # 一开始两处都填"已注册"，句子变成"状态为已注册故不符合要求，
    # 需提供已注册状态的商标"——自相矛盾，比不播更糟。
    if "商标状态为" in before:
        return "待审"
    if "提供" in before and "状态的" in after:
        return "已注册"
    if "类商标" in after:
        return "对应"
    # 注册时间不符：不编具体日期，说清"不符合平台要求"即可
    # 这里必须排在"注册时间"之前：模板是"因商标注册时间【】，请【】后再重新提交"，
    # 第二个空的 8 字窗口里同样含"注册时间"，先判就把"请重新注册商标"
    # 填成了"请不符合平台要求"——一句自相矛盾的话。
    if "重新提交" in after and before.rstrip("，, ").endswith("请"):
        return "重新注册商标"
    if "注册时间" in before:
        return "不符合平台要求"
    # 授权链两端：缺少【】到【】的授权关系。
    # 判据有两个方向：第一个空的后文以"到"开头，第二个空的前文以"到"结尾。
    # 只看后文会把第二个空漏进"缺少→材料"分支，填成"品牌方到品牌授权书的授权关系"。
    if "缺少" in before:
        if after.startswith("到"):
            return "品牌方"
        if before.rstrip("，, ").endswith("到"):
            return "贵公司"
        return _mat()
    # 资质类：缺什么就说什么，取本单需补材料的第一项
    if "上传的" in before and "资质" in after:
        return _mat()
    if any(k in after[:8] for k in ("资质上传错误", "资质范围错误",
                                    "资质时间已失效", "资质显示已到期失效")):
        return _mat()
    if "协助操作" in before:
        return "重新上传正确的资质"
    if "协助" in before:
        return "重新提交正确的授权信息"
    if "店铺名称" in before and "不符合命名规则" in after:
        return (app.get("shop_name") or "").strip() or None
    if "协助修改" in before:
        return "为符合命名规则的名称"
    if "查询状态为" in before:
        return "经营异常"
    # SKU / 类似群：档案里没有这个字段，判填不满，让调用方换一条话术
    return None


def render_script(answer, app, item_id=None):
    """把一条官方模板渲染成能念的句子。

    返回 (text, missing)：missing 非空说明有槽位填不上，调用方**不要播**。
    `item_id` 用于查播报替身（见 SCRIPT_OVERRIDES），不传就按原文渲染。
    """
    text = SCRIPT_OVERRIDES.get(item_id, str(answer or ""))
    if not text.strip():
        return "", ["（空话术）"]

    missing = []
    used_mats = [0]
    out = []
    pos = 0
    for m in _PLACEHOLDER.finditer(text):
        out.append(text[pos:m.start()])
        name = m.group(1)
        before = text[max(0, m.start() - 8):m.start()]
        after = text[m.end():m.end() + 8]

        if _is_blank(name):
            val = _blank_value(before, after, app or {}, used_mats)
        else:
            # 具名占位：二选一取第一项；"XX/XXX/XXXX类目"这类是空位写成 X 的，
            # 能换成本单真值就换，换不了就判填不满。
            val = _pick_option(name)
            if "XXXX类目" in val:
                cat = (app or {}).get("category_l3") or (app or {}).get("category_l2") or ""
                val = val.replace("XXXX类目", "%s 类目" % cat) if cat else None
            elif val in ("XX", "XXX") and (app or {}).get("brand_name"):
                val = (app or {}).get("brand_name")
            elif val == "XXX公司" and (app or {}).get("company_name"):
                val = (app or {}).get("company_name")
            if val and "类似群" in val:
                # "类目：正确类似群：" 是官方表里的待填栏位，没有可用的档案字段
                val = None

        if not val or not str(val).strip():
            missing.append(name or "（空占位符）")
            out.append(m.group(0))
        else:
            out.append(str(val))
        pos = m.end()
    out.append(text[pos:])

    rendered = "".join(out)
    # 二次检查：万一规则漏了（比如新的模板形态），残留占位一律算失败
    for mark in _RESIDUAL:
        if mark in rendered:
            missing.append("残留 %s" % mark)
            break
    return rendered, missing


# 官方话术的固定头尾：开头是自报身份，结尾是热线兜底。
# 这两段在**编排层**各有自己的位置（开场白自报身份、DONE 才留热线），
# 直接整段播会出现"核完身份又自我介绍一遍""一句话里两次再见"。
_LEAD_RE = re.compile(r"^(尊敬的商家伙伴)?您好[，,]\s*我是京东资质审核专员[，,]?\s*"
                      r"(非常感谢您愿意加入京东平台[，,])?")
_TAIL_RE = re.compile(r"[，,]?\s*如有疑问(您)?(可以)?(电话联系|拨打热线)[：:]\s*\d+"
                      r"[，,]?\s*我们将竭诚为您服务[！!]?")


def dialogue_text(script):
    """把官方话术改成"接在对话中间"的形态：去掉自报身份与热线尾句。

    只删这两段固定模板，正文一个字不改——正文是审核员原话，
    这里做的是"换场合"，不是"换说法"。
    """
    t = str(script or "").strip()
    if not t:
        return ""
    t = _LEAD_RE.sub("", t, count=1).strip()
    t = _TAIL_RE.sub("", t).strip()
    return t.rstrip("，, ")


def script_for(app, items=None):
    """给一条档案挑一条**能填满**的官方话术。

    优先用档案自带的 reject_code；它指向的话术不可用（不存在/京麦不外呼/填不满）时，
    按业务域换一条同域且能填满的。都不行就返回 None——调用方按"没有官方话术"处理。

    返回 dict(id, script, missing, via, source_id) 或 None。
    成功时 missing 一定是空列表：宁可没有，也不返回半句。
    """
    if not app:
        return None
    # 京麦驳回/无需外呼的单子：官方口径就是走站内指引、不打电话，
    # 给它配电话话术属于"用错了渠道"。这里直接不返回，调用方按"没有话术"处理。
    if app.get("close_mode") == "京麦" or app.get("call_status") == "无需外呼":
        return None
    # items 可注入：校验器的反向自测要"故意改坏一份模板"，不能只改磁盘文件。
    items = items if items is not None else _load()
    by_id = {it.get("id"): it for it in items}

    def _try(item, via):
        if not item or item.get("no_call"):
            return None
        text, missing = render_script(item.get("answer"), app, item.get("id"))
        if missing:
            return None
        return {"id": item.get("id"), "script": text, "missing": [],
                "via": via, "source_id": item.get("source_id") or ""}

    hit = _try(by_id.get(app.get("reject_code")), "reject_code")
    if hit:
        return hit

    for mod in CATEGORY_TO_MODULES.get(app.get("reject_category"), ()):
        for it in items:
            if it.get("category") != mod:
                continue
            hit = _try(it, "category_fallback")
            if hit:
                return hit
    return None


def candidates(category):
    """本业务域内**可能填满**的官方模板 id（给生成器分配 reject_code 用）。

    以前 legacy 档案的 reject_code 是 `(idx*7)%96` 硬取序号，两个后果：
    ① 28 条指向 KB-POP-069..096——kb_pop 里根本没有这些 id；
    ② 剩下的一半指向别的业务域（"品牌-商标"的单子念"公司主体"的话术）。
    结果是随机预览抽到某公司，说出来的驳回原因和它的档案对不上。
    """
    items = _load()
    if category == "京麦驳回":
        # 京麦驳回官方口径就是不外呼，这些 code 只做档案标记，不参与播报
        return [it["id"] for it in items if it.get("no_call")]
    if category == "未定位":
        return [it["id"] for it in items
                if (it.get("answer") or "").strip() and not it.get("no_call")
                and it["id"] not in UNFILLABLE_IDS]
    out = []
    for mod in CATEGORY_TO_MODULES.get(category, ()):
        for it in items:
            if it.get("category") != mod:
                continue
            if it.get("no_call") or not (it.get("answer") or "").strip():
                continue
            if it["id"] in UNFILLABLE_IDS:
                continue
            out.append(it["id"])
    return out


def best_code(category, reason):
    """按驳回原因挑一条**最贴题**的官方模板（给档案分配 reject_code）。

    只轮转不匹配的话，会出现"原因是授权链路不完整，话术念的是商标注册时间不符"——
    商家听到的是别人的驳回理由。这里用模板自带的 patterns 在原因里做词长加权，
    和 kb 检索同一套朴素的打分（长词命中得分高），没有模型、可解释。
    """
    items = _load()
    by_id = {it.get("id"): it for it in items}

    def _score(cid):
        it = by_id.get(cid) or {}
        return sum(len(p) for p in (it.get("patterns") or [])
                   if p and p in (reason or ""))

    def _best_in(ids):
        best, top = None, -1
        for cid in ids:
            sc = _score(cid)
            if sc > top:
                best, top = cid, sc
        return best, top

    ids = candidates(category)
    best, top = _best_in(ids) if ids else (None, -1)

    # 本域只命中"品牌"这种 2 字泛词（分 ≤2）时，去全库找一条真贴题的。
    # 例：档案写"申请品牌与店铺经营品牌持有人不一致"属品牌-商标域，
    # 但商标域里全是"商标状态/注册时间"，硬配就是"原因说持有人、话术念商标时间"。
    # 跨域挑一条讲持有人的（008）比"域内正确、语义错位"更贴近商家听到的内容。
    if top <= 2:
        every = [it["id"] for it in items
                 if (it.get("answer") or "").strip() and not it.get("no_call")
                 and it["id"] not in UNFILLABLE_IDS]
        alt, alt_top = _best_in(every)
        if alt and alt_top > top:
            return alt
    return best


def coverage(apps):
    """跑一批档案，统计有多少条能说出官方话术（给校验器/自检用）。"""
    total = len(apps)
    ok, bad = [], []
    for app in apps:
        s = script_for(app)
        (ok if s else bad).append({
            "application_id": app.get("application_id"),
            "reject_code": app.get("reject_code"),
            "close_mode": app.get("close_mode"),
            "via": (s or {}).get("via"),
        })
    # 失败里要区分"本来就该没话术"（京麦驳回，官方口径不外呼）
    # 和"电话单却说不出话术"（这是缺陷）
    bad_phone = [b for b in bad if b.get("close_mode") == "电话"]
    return {"total": total, "ok": len(ok), "bad": len(bad),
            "bad_phone": bad_phone, "ok_items": ok}
