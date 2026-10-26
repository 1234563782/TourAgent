# 候选输入精简与 Token 优化

更新日期：2026-10-15

## 1. 优化范围

本次优化针对酒店、火车票、机票候选选择阶段：减少发给选择模型的冗余字段，
完整候选仍由程序保留，不改变供应商查询、候选数量、后续校验或最终展示。

本方案没有新增模型调用，也没有减少现有模型调用次数。它减少的是每次选择调用的
输入内容，不是输出 token，也不是 KV Cache 或提示词缓存优化。
Jev 意图识别和主助手日期后移属于独立优化，不计入本文的效果数据。

## 2. 原有流程与优化位置

原先 `run_planner()` 将每个候选的完整 `Candidate.model_dump(mode="json")`
连同 `timeline_check` 发送给模型。模型通过 `PlanSelection` 返回 `selected_ids`。

原始候选中存在空字段、供应商内部 ID、抓取时间和重复座席等内容。它们增加输入长度，
但在当前选择任务中不提供额外的比较依据。

优化后的流程：

```text
供应商工具查询
  -> 现有适配层归一化，生成完整 Candidate
  -> planner 将完整候选保留在 collected 中
  -> 为选择模型生成 compact 投影，加上 timeline_check
  -> 模型返回 selected_ids
  -> 程序根据 ID 取回完整候选
  -> 库存、组件、组合时间校验，以及政策和酒店通勤检查
  -> 输出完整 selected / alternatives 供后续展示
```

精简发生在 planner 组装模型输入时，不是在 MCP 返回处破坏性删除原始字段。
这里的“完整”指现有适配层生成的候选；不会恢复适配层原本已截断的酒店房型或文本。

## 3. 实际删除规则

### 公共规则

| 字段或情况 | compact 行为 |
|---|---|
| 字典中值为 `null` 或 `""` 的字段 | 递归移除；顶层 `quote` 例外，保持完整 |
| 顶层 `source` | 从模型输入移除，原始候选保留 |
| 顶层 `observed_at` | 从模型输入移除，原始候选保留 |
| `truncated_fields=[]` | 移除；非空截断列表保留 |
| 值为 `false` 或 `0` | 保留，不作为空值删除 |
| 其他空列表，例如 `rates=[]` | 保留 |

不会将金额字符串改成浮点数，不改字段名，不改写退改文案，不新增时长字段。
未知的非空业务字段也保留，避免未经确认就丢失新供应商返回的信息。

### 火车票

- 当 `details.seat_type` 与顶层 `seat_class` 完全相同时，移除重复的 `seat_type`。
- 若两者不同，则保留两个值，不掩盖数据矛盾。
- 保留车次、座席、交通方式、站点、完整出发/到达时间、去返程标记及余票。

### 机票

- 当 `details.cabinClass` 与顶层 `seat_class` 完全相同时，移除重复的 `cabinClass`。
- 对 `kind=flight` 的候选，移除同义的 `transport_mode=flight`；其他值不强行删除。
- 保留航班号、航空公司、机场、时间、舱位、去返程标记及余票。
- 本次仍保留 `base_price`、`tax`，没有擅自删除票价构成。

### 酒店

- 移除 `details.hotelId` 和 `hotel_detail.requested.hotelId`，模型仍使用候选 `id`。
- 移除酒店详情的 `observed_at`，完整对象中仍保留。
- 仅当酒店 `details.inbound=false` 时移除这个无关的去返程字段。
- 酒店详情的 `date_verified`、`inventory_verified` 与顶层对应值相同时，移除重复项；
  不一致时保留。
- 保留 `requested` 中的日期，不根据入住日期自行认定它们重复。

## 4. 必须保留的选择依据

| 类型 | 当前保留内容 |
|---|---|
| 公共 | 候选 ID、类别、名称、完整 quote、请求日期、确认状态、时间检查结果 |
| 火车 | 车次、站点、出发/到达时间、交通方式、座席、去返程标记、余票 |
| 飞机 | 航班、航空公司、机场、出发/到达时间、舱位、去返程标记、余票、票价构成 |
| 酒店 | 地址、城市、入住日期、档次、非空品牌、房型、床型、早餐、报价、退改、详情状态 |
| 酒店可信度 | 库存状态、供应商默认入住条件、返回报价数量、报价截断及响应不完整标记 |

例如：

- `basis=unknown` 不代表已确认每晚价格。
- `not_queried` 表示未查详情，不能转换成售罄。
- `available_snapshot` 不是最终出票或订房保证。
- `rates_truncated=true` 表示仅提供了部分报价。
- `returned_rate_count=28` 表示返回 28 条报价，不是 28 条全部可订。
- 保留供应商余票值，不擅自将 `99` 解读成精确库存或无限库存。

## 5. 真实火车票示例

下面使用用户提供的 G547 商务座候选。为便于比较，JSON 采用紧凑排版。
这里只展示候选本身；实际模型输入还包含行程请求、偏好及 `timeline_check`。

原始候选：

