# 2026-10-08 项目总览

## 2026-10-19 提交前字段完整性校验

- `TripPatch` 和 `ExpensePatch` 是增量结构，因此预检查必须在 `stage_submission()` 合并已有 subject 后执行；只检查本轮 patch 会误报更新事项缺字段。
- 出行预检查复用 `parse_query_request()`，按 `query_scope` 分别检查酒店、交通和组合请求；费用核验复用 `ExpenseFacts.model_validate()`，当前基础必填字段为 `kind`、`expense_date`、`destination`。
- 缺字段时 `extract_slots()` 返回 `status=clarify`、`question` 和 `missing_fields`，不会调用 `review_payload()`；`graph.py` 随后清空本轮 `task_queue`，因此不会访问供应商。
- `policy_explain` 不做用户事实完整性门禁，因为它可以只解释通用制度；它的证据充分性由政策检索阶段负责。
- 第一阶段没有新增独立 clarification state 或快捷路径；当前 checkpoint 中已有的 slots、messages 和 task context 可供后续阶段接续。

# 2026-10-18 RAG 链路复核（只读）

## 已确认事实

- 当前 pgvector 链路为：已发布版本选择 → Embedding 召回 → jieba/tsvector 关键词召回 → RRF → 可选 Qwen 重排 → 精确住宿表/身份规则补充 → scope/related_ids 闭包 → 上下文预算 → PolicyAnswer → 引用逐字校验 → PolicyAnswerReview/PolicyEvidenceReview → 确定性渲染。
- 定向 RAG 回归 182 项通过；`compileall` 和 `git diff --check` 通过。
- 全量离线回归共 1051 项：90 跳过、2 失败、5 错误。5 个错误中包含评测加载器把 `evals/rag_retrieval_gold_draft.jsonl` 当作普通 `EvaluationCase` 的问题；另有候选来源哈希漂移导致的评测错误。两项失败与 RAG 观测字段兼容有关，未改变 RAG 主链路结论。
- 当前工作区 `.env` 为 `pgvector`，已配置 Embedding、政策数据库和 `TRAVEL_RERANK_API_KEY`；`TRAVEL_POLICY_RERANK_MODE` 为空时，`Settings.from_env()` 通过 `or "qwen"` 仍会启用 Qwen 模式。
- `policy_chunks` 默认只有 GIN 关键词索引，没有默认向量/HNSW 索引；检索使用按文档物化后精确排序，当前数据量小可接受，规模增长后延迟会线性上升。
- `evals/rag_retrieval_gold_review.jsonl` 的 24 条案例均为 `review_status=pending`；`rag_retrieval_eval.py` 的 `read_cases()`/`run` 路径没有强制拒绝 pending，只有 Ragas 脚本提供 `--require-reviewed`。

## 已确认风险

- 真实模型验收记录显示，语义复核曾接受省略“轮船条款不包括旅游船”的回答；引用存在性不等于条件/例外完整性，复核与生成共用同一模型配置，存在共同判断错误。
- 当前版本按 `published_at DESC` 选“最新发布”政策，实际费用日期只参与季节/住宿查询，不参与政策版本选择；若业务要求回答历史生效版本，当前 RAG 不支持按日期回溯。
- `_active()` 查询带 `LIMIT 1` 后再检查 `len(rows)>1`，`ambiguous_policy_versions` 分支实际上不可达，版本歧义保护不足。

## 待验证项

- 未调用真实 Embedding、PostgreSQL、Qwen 重排或真实模型，因此没有本轮的真实召回率、重排收益、端到端延迟和语义完整性统计。
- 需要人工审核 retrieval gold、隔离 RAG 数据集与通用 eval 数据集，并在真实数据库上分别跑 RRF/Qwen 对照后，才能判断线上质量。

- 实际项目根目录为 `TourAgent/`；外层目录不是 Git 根目录。
- 技术栈：Python 3.12、LangGraph、FastAPI/Uvicorn、Pydantic、PostgreSQL/pgvector、SQLite 检查点、MCP 供应商适配、Langfuse 可选观测。
- CLI 入口：`travel_cli.py`；Web 入口：`business_travel.web:create_app`。
- README 将系统定位为参考规划、政策解释和报销条件核对，不执行审批、下单、支付、报销提交或保险理赔。
- 当前 Git 工作区存在大量用户已有删除、修改和新增，未执行回滚。

# 并发简化实施发现

- 当前前端 `chatControls()` 已在 `chatBusy` 或 `chatPending` 时禁用输入框、发送键和确认键；请求成功、准入拒绝、异常和会话刷新路径最终都会恢复控件。
- 后端会话锁覆盖 checkpoint 读取、图执行和提交，不能由前端禁用替代。
- `resources.py` 的 `Slots.capacity()` 当前同时申请 global 与 company 槽位；本次将仅申请 global 槽位。
- company_id 仍被用于身份部署校验、学校每日请求/模型调用配额和数据库统计，不能随并发层一起删除。
- 本轮实现后，`Slots.capacity()` 只申请 global 槽位；单校部署的并发链路不再有 company/global 重复上限。
- 前端已有 `chatBusy` 期间禁用输入框和发送键，本轮增加 `aria-busy`，未重复实现新的前端锁。

# 2026-09-22 项目流转分析发现

- README 明确声明：一句话进入 `TurnRoute`，按需执行领域槽位提取，经过 schema/证据/事项版本/语义复核后提交，再由 LangGraph 主图派发。
- 规划链路是交通/酒店查询 -> 候选选择 -> 时间、政策、地图核验；政策链路是混合检索或确定性规则核验。
- 系统定位为参考规划、政策解释和报销条件核对，不执行审批、下单、报销提交或保险理赔。

## 2026-10-08 业务流程

- Web 请求入口在 `business_travel/web.py:355` 的 `run_chat()`；同一会话先取得 advisory lock，再打开 checkpoint、检查幂等运行记录和当前状态。
- `business_travel/graph.py:311` 的 `prepare()` 调用 `extract_slots()`，进行结构化槽位提取、机械校验、独立语义复核和任务重整；失败时保留待处理原话，不提交新槽位。
- 已验证事项才允许调用领域工具：`plan_trip()` 要求当前旅行事项 `context_reviewed`，`consult_policy()` 要求当前费用事项 `context_reviewed`。
- `confirm_facts()` 与 `save_trip()` 通过 LangGraph `interrupt()` 等待人工确认；保存行程明确不等于预订或审批。
- `finalize_business_reply()` 依据本轮 ToolMessage 渲染结果，当前代码对 `save_preferences` 和 `edit_preferences` 的 `saved` 状态共用“已记住你的偏好”文案；历史发现已指出删除场景可能因此误导，需用当前测试核实。
- 政策 pgvector 路径先限定已发布版本，再做向量/中文关键词召回与 RRF；政策回答另有引用校验和独立语义复核。

## 初步风险线索

- 代码审查线索（尚未判定为缺陷）：部分关键流程依赖 PostgreSQL、模型工具调用和外部供应商，默认离线测试不能覆盖真实运行组合。
- 历史测试记录中明确存在 PostgreSQL live 测试需显式开启、真实模型/供应商与开发回放分轨等限制。

# 2026-09-27 测评首批审核发现

- 52 条映射工程回归已在本机 PostgreSQL 隔离 schema 下全部通过；该结论不代表 15 条自然语言草稿已获证实。
- 15 条草稿中 7 条来自历史记忆样例、8 条为补充边界；所有决策仍是 pending，不能自行标作人工 gold。
- `gap-stale-result` 的原始首轮缺酒店执行必需条件，旧结果分支可能根本无法触发；应拆成当前追问案例和另一个注入已完成结果的版本失效案例。
- `gap-ambiguous-target` 的第二轮是否歧义依赖第一轮任务状态，不能仅凭文本无条件判错。
- `gap-review-rejection` 的固定检索和写作次数及 610 均来自脚本化注入测试，不宜写成真实模型回放的通用预期。
- 记忆保存、定向更新和删除的工程测试部分只证明协议边界，不直接证明原始自然语言在真实模型中通过。
- 审核建议归档在 `evals/review/agent_audit_2026-09-27.md`，其中 11 条适合保留为开发试跑候选、4 条须修订触发条件或政策/故障注入边界；两组都未经人工批准。
- CLI 会话以 `thread_id` 持久化 checkpoint；有中断时要求 `/resume`，确认事实与保存行程分别是不同确认动作。
- Web `run_chat()` 先拿会话级 PostgreSQL advisory lock，再打开 LangGraph PostgreSQL checkpointer；同一 `request_id` 通过请求摘要做幂等，运行记录在 `chat_runs` 中经历 `running -> completed/awaiting_confirmation`，异常为 `failed/interrupted`。
- Web 还有两层资源控制：用户并发锁、全局执行容量/等待队列，以及公司和用户的每日请求/模型调用配额；准入失败时不会执行模型或业务查询。

## 2026-09-28 RAG 召回专项评测

- `evals/policy_rag.jsonl` 的 10 条是行为契约，不含真实 `gold_chunk_id`，不能计算 Recall@K。
- `PostgresPolicyStore.search()` 的实际顺序是：当前已发布文档 -> 向量 Top-`policy_recall_k` -> Jieba/`tsvector` 关键词 Top-`policy_recall_k` -> RRF Top-`policy_top_k` -> 关联条款闭包。
- `search()` 的返回值只保留最终 `sources`，不暴露向量路、关键词路和 RRF 排名；评测应使用同一配置做只读 SQL 采集，不能把最终上下文顺序误当作纯 RRF 排名。
- 自动从政策正文/表格抽取查询和目标条款时，只能先产出 `rag_gold_draft`，不得直接称为正式 gold。
- 主图 `prepare` 负责理解和槽位提交，`dispatch` 按任务顺序派发，`tools` 执行规划/政策/记忆等工具，`advance` 生成单任务结果并更新任务状态，全部任务完成后 `finish` 合并回答。
- `plan_trip` 只有在槽位通过 `context_reviewed` 且日期有效时才调用供应商/规划器；`consult_policy` 分为解释（RAG/模型复核）和核验（确定性规则），都绑定 subject ID 与 revision。

