"""音色试听 —— 用同一句话把候选音色各合成一遍，存成本地 wav 供人耳挑选。

为什么要有这个脚本：
    音色好不好听，文字描述（"温柔""沉稳"）完全不可靠，只能听。
    每次换音色都临时写代码太浪费，所以固化成工具：改候选表就行。

**音色是分模型族的**（11-13 换成 cosyvoice-v3-flash 时踩到）：
    百炼页面上那句"所选音色须在当前模型的音色列表中"不是建议是硬约束。
    旧表里 Kai / Serena / Moon 全是 qwen3-tts 的音色，换模型后照旧表跑，
    整张表会被接口逐条拒掉——看着像"接口坏了"，其实是音色用错了族。
    所以候选表现按族分开（CANDIDATES 的 key），默认跟着配置里的模型走。

怎么用：
    python scripts/audition_tts.py                      # 用默认句子 + 当前模型族的候选表
    python scripts/audition_tts.py "自定义文本"          # 换句子
    python scripts/audition_tts.py --voices <音色名1>,<音色名2>
                                                       # 直接点名试听（从百炼音色表复制）
    python scripts/audition_tts.py --model qwen3-tts-flash
                                                       # 临时换个模型族试听，不改配置

产出：
    audition/<序号>_<音色>.mp3  直接双击播放（分段合成的几段会接成一条）
    会真的调云端接口，按字数计费（几十字，几分钱），不是免费操作。

注意：
    这里刻意不动 config/scene.json。试听到满意的音色之后，
    再手工把 voice_name 写进配置——避免"试听顺便把线上音色改了"。
    跨族换音色要同时改 tts.model 与 tts.endpoint，只改 voice_name 会被拒。
"""

import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader  # noqa: E402
from modules import voice  # noqa: E402

# 同一句话贯穿所有音色，否则没法比较。
DEFAULT_TEXT = "您好，我是京东客服中心的小采，请问是张先生本人吗？"

# 账号用的是客服外呼场景：要稳、要可信，不能太跳。
# 描述来自官方音色表，仅作参考——最终以耳朵为准。
#
# 按模型族分组：音色名只在自己那一族的模型列表里成立，跨族会被接口拒
# （"所选音色须在当前模型的音色列表中"）。键就是模型名前缀。
CANDIDATES = {
    # qwen3-tts-flash 的音色（旧模型，留着是为了能回滚试听）
    "qwen3-tts": [
        ("Kai", "中性·舒缓如SPA"),
        ("Eldric Sage", "男·睿智长者"),
        ("Maia", "女·知性温柔"),
        ("Katerina", "女·成熟有共鸣"),
        ("Ethan", "男·阳光北方口音"),
        ("Serena", "女·温柔"),
        ("Moon", "男·洒脱"),
    ],
    # cosyvoice-v3-flash 的音色。线上正在用的那个必须有，否则"当前音色"
    # 反而试听不了；其余音色从百炼音色列表复制，用 --voices 传进来即可，
    # 不用为了试听一个名字去改代码（名字写错会被接口拒，不会静默走错）。
    "cosyvoice-v3": [
        ("longyingling_v3", "女·温和共情（当前线上）"),
        ("longanhuan_v3", "女·欢脱元气（上一版）"),
    ],
}


def family_of(model):
    """模型名 → 候选表 key。与 voice.tts_request_body 的族判断保持同源。"""
    m = str(model or "").lower()
    return "cosyvoice-v3" if m.startswith("cosyvoice") else "qwen3-tts"


def pick_candidates(cfg, voices=None):
    """要试听哪几个音色。

    voices 非空时用它（命令行点名），否则取当前模型族的默认表。
    返回值统一成 [(音色名, 描述)]，调用方不用关心来源。
    """
    if voices:
        return [(v, "指定") for v in voices]
    return CANDIDATES[family_of((cfg or {}).get("model"))]


