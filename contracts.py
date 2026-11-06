"""契约层 —— 全项目唯一的"禁改区"。

用途：
    把跨模块的协议集中在一处，四个人并行开发时谁都不许私改。
    改这里的任何一项，都必须走 PR 并通知全员。

约定：
    1. 事件类型、状态机阶段名、工具名与参数名，一旦定稿只增不改；
       要改语义，必须升 EVENT_VERSION 并写迁移说明。
    2. 业务内容（话术、槽位、是否必说）不放在这里，全部放 config/scene.json，
       本文件只定义"结构"，不定义"内容"。
    3. 本文件不允许 import 项目内其他模块，保持零依赖。

后续待补充：
    - 事件类型会随真实实现增加（如 asr_partial、vad_end、tts_first_packet），
      新增时只往下面的事件类型集合里追加，不要改动已有名字。
    - 工具的参数结构在接入真实 Mock 接口后需要按接口文档校正。
"""

# 事件协议版本。字段语义变化时升级此版本号。
EVENT_VERSION = "v1"

# 事件记录必须且只能包含这 7 个字段（事件 schema 契约）。
# 顺序仅用于文档说明，实际落库见 events.py。
EVENT_FIELDS = (
    "session_id",  # 一次通话的唯一标识
    "turn_id",     # 通话内的第几轮，从 1 开始
    "ts",          # 事件发生时间（毫秒时间戳，服务端时间）
    "type",        # 事件类型，取 EVENT_TYPES 中的值
    "payload",     # 事件内容（dict，允许为空）
    "latency_ms",  # 该事件对应环节的耗时，无耗时概念则为 0
    "version",     # 写入时的事件协议版本，便于将来回溯
)

# 事件类型全集。
# 命名规则：<主体>_<动作>，全部小写下划线。
EVENT_TYPES = {
    "call_started",   # 通话开始（浏览器点了"开始通话"）
    "turn_start",     # 一轮交互开始（用户开口或打字）
    "audio_stream",   # 一轮采集结束时的音频流统计（帧数/采样数/序号缺口），S1 新增
    "vad_result",     # 端点检测结果（是否有人说话、语音时长）
    "asr_result",     # 语音识别出最终文本（哑链路里是假的）
    "no_answer",      # 用户这一轮没说话（沉默 / 空输入），用于统计静默率
    "agent_reply",    # 对话编排产出回复文本
    "tool_call",      # 发起一次业务工具调用
    "tool_result",    # 业务工具返回结果
    "writeback",      # 结构化结果回写成功（闭环完成的关键证据）
    "tts_start",      # 开始播报
    "barge_in",       # 用户打断（哑链路暂未实现，留口）
    "turn_end",       # 一轮交互结束，latency_ms 记本轮端到端总耗时
    "latency_report",  # 浏览器实测的首字响应（T4−T0），由前端上报
    "error",          # 某个环节出错（不中断通话，但要能被统计）
    "call_ended",     # 通话结束
}

# 首字响应时延门槛（赛题硬门槛，单位毫秒）。
# 放在契约层而不是配置里：它是"对外承诺的指标"，不随场景变化。
LATENCY_GATE_MS = 2000

# 音频流的格式契约（S1 定稿）：浏览器采集 ←→ 服务端接收 ←→ ASR/TTS。
# 为什么格式也要进契约：1 号写前端采集、2 号接 ASR、4 号做传输层，
# 三方只要有一处把 16k 写成 8k 或把单声道写成双声道，声音就会变成噪音，
# 而且症状是"能跑但识别很差"，最难排查。
PCM_SAMPLE_RATE = 16000   # 采样率：ASR 通用输入格式，16kHz
PCM_SAMPLE_WIDTH = 2      # 位宽：PCM16LE，每采样 2 字节
PCM_CHANNELS = 1          # 声道：单声道
FRAME_MS = 20             # 一帧时长：20ms（流式 ASR 的常规分块）
FRAME_SAMPLES = PCM_SAMPLE_RATE * FRAME_MS // 1000   # 每帧采样数 = 320
FRAME_BYTES = FRAME_SAMPLES * PCM_SAMPLE_WIDTH       # 每帧字节数 = 640


# 任务状态机的阶段名（状态机契约）。
# 顺序即推进顺序，对应赛题要求的 6 个业务环节 + 2 个边界状态。
# 谁要新增阶段，必须在这里加，并在 dialogue.py 里补迁移规则。
STAGES = (
    "GREETING",   # 开场自报身份
    "IDENTITY",   # 身份确认
    "NEED",       # 需求确认
    "OFFER",      # 方案或权益沟通
    "CONFIRM",    # 结果确认
    "WRITEBACK",  # 结构化结果回写
    "DONE",       # 通话目的已达成，可结束
)

# 终止态：进入后不再推进业务环节（但用户改口仍要能更正回写）。
TERMINAL_STAGE = "DONE"

# 刻意不提供 "CLOSED_STAGES" 这类"用阶段名判闭环"的常量。
# 原因：曾有过一个 `CLOSED_STAGES = ("WRITEBACK", "DONE")`，被用来判"是否闭环"，
# 结果走到 DONE 就算闭环——可 DONE 只是礼貌收尾，回写失败也会走到 DONE。
# 闭环的唯一证据是 `writeback` 事件（工单真的建出来了），不要用阶段名推断。

# 工具契约。键为工具名，值为参数与返回结构说明。
# 真实实现（M4）必须严格按此签名，参数名不得改。
TOOL_SCHEMAS = {
    "query_customer": {
        "desc": "按姓名或手机号后四位查客户档案",
        "params": {"keyword": "str"},
        "returns": {"found": "bool", "customer": "dict|null"},
        "idempotent": True,
    },
    "query_order": {
        "desc": "按客户 ID 查最近一笔订单",
        "params": {"customer_id": "str"},
        "returns": {"found": "bool", "order": "dict|null"},
        "idempotent": True,
    },
    # C5 新增（只增不改）：POP 资质驳回申请单定位。keyword 可为联系人姓名、
    # 手机后四位、公司全称、申请单号四路径之一；返回整条申请单档案。
    "query_application": {
        "desc": "按姓名/手机后四位/公司名/申请单号查 POP 资质驳回申请单",
        "params": {"keyword": "str"},
        "returns": {"found": "bool", "application": "dict|null"},
        "idempotent": True,
    },
    "create_ticket": {
        "desc": "登记工单，完成结构化回写",
        "params": {
            "session_id": "str",
            "content": "dict",          # 结构化字段，形状见 config/scene.json 的 writeback_template
            "idempotency_key": "str",   # 幂等键：同一键重复调用只产生一条工单
            "version": "int",           # 选填，第几次回写（从 1 开始）；改口时递增
        },
        "returns": {"ticket_id": "str", "created": "bool", "updated": "bool",
                    "version": "int"},
        # 写操作必须幂等，这是赛题明确要求的"调用幂等性与一致性"。
        # 注意幂等的边界：同一个键 → 同一条结果；**不同版本** → 同一张工单被更新，
        # 而不是另开一张。否则用户改口会在业务系统里留下两条互相矛盾的记录。
        "idempotent": True,
    },
}

# 合规底线：命中这些即视为严重违规（赛题原文：辱骂歧视、虚假承诺、诱导、
# 泄露他人隐私、使用禁用话术）。具体词表放 config，这里只定义检查项名称。
COMPLIANCE_CHECKS = (
    "no_insult",        # 无辱骂歧视
    "no_false_promise", # 无虚假承诺
    "no_inducement",    # 无诱导
    "no_privacy_leak",  # 无泄露他人隐私
    "must_say_hit",     # 必说话术已说
)
