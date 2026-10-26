## 2026-10-19 提交前字段完整性校验第一阶段

目标：把出行和费用核验的基础字段检查前置到语义复核、正式任务创建和队列提交之前；缺字段直接返回 `clarify`，政策解释的证据完整性仍留在政策检索阶段。

- [complete] 在 `slots.py` 中增加合并后 subject 的确定性预检查。
- [complete] 缺字段时跳过语义复核并阻止任务/队列提交。
- [complete] 补齐受影响的槽位测试夹具，增加缺字段、reviewer 调用和字段完整回归。
- [complete] 更新槽位协议提示、README、CHANGELOG 和架构变更文档。
- [pending] 后续阶段实现追问状态和快捷路径；本阶段不包含该功能。

验证：`tests.test_slots` 30/30、`tests.test_slot_protocol` 28/28、`tests.test_travel_graph` 16/16 通过；另通过 Python 编译和差异空白检查。尚未在本阶段宣称完整项目回归或真实供应商联调结果。

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| 一个日期缩写槽位测试的证据引用不在当前消息中 | 1 | 将 `check_in` 引用改为消息中真实出现的 ISO 日期文本；生产逻辑无需修改 |
| 根目录没有可用的系统 `python` 命令 | 1 | 使用项目已有 `.venv\\Scripts\\python.exe` 运行规划脚本和测试 |

## 2026-10-18 行程推荐引用补齐

目标：修复政策问答有引用、行程推荐标准无引用的分支遗漏；沿用正文引用、末尾展开原文的交互。

- [x] 添加推荐分支回归，复现推荐标准缺少来源。
- [x] 从当前政策版本收集规则配置、交通条款和精确住宿附表来源。
- [x] 推荐标准绑定对应引用，保留旧结果兼容与缺失来源降级。
- [x] 定向回归和行程推荐的桌面/移动浏览器验收。

验证：264 项相关组合测试通过，追加最终回复与缺来源边界后定向 50 项通过；4 项前端解析测试通过。推荐分支使用实际工具渲染入口及网页应用，在 1440/390/320 视口完成验收；原政策问答浏览器回归也通过，增加同消息重复编号隔离检查。编译、JS 语法、diff 检查通过。未改真实 .env、政策数据或运行服务。

## 2026-10-18 RAG 修复实施

目标：按已确认问题修复身份映射、审核协议、逐结论证据展示与生产检索评测。保留已有配置、历史实验和规则校验；不引入独立 reranker、不调用真实模型或发布政策。

- [x] 先添加回归，验证三处故障与评测缺口。
- [x] 复用当前版本身份规则，保持派生结果与用户证据分离。
- [x] 拒绝矛盾审核，补齐正文引用和末尾可展开摘录。
- [x] 评测接入生产检索，保留基线模式并明确指标边界。
- [x] 定向回归、前端交互及桌面/移动布局验证，编译与差异检查。

验证：274 项相关 Python 回归、4 项前端解析测试通过；追加 2 项版本边界回归后，新增文件共 17 项通过。浏览器以真实渲染器和前端组件通过 1440/390/320 视口验收，截图位于 `.verification/policy-citations/`。Python 编译、JS 语法及 diff 检查通过。未调用真实模型、检索服务或供应商，未修改数据库、真实 .env，也未重启运行服务。

## 2026-10-18 RAG 全链路复核

目标：确认是否存在语义 rerank，检查政策入库、召回、融合、扩展、上下文预算、生成、引用验证、语义复核和展示。仅诊断，不修改业务代码或调用真实付费服务。

- [x] 核对检索和 RAG 入口、配置及已有评测。
- [x] 追踪全链路并对高风险边界执行离线验证。
- [x] 运行相关回归，记录确认问题与证据缺口。
- [x] 提供带文件定位的结论和修复优先级。

验证：9 个模块 203 项离线测试通过；另以真实图入口、真实规则/资料包及模拟数据库复现身份映射断点、渲染丢引用、矛盾审核放行。未改业务代码、真实 .env、数据库或运行服务。

## 目标
将当前并发准入简化为“会话级后端锁 + 用户级并发 + 单一全局执行容量 + 有界等待”，前端继续在请求期间禁用发送控件；保留 company_id 仅用于学校范围的每日配额与身份隔离。

## 阶段
- [x] 1. 修改配置、资源准入和前端状态说明
- [x] 2. 更新测试与当前文档
- [x] 3. 运行定向回归、静态检查并记录结果

## 决策
- 不删除后端会话锁：前端禁用不能防多标签页、重复请求和直接 API 调用。
- 删除 company concurrency 槽位及其配置校验；global concurrency 成为唯一系统执行容量。
- 暂不删除 company daily quota：单校部署仍需要学校级每日配额。

## 2026-10-08 本次检查

- [x] 确认项目根目录、技术栈、入口和 Git 工作区状态
- [x] 追踪 Web/CLI、主图、槽位、政策和资源流程
- [ ] 完成 discover 回归和关键风险核验
- [ ] 输出结构、流程和问题总结

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `tests.test_graph` 不存在；`test_preference_autosave` 找不到顶层 `test_travel_graph` | 1 | 改用项目的 unittest discover 入口，并为依赖 tests 顶层模块的测试设置 `PYTHONPATH=tests` |

## 2026-09-22 项目流转分析
目标：解释用户输入一句话后，项目从入口、理解、校验、执行、持久化到返回结果的完整状态流转。

阶段：
- [x] 1. 识别项目入口、工具链和现有架构
- [x] 2. 追踪 Web/CLI 单轮请求的调用链
- [x] 3. 追踪槽位、主图、规划/政策分支和持久化状态
- [x] 4. 汇总正常、追问、确认、失败、恢复等状态
- [x] 5. 形成带文件定位的用户说明

范围：只读分析，不修改业务代码；仅更新本分析所需的规划记录。

## 2026-09-27 测评候选首批审核

目标：核对 15 条已有标注草稿与原始输入、工程证据和当前协议的一致性，形成明确的审核建议；未经用户确认不冒称人工批准或正式 gold。