# 2026-09-27 全量标注发现

- 审核队列由 80 条历史候选和 8 条人工补充建议构成；首批 15 条草稿仍待人工审核，余 73 条没有预期草稿。
- 存在 15 对逐轮输入完全相同的历史候选，来源/上下文可能不同；先标记重复组，不直接合并或改原始 ID。
- 浏览器验收报告的一部分 `historical_expected.turns` 为空，且 `observed_outcome` 是整个 run 的汇总，不能据此自动推断逐轮正确答案。
- 结构化语义夹具含逐轮 `values`、`absent`、`scenario` 等期待，可明确标为来源建议，但不是人工 gold；政策金额需确认版本和原文。
- 全量草稿包含 88 条/230 轮、16 组相同输入；已有 15 条保持原样，新 73 条的初态/政策适用性未凭历史运行推断为确定值。
- 审核先处理 4 条修订问题，再核对重复组的不同环境与来源；只将经确认且可复现的案例纳入正式回放。

# 2026-09-27 续审发现

- 16 组逐轮输入相同，但来源分属 legacy 验收报告与当前夹具；输入相同不意味着初态、政策版本、故障注入及运行结果可复用。
- 4 条问题案例的原始 `expected_drafts.jsonl`、审批 `decisions.jsonl` 继续保留；修订应放入独立审核提案，避免悄悄改变候选指纹和人工状态。
- 16 组均是 `acceptance_cases` 或 `acceptance_rules_cases` 与 `legacy_acceptance` 的配对；推荐以可检查的夹具来源为主候选，历史来源保留为证据，但尚未确认环境等价，不能直接共享旧结果。
- 原始退房日期缺失的酒店对话只验证不冒充结果；版本失效需另预置已完成酒店结果。歧义目标需双事项且无唯一焦点；合住房费不自动平摊；Reviewer 固定金额及次数只用于冻结故障注入。

# 2026-09-22 项目流转分析补充

# 2026-09-27 真实 Agent 与 Judge 试跑发现

## 2026-09-28 记忆诊断初始证据

- Langfuse 现有实验 `explicit_add` 的 Observation 为 `execution_status=failed`、`memory_contract=0.00`，实验任务输出 `save_preferences: rejected`，没有 Reviewer 内部节点，不能由此确定拒绝层。
- `business_travel/preferences.py` 的机械校验和 Reviewer 均可返回 reject；`business_travel/graph.py` 将 reject 统一转换为工具状态 rejected。
- `explicit_delete` 的最终记忆正确，但 `finalize_business_reply` 对 save/edit 的 saved 使用同一“已记住你的偏好”文案；仍须核对回放实际调用了此路径。
- `evals/langfuse_memory.py:60-116` 用 `TravelMemory` 临时目录、`InMemorySaver`、真实模型和不可用供应商桩执行单轮合成案例；可只调用 `execute_case` 一次，不创建 Langfuse 新实验。
- 待证伪假设：H1 supervisor 提交的 context 引文或 scope 不合格，机械 gate 拒绝；H2 context 合格但独立 Reviewer 拒绝；H3 proposed mutation 与用户请求不一致使 Reviewer 拒绝。删除回复高置信度候选为 `finalize_business_reply` 的共享 saved 文案。
- 2026-09-28 单次真实图复现与昨日结果不同：提取 context 全部合格、Reviewer tool_call decision=accept 且引文有效、save_preferences=saved、最终保存一条长期座位偏好。说明该样例并非稳定拒绝；现有 2026-09-27 远端 Trace 不含审核内部阶段，历史失败源头仍未证实。
- 追加两次相同真实模型回放也成功，三次成功只证明旧失败在当前环境未复现，不足以断定模型随机性或旧 run 的拒绝分支。需要旧审核输入/决定的原始观察、或在将来失败时记录最小脱敏阶段元数据，才能区分机械 gate 与 Reviewer。
- `finalize_business_reply` 对 `save_preferences`、`edit_preferences` 的 saved 工具结果均输出相同文案，本地直接复现；与旧 `explicit_delete` 的错误回复一致，根因定位置信度高。当前 9 个 `test_preference_autosave` 通过但未覆盖成功删除后的最终回复，应补图级回归断言后再修正渲染。

- `Settings.from_env()` 核查：模型、Langfuse 与政策/记忆数据库配置项均齐全，仅核对有无，未输出凭证。
- `evals/langfuse_memory.py` 已能用真实 `build_assistant`、`InMemorySaver`、隔离临时记忆和禁用供应商桩执行七条单轮记忆案例并上传开发实验；现有评分仅覆盖持久偏好合同，没有 LLM-as-judge。
- 88 条候选预期仍是 pending 草稿，不应作为金标或计算正式准确率；首批保守采用七条合成记忆案例做真实图开发试跑，将结构化硬断言与语义 Judge 分开。
- 本次七条真实图结果：六条记忆合同通过，一条 `explicit_add` 未保存靠过道座位偏好，`save_preferences` 返回 rejected，不能因回答如实告知未保存而判为任务成功。
- `explicit_delete` 实际删除酒店偏好并保留座位偏好，但回复“已记住你的偏好”误导；Judge v3 回复忠实度 0，硬合同仍通过。`explicit_update` 回复含糊，Judge v3 为 1。其余五条回复忠实度 2，其中 `explicit_add` 虽为 2，硬断言失败优先。
- 实验报告 `.verification/memory-real-20260927-escalated.json` 和最终 Judge `.verification/memory-judge-v3-20260927.json`；远端七条 Trace 均确认有硬分及 v3 语义分。v1/v2 试评分保留但不作为最终指标。

- Web 请求进入 `run_chat()` 后先获取会话级 advisory lock、打开 PostgreSQL checkpointer、处理 `request_id` 幂等，再进入资源准入和 `chat_runs` 记录。
- `prepare` 先由 `TurnRoute` 判断目标，再按领域调用 `TripExtraction` 或 `ExpenseExtraction`；机械校验和独立语义复核通过后，事项从 `pending_context_review` 变为 `context_reviewed`，任务进入 `ready`。
- `dispatch` 校验 subject revision 和依赖，任务变为 `running`；`tools` 执行领域工具；`advance` 根据工具结果写入 `completed`、`partial`、`needs_clarification`、`failed` 或 `cancelled`，并绑定 `result_ref`。
- `save_trip`、`confirm_facts` 通过 LangGraph `interrupt` 暂停，Web 运行最终状态为 `awaiting_confirmation`；下一次请求必须带相同 checkpoint revision 的 `resume=true/false`。
- 完成回答会写回 checkpoint，并把 `chat_runs` 标成 `completed`；异常或断流不自动重放，标为 `failed`/`interrupted`，前端阻断继续发送，需先核实后建立恢复对话。
- SSE 的 `progress` 是阶段事件，`delta` 是已完成任务的回答片段，`done` 是最终 checkpoint 快照；前端以最终快照覆盖临时流式文字。
# 2026-09-28 全量开发测评试跑

- 现有 `evals.runner` 对 52 条案例运行工程回归证据，不调用真实 Agent；它的通过率不能与 88 条语言案例相加。
- `expected_drafts.jsonl` 的 88 条仍全部 pending；80 条历史候选、8 条合成提案，含 16 组同输入及 4 条触发条件待修订。
- 模型、pgvector 政策后端、Langfuse 已配置；真实供应商必须用拒绝调用桩，短期 checkpoint 和长期偏好均用临时隔离存储。
- 开发试跑以“空初态的实际模型表现”呈现，不能复原历史验收的 checkpoint、故障注入或政策版本；逐轮输出须与 gold 评分分离。

## 2026-09-28 新模型测评批次发现

- 当前模型已通过复杂旅行路由单例：`history-0b8cfbe4b416f38491e6` 完成 `1/1` 轮，说明 `TurnRoute` 结构化 Tool Calling 当前可用。
- 单例输出文件为 `.verification/model-smoke-qwen-20260928.jsonl`，未启用 Langfuse，未调用真实供应商。
- `real_agent_batch` 在 `--case` 模式下仍按“是否完成全量 88 条”返回退出码 1；该退出码不是本次单例的运行失败。

## 2026-09-28 新模型全量运行发现

- 新模型批次 `qwen3.7-plus` 已完成 88/88 候选、230/230 用户轮次；未发生 provider connection/auth/balance/rate-limit 阻断。
- 候选级 `completed` 只表示 `graph.invoke` 返回并写入结果；不能作为质量通过。230 轮中 151 轮带有槽位/结构化校验错误，57 轮带有语义复核失败，69 轮明确为 `slot_validation_failed`。
- 真实工具调用被观察到：`consult_policy`、`query_memory`、`save_preferences`、`edit_preferences`、`plan_trip`、`read_task_result`、`query_weather`；由于供应商桩禁用，部分规划/天气调用按预期返回 unavailable/error。
- 试跑器当前没有把所有中间路由决策写入报告，部分完成轮的 `route` 为空；因此现有批次可用于失败分布与回答审查，不能直接做完整路由准确率。
- `history-726f06e575336a8faeae` 只记录 2/3 轮，是唯一轮次数量不一致案例，应在人工审查时优先核对其历史助手上下文。
- 预期草稿仍是 pending，且部分候选要求复现历史 checkpoint、政策版本或故障注入；不能用空初态开发运行结果替代正式金标。

## 2026-09-28 RAG 召回质量实测

