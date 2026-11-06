# -*- coding: utf-8 -*-
"""C6：嘈杂人声 / 环境噪声鲁棒性实测（进程内，pop 场景，确定性）。

注入的是**ASR 输出层的噪声文本**——真实事故里 qwen3-asr-flash 把咳嗽
识别成 "Cough."、旁人说话被当成用户发言、TTS 自己的声音被录回去。
文本层做不了「听清」，只能验「不误伤」，四条底线：

  ① 噪声轮不挂断；② 不烧核身失败次数 / 追问次数 / 含糊计数；
  ③ 不被当成同意回写；④ 噪声之后用户正常说话仍能走完整闭环（恢复力）。

每个样例注入**连续 3 轮**噪声。只注入一轮会假绿：修复前噪声被记进
identity_vague / slot_asks，第 2 声咳嗽才挂断人，单轮探针正好绕开。

用法：
    python scripts/test_noise_robustness.py               # 实测，必须全绿
    python scripts/test_noise_robustness.py --self-test   # 变异自测，必须抓到红

--self-test 是防假绿的关键：故意让防护失效（噪声容忍度退化 / 双通道
同时失效），探针必须报红。如果变异后仍全绿，说明断言根本没在盯东西。
"""
from __future__ import annotations

import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.environ["EVAL_SCENE"] = "pop_reject"
import run_eval as R              # noqa: E402  复用隔离事件库与确定性配置
import app as A                   # noqa: E402

# 连续三轮同类噪声，贴近真实嘈杂环境（不是只咳嗽一声就安静了）。
NOISE_BURSTS = [
    ("N01", "咳嗽/喷嚏被识别成英文", ["Cough.", "Sneeze.", "Ahem."]),
    ("N02", "旁人说话（中文环境人声）", ["那个订单你放桌上吧", "午饭吃什么呀", "门没关"]),
    ("N03", "纯英文环境音", ["OK, thank you", "Yeah right", "Sorry, go ahead"]),
    ("N04", "ASR 乱码 / 口头禅", ["可爱你。", "嗯那个啥", "咕噜"]),
    ("N05", "3 位尾号（半句）", ["111", "12", "888"]),
    ("N06", "TTS 回声自激", ["核对一下申请单", "感谢您的接听", "您方便再说一次吗"]),
    ("N07", "混合噪声（真实嘈杂环境）", ["Cough.", "那个订单你放桌上吧", "OK, thank you"]),
]

# 静音单列：连续两次完全无识别结果，按设计视为疑似挂机而收尾。
# 它不属于「嘈杂人声」，不能混进上面那组的不挂断断言里充数。
SILENCE = ("S01", "连续静音两次（设计边界）", ["", ""])

HANG_REASONS = ("identity_failed", "unclear", "no_voice", "jingmai_no_call",
                "silence", "hangup", "refused_identity", "handoff")

APPS = json.load(open(os.path.join(ROOT, "config", "mock", "applications.json"),
                      encoding="utf-8"))["items"]
TAIL = next(r["verify_by_tail"] for r in APPS
            if r["close_mode"] == "电话" and r["call_status"] not in ("无需外呼", "无效号码"))


def _wb(sid):
    return sum(1 for e in R.events.list_events(sid) if e["type"] == "writeback")


def _counters(sid):
    s = A._SESSIONS[sid]
    return (s.get("identity_attempts", 0),
            s.get("identity_vague", 0),
            sum((s.get("slot_asks") or {}).values()))


def _greet(sid):
    R.handle_turn(sid, user_text="我是负责人，你说。")


def run_probe(verbose=True):
    """返回结果列表 (code, label, ok, why)。"""
    results = []
    for code, label, burst in NOISE_BURSTS:
        sid = R.start_call()["session_id"]
        _greet(sid)
        base = _counters(sid)
        wb0 = _wb(sid)
        why = []

        for noise in burst:
            o = R.handle_turn(sid, user_text=noise)
            now = _counters(sid)
            if not (o.get("reply_text") or "").strip():
                why.append("无回复")
            if (o.get("end_reason") or "") in HANG_REASONS:
                why.append("误挂断:%s" % o.get("end_reason"))
            if now[0] != base[0]:
                why.append("烧核身次数 %s→%s" % (base[0], now[0]))
            if now[1] != base[1]:
                why.append("烧含糊计数 %s→%s" % (base[1], now[1]))
            if now[2] != base[2]:
                why.append("烧追问次数 %s→%s" % (base[2], now[2]))
        if _wb(sid) != wb0:
            why.append("噪声触发了回写")

        # ④ 恢复力：噪声之后正常报尾号 → 陈述业务域 → 确认 → 关单
        R.handle_turn(sid, user_text="尾号%s。" % TAIL)
        R.handle_turn(sid, user_text="我申请的品牌授权没过")
        R.handle_turn(sid, user_text="行，清楚了")
        last = R.handle_turn(sid, user_text="对，清楚了")
        final = R.events.list_events(sid)
        if not (any(e["type"] == "writeback" for e in final)
                and last.get("next_stage") in ("WRITEBACK", "DONE")):
            why.append("噪声后无法恢复闭环")
        R.end_call(sid)

        ok = not why
        results.append((code, label, ok, "；".join(why) or "-"))
        if verbose:
            print("  [%s] %s %-26s %s" %
                  ("PASS" if ok else "FAIL", code, label, "" if ok else results[-1][3]))
    return results