- [x] 1. 核对现有 52 条基线、7 条记忆试跑和 88 条候选的状态
- [x] 2. 审查 15 条草稿的输入、预期、来源、迁移标志及未决问题
- [x] 3. 形成可机判、需语义评估与需人工裁决的首批审核报告
- [x] 4. 验证报告与现有审核流程兼容，记录后续执行入口

结果：`evals/review/agent_audit_2026-09-27.md`；11 条可进入开发试跑，4 条需修订。所有候选继续 pending，须由用户确认后才能提升为人工批准。

## 2026-09-27 全量测评集预期草稿

目标：为 88 条候选逐条提供可追溯、可审核的预期结果草稿和评分边界；不改变人工审核状态，不执行或上传 Langfuse。

- [x] 1. 核对候选来源、既有 15 条草稿、重复与缺失前置条件
- [x] 2. 为剩余候选补逐轮行为预期与可观察评分点
- [x] 3. 验证 ID 覆盖、引用、审核门槛与原有测试
- [x] 4. 汇总需人工确认的边界及下一步运行顺序

结果：`evals/review/expected_drafts.jsonl` 覆盖 88 条/230 个用户轮次；`expected_review.md` 提供审核导航；16 组相同输入、4 条需修订。全部审批仍 pending，未触碰 Langfuse 或真实 Agent。

## 2026-09-27 重复案例与待修订场景续审

目标：保留原始候选与 pending 审批，给 16 组相同输入提供可执行的去重计分建议，为 4 条问题案例提出独立修订方案。

- [x] 核对当前审核文件、候选来源和工作区状态；现有大量用户未提交改动一律保留。
- [x] 编写重复组清单及运行结果共享边界。
- [x] 编写四条逐案修订提案，区分自然语言、预置状态与故障注入。
- [x] 校验覆盖、来源、状态不变并确定首批开发试跑入口。

本次仅新增审核辅助文档，不自动批准候选，不启动真实供应商或上传 Langfuse。初次 `git status` 遇到 dubious ownership，改为命令级 `-c safe.directory=...`，未修改全局配置。
结果：`evals/review/duplicate_and_revision_proposals_2026-09-27.md`；16 组输入配对覆盖、4 条修订提案，14 项定向单测通过。人工审批与真实 Agent 回放仍未发生。

## 2026-09-27 真实 Agent 试跑与 Judge

目标：用户确认纳入受控场景，并要求实际运行 Agent 与引入 LLM-as-judge。先用现有隔离回放入口验证配置和一条真实模型调用，再分批运行可复现样例；确定性硬门禁与语义 Judge 分开，不将 pending 草稿宣称为 gold。

- [x] 核对模型/Langfuse/数据库配置、执行入口和可用试跑夹具。
- [x] 复用七条记忆案例真实图入口，新增独立的回答忠实度 Judge。
- [x] 七条隔离真实图试跑并上传 Langfuse；硬断言六过一败，v3 Judge 七条均已评分。
- [x] 远端 Trace 分数回读核对，文档和定向测试；88 条候选尚未逐例审核初态/政策版本，仍不是正式 gold，未整体回放。

备注：首次无网络权限为 ConnectError，改用授权的外部网络执行；Judge v1 将已删除偏好误读为仍存在，v2 混淆任务完成与回复忠实度，最终使用单独的 v3 指标保留历史痕迹。下一阶段须为 88 条候选建立分轨逐轮回放夹具，不得把七条试跑冒充全量。

## 2026-09-28 记忆案例拒绝与删除回复定位

目标：定向复现 `explicit_add` 的拒绝来源，核实 `explicit_delete` 的错误回复链路；以最小范围验证，保留已有测评与用户工作区改动。

- [x] 1. 核对隔离回放入口、审核分支与现有测试夹具。
- [x] 2. 取得 `explicit_add` 本轮三次审核决策证据；旧实验缺少内部决策，旧拒绝原因尚不可确认。
- [x] 3. 核实删除回复的具体渲染路径与回归覆盖；当前测试未断言删除成功时的回复语义。
- [x] 4. 汇总观察、根因置信度及下一步修复建议。

范围：先诊断再决定是否需要修复；不运行七条或 88 条全量回放，不更改正式金标或历史实验。

结果：当前单案真实图运行 3/3 成功，不能追认昨日拒绝根因；删除回复的通用 saved 渲染问题已直接复现。此轮未修改业务代码或旧实验。

## 2026-09-28 全量开发测评试跑

目标：在不冒称人工金标的前提下，运行已有确定性测评及 88 条自然语言候选的隔离开发试跑，逐例呈现结果、不可运行原因和覆盖率。

- [x] 核对现有执行入口、候选前置条件、模型及政策/供应商隔离边界。
- [x] 实现可恢复的逐例开发试跑入口与保守评分，保留原草稿和审批状态。
- [ ] 运行可安全执行的样例及已有确定性集，记录未触发、环境阻断与失败。
- [ ] 验证报告完整性、状态口径及关键样例，给出下一阶段 Langfuse/Judge 工作。

边界：独立合成身份和 checkpoint，真实供应商禁止调用；未核实政策/故障初态不作为 gold，禁止把旧历史结果当成本次真实 Agent 结果。

## 2026-09-28 新模型兼容性复核

目标：确认用户替换后的模型能否承载真实 Agent 全量测评。

- [x] 记忆类单例：真实完成，`query_memory` 与 `save_preferences` 成功。
- [x] 旅行路由单例：真实请求到达 Agent，但 `TurnRoute` 强制结构化 Tool Calling 被服务拒绝。
- [x] 判定是否继续全量：暂不继续，避免重复消耗模型额度并批量生成同类阻断。

结论：`gpt-5.6-terra` 当前表现为普通文本/简单工具调用可用，复杂 JSON Schema 与强制 `tool_choice="TurnRoute"` 不兼容或由网关拒绝。它不能作为当前 Agent 的全量测评模型。需要更换兼容模型，或在明确授权后调整结构化调用适配层。