- 真实运行命令：`.venv\Scripts\python.exe -m evals.rag_retrieval_eval run`；沙箱内因外部 Embedding 网络限制失败，授权外部网络后同一命令成功。
- 24 条自动生成草稿查询覆盖条款主题和住宿标准两类；当前只能证明工程链路和这批草稿的召回表现，不能宣称为人工审核后的正式数据集。
- RRF 在 Top-3/Top-5 均覆盖目标条款，零命中为 0；Top-1 为 `20/24`，即 `83.33%`。向量路线的 Top-3/Top-5 为 `24/24`，关键词路线 Top-5 为 `22/24`。
- 未命中的 Top-1 案例为 `rag-live-004`、`rag-live-010`、`rag-live-018`、`rag-live-022`。前两条是相邻条款竞争，后两条是同一省份相邻住宿记录竞争；目标项都仍在 Top-5。
- 后续正式化应人工审核 `gold_chunk_ids`：对同一条款的拆分单元可合并为相关集合；住宿查询需加入城市、人员类别和旺季条件，避免用“省名+非季节差价”生成过宽问题。完成审核后再重跑并冻结正式指标。

## 2026-09-28 独立 Ragas 评测

- 新增 `evals/ragas_retrieval_eval.py`，只读取已记录的召回 JSONL，不调用 Agent、Embedding 或政策数据库写操作。
- 使用 Ragas 0.4.3 的 `IDBasedContextPrecision` 与 `IDBasedContextRecall`，分别评测 `vector_ids`、`keyword_ids`、`rrf_ids`。
- 当前输出 `.verification/ragas-retrieval-20260928.json`：三条路线 ID-based recall 均为 `1.0`；vector/keyword/RRF 的 ID-based precision 分别为 `0.05`、`0.05`、`0.2`。
- Ragas 导入依赖一个缺失的可选 Vertex 模块；脚本只在进程内注入兼容 shim，没有修改 `.venv`、`requirements.txt` 或业务代码。
- precision 偏低不能直接解释为语义质量差：当前每条 draft 只标一个 gold chunk，而每条路线返回 20 或 5 个候选；正式评测前需人工把同条款拆分块、关联 scope 块和同一住宿标准行标为相关集合。
- 当前没有 Agent 最终回答和人工 reference answer，因此还不能运行有语义判断的 Context Precision/Recall、Faithfulness、Answer Relevancy；当前结果属于 ID overlap 测量。

## 2026-10-08 意图链路简化发现

- `TurnRoute` 负责操作/意图/目标；`update_facts` 不携带 intent，代码从目标任务继承，`change_intent` 需要当前原话授权。
- 领域提取收到完整路由 payload 和 `TurnRoute` 原始输出，同时也收到已经足以绑定任务索引、目标和意图的 `extraction_tasks`。前两者带来重复输入。
- `SemanticReview` 同时拿路由和领域工具 schema，提示词重判路由、状态、恢复及字段。应保留对证据支持、否定、归属、遗漏、歧义和用户授权的否决权，但不产生新意图。
- `reconcile_tasks` 检查 goal transition、revision 和依赖失效，属于代码不变量，不能为压缩流程删除。
- `policy_query.category/mode` 是 `policy_explain` 的答案范围槽位，不等同于顶层 intent；仍需保留独立复核的 fail-closed 行为。
- 工作区里 `business_travel/` 和本批测试均为用户已有未跟踪文件，不能使用 Git 基线回滚；项目 `.venv` 和 `requirements.txt` 已存在。
- 2026-10-08：新增边界测试已从旧测评引用文件移到 `tests/test_intent_boundary.py`；旧测评仍绑定 `tests/test_slot_protocol.py` 的历史哈希 `86605490...`，当前文件哈希不同且旧行号也已漂移。不能假定这只是本轮新增测试所致，更不能自动改写来源引用。
- 生产提取提供 `validation_errors`/`validation_guidance`，隔离对照实验不提供；领域输入的白名单应将这两个重试反馈键视为可选。
- 全量离线测试还受到 `evals/dataset.py` 将 `rag_retrieval_gold_draft.jsonl` 误读为正式 EvaluationCase 影响；这与本轮意图链路无直接关系。

# 2026-10-08 Langfuse Token 用量接入发现

- 项目使用本地 Langfuse SDK `4.15.2`；`Langfuse.start_observation` 支持原生 `usage_details`。
- 原有 `Observation.update()` 只调用 `raw.update(metadata=...)`，模型用量因此不是 Langfuse 原生用量字段。
- 原有 metadata 白名单没有 `total_tokens`，供应商返回的总量会被过滤。
- `chat_model` 节点由 `MetadataCallback.on_llm_end()` 统一结束，适合在该处同时写入 metadata 与 `usage_details`。
- 映射采用 `input_tokens -> input`、`output_tokens -> output`、`total_tokens -> total`，只接受非负整数并保留上限校验。
- 2026-10-09：当前最终业务回复已经是确定性模板，不需要额外模型调用；`graph.py:finalize_business_reply()` 同时负责历史消息截断、工具结果解析、工具分发、状态文案和行程缺字段处理，职责过多。领域渲染器已经按模块存在，本轮只抽离总调度并保留 `graph.py` 兼容导出。

## 2026-10-18 RAG 全链路复核

- 当前为检查请求，只允许诊断与离线验证，不扩展为自动修复、真实模型请求、政策发布或服务重启。
- 已确认实际项目目录为 TourAgent；没有 .codegraph 索引。使用现有虚拟环境和 unittest。
- 检索为精确向量 + jieba/PostgreSQL 关键词 + RRF，默认双路各 20 条、融合取 5 条；没有独立语义 reranker。RRF 后有范围条款和关联条款闭包扩展。
- 已运行 9 个相关模块共 203 项测试，全部通过，均为离线/模拟验证。
- 已通过真实 render_policy 复现引用展示断点：后端摘录存在，但正文不带逐结论标记，依据折叠区只有来源标签，没有原文摘录。
- 当前真实资料包离线组装为 137 个片段；检查未发现该正文中明确编号引用被 prepare_package 丢失的实例，不将该猜测报告为已确认故障。
- 2026-09-28 的 24 条检索评测是自动草稿，查询由目标原文生成，不能作为本次完整 RAG 质量证明或正式准确率。
- 已确认问题 1（优先）：graph.py:440 将用户 facts 直接传给 RAG；policy_pg.py:396 的精确住宿查询只取 lodging_standard，不调用现有身份映射。真实图夹具传入院士/北京后仍无 lodging_standard；同一真实规则能推导部级，同一附表明确给出 1100。RAG requirements 却得到 missing_fields=[lodging_standard]、reference_only=true，不能给出完整个人限额。ANSWER_PROMPT/REVIEW_PROMPT 仍使用改映射前的“不可按交通类别推断住宿档次”口径，需与审核规则来源统一，不可直接伪造用户事实。
- 已确认问题 2：policy_guard.py:130 只输出 claim.text，139 只渲染来源标签，丢失逐结论引用标记和原文摘录。web_static/message-view.js 使用纯文本及折叠区，无逐引用定位入口；不能认为后端有 citations 就已实现用户可见证据绑定。
- 已确认问题 3：policy_rag.py:92 的 PolicyAnswerReview 未验证 verdict 和 issues 一致性；419 仅凭 accept 放行。模拟复核同时返回 accept 和 unsupported_claim，真实 explain_policy 最终为 answered。终止阶段有逐项证据检查，普通阶段没有同等结构约束。
- 评测缺口：evals/rag_retrieval_eval.py:205 将 RRF 的初始 IDs 直接用作 final_source_ids，不经过生产检索的范围/关联扩展、精确附表和预算，也未生成/审核回答；citation_target_coverage 因而不是最终回答引用覆盖率。
- 非故障/设计边界：版本取管理员最新发布是现有明确约定，不按出差日期挑版本；上下文不完整/超预算直接停止是避免缺条款误答的既定策略。没有 reranker 本身不证明效果差，收益需用自然语言人工审核集对照测量。
- 证据范围限制：本轮未核实线上加载配置、数据库已发布内容、实际 embedding 排序质量或真实模型语义审核效果；离线通过不能替代端到端质量验收。

## 2026-10-09 JEV 接入

- JEV 接入点是 `business_travel/graph.py:build_assistant().prepare()`；每轮先调用 JEV，再进入现有 `extract_slots -> TurnRoute`。
- JEV 只提供 `intent/query_scope/transport/journey` 四类白名单 hint；证据、槽位、任务拆分、语义复核和执行门禁仍由现有链路负责。
- `JEV_MODE=primary` 将 hint 作为 TurnRoute 的独立信号；`shadow` 仍调用并观测，但不影响路由。
- 缺少 `JEV_API_KEY`、答案字段非法、HTTP/网络失败均降级到原有模型链路，不写入事项。
- JEV 返回的 `usage.input_tokens/output_tokens/total_tokens/charged_tokens` 在 `jev_decision` generation 观测中记录；Langfuse 是否显示仍取决于 Langfuse 配置和服务连通性。

## 2026-10-12 主助手日期后移

- `build_assistant` 在创建图时将 `date.today()` 插入规则提示词第一句；这使跨天的开头内容不同，也使常驻服务可能继续沿用启动日。
- `agent_graph.assistant` 已有 `fixed` 消息列表，之后才附加历史；可将日期添加在任务动态上下文后，保持日期可用且规则前缀稳定。
- 工作区已有 Jev 等未提交改动；本轮仅改动日期相关行和测试。
- 默认 `suffix` 使主助手规则文本稳定，日期按每次模型调用读取；`prefix` 恢复原来的构图时内嵌日期，用于基线对照。仅改变主助手，不改 Jev 和其他模型。
- 缓存收益不能由离线测试证明，需用同等请求的供应商缓存用量与费用数据比较。

## 2026-10-15 Candidate selection input compaction

- Planner sends full Candidate dumps and per-candidate timeline checks; selects
  IDs and retrieves full objects from collected for subsequent validation.
- Compact mode will remove only empty dictionary values, internal source/timestamps,
  hotel supplier IDs, and confirmed duplicate fields. Preserve unknown extra facts,
  dates, full quotes, stock counts, cancellation text, and completeness flags.
- Keep requested dates and nonempty truncation flags. No amount conversion, duration
  enrichment, room-rate pruning, or reinterpretation of supplier stock counts.
- Full mode must reproduce the prior candidate input exactly.
- Implemented TRAVEL_PLANNER_CANDIDATE_MODE=compact (default) / full. No
  change to supplier observations, stored candidates, policy input, or display.
