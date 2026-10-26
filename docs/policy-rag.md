# 差旅政策接入

## 当前实现

主助手仍委派规划流程、政策流程。政策检索和规则计算都是工具，不为检索、引用校验另建自主 Agent。

```text
原生 Word
  -> prepare：离线解析、规则草稿、来源哈希
  -> 人工核对导入包与原文
  -> stage：Embedding + PostgreSQL 草稿（事务写入）
  -> publish：显式确认哈希后发布
  -> 政策解释：混合检索 + 关联补全 + 带引用回答 + 独立语义复核
  -> 个人费用核验：单日/单晚确定性规则校验（不经过生成式解释）
```

默认 `local` 模式兼容已有 Markdown/TXT 检索与旧规则 JSON。
设为 `pgvector` 后只使用数据库中的已发布政策，不自动回退到旧文本或示例。
此实现不预订、不支付、不审批，也不是完整报销系统。

## Hybrid RAG 首批落地（2026-09-14）

本批保留 PostgreSQL/pgvector、jieba 与 RRF，不新增知识库框架、独立模型服务或数据库列。

```text
结构化意图与已审阅槽位
  -> 锁定组织、政策键与当前发布版本
  -> 精确向量 + 关键词召回 -> RRF
  -> 完整段落/条款、适用范围与关联条款补全
  -> 来源元数据、版本一致性、上下文完整性和字符预算检查
  -> PolicyAnswer 结构化提案
  -> 逐条校验引用编号与原文连续摘录
  -> PolicyAnswerReview 独立调用复核
     -> 充分：逐结论输出 [S1] 等标记及文件/版本/条款/片段来源
     -> 缺用户信息：返回经复核的追问，任务标记为待补充
     -> 检索不足：经复核的查询改写，在同组织同政策版本内再次检索
     -> 冲突、依据不完整、协议错误或服务异常：不输出未经核验的结论
```

解释与复核复用现有聊天模型配置，不需要新 API Key；它们是独立模型调用，不代表两个不同模型，
也不保证判断彼此独立。工具调用协议及 Pydantic 负责结构校验，引用连续摘录负责来源存在性；
语义支持、例外是否完整仍依赖模型与实际问答验证，不能声称完全消除幻觉。

解释链路要求来源具备文档 ID、版本、哈希与片段编号。本地 Markdown/TXT 后端仍可单独检索，
但其无版本来源不会作为正式政策传入此解释链路，不会自动回退到旧原文展示。
历史已保存回答仍可读取；本批不删除历史记录。

可选配置（已写入 `.env.example`，未修改真实 `.env`）：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `TRAVEL_POLICY_CONTEXT_CHARS` | 24000 | 序列化来源 JSON 的字符预算，范围1000至100000；不是 token 数 |
| `TRAVEL_POLICY_RAG_MAX_RETRIEVALS` | 2 | 含首次查询的总检索次数，范围1至3；设1禁用补检索 |

来源超预算或关联缺失时不静默裁剪、不生成结论。空结果、无有效版本或外部服务失败时不自动补检索；
补检索目前只针对已有完整来源但模型判断不足的情况。再次检索若版本变化则停止，不混合证据。
每轮正常回答最多增加两次模型调用，默认最多两轮检索；没有对服务失败追加重试。
Web 复用现有阶段事件显示“检索适用政策 / 整理政策依据 / 复核回答与引用”，不提前输出未审阅文字。

### 检索收尾与部分回答（2026-09-18）

检索预算限制的是检索次数，不是“超过次数就认定没有政策依据”。
模型通过复核的补检索请求遇到次数上限或重复查询，或普通语义复核重试耗尽时，
进入一次有界收尾阶段：

- 收尾使用 `PolicyFinalAnswer` 工具契约，枚举中没有 `retrieve`；运行时同样拒绝再检索动作。
- 有依据的结论保留在 `claims`；未解决的问题通过 `limitations` 记录问题、原因和核实建议。
- `partial` 必须同时具有引用结论与未解决部分；完整 `answer` 不允许带未解决部分。
- 部分回答声明缺少政策依据且仍有额度时，程序使用已复核的未解决主题继续检索，避免模型过早停止。
  已由确定性住宿查询标为仅供参考的档次缺口不触发此分支。
