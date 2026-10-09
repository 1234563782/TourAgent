# 2026-10-08 项目总览

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
