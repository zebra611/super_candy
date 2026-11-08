# POP 商家资质审核驳回申请单档案（虚构模拟数据）

> **本文件及同目录 `applications.json` / `applications.csv` 全部为虚构测试数据，
> 不对应任何真实企业、自然人、电话号码、邮箱或商标。**

## 一、档案类别（先明确对象）

类别：**京东 POP 商家资质审核驳回「申请单 + 商家主体」业务档案**。

不是人员档案、不是设备档案，而是**一次资质申请被驳回所形成的业务记录**。选这个类别的原因：
本项目的 C5（mock 换业务）要把 `query_customer`（售后客户核身）换成
`query_application`（申请单定位），一份档案必须同时支撑五件事：

1. **核身**：姓名 / 手机后四位 / 公司名 / 申请单号四条路径；
2. **申请单定位**：确认「哪笔申请、哪个原因」；
3. **驳回话术检索**：命中 `kb_pop.json` 的 `KB-POP-0XX` 条目；
4. **五域分类**：驱动 `pop_reject.json` 的 `reject_rules` 与回写枚举；
5. **结果回写**：`call_result` / `reject_category` / `merchant_understood` / `close_mode`。

## 二、用途

| 用途 | 消费方 | 说明 |
|---|---|---|
| 核身模拟 | `modules/adapters/mock.py` → `query_application` | 替代现有 2 条假客户（张伟/李娜），让真实语音报姓名或尾号能核上 |
| 申请单定位 | `dialogue.py` 的 NEED/OFFER | 驳回原因、补充材料、品牌类目从档案取，不再由用户口述猜 |
| 评测种子 | `run_eval.py` / `adversarial_*.json` | 120 条覆盖典型与边缘，可直接转成对抗用例（多品牌、跨类目、撤回、失效等） |
| 回写校验 | `tools.validate_writeback` | `call_result`、`merchant_understood`、`close_mode` 与 `writeback_template` 枚举对齐 |
| 京麦过滤回归 | `kb.py` 的 `no_call` 检索层 | 「京麦驳回」域档案验证电话场景不念站内指引 |
| 占位符两套体系 | `kb.render` | `placeholder_style` 标明该档案适用空【】还是具名【XX】 |

## 三、格式要求

| 文件 | 格式 | 说明 |
|---|---|---|
| `config/mock/applications.json` | **JSON（主数据）** | `{"_meta": {...}, "items": [...]}`；UTF-8 / LF / 2 空格缩进；120 条 × 62 字段 |
| `config/mock/applications.csv` | **CSV（表格）** | 同样 120 行 × 62 列；UTF-8 **带 BOM**（Excel 双击不乱码）；列表字段用 `\|` 分隔；布尔写 `true/false`，`unknown` 保留字符串 |
| `scripts/gen_mock_applications.py` | 生成脚本 | 固定 seed `20261106`，任何机器重跑逐字节一致 |
| `scripts/check_mock_applications.py` | 校验脚本 | 数量/唯一性/完整性/覆盖/虚构性/独立性/可复算；`--self-test` 做 6 种变异反向测试 |

复算与自检：

```
python scripts/gen_mock_applications.py
python scripts/check_mock_applications.py --self-test
```

## 四、字段结构（51 个，八组）

**① 标识（3）**：`record_no` 档案序号、`application_id` 申请单号、`merchant_id` 商家主体 ID、
`fictional_marker` 虚构标记（合计 4 个，见下注）。

> 注：51 字段 = 标识 4 + 主体 9 + 联系人 6 + 申请单 9 + 驳回 7 +
> 核身 5 + 外呼关单 6 + 窗口标记 5。

