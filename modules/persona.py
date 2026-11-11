"""客户性格识别与针对性回复（11-08，L2 的性格维度）。

这个模块只做两件事：

    observe(session, 用户这句话)  → 从**听到的字面**推断性格，写回 session
    adapt(session, 客服这句话)    → 按性格对回复做形式层的细化，带事实守卫

三条边界，都是刻意的：

1. **不读内部状态**。不看 stage / intent / slots 的业务含义，也不看评测标签。
   真实客服只能听见对方说了什么，推断必须只用用户文本；否则模拟器"受听"
   约束与线上识别会对不上，跑批里的性格收益全是假的。
   唯一从 session 取的，是下面守卫要用到的**事实值**（公司名、单号等）。

2. **识别层不可进化**。`TEMPERAMENT_MARKS` 写死在代码里，规则表里只有 `rules`。
   让优化器能改识别词表，它第一天就会发现"把所有人都判成急性子、然后只优化
   急性子那组"能刷高分数——指标变好，实际服务没变好。可进化的只有**回复动作**。

3. **只改形式，不改事实**。适配只能删客套前缀、追加引导句、做白名单替换；
   数字串、公司名、单号、品牌、店铺名一个都不能少；禁语、占位符不能多。
   任何守卫不过就整句退回原文，绝不抛异常（外呼里突然没声比话说得生硬严重）。
   官方 QA 原话、核身问句、挂断话术由调用方直接冻结，连形式都不动。

规则表初始 `rules = {}` 是**恒等映射**：接线本身不改变任何一句回复，
所以既有 205 条自检与 88 条评测必须逐字不变。之后由 evolve_persona.py
在闸门下逐步加入规则——这才是"自进化"该有的起点。
"""

import json
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_PATH = os.path.join(_ROOT, "config", "persona_adapt.json")

#: 注入口：自进化跑批要用候选规则表做对照实验，不能靠"关掉功能"当基线。
#: 关掉功能测出来的基线是假的；换一份规则表测出来的才是真对照。
ENV_RULES = "PERSONA_ADAPT_CONFIG"

_CACHE = {"path": None, "mtime": None, "data": None}

# ---------------------------------------------------------------------------
# 识别：按优先级排好的字面信号。顺序 = 平局时的裁决顺序。
# ---------------------------------------------------------------------------
TEMPERAMENT_MARKS = (
    ("skeptical", ("凭什么", "哪来的", "你谁", "你是哪个", "怎么信", "官方的吗",
                   "不会是推销", "诈骗", "要钱", "收钱吗", "先说你是", "别问号")),
    ("impatient", ("快点", "赶紧", "别绕", "说重点", "没时间", "忙着呢", "行了行了",
                   "有完没完", "别废话", "简单点")),
    ("hard_of_hearing", ("再说一遍", "听不清", "听不明白", "大点声", "信号不好",
                         "你说啥", "你说什么", "啊？")),
    ("forgetful", ("记不住", "记不得", "想不起来", "忘了", "没纸笔", "一时真想")),
    ("talkative", ("我跟你说啊", "上次那个", "顺便帮我", "对了还有", "这两天后台")),
    ("quiet", ()),          # 靠"连续极短应答"判，不用词表
    ("cooperative", ("没问题", "我配合", "好的我报", "你说怎么弄", "我按你说的")),
)

#: 极短应答：不超过 6 个字且没有事实数字，才算"话少"的一次证据。
_QUIET_MAX = 6
#: 弱信号（话少/配合）要攒几次才敢贴标签：「嗯」「好」太常见，
#: 一次就定性会把整通电话都改掉。带明确词表的强信号（"凭什么""快点"
#: "再说一遍"）一次就够——真人在电话里一句话就能听出对方着急或起疑。
_MIN_HITS = 2
_STRONG_CODES = tuple(c for c, marks in TEMPERAMENT_MARKS if marks)

LABELS = {
    "neutral": "普通", "skeptical": "多疑", "impatient": "急性子",
    "hard_of_hearing": "听不清", "forgetful": "记不住", "talkative": "爱絮叨",
    "quiet": "话少", "cooperative": "配合",
}


def load_rules(path=None):
    """读规则表。异常一律返回恒等配置——适配层不能拖垮通话。"""
    p = path or os.environ.get(ENV_RULES) or RULES_PATH
    if not os.path.isabs(p):
        p = os.path.join(_ROOT, p)
    try:
        mt = os.path.getmtime(p)
        # 用 .get：缓存被外部清空是合法操作（跑批切换候选规则表时就会发生），
        # 下标取值会抛 KeyError，被下面的兜底吞掉后**静默退回恒等配置**——
        # 表现是"规则明明注入了却一条都没生效"，极难查。
        if _CACHE.get("path") == p and _CACHE.get("mtime") == mt:
            return _CACHE["data"]
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        _CACHE.update({"path": p, "mtime": mt, "data": data})
        return data
    except Exception:
        return {"enabled": True, "rules": {}}


