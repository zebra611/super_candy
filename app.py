"""app.py —— 哑链路入口：一个只用标准库的 HTTP 服务。

跑起来：
    python app.py              # 默认 http://127.0.0.1:8000
    python app.py 9000         # 换端口

它做四件事：
    1. 把 web/index.html 发给浏览器（前端终端）；
    2. 提供 /api/* 接口，串起"假语音 → 规则编排 → 假工具 → 真事件流"；
    3. 把每一轮的时延、事件与成本估算写进 data/events.db，供质检与评测读取；
    4. 管住会话的边界：token、并发、TTL、容量与成本闸门。

刻意不做的事（骨架阶段）：
    - 不做 WebRTC（S1 用 HTTP 分片、S2 用 /ws/call WebSocket，纯标准库实现）；
    - 不引入任何第三方库（FastAPI/uvicorn 等都先不上），保证 clone 下来就能跑。

后续待补充：
    - S2 已落地：/ws/call 双向通道（音频帧上行 + TTS 分段主动下发 + barge_in
      协议事件 + 可选流式识别字幕），见 ws_channel.py 与 modules/realtime_asr.py；
    - 访问日志与错误日志落文件（data/logs/app.log，5MB × 5 轮转），见 setup_logging()。
      控制台仍只打启动信息，避免刷屏；事后查问题看日志文件。
"""

import base64
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
import logging
from urllib.parse import urlparse, parse_qs

import config_loader
import contracts
import cost_guard
import dashboard as dashboard_mod
import events
import ws_channel
from modules import dialogue, eval as eval_mod, official_script, tools, voice
from modules import nlg
from modules import llm
from modules import semantic_judge
from modules import wsproto

_ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(_ROOT, "web")

# ---------------------------------------------------------------------------
# 日志（H3）：访问日志 + 错误日志落文件，带轮转
# ---------------------------------------------------------------------------
#
# 为什么必须落文件：演示现场崩了，控制台窗口一关就什么都不剩；而"哪一轮出问题"
# 只有日志里有。所以访问日志记 method/path/状态码/耗时，错误日志记 error 事件的
# session_id + turn_id + code——照着这两个字段能直接回到事件流里那一条。
#
# 为什么自己 RotatingFileHandler 而不是 logrotate：纯标准库、零依赖是项目底线，
# 而 Windows 上没有 logrotate。5MB × 5 = 最多 25MB，演示机不会撑爆。

_LOG_DIR = os.path.join(_ROOT, "data", "logs")
_LOG_PATH = os.path.join(_LOG_DIR, "app.log")

_access_log = logging.getLogger("super_candy.access")
_app_log = logging.getLogger("super_candy.app")
_logging_ready = False


def setup_logging(path=None, level=logging.INFO, max_bytes=5 * 1024 * 1024, backups=5):
    """把访问日志与错误日志挂到文件上。重复调用不会重复挂 handler。

    参数 path 可覆盖文件位置（测试用临时文件），默认 data/logs/app.log。
    返回 (access_logger, app_logger)。
    """
    global _logging_ready
    if _logging_ready:
        return _access_log, _app_log

    target = path or _LOG_PATH
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        handler = RotatingFileHandler(target, maxBytes=max_bytes,
                                      backupCount=backups, encoding="utf-8")
    except OSError as e:
        # 日志目录建不出来不该让服务起不来——宁可退回控制台
        print("[日志] 无法创建日志文件 %s：%s（退回控制台输出）" % (target, e))
        handler = logging.StreamHandler()

    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S"))
    for lg in (_access_log, _app_log):
        lg.setLevel(level)
        lg.propagate = False
        lg.addHandler(handler)
    _logging_ready = True
    return _access_log, _app_log


def log_turn_error(session_id, turn_id, code, detail=""):
    """记一条错误日志，字段与事件流对齐，能直接对回去。

    为什么不只依赖 events 表：事件表是给质检和看板用的，要查"刚才那一轮为什么
    没回写"时，人第一时间看的是日志文件，而不是去 sqlite 里 select。
    """
    _app_log.warning("turn_error session=%s turn=%s code=%s %s",
                     session_id, turn_id, code, str(detail)[:300])


# 单次上传音频的大小上限（字节）。防止误传大文件把内存打爆。
MAX_AUDIO_BYTES = 8 * 1024 * 1024

# 内存里的会话状态：session_id -> session dict。
# 进程重启即丢失——骨架阶段可以接受，真实实现要落库。
_SESSIONS = {}
# 只保护 _SESSIONS 这个字典本身（增删改查）。不保护对话处理：
# 同一会话的并发由 busy 标志挡住，不同会话本来就该并行。
_STORE_LOCK = threading.Lock()


class PayloadTooLarge(Exception):
    """请求体超过上限。单独一个异常是为了返回 413，而不是含糊的 400。"""


# ---------------------------------------------------------------------------
# 会话边界：token、并发、TTL、容量
# ---------------------------------------------------------------------------

def _purge_sessions():
    """清理过期会话：TTL 到期 + 超出上限时淘汰最久没动的。

    为什么必须做：浏览器关掉页面不会调 /api/call/end，会话就会一直留在内存里。
    骨架阶段不清理也能跑，但长期开演示或压测时会把内存慢慢吃满。
    正在处理中的会话（busy=True）不清理，否则会把别人正在说的那一轮丢掉。
    """
    cfg = config_loader.budget_config()
    ttl = float(cfg.get("session_ttl_sec", 1800))
    limit = int(cfg.get("max_sessions", 200))
    now = time.time()

    with _STORE_LOCK:
        for sid in [s for s, v in _SESSIONS.items()
                    if not v.get("busy") and now - v.get("last_active_ts", now) > ttl]:
            _SESSIONS.pop(sid, None)

        overflow = len(_SESSIONS) - limit
        if overflow > 0:
            idle = sorted((s for s, v in _SESSIONS.items() if not v.get("busy")),
                          key=lambda s: _SESSIONS[s].get("last_active_ts", 0))
            for sid in idle[:overflow]:
                _SESSIONS.pop(sid, None)


def _acquire(sess):
    """占住这个会话。已被占住就返回 False（调用方回 409）。

    为什么要有这个：同一通电话并行来两个 /api/turn（用户狂点、或前端 bug
    重复发送），两条请求会同时改状态机，导致跳环节、事件顺序错乱。
    宁可让第二个请求失败，也不要让状态机串台。
    """
    with _STORE_LOCK:
        if sess.get("busy"):
            return False
        sess["busy"] = True
        return True


def _release(sess):
    """释放会话并刷新活跃时间（TTL 从这里算）。"""
    with _STORE_LOCK:
        sess["busy"] = False
        sess["last_active_ts"] = time.time()


def active_session_count():
    """还没挂断的会话数。

    为什么切场景要关心这个：话术是每轮现读配置的，所以切完之后
    **正在进行的那通电话下一句就换了套业务**——实测过：开场说"京东客服中心的小采"，
    切到复购场景后下一句变成"会员礼包 20 元券"，同一通电话里两个业务。
    演示时现场对比两套场景是这个功能的设计意图，但误点一下就会毁掉正在演示的电话，
    所以至少要能告诉调用方"现在有 N 通电话在用旧场景"。
    """
    with _STORE_LOCK:
        return sum(1 for v in _SESSIONS.values() if not v.get("closed"))


#: 训练回放串行化：跑一场要独占状态机与工具层，两场并行会串台。
_TRAIN_LOCK = threading.Lock()
_TRAIN_MAX_TURNS = 14


_EVOLVE_LOCK = threading.Lock()
_EVOLVE_ROOT = os.path.join(_ROOT, "eval", "reports", "self_evolve")
_EVOLVE_ID_OK = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _evolve_runs(limit=12):
    """自进化运行清单（读 index.jsonl，最新在前）。

    只回展示必需字段：运行编号、时间、轮数、负/正面变化、通过数、采纳的调整。
    训练细节（逐句反馈、配置快照）走单次详情接口，不塞进列表里。
    """
    path = os.path.join(_EVOLVE_ROOT, "index.jsonl")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind", "train") != "train":
                continue    # 上架记录单独一行，不进训练下拉框
            rows.append(row)
    return list(reversed(rows))[:limit]