| 组 | 字段 | 类型 / 取值 |
|---|---|---|
| ① 标识 | `record_no` / `application_id` / `merchant_id` / `fictional_marker` | 序号、编号、`SIM-2026-TEST-FICTIONAL` 常量 |
| ② 主体 | `company_name` / `company_short_name` / `shop_name` / `company_type` / `region` | 虚构公司全称、字号、店铺名、8 种主体类型、34 个省级行政区 |
| ② 主体 | `registered_capital_wan` / `established_date` / `legal_representative` / `business_scope` | 注册资本（万元）、成立日期、法定代表人、经营范围 |
| ③ 联系人 | `contact_name` / `contact_role` / `contact_phone` / `phone_tail` / `contact_email` / `alternative_contact` | 虚构姓名、6 种角色、`1990000XXXX` 虚构号段、后四位、`@example.test` 保留域、备用联系人 |
| ④ 申请单 | `submitted_at` / `application_type` / `brand_name` / `brand_registration_no` | 提交日、8 种申请类型、虚构品牌、虚构商标号 |
| ④ 申请单 | `category_l1` / `category_l2` / `category_l3` / `target_channel` / `application_status` | 三级类目、POP/全球购/京麦、5 种申请状态 |
| ⑤ 驳回 | `reject_code` / `reject_category` / `reject_reason` / `reject_at` / `reject_round` | `KB-POP-0XX`、七类分类、原因、驳回日、第几轮驳回 |
| ⑤ 驳回 | `required_materials` / `recheck_deadline` | 需补材料列表、复核截止日 |
| ⑥ 核身 | `verify_by_name` / `verify_by_tail` / `verify_by_company` / `verify_by_application` / `verify_preferred` | 四条核身入口的值 + 本档案推荐入口 |
| ⑦ 外呼关单 | `close_mode` / `call_status` / `call_result` / `merchant_understood` / `callback_needed` / `hotline` | 电话/京麦、7 种外呼状态、`connected/no_answer/refused/invalid/not_applicable`、`true/false/unknown`、是否回电、官方热线 |
| ⑧ 窗口标记 | `data_valid_from` / `data_valid_to` / `placeholder_style` / `edge_cases` / `notes` | 数据有效窗口、占位符体系、边缘标签列表、说明 |

**空值约定**：不写 `null`。刻意缺失的信息用带档案序号的显式占位值，例如
`（未提供备用联系人·007）`、`（未申报品牌·013）`——既表达「没有」，又保证全局唯一、
下游按值索引不会被污染。

## 五、覆盖矩阵

| 维度 | 覆盖 |
|---|---|
| 数量 | 120 条（≥100） |
| 业务域 | 品牌-授权 19 / 品牌-商标 20 / 品牌-全球购 19 / 商品与行业资质 20 / 入驻与主体店铺 19 / 京麦驳回 20 / 未定位 3 |
| 外呼状态 | 待外呼 / 已接通 / 拒绝 / 无应答 / 无效号码 / 已关单 / 无需外呼（7 种全有） |
| 关单方式 | 电话、京麦（2 种） |
| 占位符 | 空【】、具名【XX】、无占位符（3 种） |
| 主体类型 | 有限责任公司、股份、个体工商户、合伙、分公司、个人独资、外商投资、港澳台投资（8 种） |
| 地区 | 31 个省级行政区 + 中国香港 / 中国澳门 / 中国台湾（表述按国家规范） |
| 边缘标签 | **38 种**：前导零尾号、连号尾号、顺子尾号、超长公司名、生僻字、同音近形姓名、无备用联系人、多品牌、跨类目、多次驳回、撤回、失效/过期、京麦无需外呼、热线兜底、两套占位符、超长驳回原因、多核身路径、三类港澳台主体、个体/分公司、未来窗口、需回电、拒绝/无应答/无效号码、未定位、全球购、无品牌、3 位尾号上报、近似店铺名、无截止日、超长经营范围等 |

**相互独立**：任何档案都不引用另一档案的编号；法定代表人、备用联系人姓名从独立姓名段取，
不与联系人姓名撞。校验脚本逐字段扫跨档案引用。

## 六、虚构性声明

- 公司字号、品牌名、人名均为**自造词组合**；若与真实主体名称巧合，纯属偶然，无对应关系；
- 手机号统一为 `199-0000-XXXX`，是**虚构不可拨号段**，不对应任何真实号码；
- 邮箱统一为 `@example.test`（RFC 2606 保留域），不可能投递到真实邮箱；
- 不使用本项目团队成员或任何真实自然人姓名（校验黑名单含团队成员姓名与常见真实企业字号）；
- 港澳台一律写作「中国香港 / 中国澳门 / 中国台湾」；
- `hotline` 使用赛题官方公开热线 `4006180092`，属公开号码而非个人信息；
- 每条档案带 `fictional_marker`，下游若误把本数据当真实业务数据可直接拦下。

## 七、接入 C5 的方式（2026-11-07 已接线）

本包只是**数据**，不改 `modules/adapters/mock.py`。C5 接线时：