def _classify(text):
    """单句 → 性格码（或 None）。纯函数，不碰 session。"""
    t = (text or "").strip()
    if not t:
        return None
    for code, marks in TEMPERAMENT_MARKS:
        if marks and any(m in t for m in marks):
            return code
    # 极短应答：含数字的不算（那是在报尾号/单号，不是话少）。
    # 「嗯，明白了」这类带语气词的短句也算话少；含数字的一律排除。
    if not re.search(r"\d", t) and (
            len(t) <= _QUIET_MAX
            or (len(t) <= 12 and t.startswith(("嗯", "哦", "好", "是", "行")))):
        return "quiet"
    return None


def observe(session, user_text):
    """把这一轮用户话累加进画像证据，返回当前 (code, conf)。

    只累积、不立刻切换：急性子说了一句"快点"、下一句正常报尾号，
    不该让整通电话都用急性子话术。达到 _MIN_HITS 才定性。
    """
    code = _classify(user_text)
    hits = session.setdefault("persona_hits", {})
    if code:
        hits[code] = hits.get(code, 0) + 1

    ranked = [(hits.get(c, 0), -i, c)
              for i, (c, _) in enumerate(TEMPERAMENT_MARKS)]
    ranked.sort(reverse=True)
    top, _, best = ranked[0]
    _need = 1 if best in _STRONG_CODES else _MIN_HITS
    if top < _need or not best:
        session["persona_code"] = "neutral"
        session["persona_conf"] = 0.0
    else:
        session["persona_code"] = best
        session["persona_conf"] = round(min(1.0, top / 3.0), 2)
    return session["persona_code"], session["persona_conf"]


# ---------------------------------------------------------------------------
# 适配：动作是白名单，守卫是硬约束
# ---------------------------------------------------------------------------
def _facts(session):
    """本通电话里出现过的具体事实值，适配后一个都不能少。"""
    app = (session.get("slots") or {}).get("application") or {}
    if not isinstance(app, dict):
        app = {}
    vals = []
    for k in ("company_name", "contact_name", "application_id", "brand_name",
              "shop_name", "phone_tail", "credit_code", "id_card"):
        v = str(app.get(k) or "").strip()
        if len(v) >= 2:
            vals.append(v)
    return vals


def _guard(base, new, session):
    """适配结果能不能播。任一不满足就整句退回。"""
    if not isinstance(new, str) or not new.strip():
        return False
    # 数字串：尾号、单号、热线，一个都不能被吃掉
    if re.findall(r"\d+", base) != re.findall(r"\d+", new):
        return False
    for v in _facts(session):
        if v in base and v not in new:
            return False
    # 占位符/模板残留不能在适配这一步被引入
    if any(m in new for m in ("【", "】", "XX")) and not any(
            m in base for m in ("【", "】", "XX")):
        return False
    if "{" in new and "{" not in base:
        return False
    # 禁语（配置里那套合规红线，适配层也必须守）
    try:
        import config_loader
        for w in (config_loader.forbidden_words() or []):
            if w and w in new and w not in base:
                return False
    except Exception:
        pass
    # 只做形式微调，不允许越改越长
    if len(new) > max(120, int(len(base) * 1.6)):
        return False
    return True


def _append(out, tail):
    if not isinstance(tail, str) or not tail or tail in out:
        return out
    sep = "" if out.endswith(("？", "?", "。", "！", "；")) else "，"
    return out.rstrip("，, ") + sep + tail


#: 句子里出现这些词，就不再挂"请您回一句"类引导后缀。
#: 一半是"已经在向对方要东西"（再要一遍是重复），一半是"已经收尾"（再要就挂不掉）。
#: 不含「再见」：真正的收尾轮由 `_persona_freeze`（end_reason / next_stage=DONE）
#: 整轮冻结，不靠这里；而 WRITEBACK 这类"说到再见但不是最后一句"的轮次
#: 仍需要追加引导语（test_scene_isolation 的对照组就是它）。
_SKIP_SUFFIX_MARKS = ("后四位", "尾号", "四位数字", "四个数字", "姓名", "单号",
                      "公司名", "全名", "先忙", "不打扰",
                      "稍后再联系", "稍后专员")