def _evolve_run(run_id):
    """单次运行的完整记录：每轮指标、改了什么、判定与原因、只能改代码的缺陷。"""
    if not _EVOLVE_ID_OK.match(run_id or ""):
        return None
    path = os.path.join(_EVOLVE_ROOT, run_id, "run.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        run = json.load(f)
    # 绝对路径不出浏览器：把报告路径换成仓库相对路径
    for k in ("final_scene", "base_scene"):
        v = run.get(k) or ""
        if os.path.isabs(v):
            run[k] = os.path.relpath(v, _ROOT).replace("\\", "/")
    return run


def _train_personas():
    """对抗画像清单：只回展示需要的字段，不回 knows / traits / style。

    顺带回一句"这个画像这次会抽到哪家店"：训练回放里客户说的话必须与客服
    手上的档案是同一份，前端把店名显示出来，谁看都知道两边对得上。
    """
    from scripts import adversarial_evolve as adv
    out = []
    for p in adv.load_personas():
        card = _train_target(p) or {}
        knows = card.get("knows") or {}
        out.append({
            "id": p["id"], "label": p.get("label", ""), "goal": p.get("goal", ""),
            "split": p.get("split", "train"),
            "max_level": int(p.get("max_level") or 4),
            "expect": p.get("expect") or {},
            "merchant": knows.get("company_name") or "",
            "reject": knows.get("reject_reason_short") or "",
        })
    return out


def _train_target(persona):
    """给一个画像固定一家商户：同一个 A11 每次演示都是同一个人。

    不随机抽是为了对照——两次训练只有"改了什么"不同，客户换了人就没法比。
    """
    try:
        from modules import customer_data
        cards = customer_data.callable_cards()
    except Exception:
        return None
    if not cards:
        return None
    n = int(re.sub(r"\D", "", str(persona.get("id") or "")) or 0)
    return cards[n % len(cards)]


def _train_run(persona, user="llm", max_turns=None):
    """真跑一场对抗对话，返回逐轮轨迹。

    两条隔离都必须做：
        1. 事件库切到临时目录——训练不是真实通话，不能混进会话数与时延分位数；
        2. 说出口的东西必须能降级——模型不可用时退回纯规则，绝不能让页面白屏。
    两处都在 finally 里恢复，即使跑批抛异常，也不会把生产库路径留在临时目录。

    `user="rule"` 走「强制模板话术 + 模板客户」：这一档可逐字节复现，
    和 `scripts/adversarial_evolve.py` 的跑批是同一个世界，用来对照。
    """
    from scripts import adversarial_evolve as adv
    old_dir, old_db = events.DATA_DIR, events.DB_PATH
    old_tts = config_loader.tts_config
    old_nlg = config_loader.nlg_config
    card = _train_target(persona) or {}
    application_id = (card.get("knows") or {}).get("application_id") or None
    use_llm = (str(user) == "llm") and llm.available()
    tmp = tempfile.mkdtemp(prefix="sc_train_")
    events.DATA_DIR = tmp
    events.DB_PATH = os.path.join(tmp, "train.db")
    events.init_db()
    # 训练不播语音：TTS 走浏览器口径，避免每轮调一次合成（慢且花钱）。
    config_loader.tts_config = lambda: {"provider": "browser",
                                        "voice": "zh-CN-XiaoxiaoNeural", "rate": 1.05}
    try:
        if use_llm:
            # 客服侧也开表达层：这一档是"双端都像人"的现场演示，
            # 报告里会分开记客户侧与客服侧各自的模型调用次数。
            base_nlg = dict((config_loader.load() or {}).get("nlg") or {})
            config_loader.nlg_config = lambda: dict(
                base_nlg, provider="dashscope", assist=True)
            out = adv.run_one(persona, max_turns=max_turns or _TRAIN_MAX_TURNS,
                              llm_sim=True, application_id=application_id)
        else:
            with nlg.template_only():
                out = adv.run_one(persona, max_turns=max_turns or _TRAIN_MAX_TURNS,
                                  application_id=application_id)
    finally:
        events.DATA_DIR, events.DB_PATH = old_dir, old_db
        events.init_db()
        config_loader.tts_config = old_tts
        config_loader.nlg_config = old_nlg
        shutil.rmtree(tmp, ignore_errors=True)

    expect = persona.get("expect") or {}
    want = expect.get("closed")
    out["expect"] = expect
    out["ok"] = (want is None) or (bool(want) == bool(out.get("closed")))
    out["verdict"] = "通过" if out["ok"] else "失败"
    # 期望单独回一个字段：前端原来用 `d.expect.closed === true` 推文案，
    # 画像没写 expect 时会显示成"期望不闭环"，和"失败"自相矛盾。
    # 判定与文案必须同源，否则看的人会怀疑整个结果。
    out["expect_closed"] = None if want is None else bool(want)
    out["scene"] = config_loader.scene_name()
    out["max_turns"] = _TRAIN_MAX_TURNS
    out["user_mode"] = "llm" if use_llm else "rule"
    out["llm_model"] = llm.model_name() if use_llm else ""
    # 两档都回同一个 card_id：前端靠它说明"客户报的号码就是客服手上这张单"，
    # 也方便复现"这一场是谁"。规则档由 app 自己补，run_one 只在模型档回它。
    out["card_id"] = card.get("card_id") or ""
    return out


def _auth(session_id, token):
    """校验会话存在且 token 匹配，返回 (session, error_dict, http_code)。

    为什么用会话级 token 而不是全局密码：这通电话的数据只该给发起它的人看。
    全局密码一旦写进前端就等于公开；会话 token 只活在这一次通话里，
    别人捡到 session_id 也读不到姓名与尾号。用 compare_digest 是为了避免
    按字符比较泄露长度与位置信息（虽然这里风险很低，但成本几乎为零）。

    token 缺失时**一律拒绝**，不做"本地开发就放行"的例外——
    例外条件最容易在部署时忘记关。
    """
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return None, {"error": "UNKNOWN_SESSION", "session_id": session_id}, 400
    if not secrets.compare_digest(str(sess.get("read_token", "")), str(token or "")):
        return None, {"error": "FORBIDDEN", "detail": "缺少或错误的 token"}, 403
    return sess, None, 200


# ---------------------------------------------------------------------------
# 小工具：隐私打码、成本估算、闸门判断
# ---------------------------------------------------------------------------

def _mask_digits(text):
    """把连续 4 位以上的数字打码后再入库。

    用户的手机尾号是身份凭证，事件库拿来做分析就够了，不需要保留原文。
    注意：只影响**入库的事件**，对话逻辑用的仍是内存里的原文——
    如果连逻辑一起打码，身份核验会因为"查 **** 查不到"而永远失败。
    """
    if not config_loader.privacy_config().get("mask_digits_in_events", True):
        return text
    return re.sub(r"\d{4,}", lambda m: "*" * len(m.group()), text or "")


def _budget_reason(sess):
    """判断这通电话是否已经超出容量/时长闸门，返回原因（空串表示没超）。

    只在"到点"时收尾，不做中途打断：用户正在说话时突然掐掉是最差的体验。
    所以判断放在每一轮开始，超了就礼貌收尾，本轮不再走业务逻辑。
    """
    cfg = config_loader.budget_config()
    max_turns = int(cfg.get("max_turns", 0) or 0)
    if max_turns and sess["turn"] >= max_turns:
        return "max_turns"
    max_sec = float(cfg.get("max_call_seconds", 0) or 0)
    if max_sec and time.time() - sess["created_ts"] > max_sec:
        return "max_call_seconds"
    return ""


def _accrue_cost(sess, audio_bytes, reply_text):
    """按配置单价估算本轮成本，累到会话上，返回本轮增量（元）。

    这是估算，不是账单：用来在演示里回答"这通电话花了多少钱"，
    以及防止死循环把当天额度烧完。真实成本以云厂商账单为准。
    大模型按每轮约 1500 token 粗估——够用来告警，不适合用来对账。
    """
    cfg = config_loader.budget_config()
    usage = sess.setdefault("usage", {"asr_sec": 0.0, "tts_chars": 0,
                                      "turns": 0, "cost_cny": 0.0})
    # 16kHz / 16bit 单声道 ≈ 32 字节每毫秒
    asr_sec = len(audio_bytes) / 32000.0 if audio_bytes else 0.0
    tts_chars = len(reply_text or "")

    usage["asr_sec"] += asr_sec
    usage["tts_chars"] += tts_chars
    usage["turns"] += 1

    turn_cost = (asr_sec / 60.0 * float(cfg.get("price_per_min_asr", 0))
                 + tts_chars / 1000.0 * float(cfg.get("price_per_1k_chars_tts", 0))
                 + 1500 / 1000000.0 * float(cfg.get("price_per_1m_tokens", 0)))
    usage["cost_cny"] = round(usage["cost_cny"] + turn_cost, 6)

    warn = float(cfg.get("daily_warn_cny", 0) or 0)
    if warn and usage["cost_cny"] > warn and not sess.get("cost_warned"):
        sess["cost_warned"] = True
        print("[预算告警] 会话 %s 累计估算成本 %.4f 元，已超过 %.2f 元阈值"
              % (sess["session_id"], usage["cost_cny"], warn))
    return round(turn_cost, 6)


# ---------------------------------------------------------------------------
# 音频流接收（S1）：浏览器分帧上传 PCM
# ---------------------------------------------------------------------------
# 这一段是"1 号任务"在服务端的接缝：浏览器用 AudioWorklet 把麦克风切成
# 20ms / 320 采样点的 PCM16 帧，每 8 帧（160ms）打一个 HTTP 分片上来，
# 服务端按 seq 校验连续性并累积到会话缓冲，一轮结束时整段交给 ASR。
#
# 为什么现在不用 WebSocket：S1 的目标是先把"分帧 + 边采边发 + 帧号不跳"
# 这件事本身跑通并可验证。S2 会把这条 HTTP 分片换成 /ws/call 的二进制帧，
# **下面这三个函数（_audio_state / ingest_audio_chunk / flush_audio）原样复用**，
# 只有搬进搬出的通道变了。
#
# 已知边界（都是 S2 才解决的，别在这里花时间）：
#   - 每 160ms 一个 HTTP 请求，有 TCP 与头部开销，比不上长连接；
#   - 没有服务端主动下发的通道，所以做不了"服务端 cancel 播报"（打断要用它）；
#   - 半双工：一轮说完才交给 ASR，不是真的边识别边出字。

def _audio_state(sess):
    """取（或惰性创建）这个会话的音频缓冲状态。

    缓冲有上限：一轮说话累计不超过 MAX_AUDIO_BYTES，
    否则一个异常前端（或忘了 stop 的页面）就能把内存吃满。
    """
    st = sess.get("audio")
    if st is None:
        st = {
            "expect_seq": 0,     # 下一片应当出现的 seq，用来发现丢片
            "frames": 0,         # 已收帧数（20ms 一帧）
            "samples": 0,        # 已收采样点数（16k → 每帧 320）
            "gaps": 0,           # 序号不连续的次数
            "duplicates": 0,     # 重复分片的次数（seq 倒退）
            "buf": bytearray(),  # 累积的 PCM16LE 原始字节
            "started_ts": 0.0,
        }
        sess["audio"] = st
    return st


def ingest_audio_chunk(sess, seq, pcm, sample_rate=contracts.PCM_SAMPLE_RATE):
    """收一片音频：校验格式与序号后追加到会话缓冲。

    参数：
        sess         会话字典（由调用方做鉴权后传入）
        seq          这一片的第一帧序号，从 0 开始由前端递增
        pcm          PCM16LE 单声道原始字节
        sample_rate  采样率，必须等于契约值，否则拒绝
    返回：dict(seq, bytes, received_frames, received_samples, gaps, duplicates)
    异常：ValueError（参数不合法，调用方转 400）；PayloadTooLarge（累计超限，转 413）
    """
    if sample_rate != contracts.PCM_SAMPLE_RATE:
        raise ValueError("采样率必须为 %d，收到 %d" % (contracts.PCM_SAMPLE_RATE, sample_rate))
    if not pcm:
        raise ValueError("空音频分片")
    if len(pcm) % contracts.PCM_SAMPLE_WIDTH:
        raise ValueError("PCM16 字节数必须为偶数，收到 %d" % len(pcm))

    st = _audio_state(sess)
    if len(st["buf"]) + len(pcm) > MAX_AUDIO_BYTES:
        raise PayloadTooLarge("本通电话累计音频超过 %d 字节" % MAX_AUDIO_BYTES)

    # 加锁的理由：HTTP 每个请求一个线程，两片并发到达时 seq 比较与 extend
    # 会交错，可能出现"先到的被当成重复丢掉"。这把锁只保护这几行，
    # 不涉及识别与对话，所以不会拖慢正常通话。
    with _STORE_LOCK:
        if seq < st["expect_seq"]:
            # 重复分片（重试或前端 bug）：重复追加会在音频里叠出一段回声，
            # 重复的采样还会让识别结果出现叠字。宁可丢弃并计数，也不盲信。
            st["duplicates"] += 1
        else:
            if seq > st["expect_seq"]:
                # 序号跳了：说明前面的分片丢了。不在这里补，但要记下来——
                # "帧号不跳"是 S1 的验收标准，掩盖丢帧等于验收造假。
                st["gaps"] += 1
            frames = len(pcm) // contracts.FRAME_BYTES
            st["buf"].extend(pcm)
            st["samples"] += len(pcm) // contracts.PCM_SAMPLE_WIDTH
            st["frames"] += frames
            st["expect_seq"] = seq + frames
            if not st["started_ts"]:
                st["started_ts"] = time.time()

        return {"seq": seq, "bytes": len(pcm),
                "received_frames": st["frames"], "received_samples": st["samples"],
                "gaps": st["gaps"], "duplicates": st["duplicates"]}


def flush_audio(sess, dropped=0):
    """把会话缓冲里的 PCM 取出来、清空，返回 (bytes, 统计)。

    每轮说话结束时调用一次。统计会写成 audio_stream 事件，
    它是"这一轮到底采到了多少声音、有没有丢片"的唯一证据。

    参数：
        dropped  前端因背压主动丢弃的帧数（前端自己报，服务端无法感知）
    """
    st = _audio_state(sess)
    pcm = bytes(st["buf"])
    stat = {
        "bytes": len(pcm),
        "samples": st["samples"],
        "frames": st["frames"],
        "duration_ms": round(st["samples"] * 1000.0 / contracts.PCM_SAMPLE_RATE, 1),
        "gaps": st["gaps"],
        "duplicates": st["duplicates"],
        "dropped_at_client": int(dropped or 0),
        "sample_rate": contracts.PCM_SAMPLE_RATE,
        "frame_ms": contracts.FRAME_MS,
    }
    st["buf"] = bytearray()
    # 注意：**不重置 expect_seq**。
    # 半双工时代每轮重新开采集，seq 从 0 起；现在采集在通话期间常开，
    # seq 全局单调递增。一重置，下一轮的每一片都会被判成"序号倒退"。
    st["frames"] = 0
    st["samples"] = 0
    st["gaps"] = 0
    st["duplicates"] = 0
    st["started_ts"] = 0.0
    return pcm, stat


def reset_audio_buffer(sess):
    """丢弃缓冲里堆着的音频，返回丢了多少字节。

    连续对话时必须清：播报期间麦克风是开着的（要用来判打断），
    客服自己的声音会被采进来。不清理的话，这些音频会混进下一轮识别，
    轻则识别出一堆噪声，重则把"客服说的话"当成"用户说的话"。
    """
    st = _audio_state(sess)
    n = len(st["buf"])
    st["buf"] = bytearray()
    st["frames"] = 0
    st["samples"] = 0
    st["gaps"] = 0
    st["duplicates"] = 0
    st["started_ts"] = 0.0
    return n


# ---------------------------------------------------------------------------
# 业务流程：一轮对话的完整链路
# ---------------------------------------------------------------------------

#: 只为"没启用/为什么没启用"打印一次，别每轮刷屏。
_SPK_NOTED = set()


def _speaker_probe(sess, pcm, source):
    """看这一句是不是"刚才那位"说的。返回要写进事件的 dict，或 None（不适用）。

    现在只做影子模式：结论写事件，**不改变**这一轮怎么处理。
    为什么不敢直接拦：本机标定用的是 TTS 五个音色（零误杀 / 漏放 15%），
    真人双人语料一条都没有。没有真机数据就拦，等于拿演示赌一个没测过的阈值。

    注册纪律（这一条比阈值重要）：
        · 注册未满 3 条时，把说过的都收下——这时候还没有"中心"，收谁就是谁；
        · 注册满了以后，**只收判为 same 的段**继续更新中心。
          不能把旁人也往里加，否则中心会被慢慢拽到"两个人之间"，
          之后谁都判不出——这是在线聚类最典型的漂移故障。
    """
    cfg = config_loader.load().get("speaker_gate") or {}
    if not cfg.get("enabled") or source != "asr" or not pcm:
        return None

    from modules import speaker_gate as sg
    ok, why = sg.available()
    if not ok:
        if why not in _SPK_NOTED:
            _SPK_NOTED.add(why)
            print("  [speaker-gate] 未启用：%s" % why)
        return {"decision": "abstain", "reason": why, "mode": "shadow"}

    prof = sess.get("speaker_profile")
    if prof is None:
        prof = sg.SpeakerProfile()
        sess["speaker_profile"] = prof

    res = prof.decide(pcm)
    if not prof.enrolled:
        prof.add_pcm(pcm)                     # 注册期：还没中心，收到就是它
    elif res.get("decision") == "same":
        prof.add_pcm(pcm, alpha=0.25)         # 像本人：正常更新
    elif res.get("decision") == "uncertain":
        prof.add_pcm(pcm, alpha=0.05)         # 灰区：慢速跟随，防漂移

    res["mode"] = "shadow"
    res["enrolled"] = prof.count
    # 配了 enforce 也只记录：这件事还没到能拦的时候，配置文件不该比实测更乐观。
    if cfg.get("mode") == "enforce" and "enforce" not in _SPK_NOTED:
        _SPK_NOTED.add("enforce")
        print("  [speaker-gate] mode=enforce 尚未接入对话流程，当前按 shadow 记录")
    return res


def handle_turn(session_id, user_text=None, audio_bytes=b"", audio_streamed=False,
                audio_dropped=0, asr_override=None, text_only=False):
    """执行一轮对话：这是整个哑链路的核心，也是五个人将来各自替换的接缝。

    链路（每一步都写一条事件，方便事后归因）：
        ① 记本轮开始
        ② 闸门检查（轮次/时长超限就礼貌收尾，不再走业务）
        ③ VAD 判定（假）              ← 角色 1 负责替换
        ④ ASR 出文本（假）            ← 角色 1 负责替换
        ⑤ 编排生成回复（规则状态机）   ← 角色 2 负责替换
        ⑥ 需要就调业务工具（假数据）   ← 角色 2/4 负责替换
        ⑦ 合规护栏（命中整句替换）     ← 角色 3 负责替换
        ⑧ TTS 指令（前端播报）        ← 角色 1 负责替换
        ⑨ 记本轮结束、耗时分段与成本

    异常处理原则：单轮里任何一段抛异常，都记一条 error 事件并给一句兜底话术，
    **不让整通电话断掉**。外呼里中途崩掉，用户听到的是静音，比说错话更糟。

    参数：
        session_id      通话 ID
        user_text       文字模式下用户直接输入的内容（可空）
        audio_bytes     语音模式下的音频字节（PCM16 / 16k / 单声道）
        audio_streamed  为 True 表示音频已按帧上传过，本轮从会话缓冲取，
                        此时 audio_bytes 参数被忽略（前端不再整段上传）
        audio_dropped   前端因背压主动丢弃的帧数，仅用于记账
        asr_override    S2 流式识别的现成结果（{"text","provider"[,"latency_ms"]}）。
                        用户说话期间音频已经实时识别过了，落 turn 时直接采用，
                        不再整段重识别。它来自真识别，所以 fake 恒为 False、
                        source 仍是 "asr"——事件的语义不因通道不同而改变。
    返回：dict，前端需要的全部内容
    """
    t_start = time.time()
    sess = _SESSIONS[session_id]
    turn_id = sess["turn"] + 1

    # S1：音频若是按帧上来的，先把会话缓冲取出来。
    # 必须放在 turn_start 之前——否则 turn_start 里的 audio_bytes 会记成 0，
    # 而"这一轮有没有声音"正是它要回答的问题。
    audio_stat = None
    if audio_streamed:
        audio_bytes, audio_stat = flush_audio(sess, dropped=audio_dropped)

    events.write_event(session_id, "turn_start",
                       {"audio_bytes": len(audio_bytes), "text_input": bool(user_text),
                        "audio_streamed": bool(audio_streamed)},
                       turn_id=turn_id)

    if audio_stat is not None:
        # 单独记一条：这是 S1 的验收证据（帧数/采样数/序号缺口/客户端丢帧）
        events.write_event(session_id, "audio_stream", audio_stat, turn_id=turn_id)

    llm_ms = 0.0
    asr_ms = 0.0
    heard = ""
    tool_result = None

    forced = _budget_reason(sess)
    if forced:
        # 闸门收尾：这不是用户的意思，所以不写业务结论，只写一条 error 说明原因
        events.write_event(session_id, "error",
                           {"code": "BUDGET_" + forced.upper(), "stage": sess["stage"]},
                           turn_id=turn_id)
        log_turn_error(session_id, turn_id, "BUDGET_" + forced.upper(),
                       "stage=%s" % sess["stage"])
        source = "budget"
        agent_out = dialogue.forced_end(sess, forced)
    else:
        # ③ VAD：只有真的收到音频才做端点检测。
        # 文字输入不写 vad_result——否则 VAD 准确度统计里会混进一堆"没声音"的轮次。
        vad = None
        if audio_bytes:
            vad = voice.vad_detect(audio_bytes)
            events.write_event(session_id, "vad_result", vad, turn_id=turn_id)

        # ④ 三条输入路径分开处理，绝不能把"用户没说话"伪造成为一句话：
        #    有音频   → 真语音识别（fake 由识别实现自己声明）
        #    有文字   → 打字调试（不产生 fake，也不消耗识别时长）
        #    两者都空 → 用户沉默，记 no_answer，交给编排决定重问还是挂断
        is_fake = False
        provider = ""
        silence_reason = "empty_input"
        typed = (user_text or "").strip()
        if audio_bytes:
            if asr_override is not None:
                # S2 流式识别的整句结果。latency 记的是"落 turn 时等 final
                # 花了多久"，与批式的"整段上传→出文本"不是同一口径，
                # 不能放进同一个 P50 里比——所以 provider 里带 realtime 字样。
                asr_out = {
                    "text": (asr_override.get("text") or ""),
                    "confidence": None,
                    "latency_ms": round(float(asr_override.get("latency_ms", 0.0)), 1),
                    "fake": False,
                    "provider": asr_override.get("provider", "paraformer-realtime"),
                }
            else:
                asr_out = voice.asr(audio_bytes, hints=config_loader.asr_hints(), vad=vad)
            heard = asr_out["text"]
            asr_ms = asr_out["latency_ms"]
            is_fake = bool(asr_out.get("fake", False))
            provider = asr_out.get("provider", "")
            if asr_out.get("error"):
                # 识别失败要留痕，否则用户抱怨"它总说听不清"时查不出原因
                events.write_event(session_id, "error",
                                   {"code": "ASR_FAILED", "detail": str(asr_out["error"])[:200],
                                    "provider": provider}, turn_id=turn_id)
                log_turn_error(session_id, turn_id, "ASR_FAILED", asr_out["error"])
            # 有音频但一个字都没识别出来 = 用户按了说话却没出声（或识别失败）。
            # 这不是"用户说了一句空话"，必须按沉默处理，
            # 否则会把空字符串当成有效输入推进状态机——那是缺陷 3 的老路。
            if heard.strip():
                source = "asr"
            else:
                source = "silence"
                silence_reason = "no_speech"
        elif typed:
            heard = typed
            source = "text"
        else:
            source = "silence"

        if source == "silence":
            events.write_event(session_id, "no_answer",
                               {"reason": silence_reason, "stage": sess["stage"]},
                               turn_id=turn_id)
        else:
            payload = {"text": _mask_digits(heard), "latency_ms": asr_ms,
                       "fake": is_fake, "source": source}
            if provider:
                payload["provider"] = provider
            events.write_event(session_id, "asr_result", payload,
                               turn_id=turn_id, latency_ms=asr_ms)

        # ④½ 说话人一致性门（可选）：判"这句是不是刚才那位说的"。
        # 现在只做**影子模式**——判完记一条事件，不影响任何对话行为。
        # 为什么先影子：本机只在 TTS 多音色语料上标定过（零误杀/漏放 15%），
        # 还没有真人双人语料证明接进流程不会误伤。先攒真机分布，再决定是否拦。
        _spk = _speaker_probe(sess, audio_bytes, source)
        if _spk:
            events.write_event(session_id, "speaker_gate", _spk, turn_id=turn_id)

        # ⑤ 编排：吃用户话，吐回复（可能附带工具调用）
        t_llm = time.time()
        try:
            agent_out = dialogue.respond(sess, heard)   # 会就地推进 sess 的状态机
        except Exception as exc:
            events.write_event(session_id, "error",
                               {"code": "DIALOGUE_FAILED", "detail": str(exc)[:200],
                                "stage": sess["stage"]}, turn_id=turn_id)
            log_turn_error(session_id, turn_id, "DIALOGUE_FAILED", exc)
            agent_out = dialogue.safe_fallback(sess)
        llm_ms = round((time.time() - t_llm) * 1000, 2)

        # ⑥ 工具：只有编排给出 tool_call 时才触发（走到回写时触发一次，改口再触发）
        tc = agent_out.get("tool_call")
        if tc:
            events.write_event(session_id, "tool_call", tc, turn_id=turn_id)
            try:
                tool_result = tools.dispatch(tc)
                # 写操作失败重试一次：幂等键不变，所以不会重复建单（幂等的用处就在这）
                if tc.get("name") == "create_ticket" and not (tool_result or {}).get("ticket_id"):
                    tool_result = tools.dispatch(tc)
                    tool_result["retried"] = True
            except Exception as exc:
                events.write_event(session_id, "error",
                                   {"code": "TOOL_FAILED", "detail": str(exc)[:200],
                                    "tool": tc.get("name")}, turn_id=turn_id)
                log_turn_error(session_id, turn_id, "TOOL_FAILED", exc)
                tool_result = {"error": "TOOL_FAILED", "_ms": 0.0}

            events.write_event(session_id, "tool_result", tool_result,
                               turn_id=turn_id, latency_ms=tool_result.get("_ms", 0))
            # 工单真的建出来了，才算"结构化结果回写"完成——这是闭环的唯一硬证据
            if tc.get("name") == "create_ticket" and tool_result.get("ticket_id"):
                events.write_event(session_id, "writeback",
                                   {"ticket_id": tool_result["ticket_id"],
                                    "created": tool_result.get("created"),
                                    "updated": tool_result.get("updated", False),
                                    "version": tool_result.get("version", 1),
                                    "retried": tool_result.get("retried", False)},
                                   turn_id=turn_id)
                sess["closed"] = True

    # 收尾一致性：京麦自动判定 / 挂断 / 拒绝 / 含糊退出都进 DONE，
    # 但都没有回写工单——上面那个 create_ticket 分支置不到 closed。
    # 不补这一句，前端会一直显示"这通电话还在进行中"。
    if agent_out.get("next_stage") == "DONE":
        sess["closed"] = True

    # ⑦ 合规护栏：播出去之前先过滤。
    # 回写失败时不能播"已为您登记"——那是虚假承诺，必须换成兜底话术。
    # 注意：这里要用 tool_call 判断，不能用 agent_out["stage"]，
    # 因为回写那一轮所处的阶段是 CONFIRM/OFFER，不是 WRITEBACK。
    tc = agent_out.get("tool_call") or {}
    if tc.get("name") == "create_ticket" and not (tool_result or {}).get("ticket_id"):
        agent_out["reply_text"] = dialogue.fallback_text("reply_writeback")
    # 清单没真发出去就别承诺"已经发到后台"，改成如实说明 + 热线兜底。
    if tc.get("name") == "send_checklist" and (tool_result or {}).get("error"):
        agent_out["reply_text"] = (
            "清单这边暂时没同步成功，我记下来让专员回电时补发；"
            "着急也可以拨打京东商家热线 4006180092。")
    # S3：模板层先行（零网络、默认路径逐字节不变）；只有显式开启
    # compliance_judge.provider=dashscope 且有密钥时，才会在模板层没拦住时
    # 多问一次模型，补「换个说法说同样的话」。任何异常都退回模板层结论。
    guard = semantic_judge.judge(agent_out["reply_text"])
    if guard.get("audit"):
        # 只有模型真的判了违规才写这条事件；默认配置下事件流与改造前一致。
        events.write_event(session_id, "compliance_judge", guard["audit"],
                           turn_id=turn_id)
    reply_text = guard["text"]

    _reply_payload = {"text": reply_text, "stage": agent_out["stage"],
                      "next_stage": agent_out["next_stage"],
                      "intent": agent_out.get("intent", "unknown"),
                      # 意图置信度：0~1。默认 0.0 而不是 1.0——老代码路径
                      # （开场白、空输入）本来就没有置信度，写 1.0 会造出假数据。
                      "intent_conf": agent_out.get("intent_conf", 0.0),
                      "source": source,
                      "blocked": guard["blocked"], "hits": guard["hits"],
                      "replaced": guard["replaced"]}
    # 溯源字段：只在这句真的引了资料时才写，不写空壳。
    #   qa —— 官方 27 条原话（config/mock/qa.json），逐字播报、不可改写；
    #   kb —— 我们自己维护的知识条目（可改写）。
    # 分开记，是因为答辩时"这句是官方口径"和"这句是我们的解释"必须举证得清。
    for _k in ("qa", "kb"):
        if agent_out.get(_k):
            _reply_payload[_k] = agent_out[_k]
    if agent_out.get("persona"):
        _reply_payload["persona"] = agent_out["persona"]
    events.write_event(session_id, "agent_reply", _reply_payload,
                       turn_id=turn_id, latency_ms=llm_ms)

    # ⑧ TTS：只等第一段就返回，剩余段在后台合成、由前端播完首段后来取。
    # 为什么必须这样：分段并发如果仍然等全部段落合成完，响应时间不会变，
    # 用户照样要等 1.6 秒——分段就成了只让内部指标好看的花架子。
    #
    # 纯文字模式（前端 S.textOnly）**根本不放音**，前端那行就是
    # `if (!S.textOnly) speak(...)`。所以这里一个字都不该合成：11-12 实测
    # 纯文字一轮里 TTS 占 0.3–8.0 秒（长句撞上合成接口长尾会重试一次，
    # 4 秒超时 + 再一遍），而这段音频客户永远听不到——首字超 2 秒全是它。
    if text_only:
        tts_out = {"mode": "none", "text": reply_text, "provider": "none",
                   "latency_ms": 0.0, "skipped": "text_only"}
    else:
        tts_out = voice.tts(reply_text, wait_first_only=True)
    rest_handle = tts_out.pop("_rest", None)
    if rest_handle:
        sess.setdefault("tts_rest", {})[turn_id] = rest_handle
    events.write_event(session_id, "tts_start",
                       {k: v for k, v in tts_out.items() if not k.startswith("_")},
                       turn_id=turn_id)

    # ⑨ 收尾：处理耗时与"服务端下界首字"都写进 turn_end。
    # server_first_audio_ms 是"请求到达 → 首包就绪"，不含网络回程与浏览器起播，
    # 所以它是首字响应的下界，不是用户感知值；真正的 T4−T0 由前端上报。
    server_first_audio_ms = round((time.time() - t_start) * 1000, 2)
    total_ms = round((time.time() - t_start) * 1000, 2)
    turn_cost = _accrue_cost(sess, audio_bytes, "" if text_only else reply_text)

    events.write_event(session_id, "turn_end",
                       {"stage": agent_out["next_stage"], "closed": sess["closed"],
                        "source": source, "processing_ms": total_ms,
                        "server_first_audio_ms": server_first_audio_ms,
                        "asr_ms": asr_ms, "llm_ms": llm_ms,
                        # tts_ms 是服务端真的等了多久（分段合成要等全部段落），
                        # 与 processing_ms 同一口径；tts_first_ms 才是"第一段可播"
                        # 的耗时，也就是用户实际开始听到声音的那一段。
                        # 两个数会差一倍以上，只看一个会误判。
                        "tts_ms": tts_out.get("total_ms", tts_out.get("latency_ms", 0)),
                        "tts_first_ms": tts_out.get("latency_ms", 0),
                        "tts_segments": len(tts_out.get("segments") or []),
                        "est_cost_cny": turn_cost},
                       turn_id=turn_id, latency_ms=total_ms)
    sess["turn"] = turn_id

    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "user_text": heard,
        "reply_text": reply_text,
        "stage": agent_out["stage"],
        "next_stage": agent_out["next_stage"],
        "intent": agent_out.get("intent", "unknown"),
        # 这一句是模型说的还是骨架说的。编排层返回里带了，这里必须透传——
        # 少这一行，训练轨迹里每一轮都显示"未知来源"，录制统计全是 0 轮
        # （第一次录完就是 0 轮，代码里其实一直在调模型）。
        "reply_source": agent_out.get("reply_source", ""),
        "end_reason": agent_out.get("end_reason", ""),
        # 这一轮说完就该收线（终态/拒绝/挂断）。ws_channel 靠它决定要不要挂电话；
        # 不透出来，服务端永远不会主动挂断（实测就是这样）。
        "hangup": bool(agent_out.get("hangup")),
        "intent_conf": agent_out.get("intent_conf", 0.0),
        # 这句回答引了哪份资料（官方 QA / 知识条目）。测试台直接显示在气泡下方，
        # 演示时"官方原话"四个字才有依据；没引资料时字段不出现，不写空壳。
        "qa": agent_out.get("qa"),
        "kb": agent_out.get("kb"),
        "persona": agent_out.get("persona"),
        "tool_result": tool_result,
        "closed": sess["closed"],
        "slots": dict(sess["slots"]),
        "latency": {
            "asr_ms": asr_ms,
            "llm_ms": llm_ms,
            "tts_ms": tts_out.get("total_ms", tts_out.get("latency_ms", 0)),
            "tts_first_ms": tts_out.get("latency_ms", 0),
            "total_ms": total_ms,
            # 服务端下界，不是门槛口径；前端的实测值通过 /api/turn/metric 回传
            "server_first_audio_ms": server_first_audio_ms,
        },
        "tts": tts_out,
        "compliance": {"blocked": guard["blocked"], "hits": guard["hits"],
                       "replaced": guard["replaced"]},
        "usage": dict(sess.get("usage") or {}),
    }


