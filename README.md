# Aligo 高校智能差旅助手

基于 LangGraph 的高校智能差旅助手。入口通过专用 Tool Calling 提取意图和增量槽位，
主图按已验证状态派发；规划专家负责选择真实候选，政策专业工作流负责检索与确定性核验。
当前学校资料为合肥工业大学办法与住宿附表，按项目所有者指定用于2026年项目政策。
默认使用管理员当前发布政策，不询问政策生效时间；实际出差/入住日期仅用于查询与季节标准判断。
只提供参考规划、政策解释和报销条件核对，不办理审批、下单、报销提交或保险理赔。

当前仅保留 LangGraph 实现，差旅对话入口为 `travel_cli.py`。另有 FastAPI 登录、流式对话、会话与个人记忆工作台，
入口为 `business_travel.web:create_app`，详见 [Web 登录说明](docs/web-auth.md)。
旧 AgentScope 入口、配置、插件和专属测试已删除。

## 架构

JEV 的基础用法、单意图到多意图的演进、任务路由和验证边界，见
[JEV 接入原理与多意图改造详解](docs/JEV接入原理与多意图改造详解.md)。
任务准备阶段、字段完整性门禁与语义复核职责边界，见
[任务准备架构与提交前字段校验](docs/任务准备架构与提交前字段校验.md)。

```text
用户
  -> TurnRoute（操作、目标事项、控制/恢复授权）
  -> 按需 TripExtraction / ExpenseExtraction（同领域批量提取）
  -> 后端绑定版本与执行元数据，组装内部 SlotSubmission
  -> schema / 原话证据 / 事项版本校验 / 独立语义复核 / 整批提交
  -> LangGraph 主图按意图派发
       -> 规划工作流：交通与酒店查询 -> PlanSelection -> 时间 / 政策 / 地图核验
       -> 政策工作流：政策混合检索或确定性规则校验（不再次用模型提取事实）
       -> 普通主助手：记忆查询 / 天气 / 行程草案保存确认
  -> 业务结果确定性呈现；行程草案保存仍需人工确认。不保存长期偏好
```

查询能力不是独立 Agent；政策解释使用检索、模型提案与独立复核，结构化费用核验不依赖模型计算。
个人住宿标准若已由同版本附表确定缺失或待核实条件，程序直接按字段生成追问，
不再调用政策回答模型与复核模型；完整答案和复杂条款解释仍保留引用及独立复核。
省略字段保留旧值、明确撤回才清空，新事项不继承旧事项；核验结果与事项 ID、版本绑定。
Tool Calling 仍可能出错，程序执行有限重试并在失败时拒绝提交，不能保证模型理解永远正确。
保存行程不代表预订或审批。详见 [结构化槽位说明](docs/structured-slots.md)。

入口工具只有结构化提案能力，不执行查询或写入。闲聊和历史结果解释不加载领域字段工具；
版本、状态、执行次数由后端绑定，不让模型填写。旧 `SlotSubmission` 模型工具已移除，
同名类仅作为内部原子批次保留。分阶段缩小单次schema，但可能增加总调用数及延迟。

模型输入默认启用上下文预算：近期完整轮次、带来源的历史摘录及只读原文回查，
不删除持久化历史，不截断当前问题或政策证据后继续推断。
配置、计数边界与失败提示见 [上下文预算与压缩](docs/上下文预算与压缩.md)。

## 快速开始

可选诊断接入见 [Langfuse 配置说明](docs/langfuse.md)；默认关闭，只发送白名单元数据，
不上传对话、政策或工具负载。本轮本地测试与云端待验边界见 [接入验收](docs/Langfuse接入验收_2026-09-15.md)。

已在 Python 3.12.14、pip 和项目 `.venv` 中验证。

