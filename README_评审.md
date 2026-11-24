# super_candy 语音外呼数字员工 · 评审说明

赛题：**"智语外呼"——语音外呼客服数字员工实时语音交互与自主进化**（京东科技）

本项目是一套可真实打电话的语音外呼客服：**浏览器采集 PCM → WebSocket 推流 →
云端流式识别 → 状态机 / 模型决策 → 分段合成 → 边播边取下一段**，并带打断、
事件流、质检评分、自进化与数据看板。

---

## 一、评委怎么体验（推荐，不需要任何 key）

**线上地址**：https://voice-agent-skeleton.app.workbuddy.host/?sharecode=ACYOJRLVUP

点「语音通话」即可完整走一通：麦克风授权 → 客服开场 → 报尾号核身 → 讲驳回原因 →
补材料清单 → 操作步骤 → 收尾。支持随时插话打断。

> 线上服务的 ASR / TTS 走我们自己的 DashScope 账号，评委**不需要任何密钥**。
> 这也是"语音系统完整"的验证入口：不是录音，不是文字模拟，是真识别、真合成。

数据看板（会话数、闭环率、首字 P50/P95、事件分布）：
https://voice-agent-skeleton.app.workbuddy.host/dashboard

演示数据版（种子库，只读，样本更多）：`/dashboard?db=demo`

---

## 二、本地跑起来（30 秒）

```bash
# 1) 依赖：主服务是纯标准库，不需要装第三方包
python --version          # ≥ 3.9（实测 3.11 / 3.13）
pip install -r requirements.txt          # 可选，空依赖

# 2) 启动
python app.py 8000
# 浏览器打开 http://127.0.0.1:8000
```

### 2.1 语音 key（包内已预置，开箱可用）

识别与合成调的是阿里云百炼（DashScope），**一个 key 同时管 ASR 和 TTS**。
本交付包的 `config/secrets.json` 里**已预置一把可用 key**，解压后直接启动服务
即可走云端真实识别与合成，**不需要申请人任何账号或密钥**。

启动前可用一条命令确认密钥随包到位：

```bash
python scripts/check_key.py        # 看到「key 有效」即可
```

> 注意：既然 key 随包发布，任何拿到这份包的人都能用它调用该阿里云账号的额度。
> 若要改成自己的 key，按下面第 1–3 步覆盖即可；环境变量优先级高于文件。

<details>
<summary>想换成自己的 key？展开看三步（约 5 分钟）</summary>

**第 1 步：拿 key（只有实名不能省）**

1. 打开 https://bailian.console.aliyun.com ，首次进入点「开通服务」（免费，不开通建不了 key）；
2. 控制台右上角头像 →「实名认证」，个人认证支付宝扫码即可（**没实名的话 key 建得出来但每次调用都 401**）；
3. 头像 →「API-KEY 管理」→「创建新的 API Key」，得到一串 `sk-` 开头的字符串。
   它只显示一次，关掉就看不到，直接粘进下一步。

> 要的是**百炼的 API Key（`sk-` 开头）**，不是账号 AccessKey（`LTAI` 开头）。
> 后者是账号级最高权限，本项目一次都用不到，`scripts/check_key.py` 会拦住这种填法。

**第 2 步：放进项目（二选一，环境变量优先）**

方式 A —— 写文件（本地推荐，一次配好不用每次开终端）：

```bat
:: Windows（在项目根目录，cmd 或 PowerShell 都行）
copy config\secrets.example.json config\secrets.json
```

```bash
# macOS / Linux
cp config/secrets.example.json config/secrets.json
```

然后用任意编辑器打开 `config/secrets.json`，只改这一行：

```json
"dashscope_api_key": "sk-你的真实key"
```

方式 B —— 环境变量（临时试一次、或不想落文件）：

```bat
:: Windows cmd（只影响当前这个窗口）
set DASHSCOPE_API_KEY=sk-xxxx
```
```powershell
# Windows PowerShell（写法不一样，别用 cmd 的 set）
$env:DASHSCOPE_API_KEY = "sk-xxxx"
```
```bash
# macOS / Linux
export DASHSCOPE_API_KEY=sk-xxxx
```

> 想长期生效：Windows 用 `setx DASHSCOPE_API_KEY sk-xxxx`（新开窗口才读到），
> macOS/Linux 把 `export` 那行写进 `~/.zshrc` 或 `~/.bashrc`。
> 环境变量和 `secrets.json` 同时存在时**环境变量优先**——排查时记得两边都看一眼。

**第 3 步：验一下（别跳过，30 秒分清是哪种问题）**

```bash
python scripts/check_key.py
```

它只用标准库，会直接给结论：key 有效 / key 无效 / 账号没实名 / 网络不通 /
模型名不对。看到「key 有效」再启动服务。

</details>

**如果人为移除 key 会怎样**：ASR 降级成占位识别（返回固定句）、TTS 降级成
浏览器朗读，**业务流程、状态机、事件流、评测、看板全部照常**——这条降级路径
始终保留，只是听不到真人音色、也没法自由说话。

可选增强（离线 VAD，端点更稳）：

```bash
pip install -r requirements-vad.txt      # onnxruntime + numpy
python scripts/fetch_silero_vad.py
```

---

## 三、API（评委可以自己调）

