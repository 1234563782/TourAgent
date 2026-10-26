# 2026-10-08 项目总览

## 2026-10-19 提交前字段完整性校验第一阶段

- 完成 `business_travel/slots.py` 的预检查接入：合并事实后、语义复核前校验 `plan_trip` 和 `policy_check`。
- 完成 `clarify` 分支：缺字段时返回字段列表和中文追问，不生成 slots、正式 tasks 或 task queue。
- 更新 `slot_protocol.py`，明确提取阶段可以返回部分 patch，但提交前必须满足执行契约。
- 补齐 6 个受新门禁影响的旧测试夹具，并新增 3 个回归测试。
- `tests.test_slots` 运行结果：30/30 通过；`tests.test_slot_protocol` 28/28、`tests.test_travel_graph` 16/16 通过。
- `compileall` 和 `git diff --check` 通过。
- 新增 `docs/任务准备架构与提交前字段校验.md`，同步 README 和 CHANGELOG。
- 后续待办：追问状态、原任务快捷路径、语义复核门禁放宽；本轮未实现。

## 2026-10-18 RAG 链路复核

- 已读取 RAG 入库、检索、融合、重排、上下文、生成、引用校验、语义复核、渲染和评测代码。
- 定向 182 项 RAG 回归通过；全量离线 1051 项中 90 项跳过、2 项失败、5 项错误，错误包含 RAG 草稿被通用评测加载器误读及既有来源哈希漂移。
- 未修改业务代码、数据库、`.env`，未调用真实付费服务。
- 结论按 P1/P2 风险整理：默认精确向量扫描的扩展性、pending gold 可进入生产评测、同模型语义复核的共同错误、历史政策按日期回溯缺口、`_active()` 歧义检查不可达。

- [x] 确认项目根目录、Git 状态和现有规划记录。
- [x] 读取 README、依赖清单和环境变量模板。
- [ ] 读取核心入口、主图、槽位/任务/政策/资源模块。
- [ ] 运行定向静态检查与测试。
- [ ] 汇总业务流程和风险。

## 2026-10-08 意图链路简化收尾

- 路由意图只由 `TurnRoute` 决定；领域提取不再接收完整路由、结果清单和待执行清单；复核 schema 不再包含路由 schema，复核提示词聚焦证据、归属、遗漏和歧义。
- 新边界测试独立在 `tests/test_intent_boundary.py`，未更新测评数据的历史源码哈希。修复隔离对照脚本缺少可选重试反馈键导致的 `KeyError`。
- 定向生产链路 154 项通过；对照实验及协议 65 项通过。最终全量离线 939 项，90 跳过、1 失败、5 错误；5 项受历史测试源码哈希漂移影响，另 1 项 RAG 草稿数据集扫描失败。`compileall` 和 `git diff --check` 通过。
- 未调用真实模型或供应商；语义质量、调用 token 和延迟仍需独立的受控测评，不能从离线协议测试推断提升。

## 2026-10-08 验证记录

- `travel_cli.py --check-config` 通过；当前 `.env` 使用 Tuniu `apiKey`、pgvector、PostgreSQL 记忆和 Langfuse 配置，但未验证外部授权/连接。
- `pip check` 通过。
- `compileall` 通过。
- 首次定向 unittest 命令收集失败：模块名写成 `tests.test_graph`，且 `test_preference_autosave` 依赖 `tests` 作为顶层导入路径；尚未判定为业务失败。

# 2026-09-22 并发模型简化

- 读取现有计划、资源实现、前端控件、测试和配置。
- 已确认前端发送期间已有禁用逻辑，本次重点修正后端重复的 company/global 并发层，并补充无误导的配置与文档。

# 2026-09-22 并发模型简化实现

- [x] 删除公司/学校并发槽位，仅保留用户级并发和全局执行容量。
- [x] 保留 PostgreSQL 会话锁、request_id 幂等、等待槽位和学校级每日配额。
- [x] 前端保留请求期间禁用输入/发送控件，并增加 `aria-busy` 状态。
- [x] 更新资源测试和中文配置文档。
- [x] 离线资源单测 7 项、Python 编译检查和两个前端 JS 语法检查通过。
- [ ] PostgreSQL 集成测试待在 `TRAVEL_TEST_POSTGRES=1` 环境执行；本轮未主动打开数据库测试开关。
- 完整离线回归：902 项通过，90 项按环境跳过；`git diff --check`、Python 编译和 Node JS 语法检查通过。

# 2026-09-22 项目流转分析

- 已确认项目根目录为 `TourAgent/`，依赖管理为 Python `requirements.txt` + `.venv`。
- 已确认主要用户入口为 `travel_cli.py` 和 `business_travel.web:create_app`。
- 已确认当前架构以 LangGraph 为主，Web 层还包含认证、会话、流式输出、任务和资源准入。
- 下一步沿 Web 单轮请求和 LangGraph 主图读取调用链。

# 2026-09-22 项目流转分析完成

- [x] 已定位 Web/CLI 入口和请求前置保护：身份、会话锁、幂等、资源准入、运行账本。
- [x] 已定位 LangGraph 主图：`prepare -> dispatch -> assistant/tools -> advance -> finish`。
- [x] 已定位槽位提交协议：路由、领域提取、证据/版本校验、独立语义复核、原子提交。
- [x] 已整理规划、政策解释、政策核验、确认写入、普通对话和失败恢复分支。
- [x] 已整理四类状态：`chat_runs` 请求状态、任务状态、事项 revision/validation、checkpoint/interrupt。
- [x] 已确认无需启动服务；本轮目标是静态理解项目流转，不修改业务逻辑。

# 2026-09-27 测评首批审核

- 已读取 15 条标注草稿、原始输入与所引工程测试；保留现有审核决策 pending。
- 已识别旧结果、歧义目标和 Reviewer 拒绝三类需要修订场景；下一步形成逐条建议与评分边界。
- 已写 `evals/review/agent_audit_2026-09-27.md`：15 条全部覆盖，11 条可开发试跑、4 条应先修订；硬门禁、Judge 与人工复核分工明确。
- `tests.test_eval_annotations` 与 `tests.test_eval_review` 共 11 项通过；报告 ID 与 15 条草稿逐条一致，88 条候选依旧 pending。
- 未变更审批文件或 Langfuse 数据集；待用户确认预期后再绑定回放适配器，不把预审冒充 gold。

# 2026-09-27 全量测评集标注

- 已读取候选标注生成器、审核校验器和首批 15 条预审意见；计划只补草稿与审核辅助信息，保持原审批文件不变。
- 已生成 88 条候选的 230 个逐轮预期草稿，保留首批 15 条原样，识别 16 组相同输入；发现假设房价的禁行条件缺失，正在修正并回归。
- 已修正假设价格不得覆盖实际费用，14 项测评审核相关测试通过；来源引用校验、Python 编译与 diff 检查通过。88 条审批均 pending，0 条真实 Agent 运行。