- 引用编号/逐字原文检查和独立语义复核继续执行；不能通过“还需核实”字段夹带无依据金额或政策要求。
- 个人住宿仅缺档次且已提供目的地时允许参考性部分回答，不得认定个人档次。缺地区或必要日期仍按确定性规则追问。
- 交通待遇不自动映射为住宿档次；有依据可列出各档标准，并明确请向学校财务核实个人对应档次。
- 各轮同版本来源去重合并，保留稳定编号，仍受 `TRAVEL_POLICY_CONTEXT_LIMIT` 和字符预算约束。
  版本变化、资料不完整、超预算、真实冲突、模型或数据库异常依旧单独处理，不伪装成成功。

部分回答在前端显示可回答内容与“还需核实”，任务仍为 `partial`，不冒充完整完成。
Langfuse 元数据 `policy_stop_reason` 区分 `retrieval_budget_exhausted`、`repeated_query`
与 `review_budget_exhausted`；
真实模型超时等错误继续使用独立 `policy_failure`。

收尾复用 `TRAVEL_POLICY_RAG_MAX_ANSWER_ATTEMPTS`（默认2次、可设1至3），不新增无限重试。
每个生成尝试最多另加一次复核调用；最坏模型调用数上限为
`2 × (总检索次数 + 1) × 每阶段尝试次数`，通常远低于此值。供应商失败不重试。
该收尾不是机械拼接原文，部分回答的语义可靠性仍须真实模型验收。
本轮验收见 `docs/RAG收尾与部分回答验收_2026-09-18.md`。真实测试仍观察到语义审核漏过限定语省略，
不应把离线回归通过等同于所有政策回答均完整准确。

### 复核耗尽收尾（2026-09-18）

普通复核连续拒绝后，不直接丢弃全部信息，也不直接发布被拒草稿。
程序将拒绝意见和旧草稿作为待修正数据，交给仅能结束流程的 `PolicyFinalAnswer`。
最终阶段仍使用原配置中的尝试上限；再次耗尽就停止，不能递归进入第二次收尾。

收尾中的完整/部分回答改由 `PolicyEvidenceReview` 复核，程序分别检查：

- `claim_checks`：按零起始编号逐项确认结论是否有依据。漏项、重复编号、越界或错误类型均拒绝。
- `limitations_valid`：未解决部分如实说明且有核实建议；不能夹带无引用政策或虚构要求。
- `scope_preserved`：保持当前事项目标，不能猜测身份、日期或档次。
- `coverage_complete`：单独判断是否完整回答。完整 `answer` 必须为真；
  `partial` 允许为假，但所有证据、安全与限制检查仍须通过。
- `conflict`：真实制度冲突停止回答，不能为了结束流程放过冲突。

逐项不支持的编号随修复反馈传入下一次生成；所有新草稿重新校验引用和语义。
缺少个人条件只影响相关待遇时，可以保留有依据的其他待遇，并在未解决部分说明需补的信息。
这里不新增职级映射、政策金额或身份专用分支，不增加检索预算，不对供应商故障重试。
最终仍无安全结论时可以返回不足；本改动不是保证任何输入都能给出答案。

本批尚未实现独立 reranker、语义分块模型、知识图谱、互联网政策兜底或自动上下文压缩。
RRF 仍只是排名融合，不是模型精排。独立重排的供应商协议和真实收益留待下一阶段验证。

### 新分块的使用

正文优先在句末切分，单个超长句仍受 `--chunk-size` 字符硬上限限制，保留全部字符。
同一无编号段落的分片、同章同编号条款的分片通过 `related_ids` 关联，实现检索小片段、
返回完整父段落/条款，不增加父文档表。支持多级数字条款和“第十二条”等编号；
未支持的编号格式、重复编号和复杂表头仍须人工核对。

更新代码不会重写已发布片段。使用新分块必须显式 prepare 新导入包，人工核对后 stage/publish；
现有版本内容不可覆盖，版本与生效日应由制度管理员确认，不能为技术重建随意更改政策生效时间。
本批没有执行重新上传、发布、数据库迁移或服务重启。

## 先填写配置

项目根目录 `.env` 中新增以下配置；不要覆盖已有途牛或聊天模型配置。
`.env.example` 提供全部配置键。不要把真实值提交到 Git、终端历史或聊天中。

