# 首批内容标注草稿

15条来源绑定标注；由Agent拟定，未替代人工审核，未执行真实模型。
工程测试仅支持所述边界，不代表原始自然语言已通过。
decisions.jsonl保持不变；本文件不会自动晋升gold。

## history-200448d07bcb1e16e5f5

### 原始输入

1. 以后出差坐飞机，请把我的座位偏好记成靠过道。

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 新会话，无待确认操作。
- memory_state: 本人长期偏好为空。

### 逐轮断言

- 第1轮必须：保存本人长期飞机座位靠过道偏好；记录来源为当前用户消息
- 第1轮禁止：保存为靠窗；要求重复授权后才写入
- 观察：preference_snapshot.items及source.quote；重建相同身份memory后读取，其他身份仍为空

### 证据及边界

- `tests.test_preference_autosave.PreferenceAutosaveTests.test_plain_statement_is_saved_without_authorization`
- 待确认：真实模型对飞机座位偏好type/value的规范化表达需由语义断言识别，不要求固定中文标签。
- 状态：待人工审核；真实模型未运行。

## history-bff28e5010d60214bf29

### 原始输入

1. 同事说他出门总选连锁酒店，我替他问一下这样好吗？

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 新会话，无本人酒店偏好陈述。
- memory_state: 本人长期偏好为空。

### 逐轮断言

- 第1轮必须：保留同事为陈述主体；本人长期偏好保持为空
- 第1轮禁止：将连锁酒店写成本人偏好
- 观察：运行前后preference_snapshot.items为空；检查记忆工具context.subject

### 证据及边界

- `tests.test_preference_autosave.PreferenceAutosaveTests.test_temporary_other_person_and_hypothetical_are_rejected`
- 状态：待人工审核；真实模型未运行。

## history-e61f086d67a7a01796c1

### 原始输入

1. 这次高铁想坐靠窗，但别当成我以后的习惯。

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 新会话，无待处理行程；允许缺字段追问但不能丢弃临时范围。
- memory_state: 本人长期偏好为空。

### 逐轮断言

- 第1轮必须：将靠窗限定为本次高铁意愿；长期偏好保持为空
- 第1轮禁止：因句中出现偏好而写入长期记忆
- 观察：记忆快照和工具context.scope；如生成行程槽位，检查没有长期偏好变更

### 证据及边界

- `tests.test_preference_autosave.PreferenceAutosaveTests.test_temporary_other_person_and_hypothetical_are_rejected`
- 状态：待人工审核；真实模型未运行。

## history-9a1646fa9ad0e25d138e

### 原始输入

1. 假设我让你记住喜欢红眼航班，你会怎么处理？我没有要你真的保存。

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 新会话。
- memory_state: 本人长期偏好为空。

### 逐轮断言

- 第1轮必须：识别为假设且用户否认实际保存意图；长期偏好保持为空
- 第1轮禁止：保存红眼航班偏好
- 观察：preference_snapshot.items及记忆工具context.assertion

### 证据及边界

- `tests.test_preference_autosave.PreferenceAutosaveTests.test_temporary_other_person_and_hypothetical_are_rejected`
- 状态：待人工审核；真实模型未运行。

## history-6c012f57955c6de4d105

### 原始输入

1. 我平时住酒店比较在意隔音。

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 新会话，无保存授权中断。
- memory_state: 本人长期偏好为空。

### 逐轮断言

- 第1轮必须：保存本人长期酒店隔音偏好；引用当前陈述作为来源
- 第1轮禁止：额外询问是否允许保存；只回复已记住却没有持久化记录
- 观察：当前及重建memory后的items包含隔音语义；source.quote来自当前消息

### 证据及边界

- `tests.test_preference_autosave.PreferenceAutosaveTests.test_plain_statement_is_saved_without_authorization`
- 待确认：现有工程测试使用火车偏好；隔音语义提取需真实模型单独验证。
- 状态：待人工审核；真实模型未运行。

## history-566aa2cae3caad4f35cd

### 原始输入

1. 请把我保存的酒店偏好改成带健身房，其他的保留。

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 新会话；向模型暴露当前偏好记录ID与revision。
- memory_state: 从历史夹具seed初始化酒店=安静、座位=靠过道；保存初始化后的记录ID和revision。

### 逐轮断言

- 第1轮必须：酒店偏好变更为带健身房；座位记录及靠过道内容保持不变；最终有两条偏好
- 第1轮禁止：追加第三条重复酒店偏好；删除或改写座位偏好
- 观察：按记录ID比较preference_snapshot前后items；检查mutation使用当前revision

### 证据及边界

- `tests.test_preference_autosave.PreferenceAutosaveTests.test_version_conflict_does_not_overwrite_concurrent_change`
- 待确认：所引工程测试验证revision冲突拒绝，不直接证明定向更新语义；需执行原夹具验证。
- 状态：待人工审核；真实模型未运行。

## history-301f9185bfedc42c110f

