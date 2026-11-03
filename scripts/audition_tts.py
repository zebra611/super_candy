"""音色试听 —— 用同一句话把候选音色各合成一遍，存成本地 wav 供人耳挑选。

为什么要有这个脚本：
    音色好不好听，文字描述（"温柔""沉稳"）完全不可靠，只能听。
    每次换音色都临时写代码太浪费，所以固化成工具：改 CANDIDATES 就行。

怎么用：
    python scripts/audition_tts.py                # 用默认句子与候选表
    python scripts/audition_tts.py "自定义文本"    # 换句子

产出：
    audition/<序号>_<音色>.wav  直接双击播放
    会真的调云端接口，按字数计费（几十字，几分钱），不是免费操作。

注意：
    这里刻意不动 config/scene.json。试听到满意的音色之后，
    再手工把 voice_name 写进配置——避免"试听顺便把线上音色改了"。
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
CANDIDATES = [
    ("Kai", "中性·舒缓如SPA"),
    ("Eldric Sage", "男·睿智长者"),
    ("Maia", "女·知性温柔"),
    ("Katerina", "女·成熟有共鸣"),
    ("Ethan", "男·阳光北方口音"),
    ("Serena", "女·温柔"),
    ("Moon", "男·洒脱"),
]


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TEXT
    out_dir = os.path.join(ROOT, "audition")
    os.makedirs(out_dir, exist_ok=True)

    base_cfg = config_loader.tts_config()
    orig = config_loader.tts_config

    if (base_cfg.get("provider") or "") != "dashscope":
        print("当前 tts.provider 不是 dashscope，先改 config/scene.json 再来试听。")
        return 2

    print("句子：%s" % text)
    print("输出：%s" % out_dir)
    print("-" * 72)

    ok, failed = [], []
    for i, (name, desc) in enumerate(CANDIDATES, 1):
        cfg = dict(base_cfg, voice_name=name)
        config_loader.tts_config = lambda c=cfg: c
        try:
            got = voice.tts(text)
            if got.get("mode") != "url" or not got.get("audio_url"):
                raise RuntimeError(got.get("error") or "没有拿到音频链接")
            safe = name.replace(" ", "_")
            dest = os.path.join(out_dir, "%02d_%s.wav" % (i, safe))
            size = fetch(got["audio_url"], dest)
            ok.append((name, desc, dest))
            print("%d) %-14s %-16s %6.0f ms  %5.0f KB  %s"
                  % (i, name, desc, got["latency_ms"], size / 1024.0,
                     os.path.basename(dest)))
        except Exception as e:
            failed.append((name, str(e)[:90]))
            print("%d) %-14s %-16s 失败：%s" % (i, name, desc, str(e)[:90]))
    config_loader.tts_config = orig

    print("-" * 72)
    print("成功 %d 个，失败 %d 个。" % (len(ok), len(failed)))
    if failed:
        print("失败清单（音色可能不被当前模型支持）：")
        for n, e in failed:
            print("  - %s：%s" % (n, e))
    if ok:
        print()
        print("去这个目录双击试听：")
        print("  " + out_dir)
        print()
        print("选定之后，把音色名写进 config/scene.json 的 tts.voice_name。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