## 2026-09-28 新模型测评批次

目标：使用用户已修复的新模型，重新运行 88 条自然语言候选，保留独立输出并与旧模型结果隔离。

- [x] 运行旅行路由单例冒烟，确认强制 `TurnRoute` 调用可执行。
- [x] 运行 88 条候选的可恢复真实 Agent 批次。
- [x] 汇总执行完成、模型阻断、错误和未运行案例。
- [x] 生成失败案例摘要报告，供人工快速审查。
- [ ] 与预期草稿分离，明确当前结果仍不是正式 gold 或准确率。

边界：继续使用空初态、临时记忆、内存 checkpoint 和禁用真实供应商；Langfuse 上传关闭；不修改 pending 预期草稿。

结果：`.verification/eval-batch-qwen-20260928.jsonl` 完成 88/88 个候选、230/230 个用户轮次；无模型连接/鉴权/余额/限流阻断。运行记录中 151/230 轮出现槽位或结构化校验错误，57/230 轮出现语义复核失败，69/230 轮记录 `slot_validation_failed`。这些是观察结果，不是准确率。

摘要：`.verification/eval-batch-qwen-20260928-failure-summary.md`；生成器为 `evals/summarize_real_agent_batch.py`，已通过 Python 编译检查。

## 2026-09-28 RAG 召回专项评测

目标：在不改变线上检索行为、不把工程回归通过率当作真实召回质量的前提下，建立真实政策库的 RAG 检索评测。

- [x] 1. 核对现有 RAG 契约、真实检索入口和当前配置
- [ ] 2. 读取已发布政策版本与条款，生成可审核的 RAG 查询/gold 草稿
- [ ] 3. 实现只读双路召回与 RRF 评测脚本
- [ ] 4. 执行评测，计算 Recall@1/3/5、MRR、版本混用率和引用覆盖
- [ ] 5. 更新报告、定向测试和后续 Langfuse 接入说明

边界：自动生成的 gold 只能称为 `draft`，需要人工确认后才可作为正式指标基准；评测只读数据库，不发布或修改政策版本。

错误记录：直接执行 `python evals/rag_retrieval_eval.py draft` 时未自动加入项目根目录，报 `ModuleNotFoundError: business_travel`；改用模块入口执行。

## 2026-09-28 独立 Ragas 召回评测

目标：在不接入 Agent 主流程、不重复调用 Embedding 服务的前提下，使用 Ragas 对已记录的向量、关键词和 RRF 结果做 ID-based 召回评测。

- [x] 新增独立脚本 `evals/ragas_retrieval_eval.py`
- [x] 读取 `.verification/rag-retrieval-20260928.jsonl` 并一次性评测三条检索路线
- [x] 输出 `.verification/ragas-retrieval-20260928.json`
- [x] 运行 Python 编译、RAG 定向测试和预期数据测试
- [ ] 人工审核 24 条 draft gold，并将同条款/同住宿行相关分块标成相关集合
- [ ] 补齐 Agent 最终回答和人工 reference answer 后，再运行语义版 Context Precision/Recall、Faithfulness 和 Answer Relevancy

结果：Ragas ID-based context recall 三条路线均为 1.0；RRF ID-based precision 为 0.2，主要因为每条 RRF 返回 5 个 chunk，而当前 draft gold 每条只标 1 个 chunk。该数值只反映当前 ID 标注口径，不作为正式质量结论。

## 2026-09-28 Ragas 标注入口

- [x] 增加 `--gold-field`，支持读取人工审核后的相关 chunk 集合
- [x] 增加 `--require-reviewed`，禁止 pending 草稿冒充正式 benchmark
- [x] 生成 24 条 RAG 人工审核模板
- [ ] 审核 `relevant_chunk_ids` 后重跑正式 ID-based 指标

## 2026-10-08 意图链路简化

目标：不接入 Jev，先使路由结果成为本轮唯一意图来源；领域提取只产出槽位，语义复核只检查证据与歧义，代码层维持任务状态和安全校验。

- [x] 1. 核对路由、领域提取、语义复核、任务协调协议及测试
- [x] 2. 补多意图输入边界测试；复用已有否定/多轮/失败恢复回归
- [x] 3. 收窄提取输入、reviewer schema 与 prompt，保留机械安全门禁
- [x] 4. 定向及离线回归，记录剩余边界

边界：保留 subject/revision/证据/确认门禁；不接入外部服务、不运行真实供应商；保留未提交改动。

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| 首次计划文件补丁上下文不匹配 | 1 | 确认文件末尾并以实际内容追加 |
| `test_policy_requirements` 顶层测试模块导入失败 | 1 | 按项目布局设置 `PYTHONPATH=tests` 后重跑 |
| 本轮新增测试改动使既有测评源码哈希不匹配 | 1 | 新测试移至独立文件；旧引用仍与当前未跟踪测试文件不符，未改动金标或放宽校验 |
| 对照实验缺少生产路径的可选重试反馈键 | 1 | 领域提取仅在键存在时透传；65 项相关测试通过 |

结果：路由是唯一意图来源，领域阶段仅收取分配的任务目标与提取上下文，语义复核不再重选意图。业务请求仍有路由、领域提取、语义复核三次模型调用；本阶段仅降低职责重复和输入体积，尚未用真实中文测评验证质量或实测 token 降幅。

## 2026-10-08 Langfuse 原生 Token 用量

目标：让 Langfuse 识别每个 `chat_model` 生成节点的原生 Token 用量，并支持同一 Trace 的用量汇总。

- [x] 1. 核对现有观测封装、SDK `4.15.2` 接口和测试替身
- [x] 2. 将标准 `usage_metadata` 映射到 `usage_details`
- [x] 3. 运行定向测试、编译检查并更新结果记录

范围：不改变业务流程，不上传额外正文，不配置或修改模型价格。

结果：观测定向测试 26 项、上下文/脱敏回归 38 项通过；Python 编译和 `git diff --check` 通过。新 Trace 的 `chat_model` 节点会带原生 `usage_details`，需重启服务后生效。
## 2026-10-09 回复模板层整理