# 2026-09-27 重复组与修订场景续审

# 2026-09-27 真实图试跑和 Judge

## 2026-09-28 记忆诊断开始

- 已复核七条真实图试跑报告及当前 Langfuse 实验；`explicit_add` 硬合同失败且工具返回 rejected，`explicit_delete` 状态正确但回复忠实度为零。
- 当前实验 Trace 仅含 experiment-item-task、evaluation、evaluator，不含偏好审核的内部入参和 Reviewer 决策；下一步查看本地回放夹具并定向复现。
- 工作区已有大量未提交用户改动，保留原样。
- 第一次定向复现：沙箱网络受限返回 `model_connection_failed`，经网络执行授权后调用真实图成功；context 的 subject=self、scope=long_term、assertion=actual、quote_valid=true，Reviewer 返回 accept，最终 save_preferences=saved。该次并未复现前一日拒绝，不可据此推断历史拒绝源头。
- 随后再顺序执行两次相同合成 `explicit_add`：两次 context 均合格、Reviewer=accept、工具=saved、各落盘一条；本轮三次均未复现旧拒绝，且未上传 Langfuse。
- 直接调用当前 `finalize_business_reply`：save_preferences 与 edit_preferences 的 status=saved 均输出同一句“已记住你的偏好。”；现有偏好自动保存定向单测 9 项通过，但未覆盖删除回复语义。该轮未修改业务代码、预期金标和远端实验。

- 已确认本地 `.venv` 及模型、Langfuse 配置完整，核对既有隔离入口与七条合成记忆案例。下一步调用真实图并记录运行结果，再做独立 Judge 评分。
- 首次网络受限发生 ConnectError；获得外部网络执行授权后七条真实 Agent 图运行完成，六条记忆合同通过，一条 `explicit_add` 因保存工具拒绝而失败。
- 新增 `evals/judge_memory.py`：基于初始/最终记忆、工具状态、实际回答的独立回复忠实度评分。v1/v2 抽样暴露错误归因及混淆评分口径，最终 v3 按回复真实性评分并与硬断言分开，七条均已上传。
- 远端实验七条 Trace 全有 `memory_contract` 和 `memory_answer_fidelity_v3`；定向测试及编译检查通过。88 条候选没有因此变成正式 gold 或完成回放。

- 已恢复现有计划、88 条预期草稿和首批审核报告；用命令级 safe.directory 读取 Git 状态，没有碰用户的未提交改动。
- 确认 16 组重复输入与 4 条需要拆分评分边界的案例；下一步生成审核辅助文档并做只读校验。
- 已新增 `evals/review/duplicate_and_revision_proposals_2026-09-27.md`，列出 16 组主/配对 ID 与来源类别、结果共享门槛、4 条原始对话/合成夹具分轨方案。
- 定向 `unittest` 14 项通过；额外断言核对 16/16 组、4/4 提案、88 条审批仍 pending、88 条均 `not_run` 且不可回放。未执行真实 Agent、供应商调用或 Langfuse 上传。
# 2026-09-28 全量开发测评试跑

- 已确认 52 条工程回归证据与 88 条语言候选是不同轨道；本次先实现隔离逐例运行，不自动晋升人工 gold。
- 已检查模型、pgvector 与 Langfuse 配置可用；真实供应商和天气等外部信息调用须禁用。
- `evals.runner` 完成 52 条工程证据：46 passed、6 blocked（PG live 测试未启用），退出码 1 表示关键 blocked，并非断言失败；输出 `.verification/eval-baseline-20260928/`。
- 新增 `evals.real_agent_batch` 开发试跑入口：隔离合成记忆/checkpoint、拒绝真实供应商、逐例持久报告、来源哈希与续跑；不以 pending 草稿自动评分。
- 11 项定向测试通过；首次沙箱内单例网络 `model_connection_failed`，授权外部网络后同一合成记忆单例完成，历史失败报告保留，不冒称语义通过。
- 新模型 `gpt-5.6-terra` 的普通文本请求和最小简单工具调用可返回，但真实 Agent 的强制 `TurnRoute` 协议请求出现 `model_request_rejected`，多轮后出现 `model_timeout`；新模型批次仅落盘 1 条部分执行记录，已停止继续消耗额度。当前不能视为 Agent 接入成功，需更换支持复杂 JSON Schema/强制 tool_choice 的模型或调整网关兼容层。

- 2026-09-28 复核新模型：`history-200448d07bcb1e16e5f5` 记忆单例真实完成，工具状态为 `save_preferences=saved`，无 `slot_error`；`history-0b8cfbe4b416f38491e6` 旅行单例在 `TurnRoute` 结构化调用处返回 `model_request_rejected`，未产生 route、slots 或后续工具调用。
- 对应输出：`.verification/model-smoke-new-20260928.jsonl`、`.verification/model-smoke-route-20260928.jsonl`。两条均为隔离开发试跑，未上传 Langfuse、未计入正式准确率。
- 结论：当前模型只验证了简单工具路径，未通过 Agent 必需的复杂结构化路由调用；暂停 88 条全量真实回放，下一步应先换兼容模型并重新做单例路由冒烟。

## 2026-09-28 新模型测评批次

- 当前模型旅行路由冒烟完成：`history-0b8cfbe4b416f38491e6` 为 `completed`，观察到 `1/1` 轮完成。
- 输出：`.verification/model-smoke-qwen-20260928.jsonl`；该文件为隔离开发试跑，不计入正式准确率。
- 脚本整体退出码为 `1`，原因是 `--case` 只运行了 88 条中的 1 条，不能解读为案例失败。
- 下一步启动新的全量输出 `.verification/eval-batch-qwen-20260928.jsonl`，支持中断后 `--resume`。

- 全量新模型批次已完成：88/88 候选、230/230 用户轮次均写入 `.verification/eval-batch-qwen-20260928.jsonl`；manifest 记录模型为 `qwen3.7-plus`、政策后端为 `pgvector`、供应商禁用、Langfuse 关闭。
- 本批没有模型服务级阻断；运行分布为 `slot_validation_failed=69`、`semantic_review_failed=57`、`structured_output_validation_failed=25`，另有 79 轮未挂 failure_code。候选级 `completed=88` 仅表示图执行结束。
- 进一步核对发现只有 1 个候选少执行 1 轮（`history-726f06e575336a8faeae`，2/3）；其余候选轮次完整记录。
- 批次完整性与预期校验测试通过：`tests.test_eval_real_agent_batch`、`tests.test_eval_expected`、`tests.test_eval_annotations` 共 10 项通过。
- 当前仍未把 88 条 pending 预期提升为 gold，也未计算准确率或上传 Langfuse；下一步需要先人工确认预期/初态，再补通用逐轮硬断言和 LLM-as-judge。

# 2026-09-28 RAG 召回专项评测

