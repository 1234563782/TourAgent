# 2026-10-08 项目总览

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
