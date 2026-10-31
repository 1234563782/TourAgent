# 修复发现

## 已确认事实

## 2026-10-30 Token 优化专项审查

### 调用链与基线

- 当前每轮理解链路通常包含 JEV 分类（`business_travel/graph.py:339`）、槽位协议调用（`slots.py:644`），以及语义审查调用（`slots.py:1031`）；JEV 不可用时仍会进入槽位模型。槽位失败/修复可按 `slot_max_attempts=3` 重试，规划选择默认最多 2 次，政策回答和审核默认最多各 2 次。
- `context_budget.py` 已实现按角色输入预算、历史分组、工具结果压缩和实际 usage 校准，这是现有 token 控制的基础。
- 默认上下文配置为 65536 窗口、8192 输出、4096 margin、supervisor 最近历史 12000、summary 2048、单工具结果 2048；这些是上限，不代表每次实际发送量。

### 高收益观察

1. JEV 的 payload 同时发送当前消息、全部 HumanMessage conversation、current_slots、task_context、pending_tasks、pending_turns；随后槽位调用又发送 conversation、任务快照、pending turns、JEV hint/route。相邻两次调用存在明显重复输入。可将 JEV 改为只看当前消息 + 受限摘要/哈希，并让槽位调用复用本轮确定的轻量 route，预计减少每轮输入 token，需先做意图准确率和追问回归。
2. 槽位协议固定提示词很长：`ROUTE_PROMPT` 约 7367 字符（约 1842 粗略 token），`FACT_PROMPT` 到 `TRIP_PROMPT` 约 3940 字符；Trip/Expense 又在 FACT_PROMPT 上叠加。每次重试都会重复发送。可把通用安全/证据规则压缩为短版，并把只适用于特定操作的规则按路由注入。
3. `semantic_review.REVIEW_PROMPT` 是独立的大段审查协议，且审查 payload 包含 task schema、resolved_tasks、proposal、proposed_slots 和上下文；这是一次额外模型调用。可先用确定性校验筛掉无变更、纯 other/天气/保存读结果等不需要事实审查的路径，或只发送 changed fields 和相关用户句子。
4. 政策 RAG 的一次回答可能包含 proposal、semantic review、失败反馈重试；`policy_rag_max_retrievals=2` 与 `policy_rag_max_answer_attempts=2` 组合会形成多次生成/审核。可按决策类型设置预算：已足够证据时一次生成 + 一次审核，只有 reject/retrieve 才进入第二次。
5. 监督器固定提示词约 3025 字符（约 757 粗略 token），且每轮还有 current task、subject references、task snapshot 三段 SystemMessage。可将不变规则改为短版，并把三段状态合并成一个紧凑 JSON。

### 中收益观察

- `context_budget.request_size` 用 UTF-8 JSON 字节估算而非 provider tokenizer，安全但可能保守；usage 已被记录，可按角色校准 margin，降低不必要的 blocked/过度压缩。
- `planner_candidate_mode=compact` 已存在；但选择阶段仍把每个候选的 timeline_check 和 recommendation_check 嵌入，且 `planner_max_attempts=2` 会把失败上下文追加后重发。可只在重试时发送失败原因和候选 ID 子集。
- `policy_context_limit=40`、`policy_context_chars=24000`、rerank candidates=40 主要影响检索/重排输入和后续政策 prompt；可以按 query 类型动态缩小，尤其是精确住宿表查询。
- 日期当前默认以 suffix 注入，避免 prefix 破坏缓存；可保持该设置并统一固定 prompt，减少前缀缓存失效。

### 不建议直接删减

- 不应删除 tool sequence 校验、版本/来源哈希、引用原文校验、历史数据不可信提示、语义审查中的否定/归属/单位规则。这些是正确性和安全边界，应该通过压缩表达或结构化字段传输节省 token。

### 安全附带发现

- `TourAgent/.env` 当前包含看起来像真实的 JEV 和 rerank API 凭据。未在本报告复述其值；应立即轮换并确认 `.env` 未被 Git 跟踪，避免把 token 优化工作掩盖成凭据泄露。

### 验证边界

- 本轮只读审查，没有调用真实 JEV/LLM，也没有修改业务代码；收益百分比是待基准测试的估计。

## 业务流程线索

## 问题与风险

## 待验证项

## 2026-10-16 本轮规则确认