def _scene_of(company):
    """把一条档案映射成"本次驳回场景"（官方话术，槽位已填）。

    返回 None 有三种情况，前端统一显示"该场景走站内指引/无电话话术"：
      - 京麦驳回：官方口径本来就不外呼；
      - 官方表里没有对应模板；
      - 模板里有档案填不上的槽位（如 SKU）。
    """
    if not isinstance(company, dict) or not company:
        return None
    got = official_script.script_for(company)
    if not got:
        return None
    return {"id": got["id"], "script": got["script"], "via": got["via"],
            "missing": got["missing"], "source_id": got["source_id"],
            "dialogue": official_script.dialogue_text(got["script"])}


def start_call(application_id=None, random_target=False, lock_target=False):
    """开一通新电话：生成 session_id 与会话级 token、初始化状态、记一条 call_started。

    返回的 read_token 是这通电话的读取凭证：前端拿它查事件链与报告。
    它只存在于这次通话，不进日志、不进仓库。

    外呼目标的三选一（只在开了 application_mode 的赛题场景生效）：
        application_id 给定 → 按申请单号精确取（可复现复测）
        random_target=True → 从可电话外呼的档案里随机抽一条（测试用，制造不同起点）
        都没有             → 走外呼名单取下一条待外呼（默认生产行为）
    售后场景不开 application_mode，三者都不生效，降级成没有公司名的泛问。
    """
    _purge_sessions()

    session_id = "s_" + uuid.uuid4().hex[:12]
    sess = dialogue.new_session(session_id)

    # 外呼名单：真实外呼是从名单打的，电话打给哪家系统本来就知道。
    # 官方 52 条话术因此是"请问您是（商家公司名/店铺名）…吗？"，带公司名。
    # 之前系统里没有"本通电话的目标"这个概念，开场白只能泛问"贵公司的负责人吗"，
    # 商家无从判断这通电话是找谁的——赛题四段式的第②环就这么被跳过了。
    # 名单取空（自检、名单用完）不阻塞通话：降级成没有公司名的泛问。
    # 只在**开了 application_mode 的场景**（POP 资审）去取外呼名单。
    # 售后场景不开这个开关：C5 的场景隔离要求售后完全不碰申请单库，
    # 连"取名单"都不行——否则两个业务域又共用同一份数据了。
    try:
        app_mode = bool(config_loader.load().get("application_mode"))
        if app_mode and application_id:
            _tg = tools.get_application(application_id)
        elif app_mode and random_target:
            _tg = tools.random_application()
        elif app_mode:
            _tg = tools.next_outbound_target()
        else:
            _tg = {"found": False}
    except Exception:
        _tg = {"found": False}
    if _tg.get("found") and _tg.get("application"):
        sess["outbound_target"] = _tg["application"]
    # 这一通"打给谁"是不是**外部明确指定**的。
    #
    # 指定了（运营挑了这一单 / 训练回放复测）→ 目标锁定：核身只能跟这一单比，
    #   客户报一个别家的尾号绝不许换成别人家的档案（11-11 截图那通：
    #   客户随口说 9673，命中了青海梧丘，客服接着念"梧冈品牌、运动户外类目、
    #   需补境外商标注册证"——电话打给降真，说的是梧丘的事）。
    # 没指定（跑批/自练按名单取下一条）→ 不锁定：客户报的号就是"我是谁"，
    #   查库找到谁就是谁。C4 那 340 条用例全是这么跑的（用例自带台词"尾号4565"）。
    #
    # 11-12 补 lock_target：**真实外呼一定知道打给谁**，名单取到的那一单就是本单。
    # 原来只认"外部显式指定 application_id"，于是页面点开始时走名单取单
    # （outbound_target 有值、target_locked=False），客户报任何别家的号都会被
    # 查库换单。线上实测：电话打给川上（尾号 0086），客户报 "1234" → 命中
    # 天津静姝（辛夷品牌/美妆个护）→ 客服答"核对上了，感谢配合"并把辛夷的
    # 驳回原因念给了川上——串单，还让客户以为核身通过了。
    # HTTP 入口传 lock_target=True；脚本直调 start_call() 不传，跑批行为不变。
    sess["target_locked"] = bool(application_id) or bool(lock_target)
    sess["read_token"] = secrets.token_urlsafe(12)
    sess["created_ts"] = time.time()
    sess["last_active_ts"] = time.time()
    sess["busy"] = False
    sess["usage"] = {"asr_sec": 0.0, "tts_chars": 0, "turns": 0, "cost_cny": 0.0}

    with _STORE_LOCK:
        _SESSIONS[session_id] = sess

    # 外呼目标也记在这里，不另造事件类型：contracts.EVENT_TYPES 是封闭契约，
    # 临时造名字会被 events.write_event 直接拒掉（这正是它该有的行为）。
    _started = {"scene": config_loader.scene_name()}
    _tgt = sess.get("outbound_target") or {}
    if _tgt:
        _started["outbound_application_id"] = _tgt.get("application_id")
        _started["outbound_company_name"] = _tgt.get("company_name")
    events.write_event(session_id, "call_started", _started)

    # 外呼由客服先开场：自报身份是六环节的第一环，也是合规要求。
    # 以前开场白只在"回应用户第一句"时才会出现，用户抢先报尾号就被整段跳过。
    opening_text = dialogue.opening_line(sess.get("outbound_target"))
    guard = dialogue.compliance_guard(opening_text)
    opening = guard["text"]
    events.write_event(session_id, "agent_reply",
                       {"text": opening, "stage": "GREETING", "next_stage": "GREETING",
                        "intent": "opening", "source": "opening",
                        # 开场白不是对用户输入的识别，置信度记 0。给个占位值而不是
                        # 留空，是为了让"按 intent_conf 聚合"的代码不用到处判 None。
                        "intent_conf": 0.0,
                        "blocked": guard["blocked"], "hits": guard["hits"],
                        "replaced": guard["replaced"]},
                       turn_id=0)
    # 开场白也要真合成。原来这里只把 voice.tts() 的返回值塞进事件就丢了，
    # 结果服务端花了 1 秒多、花了钱合成一段音频，前端却拿不到——
    # 用户听到的第一句永远是浏览器机械音，而后面的句子是真声音，非常割裂。
    # 同样只等首段：点"开始通话"后不该让用户干等整句 28 个字念完。
    opening_tts = voice.tts(opening, wait_first_only=True)
    rest_handle = opening_tts.pop("_rest", None)
    if rest_handle:
        sess.setdefault("tts_rest", {})[0] = rest_handle
    events.write_event(session_id, "tts_start",
                       {k: v for k, v in opening_tts.items() if not k.startswith("_")},
                       turn_id=0)

    return {"session_id": session_id, "read_token": sess["read_token"],
            "scene": config_loader.scene_name(),
            "stages": list(contracts.STAGES),
            "gate_ms": contracts.LATENCY_GATE_MS,
            "opening": opening,
            "opening_tts": opening_tts,
            # 测试/演示用：把本通电话实际锁定的外呼目标档案回传前端，
            # 前端据此在页面下方把"随机生成的公司具体信息"展示出来，
            # 便于核对客服开场白是否真的对上了这家公司。
            "matched_by": _tg.get("matched_by") if _tg.get("found") else None,
            "company": sess.get("outbound_target"),
            "scene": _scene_of(sess.get("outbound_target"))}