- 已确认现有 10 条 `evals/policy_rag.jsonl` 是工程契约，不能直接得出真实 Recall@K。
- 已定位真实 Hybrid RAG 的双路 SQL 与 RRF 参数，准备读取当前已发布政策和条款生成可审核查询草稿。
- 已生成 `.verification/eval-batch-qwen-20260928-failure-summary.md`，按失败类型列出 165 条需要人工关注的观察项，包含案例 ID、轮次、用户输入、Agent 回复和工具状态；生成器为 `evals/summarize_real_agent_batch.py`，编译检查通过。

## 2026-09-28 RAG 真实召回结果

- 在沙箱外使用项目真实 Embedding 配置完成 24 条政策查询评测，政策版本为 `2026-project-v1`，结果写入 `.verification/rag-retrieval-20260928.jsonl` 和对应 summary。
- 当前样例仍是从已发布条款/住宿行自动生成的 `draft`，因此结果是开发测量，不是正式 benchmark。
- 向量召回：Recall@1 `83.33%`、Recall@3 `100%`、Recall@5 `100%`、MRR `0.9167`。
- 中文关键词召回：Recall@1 `83.33%`、Recall@3 `87.50%`、Recall@5 `91.67%`、MRR `0.8729`。
- RRF 融合：Recall@1 `83.33%`、Recall@3 `100%`、Recall@5 `100%`、MRR `0.9167`；零命中 `0` 条，版本混用 `0` 条，目标覆盖率 `100%`。
- 4 条 RRF 首位未命中均在 Top-5 命中；其中两条是相邻政策条款，两条是同省相邻住宿行，说明当前主要问题是细粒度排序和自动 gold 过窄，不是候选召回丢失。

## 2026-09-28 独立 Ragas 评测

- 新增 `evals/ragas_retrieval_eval.py`，默认消费 `.verification/rag-retrieval-20260928.jsonl`，支持 `--route all|vector_ids|keyword_ids|rrf_ids`。
- 执行：`.venv\Scripts\python.exe -m evals.ragas_retrieval_eval --route all --input .verification\rag-retrieval-20260928.jsonl --output .verification\ragas-retrieval-20260928.json`
- 输出三条路线：vector `ID precision=0.05 / ID recall=1.0`；keyword `0.05 / 1.0`；RRF `0.2 / 1.0`。
- 同时保留排序召回：vector `Recall@1/3/5=0.8333/1.0/1.0`；keyword `0.8333/0.875/0.9167`；RRF `0.8333/1.0/1.0`。
- 验证通过：Python 编译、`git diff --check`、`tests.test_policy_rag` 与 `tests.test_eval_expected` 共 58 项。
- 当前仍是 draft gold 的开发测量；下一步应先人工修订相关 chunk 集合，再扩展到带 Agent response/reference answer 的语义评测。

# 2026-10-08 Langfuse 原生 Token 用量接入

- 已修改 `business_travel/observability.py`：增加 `safe_usage_details`，放行 `total_tokens`，并在 `chat_model` 结束时写入 Langfuse 原生 `usage_details`。
- 已新增 `tests/test_observability.py` 用量映射与非法值过滤测试。
- 已更新 `docs/langfuse.md`，说明查看 `chat_model` 节点及 Trace 汇总前提。
- 已完成定向观测测试 26 项、上下文/脱敏测试 38 项、Python 编译检查和 `git diff --check`。
- 现有服务需重启后，新 Trace 才会使用原生用量字段；历史 Trace 不回填。

## 2026-09-28 Ragas 标注入口

- `evals/ragas_retrieval_eval.py` 现在支持 `--gold-field` 和 `--require-reviewed`。
- 新增 `evals/prepare_rag_review.py`，从 24 条 draft 生成 `evals/rag_retrieval_gold_review.jsonl`。
- 模板默认复制单个 draft chunk 到 `relevant_chunk_ids`，但状态为 `pending`，不能直接作为正式 gold。
- 2026-10-09：已确认实际 Git 根目录为 `TourAgent/`，存在大量用户已有未提交改动，全部保留；已定位总回复入口、领域渲染器及相关回归测试，开始整理模板调度层。
- 2026-10-09：新增 `business_travel/reply_renderer.py`，将 ToolMessage 到用户文案的总调度从 `graph.py` 抽离；保留 `graph.py` 的兼容导入，不增加模型调用。
- 2026-10-09：新增 `tests/test_reply_renderer.py`；模板/业务渲染 53 项、主图/流式/Web 52 项、偏好/标签/事实上下文 46 项通过；编译和 diff 检查通过。

## 2026-10-09 价格展示口径调整

- 供应商返回有效金额时，将 `Quote.taxes_confirmed` 和酒店详情报价标记为含税；缺失金额仍保持未确认。
- 删除“尚未取得入住期间逐晚含税总价”等展示文案，改为“按供应商含税价格展示”，继续提示酒店搜索起价的每晚/全程口径、库存和退改规则需要核实。
- 更新 Tuniu 文档与价格相关回归测试。
- 定向价格、酒店详情和结果展示测试 70 项通过；`compileall` 通过；旧价格文案检索为空。

## 2026-10-09 JEV 接入

- [x] 新增 JEV 配置、Bearer HTTP 客户端、重试和结构化 usage 解析。
- [x] 将 JEV 结果接入主图 `prepare`，首轮新请求生成本地 `TurnRoute`，跳过原路由模型调用。
- [x] 保留 `shadow/primary` 模式；主模式不绕过证据、槽位和语义复核。
- [x] 新增 JEV 适配器测试；JEV 5 项、主图/槽位/任务回归 103 项通过。
- [x] Python 编译检查和 `git diff --check` 通过。
- [ ] 用户填写 `JEV_API_KEY` 后重启服务，执行一次真实请求并在 Langfuse 检查 `jev_decision` 的 usage。

## 2026-10-18 RAG 全链路复核开始

- 已读取 diagnose/planning-with-files，恢复项目计划并保留既有未提交改动。
- 证据范围：当前代码、政策资料、既有测试与脱敏评测；暂不声称当前运行服务与数据库已验证。
- 完成入库、混合检索、RRF、附表精确补入、条款闭包、预算、生成、引用验证、复核、补检索和 Web 输出追踪。
- 9 个相关模块 203 项测试通过；追加内存探针复现：图传入院士但没有派生住宿档次、后端引用摘录未进入最终文本、普通复核矛盾结果被放行。
- 实际资料包离线组装为 137 片段；没有发现该正文明确编号引用被包组装丢弃的实例，已排除该怀疑作为当前确认问题。
- 探针第一次使用了与模拟原文不匹配的摘录，触发预期引用拒绝；改为 source_id 由后端补原文后完成身份映射路径复现。未把此夹具错误报告为产品故障。
- 工具边界记录：首次定位前端时使用不存在的 static 路径及原生命令不支持的 -ErrorAction 参数；改用 rg --files 定位 web_static 后完成检查。
- 本轮只更新计划/发现/进度文件，未修业务代码、调用真实模型/供应商、读写真实 .env 或修改数据库。

