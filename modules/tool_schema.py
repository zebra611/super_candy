"""工具 schema 导出 + 函数调用入口（T13：Function Calling / MCP 兼容层）。

赛题原文要求"支持通过 Function Calling 或 MCP 等方式调用名单、订单和工单系统"。
这一层只做两件事，**不做第三件**：

    1. 把 `contracts.TOOL_SCHEMAS`（全项目唯一的工具契约）导出成
       OpenAI tools 格式 与 MCP tools 格式；
    2. 提供一个 `call_function(name, arguments)` 入口，把外部传进来的
       函数调用翻译成 `tools.dispatch()` 已经认得的形状。

    **不做**的是"让 LLM 决定调哪个工具"。
    400ms 的措辞预算是硬上限（docs/17 §3.2），"模型先选工具再生成"这一轮
    就吃掉两倍时间；而且一旦让模型决定，就会出现"模型觉得不用核验身份"这类
    不可复现的行为。工具必须由状态机在确定的位置触发（docs/17 §1 的三分边界）。

用法：
    python3 -m modules.tool_schema            # 打印导出的 schema，肉眼核对
    python3 -m modules.tool_schema --mcp      # 打 MCP 形状
"""

from __future__ import annotations

import json
import sys

import contracts
from modules import tools

#: 契约里标了"选填"的参数。其余一律必填。
OPTIONAL_PARAMS = {
    ("create_ticket", "version"),
}

_JSON_TYPES = {
    "str": "string",
    "int": "integer",
    "number": "number",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
}


def _params_of(tool_name, schema):
    params = schema.get("params") or {}
    properties = {}
    required = []
    for pname, ptype in params.items():
        properties[pname] = {"type": _JSON_TYPES.get(ptype, "string")}
        if (tool_name, pname) not in OPTIONAL_PARAMS:
            required.append(pname)
    return properties, required


def to_openai_tools():
    """导出 OpenAI tools 数组（`tools=[...]` 直接可用）。

    形状：{"type":"function","function":{name, description, parameters}}
    parameters 是标准 JSON Schema，strict 客户端会校验 required 与 type。
    """
    out = []
    for name, schema in contracts.TOOL_SCHEMAS.items():
        properties, required = _params_of(name, schema)
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": schema.get("desc", ""),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        })
    return out


def to_mcp_tools():
    """导出 MCP 的 tools/list 形状（name / description / inputSchema）。

    与 OpenAI 形状的差别只有外层包一层 inputSchema，字段名换成 MCP 的叫法；
    两边都从 contracts 生成，不会出现"改了契约忘了改 schema"。
    """
    out = []
    for name, schema in contracts.TOOL_SCHEMAS.items():
        properties, required = _params_of(name, schema)
        out.append({
            "name": name,
            "description": schema.get("desc", ""),
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        })
    return out


def validate_export():
    """自检：导出的 JSON 能过"OpenAI tools 格式"的硬约束。

    不引 jsonschema（项目零依赖），只查最容易踩的四条：
    工具名非空且唯一、type 是 function、parameters 是 object、required 都在 properties 里。
    返回 (ok, errors)。
    """
    errors = []
    tools_json = to_openai_tools()
    seen = set()
    if not isinstance(tools_json, list) or not tools_json:
        return False, ["导出的不是非空数组"]

    for item in tools_json:
        if item.get("type") != "function":
            errors.append("type 必须是 function：%r" % (item.get("type"),))
        fn = item.get("function") or {}
        name = fn.get("name")
        if not name:
            errors.append("工具名不能为空")
            continue
        if name in seen:
            errors.append("工具名重复：%s" % name)
        seen.add(name)
        if name not in contracts.TOOL_SCHEMAS:
            errors.append("导出了契约里没有的工具：%s" % name)
        params = fn.get("parameters") or {}
        if params.get("type") != "object":
            errors.append("%s 的 parameters.type 必须是 object" % name)
        props = set((params.get("properties") or {}).keys())
        for req in params.get("required") or []:
            if req not in props:
                errors.append("%s 的 required 里有未声明的参数：%s" % (name, req))
        for pname, pspec in (params.get("properties") or {}).items():
            if pspec.get("type") not in ("string", "integer", "number",
                                         "boolean", "object", "array"):
                errors.append("%s.%s 的 type 不合法：%r" % (name, pname, pspec.get("type")))

    missing = set(contracts.TOOL_SCHEMAS) - seen
    if missing:
        errors.append("契约里的工具没导出：%s" % sorted(missing))
    return (not errors), errors


def call_function(name, arguments):
    """函数调用入口：`name` + `arguments`（JSON 字符串或 dict）→ `tools.dispatch`。

    arguments 允许是字符串，是因为 Function Calling 的返回值本来就是 JSON 字符串，
    再让调用方自己 parse 一遍只会多一个出错的地方。
    **不**在这里做工具选择——name 是谁给的，调用方负责。
    """
    args = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except json.JSONDecodeError as exc:
            return {"error": "INVALID_ARG", "message": "arguments 不是合法 JSON：%s" % exc,
                    "_ms": 0.0}
    if not isinstance(args, dict):
        return {"error": "INVALID_ARG", "message": "arguments 必须是 JSON 对象",
                "_ms": 0.0}
    return tools.dispatch({"name": name, "args": args})


def _main(argv):
    as_mcp = "--mcp" in argv
    payload = to_mcp_tools() if as_mcp else to_openai_tools()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not as_mcp:
        ok, errs = validate_export()
        print("\n# 格式自检：%s" % ("通过" if ok else "失败"), file=sys.stderr)
        for e in errs:
            print("#  - %s" % e, file=sys.stderr)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
