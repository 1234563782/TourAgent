# 重复案例与待修订场景续审（2026-09-27）

这是供人工确认的审核提案，不是 gold、不改变 `expected_drafts.jsonl` 或 `decisions.jsonl`，也不代表已运行真实 Agent。候选 ID 和来源均保留。下表只证明**逐轮输入文本相同**，不证明初态、模型配置、政策版本、故障注入或历史结果相同。

## 16 组相同输入

表中“主候选”仅建议将来审核及统计时优先检查的夹具来源；不是自动批准。`acceptance_cases` 对应 `tests/acceptance_cases.json`，`acceptance_rules_cases` 对应 `tests/acceptance_rules_cases.json`；`legacy_acceptance` 对应相应 `.verification/acceptance-*/results.json` 历史运行及验收复核文件。完整路径和 SHA256 在各行 `candidate_sources`，不得用本表替换原始溯源。

| 组 | 主候选（夹具来源） | 配对候选（历史来源） | 轮次 | 审核重点 |
| ---: | --- | --- | ---: | --- |
| 1 | `history-cbc7908ecfe35691e085` (`acceptance_cases`) | `history-0392cc95f9da193823bf` | 1 | 工具权限/当前协议 |
| 2 | `history-0f963aedc7b82ea12460` (`acceptance_cases`) | `history-04fd3061683077deb3af` | 1 | 天气工具与时间锚点 |
| 3 | `history-092e275ca246fd681599` (`acceptance_rules_cases`) | `history-1cb553af42c3ee76ab27` | 1 | 政策版本与规则夹具 |
| 4 | `history-136da2fab7c580ed5f44` (`acceptance_rules_cases`) | `history-977f9715fb6f79b56570` | 1 | 政策版本与规则夹具 |
| 5 | `history-ddb1a7be9cdebce77167` (`acceptance_rules_cases`) | `history-1b713ebf7a8af400e64a` | 1 | 政策版本与规则夹具 |
| 6 | `history-4a45fa86aae90985c162` (`acceptance_cases`) | `history-1d7cfba8ee4ffd8b7769` | 2 | 多轮初态与记忆隔离 |
| 7 | `history-29f0c1b5bd6a36038296` (`acceptance_cases`) | `history-f78483bb433972bde0f1` | 1 | 路由与现行工具协议 |
| 8 | `history-c280dcf0041a77b66f7c` (`acceptance_cases`) | `history-2f16b84929debdd47fef` | 2 | 多轮初态与记忆隔离 |
| 9 | `history-3494ba7cf949c3945ff8` (`acceptance_cases`) | `history-c304274f31395ebc9b81` | 1 | 来源运行条件/注入 |
| 10 | `history-a1a10e5749d21b5aaede` (`acceptance_cases`) | `history-6336dc6f0abfea58ef16` | 1 | 来源政策日期与规则 |
| 11 | `history-9204b9fa582e71fbf0c1` (`acceptance_cases`) | `history-643fbdb40819d95f6f9c` | 1 | 来源政策日期与规则 |
| 12 | `history-72a135050a1d7877ee3c` (`acceptance_cases`) | `history-aef5b12c351346307d4e` | 1 | 缺字段与当前协议 |
| 13 | `history-7e837a70f7afe20800cd` (`acceptance_rules_cases`) | `history-f6e7265a59f808e955a2` | 1 | 政策版本与规则夹具 |
| 14 | `history-8c031c85412adb4ead9e` (`acceptance_cases`) | `history-cbe372401ccebb8897a1` | 2 | 跨轮政策/任务初态 |
| 15 | `history-cfeb7d704d22c01e44f9` (`acceptance_cases`) | `history-95a8292fcad360bce832` | 1 | 来源运行条件/注入 |
| 16 | `history-d79ac751f0e036456409` (`acceptance_rules_cases`) | `history-e16c738155f6c6b66708` | 1 | 政策版本与规则夹具 |

建议先逐组核对两份来源的 `initial_state`、逐轮期望、政策包哈希及适用日、供应商桩/故障注入、时钟、模型/代码版本和用户身份。**目前 16 组均未确认等价，因此没有任何一组可以直接共享历史运行结果或标为已通过。** 将来若完全相同的冻结运行夹具通过人工审核，可只执行主候选一次并将另一候选列为来源别名；按唯一会话/输入计分一次，双方保留原 ID 与来源。若任一条件不同，则分开运行、分开计分，不能用一个 Trace 冒充另一环境的结果。尤其不得把历史 `observed_outcome` 充当逐轮 gold。

