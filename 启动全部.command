#!/bin/bash
# ---------------------------------------------------------------------------
# super_candy · macOS 一键启动全部（对应 Windows 的 启动全部.bat）
#
# 同时起两个进程：
#   [1] Mock 业务服务  http://127.0.0.1:8100/mock/health
#   [2] 通话服务       http://127.0.0.1:8000
#
# 与 Windows 版不同：这里把 Mock 放在本窗口的后台跑，主服务在前台。
# 关闭窗口 / Ctrl+C 时用 trap 把 Mock 一起收掉，避免留下孤儿进程。
# ---------------------------------------------------------------------------

cd "$(dirname "$0")" || exit 1

RES="$(bash ./_pycheck.sh)"
PY="${RES#FOUND=}"

if [ "$PY" = "NONE" ] || [ -z "$PY" ]; then
  echo ""
  echo "  [错误] 没找到可用的 Python 3（需要 3.9 以上）。"
  echo "  安装：https://www.python.org/downloads/  或  xcode-select --install"
  echo ""
  read -n 1 -s -r -p "  按任意键关闭..."
  exit 1
fi

echo ""
echo "  使用 Python：$PY"
echo "  即将启动："
echo "    [1] Mock 业务服务  http://127.0.0.1:8100/mock/health"
echo "    [2] 通话服务       http://127.0.0.1:8000"
echo "                       http://127.0.0.1:8000/dashboard （指标看板）"
echo ""
echo "  关闭这个窗口 / Ctrl+C 会同时停止两个服务。"
echo ""

export PYTHONUNBUFFERED=1
"$PY" mock_business/service.py &
MOCK_PID=$!
# 无论正常退出还是被 Ctrl+C / 关窗，都把后台 Mock 一并收掉
trap 'kill "$MOCK_PID" 2>/dev/null' EXIT INT TERM

# 给 Mock 一点启动时间，否则主服务可能报"业务接口不通"
sleep 2

"$PY" app.py