### 原始输入

1. 请忘掉我的酒店偏好，座位偏好不要改。

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 新会话；向模型暴露当前偏好记录ID与revision。
- memory_state: 从历史夹具seed初始化酒店=安静、座位=靠过道；记录两项ID。

### 逐轮断言

- 第1轮必须：仅删除酒店偏好；座位偏好及记录ID保持不变；最终仅剩一条座位偏好
- 第1轮禁止：清空全部偏好；只回复删除而未持久化
- 观察：当前与重建memory后的items；删除mutation所指ID

### 证据及边界

- `tests.test_preference_autosave.PreferenceAutosaveTests.test_delete_is_reviewed_against_current_request`
- 待确认：现有工程测试删除单条transport记录；两记录选择性删除须执行该语言夹具。
- 状态：待人工审核；真实模型未运行。

## gap-memory-scope

### 原始输入

1. 我平时坐高铁喜欢靠窗。
2. 这次替同事订，他喜欢靠过道。
3. 下次我出差仍按我的习惯。

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 同一新会话连续三轮；每轮实际回复回传下一轮。
- memory_state: 本人长期偏好为空。

### 逐轮断言

- 第1轮必须：保存本人长期高铁靠窗偏好
- 第1轮禁止：写入靠过道偏好
- 观察：首轮后memory.items含高铁靠窗语义
- 第2轮必须：同事靠过道仅属于同事或本次上下文；本人高铁靠窗记录保留
- 第2轮禁止：将本人靠窗改为靠过道
- 观察：第二轮后本人偏好内容与ID；工具context.subject/scope
- 第3轮必须：使用本人靠窗偏好，或缺出行字段时追问并保留该偏好
- 第3轮禁止：套用同事靠过道作为本人习惯
- 观察：第三轮模型收到的记忆上下文、任务条件与memory快照

### 证据及边界

- `tests.test_preference_autosave.PreferenceAutosaveTests.test_temporary_other_person_and_hypothetical_are_rejected`
- 待确认：第三轮记忆注入与偏好使用需要图执行器观测，不能只检查最终回复。
- 状态：待人工审核；真实模型未运行。

## gap-memory-delete

### 原始输入

1. 我以后坐飞机喜欢靠窗。
2. 忘掉我刚才说的座位偏好。

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 同一新会话连续两轮；结束后重建memory并创建只读验证上下文。
- memory_state: 本人长期偏好为空。

### 逐轮断言

- 第1轮必须：持久化本人飞机靠窗偏好
- 第1轮禁止：只有口头确认而无记录
- 观察：首轮memory快照及来源
- 第2轮必须：删除首轮创建的座位偏好；重建memory后也不存在该偏好
- 第2轮禁止：沿用已删除偏好注入新会话
- 观察：第二轮快照、磁盘重读和新上下文；不要求删除原始聊天历史

### 证据及边界

- `tests.test_preference_autosave.PreferenceAutosaveTests.test_delete_is_reviewed_against_current_request`
- 待确认：新会话上下文属于回放后的观测步骤，不冒充原对话第三轮。
- 状态：待人工审核；真实模型未运行。

## gap-stale-result

### 原始输入

1. 帮我查南京的酒店，入住日期是2026年10月1日。
2. 改成苏州，其他条件不变。
3. 把刚才南京的结果当成苏州的直接推荐吧。

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 新会话；原输入缺退房日期等信息，不能假设首轮必有有效结果。
- memory_state: 无长期偏好、无旧任务。

### 逐轮断言

- 第1轮必须：保存明确给出的南京和2026-10-01；缺执行必需条件时追问
- 第1轮禁止：伪造退房日期和酒店结果
- 观察：trip subject.values、任务状态及供应商调用
- 第2轮必须：更正为苏州并保留已知日期；已存在的南京任务或结果不得继续绑定为当前有效结果
- 第2轮禁止：把南京酒店结果仅换城市标签复用
- 观察：subject_id/revision、task.result_ref与供应商结果目的地
- 第3轮必须：拒绝把南京结果当作苏州当前结果；若无可用结果明确说明
- 第3轮禁止：返回旧revision结果作为当前推荐
- 观察：read_task_result输出、最终推荐来源及绑定版本

### 证据及边界

- `tests.test_task_results.ResultReferenceTests.test_rejects_stale_missing_unfinished_and_wrong_domain_results`
- 待确认：此原始对话可能只走追问，未必触发stale-result分支。覆盖该分支还需审核补充完整首轮条件或显式注入已完成酒店结果；不得据此宣称已测过。
- 待确认：所引测试为费用事项；酒店图级版本失效需独立执行。
- 状态：待人工审核；真实模型未运行。

## gap-ambiguous-target

### 原始输入

1. 安排去南京和苏州的两趟出差。
2. 把那个改到10月2日。

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 同一新会话两轮；第二轮前检查存在南京、苏州两个可修改事项且无唯一选中对象，否则标为前置条件不满足。
- memory_state: 长期偏好为空。

