#!/bin/bash
# ---------------------------------------------------------------------------
# super_candy · macOS 自检（对应 Windows 的 验证.bat）
#
# 双击运行，跑 scripts/verify.py。结束前会停住，方便你看完整输出。
# 退出码 0 = 全过；非 0 = 有断言失败，以上面打印的原始输出为准。
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
echo "  Python：$PY"
echo "  运行 scripts/verify.py ..."
echo ""

export PYTHONUNBUFFERED=1
"$PY" scripts/verify.py
RC=$?

echo ""
if [ "$RC" -eq 0 ]; then
  echo "  全部通过。请读上面的原始输出，不要只看这一行结论。"
else
  echo "  有断言失败——以上面打印的内容为准，不要信文档。"
fi
echo ""
read -n 1 -s -r -p "  按任意键关闭..."
exit "$RC"
