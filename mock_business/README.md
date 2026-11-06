# Mock 业务系统

三个接口的独立模拟服务：**查客户、查订单、建工单**。
零第三方依赖，纯标准库，可单独跑、单独演示。

```bash
python mock_business/service.py          # 默认 http://127.0.0.1:8100
python mock_business/service.py 8101     # 换端口
```

## 为什么独立成一个服务

1. 真实对接里这三个接口在业务方机房，不在通话进程里。分开做，联调时才能
   换地址、换鉴权、演超时——塞进同一个进程就永远测不到这些。
2. 2B（潘柏亦）的 `modules/tools.py` 要接真实接口，需要先有一个"长得像真的"的
   对端。这个服务就是那个对端，且**不占用他的文件**。
3. 它能独立跑，答辩时可以单独演示"业务侧也是我模拟的"，不用起整条通话链路。

## 接口

### `GET /mock/health`

```json
{"ok": true, "customers": 6, "orders": 7, "tickets": 0, "uptime_sec": 5.3}
```

### `GET /mock/customer?keyword=8866`

`keyword` 可以是姓名、手机号后四位、客户号。查不到返回 `200` + `found:false`。

```json
{"found": true, "customer": {"customer_id": "C1001", "name": "张伟",
 "phone_tail": "8866", "level": "金卡", "city": "北京"}}
```

```json
{"found": false, "customer": null}
```

### `GET /mock/order?customer_id=C1001`

也可以 `?keyword=8866`。返回该客户全部订单（倒序由数据文件决定）。

```json
{"found": true, "count": 2, "orders": [
  {"order_id": "O20261101001", "amount": 2680.0, "status": "已签收",
   "product": "智能按摩椅 X3", "warranty_days": 365}]}
```

### `GET /mock/application?keyword=QZ202611060001`（C5 新增）

`keyword` 可以是联系人姓名、手机后四位、公司名（支持唯一子串）、申请单号——四路径定位
POP 资质驳回申请单。数据源是仓库根 `config/mock/applications.json`（120 份虚构档案，
见 `config/mock/SCHEMA.md`）。查不到同样返回 `200` + `found:false`。

```json
{"found": true, "matched_by": "verify_by_application",
 "application": {"application_id": "QZ202611060001", "reject_category": "品牌-授权",
                 "close_mode": "电话", "...": "共 51 字段"}}
```

`/mock/health` 的响应里带 `applications` 计数，可用来确认档案是否载入成功。

### `POST /mock/ticket`

**必须带 `idempotency_key`**，否则 400。

```bash
curl -X POST http://127.0.0.1:8100/mock/ticket \
  -H "Content-Type: application/json" \
  -d '{"idempotency_key":"s_xxx:3","customer_id":"C1001",
       "order_id":"O20261101001","consent":true,"need":"说明书看不懂"}'
```

| 情况 | 状态码 | 返回 |
|---|---|---|
| 首次建单 | 201 | `{"created": true, "ticket": {...}}` |
| 同 key 重放 | 200 | `{"created": false, "ticket": {...}, "note": "..."}` |
| 缺幂等键 | 400 | `{"error": "MISSING_IDEMPOTENCY_KEY", ...}` |

幂等键建议用 `session_id:turn_id`——一通电话一张单，改口是**更新同一张**
（这是 `modules/tools.py` 已有的约定）。

### `GET /mock/tickets` · `GET /mock/stats`

工单列表 / 调用计数与错误数。演示时用来看"回写到底成没成"。

## 数据

| 文件 | 内容 | 是否进仓库 |
|---|---|---|
| `data/customers.json` | 6 个沙箱客户 | 是 |
| `data/orders.json` | 7 笔沙箱订单 | 是 |
| `data/tickets.json` | 运行时建的工单 | 否（gitignore） |

客户与订单的字段和 `modules/tools.py` 里的假数据保持一致
（`C1001 张伟 / 8866 / 金卡`、`O20261101001 / 2680 元 / 已签收`），
这样把 `tools.py` 切到这个服务时不用改业务代码，只改地址。

## 接到 `modules/tools.py` 上（2B 的活）

```python
BASE = os.environ.get("MOCK_BASE_URL") or "http://127.0.0.1:8100"

def query_customer(keyword):
    with urllib.request.urlopen("%s/mock/customer?keyword=%s" % (BASE, quote(keyword)), timeout=3) as r:
        return json.loads(r.read().decode())        # {"found":..., "customer":...}
```

三件事别做错：

1. **别把 `found:false` 当成异常**。业务上的"查不到"要走"没查到"分支，
   不是"接口报错"分支——两者的用户可见话术完全不同。
2. **`idempotency_key` 用 `session_id` 派生**，不要每次调用新生成 UUID。
   新 UUID = 每次重试都建新单，幂等形同虚设。
3. **超时要短**（3 秒内）。业务接口慢不该拖垮通话，`app.py` 的单轮预算
   也撑不住长等待。
