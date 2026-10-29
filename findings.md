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