## 2026-10-12 主助手日期后移

- [x] 检查现有提示词、消息组装和 Python 虚拟环境；尚未修改运行配置或启动服务。
- [x] 实施 suffix 默认及 prefix 基线开关，更新示例配置与回归测试。
- [x] 相关 unittest 55 项、compileall 与 git diff --check 通过；未连接供应商或启动服务。
- 首次组合测试因未设置 `PYTHONPATH=tests` 收集失败；补齐项目测试路径后通过。

## 2026-10-15 Candidate selection input compaction

- Read existing plan and changed files; preserve all prior work.
- Confirmed local .venv, requirements.txt, and unittest-based planning suite.
- Preparing projection/configuration changes; no .env secrets accessed or services started.
- Implemented model-only projection, compact default, and full-input baseline.
- Initial targeted suite: 63 tests passed. Supplemental suite: 73 tests passed, plus
  one import failure for nonexistent test_plan_checks; rerun with actual module names.
- Real supplied data: hotel 8 candidates, 10397 -> 8233 characters (20.8% reduction);
  train 10, 5793 -> 4679 (19.2%); flight 8, 4913 -> 3801 (22.6%).
  These measure compact JSON characters, not billed tokens or model selection quality.
- Final combined suite passed 158 tests, including planner guards, hotel details,
  presentation, context budgets, graph integration, and configuration. compileall
  and git diff --check passed. No running service was started or restarted.

## 2026-10-15 Candidate compaction documentation

- Added docs/候选输入精简与Token优化.md and a scoped Unreleased changelog entry.
- Document covers actual removal rules, unchanged complete objects, real train
  before/after example, full/compact switch, character-only results, and A/B guidance.
- No additional application changes or online calls.
- Document JSON examples match the implemented projection; 27 targeted tests and
  git diff --check passed.

## 2026-10-16 规划查询并行化

- 已确认 Tuniu/FlyAI 查询入口是同步 `search()`，Tuniu 内部为每次调用建立独立异步 MCP 客户端；适合用有界线程池并发承载独立查询。
- 已在 `business_travel/planning.py` 增加并行调度，默认配置为 `TRAVEL_PLANNER_QUERY_MODE=parallel`，保留 `serial` 基线。
- 查询结果仍按原阶段顺序合并；单个查询异常只影响该查询，候选选择和后续校验逻辑不变。
- 新增 `docs/响应时间优化.md` 和配置/规划测试。
- 定向测试 `tests.test_settings tests.test_planning_stages tests.test_query_contracts`：48 项通过。

## 2026-10-16 酒店方案缺失诊断进行中

- 已确认用户截图中的 `query_hotel` 有返回候选。
- 已定位酒店从查询结果进入最终方案的关键边界为 `planning.run_planner()` 中的 `PlanSelection` 和 `missing_components`。
- 下一步读取 `tuniu.py` 的酒店详情归一化逻辑，并用现有测试/最小夹具验证空选择、售罄和时间冲突三种路径。
- 记录错误：首次读取供应商文件时使用了不存在的 `uniu.py` 文件名，已改为 `tuniu.py`。
# 2026-10-16 酒店选择完整性修复

- 已完成酒店候选排序：优先可用库存、详情成功、可比较价格和地址信息，再执行现有候选上限。
- 已完成酒店必选重选：请求包含酒店且存在可选酒店时，模型漏选会触发下一次选择尝试。
- 已保留售罄酒店的部分选择行为，避免在无可用酒店时强行补选。
- 已新增酒店漏选、售罄豁免和排序回归测试。
- 规划测试 14 项通过；相关组合测试在正确 `PYTHONPATH` 下通过；完整测试存在既有评测快照/浏览器证据失败。

# 2026-10-17 推荐阶段接入差旅标准

- 已确认用户指出的矛盾是真实代码问题：当前规划先选候选、后做政策核验，可能先推荐超住宿限额酒店。
- 已确认现有政策数据和住宿查询接口可以提供推荐所需的交通允许席别、住宿档次和城市/日期限额。
- 已完成政策画像、候选标准状态、标准分层选择池和酒店库存兜底。
- 已完成结果渲染：差旅标准靠近推荐结果，超标准候选放在末尾备选，正式核验和查询条件保持在末尾。
- 新增 3 组规划回归测试，覆盖标准内酒店优先、无标准内酒店保留超标准备选、允许席别优先。
- 定向回归 88 项通过，相关图/标签/候选输入测试 31 项通过；`compileall` 和 `git diff --check` 通过。

## 2026-10-18 行程推荐引用补齐验证

- 回归先复现推荐 profile 没有 sources/standard_citations，导致标准区域无引用；已补齐同版本原文、配置及精确住宿行。
- 264 项相关 Python 测试与 4 项前端解析测试通过。实际前端加载行程推荐夹具后，在 1440/390/320 视口通过引用展开、附表坐标和刷新验收。
- 追加合并结果测试复现同一消息重复来源编号跳到后段；已改为定位引用之后最近的对应依据，两个浏览器回归均通过。
- 本轮发现与修复均使用真实代码和资料包、模拟数据库及网页接口；没有调用真实供应商、模型或重启服务。
- 最终新增测试文件共 8 项，最后定向组合 50 项与 4 项前端解析测试通过；编译、JS 语法和 diff 检查通过。实际工具结果到最终回复的引用保留也有回归。

## 2026-10-18 RAG 修复验证记录

- 168 项核心定向测试通过。新增边界测试确认评测遇到无 chunk、非字典来源和检索异常时会中断，已针对这些路径修复。
- 首次新增测试夹具误用了 run_query 参数名和 _active 返回形状，已按真实调用协议修正夹具。
- 接续工具调用有两次不存在的等待 ID，均未执行项目操作；后续使用实际运行会话 ID。
- 274 项相关 Python 测试、4 项前端解析测试通过；真实渲染器和前端组件在 1440/390/320 视口通过引用点击、键盘操作、跨消息隔离及流式保持状态验证。
- 评测版本边界新增测试先复现：相同 chunk ID 在不同政策版本中被误算命中。已排除不可比版本并单独计数；服务失败仍计入分母。
- 一次 rg 读取 Windows 通配符路径失败，未影响已有结果；后续按目录与 glob 过滤搜索。
- 新增回归文件最终 17 项通过；Python compileall、两份 JS 语法检查和 git diff --check 通过。浏览器证据及验收结果位于 .verification/policy-citations/。
- 本轮修改：policy_pg / policy_rag / policy_guard、message-view.js / app.css、rag_retrieval_eval，以及新增回归与浏览器验收脚本。历史实验、真实 .env、数据库与运行服务保持未操作。

## 2026-10-18 推荐输出修正（原记录）

