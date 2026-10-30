# Redis 评估发现

- 项目为 Python/FastAPI/LangGraph 差旅助手，依赖 PostgreSQL checkpoint、业务 memory、会话、资源配额、认证与附件/发票持久化。
- `.env.example` 明确：长期偏好、历史行程、对话检查点统一使用 PostgreSQL；认证登录尝试限制也存 PostgreSQL；资源并发与每日配额依赖 PostgreSQL 事务和 advisory lock。
- `memory.py` 的 SQLite 仅是离线测试/迁移适配器；`memory_pg.py` 是正式业务记忆和 LangGraph checkpoint 后端。
- Redis 适合补充短 TTL、跨进程低延迟状态和队列/发布订阅；直接替换 PostgreSQL 会削弱审计、事务、恢复和长期数据可靠性。
- 编译检查：`business_travel`、`scripts`、`tests` 全部可编译。沙箱内 psycopg 受 `_ctypes` DLL 限制，切换到项目既有沙箱外验证后，迁移专项 7/7 通过。
- 运行时代码和对外文档不再要求 company_id；`single_school_migration.py`、`convert_school_bundle.py` 及迁移测试保留旧字段仅用于读取、转换和验证历史数据库。
- 正式 memory 只读预检仍为 `ready`，统计 148 个历史 thread 需要改写；未执行 `--apply`。正式 policy 预检明确返回 `policy_digest_mapping_required_convert_all_existing_bundles`，说明必须先为全部既有政策包生成摘要映射；同样未写入数据库。

## 2026-10-29 长期偏好提议核验

- tuniu.py 的酒店查询参数确实只有城市、入住/离店日期和入住人数；品牌、星级、早餐等只能在供应商返回后本地处理。
- planning.py 查询阶段会按 _candidate_sort_key 排序并截断到 settings.max_candidates，但之后还会构造 recommendation_checks，再由 _selection_candidates 按组件和合规状态选一个档位；提议把排序键排在合规档位之后描述得不完整，不能直接等同于最终选择顺序。
- candidate_input.py 的 compact 投影会移除酒店 brandName、hotelId 等字段；偏好匹配若发生在模型候选输入之后会失去品牌依据，必须在原始 Candidate 上完成并把安全的 preference_checks 单独投影。
- 当前正式身份与数据隔离以 user_id 为边界。memory_pg.py 的 trips 表主键是 (user_id,draft_id)，没有 company_id；提议的 company_id 主键会引入未定义的公司归属和授权模型。
- memory_pg.py 初始化明确删除已退休的 user_preferences 表；graph.py 系统提示明确长期偏好存储不支持，query_memory 只读保存的行程草稿。提议不是局部接入，必须同步修改身份、存储、图提示词和测试契约。
- trips.data 只在用户显式保存草稿后写入；从历史行程派生偏好必须先定义 selected 的稳定结构、样本去重和撤销/删除，否则阈值会把重复草稿或未确认候选当成行为信号。
- 提议的硬过滤保底“过滤为空就全量返回”与“硬性要求不满足就不能选”语义冲突；不能静默返回违反用户硬要求的候选。
- 180 天自动降级、expires_at 与 semantics 状态机尚未定义；数据库约束不足以保证 value schema、数组去重、星级/品牌规范化和来源证据完整性。

## 2026-10-29 身份映射与 RAG Prompt 冲突只读诊断

### 已观察事实

