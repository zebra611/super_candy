"""L2 自进化闭环：失败聚类 → 候选话术 → 双跑闸门 → 版本 → 回滚。

赛题要的 L2（原文）：
    "失败聚类与归因，自动优化提示词/业务话术/检索知识/工具调用策略，
     借助版本管理、回归验证和安全回滚"。

本脚本只做**话术层**的进化（`stages.*` 里的文案），原因不是偷懒：
    - 事实字段（公司名、申请单号、尾号）由档案与模板提供，改话术不能改事实；
    - 流程逻辑（阶段推进、槽位轮换）改的是代码，必须走代码评审与全量自检，
      不该由优化器在夜里自己改。凡是需要改代码的簇，本脚本**只写待办不生成候选**
      （见 modules/cluster.py 的 `needs_code`）。
    这条边界是刻意的：一个能自动改流程的优化器，迟早会把"没听明白"优化成
    "听到拒绝"——指标好看了，业务结论错了。

三道闸门（任何一道不过就不许上线）：
    1. 不降分   train 与 holdout 的通过数都不得低于基线；
    2. 有收益   总体通过数必须**增加**，或系统缺陷会话数必须**减少**；
    3. 不破合规 候选文案不得引入合规禁语、不得丢占位符、不得超长。

为什么 holdout 必须单独看：
    train 涨、holdout 跌 = 只拟合了调参集。赛题点名"不得对已知测试集过拟合"，
    所以这条不是可选检查，是能不能交的底线。

用法（一条完整闭环）：
    python scripts/evolve.py init --scene config/scenes/pop_reject.json \
        --baseline eval/reports/sim_baseline.json
    python scripts/evolve.py propose --out config/evolution/candidates/c001.json
    python scripts/evolve.py gate config/evolution/candidates/c001.json
    python scripts/evolve.py promote v2
    python scripts/evolve.py status
    python scripts/evolve.py rollback          # 出事就回到上一个版本
"""

import copy
import json
import os
import subprocess
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import config_loader  # noqa: E402

EVO_DIR = os.path.join(_ROOT, "config", "evolution")
VERSIONS_PATH = os.path.join(EVO_DIR, "versions.json")
CAND_DIR = os.path.join(EVO_DIR, "candidates")
SCENE_DIR = os.path.join(EVO_DIR, "versions")

#: 允许自动改的配置前缀。只放开话术，其余一律拒绝——
#: 允许改 `slots` 或 `budget` 等于让优化器改业务流程和成本闸门，那不是话术进化。
ALLOWED_PREFIX = ("stages.",)

#: 单句长度上限。优化器最容易犯的错是"把三句话塞进一句"，
#: 电话里没人听得完；超过就不收，避免上线后语速爆炸。
MAX_CHARS = 120


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _sha(path):
    """场景**内容**哈希（规范化后的 JSON，不是文件字节）。

    为什么不用字节哈希：仓库里同一套场景有两份文件，缩进一个 2 空格一个 1 空格，
    内容完全一样却会算出两个值——那样这道校验只会制造假警报，
    然后被人用 --force 关掉，等于没做。
    """
    import hashlib
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        canon = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def _get_path(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_path(obj, dotted, value):
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        cur = cur[part]
    cur[parts[-1]] = value


def _versions():
    if not os.path.exists(VERSIONS_PATH):
        return {"active": "", "scene_file": "", "history": []}
    return _read_json(VERSIONS_PATH)


def _ver_dir(v):
    return os.path.join(SCENE_DIR, v)


def _snapshot_path(v):
    return os.path.join(_ver_dir(v), "scene.json")


def _sync_apply(vd, paths, src_cfg):
    """把改动过的**键**同步到其它同场景文件，保留各自原有的开关与注释。

    为什么不是整份复制：config/scene.json 的 nlg.provider 是 dashscope（演示要真改写），
    config/scenes/pop_reject.json 是 template（跑批要确定性）。整份覆盖会把这个
    开关一起带过去，等于用"改话术"顺手改了话术引擎。
    """
    for rel in (vd.get("sync_targets") or []):
        path = rel if os.path.isabs(rel) else os.path.join(_ROOT, rel)
        if not os.path.exists(path):
            print("  提示：同步目标不存在，跳过 %s" % rel)
            continue
        cur = _read_json(path)
        changed = 0
        for p in paths:
            val = _get_path(src_cfg, p)
            if val is None:
                continue
            if _get_path(cur, p) is None:
                continue
            _set_path(cur, p, val)
            changed += 1
        _write_json(path, cur)
        print("  已同步 %d 处话术 → %s" % (changed, rel))


# ---------------------------------------------------------------------------
# 跑批
# ---------------------------------------------------------------------------
def run_sim(scene_path, out_json, tag=""):
    """在**子进程**里跑一遍模拟跑批。

    为什么不在本进程直接 import app：app 启动期会缓存场景，
    同一进程连跑两个版本会拿到前一个版本的缓存，出现"候选明明改了、分数没动"
    的假结论。子进程隔离是这里最省事也最可靠的做法。
    """
    cmd = [sys.executable, os.path.join(_ROOT, "scripts", "sim_eval.py"),
           "--scene", scene_path, "--json", out_json, "--tag", tag, "--quiet"]
    r = subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_json):
        raise SystemExit("跑批失败（%s）：\n%s\n%s"
                         % (scene_path, (r.stdout or "")[-1500:], (r.stderr or "")[-1500:]))
    return _read_json(out_json)