目标：保留确定性模板回复，不增加最终回复模型调用；将本轮 ToolMessage 到用户文案的总调度从 `graph.py` 拆出，集中管理工具渲染器和状态文案，保持现有业务输出与安全边界不变。

- [ ] 1. 新增统一回复渲染入口并迁移总调度逻辑
- [ ] 2. 增加渲染分发和历史回复隔离回归测试
- [ ] 3. 运行定向测试、编译检查和 diff 检查

范围：
- 保留 `render_policy`、`render_draft`、`render_weather` 等领域模板；
- 不修改意图识别、槽位提取、语义复核和业务工具；
- 不新增模型调用或第三方依赖。

完成情况：
- [x] 新增 `business_travel/reply_renderer.py`，统一工具结果渲染入口；
- [x] `graph.py` 保留 `finalize_business_reply` 兼容导出，业务调用方无需改动；
- [x] 新增 `tests/test_reply_renderer.py`，覆盖分发、回退、缺字段和状态隔离；
- [x] 模板、主图、流式输出和 Web 回归通过。

验证：
- 模板及业务渲染相关：53 项通过；
- 主图、流式输出和 Web 会话：52 项通过；
- 偏好、标签、事实上下文：46 项通过；
- `compileall`、`git diff --check` 通过。

错误记录：
- 首次组合测试未设置 `PYTHONPATH=tests`，导致既有 `test_policy_finalization` 顶层导入失败；补齐项目既有测试路径后 53 项全部通过。

## 2026-10-09 JEV 接入

目标：把 JEV 接入现有旅行助手的第一层意图理解，并在不绕过原有安全门禁的前提下记录 JEV 用量。

阶段：
- [x] 增加 JEV 配置和独立 HTTP 适配器。
- [x] 将 JEV 白名单分类转换为首轮本地 `TurnRoute`，跳过首轮 `TurnRoute` 模型调用。
- [x] 增加失败降级、shadow/primary 模式和适配器回归。
- [ ] 填入真实 Key 后做一次受控联调，核对 JEV 响应和 Langfuse usage。

边界：JEV 只主导首轮新请求的任务意图路由；不直接写槽位、不直接触发供应商、不替代证据引用、语义复核或确认门禁。已有任务、槽位或待办控制自动回退本地 `TurnRoute`。

## 2026-10-12 主助手日期后移

目标：固定主助手规则前缀，把当前日期作为每次模型调用的动态上下文；保留原有行为及后续实验可比性。

阶段：
- [x] 检查提示词组装、现有测试和未提交改动。
- [x] 修改日期放置位置并加入回归测试；增加 prefix/suffix 对照开关。
- [x] 运行定向测试与差异检查。

验证：55 项相关 unittest 通过；compileall 与 git diff --check 通过。

错误记录：首次组合测试未设置 `PYTHONPATH=tests`，现有 `test_settings` 无法导入顶层 `test_tuniu`；按既有测试路径重跑通过。

## 2026-10-15 Candidate selection input compaction

Scope: compact only the planner's model input; preserve full candidates, validation,
supplier data, and the full-input baseline. No provider calls or service startup.

- [x] Inspect planner, settings, existing changes, and test tooling.
- [x] Implement conservative projection and full/compact configuration.
- [x] Test projection, planner preservation, configuration, and existing guards.

Errors: initial test-file lookup used test_planning.py; the existing suite is
test_planning_stages.py. Use the existing .venv and PYTHONPATH=tests.
Supplemental suite referenced nonexistent test_plan_checks; replace with the existing
test_plan_presentation and test_hotel_details modules, keeping all assertions.

Verification: corrected combined suite passed 158 tests; compileall and
git diff --check passed. Real sample projection preserved all IDs and candidate
counts. No live provider/model calls or server restart; token savings need online
A/B measurement.

## 2026-10-15 Candidate compaction documentation

- [x] Verify documentation rules against current projection/configuration code.
- [x] Write scoped Chinese implementation, sample, limitations, and A/B guide.
- [x] Validate document JSON examples, code references, and targeted regressions.

Verification: document JSON parses and compact example exactly matches implemented
projection; 27 targeted tests and git diff --check passed.

Errors: supplier doc lookup used docs/tuniu.md; actual file is docs/tuniu-mcp.md.
No supplier documentation changes are needed.

## 2026-10-16 规划查询并行化

目标：降低独立供应商查询串行等待造成的规划响应时间，同时保留串行模式用于基线对比。

- [x] 核对规划阶段、供应商适配器和观测上下文的并发边界
- [x] 增加 `serial/parallel` 配置并实现有界并发查询
- [x] 增加并行顺序、串行基线和配置校验测试
- [x] 更新说明并运行定向回归、编译和 diff 检查

决策：
- 默认使用 `parallel`；`TRAVEL_PLANNER_QUERY_MODE=serial` 保留原行为用于 A/B 实验。
- 只并发同一规划阶段中彼此独立的供应商查询，不并发候选选择、政策检查或通勤检查。
- 结果按原 `search_stages()` 顺序合并，避免改变候选选择输入和输出顺序。
- 每个查询继续隔离异常，并保留独立 Langfuse 观测和耗时。

## 2026-10-16 酒店查询有结果但方案缺少酒店诊断

目标：基于用户截图和当前代码，确认酒店候选在哪个阶段没有进入最终方案，区分查询返回、选择模型、确定性校验和结果渲染问题。

- [x] 读取诊断与持久化计划规则，建立只读证据边界
- [x] 确认项目路径、现有计划文件和酒店规划主链路
- [ ] 读取 `tuniu.py` 酒店详情归一化与售置信息
- [ ] 用最小夹具/现有测试复核候选保留、选择失败和方案缺项行为
- [ ] 汇总证据、给出根因置信度和需要的下一次运行字段

### Errors Encountered

| Error | Attempt | Resolution |
|---|---|---|
| `business_travel/uniu.py` 不存在 | 读取供应商详情 | 项目实际文件名为 `business_travel/tuniu.py`，未修改业务文件 |

