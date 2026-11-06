# 协作规范

适用于 super_candy 仓库的所有参与者。**先读完第 1、3 节再动手**，其余按需查。

一句话原则：**一人一分支，一次一件事，自检通过再提 PR，PR 审查通过才合并。**

---

## 0. 五分钟上手

```bash
git clone https://github.com/zebra611/super_candy.git
cd super_candy
git checkout -b feature/voice-streaming     # 换成你自己的分支名
```

本地跑起来验证环境没问题：

```bash
python app.py          # 浏览器打开 http://127.0.0.1:8000
```

> Windows 上如果 `python` 报错，双击 `启动.bat` 即可。
> 用 Codespaces 的队友见 `docs/10_运行与访问方式.md` 第 2 节。

**动手写代码前，先确认"提示内容"有没有更新**（README「使用规范」规矩一）：

```bash
git pull origin main
git log --oneline -5 -- docs/ contracts.py app.py config/scene.json
```

再看 `docs/02`、`docs/03`、`docs/04` 开头那行"状态：草案 / 已冻结"还是不是你依据的那一版。契约改过而你不知道，写出来的代码就是要返工的代码。

开工前先在群里或 PR 里报三句话：**改哪些文件、依据哪版契约、会不会碰禁改区**。

---

## 1. 区域划分与文件归属

一人一个区域，**别人的区域不要碰**。这是避免冲突最有效的一条。

| 区域 | 归属文件 | 负责人 | 这个区域负责什么 |
|---|---|---|---|
| **R1 实时语音与终端** | `modules/voice.py`<br>`web/index.html` | 张博涵（1 号·队长，`@zebra611`） | VAD、流式 ASR、流式 TTS、打断、录音与播报界面 |
| **R2A 对话编排** | `modules/dialogue.py` | 佟欣（2A，`@tongxin-txtxtx`） | 状态机、意图判断、槽位填充、话术选择、多轮纠错 |
| **R2B 工具与业务** | `modules/tools.py` | 潘柏亦（2B，`@Peter13856981023`） | 工具调用、Mock 接口适配、结构化回写、幂等与错误码 |
| **R3 质检与自进化** | `modules/eval.py`<br>`eval/`<br>`scripts/run_eval.py` | 刘源（3 号，`@Matt163ly`） | 全量质检、评测集、失败归因、进化闭环 |
| **R4 平台与交付** | `app.py`<br>`events.py`<br>`config_loader.py`<br>`config/scene.json`<br>`启动.bat`<br>`README.md`、`docs/` | 黄睿哲（4 号，`@yingbaoyue`） | 路由与链路串联、事件流、配置、部署、文档 |
| **契约层（禁改区）** | `contracts.py` | 全员共有 | 事件类型、状态机阶段名、工具签名 |

> **R2 是两个人一个任务**（佟欣 + 潘柏亦），所以边界必须比别的区域更硬：佟欣只决定"下一句说什么、什么时候调工具、传什么参数"，潘柏亦只负责"给了参数就按契约返回"。两人之间唯一的约定是 `contracts.py` 里的工具签名，谁都不许单方面改。

### 三类文件的处理规则

| 类型 | 文件 | 规则 |
|---|---|---|
| **独占** | 上表 R1、R2A、R2B、R3、R4 各自的文件 | 只有负责人能改。别人要改，先在群里说，由负责人改 |
| **申请** | `contracts.py`、`app.py`、`config/scene.json` | 想改必须先说明理由，得到同意后在 PR 里**单独标出**这处改动 |
| **自由** | `docs/` 下新建文件 | 谁都可以新建自己的设计稿、笔记、实验记录 |

> **`contracts.py` 只许追加，不许改语义。** 要改事件字段含义、阶段名、工具参数名，必须全员同步，因为它一改，另外四个人的代码同时失效。

---

## 2. 提交前自检（逐项打勾，缺一项不许提 PR）

在**推代码之前**，在自己分支上跑一遍：

