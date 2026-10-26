
## 2026-10-18 当前运行时证据（追问全地区）

- 浏览器当前 8000 会话显示：首问“我是院士，我的住宿标准是多少”先要求目的地；补充“北京”后成功进入政策解释，返回北京部级 1100 元/人·天。
- 同一会话随后发送“所有地区的都列举一下呢”收到“语义复核未完成”；再次发送“所有地区的住宿标准都列举一下”收到“模型输出的事项信息未通过校验”。这证明失败仍发生在槽位提取/语义复核阶段，没有进入 policy_rag。
- 当前页面为旧会话，不能仅凭页面内容证明服务是否重载修复；需要新建会话并检查最新 Langfuse Trace 的 slot_protocol 原始输出。
- Edge 扩展因 `unsupported Codex auth method: apikey` 无法绑定；Codex 应用内浏览器仍可读取本地 8000 与已登录 Langfuse 页面。

## 2026-10-18 新会话真实回归结果

- 新建会话后首问“我是院士，我的住宿标准是多少”成功要求目的地。
- 补充“北京”成功返回住宿人员档次“部级”和北京 1100 元/人·天；链路可用。
- 追问“所有地区的住宿标准都列举一下”不再触发槽位兜底，已进入政策检索并返回政策依据。
- 但当前结果只列出西藏、天津、黑龙江、河北、海南、内蒙古 6 个地区，并提示“未覆盖所有地区”。因此当前剩余缺陷是全地区 coverage 没有使用完整结构化附表，而是落回 top-k RAG 片段。

## 2026-10-18 Langfuse 21:10 证据

- 刷新 Langfuse 后看到新会话请求：21:09:27 首问、21:10:09“北京”、21:10:40“所有地区的住宿标准都列举一下”。
- 新的追问 Trace 结果已完成，响应正文是“部分地区”列表，不再是槽位兜底。
- 需要打开 21:10:40 Trace 的子观测，确认 `policy_retrieve` 是否带 `coverage=all_regions`，以及 `policy_rag` 最终状态/`lodging_table` 是否在服务层被丢失。

## 2026-10-18 最新 Trace 深层证据

- 最新 Trace `2959a099958450d68b7065925e4f3cbc` 已确认完整进入 `consult_policy → policy_rag → policy_retrieve → policy_rerank`。
- 第二次 `policy_retrieve` 的输入查询为“未覆盖所有地区”，事实只剩 `{"kind":"hotel"}`；输出 `lodging_lookup` 反而提示缺 `destination` 和 `lodging_standard`，且没有 `lodging_table`。这解释了为什么随后模型只能基于 9 个 top-k 来源生成 6 个地区并声称“未覆盖所有地区”。
- 直接调用本地 `PostgresPolicyStore.search_for_facts(..., coverage=all_regions)` 已返回 `table_rows=86`、`source_chunks=86`，数据库完整数据存在。
- 当前高置信根因：RAG 首次检索发现证据不完整后，二次检索使用了被模型回答阶段裁剪/重建的事实，丢失原始 `destination`、`lodging_standard` 和全地区 coverage；重检索因此降级为普通 RAG。

## 2026-10-18 最新 Trace 修正后的根因收敛

- 21:10:40 Trace 的首次、二次 `policy_retrieve` 输入都只有 `facts: {kind: "hotel"}`；这不是 RAG 截断，而是进入 `consult_policy` 前当前 expense subject 已经只剩 `kind`。
- 该 Trace 的 `policy_retrieve` 第一次已经返回 10 个普通来源，第二次返回 9 个普通来源；没有 `lodging_table`，说明 all_regions 结构化分支没有被触发。
- 本地直接调用 store 已证明数据库和 86 行附表完整，故问题在追问路由/槽位合并：范围扩展被当成新任务或新 expense subject，未继承北京、院士/住宿档次等已确认事实。
- 下一步读取 `slot_protocol` 与 `slot_review` 原始输出，确认是 JEV route、TurnRoute 目标识别，还是 ExpenseExtraction patch 造成新 subject，再做确定性修复。

## 2026-10-18 Trace 结构补充

- 当前 Trace 的 `slot_review` 输出为 `verdict=accept`，说明语义复核没有拒绝这次状态转换。
- `policy_rag` 之所以继续生成普通回答，是因为它接收到的 facts 已只有 `kind=hotel`；即使 RAG 本身能返回 86 行，调用层也未带上全地区查询所需的事实/范围。
- 仍需从 Trace 下载 JSON 或读取 `slot_protocol` 原始输出，确认该追问被路由为 `new_task` 还是 `update_facts`，以及是否是 `ExpenseExtraction` 只提交了 kind。