def _metrics(payload):
    s = payload.get("summary") or {}
    codes = s.get("codes", {}) or {}
    return {
        "all_ok": s.get("all", {}).get("ok", 0),
        "all_total": s.get("all", {}).get("total", 0),
        "train_ok": s.get("train", {}).get("ok", 0),
        "train_total": s.get("train", {}).get("total", 0),
        "holdout_ok": s.get("holdout", {}).get("ok", 0),
        "holdout_total": s.get("holdout", {}).get("total", 0),
        "agent_defect_sessions": s.get("agent_defect_sessions", 0),
        "case_defect_sessions": s.get("case_defect_sessions", 0),
        "codes": codes,
        # 缺陷**条数**（不是会话数）：一个会话可能踩三个坑，
        # 只看会话数会把"少了一通但每通更糟"判成进步。
        "defect_total": sum(int(v) for v in codes.values()),
    }


def _fmt(m):
    return ("总 %d/%d｜train %d/%d｜holdout %d/%d｜系统缺陷会话 %d"
            % (m["all_ok"], m["all_total"], m["train_ok"], m["train_total"],
               m["holdout_ok"], m["holdout_total"], m["agent_defect_sessions"]))


# ---------------------------------------------------------------------------
# 候选校验
# ---------------------------------------------------------------------------
def validate_candidate(cand, base_cfg):
    """候选必须过这一关才允许进闸门。返回问题列表（空 = 可以跑）。

    查的都是"上线会出事"的项，不查文风：
        路径是否越界、键是否存在、是否动了事实占位符、是否引入禁语、是否超长。
    """
    problems = []
    patches = cand.get("patches") or []
    if not patches:
        problems.append("候选没有任何 patch")
    forbidden = list((base_cfg.get("compliance") or {}).get("forbidden_words") or [])

    for i, p in enumerate(patches):
        path = p.get("path") or ""
        val = p.get("value")
        if not path.startswith(ALLOWED_PREFIX):
            problems.append("patch[%d] 路径 %r 不在允许范围 %s（只能改话术）"
                            % (i, path, list(ALLOWED_PREFIX)))
            continue
        old = _get_path(base_cfg, path)
        if old is None:
            problems.append("patch[%d] 路径 %r 在基线配置里不存在" % (i, path))
            continue
        if not isinstance(old, str):
            problems.append("patch[%d] 目标不是字符串（%s），不允许改" % (i, path))
            continue
        if not isinstance(val, str) or not val.strip():
            problems.append("patch[%d] value 必须是非空字符串" % i)
            continue
        if len(val) > MAX_CHARS:
            problems.append("patch[%d] 文案 %d 字，超过 %d 字上限（电话里说不完）"
                            % (i, len(val), MAX_CHARS))
        for w in forbidden:
            if w and w in val:
                problems.append("patch[%d] 引入合规禁语：%s" % (i, w))
        # 占位符不许改：改 {company_name} 就是改事实来源
        for token in ("{company_name}", "{contact_role}", "{reason}",
                      "{materials}", "{application_desc}"):
            if token in old and token not in val:
                problems.append("patch[%d] 丢了事实占位符 %s" % (i, token))
        # 该阶段 must_say 的关键词不能丢
        stage = path.split(".")[1] if len(path.split(".")) > 1 else ""
        for kw in ((base_cfg.get("stages", {}).get(stage) or {}).get("must_say") or []):
            if kw in old and kw not in val:
                problems.append("patch[%d] 丢了 must_say 关键词「%s」（%s 阶段）"
                                % (i, kw, stage))
    return problems


def apply_candidate(base_cfg, cand):
    cfg = copy.deepcopy(base_cfg)
    for p in cand.get("patches") or []:
        _set_path(cfg, p["path"], p["value"])
    return cfg


