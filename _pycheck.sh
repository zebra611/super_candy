#!/bin/bash
# ---------------------------------------------------------------------------
# 在 macOS 上找一个"真能跑本项目"的 Python 3（对应 Windows 的 _pycheck.bat）。
#
# 为什么单独一个文件：三个入口（启动 / 启动全部 / 验证）共用同一套探测逻辑，
# "这台机器能不能跑"只有一处定义；改一处，三处同步。
#
# 契约：向 stdout 只打印一行，`FOUND=<命令或路径>` 或 `FOUND=NONE`。
#
# 为什么不是只看 `command -v python3`：有的机器 python3 指向 Xcode 命令行工具
# 的残缺环境，或版本过低（本项目要求 3.9+）。唯一可靠的判据是真跑一次导入 +
# 版本判断，所以每个候选都执行一遍。
# ---------------------------------------------------------------------------

for c in python3 python3.13 python3.12 python3.11 python3.10 python; do
  if command -v "$c" >/dev/null 2>&1 \
     && "$c" -c "import sys,json,socket,select,unicodedata; sys.exit(0 if sys.version_info>=(3,9) else 1)" >/dev/null 2>&1; then
    echo "FOUND=$c"
    exit 0
  fi
done

# 常见绝对路径兜底（Intel 与 Apple Silicon 的 Homebrew 位置不同）
for p in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$p" ] \
     && "$p" -c "import sys,json,socket,select,unicodedata; sys.exit(0 if sys.version_info>=(3,9) else 1)" >/dev/null 2>&1; then
    echo "FOUND=$p"
    exit 0
  fi
done

echo "FOUND=NONE"
