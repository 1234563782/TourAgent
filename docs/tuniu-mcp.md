# 途牛远程 MCP 接入说明

## 接入范围

默认配置为 `TRAVEL_PROVIDER=tuniu`。现有规划 Agent 通过适配器查询，
不会直接接收任意远程工具或供应商指令。

| 服务 | API Key 模式地址 | OAuth 模式地址 | 只读工具白名单 |
| --- | --- | --- | --- |
| 航班 | `https://openapi.tuniu.cn/mcp/flight` | `https://openapi.tuniu.cn/hybrid/mcp/flight` | `searchLowestPriceFlight` |
| 火车 | `https://openapi.tuniu.cn/mcp/train` | `https://openapi.tuniu.cn/hybrid/mcp/train` | `searchLowestPriceTrain` |
| 酒店 | `https://openapi.tuniu.cn/mcp/hotel` | `https://openapi.tuniu.cn/hybrid/mcp/hotel` | `tuniuHotelSearch`、`tuniuHotelDetail` |

应用安装的是 MCP Python 客户端 SDK，不是供应商服务端程序。
调用流程为初始化远程服务、获取工具列表、校验输入 Schema，再调用白名单查询工具。
JSON 和 SSE 响应均由 SDK 处理。返回的 `displayHint` 不会传给模型。
不开放订单、取消、支付、乘客身份或联系人数据工具。

## 授权配置

当前支持显式选择 API Key 或外部 OAuth Access Token，失败时不自动切换。

### API Key 模式

在项目根目录 `.env` 私密填写：

```dotenv
TRAVEL_PROVIDER=tuniu
TUNIU_AUTH_TYPE=apiKey
TUNIU_API_KEY=
```

将 `TUNIU_API_KEY=` 后的空值替换为途牛普通 MCP API Key，
不要填写飞猪密钥、TTMS 员工密钥或 OAuth Token。
客户端直接使用 `apiKey` 请求头连接上表 `/mcp/...` 地址，不安装途牛 CLI 或服务端。
无需设置三个 `TUNIU_*_MCP_URL`，默认会按鉴权模式选择；
已有 OAuth 自定义地址不能用于 API Key 模式，会在联网前拒绝。

API Key 路径依据官方 CLI 仓库的配置示例；配置账户已完成只读查询验证，
2026-09-16 另完成酒店搜索及房型详情实测。这不代表所有账户或授权范围均可用。
若返回 401/403，停止调用并核对密钥状态和授权，不循环尝试。

### OAuth 模式

设置 `TUNIU_AUTH_TYPE=oauth`，使用**外部获取的 OAuth Access Token**。
未设置 `TUNIU_AUTH_TYPE` 时，代码保持原有 `oauth` 默认值以兼容旧部署；
本地 `.env` 和示例模板明确选择 `apiKey`。
尚不支持打开浏览器登录、注册 OAuth 应用、使用授权码换取 Token、
保存 Refresh Token 或自动续期。这是临时的 Token 兼容接入，
不是完整的生产级 OAuth 客户端。

请通过途牛官方授权流程或已获授权的后端获取 Token。
不要将飞猪密钥、旧版途牛 API Key、授权码或 OAuth 客户端密钥当作 Access Token。

通过本地环境变量私密注入配置：

- `TUNIU_FLIGHT_ACCESS_TOKEN`：航班资源 Token。
- `TUNIU_TRAIN_ACCESS_TOKEN`：火车资源 Token。
- `TUNIU_HOTEL_ACCESS_TOKEN`：酒店资源 Token。
- `TUNIU_ACCESS_TOKEN`：可选共用 Token，仅限授权资源范围确实覆盖所请求服务时使用；
  各服务专用 Token 优先。
- `TRAVEL_PROVIDER`：`tuniu`，或明确选择 `flyai`；不会自动回退。
- `TUNIU_FLIGHT_MCP_URL`, `TUNIU_TRAIN_MCP_URL`, `TUNIU_HOTEL_MCP_URL`:
  使用上表中的官方地址。其他主机、路径、HTTP、URL 查询参数中的凭证和重定向
  都会被拒绝，避免将凭证发往非预期接收方。

应用自动加载项目根目录 `.env`，不搜索父目录，也不依赖终端工作目录；
系统环境变量优先，即使系统变量为空也会覆盖文件值。`.env.example` 仅供参考。
文件使用 UTF-8，支持 BOM、引号和注释；不执行命令、不进行 `${VAR}` 插值。
读取失败或格式错误时报告脱敏错误。未找到文件时仍可使用系统环境变量。
可通过系统环境变量 `TRAVEL_LOAD_DOTENV=0` 禁用文件加载。
`.env` 已被 Git 忽略，但明文文件仍需限制本机访问权限。
切勿将真实凭证粘贴到聊天、源码、提交文件或命令参数中。
API Key 和 Token 不会写入图状态或诊断报告。这些请求不会继承进程的 HTTP 代理设置。

缺少凭证时返回 `tuniu_authorization_required`，不发起网络请求。
Token 过期或返回 401 时需要重新授权；403 需要检查授权范围。
429 会报告为 `tuniu_rate_limited`，不要循环重试。

## 验证方式

检查本地配置是否存在，不验证凭证有效性：

```powershell
.\.venv\Scripts\python.exe travel_cli.py --check-config
```

`--check-config` 仅报告配置是否存在，不校验凭证；缺少模型配置时仍返回退出码 2，
不代表途牛配置失败。检查 `tuniu_auth_type`、`tuniu_api_key_configured`
和 `tuniu_credential_configured`，不会显示凭证内容。