- 当前 `policies/hfut-2026/reviewed-rules.json` 只定义交通映射：`title_categories.院士=一类`、正高级=二类；住宿表头在 `manifest.json` 中是 `部级`、`司局级`、`其他人员`。规则文件没有当前确认的 `一类→部级`、`二类→司局级`、`三类→其他人员` 可执行映射；加载后 `UniversityRules.lodging_categories` 为默认空字典。
- `business_travel/university_policy.py:20-22` 将 `lodging_categories` 标为兼容字段并注明“not applied”；`lodging_standard_for()` 只接受 facts 中单独给出的住宿身份，明确拒绝从交通类别、职称或岗位等级选择住宿列。`planning_standards_for()` 对“院士”实测返回 `('一类', None)`。
- `business_travel/policy_pg.py:499-531` 的身份证据生成同样调用 `transport_category()` 与 `lodging_standard_for()`，不会生成住宿档次。
- `business_travel/policy_rag.py:143-147` 的 `ANSWER_PROMPT` 明确要求交通/住宿独立、院士只能确定交通类别、不得把一类或院士说成部级/司局级/其他人员；`REVIEW_PROMPT` 及其共享 `REVIEW_CONTEXT` 在 199、214、217 行重复相同限制。
- Reviewer 的 `unsupported_claim` 并非只在 Prompt 中出现：`policy_rag.py:365-386` 将任何 claim 检查失败映射为 `unsupported_claim`，`policy_rag.py:466-480` 在拒绝后重试，耗尽次数后进入终止/partial 或 insufficient 路径。
- 现有回归测试把“交通身份不选择住宿档次”作为预期：`tests/test_university_policy.py:181-193`、`tests/test_rag_corrections.py:105-117`；因此当前测试会保护旧规则，而不是 10 月 16 日描述的新映射。
- 运行验证：`tests.test_university_policy` 39 项通过；`tests.test_rag_corrections` 无法导入，项目虚拟环境加载 `_ssl` DLL 失败（环境错误，未执行到测试断言）。独立 Python 探针确认 `rules.lodging_categories={}`、院士 transport=`一类`、lodging=`None`。

### 结论与置信度

- **Prompt 硬编码与目标映射冲突：真实存在，高置信度。** Prompt 文字与用户所述“一类→部级”等规则方向直接相反。
- **“底层规则已经能够确定标准”：当前 checkout 未观察到，结论为不成立（高置信度）。** 当前底层实际仍按独立身份处理，并且已有测试明确断言不得推导；可能存在未同步的外部/历史确认，但本地代码、配置和提交历史没有证据。
- **“必然已把回答降级为 partial”：部分成立，中等置信度。** 宾语生成的 `partial` 可能被 Prompt/reviewer 拒绝并触发重试/终止，但当前代码还有前置 `lodging_lookup` 的 `clarify`/`insufficient` 分支；仅凭静态代码不能把所有实际会话都归因于 Prompt。需用同一服务版本、facts（院士+目的地、无住宿档次）和模型调用 trace 复现。

### 最小验证实验

1. 在当前发布包中直接调用 `prepare_package()`、`UniversityRules.model_validate()` 和 `planning_standards_for()`，断言院士返回住宿档次部级；当前实验已反证为 `None`。
2. 若先把规则映射接入，再用固定 FakeModel 让 writer 输出“院士对应部级”并让 reviewer 读取当前 Prompt，观察 reviewer 是否返回 `reject/unsupported_claim`；这需要先获得实现授权，当前只读任务未修改代码。
3. 在真实部署中固定 commit、policy version、facts 和 query，记录 `policy_rag` 的 proposal、review verdict、issues、最终 status，以区分 `clarify`、`partial`、`insufficient` 与 Prompt 拒绝。

## 2026-10-29 遗留住宿映射清理

- 用户确认不采用旧的交通类别到住宿档次映射。
- 已删除 `UniversityRules.lodging_categories` 兼容字段；正式的账号 `lodging_standard` 字段仍保留。
- 已删除 `tests/test_plan_policy_citations.py` 中 `legacy_rules` 参数及两处调用，删除 `tests/test_rag_corrections.py` 对该字段存在性的测试。
- 代码和配置目录中不再引用 `lodging_categories` 或 `legacy_rules`；Prompt、manifest 和身份字段仍保持交通/住宿分离语义。
- 验证：`compileall -q business_travel tests` 通过；`tests.test_university_policy` 39 项通过。`tests.test_plan_policy_citations` 因当前运行时 `_ctypes` DLL 导入错误无法执行；此前也遇到缺少测试目录导入路径的问题，已用 `PYTHONPATH` 修正后仍被 `_ctypes` 阻断。

## 2026-10-29 方案 A-D 只读评审

### A：PolicyAnswer 前置清洗

