"""场景配置加载器（M8 的一部分）。

用途：
    赛题的"详细业务场景"要报名后才公布，所以业务内容（话术、槽位、禁语、
    回写字段）必须全部外置。换场景 = 换 config/scene.json，代码不动。
    这是应对"场景未知"这个最大变量的唯一办法。

设计取舍：
    只用标准库 json，不引入 PyYAML。配置文件里的注释用 "_comment" 字段承载。

后续待补充：
    - TODO 支持环境变量指定配置文件（APP_CONFIG=config/scene_b.json），
      便于同一套代码跑两套场景做对比；
    - TODO 加 schema 校验，配置写错要在启动时报错，而不是跑到一半 KeyError。
"""

import json
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_ROOT, "config", "scene.json")

_cache = None   # 进程内缓存，避免每次请求都读盘


def load(force=False):
    """读配置。首次调用读盘，之后走缓存。

    参数 force=True 可强制重读，方便改完配置不重启服务就生效。
    """
    global _cache
    if _cache is not None and not force:
        return _cache
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        _cache = json.load(f)
    return _cache


def stages_order():
    """状态机阶段顺序，直接来自 contracts.STAGES，不单独配置。

    刻意不让配置决定"有哪些阶段"——阶段是代码契约，话术才是配置。
    """
    from contracts import STAGES
    return list(STAGES)


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