| 配置键 | 填写内容 |
| --- | --- |
| `TRAVEL_POLICY_BACKEND` | `pgvector` |
| `TRAVEL_POLICY_DATABASE_URL` | PostgreSQL 连接串，格式 `postgresql://用户名:密码@主机:端口/数据库` |
| `TRAVEL_POLICY_COMPANY_ID` | 模拟导入包使用 `agent-demo`，实际公司需另建对应导入包 |
| `TRAVEL_POLICY_KEY` | `travel` |
| `TRAVEL_EMBEDDING_BASE_URL` | 支持 `/embeddings` 的 HTTPS 基础地址，通常以 `/v1` 结尾 |
| `TRAVEL_EMBEDDING_API_KEY` | 对应 Embedding 服务密钥 |
| `TRAVEL_EMBEDDING_MODEL` | 支持中文的向量模型名称，不是聊天模型名称 |
| `TRAVEL_EMBEDDING_DIMENSIONS` | 该模型实际输出的向量维度 |
| `TRAVEL_EMBEDDING_REVISION` | 默认 `1`，同名模型底层版本变化时更新 |
| `TRAVEL_EMBEDDING_BATCH_SIZE` | 默认 `1`；确认供应商批量限制后可调大，程序支持1至64 |
| `TRAVEL_POLICY_HNSW_M` | HNSW 图连接数，默认 `16`，范围4至100 |
| `TRAVEL_POLICY_HNSW_EF_CONSTRUCTION` | 建索引候选宽度，默认 `128`，必须不小于 `M` |
| `TRAVEL_POLICY_HNSW_EF_SEARCH` | 查询候选宽度，默认 `80`；文档过滤较多时可调大 |

数据库须预先安装可用的 pgvector 扩展文件。`init-db` 可以执行 `CREATE EXTENSION`，
但不会安装 PostgreSQL 服务或系统扩展包。连接串的用户名和密码须正确 URL 编码；
生产连接由管理员配置 TLS 和最小权限。

兼容接口仅对支持该参数的模型发送 `dimensions`（当前为 `text-embedding-v3/v4`）；
`qwen3.7-text-embedding` 使用服务端默认的1024维，并再次验证模型实际输出维度。
当前仅支持兼容 JSON `/embeddings`、Bearer Key、HTTPS 的服务，禁止重定向，
不自动继承代理，不自动切换供应商或重试鉴权失败。此项目没有选定或订购模型服务。

```powershell
.\.venv\Scripts\python.exe scripts/policy_admin.py check-config
```

此命令只输出缺少的键名，不联网、不显示配置值。存在配置不代表连接成功。

## 核对 Word 与规则

用户提供的修订版是模拟制度，不自动作为真实企业制度发布。
本轮生成的导入包位于 `artifacts/policy/demo-v1.1.bundle.json`。
其公司标识为 `agent-demo`，版本 `V1.1`，测试生效日为 `2026-09-07`，无固定截止日。

重新生成时选择不存在的新输出文件，命令默认拒绝覆盖：

```powershell
.\.venv\Scripts\python.exe scripts/policy_admin.py prepare --source "artifacts/agent有限公司差旅费报销管理制度_模拟修订版_V1.1.docx" --output artifacts/policy/demo-v1.1-new.bundle.json --company agent-demo --version V1.1 --effective-from 2026-09-07 --rules policies/demo-v1.1.rules.json
```

`prepare` 不读取 `.env`，不访问数据库或模型；只读 Word，写入独立 JSON。
规则是人工整理的配置，不由 LLM 自动提取后执行。普通新 Word 可以省略 `--rules`，
先做文档检索，但没有规则的版本无法执行金额计算。

核对项：

- 导入包公司、版本、适用日期和来源文件是否正确。
- 表头、城市等级、职级、金额、税费和单位是否与原文一致。
- 住宿合住倍率、包餐扣减、驻场阈值和来源条款是否正确。
- `warnings` 是否有合并单元格或图片提示；不要忽略未经确认的解析警告。
- `rules` 中的配置是否代表你准备发布的口径，不能只核对源 Word 而忽略规则配置。

