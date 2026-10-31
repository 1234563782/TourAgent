# 修复进度

## 2026-10-08

- 已明确范围：项目结构与技术栈、业务逻辑与主要流程、当前问题与潜在风险。
- 已读取 `planning-with-files` 与 `diagnose` 工作规范。

## 2026-10-16

- 继续上一轮差旅规则调整，核对了住宿附表、交通规则和现有住宿核验测试。
- 发现规划文件原本属于酒店选择诊断任务；本轮只记录规则确认，不修改业务代码，等待用户确认“院士”的住宿档次映射。
- 环境错误：`python` 命令不可用，已改用项目环境中的 `pwsh` 执行规划脚本。
- 用户确认“一类→部级、二类→司局级、三类→其他人员”，并授权继续实现。
- 已修改政策配置、大学政策解析、PostgreSQL 规划标准注入、结果标签和测试。
- 定向测试首轮因旧断言仍把“二类”视为非法住宿档次而失败；已更新断言。
- 定向测试通过：`tests.test_university_policy`、`tests.test_jev`、`tests.test_query_contracts` 共 67 项。
- 扩展定向回归通过：7 个相关测试模块共 140 项。
- 通过 `compileall`、政策 JSON 解析和 `git diff --check`。
- 全量测试发现 5 个与本次修改无关的既有基线问题：评测快照引用已变更的 `tests/test_slot_protocol.py`、旧版评测数据字段不匹配，以及一个观测字段断言差异；未修改这些既有问题。
- 已实现学生规划默认和条件提示，并将学生活动性质校验前置到交通、酒店等大学政策核验入口。
- 学生相关及规划回归测试通过，共 95 项；编译检查和 `git diff --check` 通过。

## 2026-10-22 竞品基准任务启动

- 已读取 README、核心 contracts/providers/graph/web 入口和业务模块清单。
- 已确认当前工作区没有 `.codegraph/`，跳过 CodeGraph。
- 已确认本轮不修改代码；下一步做官方竞品资料检索。

## 2026-10-22 竞品对照完成

- 已核对 README、高校政策接入、可执行规则和办法正文摘录。
- 已检索携程商旅、阿里商旅、合思、差旅壹号、Navan 及中国科大差旅平台公开资料。
- 未修改业务代码，未调用真实预订或支付。
- 结论已写入 findings.md，并在本轮回复中按优先级说明。

## 2026-10-22 JEV 意图诊断

- 已读 jev.py、graph.py 接入点、slot_protocol 提示、test_jev.py 和 jev-other-20261019/results.json。
- 未调用真实 JEV，未修改代码。

## 2026-10-22 追问分流实现

- 已修改 jev.py、graph.py、slots.py、test_jev.py 和任务准备架构文档。
- tests.test_jev 与 tests.test_slots 需在本轮结束后复核通过记录。
- 复核通过：tests.test_jev 与 tests.test_slots 共 61 项。

## 2026-10-22 两轴方案核对

- 已按 reference-approach 阅读当前路由，并打开 Rasa CALM 命令生成、对话理解和会话模式文档。
- 未改业务代码。结论：分轴可行；保存和读旧结果在补执行器前必须仍能到达助手节点。

## 2026-10-22 other 边界落地

- 已改 `TourAgent/business_travel/jev.py`、`slot_protocol.py`、`tests/test_jev.py`，并同步两份说明文档。
- 取消、恢复、重试、丢弃不再算 other；保存、确认事实、阅读旧结果仍算 other。
- 验证：在 `TourAgent` 目录运行 `.venv\Scripts\python.exe -m unittest tests.test_jev tests.test_slots tests.test_slot_protocol -q`，90 项通过。未调用真实 JEV。
## 2026-10-22 JEV 问题复查

- 只读核对 `jev.py`、路由提示和 TypeSafe 官方 Choice、Noul、Models 文档。
- 未改代码，未打真实分类请求。

## 2026-10-23 不确定意图落地

- 已按 reference-approach 打开 TypeSafe Confidence、Noul、confidence-routing，以及 Rasa CALM 对话理解文档。
- 已改 `TourAgent/business_travel/jev.py`、`slot_protocol.py`、`tests/test_jev.py`，并同步 JEV 接入说明中的 hint 契约。
- 直接建任务门禁未改。未调用真实 JEV。

## 2026-10-30 JEV 阈值提议审查

- 已读取提议附件、现行 `jev.py`/`settings.py`/`graph.py`/`slots.py`/评测脚本和旧版实测结果。
- 已复算双阈值候选 `(0.8,0.2)`、`(0.6,0.15)` 等组合；未修改业务代码和生产配置。
- 结论待用户确认是否进入 shadow 实验；当前不建议直接改生产阈值。

## 2026-10-30 Token 优化专项审查

- 已读取 `context_budget.py`、`graph.py`、`slots.py`、`slot_protocol.py`、`semantic_review.py`、`planning.py`、`policy_rag.py`、`settings.py`。
- 已确认当前有角色预算、历史压缩、工具结果裁剪和 usage 校准；本轮未改业务代码。
- 已定位主要节省点：JEV 与槽位的重复状态、长固定协议提示词、语义审查额外调用、政策 RAG 多阶段重试、规划重试上下文。
- 已记录一次 `rg` 正则语法错误并改用固定字符串搜索；未重复失败命令。
- 已发现 `.env` 中存在疑似真实外部服务凭据，未在输出中复述值；建议轮换。