def record_barge_in(session_id, turn_id=0, reason="user_speech", text="",
                    latency_ms=None):
    """记录一次用户打断，并丢弃那一轮还没播完的音频。

    为什么必须丢弃剩余音频：用户插话之后，那半句话已经没有意义了。
    不丢的话，用户说完新一轮、前端回头再取一次，会在新一轮对话中间
    突然冒出上一轮的残句——听起来像客服在自言自语。

    注意这里不取消正在处理的请求：HTTP 请求发出后服务端无法中止，
    强行装作取消只会让状态更难对齐。打断的**听觉效果**由前端负责
    （立刻 pause），服务端只负责记账和清理。

    text 是前端"确认打断"时那段音频的识别结果。记下来有两个用处：
    一是复盘时能看出用户为什么打断（"等一下" vs "不是"），
    二是如果这行文本其实是噪声识别出来的胡话，就说明阈值还得再收。

    latency_ms 是打断响应耗时，**这个数有两个口径，别混着报**：
        前端上报的 stop_ms = 从检测到人声到真的停声（含等说完+识别确认）
        duck 的时间没单独上报，约 60-80ms（3 帧判定即压低音量）
    验收写的"≤300ms 停声"实际指"用户感知到客服让路"，也就是 duck；
    用它去要求 stop_ms 是不可能的——要等用户说完才能认出文字。
    """
    tid = int(turn_id or 0)
    payload = {"reason": reason, "turn_id": tid}
    if text:
        payload["heard"] = text
    # latency_ms 为 None = 调用方（WS 路线）没带这个字段。事件的 latency 列
    # 不允许空，落 0 并显式标记"未知"——写 0 而不标记，统计里会出现假样本。
    if latency_ms is None:
        latency_used = 0.0
        payload["latency_unknown"] = True
    else:
        latency_used = latency_ms
    events.write_event(session_id, "barge_in", payload, turn_id=tid,
                       latency_ms=latency_used)
    sess = _SESSIONS.get(session_id)
    dropped = 0
    if sess:
        rest = sess.get("tts_rest") or {}
        if rest.pop(tid, None) is not None:
            dropped = 1
        # 清掉缓冲：播报声（含回声）不该进下一轮识别
        reset_audio_buffer(sess)
    return {"ok": True, "turn_id": tid, "dropped_pending": bool(dropped)}


