"""下载 Silero VAD 的 ONNX 模型（2.3MB）到 data/models/。

为什么会需要单独一个下载脚本：
    模型不进仓库（*.onnx 在 .gitignore，2.3MB 不该塞进 git 历史），
    但它又是 Z6 主引擎的必要条件。没有模型时 vad_gate 会自动降级到
    纯标准库谱特征门——能跑、但召回略低（见 scripts/bench_vad.py 的对比表）。
    所以这里给一条"想开满就用"的路，而不是把它设成所有人的硬依赖。

怎么用：
    python scripts/fetch_silero_vad.py                 # 下到 data/models/
    python scripts/fetch_silero_vad.py --check         # 只校验已有文件
    python scripts/fetch_silero_vad.py --mirror URL    # 换镜像（公司网常拦 github raw）

校验：
    sha256 + 字节数双重校验。只校验"能下载"不够——公司网关返回一个
    HTML 错误页也存在本地文件，onnxruntime 会报一个看不懂的 INVALID_PROTOBUF，
    所以这里先按哈希拦一次，报错话说清楚。

装依赖：
    pip install -r requirements-vad.txt
"""

import argparse
import hashlib
import os
import shutil
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modules import silero_vad  # noqa: E402

DEFAULT_URL = ("https://github.com/snakers4/silero-vad/raw/master/"
               "src/silero_vad/data/silero_vad.onnx")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(path=None):
    """校验文件存不存在、大小与哈希对不对。返回 (ok, 说明)。"""
    path = path or silero_vad.model_path()
    if not os.path.isfile(path):
        return False, "文件不存在：%s" % path
    size = os.path.getsize(path)
    if size != silero_vad.EXPECTED_BYTES:
        return False, "大小不符：%d ≠ %d（多半是下到了错误页）" % (
            size, silero_vad.EXPECTED_BYTES)
    digest = sha256_of(path)
    if digest != silero_vad.EXPECTED_SHA256:
        return False, "sha256 不符：%s" % digest[:16]
    return True, "OK（%d 字节，sha256 %s…）" % (size, digest[:16])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不下载")
    ap.add_argument("--mirror", help="换下载地址（默认 GitHub raw）")
    ap.add_argument("--out", default=silero_vad.model_path())
    args = ap.parse_args()

    if args.check:
        ok, msg = check(args.out)
        print(("  [OK] " if ok else "  [FAIL] ") + msg)
        return 0 if ok else 1

    ok, msg = check(args.out)
    if ok:
        print("  [OK] 已存在且校验通过：%s" % msg)
        return 0

    url = args.mirror or DEFAULT_URL
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    tmp = args.out + ".part"
    print("下载中：%s" % url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
    except Exception as exc:
        print("  [FAIL] 下载失败：%s" % str(exc)[:200])
        print("  换镜像：python scripts/fetch_silero_vad.py --mirror <URL>")
        return 1

    ok, msg = check(tmp)
    if not ok:
        os.remove(tmp)
        print("  [FAIL] 下载的文件没通过校验：%s" % msg)
        return 1
    os.replace(tmp, args.out)
    print("  [OK] 已保存到 %s —— %s" % (args.out, msg))
    print("      装上 onnxruntime 后 vad_gate 会自动切到 Silero：")
    print("      pip install -r requirements-vad.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
