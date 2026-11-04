"""场景配置加载器（M8 的一部分）。

用途：
    赛题的"详细业务场景"要报名后才公布，所以业务内容（话术、槽位、禁语、
    回写字段）必须全部外置。换场景 = 换 config/scene.json，代码不动。
    这是应对"场景未知"这个最大变量的唯一办法。

设计取舍：
    只用标准库 json，不引入 PyYAML。配置文件里的注释用 "_comment" 字段承载。

多场景（v2）：
    场景文件统一放 config/scenes/<场景名>.json。选哪一套有三种方式，优先级从高到低：
        1. 环境变量 APP_CONFIG=/绝对路径/xxx.json   —— 临时指定任意文件，最灵活
        2. 环境变量 APP_SCENE=retail_recall          —— 按场景名选，日常最常用
        3. 都不设                                  —— 用 config/scene.json（老行为，不变）
    为什么默认仍是 scene.json：README、docs、队友的习惯和 CI 全指向它，改了默认
    等于改所有人的入口。多场景是"加一条路"，不是"换一条路"。

schema 校验（v2）：
    配置写错要在启动时就报出来，而不是跑到第三个环节才 KeyError。
    `validate()` 返回问题清单（人话，带字段路径），`load()` 会打印前几条。
    设 SC_STRICT_CONFIG=1 则直接抛 ConfigError —— 用于部署与答辩前自检。
    默认只告警不抛：骨架阶段宁可少说话也别起不来。
"""

import json
import os

from contracts import STAGES

_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(_ROOT, "config")
SCENES_DIR = os.path.join(CONFIG_DIR, "scenes")
# 默认场景文件：保持老路径，所有人的书签、文档、脚本都还认它
CONFIG_PATH = os.path.join(CONFIG_DIR, "scene.json")
# 密钥单独一个文件，不进仓库（见 .gitignore）。仓库里只放 example 模板。
SECRETS_PATH = os.path.join(CONFIG_DIR, "secrets.json")

_cache = None   # 进程内缓存，避免每次请求都读盘
_secrets_cache = None
_active_path = None   # 当前生效的配置路径；None 表示还没解析过


class ConfigError(Exception):
    """配置不合法。只在 SC_STRICT_CONFIG=1 时抛出，其余情况降级为告警。"""


def _resolve_path():
    """算出这次该读哪个文件。见模块文档里的三级优先级。"""
    global _active_path
    if _active_path:
        return _active_path
    # 1) 显式路径
    p = (os.environ.get("APP_CONFIG") or "").strip()
    if p:
        _active_path = p if os.path.isabs(p) else os.path.join(_ROOT, p)
        return _active_path
    # 2) 场景名
    name = (os.environ.get("APP_SCENE") or "").strip()
    if name:
        cand = os.path.join(SCENES_DIR, name if name.endswith(".json") else name + ".json")
        if os.path.exists(cand):
            _active_path = cand
            return _active_path
        print("[配置] APP_SCENE=%r 找不到对应文件，回落到 config/scene.json" % name)
    # 3) 默认
    _active_path = CONFIG_PATH
    return _active_path


def scene_path():
    """当前生效的配置文件路径。看板/健康检查会展示它，便于确认"跑的是哪套"。"""
    return _resolve_path()


def set_scene(name_or_path):
    """运行时切换场景。传场景名（retail_recall）或配置文件路径都行。

    切换后清空缓存，下一次 load() 重新读盘。已开始的会话不会回滚——
    话术在每轮回复时现读配置，所以下一轮就生效，适合演示时现场对比两套场景。

    返回：实际生效的路径。文件不存在抛 FileNotFoundError（切错了要当场知道）。
    """
    global _active_path, _cache
    candidate = name_or_path
    if not os.path.isabs(candidate) and not os.path.exists(candidate):
        cand = os.path.join(SCENES_DIR, candidate
                            if candidate.endswith(".json") else candidate + ".json")
        if os.path.exists(cand):
            candidate = cand
    if not os.path.exists(candidate):
        raise FileNotFoundError("场景文件不存在：%s" % name_or_path)
    _active_path = os.path.abspath(candidate)
    _cache = None
    return _active_path