- Preserved requested dates, contradictory duplicates, nonempty URLs, empty rate
  lists, false/zero facts, nonempty truncated_fields, unknown extra facts, and quotes.
- Real samples show character reductions of 20.8% hotel, 19.2% train, and 22.6%
  flight; these are not token/cost reductions or evidence of equal real-model quality.

## 2026-10-16 酒店候选已返回但方案缺少酒店诊断

- 用户截图中的 Langfuse/观测树显示 `query_hotel` 已返回 `candidates`，因此“供应商没有返回酒店”不能作为当前首要解释。
- `business_travel/planning.py` 的流程是：查询候选 -> 汇总 `collected` -> 调用 `PlanSelection` -> 计算 `missing_components` -> 渲染结果。酒店候选只有进入 `selected` 才会被视为方案中的酒店。
- `date_verified=false` 与 `inventory_verified=false` 在当前实现中不会直接阻止候选进入选择；`PlanSelection` 明确禁止的是 `hotel_detail.availability == "sold_out"`，以及 `check_timeline` 报出的时间冲突。
- 若选择模型调用失败、超预算、返回空选择或选择结果被校验拒绝，候选会保留在 `alternatives`，最终渲染仍会提示“已查到待选候选”并追加“方案仍缺少：酒店”。
- 尚需从该次运行的 `planning` 结果确认 `selection_status`、`selected`、`alternatives`、`missing_components`、`rejected_selections` 与酒店 `hotel_detail`，才能把“模型未选中”和“酒店被 sold_out/时间冲突拒绝”区分为已证实根因。
- 读取供应商实现时首次误写文件名 `uniu.py`，实际文件名为 `business_travel/tuniu.py`；未对项目文件造成修改。

## 2026-10-17 推荐阶段标准前置诊断

- `graph.py:375` 已调用 `policy.resolve_planning_standards(request)`，但基础 `PolicyStore` 不提供身份映射，`PostgresPolicyStore` 只返回补充后的 `transport_standard` 和 `lodging_standard`，没有把允许席别或住宿限额传给规划器。
- `planning.py` 的酒店排序当前只看库存、详情状态、报价、地址和名称；选择提示只禁止售罄酒店，政策核验在模型选择完成后才执行。
- 现有项目政策资料已经提供 `transport_classes`、类别到住宿档次的映射，以及按城市/日期/住宿档次查询住宿限额的 `lookup_lodging()`，适合新增一次性的推荐政策画像。
- 当前输出中的“标准核对与待补充信息”属于选择后的核验结果，不能承担推荐前筛选职责；需要新增面向推荐的候选状态，并将用户可读标准放在推荐结果附近。
- 计划保留所有候选和原有政策核验：`within_standard` 优先、`unknown` 次之、`over_standard` 仅作备选；政策服务失败时状态为 `unverified`，不推断标准内。

## 2026-10-17 推荐阶段差旅标准实现

- 已确认原问题是流程顺序问题：规划先把候选交给模型选择，之后才调用正式政策核验，所以模型可能先选出超过住宿限额的酒店。
- `PostgresPolicyStore.recommendation_profile()` 现在在供应商检索前提供推荐所需的政策事实；政策服务不可用时保持 `unverified`，不伪造标准内结论。
- 候选标准状态只用于推荐排序和选择约束，不替代正式政策核验，也不改变供应商原始候选的保存。
- 酒店比较使用推荐报价、币种和住宿上限；交通比较使用候选的 `transport_mode` 与 `seat_class` 对照政策允许席别。
- 若存在标准内且有库存酒店，选择器只能看到标准内可选酒店；若没有标准内酒店，则依次允许未知/未核实候选，最后才允许超标准候选作为备选。
- 结果顶部显示具体标准；超标准候选放在末尾的 `超标准备选` 折叠区，技术核验仍放在输出末尾。
## 2026-10-18 推荐输出修正完成

- 推荐正文依次展示交通、酒店、具体差旅标准；计价口径、库存快照和普通核验缺项集中在末尾折叠区。确定超标、人工核验和时间冲突仍保留可见提醒。
- 酒店正文展示有正整数库存且报价有效的最低返回房型，附床型、早餐和参考报价；不推断每晚单位，不将售罄或库存未知房型作为正文房型。
- 未提供入住条件时，酒店/组合查询默认 1 间房、1 名成人、0 名儿童；默认来源单独记录，不写成用户证据，不覆盖明确多人或部分团体条件。TRAVEL_DEFAULT_SINGLE_GUEST=0 可恢复旧基线。
- 搜索起价的标准比较明确标注为估算。未知席别使用 transport_class_mapping_required；只有政策明确列出的席别不在当前类别允许范围内才判超标准。
- 离线图集成夹具验证默认入住参数实际到达供应商适配器；未触发真实供应商请求，未启动或重启运行服务。

## 2026-10-18 行程推荐引用缺口

- 用户提供的实际输出属于 render_draft，上一轮引用只接入 render_policy。前端已有引用交互，但推荐 profile 没有 sources，也没有逐字段引用绑定。
- 推荐 profile 已在当前发布文档事务内读取审核规则和精确住宿限额，可在同一事务补取交通条款和限额行，避免新增模型调用。
- 本轮修复限定于推荐政策标准的依据传递和展示；不将供应商价格或库存标为政策证据，不改酒店排序或正式核验规则。
- 已完成：recommendation_profile 附同一发布事务取得的来源和 standard_citations，render_draft 按标准字段渲染引用，折叠区展示条款/文件/版本/表格坐标及摘录。配置映射明确标为非制度原文。
- 当前真实前端的行程工具渲染入口与最终回复组装均有回归；浏览器结果在 .verification/plan-citations/results.json，桌面/手机截图已目视检查。
- 新增复现同一消息合并多个工具结果时重复“来源1”跳错段落，前端已按引用之后最近的对应依据定位，两个浏览器脚本验证通过。
- 旧消息缺少存储来源时保持可读，但不会凭空生成引用；需要重启加载新代码后重新查询，才能产生带来源的新推荐。

## 2026-10-18 RAG 修复实施验证中

- 身份映射复用当前发布规则，以独立 reviewed_rule 配置来源提供，不改用户 facts。严格学生适用条件保留，推荐默认不代替个人政策适用条件。
- 每条政策结论附引用标记；正文点击展开末尾对应条款，配置依据与制度原文分开标识。
- accept 同时带 issues 的审核结果已被协议拒绝；168 项定向回归通过，后续补测边界与浏览器交互。
- 检索评测默认走生产检索及实际证据预算，baseline 保留；明确没有测量回答引用覆盖。
- 最终验证：新增 17 项回归通过，覆盖严格身份映射、季节/地区缺项、矛盾审核重试、引用展示和评测错误路径。274 项组合回归与 4 项前端解析测试通过，追加版本边界回归通过。
- 浏览器组件验收通过引用点击、键盘操作、同页消息来源隔离、流式保持焦点和展开状态；1440/390/320 均无横向溢出，已目视检查 390 视口的原文展开截图。
- 评测默认文件按 production/baseline 分开，不再默认覆盖 2026-09-28 的历史结果；不同 gold_version 单独列出不可比数量，检索服务失败仍记为未命中。
- 验证边界：夹具和离线检查，不代表真实模型语义质量提升，未进行线上数据库检索或重启服务。

# 2026-10-16 酒店漏选修复

- 事实：酒店查询成功不代表模型一定会在合法的部分选择中包含酒店。
- 根因：原提示词允许部分选择，代码只校验 ID、重复组件、售罄和时间冲突，没有校验可用酒店必选。
- 处理：酒店按可用性、详情状态、价格和地址排序；选择阶段检测酒店缺失并给模型一次明确重选机会。
- 边界：没有可选酒店时仍允许缺失酒店；重选后仍未选择时保留 `incomplete` 和候选结果，不凭空自动补酒店。
# 2026-10-18 精确政策引用与证据去重

## 2026-10-18 费用报销金额测算

- 已有 policy_check 入口使用 ExpenseFacts 和 policy.check，住宿返回 cap/amount/excess，交通只核对席别。
- 实现必须补足计算方式和原文绑定，不能把所有超标费用机械套用 min(amount, cap)。
- 现有未提交改动包含跨轮范围扩展、向量容错和真实验证记录，本轮保留。
- 原文未给出所有超标住宿统一截额报销的规则；测算区分标准内参考金额、数学差额与最终核定金额。
- 新增实际/假设场景隔离、逐晚金额与总价范围、票面费用与混合支付范围。新模式复用同事务已发布规则和来源；原 check 与推荐核验路径保留。
- 首版不自动核算混合支付中的附加费/保险/退改费用，明确展示未测算部分，不把未知记为零。
- 金额结论必须有同文档、同版本的真实正文/附表来源；缺失时移除金额结论。
- 交通引用除条款标题外，还要求匹配已核对类别的真实表格行；不展示其他类别行作为本人的依据。
- 住宿金额比较仅在币种、人数和计价单位通过原规则检查时计算，防止将外币/合住金额与人民币个人限额直接相减。通用企业规则的每间每晚单位与学校每人每天单位分别保留。
- 实际/假设分类变化要求独立事项，原实际费用不会被假设金额覆盖；金额测算不增加模型调用或修改复核开关。
- 全量测试失败与先前 findings 的历史评测源哈希/草稿格式问题一致；当前定向 445 项全部通过。

## 2026-10-18 Qwen rerank API

- 当前每路召回20条，RRF立即截取5条；重排必须在截取之前读取同一发布文档的候选文本。
- 官方指南同时支持兼容排序与 DashScope 原生协议。本轮选择原生协议：/api/v1/services/rerank/text-rerank/text-rerank，请求 input.query/documents 和 parameters.top_n/return_documents，响应 output.results[index,relevance_score]。
- 官方 Base URL 总览确认 dashscope.aliyuncs.com 原共享域名仍可使用，不强制业务空间ID；前期误把推荐地址当成必需，已向用户纠正。
- 用户提供 maas.qianwenaiapi.com 的原生调用示例，采用其地址为默认；模型仍默认最初指定的 qwen3-rerank，支持 .env 切换 qwen3.7-text-rerank。
- 使用现有 httpx 和 unittest，无需新增依赖。必要关联条款与精确住宿行继续由现有代码补入。