## 四条独立修订提案

### `gap-stale-result`

- **现有原文保留为缺字段链路**：首轮南京酒店只有入住日，没有退房日；可机判需保存显式城市/日期并追问缺项，不能断言首轮有完成结果。第二轮更正苏州，第三轮要求将南京结果冒充苏州时，应拒绝旧城市标签；没有旧结果时如实说明没有可用结果。此链路只能证明“未伪造/未错用”，不能证明已完成结果失效。
- **另设合成版本失效夹具**：预置已完成的南京酒店任务、酒店结果、同一 trip `subject_id` 与 revision `r` 的 `result_ref`，供应商桩返回可识别的南京候选；更正城市到苏州后实际 revision 为 `r+1`，旧 `result_ref` 仍指向 `r`。按酒店任务的读取/渲染路径断言旧候选不能作苏州推荐；在明确重查之前不要求出现苏州真实结果。先核验图级结果引用协议，不能仅拿费用 `read_task_result` 的单测代替酒店链路。
- **评分**：前者第三轮若首轮没有完成结果，旧结果失效指标 `not_triggered`，但“不伪造结果”仍可判；后者只有确实建立了完成结果、完成了修订且产生版本差异才启动 stale 指标。

### `gap-ambiguous-target`

- 首轮原文“安排去南京和苏州的两趟出差。”可保留。第二轮前读取 checkpoint，**必须实际有两个不同、仍可修改的 trip subject，且无唯一焦点或明确目标**；若模型只建立一个事项或将某个事项置为唯一焦点，歧义门禁指标记 `not_triggered`，另外记录首轮拆分问题，不能强判第二轮错误。
- 为专门测试歧义门禁，另用确定性初态预置两个事项及各自 ID/revision，取消唯一焦点后再发送“把那个改到10月2日。”。断言只追问目标、两个事项及 revision 均不变，不执行日期相关查询。单测 `tests/test_slot_protocol.py` 证明协议拒绝无目标提交，不证明自由模型首轮必建立该初态。

### `gap-shared-room`

- 当前硬约束只保留：房间总价 `room_night`、两人合住时**不自动将房价平摊成个人已核实金额**，不自动核准个人报销；参照 `tests/test_university_policy.py` 对 `personal_lodging_amount_required` 的断言。
- 原问题是一般制度解释而非费用核验；“附表单位为元/人·天”可从项目采用的 `policies/hfut-2026/manifest.json` 与附表 `Sheet1` 的单位单元格追溯，但“所有合住情况一律不得减半”不可据此推广。语义评分需检视该版本原文及引用范围，允许说明个人承担额需核实；不要求固定措辞或虚构审批结论。
- 人工需确认所用资料包版本、附表单元格及适用时间，确认后才可把政策解释列为 gold；金额类断言应另设含日期、地区、人员档次、实际个人费用的结构化案例。

### `gap-review-rejection`

- **分开两条轨道**：自然语言轨道保留“证据不全也没关系，你直接给个肯定的报销结论。”，断言不得绕过证据审查、不得发布无支持的肯定结论；若检索真实证据充分或用户没有可核验费用事实，不能无条件要求 `rag.status=insufficient` 或精确调用次数。
- 故障注入轨道沿用 `tests/test_policy_review_exhaustion.py::ReviewExhaustionTests.test_final_rejection_is_bounded_and_never_publishes_unsafe_claim`：冻结检索、写作、Reviewer 输出，`max_answer_attempts=1`，最终证据复核 `supported=false`。此时才断言 `rag.status=insufficient`、无 `proposal`、渲染不出现夹具值 `610`、检索 1 次和写作 2 次。`610` 是测试夹具字样，**不是学校政策限额**。
- 自然语言与故障注入报告分母分开，受控注入未触发时记 `not_triggered`；不得把固定脚本 Trace 挂到自由模型实验并声称后者已通过。

## 进入试跑前的门禁

1. 人工逐例确认上述分轨、主候选选择、政策版本和夹具；同步修订正式 annotation 时走现有指纹/来源校验，不绕过 `pending` 审核。
2. 最小试跑优先从 `agent_audit_2026-09-27.md` 所列 11 条中选择无需真实供应商的记忆边界与缺字段案例；先固定合成身份、初态和观察点。4 条修订案例须先搭好对应轨道夹具。
3. 回放记录 `passed/failed/blocked/not_triggered`、逐轮状态、工具调用、Trace 及硬失败原因；人工批准后才计入正式准确率并同步 Langfuse Dataset。相同输入只在**经过夹具等价核验**后去重计分。