- 完成推荐优先、具体标准随后、解释末尾折叠的输出顺序；补充有库存的房型、床型、早餐及真实参考报价。
- 完成单人入住默认参数及默认来源展示；保留多人输入和关闭默认行为的实验开关。
- 完成未知席别与已知超标席别的区分，同时覆盖推荐和正式政策核验。
- 13 个相关测试模块共 199 项通过；business_travel 与本轮测试文件编译通过；git diff --check 通过。
- 测试过程遇到非法 list 查询范围引发 TypeError，已修复为契约校验错误；首次组合测试缺少 PYTHONPATH 导致 test_tuniu 导入失败，设置 PYTHONPATH=.;tests 后通过。
- 本轮仅离线验证；未读写真实 .env、未调用真实供应商、未重启服务。页面需重启后重新查询以获取新输出。
# 2026-10-18 精确政策引用与证据去重

## 2026-10-18 费用报销金额测算

- 已恢复计划和当前工作区，用户确认按已发布制度测算并保留旧模式。
- 开始核对原文和已存在的规则检查路径；使用本地 Python 虚拟环境与离线夹具。
- 已实现可切换测算入口、逐晚计算、来源校验和结论优先展示，正在运行离线回归。
- 工具读取错误：两次尝试读取磁盘上不存在的 AGENTS.md，以及误写 message view 测试文件名；沿用用户提供的 AGENTS 指令，已定位真实 tests/message_view.test.cjs。
- 定向 268 项回归通过；Node 展示解析 4 项通过。
- 全量 unittest：1100 项，90 跳过，1 失败/5 错误。6 处均来自历史评测源哈希漂移或 rag_retrieval_gold_draft 被通用案例加载器误读；当前修改未涉及这些文件，历史 findings 已记录同类失败。未更改样本哈希、审批标记或断言。
- 使用 diagnose 对全量测试失败做只读边界核对；已确认错误栈在 evals 的来源/格式校验，尚未把全量测试宣称为通过。
- 已用现有 Chrome 和预置 Playwright 完成 1280/390 宽视口验证：表内引用可点击并打开准确原文，页面无横向溢出，截图已目视核对。
- 浏览器验证工具的两处环境/夹具问题已处理：未安装 Playwright 自带浏览器，改用已安装 Chrome；跨视口用例显式关闭上一用例打开的 details，符合现有 renderer 保持展开状态的约定。未安装新依赖。
- 最终定向 445 项通过，新增费用测算测试 26 项；Node 展示解析 4 项通过；compileall 与默认 Git diff --check 通过。覆盖真实学校资料包、数据库与图边界、来源缺失、旺季、金额精度、旧模式、企业规则单位及前端引用。
- 差异检查曾错误临时关闭 autocrlf，导致现有 CRLF 被误报为行尾空白；已用项目默认设置重新检查，返回 0，未改动文件换行风格。
- 配置：TRAVEL_POLICY_CHECK_MODE=estimate（默认）或 legacy；TRAVEL_REIMBURSEMENT_MAX_NIGHTS 限制逐晚工作量。无需新增依赖、数据库迁移或发布政策。
- 本轮未读取真实 .env、未调用真实付费模型或数据库、未重启服务；重新加载当前代码后新模式生效。混合交通支付中的附加费/保险/退改费用暂单独待核，不猜最终报销额。

## 2026-10-18 Qwen rerank API 接入中

- 已恢复现有计划和未提交改动，核对 Python 虚拟环境及生产检索流程。
- 正在核实地域端点与请求协议，将使用离线 HTTP/数据库夹具验证。
- 工具记录：误发一次无效 wait ID，未执行项目操作；后续仅等待实际存在的会话。
- 官方文档初次网络读取被沙箱拒绝，升级权限后成功读取；模型概览无接口正文，改读 rerank/text-rerank-api/base-url 官方指南。
- 修正早期地址判断：统一域名仍支持，不强制业务空间ID；使用用户提供的 maas.qianwenaiapi.com 原生地址为默认。
- 核心客户端、扩池重排、RRF回退、调用预算、Langfuse和评测切换已实现。
- 首轮156项测试出现三项同一新测试夹具解包错误（helper返回3项），已修正；未修改旧断言。
- 两次rg误用Windows通配路径、一次试读不存在的eval测试文件，已改为目录搜索并定位真实test_rag_corrections.py。
- 最终定向回归292项通过；Python compileall和git diff --check通过。
- 未读取真实 `.env`、未使用真实 Key、未调用重排服务、未重启运行服务；需要用户填 Key 后重启并发起新政策查询做真实验收。

- 已恢复项目计划，使用 diagnose 与 planning-with-files；现有未提交改动保留。
- 当前阶段：建立离线回归与精确引用设计；未修改业务代码、配置密钥或运行服务。
- 已复现配置 JSON 外露，完成片段注册、去重、口径说明和 fragments/grouped 开关。
- 225 项相关后端测试与 4 项前端解析测试通过；正在执行实际前端的离线浏览器验收。
- 最终 277 项相关 Python 测试及 4 项前端解析测试通过；两个浏览器验收通过，截图已查看，桌面/手机无横向溢出，默认折叠与点击展开正常。
- 编译和 JS 语法检查通过；差异检查发现计划文件新增尾部空行，已修正。所有验证均为离线夹具，未操作真实模型、供应商、数据库或运行服务。

## 2026-10-18 用户引用仅展示真实条款

- 已恢复计划与当前引用代码，开始定向调整推荐展示；保留前序修改和实验切换。
- 新增两项回归先复现配置仍显示及默认映射伪来源；修改后 26 项推荐引用定向测试通过，继续验证来源类型和完整关联回归。
- 最终 282 项 Python 回归、4 项前端解析测试通过；两个离线浏览器验收通过，已查看手机版来源展开截图。编译、JS 语法和差异检查通过；未重启现有服务，旧消息不自动重算。

## 2026-10-18 HNSW 运行时切换

- 已恢复现有计划并确认未提交改动；本轮只围绕 pgvector 向量检索索引切换。
- 已确认现有 `MATERIALIZED` 精确 SQL 会绕过新增 HNSW，必须同时改查询而非只加 DDL。
- 规划脚本运行时被系统拒绝，已记录在计划错误表；不影响代码分析。

## 2026-10-18 HNSW 运行时切换完成

- 新增全局 cosine HNSW DDL helper 和 `policy_admin.py migrate-hnsw`。
- 增加 HNSW 配置：`TRAVEL_POLICY_HNSW_M`、`TRAVEL_POLICY_HNSW_EF_CONSTRUCTION`、`TRAVEL_POLICY_HNSW_EF_SEARCH`。
- `policy_pg.py` 查询路径已从物化精确排序切换为 HNSW 优先，缺索引自动保留 exact fallback，并在 rankings 中返回模式。
- 更新 schema 注释、`.env.example`、README、`docs/policy-rag.md`。
- 验证：128 项定向 Python 测试通过；compileall 通过；`git diff --check` 通过；DDL CLI 输出正确。
- 真实 PostgreSQL 的 `EXPLAIN (ANALYZE, BUFFERS)`、Recall@K、P95 延迟和迁移执行尚未进行。

