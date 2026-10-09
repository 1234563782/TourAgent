# LangGraph 迁移与运行说明

## 实现范围

当前默认差旅查询服务为途牛官方远程 MCP。API Key / OAuth Token 配置与验收边界见
[途牛接入说明](tuniu-mcp.md)。
下文 FlyAI 结果是明确选择该备选服务时的历史验证记录，
不代表途牛已完成真实授权验收。

LangGraph 是唯一运行实现，启动入口为 `travel_cli.py`。
旧版 AgentScope 入口、Python 配置、Agent 模块、插件代码和专用测试已删除。
模型配置从环境变量读取，不再使用 Python 配置文件。

架构：

```text
用户 -> 主助手 -> 规划专家 -> 航班/火车/酒店/天气/通勤工具
               -> 政策专家 -> 指定来源的政策文档检索
               -> 记忆/天气工具
               -> 人工确认 -> 保存草案/偏好
```

主助手负责补齐缺失信息、分派任务和解释结果。
专家 Agent 可以在分配的任务内推理并使用多个工具，不只是执行固定命令。
供应商查询和记忆读取属于工具，不单独拆成 Agent。每轮模型响应最多执行一次工具调用。

规划使用经过校验的城市和日期，只能选择供应商实际返回的候选 ID。
政策金额上限由确定性逻辑校验，政策解释由独立专家负责。
金额比较不等于审批。缺失的价格、币种、房间库存和最终费用仍视为未核验。

不支持预订、支付、取消、报销提交或审批。
保存草案或偏好前，必须确认界面展示的具体记录。保存草案不会锁定或预订任何资源。

## 环境配置

本地已使用 `.venv` 中的 Python 3.12.14 和 pip 验证。
统一使用 `requirements.txt`，无需 AgentScope 或旧版向量检索依赖。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

新机器请先使用已安装的 Python 3.12 创建 `.venv`：
`py -3.12 -m venv .venv`.

配置自动读取项目根目录 `.env`，进程环境变量优先（包括空值）。
`.env.example` 仅供参考；系统环境变量 `TRAVEL_LOAD_DOTENV=0` 可禁用文件加载。
请通过环境配置或密钥管理器私密注入凭证，不要写入源码、聊天、
共享终端记录或命令历史。

必需的模型配置：

| 变量 | 含义 |
| --- | --- |
| `TRAVEL_LLM_API_KEY` | 模型服务密钥 |
| `TRAVEL_LLM_MODEL` | 支持工具调用的模型 |
| `TRAVEL_LLM_BASE_URL` | 兼容 chat-completions 接口的服务基础地址 |

三个 Agent 当前共用同一个模型配置。模型服务兼容性与实际行为仍需真实联调验收。

可选工具配置：

| 变量 | 含义 |
| --- | --- |
| `FLYAI_NODE_PATH` | 兼容的 Node 可执行文件绝对路径 |
| `FLYAI_CLI_PATH` | 官方 FlyAI CLI 程序包绝对路径 |
| `FLYAI_PRICE_CURRENCY` | 核实供应商币种含义后才可设置 |
| `AMAP_API_KEY` | 地理编码和驾车路线服务密钥 |
| `TRAVEL_POLICY_RULES_PATH` | 已获批准的政策规则 JSON 文件 |
| `TRAVEL_POLICY_DOCS_DIR` | 已获批准的 UTF-8 `.txt`/`.md` 政策来源目录 |
| `TRAVEL_STORAGE_DIR` | 默认使用项目内 `data/graph` |
| `TRAVEL_LEGACY_MEMORY_DIR` | 默认使用项目内 `data/memory` |

本工作区已有经验证的独立 FlyAI 安装，可按以下方式显式启用：

```powershell
$env:FLYAI_NODE_PATH = (Resolve-Path ".verification/flyai/runtime/node-v22.23.2-win-x64/node.exe").Path
$env:TRAVEL_PROVIDER = "flyai"
$env:FLYAI_CLI_PATH = (Resolve-Path ".verification/flyai/package/dist/flyai-bundle.cjs").Path
.\.venv\Scripts\python.exe travel_cli.py --check-config
.\.venv\Scripts\python.exe travel_cli.py --user employee_demo --session trip_demo
```

`.verification` 被 Git 忽略，不随应用分发。
`--check-config` 检查模型和记忆必需配置是否存在，不验证连通性、凭证有效性或所有配置路径。
缺少模型或记忆配置时退出码为 2。首次部署需显式初始化 PostgreSQL 记忆表，
见 [记忆存储与迁移说明](memory-postgres.md)。

## 政策与持久化