- 当前 `PolicyAnswer` 的字段都有默认空值：claims/limitations/requested_fields 默认空列表，question/search_query 默认空字符串；`consistent_action` 主要拒绝非空的跨动作字段，以及 answer/partial/clarify/retrieve 的真正语义不一致。模型补齐 `question=""`、`search_query=""`、`claims=[]` 等默认空值本身不会触发对应的冲突校验。
- 当前示例中对 answer 删除空 `limitations`/`requested_fields` 没有实际收益，因为默认值已经是空列表；但对 `partial` 删除 limitations 会把本来需要拒绝的非法 partial 伪装成 answer-like payload，随后仍会因 `partial_requires_limitations_only` 失败。
- before validator 若原地修改 data 会污染调用方对象；应复制 dict。不能把非空跨动作值清空，也不应把非法类型通过 `str()` 转成合法值。建议仅丢弃空白字符串和空列表，然后继续让 after validator 拒绝真正混合动作；同时为默认空字段补充回归测试和工具 schema 说明。

### B：Reviewer Claim-level Pruning

- 当前只有终止阶段 `PolicyEvidenceReview` 返回逐条 `claim_checks`；普通 `PolicyAnswerReview` 只有 verdict/issues，没有 claim 索引。因此示例不能直接用于普通审核拒绝。
- `_review_evidence()` 会把 unsupported 索引放在 `review_feedback["unsupported_claim_indices"]`，但这只发生在 finalization 路径；普通审核拒绝的 `review_feedback` 为空。
- 直接将未支持 claim 改成 `missing_evidence` limitation 可能违反 limitation 规则：detail/next_step 不能夹带未经引用的政策结论；“部分限制条款”也可能不是用户实际缺口。还需重新做 citations/limitations 校验并保证 partial 的 reviewer 重新审核。
- 若只有 unsupported claim 且存在至少一个有证据 claim，安全实现应是终止阶段专用、保留 supported claim、生成基于实际 claim 的结构化 limitation，并再次跑 `_review_evidence()`；不能在普通拒绝或任意 `unsupported_claim` 上直接发布。

### C：超限平滑截断

- 当前 `policy_pg.py` 已在数据库检索阶段按 `policy_context_limit` 截断，并通过 `context_incomplete` 标记缺少链接闭包；`policy_rag.py` 发现 `context_incomplete` 或字符超限后直接返回 `incomplete`，现有测试明确锁定该安全行为。
- 直接 `evidence[:settings.policy_top_k]` 有三个问题：`policy_top_k` 是检索候选规模，不是已验证证据排序；可能丢掉住宿表行、scope 条款或 related_ids 闭包；按字符截断还可能破坏同版本/同文档来源完整性。
- 如要改善用户体验，应在 retriever 侧做“有界、可解释的证据选择”：保留 exact lodging rows、reviewed identity/config source、scope clauses 和相关闭包，再按检索排序填充；若仍不完整，继续返回 partial/incomplete 并明确披露截断原因。不能把不完整上下文直接交给模型当作完整 answer。

### D：policy_query 持久化/继承

- `PolicyQuery.coverage` 已支持 `target/all_regions`，`policy_query_dump()` 会保留 all_regions。
- `slots.py:797-802` 在 policy_explain 的 fact-only update 且未提交新 policy_query 时，已经从旧 subject 复制 policy_query；`slots.py:809` 也把显式的新 query 写回任务。
- `_scope_expansion_target()` 和 `_coalesce_scope_expansion()` 已将 all_regions 追问识别为同一政策事项的 update，并保留原 subject 的 destination/identity facts；对应测试覆盖 `test_policy_scope_expansion_preserves_confirmed_destination`。
- 因此在 `graph.py` 再按 `{**prev_query, **curr_query}` 合并存在风险：`current_task` 不是稳定的 subject policy_query 来源；空/default coverage 可能覆盖已有 all_regions；并且会绕过 slots.py 对任务 revision、scope change 和目标唯一性的校验。若仍有真实丢失案例，应先查 slot extraction 的输入/提交结果和 trace，再在 slots 层补回归测试。

### 综合建议

- 优先做 A 的最小安全修复和测试。
- B 只在 final evidence review 中实现受限版本，并要求重新审核；暂不对普通 reviewer reject 自动发布。
- C 不按简单切片实现，先建立证据优先级/闭包选择器。
- D 先用现有 slots.py 测试与真实 trace 复现；当前代码已有继承机制，不建议在 graph.py 重复合并。

## 2026-10-29 A/B 纠偏方案复核

