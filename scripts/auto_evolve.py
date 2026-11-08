#!/usr/bin/env python3
"""一键自进化：把「体检 → 读基线 → 提案 → 闸门」固定成一条命令。

用法（项目根目录）：
    python scripts/auto_evolve.py             # 一轮：本地模板草案 → 闸门 → 上架提示
    python scripts/auto_evolve.py --llm       # 改由大模型起草，坏文案仍由本地模板兜底
    python scripts/auto_evolve.py --no-gate   # 只到提案，不跑闸门

顺序写在输出的第 1～5 步里，对外只暴露这一行命令。
上架（promote）永远不自动做，必须单独一条命令。
空草稿或被静态拒绝的草稿会就地重做，不累积垃圾候选。
"""

import glob
import json
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from scripts import evolve as _evo  # noqa: E402  复用同一套候选静态校验

_PY = sys.executable
_L2_DIR = os.path.join(_ROOT, "config", "evolution", "candidates")
_P_DIR = os.path.join(_ROOT, "config", "evolution", "persona", "candidates")
_L2_VERSIONS = os.path.join(_ROOT, "config", "evolution", "versions.json")
_P_VERSIONS = os.path.join(_ROOT, "config", "evolution", "persona", "versions.json")


def _rel(path):
    return os.path.relpath(path, _ROOT).replace("\\", "/")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


#: 确定性兜底文案：不调模型也能把草稿填成「可以送闸门」的形状。
#: 硬约束与静态校验一致——不新增花括号变量、保留该阶段的「核对」关键词、不承诺结果。
_LOCAL_DRAFTS = {
    "stages.IDENTITY.reply_offtopic":
        "没关系，号码记不住也常见。您报一下联系人姓名，或者申请单编号后六位，"
        "我都能核对上；不方便的话，我让专员用官方号码再联系您。",
    "stages.IDENTITY.reply_ask_tail":
        "核对申请单需要预留手机号的后四位，说四个数字就行；"
        "不方便的话报联系人姓名，我也能核对上。",
    "stages.IDENTITY.reply_ask_identity":
        "您的号码是商家入驻时预留的联系方式，官方审核只用来核对这笔资质申请。"
        "您报手机号后四位，或者说联系人姓名，都可以。",
}


def _draft_fallback(cand, base_cfg):
    """空草稿 / 模型起草出坏文案时，用本地模板补齐；返回替换条数。

    11-08 实跑：双击入口沿用了上一轮的空草稿，跳过提案，于是既不调模型、
    也永远进不了闸门，屏幕上只留一句「待填」。这里保证第 3 步结束后
    草稿一定填好且过静态校验，第 4 步一定能真的跑起来。
    """
    n = 0
    for p in cand.get("patches") or []:
        val, old = str(p.get("value") or ""), str(p.get("old") or "")
        extra = set(re.findall(r"\{[^}]+\}", val)) - set(re.findall(r"\{[^}]+\}", old))
        bad = (not val.strip() or bool(extra)
               or bool(_evo.validate_candidate({"patches": [p]}, base_cfg)))
        if bad:
            p["value"] = _LOCAL_DRAFTS.get(p.get("path"), old)
            p["rationale"] = "本地模板草案（确定性兜底）：保留核对关键词，不新增变量"
            n += 1
    return n


def _run(label, cmd, allow_fail=False):
    print("  %s" % label)
    r = subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True)
    lines = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    for line in lines[-8:]:
        print("    " + line)
    if r.returncode != 0 and not allow_fail:
        print("\n  [中止] 这一步没过，后面不跑：%s" % label)
        print("  修法：python scripts/evolve.py rebase（或 evolve_persona.py rebase）重定基线")
        sys.exit(1)
    return r.returncode


def _next_path(directory):
    n = 1
    while True:
        p = os.path.join(directory, "auto_%03d.json" % n)
        if not os.path.exists(p):
            return p
        n += 1


def _pending(directory):
    """最近一份可继续处理的草稿：没进过闸门，或只被静态拒绝过。"""
    for p in reversed(sorted(glob.glob(os.path.join(directory, "auto_*.json")))):
        try:
            c = _read(p)
        except Exception:
            continue
        g = c.get("gate") or {}
        if "gate" not in c or g.get("result") == "rejected_static":
            return p, c
    return None, None


def _l2_ready(cand):
    patches = cand.get("patches") or []
    return bool(patches) and all(str(p.get("value") or "").strip() for p in patches)


def _p_ready(cand):
    return bool(cand.get("rules"))


def _newest_gated(path):
    try:
        vd = _read(path)
    except Exception:
        return ""
    for h in reversed(vd.get("history") or []):
        if h.get("status") == "gated":
            return h.get("version", "")
    return ""