## 2026-10-18 政策意图路由修复进行中

- 已确认截图中 `consult_policy` 0.01 秒结束，输出要求费用日期，说明实际走了 `policy_check` 直接规则校验，没有进入 RAG。
- 已确定修复点放在 JEV primary route 生成新任务前，避免对后续任务更新和显式报销核验造成影响。

## 2026-10-18 政策意图路由修复完成

- 在 `business_travel/jev.py` 增加解释/核验语义纠偏：标准、规定、原文、条款、适用条件等走 `policy_explain`；报销、超标、合规、实际金额等保留 `policy_check`。
- 新增 3 项 JEV 路由回归；与政策要求、当前政策、Rerank、Hybrid RAG 共 101 项测试通过。
- compileall 和 `git diff --check` 通过。
- 未调用真实 JEV、LLM、数据库、Rerank 或 Langfuse；需要重启当前服务后用实际问题验收 Langfuse 链路。

## 2026-10-18 政策意图描述精细化

- [complete] 先写意图定义回归测试，验证 JEV 请求体包含具体费用/制度解释边界、反例和优先级。
- [complete] 更新 `business_travel/jev.py` 的 `INTENTS` 与 intent instructions。
- [complete] 定向验证：设置 `PYTHONPATH=.;tests` 后运行 JEV、政策要求、当前政策、Rerank、Hybrid RAG 测试，102 项通过。
- [complete] `compileall` 通过。

## 2026-10-18 移除关键词纠偏

- [complete] 移除本地 policy explain/check 关键词重写逻辑。
- [complete] 将原本验证关键词纠偏的测试改为验证 `primary_route()` 保留 JEV 意图。
- [complete] 102 项定向测试、compileall、git diff --check 通过。
- 需要重启服务后用真实 JEV 请求观察误路由率；当前离线测试不调用真实 JEV。

## 2026-10-18 policy_rag 检索失败诊断

- [complete] 根据 Langfuse 截图确认意图已进入 `policy_rag`，失败点在 `policy_retrieve`，Rerank 未执行。
- [complete] 读取 `policy_rag`、`policy_pg` 和配置代码，确认错误会被统一收敛为 `policy_retrieval_failed`，截图本身不包含底层 reason。
- [pending] 获取真实 `policy_retrieve` Raw 输出或 `policy_admin.py search` 的 `reason`，再决定是重导入 Embedding、发布政策还是修复数据库/权限。

## 2026-10-18 Embedding 请求修复

- [complete] 确认 CLI 返回 `embedding_request_rejected`，故障发生在查询 Embedding 请求而非 HNSW/Rerank。
- [complete] 请求体增加 `dimensions=settings.embedding_dimensions`。
- [complete] 非 200 响应改为仅保留 HTTP 状态码的安全错误码。
- [complete] 109 项相关测试、compileall、git diff --check 通过。
- [pending] 重启当前服务后重新执行检索，依据新的 HTTP 状态码完成配置修复。

## 2026-10-18 修正 Embedding 兼容参数

- [complete] 发现 qwen3.7 不能沿用所有模型的 `dimensions` 参数，改为仅对 `text-embedding-v3/v4` 发送。
- [complete] 追加安全的供应商错误码提取，保留 HTTP 状态与 ASCII `error.code`，不泄露响应正文。
- [complete] 111 项相关测试、compileall、git diff --check 通过。
- [pending] 重启服务后重跑检索，确认 qwen3.7 请求是否恢复；若仍为 400，使用新的 provider error code 决定切换模型或原生接口。

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| 直接从项目根运行组合测试时找不到 `test_slot_protocol` 等顶层测试模块 | 1 | 按项目既有约定设置 `PYTHONPATH=.;tests` 后重跑，102 项通过 |

## 2026-10-18 最新 Langfuse 诊断进度

- 已通过应用浏览器打开 Langfuse 项目并读取最新 Trace JSON；浏览器 Edge 扩展本身因认证方式不兼容无法绑定，但应用浏览器已有登录态，可完成只读检查。
- 已复现并确认根因：第二轮“目的地北京”进入 `slot_extraction`，ExpenseExtraction 输出缺少 `policy_query`；三次重试仍相同，最终在 `policy_query_required` 处失败。
- 当前待决定：是否把未变化的 `policy_query` 在代码中从已有 policy_explain 事项自动继承，并增加两轮回归测试；若实施，需先写失败测试，再改 `propose_slots()`。
- 已补充第一轮 Trace 证据：Langfuse 树中可见 `policy_rerank`，下载 JSON 显示 `status=ok`、`model=qwen3-rerank`、`elapsed_ms=428`；RAG 与 Rerank 配置本身在该请求中工作正常。
- 诊断结论增加第二个根因：当前 Trace 的 `facts` 是 `employee_grade=院士`，不是规则需要的 `academic_title=院士`；所以即使修复 follow-up 继承，仍应修正字段抽取提示/校验，否则北京住宿标准可能继续停在“请确认住宿档次”。

## 2026-10-18 全架构复核
- 已通过 Langfuse 读取 17:54 首轮和 17:57 follow-up Trace。
- 首轮 RAG/Rerank 成功；follow-up 在槽位校验前被 policy_query_required 终止。
- 已确认待审结构点：槽位状态继承、身份字段映射、Embedding 硬门禁、统一错误包装。
- 并行审查已确认 policy_pg.search() 的 Embedding 失败不会降级到关键词召回。


## 2026-10-18 全架构复核补充
- 315 项定向回归中 314 项通过，1 项 RAG 缺失条件状态回归失败，已记录待分类。
- 已收到槽位/门禁、RAG/观测、主图/状态三路只读审查结论。
- 目前判断：架构主体可用，但存在 3 个高优先级跨层契约缺陷（follow-up policy_query、canonical question、embedding hard gate）和多处诊断/可用性问题。

- 2026-10-18 全架构复核补充：已复现 164 项定向测试 1 项 RAG clarify 回归；确认 follow-up policy_query、canonical question、Embedding 硬门禁、Web 步数、JEV 配额和观测缺口。

- 本轮补充验证：compileall 与 git diff --check 通过。定向 164 项中 163 通过；唯一 RAG 状态失败由旧 HNSW SQL 测试夹具未适配导致，未确认是线上故障。全量 1062 项含历史评测源漂移/旧 JSON schema 等既有失败。

## 2026-10-18 槽位门禁修复进行中

- 已完成代码阅读与最小故障链路定位。
- 下一步：先补回归测试使“首轮住宿标准 + 二轮北京”在旧代码下失败，再修改 `slots.py` 与 `slot_protocol.py`，最后运行定向 unittest。
- 错误记录：本轮 `rg` 两次正则包含未闭合括号，未影响代码；改用固定字符串搜索。

