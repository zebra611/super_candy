"""M6 质检与 M7 评测/自进化 —— 哑链路里的"占位件"。

这两个模块在骨架阶段**故意只留空壳**，因为它们必须等事件流攒够真实数据
才有意义：没有几十通真通话，评测集和失败归因都是编的。

但接口先定下来，好处是：角色 3 可以直接照着函数签名开工，
不用等别人把在线链路做完。

负责人：角色 3（质检评测与自进化）

后续待补充（这就是角色 3 的开工清单）：
    1. transcribe_session: 拉事件流里的 asr_result，拼成完整对话记录；
    2. compliance_report: 逐条跑 COMPLIANCE_CHECKS，出合规率与命中明细；
    3. build_eval_set: 构造评测集（正常/缺槽/拒接/情绪激动/诱导违规/多轮改口）；
    4. simulate_user: 自动扮演对抗性客户，把评测从"人工点"变成"跑批"；
    5. judge: 规则评分 + LLM-as-Judge 双通道，同一输入必须同一分数（可复现）；
    6. cluster_failures: 失败聚类到 没听清/没听懂/没查对/说错了/漏环节；
    7. propose_optimization: 产出优化项（改提示词/话术/知识/工具策略）；
    8. regression: 优化前后跑同一批用例，出对比表，不达标回滚。
"""

import events
from contracts import CLOSED_STAGES, COMPLIANCE_CHECKS


def transcribe_session(session_id):
    """把一通电话的事件流还原成"谁在什么时候说了什么"。

    哑链路实现：只挑 asr_result / agent_reply 两类事件拼成列表。
    TODO 真实实现要处理：打断造成的半句、ASR 中间结果去重、时间对齐。

    返回：list[dict(role, text, ts)]
    """
    turns = []
    for ev in events.list_events(session_id):
        if ev["type"] == "asr_result":
            turns.append({"role": "user", "text": ev["payload"].get("text", ""), "ts": ev["ts"]})
        elif ev["type"] == "agent_reply":
            turns.append({"role": "agent", "text": ev["payload"].get("text", ""), "ts": ev["ts"]})
    return turns


def compliance_report(session_id):
    """出一份质检报告。

    哑链路实现：只统计"有没有走到回写"和"有没有出现禁语命中"，
    其余检查项返回 skipped —— 宁可显示"未检测"，也不要假装检测过了。

    返回：dict(session_id, checks, closed, score)
    """
    evs = events.list_events(session_id)
    closed = any(e["type"] == "writeback" for e in evs)
    blocked = sum(1 for e in evs if e["type"] == "agent_reply"
                  and e["payload"].get("blocked"))

    checks = {}
    for name in COMPLIANCE_CHECKS:
        if name == "must_say_hit":
            checks[name] = {"status": "skipped", "note": "需要必说话术命中检测，待实现"}
        else:
            # 骨架阶段只有禁语命中这一个真信号
            checks[name] = {"status": "pass" if blocked == 0 else "warn",
                            "note": "基于字面禁语匹配，语义检测待实现"}

    return {
        "session_id": session_id,
        "closed": closed,
        "must_say_stage_reached": closed,
        "blocked_count": blocked,
        "checks": checks,
        # 分数是占位公式，只为让看板有数字，不代表真实质检水平
        "score": 1.0 if closed and blocked == 0 else 0.0,
        "is_placeholder": True,
    }


def score_session(session_id):
    """给一通电话打分（评测基线用）。

    哑链路实现：只看"是否闭环"，返回 0 或 1。
    TODO 真实实现要按六维评分中可自动化的部分算分，并保证同一输入同一分数。

    返回：dict(session_id, closed, score, detail)
    """
    evs = events.list_events(session_id)
    stages_seen = {e["payload"].get("stage") for e in evs if e["type"] == "agent_reply"}
    closed = bool(stages_seen & set(CLOSED_STAGES))
    return {
        "session_id": session_id,
        "closed": closed,
        "score": 1.0 if closed else 0.0,
        "detail": {"stages_seen": sorted(s for s in stages_seen if s)},
        "is_placeholder": True,
    }


def run_batch(session_ids=None):
    """批量跑评测，产出基线分表。

    哑链路实现：把最近的会话逐个 score_session 后求平均。
    TODO 真实实现要支持指定版本号、指定评测集、输出对比表。

    返回：dict(count, mean_score, items)
    """
    if session_ids is None:
        session_ids = [s["session_id"] for s in events.recent_sessions(limit=50)]
    items = [score_session(sid) for sid in session_ids]
    mean = round(sum(i["score"] for i in items) / len(items), 3) if items else 0.0
    return {"count": len(items), "mean_score": mean, "items": items, "is_placeholder": True}