- 用户已确认：大陆因公出差、未说明入住人数默认 1 人、未提及专项审批默认无专项审批、酒店搜索报价可作为推荐阶段的金额估计、按查询范围展示对应政策、身份/头衔应参与交通和住宿标准匹配。
- 已确认交通规则包中 `院士` 映射为交通 `一类`，见 `TourAgent/policies/hfut-2026/reviewed-rules.json`。
- 已确认住宿附表的实际列只有 `部级`、`司局级`、`其他人员`，且项目清单明确警告交通一二三类不能直接等同住宿档次，见 `TourAgent/policies/hfut-2026/manifest.json`。
- 当前唯一业务歧义：是否将“院士”映射为住宿附表的“部级”，同时保留交通字段为“一类”。不能直接把住宿标准命名为“一类”，因为底层住宿表没有该列。
- 用户已确认统一映射：一类→部级、二类→司局级、三类→其他人员。
- Jev 当前继续只做意图、范围、交通方式和行程方向分类；身份到政策级别由活动政策配置和确定性代码完成，避免增加 Jev token 及引入不可复现的政策判断。
- 已实现 `lodging_categories` 配置、身份/交通类别到住宿档次的推导，以及 PostgreSQL 规划阶段的标准注入。
- 已增加旧版已发布政策规则的兼容默认值，避免数据库中暂未重载新增字段时启动失败。
- 学生规划采用条件性默认：活动性质未知时推荐按三类/其他人员，但正式政策核验仍要求 `student_activity_eligible=True`。

## 2026-10-22 竞品基准初步发现

- 项目定位：高校/组织内部的智能差旅助手，当前核心是对话式行程规划、航班/火车/酒店只读搜索、天气、个人偏好/历史行程、政策 RAG 与确定性费用核验、任务追问/恢复/确认、组织与会话隔离。
- 明确边界：不做预订、支付、取消/退改、审批流、报销提交、保险理赔；供应商结果存在库存/价格不完整和报价口径不确定边界。
- Web/API 已覆盖登录、会话、流式聊天、运行状态/恢复、偏好读写；CLI 还有 `/history`、`/preferences`、`/resume` 等交互命令。
- 对比时应把“企业商旅平台的交易闭环能力”与“本项目有意不做的参考/合规助手边界”分开，避免把所有商业平台功能直接视为缺陷。

## 2026-10-22 竞品对照结论

对比口径：本项目是高校国内差旅的参考规划、政策解释和报销条件核对，明确不做下单、支付、审批办理、报销提交和保险理赔。不以企业商旅平台的交易闭环直接判为缺陷。

已对照公开资料：携程商旅高校方案与全流程/AI Agent 介绍、阿里商旅差标手册、合思申请审批说明、差旅壹号解决方案页、Navan 2026 产品更新，以及中国科大差旅平台操作指南。分贝通等自评评分不作为事实。

相对本校《差旅费管理办法》和同类助手，优先缺口：
1. 不能把已核验行程导出为出差审批单/报销单草稿，也没有预订交接。
2. 可执行规则未覆盖一类人员的年龄与国际行程满5小时例外、随行一人、双肩挑就高、夕发朝至软席、轮船。
3. 餐补只核单日一般标准，未按自然天数汇总，也未计算连续在途满12小时加发。
4. 报销材料清单、两个月时限、公务卡或转账结算要求没有产品化。
5. 登录身份不绑定政策待遇，用户可口头声明职级。
6. 政策发布仍是命令行，财务管理员不能自助更新财政部住宿标准。

有意不做且不应视为缺陷：预订支付、审批流执行、报销提交、保险理赔、企业用车下单、国际行程规划。政策正文适用范围是国内差旅。

不合理点：库存详情查询很深，但结果不能预订也不能交接，报价容易过期；精确住宿查询依赖模型抽出 kind=hotel，真实问答可能退回普通检索；学生未知活动时按三类出方案，正式核验仍可能不通过。

## 2026-10-22 JEV 意图识别只读诊断

边界：只读代码、离线测试和 2026-10-19 保存的 10 条真实分类结果。本轮没有调用 JEV，没有看运行中的服务，没有改业务代码。

已观察：
- 四类意图用独立 Noul，阈值默认 0.8；直接建任务要求四类都明确且复杂路由明确为否。
- 2026-10-19 空状态下 10/10 意图匹配，模型 jev-1.13.0。其中“查机票并保存偏好”的意图正确，但 requires_task_routing=0.21，直接路由被放弃。
- 已修复并复测的 other 误报只覆盖那一句，不代表当前 jev-latest 仍是 1.13.0。
- hint() 只输出超过阈值的正例。不确定时测试仍断言 jev_hint.intents 只有 plan_trip，policy_explain=0.5 被丢掉。
- 有待追问时 classify() 只问 is_clarification_answer，不再做四类意图判断。

