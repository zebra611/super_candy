#!/usr/bin/env python3
"""一键自进化：把「体检 → 读基线 → 提案 → 闸门 → 多轮反馈闭环」固定成一条命令。

用法（项目根目录）：
    python scripts/auto_evolve.py             # 一轮：三条链 + 3 轮反馈闭环 + 闸门 → 上架提示
    python scripts/auto_evolve.py --llm       # 改由大模型起草，坏文案仍由本地模板兜底
    python scripts/auto_evolve.py --no-gate   # 只到提案，不跑闸门
    python scripts/auto_evolve.py --no-selfevolve        # 跳过多轮反馈闭环
    python scripts/auto_evolve.py --selfevolve-rounds 5  # 反馈闭环跑几轮（默认 3）

四条链：L2 话术链、性格链、自适应对抗链、逐轮反馈闭环（self_evolve）。
第 5 步的反馈闭环与前三链分开记账：它只改 `evolution_policy` 里的分支开关，
每轮改一处、复跑同一批客户，收益过不了四道验收就退回。上架（promote）
永远不自动做，必须单独一条命令。
空草稿或被静态拒绝的草稿会就地重做，不累积垃圾候选。
对抗链会先复算基线分数：代码或模拟器改过却没重定基线时直接中止，
不许拿旧基线把别处的收益算到话术候选头上。
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
_ADV_BASE = os.path.join(_ROOT, "eval", "reports", "adversarial_baseline.json")


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


#: 对抗候选的确定性兜底文案：与 evolve.validate_candidate 的静态规则一致，
#: 不联网、不花钱、不依赖密钥，也不会写出原句没有的花括号变量。
_ADV_DRAFTS = {
    "stages.IDENTITY.reply_ask_tail":
        "您刚报的联系人姓名我核对一下；如果方便，再说一下预留手机号的后四位，四个数字就行。",
    "stages.IDENTITY.reply_noise":
        "不好意思，我没听明白您的意思。您是问要补什么材料，还是要核对申请单？我慢慢说。",
    "stages.GREETING.reply":
        "您好，我是京东商家资质审核专员，请问您是{company_name}的{contact_role}吗？"
        "我这边核对一下申请单，刚才报过的信息不会重复问。",
}


def _adv_draft_fallback(cand, base_cfg):
    """把对抗候选填成可送闸门的形状；返回替换条数。"""
    n = 0
    for p in cand.get("patches") or []:
        val, old = str(p.get("value") or ""), str(p.get("old") or "")
        extra = set(re.findall(r"\{[^}]+\}", val)) - set(re.findall(r"\{[^}]+\}", old))
        bad = (not val.strip() or bool(extra)
               or bool(_evo.validate_candidate({"patches": [p]}, base_cfg)))
        if bad:
            p["value"] = _ADV_DRAFTS.get(p.get("path"), old)
            p["rationale"] = "本地模板草案（确定性兜底）：保留核对关键词，不新增变量"
            n += 1
    return n


def _adv_next_path():
    n = 1
    while True:
        p = os.path.join(_L2_DIR, "adv_auto_%03d.json" % n)
        if not os.path.exists(p):
            return p
        n += 1


def _adv_pending():
    """最近一份还没进过对抗闸门的草稿。"""
    for p in reversed(sorted(glob.glob(os.path.join(_L2_DIR, "adv_auto_*.json")))):
        try:
            c = _read(p)
        except Exception:
            continue
        if "adversarial_gate" not in c:
            return p, c
    return None, None


def _adv_ready(cand):
    patches = cand.get("patches") or []
    return bool(patches) and all(str(p.get("value") or "").strip() for p in patches)


def _levels(d):
    return {k: (v["ok"], v["total"]) for k, v in (d.get("by_level") or {}).items()}


def _adv_doctor(base):
    """重跑同一批对抗客户，确认登记的基线分数没漂；返回问题列表。"""
    tmp = os.path.join(_ROOT, "eval", "reports", "_adv_doctor.json")
    rc = _run("对抗基线复算", [_PY, "scripts/adversarial_evolve.py", "run",
                              "--json", _rel(tmp), "--quiet"], allow_fail=True)
    if rc != 0 or not os.path.exists(tmp):
        return ["对抗跑批没跑起来"]
    got = _read(tmp)
    os.remove(tmp)
    problems = []
    for k in ("all", "train", "holdout"):
        a, b = base["summary"][k]["ok"], got["summary"][k]["ok"]
        if a != b:
            problems.append("%s %s→%s" % (k, a, b))
    if _levels(base) != _levels(got):
        problems.append("按难度分组不一致")
    return problems


def main():
    argv = sys.argv[1:]
    use_llm = "--llm" in argv
    no_gate = "--no-gate" in argv
    no_selfev = "--no-selfevolve" in argv
    se_rounds = 3
    for i, a in enumerate(argv):
        if a == "--selfevolve-rounds" and i + 1 < len(argv):
            try:
                se_rounds = max(1, int(argv[i + 1]))
            except ValueError:
                se_rounds = 3
    for d in (_L2_DIR, _P_DIR):
        os.makedirs(d, exist_ok=True)

    print("一键自进化｜四条链（L2 话术 / 性格 / 自适应对抗 / 逐轮反馈）：")
    print("  1 体检 → 2 读基线 → 3 提案 → 4 闸门 → 5 多轮反馈闭环 → 6 上架")

    print("\n第 0 步｜真实通话体检（有真实数据才跑：先看用户实际踩到什么问题）")
    # 只做展示，不产出候选：真实通话给的是「改什么」的优先级，能不能上仍由闸门说了算。
    # allow_fail：没有真实通话库（新克隆、纯离线环境）不应该挡住整个闭环。
    _rc = _run("真实通话体检", [_PY, "scripts/mine_calls.py", "mine", "--verify",
                               "--json", "eval/reports/auto/real_calls.json"],
               allow_fail=True)
    if _rc != 0:
        print("  [跳过] 没有可用的真实通话数据，不影响后面的模拟链")

    print("\n第 1 步｜地基体检（三条链都要过，不过就不往下跑）")
    _run("L2 链", [_PY, "scripts/evolve.py", "doctor", "--deep"])
    _run("性格链", [_PY, "scripts/evolve_persona.py", "doctor", "--deep"])
    _run("对抗链（10 项反向自测）", [_PY, "scripts/adversarial_evolve.py", "check"])
    if not os.path.exists(_ADV_BASE):
        print("  [中止] 找不到对抗基线报告：%s" % _rel(_ADV_BASE))
        print("  修法：python scripts/adversarial_evolve.py run")
        sys.exit(1)
    adv_base = _read(_ADV_BASE)
    drift = _adv_doctor(adv_base)
    if drift:
        print("  [中止] 对抗基线已失真：%s" % "、".join(drift))
        print("  修法：python scripts/adversarial_evolve.py run 重定基线，再跑本命令")
        sys.exit(1)
    print("  [完成] 三处地基对齐，后面的分数才算数")

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
    _as = adv_base.get("summary") or {}
    print("  对抗：通过 %s/%s（train %s/%s、holdout %s/%s），系统缺陷会话 %s"
          % (_as["all"]["ok"], _as["all"]["total"],
             _as["train"]["ok"], _as["train"]["total"],
             _as["holdout"]["ok"], _as["holdout"]["total"],
             _as["agent_defect_sessions"]))
    print("        按难度 %s"
          % "  ".join("%s %s/%s" % (k, v["ok"], v["total"])
                     for k, v in sorted((adv_base.get("by_level") or {}).items())))
    print("  [完成] 第 1 步已重跑复算过这三份基线；不再另外采样，避免分母不一致")

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

    adv_path, adv_cand = _adv_pending()
    if adv_cand is not None and _adv_ready(adv_cand):
        print("  [沿用] 上一轮对抗草稿已填好：%s" % _rel(adv_path))
    else:
        if adv_cand is None:
            adv_path = _adv_next_path()
        _run("对抗提案", [_PY, "scripts/adversarial_evolve.py", "propose",
                         "--out", _rel(adv_path)], allow_fail=True)
        adv_cand = _read(adv_path) if os.path.exists(adv_path) else {}
        if not (adv_cand.get("patches") or []):
            print("  [完成] 对抗集已跑满，本轮没有可优化簇（正常，不是报错）")
            if adv_cand.get("code_only") or adv_cand.get("unexplained"):
                print("         仍有 %d 个只能改代码的簇、%d 通待归因失败，"
                      "这些不进优化池"
                      % (len(adv_cand.get("code_only") or []),
                         len(adv_cand.get("unexplained") or [])))
        else:
            base_cfg = _read(os.path.join(_ROOT, "config", "scenes", "pop_reject.json"))
            n = _adv_draft_fallback(adv_cand, base_cfg)
            if n:
                _write(adv_path, adv_cand)
                print("  [完成] %d 句改写用本地模板填好（不调模型也能进闸门）" % n)

    print("\n第 4 步｜三链闸门（同一批电话只换配置；不过就只留档）")
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
        if adv_cand and _adv_ready(adv_cand):
            rc = _run("对抗闸门", [_PY, "scripts/adversarial_evolve.py", "gate",
                                  _rel(adv_path)], allow_fail=True)
            after = _read(adv_path) if os.path.exists(adv_path) else {}
            g = (after.get("adversarial_gate") or {}).get("result")
            if g == "passed":
                print("  [结果] 五道判据全过，可人工评估上架（脚本不会自动上线）")
            elif g:
                print("  [结果] 未通过，候选只留档，线上话术未动")
            else:
                print("  [结果] 候选不合规，静态拒绝；没有生成版本")
        else:
            print("  [完成] 对抗集没有可改话术的失败簇，本轮无需闸门")

    print("\n第 5 步｜多轮反馈闭环（每轮只改一处策略开关，复跑同一批客户验证）")
    if no_selfev:
        print("  [跳过] 本轮用了 --no-selfevolve")
    else:
        rc = _run("反馈闭环（%d 轮）" % se_rounds,
                  [_PY, "scripts/self_evolve.py", "run", "--rounds", str(se_rounds)],
                  allow_fail=True)
        if rc != 0:
            print("  [跳过] 反馈闭环没跑完，不影响前面三条链的结论；原因见上面的输出")

    print("\n第 6 步｜上架（永远单独一条命令，脚本不会自己上线）")
    v = _newest_gated(_L2_VERSIONS)
    pv = _newest_gated(_P_VERSIONS)
    if v:
        print("  L2 待上架：python scripts/evolve.py promote %s" % v)
    if pv:
        print("  性格待上架：python scripts/evolve_persona.py promote %s" % pv)
    if not v and not pv:
        print("  没有待上架版本：闸门没过的候选只留档，线上话术一个字都没动")
    print("  对抗链：闸门通过后需人工评估，不提供自动上架命令")
    print("  反馈闭环：最新一次运行可用 python scripts/self_evolve.py report 查看；")
    print("            学到的策略要上线：python scripts/self_evolve.py promote <运行编号> --yes")

    print("\n本轮结论：三处地基健康；没有新失败样本时，闸门会如实拒绝无收益的改动。")
    print("反馈闭环的结论看它自己的日志：每一轮的正负反馈条数、改了哪一处、")
    print("复跑后降了多少，都写在 training_log.md 与 feedback.jsonl 里。")
    print("想让它继续进化：先看第 0 步里『仍会复现』的真实缺陷（那是用户真踩到的），")
    print("再往 eval/personas_adversarial.json 加更难的画像（独立挑战集，")
    print("不动 10 通与 12 画像的分母），或修掉只改代码那几簇里的缺陷；")
    print("代码或画像改完先重定基线，再双击同一个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