# ---------------------------------------------------------------------------
# 命令：init / propose / gate / promote / rollback / status
# ---------------------------------------------------------------------------
def cmd_init(args):
    scene_file = args.get("scene") or "config/scenes/pop_reject.json"
    scene_file = scene_file if os.path.isabs(scene_file) else os.path.join(_ROOT, scene_file)
    if not os.path.exists(scene_file):
        raise SystemExit("场景文件不存在：%s" % scene_file)

    base = _read_json(scene_file)
    v1 = "v1"
    os.makedirs(_ver_dir(v1), exist_ok=True)
    _write_json(_snapshot_path(v1), base)

    metrics = None
    baseline_json = ""
    if args.get("baseline"):
        p = args["baseline"]
        p = p if os.path.isabs(p) else os.path.join(_ROOT, p)
        if os.path.exists(p):
            _bp = _read_json(p)
            _got = _bp.get("scene_sha") or ""
            _want = _sha(scene_file)
            # 基线必须和要进化的场景**内容一致**。
            # 这条校验的由来：我先把 v2 上线、再"重跑基线"，结果基线被写成了 8/10，
            # 后面闸门拿它比，等于用考过的卷子当没考过的标准。人记不住这种事，得让机器记。
            if _got and _got != _want and not args.get("force"):
                raise SystemExit(
                    "基线结果和要初始化的场景内容不一致，拒绝初始化：\n"
                    "  基线来自：%s（sha=%s）\n  当前场景：%s（sha=%s）\n"
                    "基线必须用**待进化的那份话术**跑出来。\n"
                    "  正确做法：先把场景恢复到基线版本，再跑 sim_eval。\n"
                    "  确认要强行使用请加 --force（不建议，闸门会失去意义）。"
                    % (_bp.get("scene_path"), _got, scene_file, _want))
            if not _got and not args.get("force"):
                # 没有哈希 = 无法证明这份分数对应哪份话术。
                # 第一版这里是"警告后放行"，结果负向测试当场放进来一份错基线——
                # 闸门的地基不能靠"应该没人会搞错"。
                raise SystemExit(
                    "基线结果缺少 scene_sha，无法证明它对应哪份话术，拒绝初始化：\n"
                    "  %s\n  请用当前场景重跑一次：python scripts/sim_eval.py "
                    "--json %s --tag baseline\n  （确认要用旧文件请加 --force）"
                    % (os.path.relpath(p, _ROOT), os.path.relpath(p, _ROOT)))
            metrics = _metrics(_bp)
            baseline_json = os.path.relpath(p, _ROOT).replace("\\", "/")

    # 同一套场景在仓库里有两份文件：config/scene.json（默认）与
    # config/scenes/pop_reject.json（场景包）。它们天生会漂移——
    # 只改一份，另一份还在说旧话术，演示时看到的就是"改了没生效"。
    # 所以上线时把改动的**键**同步过去，而不是整份覆盖：
    # 两份文件的 nlg.provider 本来就不同（默认开改写、跑批关改写），
    # 整份覆盖会把改写开关一起带过去。
    sync = args.get("sync")
    if sync is True or sync is None:
        sync = ["config/scene.json"] if "/scenes/" in scene_file.replace("\\", "/") else []
    elif isinstance(sync, str):
        sync = [sync]

    data = {
        "active": v1,
        "scene_file": os.path.relpath(scene_file, _ROOT).replace("\\", "/"),
        "sync_targets": sync,
        "baseline_json": baseline_json,
        "history": [{
            "version": v1, "parent": None, "created": _now(), "status": "active",
            "source": "初始基线", "candidate": None, "metrics": metrics,
            "scene_sha": _sha(_snapshot_path(v1)),
        }],
    }
    _write_json(VERSIONS_PATH, data)
    print("已初始化进化版本库：active=%s，场景=%s" % (v1, data["scene_file"]))
    if metrics:
        print("基线指标：%s" % _fmt(metrics))
    else:
        print("提示：没给 --baseline，v1 没有指标记录，后续闸门会先补跑一次。")
    return 0