def available_scenes():
    """列出 config/scenes/ 下所有可切换的场景。

    返回 [{id, scene_name, path, agent_name}]，按 id 排序。
    读坏了的文件不隐藏，带上 error 字段列出来——看不见的错误最难修。
    """
    out = []
    if not os.path.isdir(SCENES_DIR):
        return out
    for fn in sorted(os.listdir(SCENES_DIR)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(SCENES_DIR, fn)
        item = {"id": fn[:-5], "path": path, "scene_name": "", "agent_name": ""}
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            item["scene_name"] = d.get("scene_name", "")
            item["agent_name"] = d.get("agent_name", "")
        except Exception as e:
            item["error"] = str(e)
        out.append(item)
    return out


def load(force=False):
    """读配置。首次调用读盘，之后走缓存。

    参数 force=True 可强制重读，方便改完配置不重启服务就生效。
    首次读盘会跑一遍 validate()：有问题打印告警，SC_STRICT_CONFIG=1 时抛错。
    """
    global _cache
    if _cache is not None and not force:
        return _cache
    path = _resolve_path()
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    problems = validate(cfg)
    if problems:
        strict = os.environ.get("SC_STRICT_CONFIG") == "1"
        print("[配置] %s 有 %d 处问题：" % (os.path.basename(path), len(problems)))
        for msg in problems[:8]:
            print("   - %s" % msg)
        if len(problems) > 8:
            print("   ... 其余 %d 条见 python config_loader.py --check" % (len(problems) - 8))
        if strict:
            raise ConfigError("%s 配置不合法（SC_STRICT_CONFIG=1）" % path)
    _cache = cfg
    return _cache



def stage_config(stage):
    """取某个阶段的话术配置（reply / must_say 等）。取不到就返回空 dict。"""
    return load().get("stages", {}).get(stage, {}) or {}


def stages_config():
    """全部阶段配置。质检（M6）要按阶段取 must_say，所以需要一个整体视图。"""
    return dict(load().get("stages", {}) or {})


def writeback_template():
    """回写字段模板。真实实现要按 Mock 接口文档校准字段名。

    以 "_" 开头的键是给人看的注释（_comment / _todo），不能进业务单据——
    否则工单里会多出一个叫 "_comment" 的字段，对接真实接口时会被当成脏数据。
    """
    raw = load().get("writeback_template", {}) or {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def forbidden_words():
    """禁语词表（字面匹配用）。语义级检测留给 M6。"""
    return list(load().get("compliance", {}).get("forbidden_words", []))


def answer_keywords(kind):
    """取某类问题的触发关键词（如 fee / validity）。

    用途：用户问"这个要钱吗"时，能命中标准答法而不是让模型自由发挥。
    """
    val = load().get("answers", {}).get(kind, [])
    return list(val) if isinstance(val, list) else []


def apology_prefix():
    """安抚前缀。用户带情绪时拼在回复前面，避免每处都写一遍。"""
    return load().get("apology_prefix", "")


def asr_hints():
    """语音识别热词表（人名、产品名、工单号这类专有词）。

    热词属于业务内容，必须外置：换场景时只改 config，不改 voice.py。
    真实 ASR 服务一般支持 50–200 个热词，超了会被截断。
    """
    return list(load().get("asr", {}).get("hints", []))


def silence_config():
    """用户沉默时的处理话术。

    reply       第一次没说话时的追问
    reply_final 连续没说话，收尾挂断
    """
    return dict(load().get("silence", {}) or {})


def tts_config():
    """播报参数（音色、语速）。前端 speechSynthesis 与服务端流式 TTS 共用这一份。

    放在配置里而不是写死在前端：换音色属于业务决策，不该改代码。
    """
    return dict(load().get("tts", {}) or {})


def budget_config():
    """成本与容量闸门。字段含义见 config/scene.json 的 budget 段。

    这些数字是"先能观测，再决定要不要硬拦"：超出上限只结束本通电话，
    不做全局熔断——骨架阶段硬熔断会让演示莫名其妙失败。
    """
    return dict(load().get("budget", {}) or {})


def privacy_config():
    """隐私相关开关。当前只有一项：事件入库前是否把连续数字打码。"""
    return dict(load().get("privacy", {}) or {})


def compliance_categories():
    """按违规类型分组的词表（辱骂 / 虚假承诺 / 诱导）。

    和 forbidden_words 的区别：forbidden_words 是"说了就整句替换"的硬禁语，
    这里是"命中即判该项不合规"的分类词表，用于质检报告逐项给结论。
    """
    return dict(load().get("compliance", {}).get("categories", {}) or {})


def blocked_reply():
    """禁语命中后整句替换用的合规话术。

    为什么整句替换而不是打码：打码后的句子会被 TTS 念成"星星星星"，
    而且语义仍然带诱导性，等于既不通顺也没合规。
    """
    return load().get("compliance", {}).get("reply_blocked") or "这部分我需要请专员和您说明。"


def fallback_text(key="reply", default=""):
    """异常兜底话术（识别/编排/工具出错时用），与写回失败话术分开。"""
    return load().get("fallback", {}).get(key) or default


def scene_name():
    """当前场景名，展示在页面上，方便一眼看出跑的是哪套配置。"""
    return load().get("scene_name", "未命名场景")


# ---------------------------------------------------------------------------
# 密钥：与业务配置分开，绝不能进仓库
# ---------------------------------------------------------------------------

def _secrets():
    """读 config/secrets.json。文件不存在时返回空 dict，不报错。

    为什么不读就叫失败：没配密钥是一种正常状态（骨架默认用占位识别），
    不该让整个服务起不来。要不要云端识别由调用方判断。
    """
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache
    try:
        with open(SECRETS_PATH, "r", encoding="utf-8") as f:
            _secrets_cache = json.load(f)
    except FileNotFoundError:
        _secrets_cache = {}
    except Exception as e:
        # 文件存在但格式错，要说出来——否则会以为"密钥没配"而白查半天
        print("[配置] config/secrets.json 解析失败：%s" % e)
        _secrets_cache = {}
    return _secrets_cache


def secret(name, default=""):
    """取密钥。**环境变量优先，其次 secrets.json**。

    环境变量优先的理由：部署到云端沙箱时没有 secrets.json（也不该有），
    只能靠平台注入的环境变量。本地开发则写文件更省事，不用每次开终端设变量。
    环境变量名 = 键名大写，如 `dashscope_api_key` → `DASHSCOPE_API_KEY`。
    """
    env_name = name.upper()
    val = os.environ.get(env_name) or os.environ.get("SC_" + env_name)
    if val:
        return val.strip()
    val = (_secrets().get(name) or "").strip()
    return val or default


def asr_config():
    """语音识别相关配置：走哪家、用哪个模型、端点在哪。

    换服务商只改配置不改代码——这就是为什么 provider 放在这里，
    而不是在 voice.py 里写一堆 if。
    """
    return dict(load().get("asr", {}) or {})


def asr_provider():
    """当前用哪个识别实现。没配密钥时一律回落到 placeholder。

    这条回落很重要：没有密钥的机器（比如队友刚 clone 下来）必须能跑通整条链路，
    否则新人都卡在第一步。
    """
    cfg = asr_config()
    provider = (cfg.get("provider") or "placeholder").strip()
    if provider == "placeholder":
        return "placeholder"
    if not secret("dashscope_api_key"):
        return "placeholder"
    return provider



# ---------------------------------------------------------------------------
# schema 校验（M8）：把"跑到第三个环节才 KeyError"提前到启动那一眼
# ---------------------------------------------------------------------------

# 话术里允许的占位符。dialogue.py 只用 str.format(amount=..., status=...) 这两项，
# 多写一个 {foo} 不会在加载时报错，而是在那句话说出口的一刻抛 KeyError——
# 这种错在演示时最致命，所以在这里就拦下来。
_ALLOWED_PLACEHOLDERS = {"amount", "status"}

_REQUIRED_TOP = ("scene_name", "stages", "slots", "answers", "compliance", "budget")


def _is_text(v):
    return isinstance(v, str) and v.strip() != ""


def validate(cfg=None):
    """检查配置结构和内容，返回人话问题清单（空列表 = 通过）。

    只查"错了会在运行时炸"或"错了会静默失效"的项，不查文风：
        - 必备顶层字段在不在；
        - 七个阶段（contracts.STAGES）是否齐全，每段有没有 reply 与 must_say；
        - 话术里的占位符是不是 dialogue.py 认识的那两个；
        - 槽位名是否重复、是否缺 type；
        - 回写模板里有没有业务字段（全是 _ 开头等于空单据）；
        - 合规词表、预算数字的形制是否可用。

    为什么业务内容只查形制不查内容：话术好坏是业务方的事，代码只该保证
    "给了就能跑"；越界去审文风，最后会变成谁都改不动的配置。
    """
    if cfg is None:
        cfg = load()
    p = []

    for k in _REQUIRED_TOP:
        if k not in cfg:
            p.append("缺少顶层字段 %r" % k)
    if p:
        return p

    if not _is_text(cfg.get("scene_name")):
        p.append("scene_name 必须是非空字符串")

    stages = cfg.get("stages") or {}
    for st in STAGES:
        if st not in stages:
            p.append("stages 缺少阶段 %s（七个阶段由 contracts.STAGES 定义）" % st)
            continue
        sec = stages.get(st) or {}
        if not _is_text(sec.get("reply")):
            p.append("stages.%s.reply 缺失或为空" % st)
        if not isinstance(sec.get("must_say"), list):
            p.append("stages.%s.must_say 必须是列表（没有就写 []）" % st)
        # 占位符检查：只扫 reply* 这些字符串字段
        for key, val in sec.items():
            if key.startswith("_") or not isinstance(val, str):
                continue
            for token in _placeholders(val):
                if token not in _ALLOWED_PLACEHOLDERS:
                    p.append("stages.%s.%s 用了未知占位符 {%s}（只支持 %s）"
                             % (st, key, token, "/".join(sorted(_ALLOWED_PLACEHOLDERS))))
    for st in stages:
        if st not in STAGES:
            p.append("stages 出现未登记阶段 %r（要新增阶段先改 contracts.STAGES）" % st)

    slots = cfg.get("slots")
    if not isinstance(slots, list) or not slots:
        p.append("slots 必须是非空列表")
    else:
        seen = set()
        for i, s in enumerate(slots):
            if not isinstance(s, dict):
                p.append("slots[%d] 必须是对象" % i)
                continue
            name = s.get("name")
            if not _is_text(name):
                p.append("slots[%d].name 缺失" % i)
                continue
            if name in seen:
                p.append("slots 槽位名重复：%s" % name)
            seen.add(name)
            if not _is_text(s.get("type")):
                p.append("slots.%s.type 缺失（bool/str/object 之一）" % name)

    wb = cfg.get("writeback_template") or {}
    if not isinstance(wb, dict):
        p.append("writeback_template 必须是对象")
    elif not [k for k in wb if not k.startswith("_")]:
        p.append("writeback_template 里没有任何业务字段（全是 _ 开头的注释）")

    comp = cfg.get("compliance") or {}
    if not isinstance(comp.get("forbidden_words"), list):
        p.append("compliance.forbidden_words 必须是列表")
    if not _is_text(comp.get("reply_blocked")):
        p.append("compliance.reply_blocked 缺失（禁语命中后要有替换话术）")

    bud = cfg.get("budget") or {}
    for k in ("max_turns", "max_call_seconds"):
        v = bud.get(k)
        if not isinstance(v, (int, float)) or v <= 0:
            p.append("budget.%s 必须是正数" % k)
    for k in ("price_per_min_asr", "price_per_1k_chars_tts", "price_per_1m_tokens"):
        v = bud.get(k)
        if v is not None and (not isinstance(v, (int, float)) or v < 0):
            p.append("budget.%s 必须是非负数（单位：元）" % k)

    ans = cfg.get("answers") or {}
    for k in ("fee", "validity"):
        if not isinstance(ans.get(k), list):
            p.append("answers.%s 必须是关键词列表" % k)

    if not _is_text(cfg.get("apology_prefix", "")):
        p.append("apology_prefix 建议给一句安抚话术（用户带情绪时会拼在回复前）")

    return p


def _placeholders(text):
    """取出字符串里的 {xxx} 占位符名。不做完整 format 解析，够用即可。"""
    out, buf, depth = [], [], 0
    for ch in text:
        if ch == "{":
            depth += 1
            buf = []
        elif ch == "}" and depth:
            depth -= 1
            name = "".join(buf).strip()
            # 空括号、纯数字（"{0}"）也当占位符报出来，宁严勿漏
            if name:
                out.append(name)
        elif depth:
            buf.append(ch)
    return out


def describe():
    """一句话描述当前场景，给健康检查和看板用。"""
    cfg = load()
    return {
        "scene_name": cfg.get("scene_name", "未命名场景"),
        "agent_name": cfg.get("agent_name", ""),
        "company": cfg.get("company", ""),
        "path": scene_path(),
        "stages": len(cfg.get("stages") or {}),
        "forbidden_words": len(forbidden_words()),
        "problems": len(validate(cfg)),
    }


# ---------------------------------------------------------------------------
# 命令行：部署前自检用，不依赖服务是否在跑
#   python config_loader.py            # 校验当前场景
#   python config_loader.py --list     # 列出所有可切换场景
#   python config_loader.py --use retail_recall   # 校验指定场景
# ---------------------------------------------------------------------------

def _main(argv):
    if "--list" in argv:
        rows = available_scenes()
        print("可用场景（%d 个）：" % len(rows))
        for r in rows:
            mark = "*" if os.path.abspath(r["path"]) == os.path.abspath(scene_path()) else " "
            print("  %s %-20s %s" % (mark, r["id"], r.get("scene_name") or r.get("error", "")))
        return 0

    if "--use" in argv:
        i = argv.index("--use")
        name = argv[i + 1] if len(argv) > i + 1 else ""
        if not name:
            print("用法：python config_loader.py --use <场景名>")
            return 2
        try:
            path = set_scene(name)
        except FileNotFoundError as e:
            print(str(e))
            return 2
        print("已切换到：%s" % path)

    d = describe()
    print("当前场景：%s（%s）" % (d["scene_name"], d["path"]))
    print("  客服名：%s    阶段数：%d    禁语：%d 条" % (d["agent_name"], d["stages"], d["forbidden_words"]))
    problems = validate()
    if not problems:
        print("  校验：通过")
        return 0
    print("  校验：%d 处问题" % len(problems))
    for msg in problems:
        print("   - %s" % msg)
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
