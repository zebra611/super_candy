#!/usr/bin/env python3
"""训练回放接口的自测：结构、隔离、异常恢复。不启服务，直接调函数。

跑法：python scripts/test_train_api.py
"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import events  # noqa: E402

# 用一个临时库当「生产库」，训练前后对比会话数：训练必须一条都不写进来。
_tmp = tempfile.mkdtemp(prefix="sc_train_test_")
events.DATA_DIR = _tmp
events.DB_PATH = os.path.join(_tmp, "prod.db")
events.init_db()

import app as A  # noqa: E402
import config_loader  # noqa: E402
from scripts import adversarial_evolve as adv  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok), detail))


def main():
    personas = adv.load_personas()
    # 不变量是「API 清单与画像文件一致」，不是某个固定条数——
    # 挑战集会加人（11-08 加到 16），硬编码条数只会变成假红。
    _file_n = len((json.load(open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "eval", "personas_adversarial.json"), encoding="utf-8"))).get("personas") or [])
    check("画像清单与画像文件一致且都有 label",
          len(personas) == _file_n and all(p.get("label") for p in personas),
          "API %d 条 / 文件 %d 条" % (len(personas), _file_n))

    p = next(x for x in personas if x["id"] == "A03")
    before_n = len(events.recent_sessions(limit=200))
    before_db = events.DB_PATH
    before_nlg = config_loader.nlg_config
    before_tts = config_loader.tts_config

    out = A._train_run(p)
    keys = ("turns", "closed", "end_by", "level_peak", "weakness",
            "ok", "verdict", "company", "expect")
    check("返回结构完整", all(k in out for k in keys),
          "缺 " + "、".join(k for k in keys if k not in out))
    check("有逐轮轨迹", len(out["turns"]) >= 3, "%d 轮" % len(out["turns"]))
    check("每轮都带弱点字段",
          all("found" in t["user"] for t in out["turns"]))
    check("判定与期望一致",
          out["ok"] == (bool(out["expect"].get("closed")) == bool(out["closed"])),
          "期望闭环=%r 实际=%r" % (out["expect"].get("closed"), out["closed"]))
    check("公司档案随结果回传",
          bool((out.get("company") or {}).get("company_name")))

    after_n = len(events.recent_sessions(limit=200))
    check("训练不写生产库",
          after_n == before_n and events.DB_PATH == before_db,
          "会话 %d→%d" % (before_n, after_n))
    check("训练后 NLG / TTS 配置未漂移",
          config_loader.nlg_config is before_nlg
          and config_loader.tts_config is before_tts)

    # 异常路径：跑批抛异常，事件库路径也必须还原，否则后续真实通话会写进临时目录。
    orig = adv.run_one

    def _boom(*a, **k):
        raise RuntimeError("probe")

    adv.run_one = _boom
    raised = False
    try:
        A._train_run(p)
    except RuntimeError:
        raised = True
    finally:
        adv.run_one = orig
    check("跑批抛异常也会恢复事件库路径",
          raised and events.DB_PATH == before_db,
          "raised=%r db=%s" % (raised, events.DB_PATH))

    # 恢复后能正常写：往恢复后的库写一条事件，再读回来
    events.write_event("train-test-probe", "call_started", {"probe": True})
    check("恢复后事件库仍可写",
          bool(events.list_events("train-test-probe")))

    passed = sum(1 for _, ok, _ in _checks if ok)
    for name, ok, detail in _checks:
        print("  [%s] %s%s" % ("通过" if ok else "失败", name,
                               "｜" + detail if detail else ""))
    print("结果：%d/%d 通过" % (passed, len(_checks)))
    return 0 if passed == len(_checks) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