def cmd_propose(args):
    """把最新基线里的**可优化簇**列出来，并生成候选文件骨架。

    为什么默认不自动填文案：候选是"要上线的业务话术"，
    没有评审的自动生成只能当草稿。骨架里给出目标键、现值、缺陷证据，
    人或（--llm）模型来填 value，然后必须过闸门。
    """
    base_json = args.get("from") or "eval/reports/sim_baseline.json"
    base_json = base_json if os.path.isabs(base_json) else os.path.join(_ROOT, base_json)
    if not os.path.exists(base_json):
        raise SystemExit("找不到基线结果：%s（先跑 scripts/sim_eval.py --json ...）" % base_json)
    payload = _read_json(base_json)
    clusters = payload.get("clusters") or []

    # 目标键**按聚类键现算**，不直接信落盘文件里的 target：
    # 基线报告可能是旧版映射生成的，照抄会把候选指到错的那句话上（本轮就踩过）。
    from modules import cluster as cluster_mod
    for c in clusters:
        parts = (c.get("key") or "").split("|")
        code = parts[0] if parts else ""
        stage = parts[1] if len(parts) > 1 else ""
        action = parts[2] if len(parts) > 2 else ""
        c["target"] = cluster_mod._target_for(code, stage, action)
        c["needs_code"] = cluster_mod._NEEDS_CODE.get(code, "")

    optional = [c for c in clusters if not c.get("needs_code") and c.get("target")]
    code_only = [c for c in clusters if c.get("needs_code")]

    print("可优化簇（改话术）：")
    if not optional:
        print("  （无）")
    for c in optional:
        print("  [%d 通] %s" % (c["sessions"], c["key"]))
        print("       目标：%s" % c["target"])
        print("       成因：%s" % c["cause"])
        print("       证据：%s" % c["examples"][0])

    if code_only:
        print("\n只能改代码的簇（**不会**生成候选，请转为开发任务）：")
        for c in code_only:
            print("  [%d 通] %s —— %s" % (c["sessions"], c["key"], c["needs_code"]))

    scene_file = _versions().get("scene_file") or "config/scenes/pop_reject.json"
    base_cfg = _read_json(os.path.join(_ROOT, scene_file))

    patches = []
    seen = {}
    for c in optional:
        key = c["target"]
        # 多个簇可能指向同一句（例如"记不住号码"与"不愿报号码"都会落到
        # reply_offtopic）。同一句只出一条 patch，否则会生成互相覆盖的重复项，
        # 闸门里看不出问题、上线后看到的是最后一条——典型的静默覆盖。
        if key in seen:
            seen[key]["clusters"].append(c["key"])
            continue
        item = {
            "path": key,
            "old": _get_path(base_cfg, key),
            "value": "",
            "cluster": c["key"],
            "clusters": [c["key"]],
            "rationale": "",
        }
        seen[key] = item
        patches.append(item)

    out = args.get("out") or "config/evolution/candidates/c001.json"
    out = out if os.path.isabs(out) else os.path.join(_ROOT, out)
    cand = {
        "id": os.path.basename(out).replace(".json", ""),
        "created": _now(),
        "base_version": _versions().get("active") or "v1",
        "scene_file": scene_file,
        "from_baseline": os.path.relpath(base_json, _ROOT).replace("\\", "/"),
        "targets": [c["key"] for c in optional],
        "patches": patches,
    }
    _write_json(out, cand)
    print("\n候选骨架已写入：%s" % os.path.relpath(out, _ROOT))
    print("下一步：填 value（或用 --llm 自动起草），然后 `evolve.py gate %s`"
          % os.path.relpath(out, _ROOT))
    if args.get("llm"):
        _llm_fill(cand, out)
    return 0


def _llm_fill(cand, out_path):
    """用 DashScope 起草文案。失败不阻塞——候选只是草稿，闸门才是裁判。"""
    key = config_loader.secret("dashscope_api_key")
    if not key:
        print("（没有 dashscope_api_key，跳过自动起草；value 需人工填）")
        return
    import urllib.request
    cfg = config_loader.load().get("nlg") or {}
    endpoint = cfg.get("endpoint") or \
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    model = cfg.get("model") or "qwen-flash"
    for p in cand["patches"]:
        prompt = (
            "你在优化一通电话外呼的话术。下面这一句是客服要说的话，"
            "它在真实通话里失败了，请改写这一句，解决失败原因。\n"
            "硬约束：\n"
            "1) 只输出一个 JSON：{\"value\": \"改写后的话术\", \"rationale\": \"为什么这样改\"}\n"
            "2) 不超过 %d 个汉字，口语，像人说话，不用书面语套话\n"
            "3) 必须保留原句里的占位符（花括号内容）不许改\n"
            "4) 不许承诺审核结果（不许出现：保证通过/包过/一定通过）\n"
            "5) 如果用户在质疑来电身份，要**正面回答**他为什么被联系，而不是说「记下了稍后解答」\n"
            "6) 如果用户说记不住号码，要给出**别的路径**（比如报姓名）\n\n"
            "当前话术：%s\n失败原因：%s\n真实通话证据：%s\n"
            % (MAX_CHARS, p.get("old", ""), p.get("cluster", ""),
               "")
        )
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % key,
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"].strip()
            s = text.find("{")
            e = text.rfind("}")
            obj = json.loads(text[s:e + 1])
            p["value"] = (obj.get("value") or "").strip()
            p["rationale"] = (obj.get("rationale") or "").strip()
            print("  LLM 起草 %s → %s" % (p["path"], p["value"][:40]))
        except Exception as e:                       # 网络/配额/格式都可能失败
            print("  LLM 起草失败（%s），保留空 value 由人工填" % e)
    _write_json(out_path, cand)