本节先说明兼容的 `local` 模式。新 Word/pgvector 后端见 [政策接入说明](policy-rag.md)。
本地规则结构为 `business_travel.contracts.PolicyRules`，包含 `version`、
`effective_from`、`effective_to`、`currency`、`hotel_nightly_caps`
（城市到正数金额的映射）和 `transport_ticket_caps`（flight/train 到正数金额的映射）。
请使用公司授权的规则值，示例政策不会被默认当作真实政策。
`effective_to` 可空，表示没有固定截止日。生效时间必须覆盖整个行程。酒店上限校验需要每晚报价；
当前酒店基础查询结果的价格口径未知。

本地模式使用关键词文本检索。`pgvector` 模式已提供按费用日期选择已发布版本、
员工职级和明确金额规则；不迁移旧 BGE/Milvus 流程。
协议价、完整审批和企业身份权限仍需在生产使用前另行实现。

默认入口使用 PostgreSQL 保存对话检查点和已确认记录。
SQLite 和旧 JSON 仅保留离线测试兼容及显式迁移用途，运行时不再自动合并旧 JSON。
旧文件不会被删除，迁移流程见 [记忆存储与迁移说明](memory-postgres.md)。
历史测试报告、用户设计文档和 `.claude/skills/ask-question/data/documents/`
中的政策参考文本作为数据保留，不是可执行插件，也不会自动成为可信政策。
`--user` 仍是开发标识，不是身份认证。数据库操作绑定公司和用户，
检查点读写校验会话归属；未来必须把该身份替换成服务端验证后的登录信息。
拥有数据库或旧文件访问权限的人仍可能读取消息和行程。
在补齐身份认证、授权、数据保留控制和存储保护之前，不要将此 CLI 部署为多用户服务。

交互命令：

- `/history`、`/preferences`：查看已保存记录。
- `/resume`：恢复未完成操作或确认流程。
- `/clear`：切换到新对话，不删除已保存记录或旧检查点。
- `/exit`：退出。

重启后恢复对话时，使用相同的 `--user` 和 `--session`。
`/clear` 创建的对话使用新的随机会话标识，不属于原来的命名会话。
CLI 会显示新标识，重启时可用 `--session` 指定它。
尚未实现自动上下文裁剪和数据保留/删除接口。

## 验证方式

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

这些离线测试使用明确标记的模拟数据和脚本化模型，覆盖数据契约、缺价、
错误日期、进程失败、政策金额上限、记忆隔离、确认/拒绝、需求变更、
SQLite 恢复、CLI 配置错误，以及未导入已删除运行模块的检查。
它们不衡量真实模型的规划质量。

手动执行供应商真实冒烟测试（只读，需要联网）：

```powershell
.\.venv\Scripts\python.exe scripts/verify_travel_adapter.py --node $env:FLYAI_NODE_PATH --cli $env:FLYAI_CLI_PATH --kind flight --origin Beijing --destination Shanghai --date 2026-09-08
```

重跑时请使用未来日期。火车查询需修改 `--kind`；
酒店查询需增加 `--checkout`，并将 `--date` 作为入住日期。
脚本输出脱敏统计，不输出供应商原始数据。

2026-09-06，使用官方 FlyAI CLI 1.0.16 和独立 Node 22.23.2，
在未提供 API Key 的情况下通过了项目实际适配器的查询验证：

| 查询 | 候选数 | 数字价格数 | 已核验出发日期数 | 链接数 |
| --- | --- | --- | --- | --- |
| 9 月 8 日北京至上海航班 | 10 | 10 | 10 | 10 |
| 9 月 8 日上海至杭州火车 | 10 | 0 | 10 | 10 |
| 9 月 8 至 9 日上海酒店 | 10 | 0 | 0 | 10 |

所有响应都提示体验模式，不代表免费无限调用、企业协议价、完整库存或酒店可订。
此接入通过适配器调用官方 CLI，不使用 MCPWorld 页面列出的服务端。
已查阅的 CLI 文档披露了设备元数据采集和本地 `~/.flyai/device-id` 文件；
其混淆程序包尚未经过安全审计。
Node 24.19.0 在本机输出 JSON 后于进程退出时崩溃；
适配器会正确拒绝非零退出码的输出。

## 待完成验收

- 私密配置模型，测试完整对话和工具调用兼容性。
- 提供经授权的政策文档与规则，验证检索和解释。
- 配置相关服务，核验天气日期覆盖范围和地图结果。
- 使用真实模型验证数据缺失、供应商故障、日期变更和确认流程。
- 评估供应数据授权、隐私、预算总额和生产身份控制。

天气使用按日期过滤的 wttr.in 预报，地图使用高德驾车估算。
本次迁移尚未完成这两项真实联调。超出覆盖范围的预报、
缺少地图凭证时的结果必须保持不可用，不能编造。