def fetch(url):
    """下载一段音频，返回 bytes。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def strip_id3(data):
    """去掉 mp3 头部的 ID3v2 标签（10 字节头 + syncsafe 长度）。

    为什么要剥：合成是按句分段的，每段都是一个独立 mp3、各带一段 ID3。
    直接首尾相接会在第二段开始处冒出「ID3」字节，播放器要么跳过要么当场停。
    只留第一段的头，后面几段剥掉，接出来就是一条正常的音频流。
    """
    if data[:3] != b"ID3" or len(data) < 10:
        return data
    h = data[6:10]
    size = ((h[0] & 0x7F) << 21) | ((h[1] & 0x7F) << 14) \
        | ((h[2] & 0x7F) << 7) | (h[3] & 0x7F)
    return data[10 + size:]


def audio_urls(got):
    """从 voice.tts() 的返回里取出要下载的音频地址，按播放顺序。

    两条路都要认：短句走 mode="url"（单个链接），常规话术走 mode="segments"
    （按句切段，每段一个链接）。只认前者的话，任何音色都会报「没有拿到
    音频链接」——11-13 试听工具就是这么坏的，看着像音色被拒，其实是没读返回。
    """
    if got.get("mode") == "browser":
        raise RuntimeError(got.get("error") or "合成失败，退回浏览器播报")
    if got.get("audio_url"):
        return [got["audio_url"]]
    segs = got.get("segments") or []
    urls = [s.get("audio_url") for s in segs if s.get("audio_url")]
    if not urls:
        raise RuntimeError("没有拿到音频链接")
    return urls


def main():
    # 手写解析，不引 argparse：参数只有三个，多一个依赖不划算
    args = sys.argv[1:]
    text, voices, model = DEFAULT_TEXT, None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        elif a == "--voices" and i + 1 < len(args):
            voices = [x.strip() for x in args[i + 1].split(",") if x.strip()]
            i += 2
        elif a == "--model" and i + 1 < len(args):
            model = args[i + 1].strip()
            i += 2
        else:
            text = a
            i += 1

    out_dir = os.path.join(ROOT, "audition")
    os.makedirs(out_dir, exist_ok=True)

    base_cfg = config_loader.tts_config()
    orig = config_loader.tts_config

    if (base_cfg.get("provider") or "") != "dashscope":
        print("当前 tts.provider 不是 dashscope，先改 config/scene.json 再来试听。")
        return 2

    if model and family_of(model) != family_of(base_cfg.get("model")):
        # 端点跟族走：配置里的 endpoint 属于原来那族，跨族试听必须让
        # tts_request_body 的族默认值接管，否则会拿旧端点打新音色。
        base_cfg = dict(base_cfg, model=model, endpoint=None)
    elif model:
        base_cfg = dict(base_cfg, model=model)

    candidates = pick_candidates(base_cfg, voices)
    fam = family_of(base_cfg.get("model"))

    print("句子：%s" % text)
    print("模型：%s（%s 族）" % (base_cfg.get("model"), fam))
    print("输出：%s" % out_dir)
    if not voices and len(candidates) <= 1:
        print("提示：%s 族的默认表只有线上这一个音色。要听别的，"
              "从百炼音色列表复制名字用 --voices 传进来。" % fam)
    print("-" * 72)

    ok, failed = [], []
    for i, (name, desc) in enumerate(candidates, 1):
        cfg = dict(base_cfg, voice_name=name)
        config_loader.tts_config = lambda c=cfg: c
        try:
            got = voice.tts(text)
            urls = audio_urls(got)
            parts = [fetch(u) for u in urls]
            # 第一段带 ID3 头，其余剥掉，接成一条完整音频
            data = parts[0] + b"".join(strip_id3(p) for p in parts[1:])
            safe = name.replace(" ", "_")
            # 内容是 mp3（CosyVoice / qwen3-tts 都给 mp3），扩展名照实写
            dest = os.path.join(out_dir, "%02d_%s.mp3" % (i, safe))
            with open(dest, "wb") as f:
                f.write(data)
            ok.append((name, desc, dest))
            print("%d) %-14s %-16s %6.0f ms  %5.0f KB  %d 段  %s"
                  % (i, name, desc, got["latency_ms"], len(data) / 1024.0,
                     len(urls), os.path.basename(dest)))
        except Exception as e:
            failed.append((name, str(e)[:90]))
            print("%d) %-14s %-16s 失败：%s" % (i, name, desc, str(e)[:90]))
    config_loader.tts_config = orig

    print("-" * 72)
    print("成功 %d 个，失败 %d 个。" % (len(ok), len(failed)))
    if failed:
        print("失败清单（多半是音色不在这个模型的列表里，换族试试）：")
        for n, e in failed:
            print("  - %s：%s" % (n, e))
    if ok:
        print()
        print("去这个目录双击试听：")
        print("  " + out_dir)
        print()
        print("选定之后写进 config/scene.json 的 tts 段：")
        print("  - 同族换音色：只改 tts.voice_name；")
        print("  - 跨族换音色：tts.model 一起改，端点交给默认值（tts.endpoint 写 null），")
        print("    单价也要改（qwen3-tts 0.08 / cosyvoice-v3 0.1 元/千字符）。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