def cmd_gate(args):
    """跑三道闸门。通过则登记为 gated 版本，**不自动上线**。"""
    cand_path = args.get("candidate")
    if not cand_path:
        raise SystemExit("用法：evolve.py gate <候选文件>")
    cand_path = cand_path if os.path.isabs(cand_path) else os.path.join(_ROOT, cand_path)
    cand = _read_json(cand_path)

    vd = _versions()
    if not vd.get("history"):
        raise SystemExit("还没初始化版本库，先跑 evolve.py init")
    active = vd["active"]
    scene_file = vd.get("scene_file") or "config/scenes/pop_reject.json"
    base_cfg = _read_json(os.path.join(_ROOT, scene_file))

    # 线上场景被人手改过、版本库还是旧快照时，闸门跑出来的"收益"没有意义：
    # 候选是在旧话术上叠加的，线上跑的却是新话术。11-08 实测——L2 建库后
    # 又给 CONFIRM 加了 reply_after_ask，三个哈希两两不同，却没有任何一处报错。
    # 这里直接拒绝，让 doctor/rebase 先把地基对齐。
    _live = _sha(os.path.join(_ROOT, scene_file))
    _snap = _sha(_snapshot_path(active)) if os.path.exists(_snapshot_path(active)) else ""
    if _snap and _live != _snap:
        raise SystemExit(
            "线上场景内容与 active 版本快照不一致，拒绝闸门：\n"
            "  线上 %s（sha=%s）\n  快照 %s（sha=%s）\n"
            "先跑 `python scripts/evolve.py doctor` 看差异，再 `rebase` 重定基线。"
            % (scene_file, _live, os.path.relpath(_snapshot_path(active), _ROOT), _snap))

    # 基线指标：优先用 active 版本登记过的；没有就现跑一次
    active_rec = next((h for h in vd["history"] if h["version"] == active), {})
    base_m = active_rec.get("metrics")

    # 基线来源自检：登记的基线结果必须真的是**当前 active 版本**跑出来的。
    # 不一致（例如有人拿升级后的线上场景重跑过基线）就重跑，不让人凭记忆担保。
    _want_sha = _sha(_snapshot_path(active)) if os.path.exists(_snapshot_path(active)) \
        else _sha(os.path.join(_ROOT, scene_file))
    _bj = vd.get("baseline_json")
    _have_sha = ""
    if _bj:
        _p = _bj if os.path.isabs(_bj) else os.path.join(_ROOT, _bj)
        if os.path.exists(_p):
            _have_sha = _read_json(_p).get("scene_sha") or ""
    if base_m and _have_sha and _want_sha and _have_sha != _want_sha:
        print("基线结果与 active 版本内容不一致（sha %s ≠ %s），重跑基线……"
              % (_have_sha, _want_sha))
        tmp = os.path.join(_ROOT, "eval", "reports", "sim_baseline_%s.json" % active)
        run_sim(_snapshot_path(active), tmp, tag="base_" + active)
        base_m = _metrics(_read_json(tmp))
        _bj_rel = os.path.relpath(tmp, _ROOT).replace("\\", "/")
        vd["baseline_json"] = _bj_rel
        active_rec["metrics"] = base_m
        _write_json(VERSIONS_PATH, vd)
        _bj = _bj_rel

    if not base_m:
        print("active 版本没有指标记录，先补跑基线……")
        tmp = os.path.join(_ROOT, "eval", "reports", "_evo_base_%s.json" % active)
        base_m = _metrics(run_sim(os.path.join(_ROOT, scene_file), tmp, tag="base_" + active))
        active_rec["metrics"] = base_m
        _write_json(VERSIONS_PATH, vd)

    print("基线（%s）：%s" % (active, _fmt(base_m)))

    problems = validate_candidate(cand, base_cfg)
    if problems:
        print("\n候选不合规，未进闸门：")
        for p in problems:
            print("  - %s" % p)
        cand["gate"] = {"result": "rejected_static", "reasons": problems, "at": _now()}
        _write_json(cand_path, cand)
        return 1

    # 生成候选场景并跑批
    new_ver = "v%d" % (len(vd["history"]) + 1)
    variant_cfg = apply_candidate(base_cfg, cand)
    os.makedirs(_ver_dir(new_ver), exist_ok=True)
    _write_json(_snapshot_path(new_ver), variant_cfg)

    out_json = os.path.join(_ROOT, "eval", "reports", "sim_%s.json" % new_ver)
    payload = run_sim(_snapshot_path(new_ver), out_json, tag=new_ver)
    m = _metrics(payload)

    print("候选（%s）：%s" % (new_ver, _fmt(m)))

    checks = []
    checks.append(("train 不降", m["train_ok"] >= base_m["train_ok"],
                   "%d → %d" % (base_m["train_ok"], m["train_ok"])))
    checks.append(("holdout 不降", m["holdout_ok"] >= base_m["holdout_ok"],
                   "%d → %d" % (base_m["holdout_ok"], m["holdout_ok"])))
    improved = (m["all_ok"] > base_m["all_ok"]
                or m["agent_defect_sessions"] < base_m["agent_defect_sessions"])
    checks.append(("有实际收益", improved,
                   "总通过 %d→%d，系统缺陷会话 %d→%d"
                   % (base_m["all_ok"], m["all_ok"],
                      base_m["agent_defect_sessions"], m["agent_defect_sessions"])))

    # 「没有任何一通电话从通过变成不通过」——这才是回退判据。
    # 之前这里写的是「不得出现新的缺陷类型」，那是错的：候选让通话走得更远，
    # 自然会露出更深的坑（本次 v2 就露出「报了姓名却不收」）。
    # 惩罚这种"发现"等于奖励"别往下走"，也会逼着优化器只改安全的表面话术。
    base_rows = {}
    _bj = vd.get("baseline_json")
    if _bj:
        _p = _bj if os.path.isabs(_bj) else os.path.join(_ROOT, _bj)
        if os.path.exists(_p):
            base_rows = {r["id"]: r for r in (_read_json(_p).get("rows") or [])}
    regressed = sorted(r["id"] for r in (payload.get("rows") or [])
                       if base_rows.get(r["id"], {}).get("ok") and not r["ok"])
    checks.append(("无个体回退", not regressed,
                   "回退的画像：%s" % (regressed) if regressed else "无"))
    checks.append(("缺陷总数不增", m["defect_total"] <= base_m["defect_total"],
                   "%d → %d" % (base_m["defect_total"], m["defect_total"])))

    new_codes = sorted(set(m["codes"]) - set(base_m["codes"]))
    if new_codes:
        # 不阻断，但必须写进报告：这些是"被暴露出来的"问题，不是"被制造出来的"。
        print("说明：候选让通话走得更远，新暴露了缺陷类型 %s（已记入待办，不阻断上线）"
              % new_codes)

    print("\n闸门：")
    ok = True
    for name, passed, detail in checks:
        print("  [%s] %-14s %s" % ("PASS" if passed else "FAIL", name, detail))
        ok = ok and passed

    rec = {
        "version": new_ver, "parent": active, "created": _now(),
        "status": "gated" if ok else "rejected",
        "source": "候选 %s" % os.path.relpath(cand_path, _ROOT).replace("\\", "/"),
        "candidate": {"patches": cand["patches"], "targets": cand.get("targets")},
        "metrics": m, "baseline_metrics": base_m,
        "gate": [{"check": n, "pass": p, "detail": d} for n, p, d in checks],
    }
    vd["history"].append(rec)
    _write_json(VERSIONS_PATH, vd)

    cand["gate"] = {"result": "pass" if ok else "rejected_metrics",
                    "checks": rec["gate"], "version": new_ver, "at": _now(),
                    "metrics": m}
    _write_json(cand_path, cand)

    print("\n%s：%s（%s）" % ("通过" if ok else "未通过", new_ver,
                             "可 promote" if ok else "已记入历史，不采用"))
    print("落盘：%s｜候选快照：%s"
          % (os.path.relpath(out_json, _ROOT),
             os.path.relpath(_snapshot_path(new_ver), _ROOT)))
    return 0 if ok else 1