def main():
    argv = sys.argv[1:]
    use_llm = "--llm" in argv
    no_gate = "--no-gate" in argv
    for d in (_L2_DIR, _P_DIR):
        os.makedirs(d, exist_ok=True)

    print("一键自进化｜1 体检 → 2 读基线 → 3 提案 → 4 闸门 → 5 上架")

    print("\n第 1 步｜地基体检（两条链都要过，不过就不往下跑）")
    _run("L2 链", [_PY, "scripts/evolve.py", "doctor", "--deep"])
    _run("性格链", [_PY, "scripts/evolve_persona.py", "doctor", "--deep"])
    print("  [完成] 两处地基对齐，后面的分数才算数")

    print("\n第 2 步｜读当前基线（提案和闸门必须共用同一份报告）")
    l2_report = os.path.join(_ROOT, _read(_L2_VERSIONS).get("baseline_json") or "")
    p_report = os.path.join(_ROOT, _read(_P_VERSIONS).get("baseline_json") or "")
    for label, path in (("L2", l2_report), ("性格", p_report)):
        if not os.path.exists(path):
            print("  [中止] 找不到 %s 基线报告：%s" % (label, path))
            print("  评测报告按设计不进仓库；新环境先跑 rebase 重定基线：")
            print("    python scripts/evolve.py rebase")
            print("    python scripts/evolve_persona.py rebase")
            sys.exit(1)
        s = _read(path).get("summary") or {}
        print("  %s：通过 %s/%s，系统缺陷会话 %s，缺陷构成 %s"
              % (label, (s.get("all") or {}).get("ok"), (s.get("all") or {}).get("total"),
                 s.get("agent_defect_sessions"), s.get("codes")))
    print("  [完成] 第 1 步的深度体检已重跑复算过这份基线；不再另外采样，避免分母不一致")

    print("\n第 3 步｜按失败聚类出候选")
    l2_path, l2_cand = _pending(_L2_DIR)
    p_path, p_cand = _pending(_P_DIR)
    stale = l2_cand is not None and (
        not _l2_ready(l2_cand)
        or (l2_cand.get("gate") or {}).get("result") == "rejected_static")
    if l2_cand is not None and not stale:
        print("  [沿用] 上一轮草稿已填好，直接送闸门：%s" % _rel(l2_path))
    else:
        if l2_cand is None:
            l2_path = _next_path(_L2_DIR)
            print("  [新建] 本轮候选：%s" % _rel(l2_path))
        else:
            print("  [重做] 上一轮草稿是空的或被静态拒绝，覆盖重提：%s" % _rel(l2_path))
        cmd = [_PY, "scripts/evolve.py", "propose", "--from", _rel(l2_report),
               "--out", _rel(l2_path)]
        if use_llm:
            cmd.append("--llm")
        _run("L2 提案", cmd)
        l2_cand = _read(l2_path) if os.path.exists(l2_path) else {}
    # 无论沿用、重做还是模型起草，送闸门前的草稿必须填好且过静态校验。
    if l2_cand and l2_cand.get("patches"):
        base_cfg = _read(os.path.join(_ROOT, "config", "scenes", "pop_reject.json"))
        n = _draft_fallback(l2_cand, base_cfg)
        if n:
            _write(l2_path, l2_cand)
            print("  [完成] %d 句改写用本地模板填好（不调模型也能进闸门）" % n)
    if p_cand is not None:
        print("  [沿用] 上一轮性格草稿：%s" % _rel(p_path))
    else:
        p_path = _next_path(_P_DIR)
        rc = _run("性格提案", [_PY, "scripts/evolve_persona.py", "propose",
                              "--auto", "--out", _rel(p_path)], allow_fail=True)
        p_cand = _read(p_path) if os.path.exists(p_path) else None
        if rc != 0 and p_cand is None:
            print("  [完成] 性格集全部跑满，本轮没有可优化的性格（正常，不是报错）")

    print("\n第 4 步｜双跑闸门（同一批电话只换配置；不过就只留档）")
    if no_gate:
        print("  [跳过] 本轮用了 --no-gate")
    else:
        if l2_cand and _l2_ready(l2_cand):
            rc = _run("L2 闸门", [_PY, "scripts/evolve.py", "gate", _rel(l2_path)],
                      allow_fail=True)
            after = _read(l2_path) if os.path.exists(l2_path) else {}
            if (after.get("gate") or {}).get("result") == "rejected_static":
                print("  [结果] 候选不合规，静态拒绝；没有生成版本，线上话术未动")
            else:
                print("  [结果] %s" % ("通过，进第 5 步" if rc == 0 else "未通过，版本只留档"))
        elif l2_cand:
            print("  [待填] L2 草稿的话术还空着：%s" % _rel(l2_path))
            print("         填 patches[].value 后再跑一次本命令；或加 --llm 自动起草")
        if p_cand and _p_ready(p_cand):
            rc = _run("性格闸门", [_PY, "scripts/evolve_persona.py", "gate", _rel(p_path)],
                      allow_fail=True)
            print("  [结果] %s" % ("通过，进第 5 步" if rc == 0 else "未通过，版本只留档"))

    print("\n第 5 步｜上架（永远单独一条命令，脚本不会自己上线）")
    v = _newest_gated(_L2_VERSIONS)
    pv = _newest_gated(_P_VERSIONS)
    if v:
        print("  L2 待上架：python scripts/evolve.py promote %s" % v)
    if pv:
        print("  性格待上架：python scripts/evolve_persona.py promote %s" % pv)
    if not v and not pv:
        print("  没有待上架版本：闸门没过的候选只留档，线上话术一个字都没动")

    print("\n本轮结论：地基健康；没有新失败样本时，闸门会如实拒绝无收益的改动。")
    print("想让它真的改东西：加更难的画像，或修掉需要改代码的缺陷，再跑一次本命令。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