def run_silence(verbose=True):
    """静音是设计边界：第二次无应答应礼貌收尾，但不得回写、不得记核身失败。"""
    sid = R.start_call()["session_id"]
    _greet(sid)
    base = _counters(sid)
    o1 = R.handle_turn(sid, user_text="")
    o2 = R.handle_turn(sid, user_text="")
    ok = (not (o1.get("end_reason") or "")
          and o2.get("end_reason") == "silence"
          and _wb(sid) == 0
          and _counters(sid) == base)
    why = "o1=%r o2=%r counters=%s/%s wb=%d" % (
        o1.get("end_reason"), o2.get("end_reason"), base, _counters(sid), _wb(sid))
    R.end_call(sid)
    if verbose:
        print("  [%s] %s %-26s %s" %
              ("PASS" if ok else "FAIL", SILENCE[0], SILENCE[1], "" if ok else why))
    return ok


def self_test():
    """两种变异都必须让噪声探针报红；否则说明断言是摆设。"""
    from modules import intent, dialogue

    checks = []

    # M1：噪声容忍度退化成 1 轮（第一声咳嗽就挂断）
    orig_max = dialogue.NOISE_MAX
    dialogue.NOISE_MAX = 1
    try:
        r = run_probe(verbose=False)
    finally:
        dialogue.NOISE_MAX = orig_max
    checks.append(("M1 噪声容忍度退化为 1 轮 → 探针应报红",
                   any(not x[2] for x in r),
                   sum(1 for x in r if not x[2])))

    # M2：双层防护同时失效（noise 标记被抹 + 任何输入都当成有效槽位）
    # 这模拟修复前的形态：环境人声被当成用户发言，堂而皇之地烧核身次数。
    orig_cls, orig_take = intent.classify, dialogue._take_identity_slots

    def broken_classify(t, *a, **k):
        f = dict(orig_cls(t, *a, **k))
        if f.get("evidence") == "noise":
            f["evidence"] = "none"
            f["vague"] = False
        return f

    def broken_take(sess, text, flags):
        orig_take(sess, text, flags)
        return ["name"]

    intent.classify = broken_classify
    dialogue._take_identity_slots = broken_take
    try:
        r2 = run_probe(verbose=False)
    finally:
        intent.classify, dialogue._take_identity_slots = orig_cls, orig_take
    checks.append(("M2 噪声双通道同时失效 → 探针应报红",
                   any(not x[2] for x in r2),
                   sum(1 for x in r2 if not x[2])))

    for label, ok, nred in checks:
        print("  [%s] %s（红 %d 条）" % ("PASS" if ok else "FAIL", label, nred))
    print("=" * 70)
    npass = sum(1 for _, ok, _ in checks if ok)
    print("C6 变异自测：%d/%d 通过（全绿后必须能变红，才不是假绿）"
          % (npass, len(checks)))
    return npass == len(checks)


def main():
    if "--self-test" in sys.argv:
        return 0 if self_test() else 1
    print("C6 噪声鲁棒性实测（每组连续 3 轮噪声，pop 场景）")
    print("-" * 70)
    results = run_probe()
    s_ok = run_silence()
    print("=" * 70)
    npass = sum(1 for r in results if r[2])
    print("噪声鲁棒性：%d/%d 通过；静音设计边界：%s"
          % (npass, len(results), "符合" if s_ok else "不符"))
    print("口径：不挂断 / 不烧核身·含糊·追问次数 / 不误回写 / 噪声后可恢复")
    for r in results:
        if not r[2]:
            print("  FAIL %s %s：%s" % (r[0], r[1], r[3]))
    return 0 if npass == len(results) and s_ok else 1


if __name__ == "__main__":
    sys.exit(main())