核对后保留 `prepare` 或 `stage` 输出的 `bundle_sha256`，发布时输入同一个值。
它绑定的是包含正文、规则和元数据的规范化导入包，不只是 Word 文件。
代码负责校验哈希一致，不能代替人工核对，也没有企业签章或审批权限系统。

## 数据库导入与发布

以下命令在用户配置完成后再执行；本轮没有执行这些真实服务操作。
`stage` 会把政策章节正文发送给所配置的 Embedding 服务，请先确认该服务的数据使用权限。

```powershell
# 显式创建表；需要数据库 DDL 权限
.\.venv\Scripts\python.exe scripts/policy_admin.py init-db

# 上传正文做向量化并保存草稿，不会发布
.\.venv\Scripts\python.exe scripts/policy_admin.py stage --bundle artifacts/policy/demo-v1.1.bundle.json

# 以下参数使用 stage 返回值；此处只是语法示意，不含真实凭证
.\.venv\Scripts\python.exe scripts/policy_admin.py publish --document-id <返回的编号> --reviewed-sha256 <核对后的哈希>
```

同公司、同政策键、同版本且内容、模型空间和分词版本相同，重复导入返回 `unchanged`，
不再次请求 Embedding。已有版本内容不一致时拒绝覆盖，须使用新版本号。
导入中任何写入失败，事务回滚；Embedding 失败不写入半成品文档。

发布时拒绝有效期重叠。明确替代旧版时加 `--supersede`，
将旧版截止日更新为新版生效前一天，保留旧条款供管理追溯；
不允许这样覆盖生效日期相同或更晚的已发布版本。
这不是删除旧政策。上述日期是导入发布元数据，不再是聊天选版本的条件。
运行时采用管理员最新发布的版本；本轮不改发布元数据管理流程。

## 检索方式

每次请求先根据部署绑定的组织、政策键、已发布状态选择最近发布文档，
按 `published_at DESC NULLS LAST, id DESC` 确定当前版本，草稿不可见。
一般咨询不需要日期；实际费用/入住日期只用于季节标准等业务条件，不选政策版本。
用户提到政策年份不能填成费用日期，缺失实际日期不能默认今天。
然后分别执行：

1. pgvector 余弦距离精确搜索。
2. jieba 中文分词，PostgreSQL `simple` 词典及 GIN 索引关键词搜索。
3. RRF 合并排名，默认双路各20条、保留5条基础命中。
4. 有基础命中时，先补齐当前有效版本已审阅规则中 `source_clauses.scope` 的全局边界，
   再补齐其他关联条款；不依赖基础命中是否已带有关联编号。

默认最多返回40个片段。关联补全会沿引用继续查找，处理循环引用；
关联条款超限或关联编号对应行缺失时返回 `context_incomplete=true`。
解释流程对此停止生成并明确说明缺失，不认定依据完整。
基础命中优先保留，全局边界和其他关联条款共用剩余额度并按片段编号去重。
没有规则配置的文档不添加全局边界。该补全不改变RRF排名，也不提升模型审批权限。
RRF 分数是排序依据，不是“回答正确概率”；仍需真实检索评测。

可以配置 `TRAVEL_POLICY_TOP_K`、`TRAVEL_POLICY_RECALL_K`、
`TRAVEL_POLICY_RRF_K`、`TRAVEL_POLICY_CONTEXT_LIMIT`。
可通过 `TRAVEL_POLICY_DICTIONARY_PATH` 指定 jieba 用户词典。
更换分词词典、Embedding 基础地址/模型/维度/修订标识后需重新导入新版本，
不能混用不兼容的向量。

向量召回默认使用全局 HNSW 余弦索引，查询保留 `document_id` 范围过滤，
并在每次查询事务内设置 `hnsw.ef_search`。初始化数据库或导入政策草稿时会确保当前 Embedding
维度对应的索引存在；已有数据库可显式执行：

```powershell
.\.venv\Scripts\python.exe scripts/policy_admin.py migrate-hnsw
```

也可以只生成 SQL 供管理员审核：
`scripts/policy_hnsw_ddl.py --dimensions <维度> --m 16 --ef-construction 128`。
索引缺失时查询会回退到原来的精确排序，`search(..., _include_rankings=True)` 会在
`rankings.vector_search_mode` 标记为 `exact_fallback`；这用于迁移窗口和事故恢复，不应作为长期运行状态。
由于近似索引先检索再应用文档过滤，多个政策文档共存时需要通过 `TRAVEL_POLICY_HNSW_EF_SEARCH`
和真实库上的 `EXPLAIN (ANALYZE, BUFFERS)`、Recall@K、P95 延迟验证召回与性能。