- policy_pg._recommendation_evidence 把 P1 配置与所有 scope/transport 条款绑定到交通类别和席别；住宿档次仅绑定 P1。
- plan_checks._recommendation_profile_lines 以 source ID 元组去重，重叠组合重复输出 P1，且直接展示完整配置 JSON。
- 采用真实资料包和模拟数据库离线验证，不将静态代码或历史截图视为当前服务运行证据。
- 住宿档次映射可能来自兼容默认规则，必须与发布配置明确区分；不能伪称制度原文。
- 已采用独立片段与绑定表，原始 sources/standard_citations 保留审计和 grouped 基线。交通按结构化标准单元格定位唯一行，保留完整人员条件，仅展示相关交通列。
- 项目席别列表和住宿映射分别投影为中文口径；兼容默认住宿映射明确标注未写入当前发布配置。学生条件在酒店/交通范围均保留。
- 新版引用核对来源元数据、父文本校验和、摘录精确包含关系、字段值、查询上下文和重复标识歧义。该校验防止错配或损坏，不代表密码学签名或通用语义证明。
- 多岗位身份保留“就高”的对应段落；学生酒店查询也保留活动适用条款，未确认与明确不适用两种情况分别提示。正文推荐和既有严格核验规则未被放宽。

## 2026-10-18 用户引用仅展示真实条款

- 用户明确希望引用只展示真实制度条款，不希望看到项目配置、兼容默认值的实现细节。
- 当前默认 fragment_references 同时注册 original/transport_rule/lodging_rule；应仅把真实 original 作为政策来源，项目规则保留后台用于判断和实验基线。
- 金额附表只支持目的地/档次/金额，不证明身份到住宿档次的对应。缺少原文对应的映射需要以推荐假设说明，而非新增政策来源。
- 已将默认公开引用限定为 original，配置仍在后台核对但不注册来源编号；非原文 source_type 不允许伪装成 original。
- 学生适用条款改绑 lodging_applicability，在末尾单独引用，避免把第三十五条当作“第三类对应其他人员”的住宿映射证明。
- 样例确认：交通标准共用第六条相关表格原文，住宿金额引用北京附表；住宿档次不附虚假来源，仅在末尾推荐说明中注明个人档次尚待核对。

## 2026-10-18 HNSW 切换发现

- 全局 HNSW 索引必须与 `embedding::vector(<dimensions>)` 表达式和 `vector_cosine_ops` 对齐；查询不能继续使用物化候选集，否则索引无法承担近似排序。
- `document_id` 过滤保留在同一 SQL 中；由于近似索引在过滤后可能有召回损失，默认提升 `hnsw.ef_search`，并继续保留精确排序作为缺失索引回退/基线。
- 按文档动态创建 partial index 不适合运行时查询：活动文档变化后索引名和 SQL 计划难以维护；采用全局 HNSW 索引并通过维度配置管理。
- 初始化/导入可自动确保索引存在；已有数据库提供显式迁移命令。查询先检查目标索引是否存在，缺失时返回精确检索结果并在 rankings 中标明 `vector_search_mode=exact_fallback`。

## HNSW 实现结果

- 新增 `business_travel.policy_hnsw`，统一校验维度、M、ef_construction、ef_search，并生成全局索引 DDL。
- `PostgresPolicyStore.initialize` 和 `stage` 会确保当前维度索引存在；`migrate_hnsw` 为已有数据库提供显式入口。
- 向量查询先检查 `to_regclass`，有索引时使用 `embedding::vector(dim) <=> query::vector(dim)` 和 `hnsw.ef_search`；无索引时使用原 `MATERIALIZED` 精确排序并标记 `exact_fallback`。
- 离线测试覆盖 HNSW DDL、运行时模式标记和缺失索引回退；没有连接真实 PostgreSQL，因此索引是否被实际选用仍需现场 EXPLAIN 验证。

## 2026-10-18 政策意图路由问题

- “标准是多少/怎么规定/政策原文”属于解释型目标；“能不能报/是否超标/这笔费用/发票”属于实际核验型目标。
- 当前 `consult_policy` 根据 route 选择 explain/check；check 分支先校验 `ExpenseFacts`，缺少 `expense_date` 时不会进入 RAG。
- JEV primary route 的模型意图可能把解释型问题误判为 `policy_check`。在空白首轮可用窄范围本地语义纠偏，不影响已有任务更新和显式核验词。

## 政策意图路由修复结果

- `JevDecision.primary_route` 现在对空白首轮的 `policy_check` 做窄范围纠偏：解释型关键词改为 `policy_explain`，明确报销/超标/实际费用关键词保持 `policy_check`。
- 修复后“我是院士去北京出差，住宿标准是多少”会进入政策解释链路，具备进入 `policy_retrieve` 和 `policy_rerank` 的条件；实际是否成功仍取决于当前发布政策、Embedding 和 Rerank 服务。
- 未改变已有任务更新、历史任务控制和非首轮路由。

## 2026-10-18 政策意图定义精细化

- 原 JEV 意图描述只有“核对报销、差旅政策或合规条件”和“解释差旅政策、规则或条款含义”，动作边界不足，无法稳定区分“了解制度”和“验证具体费用”。
- 已将 `policy_check` 定义为验证具体/拟发生费用或情形，覆盖能否报销、是否超标、是否合规、是否需要审批，并明确金额、日期、发票、实际支付、审批状态等事实信号。
- 已将 `policy_explain` 定义为查询或解释制度本身，覆盖标准、限额、适用条件、规则含义、条款原文和政策依据，并明确带身份/城市/日期不改变其解释目标。
- JEV 的 intent instructions 增加动作优先级：同时出现解释和核验表达时，具体费用/情形核验优先；只有制度问题才走 `policy_explain`。
- 新增回归测试验证上述定义确实进入 JEV 请求体；`tests.test_jev` 与相关政策测试共 102 项通过。
- 按用户要求移除 `_POLICY_EXPLAIN_CUES`、`_POLICY_CHECK_CUES` 和 `_refine_policy_intent`，`primary_route()` 现在完全保留 JEV 返回的意图，用于验证精细定义本身的效果。

## 2026-10-18 解释型问题检索失败诊断

- 用户 Langfuse 截图显示链路已进入 `consult_policy -> policy_rag -> policy_retrieve -> policy_result`，没有 `policy_rerank`；最终回答为“政策资料检索失败”。
- `policy_rag.explain_policy()` 在 `policy_retrieve` 返回 `status` 不是 `ok/empty` 或检索抛出异常时直接以 `stage=policy_retrieve, failure_code=policy_retrieval_failed` 结束，因此当前问题发生在 Rerank 之前。
- `PostgresPolicyStore.search()` 的主要可用性前置条件包括：当前 company/policy_key 存在已发布文档、发布文档的 embedding_space/tokenizer 与当前配置一致、查询 Embedding 成功且维度为1024、数据库连接/权限/表结构可用。HNSW 迁移成功只覆盖索引 DDL，不覆盖这些条件。
- 当前 `policy_admin.py check-config` 只检查缺失配置，固定输出 `database_verified=false`、`embedding_verified=false`，不能证明真实检索成功。
- 当前证据不足以区分具体错误码；需要查看 Langfuse 的 `policy_retrieve` Raw 输出，或运行 `policy_admin.py search --question ...` 获取 `status/reason`。不应先修改 HNSW 或 Rerank。
- 用户 CLI 已确认具体原因是 `embedding_request_rejected`。已将 Embedding 请求补充为显式 `dimensions`，并把非 200 响应安全细化为 `embedding_request_rejected_http_<status>`，便于区分请求参数、鉴权、额度和服务端错误。
- 复核官方兼容接口后发现，`dimensions` 不能对所有模型无条件发送；当前 qwen3.7 兼容调用恢复不带该字段，仅对 `text-embedding-v3/v4` 发送。错误码现在还会安全保留服务商 JSON 中的 ASCII `error.code`，不保留原始消息。

## 2026-10-18 Langfuse 最新失败 Trace 诊断

- Trace 时间：2026-10-18 17:57:07（本地时间），用户第二轮输入“目的地北京”。
- 观察到的链路：`chat -> jev_decision -> slot_extraction`；没有 `consult_policy`、`policy_rag`、`policy_retrieve` 或 `policy_rerank`。
- Langfuse 的三次 `slot_validation` 输出均为 `policy_query_required`，三次尝试都未进入 `slot_review`，最终 `slot_commit` 为 `slots_committed=false`。
- 下载的 Trace JSON 显示第一次已有事项的 `policy_query={category:hotel,mode:personal_limit}`，当前槽位已有 `kind=hotel`、`employee_grade=院士`；第二轮 ExpenseExtraction 模型三次均只提交 `expense.destination=北京` 和证据，省略了未变化的 `policy_query`。
- 代码 `business_travel/slots.py:503-505` 对所有 `policy_explain` patch 强制要求模型重复提交 `policy_query`；因此模型省略未变化的 answer scope 后，机械校验直接失败。`business_travel/slot_protocol.py:293-297` 的提示虽要求 follow-up 重复提交，但只能提高概率，不能保证结构化模型输出。
- 结论：当前失败点是 follow-up 槽位协议的状态继承设计，RAG/Embedding/HNSW/Rerank 尚未执行；此前 `AllocationQuota.FreeTierOnly` 是另一条独立的向量检索配置/配额问题。
- 第一轮 Trace `c1abb805...`（17:54:14）包含 `consult_policy -> policy_rag -> policy_retrieve -> policy_rerank -> policy_result`；`policy_rerank` 耗时约 0.44s，输出 `status=ok`、`model=qwen3-rerank`，选出 `b0008-u1-0`、`b0008-u2-0`、`lodging-r9` 等候选。说明 Rerank 已真实执行；第二轮看不到它是因为在槽位校验前提前结束。
- 额外发现：第一轮 ExpenseExtraction 将“院士”写入 `employee_grade`，但当前高校政策映射 `business_travel/policy_pg.py:491-505` 只读取 `academic_title`、技术/管理岗位等字段；审核规则明确 `academic_title=院士 -> 一类 -> 部级`，测试 `tests/test_rag_corrections.py:105-116` 也以此为契约。因此第一轮虽然成功进入 RAG/Rerank，却因身份字段错位没有应用“院士 -> 部级”，返回了住宿附表档次追问。
- `business_travel/contracts.py:46-50` 与 `business_travel/policy_rules.py:19-22` 当前字段描述对 `employee_grade`/`academic_title` 区分不够具体，模型把学术称号误放进了通用 grade 字段；这是独立于 `policy_query_required` 的第二个质量问题。