- 用户确认 A 只处理 Tool Call 的 `None`/空白占位，保留动作互斥；评审结论正确。实现应使用 `deepcopy`，只处理已知字符串字段 `question/search_query` 和列表字段 `claims/limitations/requested_fields`，不把任意类型通过 `str()` 强转。
- 用户确认 B 只在终止 `PolicyEvidenceReview` 有逐条索引、issues 唯一为 `unsupported_claim`、至少保留一条 supported claim 时执行；修剪后必须重新校验引用并重新走证据审核。评审结论正确。建议在 generate 的 final 分支调用独立 helper，不在普通 `PolicyAnswerReview` 拒绝路径处理。
- C 的证据分层与 D 的 slots 继承判断维持此前结论：不做简单切片，不在 graph 重复合并。
- 本轮未修改业务代码。

## 2026-10-29 三项方案复核补充

### `.gitignore` 临时目录

- 当前现象属实：`git status` 会因 `.tmp-test/`、`pytest-cache-files-*` 和权限受限目录产生大量 `Permission denied`；但用户级 Git exclude 的权限警告是独立问题，项目 `.gitignore` 不能消除。
- `git ls-files` 显示 `.tmp-test/e.txt`、`.tmp-test/jieba.cache`、`.tmp-test/o.txt` 已被跟踪，因此新增 ignore 规则不会改变这些文件的跟踪状态；`tmpk5l91ry/memory.sqlite` 也处于已有删除改动，不能把它当作未跟踪垃圾擅自清理。
- `.tmp-test/` 和 `pytest-cache-files-*/` 方向合理；`tmp*/`、`*.sqlite` 过宽，可能隐藏合法临时命名目录、数据库 fixture 或本地数据，实施前应检查命中清单并优先使用项目根目录限定模式。

### 酒店品牌与 compact

- `tuniu.search_arguments()` 的酒店查询只发送城市、入住/退房日期和入住人数，未发送品牌、星级、早餐过滤条件；供应商返回归一化会保留 `hotelId`、`starName`、`brandName`、`meal`。
- `candidate_input.py` 的 compact 明确删除 `hotelId`；非空 `brandName` 会保留，品牌为空时才由通用空值压缩删除。当前没有 QueryFields/QueryRequest 的品牌、星级、早餐偏好字段，也没有名为 `preference_checks` 的确定性硬过滤链路。
- 因此“模型完全看不到品牌”不完全属实；“用户品牌硬约束当前不会被可靠执行”属实。若要实现硬约束，应在 provider 结果进入 `max_candidates` 截断前，依据结构化请求字段完成规范化匹配和过滤，再排序；模型投影可保留非空品牌/星级/餐食作为解释事实，`hotelId` 可继续仅留在内部原始 Candidate 供详情查询。

### 审批/报销导出

- 当前已有结构化 `draft`、`save_trip`（需确认后保存）和 `plan_checks.render_draft()` Markdown 文本渲染；因此“结果只留在聊天气泡和数据库草稿”不应表述为“没有结构化结果”。
- `web.py` 现有路由覆盖聊天、会话、运行状态、发票预审和附件，没有按用户授权导出行程/审批/报销草稿的 JSON、Markdown 或 PDF 下载接口；用户确实缺少一键交接入口。
- 行程 draft 可支持第一阶段的行程单 JSON/Markdown 导出，但直接称为“报销单”还缺少学校模板字段、费用归集/项目号、审批状态、发票关联等契约；导出必须标注草稿、待核验、未预订/未审批，不能把政策检查或供应商起价变成已批准金额。

### 结论

- 方案 1：问题真实；规则方向可采纳，但不能原样加入四条宽泛规则，也不能未经授权删除已跟踪文件。
- 方案 2：问题部分真实、描述过度；应先补齐用户偏好契约和 provider 前的确定性过滤，不能只依赖 compact 保留字段或模型判断。
- 方案 3：产品缺口真实；建议先定义按 user/task/revision 授权的 JSON/Markdown 导出契约，再评估 PDF 和学校模板适配。

## 2026-10-29 用户出行偏好画像方案复核

