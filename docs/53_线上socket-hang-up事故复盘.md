# 53 · 线上「服务没连上（socket hang up）」事故复盘

2026-11-12 21:32，用户在线上点「文字对话」连续两次得到：

```
服务没连上（Unexpected token 's', "socket hang up" is not valid JSON）。确认后端已启动再点一次。
```

本机复现不出来（本地开单 200ms）。最后定位到**两个叠在一起的问题**，都不是对话逻辑。

## 一、表面现象挡住的真因

前端的写法是 `await r.json()`。网关出错时返回的是**纯文本** `socket hang up`，
不是 JSON，于是 `JSON.parse` 先炸，抛出的错误把真正的原因盖掉了——用户看到
“服务没连上”，我们这边一行有用信息都没有。

线上只能靠加一个临时接口把 traceback 取回来：

```
File "/workspace/app.py", line 1021, in start_call
    events.write_event(session_id, "call_started", _started)
File "/workspace/events.py", line 172, in write_event
    cur = conn.execute(...)
sqlite3.DatabaseError: database disk image is malformed
```

**事件库坏了。** 而开单第一件事就是写 `call_started` 事件：写不进去抛异常 →
连接被直接关闭 → 网关回 `socket hang up`。所以只有 `/api/call/start` 挂，
其它接口（`/api/health`、`/api/applications`）都正常——这也解释了为什么
“页面能打开但点开始就失败”。

## 二、库为什么会坏：部署包把运行时数据一起传上去了

`data/events.db` 是**运行时数据**，但部署工具打包的是整个项目目录。
本地这个库当时正在被写（WAL 未 checkpoint），传上去就成了半写状态的文件。
每部署一次就坏一次——这也是为什么修好一版、再部署一版又挂。

## 三、45 秒：第二层问题

库坏掉之后，现象从“立刻 500”变成“卡 45.5 秒后 200”。加响应头 `X-Server-Ms`
之后确认是**服务端自己**慢，不是网关：

```
第1次 total=1.36s   X-Server-Ms: 1202
第2次 total=45.58s  X-Server-Ms: 45418
第3次 total=45.55s  X-Server-Ms: 45379
```

45.4 秒 = **3 × 15 秒**：开单要写 3 条事件，旧的 `_connect()` 每条新连接都跑一次
`PRAGMA journal_mode=WAL`（切换日志模式要拿排他锁），撞上锁就按 `timeout=15.0`
干等；三个请求线程各等 15 秒，正好 45 秒。而 HTTP 请求是**每请求一个线程、
每线程一条新连接**，所以第一次（连接还没抢锁）快，之后每次都慢。

## 四、改法

| 层 | 改动 | 理由 |
| --- | --- | --- |
| 部署纪律 | 新增 `scripts/prep_deploy.py`：checkpoint 后把 `data/events.db*` 移到 `data/archive/` | 运行时库不该进部署包，这是根因 |
| 事件库自愈 | `_open()` 做真查询探测；malformed → 隔离成 `*.corrupt-<时间戳>` 并重建；`_insert_event` 运行中损坏也走同一套 | 坏文件不该堵死业务，也不该被偷偷删掉 |
| 连接参数 | WAL 只在 `init_db()` 设一次；每请求连接 `timeout=0.3` / `busy_timeout=300` | 事件是遥测，最多等 300ms，不许等 15 秒 |
| 写入熔断 | 写失败后 30 秒内直接跳过 | 库被长期占用时，不让每个请求都白等 0.3 秒 |
| 写入语义 | `write_event` 永不抛；另给 `write_event_now`（脚本/测试要真写入时用） | 事件链缺一条可以补，电话打不出去是事故 |
| 请求兜底 | `Handler._guard()` 包住 `do_GET` / `do_POST`，未捕获异常一律回 JSON 500 `{error:SERVER_ERROR, detail:异常名: 原因}` | 下次不用再靠临时接口猜 |
| 前端 | 先 `r.text()` 再自己 parse；非 JSON 时显示 `HTTP xxx` + 原文前 120 字；网络错误自动重试一次；带 `error` 时如实显示 | `Unexpected token 's'` 这种提示等于没说 |
| 观测 | 所有 JSON 响应带 `X-Server-Ms` | 一眼分清“慢在我们这儿”还是“慢在网关” |

## 五、验证

线上（`voice-agent-skeleton.app.workbuddy.host`）：

- 事件写入：`{"write_ok": true, "write_ms": 158.9}`，之后单条 27ms
- 连开三单：`0.489s / 0.466s / 0.556s`（改前 45.5s）
- 指定档案开单（用户那张青岛/宁夏霄云）：正常返回 `QZ202612010056`

本地：`verify.py` **269/269**；新增 `scripts/test_events_recovery.py` 5 条
（坏库隔离重建、运行中损坏重试、只读介质不抛异常、正常库反向对照、
`_guard` 回 JSON）。其余 9 套单测全过。

## 六、遗留与教训

- 容器重启后事件库是空的（没挂持久卷）。演示够用；要看历史就从
  `data/archive/` 拷回本地库。
- **教训一**：遥测永远不能挡在业务路径上。事件写失败最多丢一条记录，
  不该让“开一通电话”失败，更不该让它等 45 秒。
- **教训二**：错误提示要能指向真因。`await r.json()` 这种写法在网关返回纯文本时
  会把原始错误吞掉，排查时间从 5 分钟变成 40 分钟。
- **教训三**：我前两轮把注意力放在“对话逻辑”，直线怀疑业务代码，
  直到量了服务端自报耗时（45.4s ≈ 3×15s）才转向存储层。
  以后线上慢问题第一步就该分“服务端 vs 网关”，别再猜。