# 2026-10-17 推荐阶段接入差旅标准

目标：在推荐阶段提前使用已发布差旅政策的交通席别和住宿限额，优先推荐标准内候选；没有标准内候选时保留并明确标注超标准备选。统一用户输出层级，将标准说明靠近推荐结果，技术状态和查询条件放在末尾。

阶段：

- [x] 读取现有政策解析、规划选择和渲染链路
- [x] 增加政策推荐画像和候选标准状态
- [x] 将标准状态接入模型选择、代码校验和酒店兜底
- [x] 调整用户输出并补充回归测试
- [x] 运行定向测试、编译检查和 diff 检查

决策：

- 不删除任何供应商候选；只改变主推荐排序和选择约束。
- 酒店标准比较使用政策库返回的住宿限额和当前推荐报价；标准库不可用时不伪造合规结论。
- 有标准内且有库存的酒店时，超标准酒店不能进入主推荐选择池，但保留在备选中。
- 没有标准内酒店时，主结果明确提示未找到标准内酒店，并可展示价格最接近的超标准候选。
- 交通候选按当前政策允许席别参与推荐；超标准交通保留为备选。

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| 项目根目录的 `python` 命令不可用 | 1 | 使用 `TourAgent\\.venv\\Scripts\\python.exe`，未改变环境配置 |

## 2026-10-17 推荐阶段接入差旅标准完成

- [x] 提前读取已发布政策的推荐画像，包含交通允许席别、住宿档次和城市/日期住宿上限
- [x] 为每个候选生成 `within_standard`、`over_standard`、`unknown` 或 `unverified` 状态
- [x] 检索结果在 `max_candidates` 截断前按库存和标准状态排序
- [x] 模型选择池按“标准内 → 未知/未核实 → 超标准”分层；超标准候选仍保留在 `alternatives`
- [x] 保留有库存酒店的强制选择和代码兜底；售罄酒店不强行补选
- [x] 结果顶部展示具体交通席别和住宿限额；标准说明、超标准备选和查询条件分层展示
- [x] 增加标准内酒店优先、无标准内酒店保留超标准备选、交通席别优先三组回归测试

验证：`tests.test_planning_stages`、`tests.test_plan_presentation`、`tests.test_query_contracts`、`tests.test_university_policy` 共 88 项通过；相关图、标签、候选输入测试 31 项通过；`compileall` 和 `git diff --check` 通过。
# 2026-10-16 酒店选择完整性修复

## 2026-10-18 推荐输出修正

- [x] 复现酒店估算表述、默认人数缺失和未知席别误判。
- [x] 实际查询写入单人默认值并记录默认来源；保留明确团体输入。
- [x] 推荐结果优先，具体标准随后，解释和默认条件放在末尾。
- [x] 正文展示有库存的真实房型、床型、早餐与参考报价，不假定每晚计价。
- [x] 未映射席别保留为待核实，并验证正常允许/超标席别。
- [x] 定向回归 199 项通过，编译和差异检查通过。

证据边界：本轮使用离线夹具验证，不重放供应商请求或重启服务。

## 目标

修复已查询到可用酒店但模型漏选酒店的问题，保留交通选择、部分结果和候选替代项，支持后续 A/B 对比。

## 阶段

- [x] 梳理现有候选选择与校验流程
- [x] 增加酒店可用性排序并降低候选噪音
- [x] 增加酒店必选重选约束
- [x] 添加回归测试并运行验证

## 结果

- 可用酒店优先进入现有 `max_candidates` 截断范围。
- 请求酒店且存在可选酒店时，模型漏选会触发一次后续选择尝试。
- 售罄/不可用酒店不会触发强制补选。
- 未改变完整候选结果的保存方式，也未删除原有兜底路径。
# 2026-10-18 精确政策引用与证据去重

## 2026-10-18 费用报销金额测算

- [x] 核对制度原文、事实契约、政策核验和真实引用链路。
- [x] 增加可切换的金额测算，明确实际/假设、逐晚/票价和例外边界。
- [x] 接入现有对话和结果展示，保留旧核验路径。
- [x] 验证普通住宿、多晚住宿、交通、缺项、超标和引用，并记录结果。

用户确认范围：以当前已发布制度真实条款为依据；本轮不改变语义复核策略，不读取真实 .env、不发布政策或重启现有服务。

结果：新增测算默认模式 estimate，legacy 保留原核验；445 项定向测试、4 项展示解析、桌面/手机 Chrome 引用与截图验证、编译及默认 Git 差异检查通过。全量测试另有 6 处已记录的历史评测资料问题，未改写评测证据或声称全量通过。真实模型、真实数据库和现有服务未调用或重启。

## 2026-10-18 Qwen rerank API 接入

- [x] 确认生产召回、RRF 截断点和既有工具链。
- [x] 核实官方接口，添加可配置 API 客户端和安全回退。
- [x] 接入扩大的 RRF 候选池，保留精确限额和关联条款补齐。
- [x] 补充环境变量示例、对照评测和离线回归验证。

边界：不读取真实 .env、不调用付费接口、不重启用户服务；保留旧 RRF 基线。

结果：客户端、RRF 扩池重排、预算、Langfuse 元数据、评测开关和文档已完成；292 项定向回归、compileall、git diff --check 通过。

目标：推荐结论绑定独立证据片段，共享编号，隐藏内部 JSON，区分项目口径与制度原文；保留旧展示实验入口。

- [x] 恢复计划并核对重复的静态原因：整组条款绑定、整组去重、配置 JSON 直出。
- [x] 使用真实资料包离线夹具补充并复现重复、无关条款与来源类型问题。
- [x] 实现精确绑定、片段级注册、项目口径投影和基线开关。
- [x] 验证后端引用、来源缺失与版本边界、前端点击与移动端展示。

