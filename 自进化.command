#!/bin/bash
# ---------------------------------------------------------------------------
# super_candy · macOS 一键自进化（对应 Windows 的 自进化.bat）
#
# 双击运行：1 体检 → 2 读基线 → 3 提案 → 4 闸门 → 5 上架提示。
# 不会自动改线上话术；结束前停住，方便看完整输出。
# 去掉下面的 --llm 就不调用大模型起草，改为手工填候选话术。
# 首次双击若被文本编辑器打开，先在终端执行一次：chmod +x 自进化.command
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
echo "  运行 scripts/auto_evolve.py --llm ..."
echo ""

export PYTHONUNBUFFERED=1
"$PY" scripts/auto_evolve.py --llm "$@"
RC=$?

echo ""
if [ "$RC" -eq 0 ]; then
  echo "  跑完了。请读上面的原始输出，不要只看这一行结论。"
else
  echo "  中途停下——以上面打印的原因为准，不要信文档。"
fi
echo ""
read -n 1 -s -r -p "  按任意键关闭..."
exit "$RC"