## 表格与规则边界

原生 DOCX 按正文顺序解析，不按渲染页数分块。
一张表保留网格、表头、数据行、章节和条款；每行生成带首行表头的检索文本。
解释模型只接收行文本及来源，不重复发送整张表格网格；关联条款另行补全。
合并单元格展开并产生核对警告；嵌套表格、未处理修订及文本框拒绝导入，
要求先展平或整理，防止静默丢失。图片文字暂不做 OCR。

第一版规则工具按单个费用日/住宿晚计算：

| 类型 | 校验范围 |
| --- | --- |
| 住宿 | 职级、城市映射、单人/双人自愿合住、单间单晚含税上限 |
| 交通 | 已确认席别、普通员工乘机明确批准条件、铁路优先提示 |
| 餐补 | 过夜/当日往返、确认后的地区、包早餐/午餐/晚餐扣减，同餐去重 |
| 市内交通 | 是否有公务出行、接送覆盖、自付路段、自驾费用重复提示 |
| 特殊场景 | 连续驻场达到阈值、专项审批、适用范围不符转人工 |

所有规则金额、扣减比例和职级映射来自规则 JSON，而不是在业务代码中写死。
四类结果为 `within_checked_rules`、`over_standard`、
`insufficient_information`、`manual_review`；全部不代表批准。

输入 `mainland_business_trip` 表示该费用日已确认属于制度范围内的大陆异地公务，
不是只确认“人在大陆”。`long_assignment_days` 是同一驻场安排的连续自然日，
未知时保持空值。`special_approval` 表示是否存在需优先核对的专项审批，不能猜测为 false。
餐补 `allowance_region` 使用规则配置中的已确认地区标签；目前普通地区使用“普通地区”。
员工职级使用“高管”“经理主管”“普通员工”，职级映射需明确确认。

模拟规则已列A/B类城市，C类金额已录入，但没有导入完整行政区划库。
未知城市或区县不会自动落到C类；须由管理员核对后补充
`city_categories.C` 或 `city_aliases`，生成新版本后发布。
不得由模型自行把不明城市归类，或自行把区县映射到上级城市。

供应商酒店起价、未知计价单位/税费/日期仍返回无法判断。
多晚酒店列表没有逐晚房价时，不以同一个起价判断整段住宿。
交通工具不能仅凭票价认定席别合规，须确认具体席别后交政策工具。
总预算、票据真伪、跨单重复报销、全部审批流程和地区日程自动推导尚未实现。

## 验证命令

离线测试，不使用真实凭证：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip check
```

用户填好配置并完成导入发布后，显式执行：

```powershell
.\.venv\Scripts\python.exe scripts/policy_admin.py search --question "其他人员档在南京的住宿上限是多少？"
.\.venv\Scripts\python.exe scripts/policy_admin.py check --facts policies/demo-hotel-facts.json
.\.venv\Scripts\python.exe scripts/verify_policy.py --cases policies/demo-retrieval-cases.json
.\.venv\Scripts\python.exe travel_cli.py --user employee_demo --session policy_demo
```

示例住宿事实应得到上限420元、已核验金额400元、`approved=false`。
检索验收包含30个固定问题，要求指定条款均被找回且关联上下文未截断。
它只评测来源召回，不证明真实模型回答正确。

## Qwen API 重排

仅接入 PostgreSQL 政策文字检索，不给机票、酒店推荐或确定性政策金额查询额外增加重排请求。
流程为：向量/关键词各召回20条，RRF去重合并，最多40条交给重排API，保留前5条作为种子，
再按原逻辑补齐精确住宿附表、适用范围和关联条款。原文、来源编号和版本绑定保持完整。

在现有 `.env` 中添加 `TRAVEL_RERANK_API_KEY=你的Key`，默认即启用 `qwen3-rerank`。
其余参数可选：

```dotenv
TRAVEL_POLICY_RERANK_MODE=qwen
TRAVEL_RERANK_BASE_URL=https://maas.qianwenaiapi.com/api/v1
TRAVEL_RERANK_MODEL=qwen3-rerank
TRAVEL_RERANK_TIMEOUT=8
TRAVEL_RERANK_CANDIDATES=40
```

地址采用用户提供的控制台示例，原生北京统一地址也可配置为
`https://dashscope.aliyuncs.com/api/v1`；使用与该服务/地域匹配的Key。
如需使用控制台示例中的另一模型，设 `TRAVEL_RERANK_MODEL=qwen3.7-text-rerank`。
这里是原生API地址，不能填写聊天接口 `/compatible-mode/v1` 或完整排序端点。

