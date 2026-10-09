# Langfuse 观测接入

## 当前工作区状态（2026-09-18）

用户已选择官方云端，本地 `.env` 的观测和脱敏内容记录开关均已设为1。
源码和 `.env.example` 的缺省仍为关闭，不改变其他部署的默认数据出口。
服务已按用户要求停止，本轮未启动；下次启动后，新请求才会记录内容，旧追踪不会回填。
本轮已通过真实 SDK 内存导出验证和云端合成追踪读回，未上传真实历史或执行真实模型/供应商请求。
云端合成追踪 `7ebb312742fc7a94c01e7666f15afdb9`：5个节点均已结束，
输入输出可见、伪造敏感值未原样出现、模型父子节点关联通过。
离线回归824项：733通过、91跳过、零失败；依赖检查通过。跳过项不计入通过，
本轮未进行真实数据库或模型/供应商端到端验收。

可运行下列脚本单独验证云端认证、内容可见和脱敏：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/verify_langfuse.py --capture-content
```

该脚本只发送合成正文和伪造敏感值，不调用真实模型、政策数据库或供应商；
只输出验证状态和合成 trace ID，不输出凭据或云端原文。不加该参数仍强制验证纯元数据模式。

### 历史证据（2026-09-15）

当时完成了新密钥认证、4节点合成追踪，以及隔离账号的真实 Web/SSE 酒店对话验收：
11轮、14项业务检查和11条云端追踪通过。这不是本轮内容记录的云端验收结果。

真实浏览器联合验收在 `scripts/verify_auth_browser.py --hotel-flow` 后增加 `--langfuse`，
并按原有参数传入本机 Node、Playwright 模块目录和浏览器路径。会调用真实模型和酒店服务，
但只使用临时账号/数据库 schema，不预订或支付。该历史验收脚本显式关闭内容记录，
完整问答仅保留在本地，云端只保存白名单元数据，原有隐私断言不放宽。
结果位于本批证据目录的 `summary.json`、`hotel-events.json` 和 `langfuse.json`。
SDK指定trace ID会生成虚拟父节点，验收以应用捕获的根观测ID检查树，仍严格拒绝子节点断链或循环。
详见 [真实对话联合验收](真实对话与Langfuse联合验收_2026-09-15.md)。

## 本轮范围

Langfuse 是可选诊断出口，不是业务数据库、记忆库或自动纠错器。
SDK 固定为 `langfuse==4.15.2`，默认关闭；无需在本机部署 Langfuse 服务。
业务继续使用已有 LangGraph、PostgreSQL、鉴权、会话和请求幂等机制。
本轮不添加数据库表、不改变酒店查询协议、不自动重放失败请求。

## 配置

在项目根目录 `.env` 中配置，原有业务凭据不需要更换：

```dotenv
TRAVEL_LANGFUSE_ENABLED=0
TRAVEL_LANGFUSE_CAPTURE_CONTENT=0
TRAVEL_LANGFUSE_CONTENT_MAX_CHARS=131072
LANGFUSE_BASE_URL=
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
TRAVEL_LANGFUSE_HASH_KEY=
```

1. 在所使用的 Langfuse 实例中创建或选择一个项目，从该项目的 API Keys 设置获取公钥和私钥。
2. 使用同一实例提供的 SDK 基础地址填写 `LANGFUSE_BASE_URL`。不能填写控制台项目页面地址、模型地址或途牛 MCP 地址。
3. 在本机单独生成 HMAC 密钥，将结果填入 `TRAVEL_LANGFUSE_HASH_KEY`；至少32字符，不复用账号密码和平台密钥。可在自己的终端运行：

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

4. 准备联调时才将 `TRAVEL_LANGFUSE_ENABLED` 改为 `1`，检查配置并重启服务。其他值均不启用。本轮未重启正式8000服务。
5. 同意向所配 Langfuse 实例上传脱敏业务正文后，将 `TRAVEL_LANGFUSE_CAPTURE_CONTENT=1`。
   `TRAVEL_LANGFUSE_CONTENT_MAX_CHARS` 限制每个 Input/Output 的 JSON 字符规模，
   默认131072，允许1024至1048576；另有深度和节点保护，省略时显示 `_truncated` 或 `OMITTED`。
   它不是模型 token 预算，也不能保证任意长提示词都完整保留。

```powershell
.\.venv\Scripts\python.exe travel_cli.py --check-config
```

诊断输出中的 `langfuse_configuration_problem` 为空表示本地格式检查通过，不代表认证或网络通过。
`langfuse_connection_verified` 目前始终为 `false`。配置诊断不会请求云端，也不会输出观测密钥。
进程环境变量优先于 `.env`；已有进程不会自动重读文件。

地址必须是 HTTPS 根地址，不接受凭据、查询串、片段或项目路径。
仅本机自建实例允许 HTTP，例如 `http://127.0.0.1:3000`。
两种平台密钥必须配套；HMAC 密钥由本地管理，平台不需要知道它。

## 能看到什么

| 观测位置 | 内容 |
| --- | --- |
| `chat` | 每次实际业务执行的起止时间、执行状态、检查点是否提交 |
| `chat_model` | 模型调用耗时、异常类别、模型返回的标准输入/输出 token 计数 |
| 工具调用 | 白名单工具名、起止时间、是否抛出异常或等待人工确认 |
| 槽位 | 尝试次数、意图/操作类型、校验失败分类、复核结论、是否提交 |
| 任务 | 任务终态、版本、HMAC 后的任务引用 |
| 规划 | 交通/酒店查询状态与候选数量、候选政策核验和通勤状态 |
| RAG | 检索次数、来源数量、上下文是否不完整、最终回答状态 |

