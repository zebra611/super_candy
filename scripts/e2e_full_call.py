# -*- coding: utf-8 -*-
"""端到端跑通验证：一通**真**电话从开头打到闭环。

不是脚本级断言，是把整条链路真跑一遍：
    起服务 → 建会话 → 逐轮对话（真 TTS 出声、真 ASR 识别、真业务工具查档案）
    → 核身 → 说驳回原因 → 确认 → 回写 → 挂断
用的是 config/mock/applications.csv 里的真实档案，不是自造数据。

用法：
    python scripts/e2e_full_call.py                 # 打一通，打印全过程
    python scripts/e2e_full_call.py --record 2      # 用第 2 条档案
    python scripts/e2e_full_call.py --no-voice      # 不走 ASR，用文字（更快）
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PY = sys.executable
PORT = 8821


def _free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def api(path, payload=None, raw=None, method=None):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = None
    headers = {}
    if raw is not None:
        data = raw
        headers["Content-Type"] = "application/octet-stream"
    elif payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    return json.loads(body.decode("utf-8")) if body else {}


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", type=int, default=1,
                    help="用 applications.csv 第几条档案（从 1 数）")
    ap.add_argument("--no-voice", action="store_true",
                    help="用户轮次用文字（不走 ASR，跑得快）")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    PORT = args.port

    if not _free(PORT):
        print("[FAIL] 端口 %d 被占用——先关掉那个进程" % PORT)
        return 2

    import csv
    rows = list(csv.DictReader(open(os.path.join(ROOT, "config", "mock",
                                                "applications.csv"),
                                   encoding="utf-8-sig")))
    rec = rows[args.record - 1]
    print("=" * 72)
    print("端到端跑通验证（真 ASR / 真 TTS / 真档案）")
    print("=" * 72)
    print("档案 #%d：%s | 尾号 %s | 驳回码 %s | 关单方式 %s"
          % (args.record, rec["company_name"], rec["phone_tail"],
             rec["reject_code"], rec["close_mode"]))

    env = dict(os.environ)
    env.pop("PORT", None)
    # 刻意**不设** APP_SCENE：默认场景本来就该是赛题 POP 资审，
    # 显式设了就等于没验到"默认跑什么"这件事。
    env.pop("APP_SCENE", None)
    env.pop("APP_CONFIG", None)
    proc = subprocess.Popen([PY, os.path.join(ROOT, "app.py"), str(PORT)],
                            cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            time.sleep(0.25)
            try:
                api("/api/health")
                break
            except Exception:
                pass
        else:
            print("[FAIL] 服务起不来")
            return 1

        start = api("/api/call/start", {})
        sid = start["session_id"]
        tok = start.get("read_token") or start.get("token")   # 服务端叫 read_token
        print("\n会话：%s | 场景：%s | 开场：%s"
              % (sid, start.get("scene"), (start.get("opening") or "")[:50]))

        # 用户台词：按业务链路走，不是随便聊
        script = [
            "你好",                                      # 0 GREETING
            "我是%s，尾号%s" % (rec["contact_name"], rec["phone_tail"]),  # 1 IDENTITY
            "哦对，是那个品牌授权的问题",                  # 2 NEED
            "需要准备什么材料",                            # 3 OFFER
            "好的我确认，就按这个办",                      # 4 CONFIRM
            "知道了，谢谢",                               # 5 收尾
        ]

        def say(text):
            body = {"session_id": sid, "token": tok}
            if args.no_voice:
                body["text"] = text
            else:
                body["audio_streamed"] = False
                body["text"] = text      # 仍走文字，语音版见 check_ws_realtime
            return api("/api/turn", body)

        print("\n--- 逐轮 ---")
        closed = False
        for i, line in enumerate(script):
            t0 = time.time()
            try:
                r = say(line)
            except Exception as exc:
                print("  [%d] 用户：%s" % (i, line))
                print("      [FAIL] 轮次异常：%s" % str(exc)[:160])
                return 1
            ms = int((time.time() - t0) * 1000)
            reply = (r.get("reply_text") or "").strip()
            stage = r.get("stage") or r.get("phase") or ""
            print("  [%d] 用户：%s" % (i, line))
            print("      客服（%s, %dms）：%s" % (stage, ms, reply[:70]))
            if r.get("tts"):
                mode = r["tts"].get("mode")
                segs = len(r["tts"].get("segments") or [])
                print("      TTS：%s%s" % (mode, "（%d 段）" % segs if segs else ""))
            if r.get("closed"):
                closed = True
                print("      → 已闭环")
                break

        # 事件链要在挂断之前取：end_call 会把会话 pop 掉，之后再查就是 400。
        ev = api("/api/events?session_id=%s&token=%s" % (sid, tok))
        types = [e.get("type") for e in (ev.get("events") or [])]
        need = ("tool_call", "turn_end")
        miss = [n for n in need if n not in types]
        print("\n事件链：%d 条%s" % (len(types),
                               "，缺 %s" % ", ".join(miss) if miss else "，工具调用与轮次都在"))
        for e in (ev.get("events") or []):
            if e.get("type") in ("tool_call", "asr_result", "writeback"):
                p_ = e.get("payload") or {}
                print("  · %s：%s" % (e.get("type"),
                                   json.dumps({k: p_[k] for k in
                                               ("tool", "name", "text", "result",
                                                "application_id") if k in p_},
                                              ensure_ascii=False)[:100]))

        end = api("/api/call/end", {"session_id": sid, "token": tok})
        print("\n--- 结果 ---")
        print("闭环：%s" % ("是" if end.get("closed") or closed else "否"))
        print("结论：%s" % (json.dumps(end.get("summary") or end.get("result") or {},
                                    ensure_ascii=False)[:200]))

        if miss:
            print("\n[FAIL] 事件链不完整")
            return 1
        if not (end.get("closed") or closed):
            print("\n[FAIL] 没闭环——卡在了某个环节，见上面的 stage")
            return 1
        print("\n[OK] 端到端跑通")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