```powershell
# 新环境首次创建虚拟环境；已有 .venv 时跳过
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

启动前，在项目根目录 `.env`、本机环境变量或密钥管理工具中配置：

| 环境变量 | 用途 |
| --- | --- |
| `TRAVEL_LLM_API_KEY` | 模型密钥 |
| `TRAVEL_LLM_MODEL` | 支持工具调用的模型名称 |
| `TRAVEL_LLM_BASE_URL` | 兼容 Chat Completions 的接口基础地址 |
| `TRAVEL_PROVIDER` | 默认 `tuniu`；可显式设为 `flyai`，不自动回退 |
| `TUNIU_AUTH_TYPE` | `.env` 示例为 `apiKey`；未设置时保持兼容的 `oauth` 模式 |
| `TUNIU_API_KEY` | 普通途牛 MCP API Key，仅用于 `apiKey` 模式 |
| `TUNIU_HOTEL_DETAIL_LIMIT` / `TUNIU_HOTEL_RATE_LIMIT` | 酒店详情数量/每家保留报价数，默认3/4；详情数0关闭 |
| `TUNIU_HOTEL_DETAIL_TIMEOUT` / `TUNIU_HOTEL_DETAIL_BUDGET` | 单家详情/详情阶段时间上限，默认12/25秒 |
| `TUNIU_ACCESS_TOKEN` | 途牛 OAuth Access Token，仅在授权覆盖所需服务时共用 |
| `TUNIU_FLIGHT_ACCESS_TOKEN` / `TUNIU_TRAIN_ACCESS_TOKEN` / `TUNIU_HOTEL_ACCESS_TOKEN` | 各服务独立 Token，优先于通用 Token |
| `FLYAI_NODE_PATH` | 飞猪 CLI 使用的 Node 可执行文件绝对路径 |
| `FLYAI_CLI_PATH` | 飞猪官方 CLI bundle 的绝对路径 |
| `TRAVEL_POLICY_RULES_PATH` | 旧本地文本模式的规则 JSON；高校资料包使用 pgvector 模式 |
| `TRAVEL_POLICY_DOCS_DIR` | 旧本地文本模式的政策目录 |
| `TRAVEL_POLICY_KEY` | 当前政策键；高校资料包使用 `hfut_travel` |
| `TRAVEL_POLICY_RAG_MAX_ANSWER_ATTEMPTS` | 每次检索证据下的回答尝试数，默认2，范围1-3；供应商故障不重试 |
| `TRAVEL_RERANK_API_KEY` | 政策语义重排 Key；默认 `qwen3-rerank`，填入后启用，未填继续使用 RRF |
| `TRAVEL_POLICY_RERANK_MODE` | `qwen` 使用 API 重排；`rrf` 保留旧排序用于对照实验 |
| `TRAVEL_POLICY_HNSW_M` / `TRAVEL_POLICY_HNSW_EF_CONSTRUCTION` / `TRAVEL_POLICY_HNSW_EF_SEARCH` | pgvector HNSW 图和查询参数，默认 `16` / `128` / `80` |
| `policy_admin.py migrate-hnsw` | 为已有政策数据库创建当前 Embedding 维度对应的 HNSW 索引 |
| `AMAP_API_KEY` | 高德地图密钥，可选 |
| `TRAVEL_MEMORY_DATABASE_URL` | PostgreSQL 记忆连接串；留空复用政策数据库 |
| `TRAVEL_MEMORY_COMPANY_ID` | 组织标识（沿用字段名）；留空复用政策组织，须与政策组织一致 |
| `TRAVEL_MEMORY_SCHEMA` | 记忆专用 schema，默认 `travel_memory` |

程序自动读取项目根目录 `.env`，不依赖终端当前目录；系统环境变量优先（包括空值）。
`.env.example` 是无密钥参考模板，不会被自动读取。
设置系统环境变量 `TRAVEL_LOAD_DOTENV=0` 可禁用文件加载。
`.env` 已被 Git 忽略，但仍是明文文件；不要分享，也不要把密钥写入源码、文档或聊天。
只验证途牛查询时无需配置模型，使用下面接入说明中的只读验证脚本。

```powershell
.\.venv\Scripts\python.exe travel_cli.py --check-config
# 新环境首次运行；本工作区已初始化并完成旧数据导入
.\.venv\Scripts\python.exe scripts/memory_admin.py init
.\.venv\Scripts\python.exe travel_cli.py --user employee_demo --session trip_demo
```

途牛远程接入与授权边界见 [途牛配置说明](docs/tuniu-mcp.md)。
飞猪备选配置、政策格式和完整限制见 [运行说明](docs/langgraph-migration.md)。
Word 导入、PostgreSQL/pgvector、Embedding 配置与发布流程见 [政策接入说明](docs/policy-rag.md)。
Qwen 重排的接口配置、回退与对照实验也见该说明的“Qwen API 重排”一节。
学校正文和住宿附表的归档、资料包导入、适用规则及限制见 [高校政策接入](docs/高校政策接入.md)。
原有 `.env` 不会被覆盖；新政策配置键请参照 `.env.example` 添加。
记忆存储、旧 SQLite 导入与身份边界见 [PostgreSQL 记忆说明](docs/memory-postgres.md)。
本轮结果见 [记忆迁移验收报告](docs/记忆迁移验收报告_2026-09-08.md)：150 项测试通过，包含 13 项真实 PostgreSQL 测试。

## 交互命令

| 命令 | 用途 |
| --- | --- |
| `/history` | 查看已保存的历史行程 |
| `/resume` | 恢复未完成操作或待确认记录 |
| `/clear` | 开始新会话，不删除历史记录 |
| `/exit` | 退出 |

同一组织部署下使用相同 `--user` 和 `--session` 可恢复对应会话。
存储已按组织/用户/会话隔离，但 CLI 用户标签不是身份认证，不可直接暴露为多用户服务。
切换政策后应新开会话；历史企业事项、已保存结果不自动改写为学校结果。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

默认测试使用离线数据和脚本化模型，真实 PostgreSQL 测试需显式启用，命令见记忆说明。
覆盖记忆隔离、人工确认、SQLite 迁移、PostgreSQL 重启恢复及原有业务回归，不代表真实模型质量验收。

## 项目结构

```text
TourAgent/
  travel_cli.py             CLI 入口
  requirements.txt          统一依赖清单
  business_travel/
    graph.py                主助手、规划和政策 Agent
    contracts.py            请求、候选、报价与政策契约
    providers.py            飞猪、天气和地图适配器
    tuniu.py                途牛官方远程 MCP 只读查询适配器
    memory.py               记忆接口及离线 SQLite 兼容适配器
    memory_pg.py            PostgreSQL 行程与身份绑定检查点
    memory_migration.py     只读旧数据导入、事务及逐条核验
    policy.py               政策后端选择与本地文本兼容
    policy_documents.py     Word 条款、表格与版本化导入包
    policy_package.py       学校正文与住宿附表的同版本资料包
    policy_lodging.py       住宿地区、档次与旺季的确定性查询
    university_policy.py    学校费用规则核验，不套企业默认值
    policy_pg.py            pgvector 精确向量与关键词混合检索
    policy_rules.py         已核对政策的确定性规则校验
    settings.py             环境配置
  scripts/                  途牛与飞猪只读验证脚本
  tests/                    当前离线测试；results 中保留历史报告
  docs/                     运行说明及历史设计资料