def _apply_actions(base, rule, freeze=False, repeated=False, repeat_n=0):
    """按规则改形式。freeze=True 时**只允许**重复引导，其余动作一律不做。

    为什么要给冻结句留这一个口子：官方原话必须逐字复述，但把同一句原话
    连念三遍会把客户念走（性格跑批里 T10 就是这样挂断的）。追加一句
    轮换的引导语既不改官方原句，也让"这一轮听到的"和上一轮不是同一串字，
    模拟器的重复惩罚不再触发。这是形式层能修的真实问题，不是刷分。
    """
    out = base
    # 句子里已经在要东西（后四位/姓名/单号）或已经收尾（再见/先忙），
    # 就不再挂任何"请您回一句"类尾巴——两种尾巴（suffix 与 avoid_repeat）都要拦。
    # 11-11 实测：quiet 性格的 avoid_repeat 是「您回一个字也行。」，
    # 客户正被要求"报四个数字"，尾巴又来一句"回一个字也行"，自相矛盾。
    # 还有一条：**已经说了整段内容就别再讨一句回应**。
    # 11-11 线上实测：NEED 里解释完"为什么被驳回、要补哪三样"（60 多字），
    # 末尾又挂一句「您回一个字也行。」——像在求人回话，也把刚说完的重点稀释了。
    # 40 字是分界：短句（"感谢您的配合，再见。"）仍可以带引导，
    # 整段解释不带。test_scene_isolation 的对照组就是那条短句。
    _quiet_tail = (any(w in out for w in _SKIP_SUFFIX_MARKS)
                   or "？" in out or "?" in out or len(out) >= 40)
    if repeated and not _quiet_tail:
        opts = rule.get("avoid_repeat") or []
        if opts:
            tail = opts[repeat_n % len(opts)]
            out = _append(out, tail)
    if freeze:
        return out
    for lead in (rule.get("strip_lead") or []):
        if isinstance(lead, str) and out.startswith(lead):
            out = out[len(lead):]
            break
    for pair in (rule.get("replace") or []):
        if isinstance(pair, list) and len(pair) == 2 and pair[0] in out:
            out = out.replace(pair[0], pair[1], 1)
    # 含问号的句子不许接陈述式引导。第一版只判「以问号结尾」，实测是假绿：
    # 真实话术是「那方便说一下您的姓名吗？我核对一下申请单。」——问号在中间、
    # 句尾是句号，判据没命中，线上仍念出「…我就不多说了」，刚问完又说不多说。
    # 改成整句含问号就跳过 suffix；avoid_repeat 同上（见 _quiet_tail）。
    #
    # 11-11 又补一条：祈使式问句**没有问号**（「…预留手机号的后四位，
    # 说四个数字就行。」），旧判据没命中，后面就挂上「您回一个字也行。」——
    # 刚让对方报四位数字，又说"回一个字也行"。收线话术同理：说了"您先忙"，
    # 再挂一句"回一个字也行"，等于不让挂。判据扩成 _SKIP_SUFFIX_MARKS。
    tail = rule.get("suffix") or ""
    if _quiet_tail:
        tail = ""
    return _append(out, tail)


def adapt(session, text, stage="", freeze=False):
    """按性格细化回复。返回 (text, applied)。

    freeze=True 时原样返回（官方原话 / 核身问句 / 挂断话术）。
    规则表为空、性格为 neutral、守卫不过，三种情况都是原样返回——
    调用方不需要区分原因，事件里记 applied=False 即可。
    """
    if not text:
        return text, False
    rules = load_rules()
    if not rules.get("enabled", True):
        return text, False
    code = session.get("persona_code") or "neutral"
    if code == "neutral":
        return text, False
    table = rules.get("rules") or {}
    rule = (table.get(code) or {}).get(stage) or (table.get(code) or {}).get("*")
    if not isinstance(rule, dict) or not rule:
        return text, False
    last = session.get("last_reply_text") or ""
    repeated = (text == last)
    n = int(session.get("persona_repeat_n") or 0)
    # 同一个后缀在一通电话里最多挂 2 次。
    #
    # 11-11 双端自对练实测：客户被识别成"听不太清"之后，客服每一句陈述式回复
    # 都挂「我放慢一点说。」——第 6/7/8/9/12 轮连续出现五次。单看每一句都没错，
    # 连着听就是一个复读机；客户在第 9 轮已经说"哦，补这三样东西啊"，
    # 客服还在"我放慢一点说"，等于没听见他。
    # 这不是"语气太客气"，是**同一句话重复太多次**，规则表管不了次数，只能这里管。
    _sfx = (rule.get("suffix") or "").strip()
    _used = session.get("persona_suffix_used") or {}
    if _sfx and int(_used.get(_sfx, 0)) >= 2:
        rule = dict(rule)
        rule["suffix"] = ""
    # 客户这一轮刚递上信息（报单号、重申身份）时，跳过「要信息型」后缀：
    # 规则表的后缀有些是在向客户要东西（如「您也可以对照申请单编号核实」），
    # 紧跟在客户刚报完单号之后读出来，等于当面又要一遍。
    # 由 dialogue 的接住分支置 fact_ack_turn，每轮开头清除。
    if session.get("fact_ack_turn"):
        rule = dict(rule)
        rule["suffix"] = ""
    try:
        new = _apply_actions(text, rule, freeze=freeze,
                             repeated=repeated, repeat_n=n)
    except Exception:
        return text, False
    if repeated and new != text:
        session["persona_repeat_n"] = n + 1
    if new == text or not _guard(text, new, session):
        return text, False
    if _sfx and _sfx in new:
        _used[_sfx] = int(_used.get(_sfx, 0)) + 1
        session["persona_suffix_used"] = _used
    session.setdefault("persona_applied", []).append(
        {"code": code, "stage": stage, "before": text, "after": new})
    return new, True


def summary(session):
    """给事件 / 接口用的溯源字段。"""
    return {
        "code": session.get("persona_code") or "neutral",
        "label": LABELS.get(session.get("persona_code") or "neutral", "普通"),
        "conf": round(float(session.get("persona_conf") or 0.0), 2),
        "applied": len(session.get("persona_applied") or []),
    }
