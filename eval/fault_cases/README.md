# 故障注入用例（不进通用对抗跑批）

本目录的用例**必须先注入上游故障**才有意义，不能用
`python scripts/adversarial_run.py`（默认合并 `eval/adversarial*.json`）直接跑。

## 为什么单独放

| 文件 | 驱动脚本 | 故障 |
|---|---|---|
| `p4_tool_error.json` | `scripts/test_p4_timeout.py` | `query_customer` / `query_application` 返回 `{error: TIMEOUT|UPSTREAM, found: false}` |

P4 事故只在上游掉线时发生。通用跑批没有故障注入能力，直接跑这三条
会看到 **0/3 全红**——那不是系统缺陷，是**没注入故障**：系统正常查到了
客户，自然不会走兜底收尾，而用例期望的正是兜底收尾。

11-06 实测过这个坑：该文件原名 `eval/adversarial_p4_tool_error.json`，
被 `adversarial_run.py` 的默认 glob 合并进全量对抗集，污染了默认跑批结果。
因此移出 `adversarial*.json` 命名空间，改由专用脚本驱动。

## 正确跑法

```bash
python scripts/test_p4_timeout.py     # 基线 3/3 + 变异（关掉 _lookup_failed 后必须报红）
```

`verify.py` 证据 25 已把这个脚本挂进总闸门，防回归。