# 插话判定用到的字表。
# 全都是语气词：单独出现说明只是随口应了一声，不是在抢话。
_FILLER_CHARS = set("嗯啊哦呃唉诶哎喔噢哈呀吧呢嘛哟")
# 单字里只有这几个带明确的"停下/否认/疑问"意图，值得立刻让客服闭嘴。
# 其余的单个字（"我""那""这"）多半是开口的第一声，后面还有话，
# 让客服先停下来等他说完反而更自然——但会被识别噪声误导，所以宁可不收。
_STRONG_SINGLE = set("停别慢不等喂哎对")


def _has_interrupt_content(text):
    """这段识别结果够不够格打断客服。

    用户提的问题：能量一超阈值就停播，咳嗽、碰麦、隔壁说话都能把客服打断。
    所以改成"先识别、再决定"——但光"识别出文字"还不够，"嗯"也是文字。

    实测踩到的坑（2026-11-04，真云识别）：
        白噪声 → 识别成 "Thank you."  → 原规则判"该打断"，误停客服
        "嗯"   → 识别成日文 "うん。"    → 侥幸被标点过滤挡住
    ASR 遇到噪声会吐训练集里的高频短语，这类幻觉必须单独挡，不能靠运气。

    规则（宁可少打断，不可乱打断）：
        去掉标点后为空            → 不打（纯噪声）
        **一个汉字都没有**         → 不打（英文幻觉；中文外呼场景下
                                    用户插话必是中文，宁可漏掉一次 "OK"）
        汉字全是语气词（嗯/啊/哦） → 不打（随口应一声）
        只有 1 个汉字              → 只有停/别/慢/不/等/喂/哎/对 才打
        >= 2 个汉字且有实义       → 打

    为什么单字这么严：实测连续通话时，环境里别人的半句话、清嗓子、
    椅子响都能被识别成一两个字。放开单字，客服就永远说不完一句话。
    """
    t = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", text or "")
    if not t:
        return False
    han = re.findall(r"[\u4e00-\u9fa5]", t)
    if not han:
        return False        # 纯英文/纯数字：中文场景下几乎只能是识别幻觉
    if all(ch in _FILLER_CHARS for ch in han):
        return False
    if len(han) == 1:
        return han[0] in _STRONG_SINGLE
    return True


def _is_self_echo(text, agent_text, ratio=0.6):
    """这段识别结果是不是**客服自己播报的回声**。

    为什么必须单独判：外放时麦克风收得到自己的声音（AEC 只压 20~30dB，
    而且只在浏览器里生效）。回声本来就是人声，谱特征挡不住；ASR 会把客服
    刚说的话原样识别出来；`_has_interrupt_content` 看到一整句中文，判"有实义"
    → **直接停播**。用户看到的就是"客服说一半突然不说了"，而且找不到原因。

    判据用字符级覆盖度而不是编辑距离：ASR 对回声的识别通常是原句的一部分，
    末尾可能因为截断缺几个字，覆盖度对这种情况稳，编辑距离会被长度差带偏。

    阈值 0.6 的取舍：宁可漏掉一次"用户复述客服的话"（极少），
    也不能把回声放过去——放过去就是停播，不可逆。
    """
    a = re.sub(r"[^\u4e00-\u9fa5]", "", text or "")
    b = re.sub(r"[^\u4e00-\u9fa5]", "", agent_text or "")
    if len(a) < 4 or len(b) < 4:
        return False           # 太短没法和播报文本比，交给 _has_interrupt_content 判
    import difflib
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(blk.size for blk in sm.get_matching_blocks())
    return matched / len(a) >= ratio