推断：回退路由可能把不完整 hint 当成完整意图；追问轮如果误判为补充答案，会跳过新意图。这两点尚未用真实模型复现。

## 2026-10-22 四类意图是否合理

判断：plan_trip、policy_check、policy_explain 对应不同执行器，拆开合理。other 把天气、偏好、读旧结果、保存/取消/恢复塞进同一个对等意图，和操作类型混在一起，作为第四类业务意图不合理。这四类是独立存在判断，不是互斥单选。

## 2026-10-22 追问是否应作为意图

结论：不应把追问做成第五个业务意图。Rasa CALM 把 clarify 做成理解命令，把用户回答做成 set/correct slot；缺槽位时表单不重新分类意图。LangGraph 用 interrupt 暂停并在同一节点恢复。ISO 24617-2 把提问当作交际功能，和领域语义内容分开。本项目应保留待答关系判断，同时继续识别这句是否另有新业务目标。

## 2026-10-22 追问分流已实现

待追问时 JEV 同时判断是否回答待补字段，以及是否另有新目标。回答不是意图。四个意图都明确且不需要更细拆分时，回答部分走 update_facts，额外目标另建任务。未回答或意图不确定时回到 TurnRoute。离线测试覆盖纯回答、混合目标和不确定回退。未调用真实 JEV。

## 2026-10-22 other 收窄方案

不增加第五个意图。三个业务目标保留。other 只留天气、偏好和闲聊。保存、取消、恢复、重试、阅读旧结果、补充事实是操作，由现有 TurnRoute 绑定目标；JEV 不直接为这些操作建 other 任务。JEV 不能填写任务 ID，所以这些操作不能走直接建任务。

## 2026-10-22 两轴方案是否可落地

已观察：
- 四个意图对应不同执行器。`plan_trip`、`policy_check`、`policy_explain` 由业务节点执行；只有 `intent == other` 才会进入带 `save_trip`、`read_task_result`、天气和偏好工具的助手节点。见 `TourAgent/business_travel/graph.py` 的 dispatch。
- `TurnRoute` 只有 `new_task`、`update_facts`、`change_intent`、`task_controls`（resume/cancel）、`recovery`（retry/discard）。没有保存或阅读旧结果字段。
- `requires_task_routing` 的正例写了更新、改目标、取消、恢复、重试，没有写保存或阅读旧结果。

对照 Rasa CALM 官方文档后的结论：
- 业务目标和操作拆轴成立。命令可以同时包含填槽和开启新流程；取消、闲聊、知识回答是系统模式，不是业务意图。
- `force_slot_filling` 会吞掉新目标，不能用来处理追问。
- “保存和读旧结果不能再出现 other 任务”在当前图上不成立。只改 `other` 文案会让这两类话失去执行器。取消、恢复、重试已经有独立执行器，才可以不建 other。

## 2026-10-22 other 边界已改

- `other` 保留天气、偏好、闲聊、确认事实、保存当前出差、阅读已返回结果。
- 取消、恢复、重试、丢弃失败回合改为任务控制或失败恢复，并写入 `requires_task_routing` 正例。
- `ROUTE_PROMPT` 要求只控制时不另建 other，保存和读旧结果不能丢掉。
## 2026-10-22 JEV 只读复查

- 未调用真实 JEV，未改业务代码。
- 已观察：`hint()` 只保留不低于 0.8 的正例，不传 Choice `confidence`；`query_scope`、`transport`、`journey` 没有“未提及”选项。
- 官方文档：Choice 总会返回最高概率选项，低置信度不应直接执行；Noul 中间值应交给另一条路径；`jev-latest` 是会移动的别名。
## 2026-10-23 JEV 两点的建议改法

- 范围、交通、往返不加入“未提及”枚举。同一次请求另问三道“是否明确提到”的 Noul，只有明确为是且 Choice confidence 达标才写入 hint。
- `intents` 继续只放明确正例。另传 `undecided_intents`、`rejected_intents` 和 `requires_task_routing`。原路由不得把缺失当成否定。
- 尚未改代码。

## 2026-10-23 不确定意图的处理

原先不确定的是：Noul 落在 0.2 和 0.8 之间时，回退给原路由后如何避免把“没放进 intents”当成否定。