服务是单端口 HTTP + WebSocket，所有接口返回 JSON。完整清单见
`docs/14_接口与代码地图.md`，下面是主链路。

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/call/start` | 开一通电话，返回 `session_id` / `read_token` / 开场白与其音频 |
| POST | `/api/audio/chunk?session_id=&token=&seq=&rate=16000` | 上传一片 PCM（16k/单声道/16bit，二进制体） |
| POST | `/api/turn` | 一轮对话。音频已分片传完用 `{audio_streamed:true}`；打字用 `{text}` |
| GET | `/api/tts/rest?...&turn_id=` | 分段合成的剩余音频（首段在 `/api/turn` 响应里） |
| POST | `/api/call/barge_in` | 用户打断：记账 + 丢弃未播音频 |
| GET | `/api/events?session_id=&token=` | 这通电话的完整事件链 |
| GET | `/api/report?session_id=&token=` | 转写 + 质检 + 评分 |
| GET | `/api/metrics` | 首字 P50/P95、闭环数、成本估算 |
| GET | `/api/health` | 存活检查（含首字门槛） |

**纯文字跑一轮（最快验证，不用麦克风）**：

```bash
curl -s -X POST http://127.0.0.1:8000/api/call/start -H 'Content-Type: application/json' -d '{}'
# → {"session_id":"s_xxx","read_token":"...","opening":"您好，我是京东商家资质审核专员…"}

curl -s -X POST http://127.0.0.1:8000/api/turn -H 'Content-Type: application/json' \
  -d '{"session_id":"s_xxx","token":"...","text":"0086"}'
```

**实时语音通道**：`ws://127.0.0.1:8000/ws/call`
上行 `{"type":"start"}` → `{"type":"turn"}` / 二进制 PCM 帧；下行 `turn_result`、
`tts`（逐段 `audio_url`）、`tts_done`、`barge_ack`、`call_ended`。协议逐字段见
`docs/16_实时通话与打断.md`。

---

## 四、复现关键结果（results/）

```bash
python scripts/export_submission.py          # 跑 340 条 C4 用例 + 重新打包
python scripts/export_submission.py --limit 40   # 只跑前 40 条，快速核对
python scripts/verify.py                     # 全量自检（284 项）
```

- 每条用例**按场景号固定到对应申请单**（`goal` 里的「官方场景NN」→
  `KB-POP-NN`），并把用例里那句脚手架尾号换成这张单的真实尾号
  （`tail_rewritten` 字段记录了这次替换）。同一份代码重复跑得到同一批判定，
  不存在"随机抽单导致结果漂移"；`application_id` / `reject_code` / `verify_tail`
  都写进结果，评审可以逐条回溯到具体申请单。
- 结果落在 `results/`，**一个用例一个 JSON，文件名 = 用例 id**
  （`results/C4-S01-Q1.json`）。这是表单"每张图片一个 JSON、文件名与图片同名"
  在语音场景下的等价口径：本项目没有图片输入，测试单元是"用例"。
- 有些场景在官方档案里**只有京麦站内信或号码无效的单**，电话外呼链路不适用。
  这类用例不硬跑成失败，记在 `results/_summary.json` 的 `skipped` 里，
  附场景号与原因；`cases` 只统计真正打过电话的用例。
- 每个 JSON 都含 `input.turns`（客户原话）、`expect`（判定条件）、
  `actual.replies`（客服逐轮回复 + 阶段 + 意图）、`actual.closed` / `consent`、
  `verdict`、`problems`、`application_id`，可直接逐条核对，不必先跑代码。
- `results/_summary.json` 是汇总（通过率、分类统计、生成时间）。

---

## 五、目录

```
app.py                  HTTP 入口与主链路（开单 / 一轮 / 挂断 / 指标 / 看板接口）
ws_channel.py           WebSocket 实时通道（音频帧、逐段推 TTS、打断、收线）
modules/dialogue.py     状态机与话术编排（六环节、核身、追问收口、合规护栏）
modules/voice.py        识别与合成（DashScope，分段合成 + 首段优先）
modules/realtime_asr.py 流式识别适配（半句字幕）
modules/politeness.py   出话前统一敬语润色
web/                    前端（语音通话 / 事件链 / 训练 / 数据看板）
scripts/                评测、验证、进化、导出（export_submission.py 是交付入口）
results/                逐用例 JSON + 汇总（评审要求的结构化结果）
docs/                   架构、契约、时延口径、接口地图（14 号是 API 清单）
eval/                   测试集（C4 真实通话用例 340 条等）
```

---

## 六、时延口径（避免误读）

- **首字响应**：客户说完 → 浏览器真的出声（`onplaying`），含云端合成与下载起播；
- **服务端首字**：请求到达 → 首段音频就绪，是下界，不含网络回程与起播；
- 门槛 2000ms；面板与 `/api/metrics` 都按前者统计，两套口径分开记录，不混报。

产品侧取舍：长回复按句末标点切段、**首段优先合成**，后面几段在客户听第一段时
已经合成好；收线按本轮实际播放时长兜底，并把"每段开播"上报为进度，保证
结束语一定播完才挂断。

---

## 七、已知边界

- 说话人分离未做：环境里第三人说话会被当成客户（VAD 只能判"有没有人声"）；
- 数字串（手机号/单号）逐位念，TTS 时长比正文估算略长；
- 线上演示库每次重新发布会清空真实通话统计，需要样本时用 `/dashboard?db=demo`。