边界：不读取真实 .env，不调用付费模型或供应商，不发布政策，不重启用户服务。
错误记录：一次搜索误用根目录 web_static，实际目录为 business_travel/web_static；已纠正。
回归记录：新旧编号因片段拆分不同，更新固定编号断言；旧 standard_citations 非法时，新增片段路径也拒绝引用，保持来源校验边界。
验证：277 项相关 Python 测试、4 项前端解析测试通过；行程推荐与政策问答浏览器验收通过（1440/390/320），含共享引用、项目口径、编号隔离、移动端布局和安全文本。编译与 JS 语法检查通过。首次 diff 检查发现本轮计划文件末尾空行，已移除。

## 2026-10-18 用户引用仅展示真实条款

- [x] 恢复上下文，确认当前默认仍把 transport_rule/lodging_rule 注册为用户来源。
- [x] 先用回归复现并去除默认展示中的项目配置引用，保留后台数据和 grouped 实验基线。
- [x] 住宿档次缺少原文绑定时不伪造引用，必要推荐假设置于末尾简短说明。
- [x] 验证共享引用、来源类型、学生适用条件和桌面/移动端点击。

边界：沿用离线夹具，不操作真实 .env、数据库、模型、供应商或运行服务。

验证：先复现两项失败后完成修改；最终 282 项相关 Python 测试、4 项前端解析测试通过；推荐和政策问答浏览器验收通过（1440/390/320），推荐默认仅展示两项真实来源，手机版截图已查看。编译、JS 语法、差异检查通过。旧 grouped 对比模式继续保留。

# 2026-10-18 HNSW 运行时切换计划

## 目标

将 PostgreSQL policy RAG 的向量召回从仅精确排序切换为可实际命中的 HNSW 余弦索引，同时保留索引缺失时的显式精确回退和离线基线验证。

## 阶段

- [in_progress] 1. 设计索引生命周期、配置和查询回退
- [pending] 2. 实现 HNSW DDL、初始化/导入/迁移入口与查询路径
- [pending] 3. 补充离线测试和文档
- [pending] 4. 运行定向回归、编译和差异检查

## 约束

- 保留当前用户未提交改动，不执行破坏性 Git 操作。
- 不连接真实数据库、Embedding、重排服务或付费模型。
- HNSW 使用 `vector_cosine_ops`，查询保持余弦距离；精确路径保留为索引缺失时的回退和评测基线。
- 所有 HNSW 参数经配置/环境变量传入，维度与 Embedding 配置一致。

## 已观察事实

- `policy_pg.py` 当前向量 SQL 使用 `WITH scoped AS MATERIALIZED`，会阻止全局 HNSW 索引直接承担排序。
- `policy_schema.sql` 当前没有向量索引；`scripts/policy_hnsw_ddl.py` 只输出按文档 partial index SQL。
- 当前 Embedding 默认维度为 1024；查询使用 `<=>`。

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| planning session-catchup.py 被系统拒绝启动 | 1 | 已读取现有项目计划并在项目根追加本轮 HNSW 阶段；不重复执行同一失败命令 |

## 阶段结果

- [complete] 1. 设计索引生命周期、配置和查询回退
- [complete] 2. 实现 HNSW DDL、初始化/导入/迁移入口与查询路径
- [complete] 3. 补充离线测试和文档
- [complete] 4. 运行定向回归、编译和差异检查

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| planning session-catchup.py 被系统拒绝启动 | 1 | 读取并沿用现有项目计划，在项目根追加 HNSW 阶段 |
| 直接运行 `tests.test_settings` 缺少 `test_tuniu` | 1 | 按项目既有方式设置 `PYTHONPATH=.;tests` 后重跑通过 |

# 2026-10-18 政策意图路由修复计划

## 目标

修复“我是院士去北京出差，住宿标准是多少”被路由为 `policy_check`、因缺少费用日期而跳过 RAG 的问题，让明确的政策解释型表达进入 `policy_explain`，同时保留实际报销核验的 `policy_check`。

## 阶段

- [in_progress] 1. 复现并锁定 JEV/本地路由边界
- [pending] 2. 增加解释/核验语义保护规则
- [pending] 3. 补充回归测试并验证政策链路

## 约束

- 不改变已有任务更新、历史任务和跨领域路由。
- 只对空白首轮且 JEV 给出 `policy_check` 的新请求做窄范围纠偏。
- 含“报销、能否报、是否超标、这笔费用、发票”等实际核验信号时保持 `policy_check`。
- 不调用真实 JEV、LLM、数据库或 Langfuse。

## 已观察事实

- 当前截图输出为“政策核对结果”并要求“费用发生日期”，对应 `consult_policy` 的 `policy_check` 分支。
- `policy_rag.explain_policy` 只在 `policy_explain` 分支调用；因此当前请求在 RAG 前被校验短路。
- JEV primary route 会直接注入新任务意图，绕过普通 TurnRoute 的重新判断。

## 阶段结果

- [complete] 1. 复现并锁定 JEV/本地路由边界
- [complete] 2. 增加解释/核验语义保护规则
- [complete] 3. 补充回归测试并验证政策链路

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `functions.exec` 脚本两次因参数引号拼写错误未执行 | 1-2 | 修正 JS 调用后继续；未重复失败命令 |

# 2026-10-18 政策意图描述精细化计划

## 目标

先通过更精确的意图定义降低 JEV 将政策解释问题误判为具体核验的概率，并保留本地规则作为兜底。

## 阶段

- [complete] 1. 为意图定义建立失败回归测试
- [complete] 2. 增加 policy_check/policy_explain 的目标、边界、反例和优先级
- [complete] 3. 运行 JEV 与政策链路定向回归

## 结果

- `policy_check` 明确针对具体或拟发生费用/情形的报销、超标、合规和审批判断。
- `policy_explain` 明确针对标准、限额、适用条件、规则含义、条款原文和政策依据。
- 同时出现制度询问和具体费用核验时，JEV instructions 指定核验优先。
- 102 项定向回归通过，未连接真实 JEV、数据库、Embedding、Rerank 或 Langfuse。

## 后续实验

- 已按用户要求移除本地关键词纠偏，当前分类完全依赖 JEV 的精细定义。
- 真实服务重启后应使用解释型、核验型和混合型问题各测试一次，再决定是否需要恢复更窄的规则兜底或引入低置信度澄清。