```
query_application(keyword) → 按 verify_by_name / verify_by_tail /
    verify_by_company / verify_by_application 任一命中 → 返回整条档案
```

`reject_category` 直接喂 `pop_reject.json` 的 `reject_rules` 与回写 `writeback_template`；
`close_mode=京麦` 或 `call_status=无需外呼` 的档案应跳过电话外呼（与 kb `no_call` 过滤同口径）。
`call_result` / `merchant_understood` 已与 `writeback_template.required` 枚举对齐，可直接做回写断言。

---

## 八、2026-11-08 扩充：完整 mock 数据包

原来这里只有「120 条档案 + 2 个假客户」，与官方资料包对不上：官方有 **68 个关单场景**、
**52 条驳回话术**、**高频 QA**，而档案里的驳回原因只有 8 条自造说法。现在把官方表
机械转成结构化数据，并按目录补一套档案，形成完整 mock 包（**原文件一个都没删**）。

### 8.1 文件清单

| 文件 | 条数 | 来源 / 生成方式 | 谁在用 |
|---|---:|---|---|
| `applications.json` / `.csv` | 120 | `scripts/gen_mock_applications.py`（结构未变；11-08 修掉 14 条「电话却标无需外呼」的自相矛盾） | 评测基线、`verify` 证据 21、C4 340 条 |
| `applications_catalog.json` / `.csv` | 68 | `scripts/gen_mock_catalog_applications.py`（一场景一条） | 运行时合并池、证据 32 |
| `catalog.json` | 68 | `scripts/build_mock_bundle.py` 解析官方 xlsx | `tools.reject_catalog()` / `reject_script()` |
| `rebuttals.json` | 52 | 同上（驳回话术 sheet） | 话术对照与答辩引用 |
| `qa.json` | 27 | 同上（高频 QA 11 + 特殊场景 16，已去重） | `tools.lookup_qa()` |
| `customers.json` | 12 | 手写，C1001/C1002 与 `mock_business` 同值 | `query_customer` |
| `orders.json` | 16 | 手写，`query_order` 取该客户最近一笔 | `query_order` |

> 运行时申请单池 = `applications.json` **在前** + `applications_catalog.json` **在后** = 188 条。
> 顺序固定是为了让 `next_outbound_target()`（取第一条待外呼）行为与只有原集时完全一致。

### 8.2 新增字段（只在 `applications_catalog.json` 里）

| 字段 | 含义 |
|---|---|
| `catalog_id` | 对应 `catalog.json` 的场景 id（`KB-POP-001`…`KB-POP-068`），与 `reject_code` 必须相等 |
| `catalog_scene_key` | `一级菜单/一级分类/二级分类`，便于人读与反查 |
| `official_script` | 官方该场景的参考话术原文（逐字，不改写） |
| `official_close_mode` | 官方指定的关单方式（`电话` / `京麦`） |
| `reject_source` | 固定 `官方场景目录`，标明这条不是自造原因 |

原集 120 条没有这几个字段——合并池里读到的是 `None`，下游一律用 `.get()` 取，
不要写 `r["catalog_id"]`。

### 8.3 一致性约束（`check_mock_bundle.py` 会逐条查）

- 68 个场景 **每个都有档案**，且 `reject_code == catalog_id`；
- 档案的 `close_mode` 必须等于官方场景的关单方式；`京麦` → `call_status=无需外呼`，
  `电话` → 不得标 `无需外呼`（原集有 14 条这种自相矛盾，已在生成器里修掉）；
- `reject_category` 必须等于目录的 `domain`（业务域映射：菜单 + 关键词 → 七域枚举）；
- 合并池的 14 组关键字段全局唯一（单号/尾号/公司名/电话/信用代码/身份证…）；
- 订单的 `customer_id` 必须在客户表里，且每个客户至少一单；C1001 最近一笔必须是
  `O20261101001`（`test_tools` 依赖这个值）。

### 8.4 复算

```bash
python scripts/build_mock_bundle.py        # 目录/话术/QA（源：competition/官方资料包/*.xlsx）
python scripts/gen_mock_applications.py    # 原集 120 条
python scripts/gen_mock_catalog_applications.py   # 官方场景 68 条
python scripts/check_mock_bundle.py --self-test   # 七类校验 + 6 种变异反向测试
```

三个生成脚本都是**固定种子**，重跑逐字节一致；`check_mock_bundle.py` 里的
「可复算」一项就是重新生成一遍再比对 sha。