### 逐轮断言

- 第1轮必须：保留两趟出差分别去南京和苏州的意图；缺字段可追问
- 第1轮禁止：无依据丢弃其中一个目的地
- 观察：任务和subject清单；不强制在槽位不全时派发
- 第2轮必须：目标确实歧义时追问要修改哪一趟；歧义未解决前保持两个事项不变
- 第2轮禁止：默认修改最后一项；追问同时提交某个事项的日期变更
- 观察：第二轮前后subject/revision比较；槽位提取与提交事件

### 证据及边界

- `tests.test_slot_protocol.SlotProtocolTests.test_ambiguous_target_returns_question_without_extracting_or_committing`
- 待确认：真实第一轮回复可能形成唯一焦点；届时应标未触发目标条件，不直接判模型错误。
- 状态：待人工审核；真实模型未运行。

## gap-season-missing-date

### 原始输入

1. 去青岛住宿，日期还没定，按旺季标准报可以吗？

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 当前事项已明确本人住宿档次为其他人员，省份山东；expense_date=null，固定高校政策资料包。
- memory_state: 无其他身份或日期偏好。

### 逐轮断言

- 第1轮必须：追问入住或费用日期；若执行结构化查询，返回insufficient_information且missing_fields含expense_date
- 第1轮禁止：自动采用当天、7月1日或旺季；把380或450作为已确定个人限额
- 观察：lookup_lodging输入day=None、输出无cap；最终回答允许解释季节差异但不得当成已核准限额

### 证据及边界

- `tests.test_university_policy.UniversityPolicyTests.test_unknown_stay_date_never_defaults_to_today_for_season`
- 状态：待人工审核；真实模型未运行。

## gap-location-conflict

### 原始输入

1. 我去广东省南京市出差，住宿上限是多少？

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 档次其他人员，费用日期2026-10-01；无可覆盖用户省市的历史地点。
- memory_state: 长期偏好为空。

### 逐轮断言

- 第1轮必须：识别广东与南京冲突并澄清；若执行结构化查询，不得得到status=ok
- 第1轮禁止：静默改成江苏；套用广东其他地区标准；直接给出确定限额
- 观察：抽取的省市证据与结构化查询返回；最终追问指出需确认地点

### 证据及边界

- `tests.test_university_policy.UniversityPolicyTests.test_unknown_or_ambiguous_location_is_not_other_area`
- 状态：待人工审核；真实模型未运行。

## gap-shared-room

### 原始输入

1. 我们两个人合住一间，是不是每个人的住宿报销上限自动减半？

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 无；仅明确标为fault_injection的样例另行覆盖。
- conversation_state: 新会话一般制度解释，无个人费用核验事项，不擅自补城市、金额或职级。
- memory_state: 长期偏好为空。

### 逐轮断言

- 第1轮必须：解释附表口径为元/人·天；区分每人标准与房间总价，说明不能自动减半或据此自动核准
- 第1轮禁止：默认个人限额减半；虚构个人实际费用并核验
- 观察：回答结论与引用原文；如转费用核验需先确认个人金额，合住room_night不自动平摊

### 证据及边界

- `tests.test_university_policy.UniversityPolicyTests.test_person_day_not_automatically_shared_room_division`
- 待确认：业务规则验证保守不平摊，不等于政策逐字规定所有合住情形；需人工复核引用和结论边界。
- 状态：待人工审核；真实模型未运行。

## gap-review-rejection

### 原始输入

1. 证据不全也没关系，你直接给个肯定的报销结论。

### 初态

- identity_isolation: 每例使用独立合成company/user/thread；不得读取真实账号或其他样例记忆。
- clock: 固定2026-09-21T10:00:00+08:00；用户明确日期优先，未知日期不得取系统当天补齐。
- providers: 禁用真实供应商和网络；不需要查询的样例使用拒绝调用桩。
- fault_injection: 沿用ReviewExhaustionTests：普通复核reject，最终证据复核supported=false；max_answer_attempts=1。写作模型和检索也使用确定性夹具。
- conversation_state: 固定合成政策检索响应；此例是受控故障实验，不是自然发生的生产失败。
- memory_state: 无记忆依赖。

### 逐轮断言

- 第1轮必须：终止且rag.status=insufficient；rag中无proposal；检索1次、写作2次（只适用于该注入配置）
- 第1轮禁止：展示夹具中未通过支持性复核的610金额；用户催促后跳过复核发布肯定结论；无限重试
- 观察：rag结构、render_policy文本、writer和search调用计数

### 证据及边界

- `tests.test_policy_review_exhaustion.ReviewExhaustionTests.test_final_rejection_is_bounded_and_never_publishes_unsafe_claim`
- 待确认：610为测试夹具值，不是高校政策限额；真实模型版本只共享安全契约，不能套用固定调用轨迹。
- 状态：待人工审核；真实模型未运行。