## 2026-10-18 架构复核补充进度

- 已完成 graph/tasks/policy_rag/policy_pg/settings/web/jev 的逐段复核。
- 已向主线程报告 5 个结构性问题及置信度；未修改业务代码，避免与身份归一化和槽位门禁修复冲突。

## 2026-10-18 身份归一化子任务完成

- 已完成大学政策层的保守字段兼容和回归验证。
- 未改 `slots.py`、`slot_protocol.py`，避免与门禁继承修复冲突。
- [x] 先补最小回归：旧代码在省略 policy_query 且 question 为空时返回 error。
- [x] 修复 policy_explain 更新的 scope 继承、协议提示和 canonical question 回退。
- [x] 定向回归：60 项槽位/任务/政策需求测试通过；90 项主图/混合政策/任务控制测试通过。
- [x] `compileall` 与 `git diff --check` 通过。

## 2026-10-18 浏览器复核与 canonical 目标修复

- [x] 运行完整 unittest discover，记录 1069 项结果及历史评测夹具漂移。
- [x] 用 8001/8002 隔离浏览器验证来源校验、模型连通性和失败展示边界。
- [x] 用真实政策库 CLI 验证 Embedding 配额失败后的关键词降级检索。
- [x] 修复显式政策范围变更继续复用旧 canonical 查询的问题，并增加回归。
- [ ] 在允许外部模型网络的隔离服务中完成真实浏览器跨轮 RAG 回放。

结果：任务操作回归 13 项通过；RAG 关键词降级查询返回 `status=ok`、`degraded=true` 和政策来源。8001 的失败是 Origin 校验配置不匹配，8002 在正确 Origin 下进入业务但外部模型连接受限；两者都不能替代真实模型联调。

## 2026-10-18 最终验证

- [x] 跨轮政策范围/canonical 目标测试 17/17 通过。
- [x] `compileall` 与 `git diff --check` 通过。
- [x] 完整 unittest 1071 项：90 skipped，1 failure、5 errors，均定位到历史评测夹具/源哈希/schema 漂移。
- [x] 真实政策 CLI 查询验证 Embedding 失败后的关键词降级和来源闭包。
- [x] 浏览器 8003 验证页面加载、会话保留和模型失败提示边界。
- [ ] 需要在可访问外部模型供应商的环境重启新代码，完成真实浏览器跨轮回答及 Langfuse 新 Trace 复核。

## 2026-10-18 当前复现：范围扩大到所有地区

- [x] 通过 8000 浏览器复现“北京回答成功，随后所有地区列表请求进入语义复核兜底”。
- [x] 通过 Langfuse 旧同类 Trace 确认该类兜底发生在槽位提案/语义复核阶段，未进入 RAG/Rerank。
- [ ] 抓取当前 20:24 Trace 的 JEV、ExpenseExtraction、slot_review 原始输出，区分路由失败、空 patch、范围契约失败和 reviewer 拒绝。

## 2026-10-18 范围扩展修复

- [x] Langfuse 当前 Trace 读取到完整 `slot_review` 拒绝原因。
- [x] 先写失败回归并确认旧逻辑失败。
- [x] 新增 `PolicyQuery.coverage=all_regions`，默认 target 状态保持旧记录兼容。
- [x] 全地区住宿表走导入附表的确定性投影和逐行来源，绕过模型生成 86 行政策数字。
- [x] 定向 149 项通过，compileall 和 git diff --check 通过。
- [ ] 重载 8000 服务并用浏览器复现跨轮真实模型链路；当前环境无法通过进程查询 API 直接确认服务启动命令。

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `Get-NetTCPConnection`/`Get-CimInstance` 被拒绝访问 | 1 | 记录为运行时可见性限制；改用浏览器和离线回归验证，未据此宣称服务已重载 |
## 2026-10-18 范围扩展修复后验证
- 已完成代码级回归：所有地区范围扩展测试通过。
- 已完成 Python 编译检查。
- 真实浏览器当前会话已过期；8000/8001 进程存在但命令行读取被系统拒绝，未把旧页面结果作为新代码验证。
- 下一步：重启或启动一个明确加载当前工作区代码的隔离服务；获得有效 Web 登录会话后复测“首轮住宿标准 → 北京 → 所有地区”，并核对 Langfuse 新 Trace。
- 隔离服务 8003 已启动并确认端口监听、页面 HTTP 200；等待用户在该页面登录后进行真实三轮回放。

## 2026-10-18 8000 真实验证与观测边界

- [x] 用户在 8000 登录后，复核已有“住宿标准 → 北京 → 所有地区”会话；页面显示 86 行结构化住宿表和逐行来源。
- [x] 在 8000 新建对话并发送“我是院士，去上海出差，我的住宿标准是多少”；当前服务返回上海部级 1100 元/人·天并展示来源。
- [x] 刷新 Langfuse；列表仍停在 21:10:40 旧 Trace，没有新测试请求。
- [x] 检查 `.env` 与观测代码：Langfuse 配置项存在，`Telemetry` 会创建 `policy_rag`/`policy_retrieve`/`policy_rerank` 等节点；当前问题发生在 OTLP exporter 发往 `jp.cloud.langfuse.com:443` 的网络连接。

结论：业务链路已在 8000 真实页面完成；Langfuse 新 Trace 尚未云端可见，不能用旧 Trace 判断新代码。DNS 能解析 Langfuse 域名，但本机 TCP 443 失败；需要允许该运行实例出站 HTTPS 或配置可用代理后重启，再刷新验证。

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| Langfuse list did not show the 8000 test request after refresh | 1 | Confirmed service response separately; inspected config and DNS/TCP connectivity, found outbound 443 unavailable |

## 2026-10-18 JEV multi-intent implementation

- Applied planning-with-files and resolved legacy root planning files.
- Read adapter, graph, route/extraction contracts, detailed criteria, tests and local venv.
- Read official API, Noul and Choice docs through approved public network requests.
- Next: independent intent questions, validated probabilities and scoped task generation;
  verify with synthetic HTTP/model responses and offline regressions.

- Implemented multi-intent adapter, detailed rubrics, configurable threshold, scoped route
  questions, pending recovery protection and first-attempt-only JEV route proposals.
- Initial test_jev run: 21/21 passed. Added full-graph primary/shadow/failure regressions
  and task-limit fallback; next run includes settings and task/review regressions.

- Expanded JEV suite: 25/25 passed, including full graph primary/shadow/fallback cases.
- Broader targeted suite: 247/247 passed in 9.379s. Python compile checks passed.
- git diff --check passed with core.whitespace=cr-at-eol; the initial forced-autocrlf
  check was noisy because existing files use CRLF, not because of introduced whitespace.
- Clarified that a standalone greeting is other, while politeness appended to a business
  request does not create another task. Final focused verification follows this wording edit.