开启内容记录后，点击一条新 Trace，再选左侧具体节点，查看 **Input / Output**：

| 节点 | 新增正文 |
| --- | --- |
| `chat` | 本轮用户输入/确认动作、本轮新出现的最终回复和待确认事项 |
| `chat_model` | 实际传入的消息（含系统提示词和已裁剪上下文）、工具定义、模型名及调用控制参数；完整返回的正文与 tool calls |
| 工具回调 | 工具入参、返回消息；不导出 ToolMessage 的任意 artifact |
| `query_flight/train/hotel` | 适配器实际接收的查询条件和返回候选，不是底层 HTTP 抓包 |
| `candidate_policy`、`commute` | 候选核验、通勤的输入与结果 |
| `policy_retrieve`、`policy_result` | 查询条件、实际召回条款及来源、最终提案/失败诊断 |
| `slot_review`、`slot_validation` | 复核结果、字段校验或机械校验反馈 |

模型流式调用在结束时记录聚合回答，不逐 token 新建观测。
异常仍只记录安全分类及状态，不上报可能包含凭据的任意异常原文。
如果可见历史发生非追加变化，根节点省略无法可靠归属本轮的答案，不冒充为本轮输出。

模型调用的 token 计数同时写入观测 metadata 和 Langfuse 原生 `usage_details`：
`input`、`output`、`total`。因此打开单条 Trace 后，选中紫色的 `chat_model`
生成节点即可查看该次模型调用的用量；Langfuse 可据此在 Trace/Usage/Cost 视图中汇总
同一 Trace 下的多个模型调用。若供应商没有返回标准 `usage_metadata`，该次用量不会伪造。
当前项目没有额外上传费用字段，费用是否可见仍取决于 Langfuse 中配置的模型价格。
HTTP 状态码目前仅记录识别到的模型 API 状态异常，不保证所有供应商异常都有状态码。
未对每个 LangGraph 节点单独建 span，也未采集底层 SQL、HTTP 请求明细。

普通请求完成不等于业务成功：

- `chat.execution_status=completed` 只表示本轮执行和落盘完成。
- `slot_commit.slots_committed=false` 表示本轮槽位未提交。
- 查询 `status=empty` 表示查询无候选，不表示工具网络失败。
- 通用工具回调的 `status=ok` 只表示正常返回，应继续看内部业务状态。
- 人工确认中断记录为 `awaiting_confirmation`，不算供应商异常。

## 隐私与隔离

内容开关为0时，只采集固定名称、枚举、非负计数、布尔值和 HMAC 引用；
不处理或发送提示词、用户消息、模型回答和工具正文，保持原有白名单边界。

内容开关为1时，应用先显式投影所需字段，再在本机过滤敏感键、已知配置密钥、
常见凭据/手机号/邮箱/证件号模式；JSON 字符串会尝试结构化处理。
未知对象、二进制/媒体载荷不直接交给 SDK，过长内容有标记省略。
元数据继续使用原有白名单；不导出序列化模型配置、鉴权对象或任意 provider metadata。

这是尽力脱敏，不保证识别自然语言中的所有姓名、地址、商业机密或变形/编码后的敏感内容。
政策条款、行程及用户问题本身会发往所配置的云端项目，请限制项目成员权限和数据保留期限；
含真实敏感资料时先关闭内容记录，不能把它当作完全匿名化或合规审批。

使用独立 OpenTelemetry provider，不注册为全局 provider；SDK mask 再次过滤内容。
用户引用包含公司作用域，会话/请求引用继续包含上级作用域。
HMAC 引用是可关联的假名标识，并非完全不可识别的匿名数据；仍应限制项目访问权限和保留期限。
更换 HMAC 密钥后，新旧身份引用不再相同。

每个执行请求创建独立回调；SSE 在线程内部建立上下文，避免并发请求混用父 span。
历史读取和幂等返回已完成请求不创建新的执行追踪。
不在每轮末尾强制 flush，正常进程关闭时关闭 SDK 导出队列及 provider。
进程强制终止时仍可能丢失未发送观测，不将遥测作为审计唯一凭据。

## 故障行为

- 开关关闭时不初始化或加载 Langfuse SDK。
- 配置不完整、SDK 初始化/记录/关闭抛出异常时，业务继续；项目侧只输出固定警告，不记录异常正文。
- 不因观测失败重试聊天或修改业务状态。后台导出失败可能仅体现在 SDK 日志中，不能以没有项目警告认定已送达。
- 初始化失败后本进程不反复初始化，修好配置后重启；`0` 是回退开关。
- SDK 有独立后台网络和队列开销，不能承诺启用后完全零延迟或零资源占用。

## 验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_observability.py -q
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_telemetry_content -q
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
node --test tests/chat_stream.test.cjs
.\.venv\Scripts\python.exe -m pip check
```

离线集成测试使用真实 SDK、真实 LangGraph、内存导出器和模拟模型，并禁止 HTTP 客户端联网。
它能验证本地组装和最终导出内容，不能证明平台账户权限、网络、费用或远端持久化正确。
SDK 接口依据本地已安装版本源码核对，本轮官方网页检索没有返回可用正文，未据此推断云端配置已正确。

后续云端联调使用无敏感信息的测试对话，逐项核对：

1. 同一请求的模型和业务 span 属于同一个 trace，刷新历史不新增执行。
2. 缺日期追问、查询空结果、槽位拒绝和接口异常可以区分。
3. 元数据模式不含正文；内容模式能看到合成提示词、回答和工具结果，但伪造凭据已遮盖。
4. 两个会话引用隔离；关闭开关并重启后不再新增观测。

详见 [本轮验收报告](Langfuse接入验收_2026-09-15.md)。