def cmd_promote(args):
    """把某个 gated 版本写入线上场景文件，并切换 active 指针。"""
    ver = args.get("version")
    if not ver:
        raise SystemExit("用法：evolve.py promote <版本号>")
    vd = _versions()
    rec = next((h for h in vd["history"] if h["version"] == ver), None)
    if not rec:
        raise SystemExit("没有这个版本：%s" % ver)
    # 允许重新上线被回滚过的版本：回滚本身也应当可撤销，
    # 否则"先回滚看看，确认没问题再放回去"这条路就走不通，人只能硬着头皮不回滚。
    if rec.get("status") not in ("gated", "active", "rolled_back", "superseded"):
        raise SystemExit("版本 %s 状态是 %s，没过闸门的版本不允许上线"
                         % (ver, rec.get("status")))
    if not os.path.exists(_snapshot_path(ver)):
        raise SystemExit("版本快照不存在：%s" % _snapshot_path(ver))

    scene_file = os.path.join(_ROOT, vd.get("scene_file") or "config/scenes/pop_reject.json")
    # 上线前留一份线上原件，回滚时用它兜底（快照被误删也还有救）
    backup = os.path.join(_ver_dir(ver), "live_before_promote.json")
    _write_json(backup, _read_json(scene_file))

    _write_json(scene_file, _read_json(_snapshot_path(ver)))
    paths = [p.get("path") for p in ((rec.get("candidate") or {}).get("patches") or [])
             if p.get("path")]
    _sync_apply(vd, paths, _read_json(_snapshot_path(ver)))
    prev = vd["active"]
    vd["active"] = ver
    rec["status"] = "active"
    rec["promoted_at"] = _now()
    for h in vd["history"]:
        if h["version"] == prev and h["status"] == "active":
            h["status"] = "superseded"
            h["superseded_at"] = _now()
    _write_json(VERSIONS_PATH, vd)

    print("已上线：%s（原 %s）→ %s" % (ver, prev,
                                    os.path.relpath(scene_file, _ROOT)))
    print("线上原件备份：%s" % os.path.relpath(backup, _ROOT))
    print("回滚命令：python scripts/evolve.py rollback")
    return 0