配置已获授权的 API Key 或 Token 后，可手动执行只读验证，无需模型凭证。
请选择供应商支持的未来日期：

```powershell
.\.venv\Scripts\python.exe scripts/verify_tuniu.py --kind flight --origin Beijing --destination Shanghai --date 2026-09-08
.\.venv\Scripts\python.exe scripts/verify_tuniu.py --kind train --origin Shanghai --destination Hangzhou --date 2026-09-08
.\.venv\Scripts\python.exe scripts/verify_tuniu.py --kind hotel --origin Beijing --destination Shanghai --date 2026-09-08 --checkout 2026-09-09
```

每条命令发起一次搜索；酒店搜索成功后默认再查询前3家详情，
另有各次调用的协议初始化和工具发现请求。
SDK 可能重新连接 SSE 流，但应用不会重试搜索或详情调用。
请逐条执行并遵守平台额度。以上示例编写于 2026-09-06，
若在出发日期之后重跑，必须更新日期。
脚本仅输出数量统计和固定错误码，不输出原始响应或 Token。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

离线测试使用模拟数据，并通过模拟 HTTP 测试真实 SDK。
通过测试不代表凭证有效、兼容生产 Schema 或已验证实时库存。

## 数据与生产限制

- 仅请求搜索结果第一页；`TRAVEL_MAX_CANDIDATES` 限制输出数量。
- 工具 Schema 不兼容时直接拒绝调用。不解析 Schema 引用，包括远程引用。
  不继续获取工具列表后续页；第一页没有目标工具时，明确报告不可用。
- 机票金额包含文档定义的票面价与税费；任意一项缺失，总价均视为未知。
  额外费用和最终预订条件尚未核验。
- 火车候选保留各座席类型及对应价格。排除已知零库存座席；
  库存未知时不会标记为已确认有票。
- 交通结果必须包含有效出发时间且与请求日期一致，返程查询同样适用。
- 酒店入住和离店日期始终一起发送；可传明确的房间数、成人数和儿童年龄。
  未知值不自行补造，省略时明确标注供应商默认条件，不视为用户已确认。
- 搜索起价与房型报价分开保留；供应商返回金额按含税价格展示，但详情金额仍不能证明
  每晚/全程/多间口径，不能直接用于确定预算合规。详情请求包含日期，但响应未独立核实日期，
  库存只展示供应商快照，不保证预订时有房。
- 尚未实现供应商分页、缓存或专用调用额度限制器。应用已有用户/公司并发和每日
  请求/模型调用配额，但不等于供应商侧额度控制；仍应核实平台商业额度。
- 完整 OAuth 登录与续期、供应商房价口径核验、
  经批准的企业政策和真实模型行为都需要单独验收。

## 酒店详情配置

以下均可在项目 `.env` 配置；不填时使用默认值，生效需重启进程。

| 配置 | 默认 | 边界 |
| --- | --- | --- |
| `TUNIU_HOTEL_DETAIL_LIMIT` | `3` | 查询搜索结果前几家详情，0关闭，最多10家 |
| `TUNIU_HOTEL_RATE_LIMIT` | `4` | 每家保留报价数量，1至20 |
| `TUNIU_HOTEL_DETAIL_TIMEOUT` | `12` | 单家详情超时秒数，须大于0且不超过120 |
| `TUNIU_HOTEL_DETAIL_BUDGET` | `25` | 整个详情阶段额外时间预算，须大于0且不超过120 |

详情顺序执行，受单家超时和阶段总时限双重限制。搜索沿用原查询超时，
因此详情阶段的25秒不是整次规划总时限；模型、地图及其他查询另计。
单家详情失败保留搜索结果；鉴权、限流或工具协议不兼容时停止本批后续详情。
没有自动重试，不会为了补齐详情无限调用。

`hotel_rooms` 仅传给详情的 `roomNum`；`hotel_adults` 和 `hotel_child_ages`
分别映射成人数、儿童数及年龄列表。`hotel_child_ages=[]` 表示明确无儿童，
`null` 表示未知；不使用政策字段 `room_occupants` 推断成人总数。

详情只接受搜索返回的有效整数酒店ID，并核对响应ID一致。保留房型、床型、
餐食、退改、金额和库存等白名单数据；不保存或传给模型预订参数、供应商操作指令。
长文字截断时显示提示，不能把截断后的内容当成完整退改条款。
已知本次全部报价零库存的酒店禁止被规划器选中；未知库存不冒充售罄。
默认只展示少量房型，不代表完整库存或全网最低价。
应用不下单，用户自行到对应平台确认价格、房型和条款后预订。

## 资料来源

以下官方文档查阅于 2026-09-06：

- `https://open.tuniu.com/mcp/docs/guide/mcp-auth.html`
- `https://open.tuniu.com/mcp/docs/apidoc/mcp/flightMCP.html`
- `https://open.tuniu.com/mcp/docs/apidoc/mcp/trainMCP.html`
- `https://open.tuniu.com/mcp/docs/apidoc/mcp/hotelMCP.html`

2026-09-07 核验 API Key 地址及请求头的官方仓库示例：

- `https://raw.githubusercontent.com/tuniucorp/tuniu-cli/main/config.example.json`

OAuth 指南描述新模式；API Key 使用官方示例中的独立旧版路径，不混用两种凭证。
实际授权后的 `tools/list` 是运行时契约；2026-09-16 已实测酒店详情协议。
服务结构后续仍可能变更，遇到差异时会拒绝对应调用并报告错误。