- [ ] **1. 先同步**：`git pull origin main`，确认没有落后于主干
- [ ] **2. 看清单**：`git status` 里**不应该出现** `data/`、`__pycache__/`、`*.webm`、`*.wav`、`.env`、任何密钥文件
- [ ] **3. 能启动**：`python app.py` 正常起来，没有异常堆栈
- [ ] **4. 主流程没断**：点「开始通话」能一路走到 `WRITEBACK`，事件链里出现 `writeback`（如果你的改动不涉及这条路径，在 PR 里说明原因）
- [ ] **5. 没偷偷加依赖**：`git diff` 里没有新增 `requirements.txt` 之外的第三方包；如果有，写进 PR 说明并更新 README
- [ ] **6. 没动契约**：`git diff contracts.py` 为空；如果有内容，PR 标题必须写「含契约变更」
- [ ] **7. 时延没退化**：把改动前后的 P50 数字贴进 PR（页面右下角能看）
- [ ] **8. 清理干净**：临时脚本（`_t.bat`、`_t.log`、`probe_*.py`、`tmp_*.py`）、被替换掉的旧实现、注释掉的大段代码、调试 `print` 全部删掉。判据是"没人会再读、也没人会再跑"，不留"万一以后用得上"
- [ ] **9. 删完能跑**：清理之后重新跑一次 `python app.py` 和 `python scripts/run_eval.py`，服务和分数都正常——删错了就用 `git checkout -- <文件>` 找回来
- [ ] **10. 自检没回归**：`python scripts/verify.py`（或双击 `验证.bat`）必须是 `N/N 通过`。它断言的是「沉默不被当成说话」「文字轮不写 vad_result」这类**已经踩过坑的行为**，挂了就说明你改坏了别人的防线

---

## 3. 保证原代码完整性的硬约束（红线）

违反任何一条，PR 直接打回。

### 绝对禁止

| 禁止行为 | 后果 | 正确做法 |
|---|---|---|
| 直接 `git push origin main` | 主干被污染，别人拉下来就跑不起来 | 推自己的分支，走 PR |
| `git push --force` / `--force-with-lease` | 覆盖别人已合并的提交，**可能永久丢代码** | 冲突用 `pull` + 手工解决 |
| `git reset --hard` 后强推 | 同上，且更难恢复 | 要回退就新建一个 revert 提交 |
| 改别人区域的文件 | 两人同时改同一文件，必然冲突 | 先沟通，由负责人改 |
| 删文件、整段注释别人的代码 | 功能凭空消失，且很难查出是谁弄掉的 | 提出建议，由负责人决定 |
| 提交超过 10MB 的文件 | 仓库体积爆炸，clone 变慢 | 放网盘，README 写下载地址 |
| 提交 `.env`、token、密钥 | 泄露后要作废重发 | 写进 `.gitignore`，用环境变量 |

### 冲突时怎么办

```bash
git checkout main
git pull origin main
git checkout 你的分支
git merge main            # 或 git rebase main
# 有冲突时，VS Code 里会标红，手工改成最终想要的样子，删掉 <<<<<<< 标记
git add 冲突文件
git commit -m "merge: 解决与 main 的冲突"
```

**冲突不要自己硬吞。** 如果冲突发生在你不负责的文件里，停下来问那个区域的负责人。

---

## 4. 提交信息规范

格式：`类型: 一句话说清做了什么`

```
feat: 接入流式 ASR，首字延迟从 800ms 降到 320ms
fix: 修复打断后状态机没回退到聆听态的问题
docs: 补上评测集构造说明
refactor: 把工具分发从 if 链改成注册表
test: 加 20 条缺槽场景用例
chore: 更新 .gitignore，忽略录音文件
```

规则：

- **一个 commit 只做一件事。** 改了 8 个不相关文件还写「更新」，审查时会被退回
- 第一行不超过 50 字，细节写在空行之后
- 用中文写没关系，说清「做了什么」就行，不用写「修改了代码」

---

## 5. 提交与上传的完整步骤

```bash
# ① 确认在正确的分支上（不是 main）
git branch --show-current

# ② 同步主干
git fetch origin
git merge origin/main

# ③ 加改动
git add modules/voice.py          # 建议按文件 add，不要无脑 git add .
git status                        # 再看一眼有没有多余文件

# ④ 提交
git commit -m "feat: 接入流式 ASR"

# ⑤ 推上去（第一次带 -u，之后直接 git push）
git push -u origin feature/voice-streaming
```

**分支命名**：

```
feature/<区域>-<做什么>     例：feature/voice-asr-streaming
fix/<区域>-<修什么>         例：fix/agent-state-rollback
docs/<写什么>               例：docs/eval-set-spec
exp/<试验名>                例：exp/local-tts-latency
```

---

## 6. 提 PR

推送后去 https://github.com/zebra611/super_candy/pulls → **New pull request** → base 选 `main`，compare 选你的分支。

**标题**：和 commit 保持一致，一句话。

**正文直接复制这个模板填**：