- Final focused recheck: 25/25 passed after greeting/order rubric refinements; compileall
  and scoped diff checks passed. Implementation complete, with no dependency installation,
  production inference, service restart or credential changes.

## 2026-10-18 JEV other rubric refinement

- Resolved legacy root plan files and inspected adapter, existing tests and dependencies.
- Read planning-with-files and diagnose guidance. The prior screenshot proves an observed
  classification error; the cause and effectiveness of a wording repair require live evaluation.
- Official documentation index available after approved network escalation.
- Next: inspect question semantics, update the rubric and validate contrasting examples.
- Reproduced the reported misclassification with a real JEV request before editing.
- Updated business_travel/jev.py and strengthened the existing outgoing-question contract
  test. Detailed policy_check/policy_explain criteria and route thresholds remain intact.
- The same live sentence after editing returned other=0.05 (before: 0.91), with exactly
  plan_trip and policy_explain proposed. Provider model remained jev-1.13.0.
- First combined unittest command ran 67 successful tests but had two loader errors:
  tests/test_settings.py imports test_tuniu by top-level name, and test_semantic_review
  is not a module. Correct the runner's tests path and select existing modules.
- Added an ignored, local .verification/jev-other-20261019/verify.py probe for ten
  synthetic semantic contrast cases. It calls only JEV and prints no credentials.
- 2026-10-19: real JEV semantic probe completed 10/10 successful intent matches. The
  reported example again had other=0.05. Local results.json contains probabilities,
  route/fallback results, model identifier and usage, without secrets.
- Corrected offline runner (sys.path includes tests) completed 172/172 tests across
  JEV, settings, slot protocol, task operations/lifecycle/controls, semantic/context
  review, failed-turn recovery and graph dispatch. Compileall passed.
- CRLF-aware tracked diff checks passed. No-index checks additionally covered the
  pre-existing untracked adapter and tests; removed the extra EOF blank they found.
- Work complete; no running service was restarted. Existing Python services must load
  the updated module before their next classification can use the refined description.

## 2026-10-19 JEV onboarding and evolution document

- Read init-project and planning-with-files, resolved root plans and inventoried docs,
  README, changelog and dirty worktree. Memory lookup found no relevant entries.
- Document output will be Markdown inside docs/, with a discoverable README entry.
- Next: verify official request/response semantics and current routing/fallback code.
- Verified graph prepare, adapter, extraction, retries, settings, policy-rubric history
  and saved live-evaluation results. Refreshed five official documentation pages.
- Public-doc request initially hit sandbox socket permissions, then succeeded with
  approved escalation. No live model or business provider was called this turn.
- Wrote the 18-section Chinese guide, evolution table, API and Python examples,
  pipeline diagram, current routing details and live/offline/cost evidence boundaries.
- Added the README entry and documentation-only changelog note, preserving existing text.
- First inline validation command failed because PowerShell interpreted Markdown
  backticks in the command string. Replaced that invocation with an ignored local
  validation script; no document defect was inferred from the harness failure.
- Final validation passed: all 7 JSON examples, 2 Python examples, a mocked HTTP
  success path and missing-key path, 11 source files and key symbols, configuration
  defaults, README link and all 10 saved live-case results. No real HTTP inference ran.
- Removed two intentional Markdown hard-break spaces flagged by whitespace checks;
  final document and tracked documentation checks are clean (normal CRLF notices only).
- Guide is complete at docs/JEV接入原理与多意图改造详解.md. No business-code edits,
  dependency changes or runtime restarts were made for this documentation request.

## 2026-10-19 追问快捷路径第二阶段

- 已在 `AgentState` 增加 `clarification`，首轮 preflight 缺字段时保存未提交草案、追问、缺失字段、subject ID 和 draft task ID。
- 已在 JEV 适配器增加专用 `is_clarification_answer` 判断；存在 clarification 时不发送普通意图问题。
- 已在槽位层增加服务器生成的定向草案更新路由；补充回答不调用普通 `TurnRoute`，仍经过 preflight 和语义复核。
- 字段补齐且复核通过后，内部草案更新转换为 `new_task`，由现有 `reconcile_tasks` 创建正式任务并入队。
- 定向验证：`tests.test_jev tests.test_slots` 59/59 通过；与第一阶段相关的 `tests.test_slot_protocol tests.test_travel_graph` 通过；compileall 通过。
- 更宽的旧生命周期测试仍期待缺字段先生成 `needs_clarification` 任务，已确认与第一阶段契约冲突，待后续迁移测试夹具。
- 复核后补充边界：字段缺失属于 `clarification`，不再写入 `pending_turns`；只有模型/执行错误才进入失败恢复上下文，避免追问状态污染后续 JEV 路由。

## 2026-10-19 语义复核门禁收窄第三阶段

- 已完成当前复核链路和历史拒绝案例核对，确认问题是 reviewer 协议没有表达“提醒但不阻断”，不是取消语义复核。
- 正在实现结构化阻断问题/提醒分离，并补充软问题与硬问题的回归。

## 2026-10-20 other 意图边界补充

- 保留四类意图，不新增 task_control 或 memory 意图。
- 补充 `other` 正向定义：保存、确认、修改、取消、恢复或重试当前出差和行程事项。
- 补充 `other` 反向定义：这些事项操作属于独立通用操作，不能因与出行主题相关而忽略。
- 更新 `TASK_SCOPES["other"]`，保持 Jev 问题数量和路由结构不变。
- `tests.test_jev` 28/28 通过，Python 编译通过。
- 组合运行中 `tests.test_intent_boundary` 有1项既有 TurnRoute 夹具失败，与本次 Jev 文案改动无调用关系，未修改该夹具。

## 2026-10-20 Jev 100条意图评测集

- 新增 `evals/intent_accuracy_cases_100.jsonl`，共100条，包含80条单意图和20条多意图样本。
- 四类意图在期望标签中的覆盖次数为：`plan_trip` 32、`policy_check` 31、`policy_explain` 31、`other` 32。
- 原20条小样本保留，评测脚本默认切换到100条数据。
- 修正评测摘要中的数据集路径，支持自定义 `--dataset` 时报告真实路径。
- JSONL加载、标签校验、Python编译和差异检查通过；未运行100次真实 Jev 调用。

## 2026-10-20 意图评测上下文修正

- 已核对生产 `prepare()` 的 Jev 状态结构，确认评测器原先缺少真实会话和任务上下文。
- 已修改 `evals/intent_accuracy.py`：支持 Case 可选携带 `conversation`、`current_slots`、`task_context`、`pending_tasks`、`pending_turns` 和 `clarification`。
- 已修改 `evals/intent_accuracy_cases_100.jsonl`：为重试、结果追问、取消、查看已保存行程、确认等依赖历史的样本补充最小上下文，并保留 `intent100-072` 的 `label_review` 标记。
- 保留 `.verification/intent-accuracy-100.json` 作为上下文修正前基线；本阶段未重新调用 Jev，避免把两种评测条件混成一个指标。
