"""自检脚本 —— 用来验证"代码实际做了什么"，不是"文档说做了什么"。

为什么要这个东西：
    过去出现过这样的问题——文档写"已实现"，代码里却是死代码或硬编码。
    口头汇报和文档都不可靠，唯一可靠的是**实际跑一遍，看原始输出**。

怎么用：
    python scripts/verify.py

它会做两件事：
    1. 打印当前提交与改动文件（来自 git，不是我自己写的说明）；
    2. 逐条跑断言，并把**原始事件链**打出来，让你自己判断对错。

设计原则：
    - 只用标准库，不依赖服务是否启动；
    - 用临时数据库，不污染 data/events.db；
    - 每条都打印实际数据，不只打印"PASS"——结论可以被伪造，原始输出不能。
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 先把事件库指向临时目录，避免把自检数据写进正式库
import events  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="sc_verify_")
events.DATA_DIR = _tmp
events.DB_PATH = os.path.join(_tmp, "verify.db")
events.init_db()

from app import end_call, handle_turn, start_call  # noqa: E402
import contracts  # noqa: E402

results = []


def check(name, ok, detail):
    """记一条断言。detail 必须是可以被人自己看懂的证据。"""
    results.append((name, ok))
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    for line in detail:
        print("        " + line)


def event_types(session_id):
    return [e["type"] for e in events.list_events(session_id)]


def payload_of(session_id, etype):
    for e in events.list_events(session_id):
        if e["type"] == etype:
            return e["payload"]
    return None


def git(*args):
    """调用 git 拿第三方记录。没装 git 就返回空，不影响断言执行。"""
    try:
        out = subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip()
    except Exception:
        return ""


def main():
    print("=" * 72)
    print("证据 1：git 记录（这部分不是我自己写的）")
    print("=" * 72)
    head = git("log", "-1", "--format=%h %ad %s", "--date=short")
    print("  当前提交: %s" % (head or "（取不到，可能没装 git）"))
    changed = git("show", "--stat", "--oneline", "HEAD")
    if changed:
        print("  最近一次提交改了这些文件:")
        for line in changed.splitlines()[1:]:
            print("    " + line.strip())
    print("  另可在 GitHub 网页上直接看 diff，那边的历史我改不了。")

    print()
    print("=" * 72)
    print("证据 2：契约里到底有没有 no_answer")
    print("=" * 72)
    check("EVENT_TYPES 包含 no_answer",
          "no_answer" in contracts.EVENT_TYPES,
          ["实际事件类型共 %d 个：%s" % (len(contracts.EVENT_TYPES),
                                    ", ".join(sorted(contracts.EVENT_TYPES)))])

    print()
    print("=" * 72)
    print("证据 3：文字输入这一轮，到底写了哪些事件")
    print("=" * 72)
    sid = start_call()["session_id"]
    out = handle_turn(sid, user_text="是我，尾号8866")
    types = event_types(sid)
    asr = payload_of(sid, "asr_result")
    check("文字输入不写 vad_result",
          "vad_result" not in types,
          ["实际事件链：%s" % " → ".join(types)])
    check("文字输入标 source=text 且 fake=false",
          bool(asr) and asr.get("source") == "text" and asr.get("fake") is False,
          ["asr_result.payload = %s" % json.dumps(asr, ensure_ascii=False)])
    end_call(sid)

    print()
    print("=" * 72)
    print("证据 4：用户不说话时，系统会不会假装他说了话")
    print("=" * 72)
    sid2 = start_call()["session_id"]
    out1 = handle_turn(sid2, user_text="")
    types2 = event_types(sid2)
    na = payload_of(sid2, "no_answer")
    check("沉默写 no_answer 事件",
          "no_answer" in types2,
          ["实际事件链：%s" % " → ".join(types2)])
    check("沉默不写 asr_result（不伪造『（空输入）』）",
          "asr_result" not in types2,
          ["no_answer.payload = %s" % json.dumps(na, ensure_ascii=False)])
    check("沉默不推进状态机",
          out1["next_stage"] == "GREETING",
          ["stage=%s → next_stage=%s（GREETING 说明没往前走）" % (out1["stage"], out1["next_stage"])])
    check("沉默时说的是追问，不是挂断",
          "还在吗" in out1["reply_text"] or "听不到" in out1["reply_text"],
          ["实际回复：%s" % out1["reply_text"]])

    print()
    print("=" * 72)
    print("证据 5：连续两次不说话，会不会挂断")
    print("=" * 72)
    out2 = handle_turn(sid2, user_text="")
    check("第二次沉默进入 DONE",
          out2["next_stage"] == "DONE",
          ["实际回复：%s" % out2["reply_text"],
           "next_stage = %s" % out2["next_stage"]])
    end_call(sid2)

    print()
    print("=" * 72)
    print("证据 6：语音输入这条路还通不通（占位 ASR 应标 fake=true）")
    print("=" * 72)
    sid3 = start_call()["session_id"]
    handle_turn(sid3, audio_bytes=b"\x00" * 3200)
    types3 = event_types(sid3)
    asr3 = payload_of(sid3, "asr_result")
    check("语音输入有 vad_result",
          "vad_result" in types3,
          ["实际事件链：%s" % " → ".join(types3)])
    check("占位识别标 fake=true（接真 ASR 后应变 false）",
          bool(asr3) and asr3.get("fake") is True,
          ["asr_result.payload = %s" % json.dumps(asr3, ensure_ascii=False)])
    end_call(sid3)

    print()
    print("=" * 72)
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("结果：%d/%d 通过" % (passed, total))
    if passed != total:
        print("有断言失败，说明代码与预期不符 —— 以这里为准，不要信文档。")
        return 1
    print("全部通过。这只证明上面这几条行为是真的，不代表整个系统都完成了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