# 2026-10-18 policy_rag 检索失败诊断计划

## 当前状态

- [complete] 通过 Langfuse 截图定位到 `policy_retrieve`，排除意图路由和 Rerank 作为首个故障点。
- [complete] 确认 HNSW 索引创建不等于政策检索链路可用。
- [pending] 获取固定检索错误码，验证数据库、已发布文档、Embedding space/维度和接口连通性。

## 约束

- 不调用真实付费 Embedding、Rerank 或数据库；先由用户从当前运行实例提供只读检索结果。
- 不先改 HNSW、Rerank 或政策数据，避免掩盖真实故障原因。

## 诊断进展

- CLI 已返回 `embedding_request_rejected`，确认故障在查询向量服务。
- 已补充显式 `dimensions` 参数并保留 HTTP 状态码；109 项定向测试通过。
- 重启服务后应重新执行 `policy_admin.py search`，根据 `embedding_request_rejected_http_<status>` 处理 Key、模型、额度或请求参数。

## Embedding 兼容参数修正

- qwen3.7 兼容调用不再无条件发送 `dimensions`；v3/v4 仍可显式发送配置维度。
- 通过 JSON 中安全的 `error.code` 补足 400 的诊断信息。
- 111 项定向测试通过；等待用户重启并重跑真实请求。

## 2026-10-18 全架构复核

- [x] 读取并核对当前项目计划、发现记录、规范与诊断流程
- [x] 读取 Langfuse 17:54 首轮 RAG/Rerank Trace
- [x] 读取 Langfuse 17:57 follow-up 槽位失败 Trace
- [x] 审查意图路由、槽位协议与合并、任务问题、语义审核和主图门禁
- [x] 审查 Embedding、HNSW、关键词召回、RRF、Rerank、RAG 生成和证据预算
- [x] 审查错误包装、Langfuse metadata、JEV 预算与 Web recursion limit
- [x] 运行 315 项定向回归：314 通过，1 项 RAG 缺失条件状态回归失败
- [ ] 输出分级架构结论与实施顺序

## 2026-10-18 全架构复核结论待输出

- [x] 复现定向 RAG 回归并区分全量历史评测失败
- [x] 对齐 Langfuse Trace 与 graph/slots/tasks/policy_pg 代码事实
- [ ] 输出分级整改方案，用户确认后再进入实施

## 2026-10-18 验证收口

- [x] 修复 policy_explain 更新的范围继承与 canonical 查询目标连续性。
- [x] Embedding 失败降级到关键词召回，并保留 RRF、Rerank、来源闭包和可观测错误码。
- [x] 完成身份字段兼容，按审核规则配置映射，不硬编码具体身份或城市。
- [x] 完成定向回归、编译检查、完整 unittest 和真实 CLI 检索验证。
- [ ] 仅剩外部模型网络受限导致的浏览器真实模型回放；需在允许供应商网络的运行环境执行。

| Test failure | 1 | `tests.test_university_policy tests.test_rag_corrections` ran 55 tests; one existing RAG clarify fixture failed because the HNSW/SQL mock path returns `insufficient` before the expected lodging clarification. Identity compatibility test passed. | Pending owner: RAG/HNSW agent |
## 2026-10-18 范围扩展修复验证进展
- [x] 代码回归和编译检查
- [ ] 明确新服务实例并完成真实浏览器跨轮验证
- [ ] 核对新 Langfuse Trace 的 policy_query.coverage=all_regions、完整 facts 和 lodging_table
- 记录的运行时限制：浏览器登录会话已失效；系统拒绝读取 Python 服务命令行。

## 2026-10-18 JEV multi-intent implementation

- [complete] Verify typed questions, answers and existing routing boundaries.
- [complete] Implement independent intent decisions, detailed rubrics and other.
- [complete] Generate scoped tasks and retain existing-task/ambiguous-result fallback.
- [complete] Add adapter, extraction, graph and configuration regressions; run offline checks.

Scope: preserve evidence/review gates and all pre-existing work. No keyword intent
overrides, new dependencies, credential changes or production provider calls.

Errors: parent workspace is not the Git root; use TourAgent. Public docs required
approved network escalation. Initial plan append context mismatch was corrected
after reading each file tail.

Validation: 247 targeted offline tests passed; final JEV wording recheck passed 25/25.
compileall and CRLF-aware diff checks passed. Live JEV classification accuracy and
net token/cost changes remain unmeasured; no live inference or running service restart.

## 2026-10-18 JEV other rubric refinement

- [complete] Check official Noul/question documentation and current request construction.
- [complete] Clarify independent goals and other positive/negative criteria, preserving policy rules.
- [complete] Verify outgoing questions and route regressions; evaluate live synthetic examples if available.

Scope: requested wording change only, with focused validation. Preserve current routing,
thresholds, user changes and dependencies. Do not claim semantic accuracy from mocked answers.

Errors: repository has no local AGENTS.md; follow the user-provided project instructions.
Public documentation access failed with a socket permission error in the sandbox;
the approved elevated request successfully retrieved the official documentation index.

Validation completed on 2026-10-19: 10/10 live intent contrast cases passed with
jev-1.13.0; 172/172 focused offline tests passed; compilation passed. A no-index
whitespace check for the two pre-existing untracked code files found an extra EOF
blank in jev.py, which was removed. Test loader invocation was corrected as recorded
in progress.md; no dependency or production routing changes were needed.

Remaining boundary: no service restart or complete business workflow replay. The
preference case correctly retained both intents but used the existing richer-route
fallback at requires_task_routing=0.21. JEV input tokens for the reported sentence
increased from 2470 to 3617; end-to-end token/cost savings were not measured.

## 2026-10-19 JEV onboarding and evolution document

Goal: create a detailed Chinese Markdown guide for readers without JEV experience,
covering API usage, project integration, historical changes, evidence and limitations.