def cmd_rollback(args):
    """回到上一个版本。**保留文件**，只挪指针——回滚本身也要可再回滚。"""
    vd = _versions()
    active = vd.get("active")
    rec = next((h for h in vd["history"] if h["version"] == active), None)
    if not rec or not rec.get("parent"):
        raise SystemExit("当前是初始版本，没有可回滚的上一个版本")
    prev_ver = rec["parent"]
    if not os.path.exists(_snapshot_path(prev_ver)):
        raise SystemExit("上一版本快照不存在：%s" % _snapshot_path(prev_ver))

    scene_file = os.path.join(_ROOT, vd.get("scene_file") or "config/scenes/pop_reject.json")
    _write_json(scene_file, _read_json(_snapshot_path(prev_ver)))
    # 同步目标也要跟着退回去：只改主文件会让两份场景一个说新话术、一个说旧话术。
    paths = [p.get("path") for p in ((rec.get("candidate") or {}).get("patches") or [])
             if p.get("path")]
    _sync_apply(vd, paths, _read_json(_snapshot_path(prev_ver)))
    rec["status"] = "rolled_back"
    rec["rolled_back_at"] = _now()
    prev_rec = next((h for h in vd["history"] if h["version"] == prev_ver), None)
    if prev_rec:
        prev_rec["status"] = "active"
    vd["active"] = prev_ver
    _write_json(VERSIONS_PATH, vd)

    print("已回滚：%s → %s" % (active, prev_ver))
    print("线上场景文件已恢复为 %s 的内容" % prev_ver)
    return 0


def cmd_doctor(args):
    """体检：三个哈希必须一致，否则自进化的结论不可信。

    三个哈希分别是：
        live     线上场景文件当前内容
        snapshot active 版本登记时的内容
        baseline 基线跑批结果自称对应的内容
    两两不等最常见的成因是"建库之后又有人改了话术但没重定基线"。
    """
    vd = _versions()
    if not vd.get("history"):
        print("还没有版本库。先跑 evolve.py init")
        return 1
    active = vd["active"]
    scene_file = vd.get("scene_file") or "config/scenes/pop_reject.json"
    live = _sha(os.path.join(_ROOT, scene_file))
    snap = _sha(_snapshot_path(active)) if os.path.exists(_snapshot_path(active)) else ""
    bj = vd.get("baseline_json") or ""
    bp = bj if os.path.isabs(bj) else os.path.join(_ROOT, bj)
    base = _read_json(bp).get("scene_sha", "") if bj and os.path.exists(bp) else ""

    print("active        : %s" % active)
    print("线上场景      : %s  sha=%s" % (scene_file, live))
    print("active 快照   : sha=%s" % (snap or "(缺失)"))
    print("基线结果      : %s  sha=%s" % (bj or "(未登记)", base or "(缺失)"))

    problems = []
    if not snap:
        problems.append("active 版本没有快照，无法证明线上跑的是哪一版")
    elif live != snap:
        problems.append("线上场景已被改动，与 active 快照不是同一份话术")
    if not base:
        problems.append("基线结果没有内容哈希，无法证明它对应哪份话术")
    elif snap and base != snap:
        problems.append("基线结果不是用 active 版本跑出来的（可能是升级后的场景重跑过）")

    # --deep：内容哈希只能证明"话术没变"，证明不了"代码没变"。
    # 11-08 修了两个流程缺陷（姓名槽位 / NEED 不复读），场景一个字没动，
    # 但跑批从 8/10 变成 9/10——这种漂移三个哈希全绿也照不出来。
    # 所以深度体检直接重跑一遍 active 场景，比对登记的分数。
    if args.get("deep") and not problems:
        active_rec = next((h for h in vd["history"] if h["version"] == active), {})
        want = (active_rec.get("metrics") or {}).get("all_ok")
        tmp = os.path.join(_ROOT, "eval", "reports", "_doctor_recheck.json")
        got = _metrics(run_sim(scene_file, tmp, tag="doctor_" + active))["all_ok"]
        print("深度复算：登记 %s/10，重跑 %s/10" % (want, got))
        if want is not None and got != want:
            problems.append("基线分数已失真（%s → %s）：代码变过，先跑 rebase" % (want, got))

    if problems:
        print("\n体检不通过：")
        for x in problems:
            print("  - %s" % x)
        print("修法：python scripts/evolve.py rebase（用当前线上话术重定基线）")
        return 1
    print("\n体检通过：三处内容一致，可以跑闸门。")
    return 0