- 身份与偏好分离、偏好只在同一合规层内做软排序的原则正确；但当前项目已退休长期偏好能力，memory_pg.py 不创建替代偏好表，graph.py 明确不支持长期偏好，tests/test_preference_autosave.py 将其作为回归契约。新增 user_travel_profiles、偏好 API 和画像读取会直接冲突。
- save_trip 是保存当前草稿的用户确认，不等于确认酒店品牌、座位或早餐为长期偏好；selected 可能含模型补选、fallback、未知库存和未核验报价，不能直接统计成习惯。
- 方案缺少版本、来源、置信度、撤销/清除、过期、审计和并发更新条件；Redis 降级还需要缓存键、TTL、失效和 PG 不可用语义。
- 当前 QueryRequest 没有长期偏好字段；应使用独立只读 preference snapshot，避免混淆本次硬约束与长期软偏好。tuniu 的 brandName/starName/meal 不能分别直接当作品牌、星级和早餐布尔值；现有 Candidate 也没有靠窗/过道座位信息。
- normalize_tuniu 会先按 provider limit 截断，planner 再截断；仅在 planner 排序无法保证偏好酒店进入候选池。偏好命中应在 provider 结果截断前扩大召回或做明确保底扫描。
- custom_notes 不应直接进入 Prompt；接口必须绑定已认证 user_id，禁止客户端指定目标用户，并提供白名单、限长、审计和清除操作。
- 建议先做显式偏好最小闭环，暂缓历史自动画像；在当前项目边界下，本方案判定为方向可取、直接改造不可接受。

## 2026-10-29 纯 PostgreSQL 用户偏好画像方案复核

- 去掉 Redis、使用现有 memory_pg.py 的同一 PostgreSQL schema、把偏好限制为合规层内软排序，这些方向比前版合理；但新增表仍会重新启用当前已退休的长期偏好能力，必须先改变产品边界、测试契约和迁移策略。
- setup_memory() 当前会显式删除 user_preferences 且不创建偏好表；若改为 user_travel_profiles，不能只追加 CREATE TABLE，还要更新单学校迁移的表清单、初始化测试、回滚/清除语义和现有无偏好 API回归测试。
- UserPreferenceProfile 不应让客户端提供 user_id；服务层应使用已认证 MemoryIdentity 绑定用户。JSONB 需要 schema_version、字段白名单、版本/并发更新条件、来源、置信度和清除操作，不能只用 user_id/preferences/updated_at。
- _preference_match_score(item, pref) 的 profile 参数与现有 planning.py 的 policy recommendation_profile 概念不同；应使用独立 preference snapshot，不能把长期偏好塞进 QueryRequest 或政策 profile。
- 方案的排序代码只查 item.label 和不存在的 has_breakfast，未使用 star_min；当前 Tuniu 字段是 brandName、starName、meal，必须先做结构化字段读取、品牌别名规范化、星级解析和早餐的含早/不含早/未知状态。当前 Candidate 没有靠窗/过道座位信息，train_seat_position 不能宣称已实现。
- normalize_tuniu() 已按 provider limit 截断，planning.py 再按 max_candidates 截断；只在 planner 的 _select_top_hotel_candidates 内加偏好分，可能在偏好排序前丢掉目标品牌。需要扩大 provider 召回或在 provider 截断前做偏好保底扫描。
- 同一合规桶内按偏好、价格、房态排序与现有 _candidate_sort_key 的实际 tuple 不一致；当前 key 仍会把 availability 放在价格前，应明确 tie-break 目标并固定测试。
- avoid_red_eye 默认 True 会在用户没有设置时产生隐含偏好；长期画像应使用显式/未知状态，不能用默认值替用户做偏好选择。time_window、航班/列车时段、跨午夜和时区也需定义。
- save_trip 后异步统计没有现成可靠作业契约。若保留历史沉淀，需要幂等事件、失败重试、版本更新、显式偏好优先级、样本去重，并只统计用户确认保存且最终采用的候选，排除 fallback、alternatives、sold_out、unknown inventory 和证据缺失记录。频次 >=3/5 还需要置信度与撤销规则。
- 建议分阶段：先实现显式偏好 PG CRUD 和 preference snapshot 注入；再在已截断前解决偏好候选召回；最后才做历史统计。custom_notes 首版应删除或改为白名单结构化标签，不要直接进 Prompt。