```

## 当前边界

- 默认查询源为途牛官方远程 MCP，支持显式选择 API Key 或外部 OAuth Access Token；
  不自动切换鉴权方式、不自动浏览器登录或续期；已完成配置账户的只读查询验收。
- 途牛仅调用三个搜索工具及酒店详情工具，不开放预订与取消；有界补充房型、餐食、退改和库存快照。
  首屏列表不等同完整库存，酒店起价或房型报价均不能在计价口径未知时作为每晚价或住宿总额。
- 飞猪官方 CLI 的航班、火车和酒店只读查询已实测，返回体验模式。不是完整库存、企业协议价或不限额免费服务的承诺。
- 部分结果缺少数字价格；酒店列表不能证明指定日期有可预订房型。价格、币种、库存或总预算未确认时保持未知。
- 政策支持本地文本模式或显式 pgvector 模式，提供 Word 导入、版本发布和规则校验；
  不包含旧 BGE/Milvus 实现，也不替代审批。
- 高校资料包使用 pgvector：正文、交通表和住宿行参加检索；住宿金额另走同版本结构化查询。
  学校职称/岗位、交通类别与住宿档次分开保存，未知档次不猜测。单位为元/人·天，不自动均摊合住房价。
- 2026-09-09最终226项测试通过（含真实隔离数据库），真实业务固定16组19轮通过；
  范围及限制见[结构化槽位验收报告](docs/结构化槽位验收报告_2026-09-09.md)，不代表所有输入均可靠。
- 不支持订票、支付、退改、报销提交或审批。
- 旧数据、政策参考文本和历史测试报告保留；`.claude/skills/ask-question/data/documents/` 中的资料不会自动当作有效企业政策加载。
