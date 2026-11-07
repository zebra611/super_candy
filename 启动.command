#!/bin/bash
# ---------------------------------------------------------------------------
# super_candy · macOS 一键启动（对应 Windows 的 启动.bat）
#
# 用法：在 Finder 里双击本文件即可。第一次若提示"无法验证开发者"，
#       右键 → 打开；或先在"终端"里执行一次： chmod +x 启动.command
#
# 启动后浏览器会自动打开 http://127.0.0.1:8000 ，点「开始通话」即可。
# 关闭这个终端窗口（或按 Ctrl+C）就是停止服务。
# ---------------------------------------------------------------------------

cd "$(dirname "$0")" || exit 1

RES="$(bash ./_pycheck.sh)"
PY="${RES#FOUND=}"

if [ "$PY" = "NONE" ] || [ -z "$PY" ]; then
  echo ""
  echo "  [错误] 没找到可用的 Python 3（需要 3.9 以上）。"
  echo ""
  echo "  安装方式（任选其一）："
  echo "    1) 官网下载：https://www.python.org/downloads/"
  echo "    2) 终端执行：xcode-select --install"
  echo "    3) Homebrew：brew install python"
  echo ""
  read -n 1 -s -r -p "  按任意键关闭..."
  exit 1
fi

echo ""
echo "  使用 Python：$PY"
echo "  启动服务：http://127.0.0.1:8000 （浏览器会自动打开）"
echo "  关闭这个终端窗口即可停止服务。"
echo ""

export PYTHONUNBUFFERED=1
exec "$PY" app.py