def probe_speech(session_id, pcm, agent_text=""):
    """播报期间截到的一小段疑似插话：只识别，不建轮次、不推进状态机。

    为什么不复用 handle_turn：那一整套会写 turn_start/turn_end、推进阶段、
    可能触发工具调用和回写。而这里只想问一句"这声音是不是人话"。
    答案是否定时，这一轮必须当作没发生过——不能留下任何业务痕迹。

    也刻意不写事件：用户在客服说话时清了下嗓子，不该在事件链上留一条。
    真正的打断（和误判）由前端在确认后调 /api/call/barge_in 记账。
    """
    t0 = time.time()
    if not pcm:
        return {"text": "", "has_content": False, "asr_ms": 0.0,
                "reason": "empty_audio"}
    try:
        out = voice.asr(pcm)
    except Exception as e:
        # 识别挂了不代表用户在说话。这种情况按"不打断"处理——宁可让客服说完。
        return {"text": "", "has_content": False,
                "asr_ms": round((time.time() - t0) * 1000, 1),
                "reason": "asr_error", "error": str(e)[:200]}

    text = (out.get("text") or "").strip()
    echo = _is_self_echo(text, agent_text)
    return {
        "text": text,
        # 回声一律否决，理由单独写出来——不要让它伪装成"识别到噪声"，
        # 否则以后排查会以为是 ASR 不行，其实是外放耦合。
        "has_content": False if echo else _has_interrupt_content(text),
        "reason": "echo_self" if echo else None,
        "asr_ms": round((time.time() - t0) * 1000, 1),
        "fake": bool(out.get("fake")),
        "error": out.get("error"),
    }


def end_call(session_id):
    """挂断：记一条 call_ended，返回这通电话的简单结论（是否闭环）。

    用 pop 而不是标记删除：会话状态没有保留价值，内存里留着只会让 TTL 逻辑更复杂。
    """
    sess = _SESSIONS.get(session_id)
    closed = bool(sess and sess.get("closed"))
    usage = dict((sess or {}).get("usage") or {})
    events.write_event(session_id, "call_ended",
                       {"closed": closed, "est_cost_cny": usage.get("cost_cny", 0)})
    with _STORE_LOCK:
        _SESSIONS.pop(session_id, None)
    return {"session_id": session_id, "closed": closed, "usage": usage}


def report_client_metric(session_id, turn_id, first_audio_ms, estimate=False):
    """记录浏览器实测的首字响应（T4 − T0）。

    为什么必须由前端上报：只有浏览器知道自己什么时候真的出声。
    服务端能测的只是"请求到达 → 首包就绪"，那是下界，不是用户感知。
    也不做时钟换算——前端传的是 performance.now() 相减的**时间差**，
    所以客户端时钟不准也不会变成负数（docs/04 第 5 节那条提醒就是讲这个）。
    """
    value = float(first_audio_ms)
    if not (0 < value < 60000):
        raise ValueError("first_audio_ms 超出合理范围（0-60000ms）")

    # turn_id 必须同时写进事件列：面板和排查都按列取，只放 payload 里的活
    # 会出现"每轮都是 turn 0"的假象。payload 里也留一份，因为 metrics()
    # 是按 payload 配对浏览器值与 turn_end 的，两处必须同步。
    events.write_event(session_id, "latency_report",
                       {"turn_id": int(turn_id), "first_audio_ms": round(value, 1),
                        "estimate": bool(estimate)},
                       turn_id=int(turn_id))
    return {"ok": True, "turn_id": int(turn_id), "first_audio_ms": round(value, 1)}


# ---------------------------------------------------------------------------
# HTTP 层：路由与序列化（写得笨一点，方便看不懂框架的人直接读）
# ---------------------------------------------------------------------------

class _WsBackend:
    """ws_channel 的业务后端：把协议层需要的动作接到 app 自己的函数上。

    为什么包一层而不是让 ws_channel 直接 import app：
        ws_channel 的协议逻辑要在 verify.py 里用假后端离线测试，
        一旦它 import app 就绕不开真会话库，测试就只能起真服务了。
    """

    @staticmethod
    def auth(session_id, token):
        sess, err, _code = _auth(session_id, token)
        return sess    # 失败时返回 None，协议层负责把原因告诉前端

    @staticmethod
    def attach(sess, ws):
        with _STORE_LOCK:
            sess["ws"] = ws

    @staticmethod
    def detach(sess):
        with _STORE_LOCK:
            sess.pop("ws", None)

    @staticmethod
    def ingest(sess, pcm):
        # WS 帧不带 seq：单条 TCP 有序，不存在乱序与缺口。
        # 直接用"服务端期望的下一帧序号"落账，gaps/duplicates 恒为 0——
        # 这是真实的（TCP 保证的），不是掩盖。
        st = _audio_state(sess)
        ingest_audio_chunk(sess, st["expect_seq"], pcm)

    @staticmethod
    def handle_turn(session_id, audio_streamed=True, asr_override=None):
        return handle_turn(session_id, audio_streamed=audio_streamed,
                           asr_override=asr_override)

    @staticmethod
    def take_rest(sess, turn_id):
        # 拿走之后 /api/tts/rest 就取不到了——同一份剩余段不能两条路各发一次
        return (sess.get("tts_rest") or {}).pop(turn_id, None)

    @staticmethod
    def barge(session_id, heard, latency_ms=None):
        sess = _SESSIONS.get(session_id)
        turn_id = (sess or {}).get("turn", 0)
        record_barge_in(session_id, turn_id=turn_id,
                        reason="user_speech", text=heard,
                        latency_ms=latency_ms)
        return turn_id

    @staticmethod
    def realtime_enabled():
        return voice.realtime_enabled()


_WS_BACKEND = _WsBackend()