- [complete] Verify current implementation, history and official API documentation.
- [complete] Write docs/JEV接入原理与多意图改造详解.md with examples and source references.
- [complete] Add a README entry, validate examples/links/facts and record completion.

Scope: documentation only. Preserve pre-existing code and repository changes; no
service restart, dependency installation or provider inference is required. Read
init-project for document conventions without running repository initialization.

Delivered an 18-section guide with reading/evolution tables. Validation passed:
7 JSON examples parsed, 2 Python examples parsed, HTTP teaching example exercised
with a mocked success and missing-key response, 11 source paths/symbols checked,
all 10 saved live-case results and documented configuration defaults cross-checked,
README entry and CRLF-aware whitespace checks clean. No business-code changes or
new live inference this turn. Historical test counts are clearly labeled as records.

## 2026-10-19 追问快捷路径第二阶段

目标：在第一阶段“缺字段不创建任务”的契约上，保存未提交 clarification 草案；下一轮只用 JEV 判断是否回答当前追问，回答后定向补充字段并重新执行字段完整性、语义复核和正式任务创建。

阶段：

- [complete] 增加 `AgentState["clarification"]` 及缺字段草案保存。
- [complete] 增加 JEV 专用 `is_clarification_answer` 判断和阈值门禁。
- [complete] 增加服务器生成的定向草案更新路由，跳过普通 `TurnRoute`。
- [complete] 补齐后重新预检查，完整后转换为 `new_task` 进入原任务队列。
- [complete] 增加 JEV、槽位和主图跨轮回归，更新架构文档。
- [pending] 迁移仍依赖“先创建 needs_clarification 任务”的旧任务生命周期夹具。

设计边界：

- `clarification` 中的 `draft_slots` 是未提交草案，成功前不写入正式 `slots`、`tasks` 或 `task_queue`。
- JEV 只接收追问摘要，不接收完整草案；字段值仍由领域提取，最终仍需 preflight 和 semantic review。
- JEV 否定或不确定时不合并草案；普通新话题成功后清除过期 clarification。
- 当前复用 LangGraph checkpoint，不新增独立追问表。

验证：核心定向回归 `tests.test_jev tests.test_slots` 59/59 通过；原第一阶段回归 `tests.test_slot_protocol tests.test_travel_graph` 通过；编译检查通过。更宽的旧任务生命周期/语义复核测试仍有夹具把缺字段预检查前的任务创建行为当作契约，需要后续迁移，不作为本阶段代码失败结论。

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| 规划脚本无法通过系统 `python` 命令启动 | 1 | 使用项目已有 `.venv\\Scripts\\python.exe`；保留该环境限制记录 |
| 旧生命周期测试期待缺字段任务先进入队列 | 1 | 确认与第一阶段已确认契约冲突；保留代码行为，标记旧夹具待迁移 |

## 2026-10-19 语义复核门禁收窄第三阶段

目标：在字段预检和追问快捷路径之后，收窄语义复核的阻断边界。语义复核继续拦截证据冲突、否定错误、错误归属、未授权目标变化、任务依赖错误和真实歧义；字段完整但只有表达性提醒或非执行性小问题时，不再阻断提交。

- [in_progress] 明确语义复核的 blocking issues 与 warnings 协议。
- [pending] 增加“软问题不阻断、硬问题继续阻断”的回归。
- [pending] 更新架构文档和运行记录。
- [pending] 运行槽位、语义复核、任务生命周期及主图定向回归。

边界：不删除语义复核，不允许 reviewer 绕过机械校验、证据引用、subject/revision、任务授权或政策证据门禁；不改变第一、二阶段的 clarification 状态模型。
## 2026-10-20 意图识别准确率评测

目标：对当前 Jev 意图识别做独立的标注集评测，区分离线协议测试与真实模型准确率，输出逐例结果、总体准确率和混淆统计；不执行交通、酒店或政策查询。

- [complete] 核对现有意图定义、评测样例和真实 Jev 运行入口。
- [complete] 增加独立评测数据/脚本，保留可重复输出和敏感信息脱敏。
- [complete] 完成离线校验；真实 Jev 运行因 HTTP 451 被服务端拒绝，未得到可计分答案。
- [in_progress] 交给用户在本地可用网络环境运行，并根据报告汇总准确率和误分类样本。

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| 系统 `python` 命令不可用 | 1 | 改用项目已有 `.venv\\Scripts\\python.exe`；不改系统环境 |
| Jev 请求返回 HTTP 451 | 1 | 标记为 blocked，不计入准确率；提供本地运行命令由用户在可用网络环境执行 |

## 2026-10-20 意图评测上下文修正

目标：避免把依赖“刚才/已保存/失败任务”的多轮样本错误地评成无上下文单轮请求，同时保留旧报告作为基线。

- [complete] 检查生产 `prepare()` 传给 Jev 的会话、槽位和待处理任务字段。
- [complete] 评测脚本支持每条 Case 可选携带 `conversation`、`current_slots`、`task_context`、`pending_tasks`、`pending_turns` 和 `clarification`。
- [complete] 给历史结果、取消、重试、查看已保存行程等样本补最小上下文。
- [complete] 保留 `intent-accuracy-100.json` 作为修改前报告；上下文修正后的数据需要重新运行才产生新指标。
- [complete] 标记“已返回政策结论是什么意思”存在 `other` 与 `policy_explain` 的人工标注边界争议。
- [complete] 完成 JSONL 加载、编译和差异检查；本阶段不重新调用 Jev。

边界：本阶段只修正评测条件，不修改 Jev 意图定义，不把新的离线数据集当作已验证准确率。

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| 外层目录不是 Git 根目录 | 1 | 切换到 `TourAgent` 实际仓库继续操作 |
| 系统 `python` 命令不可用 | 1 | 使用项目已有 `.venv\\Scripts\\python.exe` |
| 读取评测报告时按错误的顶层结构访问字段 | 1 | 按报告实际的 `summary` / `cases` 结构读取 |
| 上下文样本补丁中出现重复 JSON 键 | 1 | 删除重复键并重新通过 JSONL 加载校验 |