# 2026-10-18 全架构复核（当前轮）

目标：在不修改业务代码的前提下，审查意图路由、槽位继承、门禁、RAG/Rerank、错误恢复、任务调度和 Langfuse 观测，区分结构性问题、过严门禁与可接受安全边界。

当前状态：只读诊断进行中；保留全部用户未提交改动。

已确认运行证据：
- Langfuse 2026-10-18 17:54:14：jev_decision 选择 policy_explain（confidence 1），随后 slot_extraction、slot_review、consult_policy、policy_rag、policy_retrieve、policy_rerank、policy_result 均执行；Rerank status=ok，模型 qwen3-rerank，耗时约 428ms。
- 同一首轮 Trace 的 Rerank 输入含北京住宿标准 1100、院士对应一类交通等候选；输出 selected_ids 5 个，说明检索和重排不是当前首轮阻断点。
- Langfuse 2026-10-18 17:57:07：输入“目的地北京”只执行 jev_decision、slot_extraction 和三轮槽位协议/校验，最终输出 policy_query_required，没有 RAG。
- 首轮 Trace 的 JEV 路由 prompt/输出明确为 policy_explain；问题出现在领域槽位和后端状态承接，不是意图识别。

高置信结构性问题：
1. Follow-up 槽位协议要求 policy_explain patch 每轮提交 policy_query，后端没有在未变化时从现有 subject 自动继承；模型省略旧字段即被拒绝，导致目的地补充不能进入 RAG。
2. employee_grade=院士 与高校规则要求的 academic_title=院士字段错位，导致规则映射不能自动得出住宿档次。
3. PostgresPolicyStore.search() 先强制调用 Embedding；Embedding 400/配额错误在 except PolicyError 直接返回 unavailable，关键词召回未执行。当前 Hybrid/RRF 实际是向量硬门禁，Embedding 故障会令整个 RAG 不可用。
4. graph.py 把槽位校验失败统一渲染为“模型输出的事项信息未通过校验”，用户和 Langfuse 根节点难以看到具体失败码；底层 span 有 policy_query_required，外层输出没有透传。
5. RAG 召回、精确政策规则、结果渲染职责分散在多个模块，任务状态以 dict 贯穿；存在“查询证据已成功但标准映射/回答模板仍停在追问”的跨层耦合风险。
6. graph.py 的 consult_policy 分支把 policy_explain 绑定到 RAG，把 policy_check 绑定到确定性规则；意图误判为 check 时不会出现 RAG/Rerank，这属于硬分支边界，不是随机失效。

待核对：
- context_reviewed 是否同时承担证据审核、槽位完整性和执行准入，是否存在过宽门禁。
- consult_policy 的 check/explain 路径是否混合了两个不同的业务契约。
- RAG empty/unavailable/clarify/partial 状态在任务调度与最终回复中的映射是否丢失诊断信息。
- Langfuse observation 白名单、子 span 父子关系及输入输出脱敏是否会隐藏关键字段。


## 当前轮补充证据与测试

- 现有三组回归共 315 项：槽位/图/恢复 104 项通过；JEV/高校政策/政策要求 69 项通过；RAG/Hybrid/Rerank/RAG 修复 142 项中 141 项通过，1 项失败：test_missing_destination_or_seasonal_date_still_requires_clarification 期望 rag.status=clarify，当前得到 insufficient。该失败说明个人住宿查询的缺失条件状态契约已有回归漂移，需要单独修复或明确口径。
- stage_submission() 已支持 policy_explain 的增量合并：省略 policy_query 时会保留旧值（slots.py:239-252）；真正阻断发生在 propose_slots() 的前置机械校验（slots.py:503-515）。这是上下层契约矛盾，不是数据库或 RAG 检索错误。
- 更新任务的 question 在 reconcile_tasks() 中直接写入模型提交值（tasks.py:105-110）；consult_policy() 又把 current_task.question 当作 RAG question（graph.py:424）。短追问“目的地北京”可能把原始政策目标替换掉，导致即使放开 policy_query 门禁，RAG 查询仍可能只看到“目的地北京”。
- context_reviewed 的生成路径是整批槽位提交先经过机械校验和一次语义复核，再把所有 pending_context_review 标记为 context_reviewed（slots.py:603-634）。它同时承担证据接受和 specialist 执行准入；reviewer 不可用或一条任务 revise 会阻断整批任务。
- JEV primary 首轮可直接构造任务并跳过本地 TurnRoute（jev.py:75-106，slots.py:460-468），当前没有本地二次意图校正；JEV 还未接入 BudgetedModel 的 consume_model_call，调用预算可能低估。
- web 入口固定 recursion_limit=30（web.py:362-364），与 Settings.max_steps 脱钩，配置无法影响 Web 图上限。
- 普通 RAG 回答在第一次 PolicyAnswerReview accept 时直接返回；更强的 PolicyEvidenceReview 只在最终收尾阶段使用（policy_rag.py:420-450）。引用字符串校验是确定性的，但政策结论的完整性、条件、否定、适用性主要依赖单次语义复核，存在“引用是真的但结论不完整”的质量风险。
- RAG 证据预算超限或关联闭包不完整时直接返回 incomplete，不生成核心命中加 partial（policy_rag.py:542-547；policy_pg.py:614-645），属于安全优先但可用性偏低的硬门禁。

## 2026-10-18 全架构复核补充证据（本轮）

### 已复现

- 定向运行 `tests.test_policy_rag tests.test_policy_hybrid tests.test_policy_requirements tests.test_rag_corrections tests.test_travel_graph`：164 项，163 通过、1 项失败。
- 失败为 `test_missing_destination_or_seasonal_date_still_requires_clarification`：个人住宿查询缺目的地时，契约期望 `rag.status=clarify`，实际为 `insufficient`。
- 全量 `unittest discover`：1062 项，3 failures、5 errors、90 skipped。额外失败主要来自历史评测引用源已变化、旧评测 JSON 不符合当前 schema、一个旧观测断言包含 `usage_details=None`；这些与当前线上 RAG 阻断不是同一根因。

### 代码事实

- `graph.py:258-338` 将 JEV 分类、槽位提取、语义复核、任务合并和失败反馈集中在 `prepare`；任何槽位校验失败都会在 dispatch 之前终止任务。
- `graph.py:414-454` 按 route 二分：`policy_check` 只执行确定性 `policy.check`，`policy_explain` 才进入 `explain_policy`、检索和 Rerank。
- `slots.py:503-515` 对每个 `policy_explain` patch 强制要求 `policy_query`；`stage_submission()` 已有省略字段时保留旧值的增量合并逻辑，形成上下层契约矛盾。
- `tasks.py:101-110` 用本轮 proposal.question 直接覆盖任务 question；`graph.py:424` 再把 current_task.question 直接作为 RAG query，补充轮短句可能替换原政策目标。
- `policy_pg.py:567-660` 先调用 Embedding，再执行关键词召回；Embedding 失败直接返回 unavailable，没有 keyword-only 降级。
- `web.py:362-364` 固定 `recursion_limit=30`，未读取 `settings.max_steps`。
- `jev.py:149-205` 直接通过 httpx 调用 JEV，未调用 `consume_model_call()`，请求模型调用预算因此低估。
- `observability.py` 虽允许 `prepare/dispatch/advance` 名称，但主图没有显式为这些阶段创建 observation；外层只保留 slot_commit 和 task_result，错误码需要从子 span 反推。

### 结论

主体架构不是完全失效；高优先级问题是跨层状态与故障契约，门禁本身在很多地方是有意的安全策略，但当前把“字段未重复提交”“Embedding 临时不可用”“解释/核验路由误判”都变成了整轮不可执行。

## 2026-10-18 槽位门禁最小复现（当前轮）

- 观察事实：`propose_slots()` 在 `business_travel/slots.py:504` 对所有 `policy_explain` patch 无条件要求 `policy_query`；但 `stage_submission()` 已按“缺省时保留旧值”的方式合并，前后协议不一致。
- 观察事实：`ExpenseTaskPatch.policy_query` 的协议描述写成“including follow-up updates”，促使模型在只补充目的地时重复提交 scope；漏提交则重试，最终 `slot_validation_failed`，RAG 不会执行。
- 观察事实：`extract_slots()` 记录任务 question 时使用 `task.question or users[-1]`；`reconcile_tasks()` 无条件用 proposal question 覆盖任务 question。若 follow-up 只写“北京”，canonical goal 可能退化为“北京”。
- 修复假设：更新事项缺省 `policy_query` 应继承目标 subject 的旧 scope；只有明确 scope 改变时才要求新 query。更新的 canonical question 应保留旧任务目标，并由 follow-up question 只在明确重写完整目标时替换。
- 当前范围：只修复槽位协议/任务目标连续性并添加回归；不删除语义复核、版本校验或政策证据门禁。

## 2026-10-18 真实浏览器与 canonical 目标复核（当前轮）

### 观察事实