HTTP协议：`POST {BASE_URL}/services/rerank/text-rerank/text-rerank`，Bearer认证；
请求使用 `input.query/documents` 和 `parameters.top_n/return_documents`，
读取 `output.results[].index/relevance_score`，按输入索引还原政策片段，记录 `usage.total_tokens`。
使用原文全文、不静默截断；若供应商拒绝超长输入，按失败回退，不丢弃证据后继续判断。
不配置分数阈值，相关性分数不当作政策合规概率。

Key/地址缺失、超时、网络错误、HTTP失败或索引/分数异常均使用原RRF前5条，不重试接口。
显式 `TRAVEL_POLICY_RERANK_MODE=rrf` 可关闭请求。缺少已发布政策或Embedding仍遵循原有错误处理，
不能用重排替代召回。Key未填写时不请求API，完整配置且发生请求后计入现有模型调用预算。
无需重新导入政策或生成Embedding；更新配置后重启现有服务并发起新查询。

启用Langfuse时，`policy_retrieve` 下可见 `policy_rerank`，默认只上传安全元数据：
候选数量、耗时、固定错误码、模型摘要和供应商Token总数。
候选ID、分数和模型名等详细负载遵循已有内容采集开关及脱敏；默认不上传问题和政策原文。
本地评测的 `rankings.rerank` 同样保留实际重排状态、候选ID、入选ID、分数、Token数和耗时。

对照实验使用同一份经人工确认的gold测试集和同一已发布资料，不使用旧 `--mode baseline`
比较本次重排（该模式还会移除生产上下文补齐）。请分别运行生产流程的两个排序模式：

```powershell
$env:PYTHONPATH='.;tests'
.\.venv\Scripts\python.exe -m evals.rag_retrieval_eval run --mode production --rerank-mode rrf --cases evals/rag_retrieval_gold_reviewed.jsonl
.\.venv\Scripts\python.exe -m evals.rag_retrieval_eval run --mode production --rerank-mode qwen --cases evals/rag_retrieval_gold_reviewed.jsonl
```

`--cases` 应指向实际人工审核的测试集路径。真实评测会调用Embedding和重排服务并产生费用，
本次未执行。结果分别保存为 `.verification/rag-retrieval-production-rrf*` 和
`.verification/rag-retrieval-production-qwen*`，避免覆盖对照结果。
比较 `selected_ids_recall_at_1/3/5`、`selected_ids_mrr`、`context_target_coverage`、
`retrieval_p50_ms`、`rerank_success_cases`、`rerank_fallback_cases` 及逐请求费用。
旧 `rrf_ids` 保留原RRF结果，不能把它当作重排结果。检索指标不等同于最终回答正确率；
回答引用质量、端到端耗时和总费用仍需单独实验。

接口参考：https://help.aliyun.com/zh/model-studio/text-rerank-api
地域/统一域名参考：https://help.aliyun.com/zh/model-studio/base-url

## 权限与隐私

四张表分别为 `policy_documents`、`policy_chunks`、`policy_rules`、`policy_check_logs`。
审计表只保存政策版本关联、费用类别/日期、结果状态、原因和条款，不保存员工姓名、
问题原文或凭证。图的既有 SQLite 对话存储仍会保留对话，应按敏感数据保护。

公司范围从可信部署配置绑定，不开放给模型工具参数。当前 CLI 无企业登录、
用户级 ACL 或 PostgreSQL RLS，不应直接部署成共享多租户服务。
生产应由管理员配置独立迁移账号、只读检索/规则读取及审计写入权限、
行级权限、备份和数据保留策略；当前没有自动数据清理服务。
