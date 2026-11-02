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


def writeback_template():
    """回写字段模板。真实实现要按 Mock 接口文档校准字段名。"""
    return dict(load().get("writeback_template", {}))


def forbidden_words():
    """禁语词表（字面匹配用）。语义级检测留给 M6。"""
    return list(load().get("compliance", {}).get("forbidden_words", []))


def scene_name():
    """当前场景名，展示在页面上，方便一眼看出跑的是哪套配置。"""
    return load().get("scene_name", "未命名场景")