def cmd_rebase(args):
    """用**当前线上场景**重定基线，登记为一个新版本。

    场景配置在版本库建成之后被人为改动（加话术、改口径）是正常的产品迭代，
    不该要求把改动退回去；但也绝不能假装基线还是那个基线。做法是把当前内容
    原样登记成新版本、用这同一份内容重跑跑批，让三个哈希重新对齐。
    历史版本全部保留，随时可以 rollback 回去。
    """
    vd = _versions()
    if not vd.get("history"):
        raise SystemExit("还没有版本库。先跑 evolve.py init")
    active = vd["active"]
    scene_file = vd.get("scene_file") or "config/scenes/pop_reject.json"
    live_path = os.path.join(_ROOT, scene_file)
    new_ver = "v%d" % (len(vd["history"]) + 1)

    os.makedirs(_ver_dir(new_ver), exist_ok=True)
    _write_json(_snapshot_path(new_ver), _read_json(live_path))

    out_json = os.path.join(_ROOT, "eval", "reports", "sim_baseline_%s.json" % new_ver)
    payload = run_sim(scene_file, out_json, tag="base_" + new_ver)
    m = _metrics(payload)

    # 重跑出来的跑批文件，其 scene_sha 必须就是这份线上内容的哈希；
    # 不是就说明子进程拿到了别的场景（环境注入失效），不能登记。
    got = payload.get("scene_sha") or ""
    want = _sha(_snapshot_path(new_ver))
    if got != want:
        raise SystemExit("重定基线失败：跑批结果 sha=%s，但新快照 sha=%s，"
                         "子进程没跑在目标场景上" % (got, want))

    rec = {
        "version": new_ver, "parent": active, "created": _now(), "status": "active",
        "source": "rebase：线上场景内容变更，重定基线", "candidate": None,
        "metrics": m, "scene_sha": want,
    }
    for h in vd["history"]:
        if h["version"] == active and h.get("status") == "active":
            h["status"] = "superseded"
            h["superseded_at"] = _now()
    vd["history"].append(rec)
    vd["active"] = new_ver
    vd["baseline_json"] = os.path.relpath(out_json, _ROOT).replace("\\", "/")
    _write_json(VERSIONS_PATH, vd)

    print("已重定基线：%s → %s" % (active, new_ver))
    print("线上场景 sha=%s，快照 sha=%s，跑批 sha=%s（三者一致）" % (want, want, got))
    print("基线指标：%s" % _fmt(m))
    print("回滚命令：python scripts/evolve.py rollback")
    return 0


def cmd_status(args):
    vd = _versions()
    if not vd.get("history"):
        print("还没有版本库。先跑 evolve.py init")
        return 0
    print("场景文件：%s" % vd.get("scene_file"))
    print("当前生效：%s" % vd.get("active"))
    print()
    print("%-6s %-8s %-22s %-10s %s" % ("版本", "状态", "指标", "父版本", "来源"))
    print("-" * 100)
    for h in vd["history"]:
        m = h.get("metrics")
        print("%-6s %-8s %-22s %-10s %s" % (
            h["version"], h.get("status", ""),
            ("%d/%d｜缺陷 %d" % (m["all_ok"], m["all_total"], m["agent_defect_sessions"]))
            if m else "-",
            h.get("parent") or "-", h.get("source", "")))
    return 0


# ---------------------------------------------------------------------------
def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    args = {}
    i = 0
    positional = []
    while i < len(rest):
        a = rest[i]
        if a.startswith("--"):
            key = a[2:].replace("-", "_")
            if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
                args[key] = rest[i + 1]
                i += 2
            else:
                args[key] = True
                i += 1
        else:
            positional.append(a)
            i += 1
    if cmd == "gate" and positional:
        args["candidate"] = positional[0]
    if cmd == "promote" and positional:
        args["version"] = positional[0]
    if cmd == "propose" and positional and "out" not in args:
        args["out"] = positional[0]

    table = {
        "init": cmd_init, "propose": cmd_propose, "gate": cmd_gate,
        "promote": cmd_promote, "rollback": cmd_rollback, "status": cmd_status,
        "doctor": cmd_doctor, "rebase": cmd_rebase,
    }
    fn = table.get(cmd)
    if not fn:
        print("未知命令：%s（可用：%s）" % (cmd, "/".join(sorted(table))))
        return 2
    return fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