```markdown
## 做了什么



## 为什么这么做



## 怎么验证的
- 启动命令：
- 操作路径：
- 结果：（贴关键数字，比如 P50 = xxx ms、闭环 = 成功）

## 影响范围
- [ ] 只动了我自己区域的文件
- [ ] 改了 contracts.py（如果是，说明原因：）
- [ ] 新增了第三方依赖（如果是，列出来：）
- [ ] 改了 app.py / config/scene.json

## 需要谁配合
（没有就写「无」）
```

**不要自己点合并。** 合并由仓库管理员在审查通过后执行。

---

## 7. 提交后交由 AI 审查的流程

### 7.1 你要做的

1. PR 建好后**不要合并、不要关**
2. 把 PR 链接发到对话里，例如：
   > 审查 https://github.com/zebra611/super_candy/pull/3
3. 等结论。中途不要往这个分支继续推（会让我审的和你看的不是同一份代码）

### 7.2 我会做的

1. 把分支拉到本地，读完整 diff（不只看新增行，也看被删掉的行）
2. 按下面的要点逐条过，必要时实际跑一遍
3. 给出结论 + 逐条问题清单

### 7.3 审查要点（我会按这个顺序看）

| # | 检查项 | 不合格的典型表现 |
|---|---|---|
| 1 | **契约有没有被动** | `contracts.py` 里改了一个字段名，导致另外三个人的代码同时失效 |
| 2 | **有没有越界改别人的文件** | R2 的 PR 里出现 `web/index.html` 的改动 |
| 3 | **接口签名有没有变** | `voice.asr()` 少了一个参数，`app.py` 调用处没跟着改 |
| 4 | **事件写全了没** | 新增了工具调用，但只写了 `tool_result`，没写 `tool_call` |
| 5 | **幂等还在不在** | 回写改成每次都新建工单，同一次通话重复回写产生两条 |
| 6 | **兜底有没有** | 外部接口超时直接抛异常，整通电话崩掉 |
| 7 | **时延有没有退化** | ASR 从流式改成整段等待，P50 从 320ms 涨到 1.8s |
| 8 | **注释和 TODO 有没有落空** | 文件头写着「后续待补充：接流式」，代码里其实没接，也没说明 |
| 9 | **能不能一键跑起来** | 新增了依赖但没写进 README，别人 clone 下来起不来 |
| 10 | **提交卫生** | 带进了 `data/`、录音文件、调试 print、注释掉的大段代码 |

### 7.4 三种结论

| 结论 | 含义 | 你要做什么 |
|---|---|---|
| **可以合并** | 十个要点全过 | 回一句「可以合」，由管理员点 Merge |
| **改完再合** | 有小问题，方向没错 | 我会列成「文件:行号 → 问题 → 建议」，改完在原分支再推一次，我会重看 |
| **需要先讨论** | 涉及契约、跨区域、架构取舍 | 先回一句你的想法，达成一致再动手，避免白写 |

**反馈形式**：我会直接在对话里给清单，格式固定为：

```
modules/voice.py:42  问题：asr() 多了一个 hints 参数但 app.py 没传
                     建议：要么给默认值，要么同步改 app.py:88
```

你改完不需要重新开 PR，在原分支 push 即可，PR 会自动更新。

---

## 8. 常见错误速查

| 现象 | 原因 | 解决 |
|---|---|---|
| `Updates were rejected` | 远端有你本地没有的提交 | `git pull origin main` 后再推 |
| `CONFLICT (content)` | 两人改了同一处 | 见第 3 节「冲突时怎么办」 |
| `src refspec does not match any` | 分支还没 commit | 先 `git add` + `git commit` |
| PR 里出现几十个 `data/` 文件 | `.gitignore` 没生效或之前被 add 过 | `git rm -r --cached data` 后再提交 |
| `Authentication failed` | 凭据过期 | 重新登录 GitHub（见 README） |
| 推上去后发现推错分支 | — | 不用 `reset`，直接在正确分支重推，错的那个分支删掉 |

---

## 9. 一周节奏

| 时间 | 动作 |
|---|---|
| 每天开工 | `git checkout main && git pull` |
| 每天收工 | 推自己的分支，哪怕没写完（用 `wip:` 前缀） |
| 每 2–3 天 | 提一个 PR 送审 |
| 每周一次 | 管理员合并、清理已合并分支、更新 README 进度 |
| 演示前 3 天 | **冻结新功能**，只修 bug，确保 main 一键能跑 |

---

## 10. 一句话记住

**你的分支你随便改，主干只接受审查过的合并；别人的文件不要碰，契约不要私改。**
