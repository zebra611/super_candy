# eval/ 评测与质检工具链（R3）

> 负责人：刘源（3 号）。本目录与 `modules/eval.py`、`scripts/run_eval.py` 同属 R3。
> 本文是使用手册：跑什么、怎么读、坏了怎么办。口径定义见 `docs/24_指标口径定义.md`。

---

## 1. 这套工具解决什么

一句话：**把"系统到底有没有变好"变成一条命令、一组可复现的数字。**

三个层次，各管一段，互不重复实现判定：

| 层次 | 入口 | 回答的问题 |
|---|---|---|
| 单点 | `scripts/run_eval.py` | 基线 30 条过没过、闭环该闭的闭上没有 |
| 对抗 | `scripts/adversarial_run.py` | 边界输入扛不扛得住、多轮结论稳不稳 |
| 汇总 | `scripts/eval_suite.py` | 上面两件事合成一份报告，一个退出码 |
| 归档 | `scripts/daily_run.py` | 每天跑一次，留下趋势曲线 |

判定的唯一来源是 `scripts/run_eval.py::judge()`（接口 6 冻结）。上面四个脚本
**都不复制判定逻辑**——两套判定就是两个分数，评审一问就露馅。

---

## 2. 文件清单

| 文件 | 类型 | 职责 |
|---|---|---|
| `scripts/run_eval.py` | 改（R3 原有） | 基线跑批；闭环双指标；启动断言事件库在临时目录 |
| `scripts/bench_voice.py` | 改（R3 原有） | 语音时延基准；分位用线性插值；补样本数/超时条数 |
| `scripts/adversarial_run.py` | 新增 | 对抗集校验 + 多轮执行 + 稳定性统计 + 报告 |
| `scripts/eval_suite.py` | 新增 | 一键汇总：基线 + 对抗 + 回写合规，一份报告 |
| `scripts/daily_run.py` | 新增 | 每日归档：JSON 快照 + history.csv + 失败日志 |
| `scripts/test_adversarial.py` | 新增 | 对抗跑批器单测（27 条） |
| `scripts/test_eval_suite.py` | 新增 | 套件/归档单测（16 条） |
| `eval/adversarial_cases.json` | 新增 | 10 条对抗用例（街道 3 / 车载 3 / 多人 2 / 语义 2） |
| `eval/reports/` | 新增 | 报告产物（md 给人看，json 给机器读） |
| `eval/runs/` | 新增（运行后生成） | 每日快照与 `history.csv` |
| `docs/24_指标口径定义.md` | 新增 | 闭环双指标、回写合规率的分母口径 |

---

## 3. 三条常用命令

```bash
# 只想知道基线过没过（最快，约 3 秒）
python3 scripts/run_eval.py

# 想知道"这次改动到底有没有变好"（基线 + 对抗，约 15 秒）
python3 scripts/eval_suite.py --rounds 3 --fail-under 0.6

# 每天收工前归档一次
python3 scripts/daily_run.py
```

跑测试（不联网、不花钱）：

```bash
python3 scripts/test_adversarial.py
python3 scripts/test_eval_suite.py
```

---

## 4. 退出码约定（CI 靠它卡口）

| 码 | 含义 | 谁会产生 |
|---|---|---|
| `0` | 正常且达标 | 全部脚本 |
| `1` | 低于 `--fail-under` 阈值 / 归档失败 | `eval_suite.py`、`daily_run.py` |
| `2` | 输入不合法（用例校验失败、参数越界、文件不存在） | 全部带参数的脚本 |
| `3` | 执行期出现用例异常（被隔离并计入统计，但不算通过） | `eval_suite.py`、`adversarial_run.py` |

**为什么区分 3 和 1**：3 是"有用例把系统搞崩了"，属于能力缺陷，要修代码；
1 是"跑通了但分数不够"，属于没达标，要改话术或指标。两者的后续动作不同。

---

## 5. 每日归档与定时

`daily_run.py` 产出：

```
eval/runs/2026-11-06.json        当天第一次
eval/runs/2026-11-06-2.json      当天第二次（不覆盖）
eval/runs/history.csv            表头 + 每天一行，可直接画趋势
eval/runs/error.log              只在失败时出现，含堆栈
```

**方案 A：macOS launchd（推荐，开机自动）**

```xml
<!-- ~/Library/LaunchAgents/com.supercandy.eval.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.supercandy.eval</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/绝对路径/super_candy/scripts/daily_run.py</string>
  </array>
  <key>WorkingDirectory</key><string>/绝对路径/super_candy</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>22</integer>
    <key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>/tmp/sc_eval.out</string>
  <key>StandardErrorPath</key><string>/tmp/sc_eval.err</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.supercandy.eval.plist
launchctl list | grep supercandy        # 确认已加载
```

改为 `launchctl unload …` 即停用。

**方案 B：手动 / 钩子**

每天收工跑一次；或写进 `.githooks/pre-push`，推代码前自动留一份快照。
不需要常驻进程，适合演示前临时加密度。

**两者选一个**，不要同时装：同时跑会一天产生两份快照，趋势图会出现台阶。

---

## 6. 怎么扩展

**加对抗用例**：编辑 `eval/adversarial_cases.json`，字段与 `seed_cases.json` 同源。
保存后先跑 `python3 scripts/adversarial_run.py --rounds 1`，校验层会一次性列出
所有格式问题（缺字段、id 重复、`expect` 拼错、阶段名非法）。**不要**先跑全量——
坏数据跑出来的半截结果最容易被当成"跑完了"。

**加指标**：在 `eval_suite.collect_*` 里加计算函数，返回结构化字段；在 `render`
里加一行表格。不要把指标塞进 `note` 字符串——下游没法统计。

**改阈值**：改 CI 里的 `--fail-under`，不要改脚本默认值。默认 `None` 表示不卡口，
让手动跑批永远返回 0，避免"跑一下就红"。

---

## 7. 已知边界（别把这些当结论）

1. **文字驱动**：对抗集的噪声类只验证"听不清时的文本"，不是真实声学噪声；
   真要证噪声鲁棒性，需赛场三类音源 + 真麦克风。
2. **基线只有 30 条**：通过率 100% 已到天花板，没有信息量；看闭环率与对抗稳定率。
3. **样本量**：对抗 10 条 × 3 轮 = 30 次执行，够看稳定性，不够做统计显著性。
4. **`consent="unknown"` 是死值**：`judge()` 只判 `True/False`，校验层放行但判定不生效；
   要支持必须走 PR 改 `judge()`，不能在本工具里绕。
5. **未做并发与超时**：文本链路无外网调用，风险低；接入真语音后必须补超时，
   否则一次挂起会拖死 CI。