- 8001 隔离服务在浏览器提交时返回“请求来源不允许”；该进程沿用了 `TRAVEL_WEB_ORIGIN=http://127.0.0.1:8000`，因此不能把 8001 的业务失败归因于 RAG。
- 8002 使用同一工作区代码并把 Origin 配为 8002 后，页面可以建立请求；当前受限运行环境无法连接外部模型服务，页面返回“无法连接模型服务”，没有进入可比较的真实模型回答。
- `policy_admin.py search` 对真实政策库执行查询时，Embedding 请求返回 `embedding_request_failed_check_configuration`，但关键词召回、RRF、来源闭包仍完成，结果为 `status=ok`、`degraded=true`。
- 原实现对显式 `policy_query` 范围变更可能继续保留旧 `canonical_question`；这会让新范围使用旧检索目标。

### 修复与验证

- `propose_slots()` 对范围真正变化的更新要求新的独立问题；事实-only 更新继续继承旧范围和旧 canonical 目标。
- 新增 `question_required_for_policy_scope_change` 的固定修复提示，未写入任何具体身份、城市或样例关键词。
- `tests.test_task_operations` 13 项通过；`compileall` 与 `git diff --check` 通过。

### 可见性边界

- 当前工作区进程的外部模型/观测请求受执行环境网络权限限制；需在允许访问供应商的隔离进程中重跑浏览器，才能确认真实模型下的 Langfuse 父子节点和跨轮回答。

## 2026-10-18 全架构复核（本轮补充）

### 观察事实
- `graph.py:424-435` 的 `policy_explain` 把 `current_task.question` 作为本轮 RAG query；任务表由 `tasks.py:105-110` 每轮直接写入 `proposal["question"]`，所以 follow-up 的短句可能覆盖原始目标。
- `policy_pg.py:577-586` 先强制请求 query embedding；异常进入 `PolicyError` 后在 `policy_pg.py:663-664` 统一返回 `unavailable`，关键词查询没有执行降级。
- `policy_rag.py:545-547` 在证据关联缺失或上下文超预算时直接返回 `incomplete`，不会把已确认的 claims 组织成 `partial`。
- `web.py:363` 固定使用 `recursion_limit=30`，没有读取 `settings.max_steps`。
- `jev.py:265-285` 每个有用户消息的 prepare 都会调用 JEV；调用不经过 `budgeted_model`，也没有与本轮模型预算统一计数。

### 解释/置信度
- 高置信度：第一、第二项可直接解释已观测的 follow-up 丢失目标及 embedding 失败导致 RAG 不可用。
- 中置信度：证据超预算和 Web 步数属于边界可用性缺陷，需对应场景回放验证影响范围。
- 中置信度：JEV 配额问题取决于部署模式，但代码层确实缺少统一预算约束。

### 结论
架构主体（路由→槽位审核→任务调度→政策规则/RAG→引用审核）可工作；主要问题是跨层契约没有单一真源、外部检索无降级，以及失败状态过早终止。修复优先级应先处理 follow-up 继承和 canonical goal，再处理 embedding keyword fallback，最后补齐预算/观测。

## 2026-10-18 身份字段兼容修复（子任务）

- 修改 `business_travel/university_policy.py`：大学规则映射在 `academic_title` 缺失时，仅复用规则明确登记在 `title_categories` 中的 `employee_grade` 值。
- 新增 `tests/test_university_policy.py` 回归：`employee_grade=院士` 可映射为一类/部级；`普通员工` 不会被误映射。
- 单测结果：身份回归通过；组合 `tests.test_university_policy tests.test_rag_corrections` 共 55 项中 54 项通过，唯一失败是既有 HNSW SQL mock 未适配导致的 RAG clarify 状态漂移。
- 仍需由 RAG 检索维护者在 `_explanation_standards` 的 identity fields 中加入 `employee_grade`，否则只有该字段时解释映射不会触发。

## 2026-10-18 最终验证补充

- 跨轮政策范围与 canonical 查询目标回归：17/17 通过。
- 完整 unittest：1071 项，90 skipped，1 failure、5 errors；剩余问题均为历史评测资产漂移（源文件哈希、旧 JSON schema、旧断言），未发现业务主链新增回归。
- `policy_admin.py search --question "我是院士，去北京出差，我的住宿标准是多少"` 返回 `status=ok`，来源闭包完整，`degraded=true`，`vector_error_code=embedding_request_failed_check_configuration`；这证明向量服务失败时关键词召回/RRF/Rerank 后续来源仍可用。
- `policy_admin.py check-config` 显示配置变量齐全，但 `database_verified=false`、`embedding_verified=false`；这是运行环境验证状态，不应被解释为 HNSW 创建失败。
- 浏览器隔离服务 8003 可以加载工作台并保留用户问题；页面返回模型连接失败，原因是执行环境无法连接外部模型服务，因此未把该次结果归因于代码或 RAG。

## 2026-10-18 当前用户复现：已完成北京后继续列举全部地区

### 运行时观察

- 8000 工作台当前会话真实显示：首轮“我是院士，我的住宿标准是多少”先要求目的地；补充“北京”后成功生成政策解释，引用北京部级 1100 元/人·天；随后发送“所有地区的都列举一下呢”返回“语义复核未完成”，并声明没有写入新事项槽位或执行后续查询。
- Langfuse 当前可见的同类失败 Trace（2026-10-18 17:57:07，输入“目的地北京”）只包含 `jev_decision → slot_extraction → slot_protocol → context_budget/chat_model → slot_review`，没有 `consult_policy`、`policy_rag`、`policy_retrieve` 或 `policy_rerank`。
- 因此本次兜底发生在槽位提案/语义复核阶段，不能归因于 HNSW、Embedding 或 Rerank。

### 当前假设

- 高置信：上一轮已经完成的 `policy_explain` 任务在下一轮被当作已完成事项处理；“所有地区”是对回答范围的显式扩大，但现有任务更新契约主要围绕事实补充和单一政策范围，未把“列举所有地区”建模为新的 `policy_query` 范围。
- 待验证：JEV 是否把该句路由为 `policy_explain`，以及 ExpenseExtraction 是否输出了空 patch、错误的 `policy_query` 或未生成任务。需抓取 20:24 Trace 的 raw slot output 和 review verdict。

## 2026-10-18 最新真实复现：追问“所有地区的都列举一下呢”

### 已确认事实（Langfuse Trace）

- Trace: `e274d3f82646c8bd40c12904a1ffad1e`
- 时间: `2026-10-18 20:24:05`（Asia/Shanghai）
- 节点链路到 `slot_review` 后结束，没有进入 `slot_commit`、`consult_policy`、`policy_rag`、`policy_retrieve` 或 `policy_rerank`。
- `slot_review` 原始输出为 `verdict: "revise"`。
- 审查原因：模型提案将已确认的 `destination=北京` 置为 `null`，但仍保持 `mode=personal_limit`。当前消息“所有地区的都列举一下呢”被判断为扩大政策查询范围，而不是撤销北京事实；直接清空目的地会丢失已确认上下文。

### 初步根因

现有政策查询契约只能表达单地区个人标准或通用标准，不能表达“保留当前事实，同时把回答范围扩展到全部地区/完整表格”。因此模型只能通过清空 `destination` 来尝试扩大范围，语义审查器正确拒绝了这个不一致提案。

### 修复约束

需要新增通用的范围表达（配置/契约字段），保留已有事实并显式表达全量列举；不得加入“院士”“北京”等具体案例特判。

## 2026-10-18 范围扩展修复实现

- 在 `PolicyQuery` 中新增配置化 `coverage`：`target`（默认，持久化时省略以兼容旧状态）和 `all_regions`。
- `EXPENSE_PROMPT` 与 `semantic_review` 明确：扩大政策回答范围要保留已确认目的地/身份事实；不得把范围扩大误写成目的地撤回。
- `PostgresPolicyStore` 新增全住宿附表投影：从已发布版本的 `policy_lodging_limits` 逐行生成完整表格和来源闭包，支持指定住宿档次及旺季列，不由模型拼接金额。
- `policy_rag` 将完整表格作为已验证结构化结果直接渲染，兼容旧的二参数 `search_for_facts` 存根。
- `render_policy` 增加完整住宿地区表展示和折叠原文依据。
- 回归测试：范围扩展保留目的地、结构化全地区表不调用回答模型、86 行附表投影均已覆盖。
- 定向测试 149 项通过；身份附表新增测试通过；compileall 与 git diff --check 通过。
## 2026-10-18 当前轮验证补充
- 复核 slots.py：PolicyQuery.coverage=all_regions 会在同类别/同模式且唯一的既有 policy_explain subject 上转换为 update_facts，保留 subject、目的地、身份和住宿档次。
- tests.test_slot_protocol 28/28 通过；compileall 通过。
- 运行时端口：8000 PID 52664、8001 PID 58528，均为 Python 服务；进程命令行查询受系统访问限制，无法仅凭 PID 证明加载了哪一版代码。
- 浏览器 8000 当前显示“登录已失效，请重新登录”，没有可用登录凭据；因此尚未宣称真实浏览器跨轮回放已完成。
- 已启动隔离服务 8003，HTTP 200 且加载当前工作区 Web 入口；8002 启动因已被占用而退出，未覆盖已有服务。
- 8003 浏览器页面等待有效账号登录；当前没有使用任何密码或绕过鉴权。

## 2026-10-18 8000 浏览器与 Langfuse 复核

- 8000 当前登录会话已可见，最近会话中“首轮住宿标准 → 北京 → 所有地区的住宿标准都列举一下”显示完整政策表；页面 DOM 统计 86 行，逐行有来源片段，`context_incomplete=false`。
- Langfuse 列表刷新后仍只显示到 21:10:40 的旧 Trace `2959a099958450d68b7065925e4f3cbc`，其输出仍是旧的“只列 6 个地区/未覆盖所有地区”逻辑；该旧 Trace 的树包含 `consult_policy`、`policy_rag`、两次 `policy_retrieve`、两次 `policy_rerank`、`policy_result`。
- 证据边界：不能把 8000 页面 21:53 的新成功会话冒称为已在 Langfuse 出现；当前需要新建一次测试会话并发送消息，确认新运行实例是否实际向 Langfuse 写入 Trace，或确认当前 8000 运行实例的 Langfuse 配置/版本不一致。

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `apply_patch` context mismatch while appending the Langfuse observation | 1 | Read the file tail and will append using the current tail context |

