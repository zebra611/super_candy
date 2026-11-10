#!/usr/bin/env python3
"""PR #43 守卫：HTTP（纯文字）收线判据与服务端一致、定时器可取消。

为什么单列一个脚本而不是塞进 verify.py：#43 基于较早的 main，两边同时改
verify.py 会在合并时撞车；这个文件是新增的，合并不需要解冲突。

跑法：python scripts/test_hangup_http.py
"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

HTML = os.path.join(ROOT, "web", "index.html")
APP = os.path.join(ROOT, "app.py")


def _block(html, marker, size=1600):
    tail = html.split(marker, 1)[-1] if marker in html else ""
    return tail[:size]


def static_results(html, app):
    """返回 {断言名: 是否通过}；纯字符串判据，不跑浏览器。"""
    start = _block(html, "async function startCall")
    end = _block(html, "async function endCall")
    timer = _block(html, "S.hangupTimer = setTimeout", 260)
    return {
        "前端收线判据同时看 hangup 与 end_reason":
            "if (d.hangup || d.end_reason)" in html,
        "S 里声明并初始化 hangupTimer":
            "hangupTimer: null" in html,
        "startCall 开新通话前清掉上一通的定时器":
            "clearTimeout(S.hangupTimer)" in start,
        "endCall 一律清掉定时器（含服务端 silent 收线）":
            "clearTimeout(S.hangupTimer)" in end,
        "定时器触发后清空自己的引用":
            "S.hangupTimer = null;" in timer,
        "app.py 透出 hangup":
            '"hangup": bool(agent_out.get("hangup"))' in app,
        "app.py 透出 end_reason":
            '"end_reason": agent_out.get("end_reason", "")' in app,
    }


def runtime_results():
    """真跑三类收尾出口：判据是事件/返回字段，不是回复文本。"""
    tmp = tempfile.mkdtemp(prefix="sc_hangup_")
    import events
    events.DATA_DIR = tmp
    events.DB_PATH = os.path.join(tmp, "p.db")
    events.init_db()
    from app import start_call, handle_turn, end_call
    try:
        from ws_channel import should_auto_hangup
    except ImportError:
        # #43 分支还没合入加固后的 ws_channel，用等价判据兜底
        should_auto_hangup = lambda out: bool(
            out.get("hangup") or out.get("end_reason"))

    out = {}
    cases = {
        "hangup": ["不说了，我忙着呢"],
        "not_me": ["打错了，没这个人"],
        "done": ["不说了，我忙着呢", "再见"],
    }
    for label, turns in cases.items():
        sid = start_call()["session_id"]
        last = None
        for text in turns:
            last = handle_turn(sid, user_text=text)
        out[label] = (last.get("end_reason"),
                      bool(should_auto_hangup(last)))
        end_call(sid)
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def main():
    html = open(HTML, encoding="utf-8").read()
    app = open(APP, encoding="utf-8").read()
    checks = []

    for name, ok in sorted(static_results(html, app).items()):
        checks.append((name, ok, ""))

    # 反向自测：三条最容易被改回去的地方，改坏一条就必须有一条判据报红。
    bad_judge = html.replace("if (d.hangup || d.end_reason)", "if (d.hangup)", 1)
    bad_start = html.replace(
        "async function startCall(opts) {\n  opts = opts || {};\n"
        "  clearTimeout(S.hangupTimer);\n  S.hangupTimer = null;",
        "async function startCall(opts) {\n  opts = opts || {};", 1)
    bad_end = html.replace(
        "async function endCall(opts) {\n  clearTimeout(S.hangupTimer);\n"
        "  S.hangupTimer = null;\n  if (!S.sessionId) return;",
        "async function endCall(opts) {\n  if (!S.sessionId) return;", 1)
    for label, mutated in (("判据改回只判 hangup", bad_judge),
                           ("startCall 不清定时器", bad_start),
                           ("endCall 不清定时器", bad_end)):
        ok = mutated != html and not all(static_results(mutated, app).values())
        checks.append(("反向：%s 必须报红" % label, ok, ""))

    rt = runtime_results()
    checks.append(("挂断出口：有 end_reason 且判据成立",
                   bool(rt["hangup"][0]) and rt["hangup"][1],
                   "end_reason=%r" % rt["hangup"][0]))
    checks.append(("打错出口：有 end_reason 且判据成立",
                   bool(rt["not_me"][0]) and rt["not_me"][1],
                   "end_reason=%r" % rt["not_me"][0]))
    checks.append(("终态出口：判据成立", rt["done"][1],
                   "end_reason=%r" % rt["done"][0]))

    for name, ok, detail in checks:
        print("  [%s] %s%s" % ("通过" if ok else "失败", name,
                               "｜" + detail if detail else ""))
    passed = sum(1 for _, ok, _ in checks if ok)
    print("结果：%d/%d 通过" % (passed, len(checks)))
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