```json
{
  "id": "3ca6dc4465a5300ec6ef", "kind": "train", "label": "G547 / 商务座",
  "source": "tuniu", "observed_at": "2026-10-13T11:25:04.994763+00:00",
  "request_date": "2026-10-15",
  "quote": {
    "amount": "1604.0", "currency": "CNY", "basis": "ticket",
    "status": "indicative", "taxes_confirmed": true
  },
  "departure": "2026-10-15 06:18", "arrival": "2026-10-15 10:48",
  "address": null, "url": null,
  "date_verified": true, "inventory_verified": false,
  "transport_mode": "high_speed_train", "seat_class": "商务座",
  "details": {
    "inbound": true, "departStationName": "北京南", "destStationName": "南京南",
    "seat_type": "商务座", "remaining_seats": 8
  }
}
```

compact 投影：

```json
{
  "id": "3ca6dc4465a5300ec6ef", "kind": "train", "label": "G547 / 商务座",
  "request_date": "2026-10-15",
  "quote": {
    "amount": "1604.0", "currency": "CNY", "basis": "ticket",
    "status": "indicative", "taxes_confirmed": true
  },
  "departure": "2026-10-15 06:18", "arrival": "2026-10-15 10:48",
  "date_verified": true, "inventory_verified": false,
  "transport_mode": "high_speed_train", "seat_class": "商务座",
  "details": {
    "inbound": true, "departStationName": "北京南", "destStationName": "南京南",
    "remaining_seats": 8
  }
}
```

删除的是 `source`、`observed_at`、空 `address/url` 和重复 `seat_type`。
价格、座席、时间、车站和余票均未改变。

## 6. 切换与代码入口

在项目 `.env` 中选择一种模式：

```dotenv
# 默认值：使用精简的候选输入
TRAVEL_PLANNER_CANDIDATE_MODE=compact
```

或恢复原始输入作基线：

```dotenv
TRAVEL_PLANNER_CANDIDATE_MODE=full
```

配置只接受 `compact` 或 `full`，非法值会在配置加载时被拒绝。
系统环境变量优先于 `.env`；常驻服务修改配置后需重启。
两种模式均保留完整候选；`full` 返回原来的候选 model_dump，不应用精简规则。

代码位置：

- `business_travel/candidate_input.py`：生成模型专用候选投影。
- `business_travel/planning.py`：调用投影并追加原有时间检查。
- `business_travel/settings.py`：读取和验证模式配置。
- `.env.example`：切换配置示例。

## 7. 已验证结果与局限

使用用户提供的三份真实查询结果，对完整候选和投影采用相同 JSON 序列化方式：
`ensure_ascii=False, separators=(",", ":")`，统计候选列表字符数。

| 样本 | 候选数 | full 字符数 | compact 字符数 | 字符数减少 |
|---|---:|---:|---:|---:|
| 酒店 | 8 | 10397 | 8233 | 20.8% |
| 火车 | 10 | 5793 | 4679 | 19.2% |
| 飞机 | 8 | 4913 | 3801 | 22.6% |

统计对象是归一化后的候选列表，不是原始附件文件大小，也不是整次模型输入。
行程请求、偏好、系统提示词、工具定义和时间检查不在上述统计范围内。

因此这些数字不能写成“实际 token 或费用降低约 20%”。
不同 tokenizer 的结果可能不同；整轮流程还存在其他模型调用，
整轮节省比例也不能由候选字符数直接推导。

实现阶段的组合回归通过 158 项测试，编译和 `git diff --check` 通过。
验证覆盖字段投影、配置切换、完整候选恢复、售罄拦截、时间约束、政策输入和结果展示。
测试使用脚本模型，不能证明真实模型选择质量与原方案完全一致。

核心回归命令，项目根目录 PowerShell：

```powershell
$env:PYTHONPATH = "tests"
.\.venv\Scripts\python.exe -m unittest tests.test_candidate_input tests.test_planning_stages tests.test_settings
```

## 8. 后续 A/B 实验

1. 固定同一批供应商查询结果，分别以 full 和 compact 输入，避免价格或库存变化干扰。
2. 保持模型、提示词、偏好、候选数量、重试上限及其他配置一致。
3. 用供应商实际 usage 记录每次输入/输出 token、重试次数、费用和延迟；
   缓存命中与未命中请求应区分统计，不把缓存折扣当作输入长度减少。
4. 同时检查用户约束满足率、无效选择率、政策校验结果及缺失组件；
   选择 ID 不同不一定是质量下降，需要核对选择是否仍符合要求。
5. 对同一用例进行重复测量，分别报告选择调用和整轮请求的效果。

实际输入 token 减少率：

```text
(full 输入 token - compact 输入 token) / full 输入 token × 100%
```

只有实际测量后才能报告 token、成本和性能提升。
若未来增加按供应商或报价新鲜度排序的要求，应重新评估 `source`、`observed_at`
是否需要送给模型；本次删除规则服务于当前选择任务，不代表这些字段永远无用。
