"""下载说话人嵌入模型（WeSpeaker 中文 CN-Celeb ResNet34，约 26MB）。

为什么不进仓库：*.onnx 已在 .gitignore。模型是"可选增强"，
和 silero_vad.onnx 同一套策略——缺了主链路照跑，只是说话人门不生效。

为什么必须是中文模型：说话人嵌入对语种/口音敏感。英文 VoxCeleb 模型在中文上
可用但掉点明显；CN-Celeb 训练的中文模型在本项目语料上实测更好（见 docs/33）。

用法：
    python scripts/fetch_speaker_model.py
    python scripts/fetch_speaker_model.py --check     # 只校验已有文件
"""

import argparse
import hashlib
import os
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(_ROOT, "data", "models", "wespeaker_zh_cnceleb_resnet34.onnx")
URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
       "speaker-recongition-models/wespeaker_zh_cnceleb_resnet34.onnx")
SHA256 = "f86cd6c509f331f0e20b07bd48d1b2eb7de54202643401c4e84695ac861a0e5a"
SIZE = 26534363


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check():
    if not os.path.exists(DEST):
        print("没有模型：%s" % DEST)
        return False
    size = os.path.getsize(DEST)
    got = _sha(DEST)
    ok = (size == SIZE and got == SHA256)
    print("大小：%d（期望 %d）" % (size, SIZE))
    print("sha256：%s" % got)
    print("校验：%s" % ("通过" if ok else "**不通过**（文件截断或版本不对，删掉重下）"))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if args.check:
        return 0 if _check() else 1

    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    if os.path.exists(DEST) and _check():
        print("\n已存在且校验通过，不用重下。")
        return 0

    print("下载：%s" % URL)
    tmp = DEST + ".part"
    # 断点续传：GitHub release 支持 Range，网络差的时候不至于每次从头来。
    # 上一次就踩过——直接下 26MB 超时，拿到一个能读却解析不了的半截 ONNX，
    # 报错是 "Protobuf parsing failed"，很容易误以为是模型不兼容。
    pos = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    req = urllib.request.Request(URL)
    if pos:
        req.add_header("Range", "bytes=%d-" % pos)
        print("  从 %d 字节处续传" % pos)
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "ab") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, DEST)

    if not _check():
        print("\n下载完成但校验失败，请重跑一次（会自动续传）。")
        return 1
    print("\n完成：%s" % os.path.relpath(DEST, _ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
