"""适配器工厂 —— 按 `tools.provider` 选实现，默认 mock。

这一层是 `tools.py` 与"数据从哪来"之间的唯一开关：
    config/scene.json  tools.provider = "mock" | "real"
    tools.timeout_ms  = 300
    tools.base_url    = http://127.0.0.1:8100

切换只改配置，不改代码——`tools.dispatch()` 的签名与返回形状一个字都不动。
"""

from __future__ import annotations

from .base import (DEFAULT_BASE_URL, DEFAULT_TIMEOUT_MS, ERROR_CODES,
                   ERROR_INVALID_ARG, ERROR_NOT_FOUND, ERROR_TIMEOUT,
                   ERROR_UNKNOWN_TOOL, ERROR_UPSTREAM, TOOL_METHODS, BaseAdapter,
                   ToolError)
from .mock import MockAdapter
from .real import RealAdapter

ADAPTERS = {"mock": MockAdapter, "real": RealAdapter}

_DEFAULT = MockAdapter
_cache = {}


def adapter_class(provider):
    """provider 名 -> 适配器类。未知 provider 一律退回 mock（不因为配置写错就跑不起来）。"""
    return ADAPTERS.get((provider or "").strip().lower(), _DEFAULT)


def get_adapter(cfg=None, fresh=False):
    """取当前 provider 的适配器单例。

    每次都用 config_loader.tools_config() 现读，是为了让"改配置就能切"成立——
    自检脚本会临时改 provider，缓存住旧实例会读到错的那一个。
    """
    if cfg is None:
        import config_loader
        cfg = config_loader.tools_config()
    provider = (cfg.get("provider") or "mock").strip().lower()
    if provider not in ADAPTERS:
        provider = "mock"
    store = (cfg.get("idempotency_store") or "memory").strip().lower()
    store_path = str(cfg.get("idempotency_path") or "")
    key = (provider, str(cfg.get("base_url") or ""), int(cfg.get("timeout_ms") or 0),
           store, store_path)
    if fresh or key not in _cache:
        _cache.clear()  # 同一时刻只留一个实例，避免两套幂等存储并存
        ad = adapter_class(provider)(cfg)
        if store == "sqlite" and isinstance(ad, MockAdapter):
            from .store import DEFAULT_PATH, attach_sqlite
            attach_sqlite(ad, store_path or DEFAULT_PATH)
        _cache[key] = ad
    return _cache[key]


def reset_adapter():
    """清掉缓存与内存态（自检用）。

    注意：**落库的适配器不清**。`_reset_for_test()` 的本意是让自检可重复，
    如果它顺手把 `tickets.db` 也删了，那"重启后幂等仍然成立"这个能力就永远测不出来，
    线上一跑自检还会把真工单抹掉。
    """
    for a in list(_cache.values()):
        reset = getattr(a, "reset", None)
        if callable(reset) and not getattr(a, "_persistent", False):
            reset()
    _cache.clear()


__all__ = [
    "ADAPTERS", "BaseAdapter", "MockAdapter", "RealAdapter", "ToolError",
    "adapter_class", "get_adapter", "reset_adapter",
    "DEFAULT_TIMEOUT_MS", "DEFAULT_BASE_URL", "TOOL_METHODS",
    "ERROR_CODES", "ERROR_NOT_FOUND", "ERROR_INVALID_ARG", "ERROR_UPSTREAM",
    "ERROR_TIMEOUT", "ERROR_UNKNOWN_TOOL",
]
"""适配器工厂 —— 按 `tools.provider` 选实现，默认 mock。

这一层是 `tools.py` 与"数据从哪来"之间的唯一开关：
    config/scene.json  tools.provider = "mock" | "real"
    tools.timeout_ms  = 300
    tools.base_url    = http://127.0.0.1:8100

切换只改配置，不改代码——`tools.dispatch()` 的签名与返回形状一个字都不动。
"""

from __future__ import annotations

from .base import (DEFAULT_BASE_URL, DEFAULT_TIMEOUT_MS, ERROR_CODES,
                   ERROR_INVALID_ARG, ERROR_NOT_FOUND, ERROR_TIMEOUT,
                   ERROR_UNKNOWN_TOOL, ERROR_UPSTREAM, TOOL_METHODS, BaseAdapter,
                   ToolError)
from .mock import MockAdapter
from .real import RealAdapter

ADAPTERS = {"mock": MockAdapter, "real": RealAdapter}

_DEFAULT = MockAdapter
_cache = {}


def adapter_class(provider):
    """provider 名 -> 适配器类。未知 provider 一律退回 mock（不因为配置写错就跑不起来）。"""
    return ADAPTERS.get((provider or "").strip().lower(), _DEFAULT)


def get_adapter(cfg=None, fresh=False):
    """取当前 provider 的适配器单例。

    每次都用 config_loader.tools_config() 现读，是为了让"改配置就能切"成立——
    自检脚本会临时改 provider，缓存住旧实例会读到错的那一个。
    """
    if cfg is None:
        import config_loader
        cfg = config_loader.tools_config()
    provider = (cfg.get("provider") or "mock").strip().lower()
    if provider not in ADAPTERS:
        provider = "mock"
    key = (provider, str(cfg.get("base_url") or ""), int(cfg.get("timeout_ms") or 0))
    if fresh or key not in _cache:
        _cache.clear()  # 同一时刻只留一个实例，避免两套幂等存储并存
        _cache[key] = adapter_class(provider)(cfg)
    return _cache[key]


def reset_adapter():
    """清掉缓存与内存态（自检用）。"""
    for a in list(_cache.values()):
        reset = getattr(a, "reset", None)
        if callable(reset):
            reset()
    _cache.clear()


__all__ = [
    "ADAPTERS", "BaseAdapter", "MockAdapter", "RealAdapter", "ToolError",
    "adapter_class", "get_adapter", "reset_adapter",
    "DEFAULT_TIMEOUT_MS", "DEFAULT_BASE_URL", "TOOL_METHODS",
    "ERROR_CODES", "ERROR_NOT_FOUND", "ERROR_INVALID_ARG", "ERROR_UPSTREAM",
    "ERROR_TIMEOUT", "ERROR_UNKNOWN_TOOL",
]