class Handler(BaseHTTPRequestHandler):
    """所有请求的入口。路由用 if/elif 硬写，不引入路由器。"""

    server_version = "super_candy-skeleton/0.2"

    # ---- 工具方法 ----

    def _send_json(self, obj, code=200):
        """统一的 JSON 响应。中文不转义，方便直接看。"""
        # 4xx/5xx 落错误日志：接口报错是"演示时突然不能用"最常见的形态，
        # 控制台又不打请求日志，不写文件就等于没发生过。
        if code >= 400:
            _app_log.warning("%s %s -> %d %s",
                             getattr(self, "command", "-"),
                             (self.path or "").split("?")[0], code,
                             json.dumps(obj, ensure_ascii=False)[:240])
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 服务端自报耗时：和 curl 的总时间一对比，就能区分"慢在我们这儿"
        # 还是"慢在网关/网络"。11-12 线上排查 45 秒开单就是靠这个分的。
        try:
            self.send_header("X-Server-Ms",
                             str(int((time.perf_counter() - self._t0) * 1000)))
        except Exception:
            pass
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        """把静态文件原样发出去。每次读盘，改完前端刷新即生效，不用重启。"""
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._send_json({"error": "NOT_FOUND", "path": path}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        """读请求体并解析 JSON。空体返回空 dict，解析失败抛 ValueError。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_AUDIO_BYTES * 2:   # base64 会膨胀约 1.33 倍，留余量
            raise PayloadTooLarge("请求体超过 %d 字节" % (MAX_AUDIO_BYTES * 2))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _read_raw(self):
        """读原始二进制请求体（音频分片用）。空体返回 b""，超限抛 PayloadTooLarge。

        为什么不复用 _read_json：音频是二进制，解码成 JSON 只会报 BAD_JSON，
        而且 base64 会把体积撑大 33%，分片上传的意义就没了。
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_AUDIO_BYTES:
            raise PayloadTooLarge("单个分片超过 %d 字节" % MAX_AUDIO_BYTES)
        return self.rfile.read(length)

    def _is_loopback(self):
        """请求是否**真的**来自本机。仅用于 /api/sessions、/api/scene 这类运维接口。

        ⚠️ 只看 `client_address` 是不够的，这是实测踩出来的：
        线上是反向代理，后端看到的源地址就是 127.0.0.1。加了这条判断之后，
        打线上 `/api/sessions` 不带任何凭证仍然返回 200 + 全量会话列表——
        等于这道门在线上一直是开着的。

        所以改成四道闸门，全过才算本机：
          ① 托管环境（平台注入 PORT）→ 整个运维接口直接关掉，线上不需要它们；
          ② 有转发头（X-Forwarded-For / X-Real-IP / Forwarded）→ 前面有人，源地址无意义；
          ③ Host 不是本机名 → 说明是经域名/隧道访问的，可能是 DNS 重绑定；
          ④ 最后才看源地址。

        宁可线上把这几个接口关掉（少一个演示点），也不能让它对公网可写。
        """
        if os.environ.get("PORT"):
            return False
        for h in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
            if self.headers.get(h):
                return False
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        if host not in ("", "localhost", "127.0.0.1", "::1"):
            return False
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _ops_denied(self, what):
        """运维接口被拒时的统一响应。把"为什么被拒"说清楚，别让人猜。"""
        detail = "%s只允许本机访问" % what
        if os.environ.get("PORT"):
            detail += "；当前服务运行在托管环境（检测到 PORT 环境变量），这类接口已自动关闭"
        elif self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP"):
            detail += "；检测到反向代理转发头，源地址不可信，故拒绝"
        self._send_json({"error": "FORBIDDEN", "detail": detail}, 403)

    def log_message(self, fmt, *args):
        """压掉默认的逐请求日志，只保留我们自己关心的输出。"""
        pass

    def log_request(self, code="-", size="-"):
        """访问日志：方法、路径、状态码、耗时（毫秒）。

        **必须去掉查询串**：`/api/events?session_id=..&token=..` 里的 token
        一旦落盘，日志就成了泄露源。所以只记 path 部分。
        """
        t0 = getattr(self, "_t0", None)
        ms = (time.perf_counter() - t0) * 1000.0 if t0 else 0.0
        _access_log.info("%s %s -> %s %.1fms", getattr(self, "command", "-"),
                         (self.path or "").split("?")[0], code, ms)

    # ---- S2：WebSocket 升级与下行 ----

    def _handle_ws(self):
        """把这条 HTTP 连接升级成 WebSocket（/ws/call），然后常驻服务它。

        为什么不校验 Origin：页面与会话的绑定靠 token（start 消息里验），
        WS 层不重复造一道门；真正的边界是"没有 token 拿不到任何会话数据"。
        """
        key = self.headers.get("Sec-WebSocket-Key")
        if (self.headers.get("Upgrade") or "").lower() != "websocket" or not key:
            self._send_json({"error": "BAD_REQUEST",
                             "detail": "/ws/call 只接受 WebSocket 升级请求"}, 400)
            return
        self.send_response_only(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", wsproto.accept_key(key))
        self.end_headers()
        self.close_connection = True     # 连接归 WS 管，别让基类再碰它
        self._ws_lock = threading.Lock()
        self._ws_alive = True
        ws_channel.serve_loop(self, _WS_BACKEND)

    def send_ws_text(self, text):
        """下发一帧 JSON 文本。ws_channel 的会话锁保证不并发写。"""
        self.send_ws_frame(wsproto.OP_TEXT, text.encode("utf-8"))

    def send_ws_frame(self, opcode, payload):
        """下发一帧。两个线程（读循环 + TTS 推送线程）都可能写同一个 socket，
        没有这把锁就会出现两帧交错的字节流——对端解析直接报废。"""
        with self._ws_lock:
            if not self._ws_alive:
                raise ConnectionError("WS 连接已关闭")
            self.wfile.write(wsproto.encode_frame(opcode, payload))
            self.wfile.flush()

    def close_ws(self):
        """读循环退出时收尾。幂等：正常关、异常关、对端先关都会走到。"""
        self._ws_alive = False
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass

    # ---- 路由 ----

    def do_GET(self):
        self._guard(self._do_GET)

    def do_POST(self):
        self._guard(self._do_POST)

    def _guard(self, fn):
        """任何未捕获异常都要变成一条 JSON 错误，绝不把连接掐断。

        11-12 线上事故：事件库损坏 → `/api/call/start` 抛 sqlite3.DatabaseError
        → 连接被直接关闭 → 网关只回一句纯文本 "socket hang up"
        → 前端 `r.json()` 报 `Unexpected token 's'`，用户看到"服务没连上"。
        真正的异常名和原因一个字都没传出来，只能靠线上加临时接口才查到。
        所以这一层必须有：出错可以，但必须"说清楚是什么错"。
        """
        try:
            fn()
        except Exception as exc:
            traceback.print_exc()
            try:
                self._send_json({"error": "SERVER_ERROR",
                                 "detail": "%s: %s" % (type(exc).__name__,
                                                       str(exc)[:200])}, 500)
            except Exception:
                pass

    def _do_GET(self):
        self._t0 = time.perf_counter()
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/ws/call":
            # S2 双向通道。必须在普通路由之前：升级请求不走 _send_json 的响应语义。
            self._handle_ws()
            return
        if path in ("/", "/index.html"):
            self._send_file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            # 预留：将来拆出 app.js / style.css 时走这里
            name = os.path.basename(path)
            ext = os.path.splitext(name)[1].lower()
            ctype = {".js": "text/javascript", ".css": "text/css",
                     ".png": "image/png", ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
            self._send_file(os.path.join(WEB_DIR, name), ctype + "; charset=utf-8")
            return
        if path == "/api/health":
            # 健康检查不带任何业务数据，故意不加 token：部署探针要能匿名探活
            self._send_json({"ok": True, "scene": config_loader.scene_name(),
                             "gate_ms": contracts.LATENCY_GATE_MS})
            return
        if path == "/api/train/personas":
            # 训练回放的画像清单。不含 knows / traits / style：
            # 前端只需要展示用的字段，档案细节不该顺手回给浏览器。
            self._send_json({"personas": _train_personas()})
            return
        if path == "/api/evolve/runs":
            # 自进化训练记录清单：只读，不含个人信息（客户是模拟画像）
            self._send_json({"runs": _evolve_runs()})
            return
        if path == "/api/evolve/run":
            rid = (qs.get("id") or [""])[0]
            run = _evolve_run(rid)
            if run is None:
                self._send_json({"error": "UNKNOWN_RUN",
                                 "detail": "没有这次训练记录：%s" % (rid or "(空)")}, 404)
                return
            self._send_json(run)
            return
        if path == "/api/applications":
            # 全部可电话外呼档案的展示字段（演示页"分类浏览示例数据"用）。
            res = tools.list_applications()
            self._send_json(res)
            return
        if path == "/api/company/get":
            # 按单号取一条完整档案（含补全部件），口径与随机预览一致。
            _qs = parse_qs(parsed.query)
            _aid = (_qs.get("application_id") or [""])[0]
            _res = tools.get_application(_aid)
            _company = _res.get("application") or {}
            self._send_json({
                "matched_by": _res.get("matched_by") if _res.get("found") else None,
                "company": _company,
                "scene": _scene_of(_company),
            })
            return
        if path == "/api/company/random":
            # 纯预览：随机抽一条可电话外呼的档案回传，**不创建会话**。
            # 用途：测试页"随机"按钮先看清这家公司的信息，再决定用哪家与客服对话——
            # 不该因为"点了一下预览"就开一通电话、占用一个 session。
            # 回传的完整档案（含身份证号/统一社会信用代码等补全部件）即客服"知道"的信息。
            _tg = tools.random_application()
            _company = _tg.get("application") or {}
            self._send_json({
                "matched_by": _tg.get("matched_by") if _tg.get("found") else None,
                "company": _company,
                # 本次随机到的"驳回场景"：官方话术已按这条档案填好槽位，
                # 前端直接展示即可；没有话术（京麦驳回/官方无模板）时为 null。
                "scene": _scene_of(_company),
            })
            return
        if path == "/api/metrics":
            # 聚合指标（分位数、计数、成本），不含任何个人信息，保持匿名可读
            self._send_json(events.metrics())
            return
        if path == "/api/cost":
            # 当天成本快照与闸门开关状态。给看板和运维脚本用，同样不含个人信息。
            self._send_json(cost_guard.snapshot())
            return
        if path == "/api/events":
            sid = (qs.get("session_id") or [""])[0]
            token = (qs.get("token") or [""])[0]
            sess, err, code = _auth(sid, token)
            if err:
                self._send_json(err, code)
                return
            self._send_json({"session_id": sid, "events": events.list_events(sid)})
            return
        if path == "/api/sessions":
            # 会话列表会暴露别人的 session_id，只允许本机运维查看
            if not self._is_loopback():
                self._ops_denied("会话列表")
                return
            self._send_json({"sessions": events.recent_sessions(limit=20)})
            return
        if path == "/api/report":
            sid = (qs.get("session_id") or [""])[0]
            token = (qs.get("token") or [""])[0]
            sess, err, code = _auth(sid, token)
            if err:
                self._send_json(err, code)
                return
            self._send_json({
                "transcript": eval_mod.transcribe_session(sid),
                "compliance": eval_mod.compliance_report(sid),
                "score": eval_mod.score_session(sid),
            })
            return

        # 取分段的剩余音频。前端在首段播完后调用，播报不需要等它。
        # 这里最多等 3 秒：等不到就返回已有部分，用户听到的是一句被截断的话，
        # 但那比"播报卡住不动"好——后者看起来像程序挂了。
        if path == "/api/tts/rest":
            sid = (qs.get("session_id") or [""])[0]
            token = (qs.get("token") or [""])[0]
            sess, err, code = _auth(sid, token)
            if err:
                self._send_json(err, code)
                return
            try:
                tid = int((qs.get("turn_id") or ["0"])[0])
            except ValueError:
                self._send_json({"error": "BAD_QUERY",
                                 "detail": "turn_id 必须是整数"}, 400)
                return
            handle = (sess.get("tts_rest") or {}).pop(tid, None)
            self._send_json({"segments": voice.collect_rest(handle) if handle else []})
            return

        # ---- 指标看板（M5）----
        # 只读，不碰在线链路：路由里只做参数整形，聚合逻辑在 dashboard.py。
        # 单独一个页面而不是改 index.html：index.html 是 1 号的地盘，
        # 看板是 4 号的交付物，两块分开，谁改谁的不打架。
        if path in ("/dashboard", "/dashboard.html"):
            self._send_file(os.path.join(WEB_DIR, "dashboard.html"),
                            "text/html; charset=utf-8")
            return
        if path == "/api/dashboard/summary":
            days_raw = (qs.get("days") or [""])[0].strip()
            try:
                days = int(days_raw) if days_raw else None
            except ValueError:
                self._send_json({"error": "BAD_QUERY",
                                 "detail": "days 必须是整数（不传=全部历史）"}, 400)
                return
            if days is not None and days <= 0:
                days = None
            try:
                limit = int((qs.get("limit") or ["10"])[0])
            except ValueError:
                limit = 10
            # 上限 50：逐会话质检要读整通事件，放太大等于让看板把库扫一遍
            limit = max(1, min(limit, 50))
            self._send_json(dashboard_mod.summary(days=days, session_limit=limit))
            return
        if path == "/api/scenes":
            # 场景清单：谁都能看，切换才是敏感操作（见 POST /api/scene）
            self._send_json({"current": config_loader.describe(),
                             "scenes": config_loader.available_scenes()})
            return

        self._send_json({"error": "NOT_FOUND", "path": path}, 404)

    def _do_POST(self):
        self._t0 = time.perf_counter()
        path = urlparse(self.path).path

        # 音频分片走二进制体，必须在解析 JSON 之前分流，否则会被判成 BAD_JSON
        if path == "/api/asr/probe":
            self._handle_asr_probe()
            return

        if path == "/api/audio/chunk":
            self._handle_audio_chunk()
            return

        try:
            body = self._read_json()
        except PayloadTooLarge as e:
            self._send_json({"error": "PAYLOAD_TOO_LARGE", "detail": str(e)}, 413)
            return
        except Exception as e:
            self._send_json({"error": "BAD_JSON", "detail": str(e)}, 400)
            return

        if path == "/api/train/run":
            # 现场跑一场对抗对话，把逐轮轨迹回给浏览器。
            # 有真实通话在跑时拒绝：训练会独占状态机与工具层，还会把事件库切到临时目录。
            pid = str(body.get("persona_id") or "").strip()
            from scripts import adversarial_evolve as _adv
            raw = next((p for p in _adv.load_personas() if p["id"] == pid), None)
            if raw is None:
                self._send_json({"error": "UNKNOWN_PERSONA",
                                 "detail": "没有这个画像：%s" % (pid or "(空)")}, 404)
                return
            if active_session_count() > 0:
                self._send_json({"error": "CALL_IN_PROGRESS",
                                 "detail": "有通话进行中，先挂断再训练（训练会独占状态机）"}, 409)
                return
            if not _TRAIN_LOCK.acquire(blocking=False):
                self._send_json({"error": "TRAIN_BUSY",
                                 "detail": "上一场训练还没跑完"}, 409)
                return
            try:
                out = _train_run(raw, user=(body.get("user") or "llm"))
            except Exception as e:
                self._send_json({"error": "TRAIN_FAILED", "detail": str(e)}, 500)
                return
            finally:
                _TRAIN_LOCK.release()
            self._send_json(out)
            return

        if path == "/api/evolve/train":
            # 现场跑多轮反馈闭环。只写 eval/reports（运行记录），**不碰线上配置**：
            # 学到的策略要上线必须走 self_evolve.py promote <run> --yes 这一条人工命令。
            # 为什么放在子进程：训练里每轮都要用不同配置起一次跑批，同进程会命中
            # app 的场景缓存，出现“改了配置分数没动”的假结论（evolve.py 踩过）。
            try:
                rounds = int(body.get("rounds") or 3)
            except (TypeError, ValueError):
                rounds = 3
            rounds = max(1, min(8, rounds))
            if not _EVOLVE_LOCK.acquire(blocking=False):
                self._send_json({"error": "EVOLVE_BUSY",
                                 "detail": "上一轮自进化还没跑完"}, 409)
                return
            try:
                r = subprocess.run(
                    [sys.executable, os.path.join(_ROOT, "scripts", "self_evolve.py"),
                     "run", "--rounds", str(rounds)],
                    cwd=_ROOT, capture_output=True, text=True, timeout=600)
            except Exception as e:
                self._send_json({"error": "EVOLVE_FAILED", "detail": str(e)}, 500)
                return
            finally:
                _EVOLVE_LOCK.release()
            if r.returncode != 0:
                self._send_json({"error": "EVOLVE_FAILED",
                                 "detail": (r.stderr or r.stdout)[-800:]}, 500)
                return
            latest = _evolve_runs(limit=1)
            run = _evolve_run((latest[0] or {}).get("run_id") or "")
            self._send_json({"ok": True, "run": run, "log_tail": r.stdout[-1200:]})
            return

        if path == "/api/call/start":
            # body 透传外呼目标选择：{application_id} 指定复测 / {random:true} 随机生成。
            # 都不带时退回外呼名单下一条（默认生产行为）。
            # 这条是**真实外呼**入口：打给谁已经由名单决定了，所以锁目标
            # （见 start_call 里 lock_target 的说明）。脚本直调的跑批不锁。
            self._send_json(start_call(
                application_id=body.get("application_id") or None,
                random_target=bool(body.get("random")),
                lock_target=True,
            ))
            return

        if path == "/api/turn":
            self._handle_turn(body)
            return

        if path == "/api/turn/metric":
            self._handle_metric(body)
            return

        if path == "/api/call/barge_in":
            sid = body.get("session_id") or ""
            sess, err, code = _auth(sid, body.get("token"))
            if err:
                self._send_json(err, code)
                return
            try:
                tid = int(body.get("turn_id") or 0)
            except (TypeError, ValueError):
                self._send_json({"error": "BAD_JSON",
                                 "detail": "turn_id 必须是整数"}, 400)
                return
            try:
                lat = body.get("latency_ms")
                lat = float(lat) if lat is not None else None
            except (TypeError, ValueError):
                lat = None
            self._send_json(record_barge_in(
                sid, tid, body.get("reason") or "user_speech",
                body.get("text") or "", lat))
            return

        if path == "/api/audio/reset":
            sid = body.get("session_id") or ""
            sess, err, code = _auth(sid, body.get("token"))
            if err:
                self._send_json(err, code)
                return
            self._send_json({"ok": True,
                             "discarded_bytes": reset_audio_buffer(sess)})
            return

        if path == "/api/call/end":
            sid = body.get("session_id") or ""
            sess, err, code = _auth(sid, body.get("token"))
            if err:
                self._send_json(err, code)
                return
            self._send_json(end_call(sid))
            return

        # 运行时切场景（M8 的演示点）：换一套配置，下一轮回复就变。
        # 只允许本机：切场景会改变所有在线会话的话术，不能让外部随手调。
        if path == "/api/scene":
            if not self._is_loopback():
                self._ops_denied("切换场景")
                return
            name = (body.get("name") or "").strip()
            if not name:
                self._send_json({"error": "BAD_JSON",
                                 "detail": "缺少 name（场景名或配置文件路径）"}, 400)
                return
            # 先数一遍：切完之后，正在进行的这些电话下一句就会换套业务。
            # 不拦（演示时现场对比两套场景就是这个功能的用途），但要如实告知。
            active = active_session_count()
            try:
                applied = config_loader.set_scene(name)
            except FileNotFoundError as e:
                self._send_json({"error": "NOT_FOUND", "detail": str(e)}, 404)
                return
            config_loader.load(force=True)
            dashboard_mod.invalidate()
            scene_name = os.path.splitext(os.path.basename(applied))[0]
            print("[场景] 已切换到 %s（%d 通进行中的电话会受影响）" % (scene_name, active))
            self._send_json({
                "ok": True,
                "name": scene_name,          # 不返回绝对路径，见 config_loader._exposed_path
                "scene": config_loader.describe(),
                "active_sessions": active,
                # 前端可以把 warning 直接显示出来；为空表示没有正在进行的通话
                "warning": ("有 %d 通电话正在进行，它们的下一句就会用新场景的话术"
                            % active) if active else "",
            })
            return

        self._send_json({"error": "NOT_FOUND", "path": path}, 404)

    def _handle_turn(self, body):
        """一轮对话：鉴权 → 占住会话 → 处理 → 释放。"""
        sid = body.get("session_id") or ""
        sess, err, code = _auth(sid, body.get("token"))
        if err:
            self._send_json(err, code)
            return

        # 成本与速率闸门（默认关闭）。关着的时候 allow() 直接返回 True，
        # 只有 budget.guard_enabled=true 才会真的拦——演示前不动配置就等于没有这一层。
        # 返回 429 而不是 400：这是"现在不行、等会儿再来"，不是"你请求写错了"。
        guard_ok, guard_reason, guard_detail = cost_guard.allow(sid)
        if not guard_ok:
            self._send_json({"error": guard_reason, "detail": guard_detail}, 429)
            return

        # 两条音频来路，二选一：
        #   audio_streamed=True → 音频已按帧传过，从会话缓冲取（S1 之后前端走这条）
        #   audio_b64           → 整段上传（保留给脚本与旧客户端，不算废弃接口）
        streamed = bool(body.get("audio_streamed"))
        audio_bytes = b""
        if not streamed and body.get("audio_b64"):
            try:
                audio_bytes = base64.b64decode(body["audio_b64"])
            except Exception as e:
                self._send_json({"error": "BAD_AUDIO", "detail": str(e)}, 400)
                return
            if len(audio_bytes) > MAX_AUDIO_BYTES:
                self._send_json({"error": "AUDIO_TOO_LARGE",
                                 "detail": "单次音频上限 %d 字节" % MAX_AUDIO_BYTES}, 413)
                return

        if not _acquire(sess):
            # 同一会话上一轮还没处理完。宁可让这一条失败，也不要让状态机串台
            self._send_json({"error": "SESSION_BUSY",
                             "detail": "上一轮还在处理，请稍后重试"}, 409)
            return
        try:
            out = handle_turn(sid, user_text=body.get("text"), audio_bytes=audio_bytes,
                              audio_streamed=streamed,
                              audio_dropped=body.get("audio_dropped") or 0,
                              text_only=bool(body.get("text_only")))
        except Exception as e:
            # 兜底：不能让一次异常把整通电话打断
            self._send_json({"error": "TURN_FAILED", "detail": str(e)}, 500)
            return
        finally:
            _release(sess)
        self._send_json(out)

    def _handle_audio_chunk(self):
        """收一片音频（S1）：鉴权 → 校验格式/序号 → 追加到会话缓冲。

        这个接口设计成"只进不出业务"：它不推进状态机、不产生回复、不写业务事件，
        只负责把音频攒起来。这样即使前端猛推帧，也不会打乱对话流程。

        参数从 query 走而不是 JSON 体：请求体整个留给二进制 PCM，
        省掉一次 base64 编解码（体积小 33%，也少一次 CPU 拷贝）。
        """
        qs = parse_qs(urlparse(self.path).query)
        sid = (qs.get("session_id") or [""])[0]
        sess, err, code = _auth(sid, (qs.get("token") or [""])[0])
        if err:
            self._send_json(err, code)
            return

        try:
            seq = int((qs.get("seq") or ["0"])[0])
            rate = int((qs.get("rate") or [str(contracts.PCM_SAMPLE_RATE)])[0])
            if seq < 0:
                raise ValueError("seq 不能为负")
            pcm = self._read_raw()
            out = ingest_audio_chunk(sess, seq, pcm, rate)
        except PayloadTooLarge as e:
            self._send_json({"error": "AUDIO_TOO_LARGE", "detail": str(e)}, 413)
            return
        except (TypeError, ValueError) as e:
            self._send_json({"error": "BAD_CHUNK", "detail": str(e)}, 400)
            return

        self._send_json(dict(out, ok=True))

    def _handle_asr_probe(self):
        """播报期间的插话探针：识别一小段音频，回答"这是不是人话"。

        与 /api/audio/chunk 的区别：那个把音频攒进本轮缓冲，这个用完就扔。
        与 /api/turn 的区别：那个会推进状态机、写一整套事件，这个什么都不改。

        前端在客服播报时听到疑似人声，会先压低播报音量、攒下这一小段，
        等这段说完就发过来。返回 has_content=True 才真停播；False 就恢复音量。
        """
        qs = parse_qs(urlparse(self.path).query)
        sid = (qs.get("session_id") or [""])[0]
        sess, err, code = _auth(sid, (qs.get("token") or [""])[0])
        if err:
            self._send_json(err, code)
            return

        try:
            pcm = self._read_raw()
        except PayloadTooLarge as e:
            self._send_json({"error": "AUDIO_TOO_LARGE", "detail": str(e)}, 413)
            return

        self._send_json(probe_speech(sid, pcm,
                                     (qs.get("agent_text") or [""])[0]))

    def _handle_metric(self, body):
        """前端上报首字响应（T4−T0）。上报失败不影响通话，所以错误码给得很明确。"""
        sid = body.get("session_id") or ""
        sess, err, code = _auth(sid, body.get("token"))
        if err:
            self._send_json(err, code)
            return
        try:
            out = report_client_metric(sid, body.get("turn_id"),
                                       body.get("first_audio_ms"),
                                       bool(body.get("estimate")))
        except (TypeError, ValueError) as e:
            self._send_json({"error": "BAD_METRIC", "detail": str(e)}, 400)
            return
        self._send_json(out)


def main():
    """启动服务。端口优先取命令行参数，其次环境变量 PORT，默认 8000。

    绑定地址的选择规则（部署到云端时必须监听 0.0.0.0，否则外部访问不到）：
        - 显式给了 HOST 环境变量 → 用它；
        - 否则如果存在 PORT 环境变量（说明跑在托管沙箱里）→ 0.0.0.0；
        - 否则（本地自己跑）→ 127.0.0.1，只对本机开放，更安全。

    本地跑会自动打开浏览器；部署环境不打开（没有显示器，也没必要）。
    不想自动打开就设 NO_BROWSER=1。
    """
    events.init_db()
    setup_logging()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8000"))
    deployed = "PORT" in os.environ
    host = os.environ.get("HOST") or ("0.0.0.0" if deployed else "127.0.0.1")

    server = ThreadingHTTPServer((host, port), Handler)

    url = "http://127.0.0.1:%d" % port
    print("super_candy 哑链路已启动")
    print("  场景：%s" % config_loader.scene_name())
    print("  监听：%s:%d" % (host, port))
    print("  本地地址：%s" % url)
    print("  事件库：%s" % events.DB_PATH)
    print("  日志：  %s（5MB × 5 轮转）" % _LOG_PATH)
    print("  首字门槛：%d ms（口径 T4−T0，由浏览器上报）" % contracts.LATENCY_GATE_MS)
    print("  停止：Ctrl+C")

    # 延后 1.5 秒再开浏览器：服务已经在监听，页面一打开就能拿到数据，
    # 不会出现"连不上服务"的假报错。用守护线程，不影响主线程阻塞。
    if not deployed and os.environ.get("NO_BROWSER") != "1":
        def _open():
            time.sleep(1.5)
            try:
                webbrowser.open(url)
            except Exception:
                pass   # 打不开浏览器不算错误，用户手动点地址即可
        threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