已打开的来源：
- TypeSafe Confidence：高置信度自动执行，中间值谨慎处理，低置信度不要行动，可澄清或交给另一套系统。阈值按风险另定，不照搬示例数字。
- TypeSafe Noul：没有单独 confidence，接近 0.5 就是没判清。官方多题示例用 0.8/0.2，中间值交给另一条路径，不直接当是或否。
- TypeSafe confidence-routing：答案决定做什么，置信度决定是否行动。人工队列是示例路径，不是本项目已有路径。
- Rasa CALM 对话理解：含义清楚就直接开始流程；多个流程同样可能或原话不精确时才 clarify。不把每次低置信度都做成确认问句。

决定：
- 直接建任务条件不变，0.8 不降低。
- hint 增加 undecided_intents、rejected_intents，以及 requires_task_routing 的 yes/no/undecided。
- 缺失或非法答案归入未决，不归入否定。
- 原路由保留正例，不新建明确否定的意图。未决项先读原话；仍分不清含义或目标才澄清，且不能同时提交任务。
- 不新增意图，不建人工队列，也不在中间概率时直接问用户。

故意没照搬：TypeSafe 的人工复核队列、按 0.6 立即行动、Rasa 两阶段“是不是这个意图”确认。

## 2026-10-23 中间区间不再单独重判

- 0.2 到 0.8 不是丢弃。Jev 不直接建任务，整轮交给 TRAVEL_LLM_MODEL 的 TurnRoute。
- 不另加一次只重判意图的 GPT/千问调用。语言模型用来读原话并决定新建、修改、控制或澄清。
- 依据：TypeSafe 中间值交给另一套系统；Rasa 高置信度分类器可跳过 LLM，低置信度由 LLM 产出命令，不重跑同一分类。
- 尚未改代码。若要加强，应在同一次 TurnRoute 里要求每个未决意图给出保留、不建或澄清。

## 2026-10-30 JEV 阈值提议审查

- 当前实现只有 `jev_intent_threshold`；`_decided` 使用 `p >= t` 或 `1-p >= t`，`_ternary` 同样由一个 t 同时派生 yes/no。提议指出的对称边界缺口真实存在。
- `primary_route` 只在无现有任务/槽位/澄清且四个意图均已判定、`requires_task_routing` 判定为 no 时创建任务；否则仍走 TurnRoute。降低阈值会改变 JEV 直达分支和传给 TurnRoute 的 hint，不是纯指标参数。
- 旧数据 `.verification/intent-accuracy-1130.json` 为 jev-1.13.0、98 个 ok 样本、392 个 Noul 观测。复算：accept/reject=(0.8,0.2) exact 79/98=0.8061、micro P=.9900、R=.8250；(0.6,0.15) exact 83/98=.8469、P=.9633、R=.8750，F1=.9170。该结果支持在离线集上做候选阈值实验，不支持直接发布。
- 低概率正例并非全是标签错：<=.30 的 9 个正例中至少 `intent100-047`、`intent100-093` 是 policy_explain，且 `intent100-065` 的 other 是否应保留要按当前业务边界重新标注；不能据此断言“0.15 以下全是真负例”。按意图统计，other 在 <=.20 有 7 个 gold 正例，policy_explain 有 1 个。
- 392 个观测混合四个不同 Noul 问题、每类只有 98 个样本，合并分箱不等于每类校准；当前证据没有 Brier/ECE、置信区间、独立校准集或新模型 `jev-latest` 结果。
- 方案中的代价数值 C_FN=5、C_FP=1 只是主观设定，没有从真实用户澄清时延、TurnRoute 成功率、错误任务代价或政策漏判风险测量。由此得到的 0.17 只能作为敏感性分析点，不能作为生产阈值证明。
- “reject 对离线意图指标零影响”只对不把 rejected 当作 actual 的静态分类指标成立；reject 会改变 undecided 数量、TurnRoute 负担、JEV 直达资格以及 hint 中的 rejected/undecided 语义，线上效果必须走 shadow/端到端评测。
- 采纳方向：拆分独立 `jev_accept_threshold`/`jev_reject_threshold`，保留旧变量兼容；先 shadow 记录双阈值决策，再在冻结模型和盲测集上比较 false accept/false reject/clarification/TurnRoute 调用率/延迟/token/任务正确率。
- 暂不采纳：直接把生产值改成 0.60/0.15、把 `reject<=0.15` 描述为真负例、把结果描述为“纯配置零代码变更”。
