"""验证云 API 的 key 到底能不能用 —— 30 秒给结论，不用先改代码。

为什么单独一个脚本：
    接云服务最耗时的不是写代码，是"到底卡在哪"——是 key 错、账号没实名、
    没开通模型、还是网络到不了？这个脚本把这几件事分开判定，省掉反复试。

怎么用：
    python scripts/check_key.py

它会：
    1. 说明密钥读到了没有（先看环境变量，再看 config/secrets.json）；
    2. 发一个最小请求（几个 token，花费约 0）验证鉴权；
    3. 把结论翻译成人话：key 有效 / key 无效 / 账号未开通 / 网络不通。

判断原理（重要）：
    鉴权失败会返回 401 或 403 —— 那才是"key 有问题"。
    返回 400 且提示模型不存在，说明**key 已经通过鉴权**，只是模型名不对；
    这两件事必须分开，否则会误以为 key 错了，白折腾一晚上。

只用标准库，不装任何依赖。
"""

import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_loader  # noqa: E402

# 百炼的 OpenAI 兼容端点。旧的 dashscope.aliyuncs.com 域名仍然可用，
# 2026 年新增的 {WorkspaceId}.cn-beijing.maas.aliyuncs.com 只是性能更好。
ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

# 依次尝试几个常见模型：只要能有一个通，就说明 key 与账号没问题。
# 模型名会随版本下线，所以写成列表而不是写死一个。
MODEL_CANDIDATES = ["qwen-plus", "qwen-flash", "qwen-turbo"]


def mask(key):
    """只显示头尾，避免截图或日志里泄露完整 key。"""
    if not key:
        return "（空）"
    if len(key) <= 12:
        return key[:3] + "***"
    return key[:6] + "..." + key[-4:] + "（共 %d 字符）" % len(key)


def call(model, key):
    """发一个最小请求。返回 (HTTP 状态码, 响应 dict 或原文)。"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "只回复两个字：通了"}],
        "max_tokens": 8,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:400]}


def main():
    print("=" * 68)
    print("第 1 步：找到密钥了吗")
    print("=" * 68)
    key = config_loader.secret("dashscope_api_key")
    env_val = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("SC_DASHSCOPE_API_KEY")

    print("  secrets.json 路径：%s" % config_loader.SECRETS_PATH)
    print("  文件存在：%s" % ("是" if os.path.exists(config_loader.SECRETS_PATH) else "否"))
    print("  环境变量 DASHSCOPE_API_KEY：%s" % ("已设置" if env_val else "未设置"))
    print("  最终读到的 key：%s" % mask(key))

    if not key or key.startswith("sk-在这里填") or "填" in key:
        print()
        print("  结论：还没配密钥。做两件事就行——")
        print("    1) 把 config/secrets.example.json 复制成 config/secrets.json")
        print("    2) 把里面那行改成你的 key（sk- 开头）")
        print("  详细开号步骤见 docs/15_开通云ASR.md")
        return 2

    print()
    print("=" * 68)
    print("第 2 步：拿这个 key 发一个最小请求（花费约 0 元）")
    print("=" * 68)

    last_code, last_body, worked = None, None, None
    for model in MODEL_CANDIDATES:
        print("  试模型 %s ..." % model, end=" ", flush=True)
        code, body = call(model, key)
        last_code, last_body = code, body
        if code == 200:
            worked = model
            print("通")
            break
        print("返回 %s" % code)
        # 401/403 是鉴权问题，换模型也没用，直接停
        if code in (401, 403):
            break

    print()
    print("=" * 68)
    print("第 3 步：结论")
    print("=" * 68)

    if worked:
        print("  ✅ key 有效，模型 %s 可用。" % worked)
        print("     接下来：把 config/scene.json 里 asr.provider 改成 \"dashscope\"，")
        print("     语音识别就会走云端（改完重启服务生效）。")
        return 0

    if last_code in (401, 403):
        print("  ❌ key 无效或没有权限（HTTP %d）。" % last_code)
        print("     常见原因：")
        print("       1) key 复制时多了空格或换行；")
        print("       2) key 属于另一个账号，或已被删除重建（重建后旧的立即失效）；")
        print("       3) 用的是国际站 key 却在调中国站端点（或反过来）。")
        print("     到 https://bailian.console.aliyun.com → 头像 → API-KEY 管理 重新复制。")
        print("     服务端原话：%s" % json.dumps(last_body, ensure_ascii=False)[:300])
        return 1

    if last_code == 400:
        print("  ⚠️ key 本身能用（鉴权已通过），但请求参数被拒（HTTP 400）。")
        print("     说明账号与 key 没问题，通常是模型名不在这个账号的可用列表里。")
        print("     到控制台「模型广场」看一眼实际可用的模型名，再改 MODEL_CANDIDATES。")
        print("     服务端原话：%s" % json.dumps(last_body, ensure_ascii=False)[:300])
        return 0

    if last_code == 429:
        print("  ⚠️ 请求太频繁或额度用尽（HTTP 429）。key 是好的，稍后再试或去控制台看额度。")
        return 0

    print("  ❌ 没得到明确结论（HTTP %s）。" % last_code)
    print("     服务端原话：%s" % json.dumps(last_body, ensure_ascii=False)[:400])
    print("     如果是超时或连接失败，检查网络/代理，或换手机热点再试。")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as e:
        print()
        print("  ❌ 连不上 dashscope.aliyuncs.com：%s" % e)
        print("     这不是 key 的问题，是网络到不了。校园网/代理常见，换热点试试。")
        sys.exit(1)