## 2026-10-18 8000 新会话实测

- 在 8000 新建对话并发送“我是院士，去上海出差，我的住宿标准是多少”，当前服务返回政策解答：院士→部级，上海 1100 元/人·天，并展示 2 个来源。
- 该请求期间页面先显示“正在整理政策依据”，随后正常完成；说明当前 8000 实例可执行新请求，不能只把成功结果归因于历史缓存。
- 刷新 Langfuse 后仍没有新 Trace。`.env` 中 `TRAVEL_LANGFUSE_ENABLED`、Langfuse 地址、公钥、私钥和哈希键均存在；DNS 可解析 `jp.cloud.langfuse.com`，但本机到其 TCP 443 连接失败。现有运行日志也记录过 OTLP exporter 的 DNS/连接失败和 `WinError 10013`。
- 结论：当前缺失 Trace 的直接原因是运行实例无法把观测批次发送到 Langfuse，证据不足以指向 RAG、Rerank 或槽位逻辑；Langfuse 页面看到的 21:10 Trace 是旧实例/旧结果，不能代表 8000 的 21:53 新会话。

## 2026-10-18 JEV multi-intent implementation

- Current Choice explicitly prioritizes check over explain; primary_route emits one task.
- Official Noul HTTP shape: type=noul, instructions, optional criteria.true/false;
  each response is {type: noul, noul: probability}. Near 0.5 needs fallback, not a guess.
- Retain detailed policy rules; rule amounts do not imply checking, hypothetical checks
  remain checks, and explaining the reasoning of a check is not an independent explanation.
- Graph assistant consumes current_task.question. Scope mixed-intent tasks explicitly
  so generic other does not repeat business actions. Keep full original text as data.
- Existing targets, controls, recovery, repeated same-intent goals and dependencies
  require richer routing than independent intent presence; preserve TurnRoute fallback.
- requirements.txt and existing Python 3.12 .venv are available; worktree is dirty.

- Implemented four independent Noul questions plus a narrow richer-routing guard, in one
  HTTP request. Query scope/transport/journey remain Choice questions.
- JEV_INTENT_THRESHOLD defaults to 0.8. Direct routes require confident positives AND
  negatives for every intent and no richer-routing requirement. No-match is not other.
- Mixed routes carry distinct scoped questions with the original user text preserved.
- Slot retries now release the JEV route after the first rejected proposal, allowing
  TurnRoute to repair omitted tasks or dependencies instead of repeating a fixed route.
- Initial 21 JEV adapter/extraction tests passed, including all intent combinations,
  malformed/uncertain answers, recovery fallback, attribution and semantic repair.

- Full scoped graph regression proves primary mode executes business + other, shadow mode
  ignores JEV hints/routes, and error/no-match decisions retain original routing.
- 247 targeted tests passed across JEV, configuration, routing/extraction, semantic review,
  graph dispatch, failed-turn recovery, lifecycle, context budgets and observability.
- compileall passed. git diff --check passed with Windows CRLF recognized. An earlier
  command forced core.autocrlf=false and produced CRLF-only noise; no line endings were changed.
- No live JEV or supplier inference was invoked. Synthetic answers verify protocol and
  execution behavior, not language-classification accuracy or actual token/cost savings.

## 2026-10-18 JEV other rubric refinement

- Reported example: flight query plus accommodation-policy explanation wrongly had
  has_other_request=0.91. Expected goals are plan_trip and policy_explain only.
- Current other definition lacks an explicit policy-explanation exclusion and all intent
  questions share one generic negative criterion. Strengthen semantic boundaries in
  the request, without filtering other whenever a business goal is also present.
- Official documentation index retrieved from https://docs.typesafe.ai/llms.txt;
  relevant pages are primitives/noul.md and primitives.md. Existing local requirements.txt
  and .venv are the test toolchain. No memory registry matches for JEV.
- Official primitives and Noul pages confirm question IDs are not sent to the model,
  sibling answers are independent, instructions should state the complete judgment,
  and state fields should be referenced explicitly. Source pages:
  https://docs.typesafe.ai/primitives.md and https://docs.typesafe.ai/primitives/noul.md.
- Live baseline with the reported sentence, empty business state and one-message
  conversation reproduced other=0.91 with jev-1.13.0. Route intents were
  plan_trip, policy_explain, other. This is fresh classification evidence, not a supplier call.
- Implemented explicit per-question goal and current-message references, independent-goal
  instructions, self-contained other exclusions and contrasted business/general examples.
- Live verification completed 2026-10-19 with jev-1.13.0: reported other probability
  fell from 0.91 to 0.05, while flight and policy-explanation probabilities were 0.98
  and 0.97. The reported example was classified correctly in two post-change requests.
- Ten live intent contrast cases passed, including single goals, business + weather,
  business + preference, check + explain, politeness and negated weather. The preference
  case retained both intents but requires_task_routing=0.21 correctly prevented direct
  routing under the unchanged threshold; this is classification validation, not proof
  of downstream execution. Results: .verification/jev-other-20261019/results.json.
- More explicit rubrics increased JEV input tokens for the reported sentence from
  2470 to 3617. Net application token/cost savings were not evaluated.

## 2026-10-19 JEV onboarding and evolution document

- User requests a shareable, detailed document teaching JEV usage and explaining
  integration history, the reasons for each change and how the changes work.
- Root plan resolver returned no selected task directory; legacy root plans apply.
- Existing docs use Chinese Markdown and README links. No CodeGraph index or
  repository AGENTS.md was found. Apply the user-supplied project instructions.
- Separate historical plan notes from current code and local live-evaluation results.
  Do not turn the 10-case success or 172-test regression into a general accuracy claim.
- Official API, primitives, Noul, Choice and models documentation refreshed on
  2026-10-19 through an approved public request after sandbox socket denial.
- Verified current request includes five Noul questions and three Choice questions.
  Every nonempty user-message prepare still attempts JEV; direct task construction
  checks empty business state, all confident intent decisions and a confident negative
  richer-routing judgment. Hint construction alone does not satisfy these gates.
- Current auxiliary Choice hints accept allowlisted values without a confidence gate;
  direct-route construction does not use these values to populate business slots.
- Current HTTP retries cover 429/502/503/504 and network failures, at most three
  attempts. Official docs also mention 529; the adapter presently falls back for it.
- Official model documentation distinguishes stable and preview aliases and notes
  English/CJK accuracy differences. Record the actual tested model jev-1.13.0 rather
  than treating an alias as a permanent model version. No pricing claim is needed.
- BAC is unavailable in the project venv. This is a scoped explanatory document,
  so no initialization or dependency installation is performed; existing plans record
  the documentation work and verification.
- Completed guide includes 18 numbered sections plus reading/evolution navigation,
  typed API examples, a self-contained HTTP example, project adapter usage, a Mermaid
  pipeline, per-change causes and behavior, 10-case evidence, cost model and limitations.
- Local validation checks JSON/Python syntax, referenced files/symbols, default config,
  saved probabilities and README discoverability. Initial document whitespace check
  reported two Markdown hard-break spaces; removed them for a clean check.

## 2026-10-20 意图评测上下文复核

- 生产 `business_travel/graph.py::prepare()` 会把当前会话中的所有用户轮次、当前槽位、任务快照、待处理任务和失败轮次传给 Jev；原评测脚本固定发送单条 `conversation` 且其余上下文全部为空。
- 因此“重新执行刚才失败的查询”“查看刚才保存的行程”“取消这次核验”等 Case 在原报告中并未模拟生产前置条件；这些结果不能直接归因于 Jev 语义能力。
- `evals/intent_accuracy.py` 现支持 Case 通过可选字段覆盖这些状态，缺省样本继续保持原来的单轮基线。
- `intent100-072`（“你刚才返回的政策结论是什么意思”）仍保留 `other` 标签，但加入 `label_review`，因为它也可能被业务方定义为政策解释；不能在未确认口径前拿它作为纯模型错误。
- 原报告 `\.verification/intent-accuracy-100.json` 的 86% 是修改前数据条件下的真实结果；上下文修正后必须重新运行并生成新文件，不能覆盖或改写旧报告。

- 第二阶段的状态边界已确认：`clarification.draft_slots` 是未提交事实草案，不能直接放入正式 `slots` 或 `tasks`；补齐后必须重新走 preflight 和 semantic review。
- JEV 追问判断只发送 `is_clarification_answer` 一个 Noul 问题，并只接收追问摘要；服务器根据 checkpoint 中的 subject/draft ID 生成唯一允许的 `update_facts` 路由。
- 快捷路径成功后把内部草案更新转换为 `new_task`，因此既复用了原任务协调和队列逻辑，又避免了未完成事项提前进入队列。
- 宽回归中发现的旧生命周期失败集中在历史夹具仍期待“缺字段先创建 needs_clarification 任务”；这与当前第一阶段契约冲突，属于测试迁移项，不应通过恢复旧行为解决。
- 字段缺失追问不应进入 `pending_turns`：后者只表示失败请求恢复上下文；否则追问草案完成后仍会阻止 JEV 对后续新话题生成首轮路由。

## 2026-10-19 第三阶段实施前确认

- `extract_slots()` 当前在 preflight 通过后调用一次 `review_payload()`；reviewer 返回 `revise` 会抛出 `semantic_review:*`，随后整轮重试，最终可能表现为 `slot_validation_failed`。
- 语义复核的提示已经声明“字段缺失由下游处理”，但协议只有 `issues`，没有区分必须阻断的安全/一致性问题和可记录的非阻断提醒；因此模型即使发现的是表达或完整性建议，也只能用 `revise` 表达。
- 第三阶段采用结构化 `blocking_issues` 与 `warnings`：`blocking_issues` 才能使 verdict 为 `revise`，`warnings` 可以随 `accept` 返回并进入观测；旧版仅返回 `issues` 的 `revise` 结果继续按阻断处理，保证兼容和 fail-closed。
