# TourAgent Agent Evaluation Set v1

这是一套面向 TourAgent 的第一版评测数据集，重点覆盖状态机、工具调用、政策 RAG、两层记忆、版本失效、失败恢复、幂等、并发准入和请求级模型调用预算。

## 目录

- `routing.jsonl`：路由、多事项拆分、槽位追问和更正。
- `policy_rag.jsonl`：条款召回、引用连续性、结构化住宿规则和证据不足。
- `tool_safety.jsonl`：工具白名单、Schema、调用门禁、失败和重试边界。
- `memory_revision.jsonl`：事项版本失效和长期偏好功能已移除的回归。旧偏好 CRUD 案例已退出当前运行清单；历史报告保留。
- `recovery_idempotency.jsonl`：失败上下文、显式恢复、幂等和旧结果失效。
- `concurrency_budget.jsonl`：共享 API Key 场景下的并发槽位、队列、配额和请求预算。

## 样本格式

```json
{
  "case_id": "recovery-001",
  "category": "recovery",
  "oracle_type": "state_assertion",
  "case_type": "regression",
  "severity": "high",
  "source": "project_regression_suite",
  "expected_outcome": "state_match",
  "conversation": [
    {"role": "user", "content": "核验 2026-10-12 合肥住宿，实际 413 元。"},
    {"role": "user", "content": "金额更正为 467 元。"}
  ],
  "expected": {
    "route": "policy_check",
    "status": "clarify_or_failed"
  },
  "allowed_tools": ["SlotSubmission", "consult_policy"],
  "forbidden_behaviors": ["reuse_stale_result", "duplicate_execution"],
  "tags": ["revision", "recovery"]
}
```

字段约定：

- `oracle_type`：`schema_assertion`、`state_assertion`、`retrieval_assertion`、`human_review` 或 `fault_injection`。
- `case_type`：`golden`、`regression`、`edge`、`adversarial`、`fault_injection` 或 `production`。
- `severity`：`low`、`medium`、`high` 或 `critical`，用于判断安全门禁。
- `source`：Case 的来源；当前第一版均来自项目回归测试。
- `expected_outcome`：预期结果类别，例如状态匹配、安全拒绝或故障被隔离。
- `expected`：可由状态、事件、工具调用或最终答案断言的结果。
- `allowed_tools` / `forbidden_tools`：工具调用安全边界。
- `forbidden_behaviors`：不能出现的副作用，例如旧 revision 继续生效、失败后盲目重放。

## 离线执行

## Jev 意图准确率评测

`intent_accuracy_cases_100.jsonl` 是当前的100条人工标注意图测试集，覆盖四类意图、单意图、多意图、否定、假设、政策解释、费用核验以及保存/确认/修改/取消出差等 `other` 操作。原来的20条小样本保留在 `intent_accuracy_cases.jsonl`，可用于前后版本快速对比。

脚本默认使用100条数据：

```powershell
.\.venv\Scripts\python.exe evals\intent_accuracy.py `
  --output .verification\intent-accuracy-100.json
```

先跑少量样本确认接口和 Key：

```powershell
.\.venv\Scripts\python.exe evals\intent_accuracy.py `
  --limit 5 `
  --output .verification\intent-accuracy-smoke.json
```

需要把 Trace 和 `intent_exact_match` 分数写入 Langfuse 时，加上 `--langfuse`。准确率统计只针对有 Jev 正常返回的样本，接口不可用的样本会标记为 blocked，不会被当作错误分类。

第一阶段 Runner 不调用真实模型、MCP、供应商或正式数据库，而是把每条 Case 映射到已有的确定性或合成回归测试并真实执行：

```powershell
# 校验 52 条 JSONL 数据
.\.venv\Scripts\python.exe -c "from pathlib import Path; from evals.dataset import load_cases; print(len(load_cases(Path('evals'))))"

# Runner 自测
.\.venv\Scripts\python.exe -m unittest tests.test_eval_runner -v

# 默认离线基线；PostgreSQL 集成 Case 会标记为 blocked
$env:TRAVEL_TEST_POSTGRES='0'
.\.venv\Scripts\python.exe -m evals.runner

# 隔离 PostgreSQL 基线；需具备创建和删除 schema 的权限
$env:TRAVEL_TEST_POSTGRES='1'
.\.venv\Scripts\python.exe -m evals.runner
Remove-Item Env:TRAVEL_TEST_POSTGRES
```

输出文件：

- `evals/results/results.jsonl`：逐 Case 状态、证据选择器、耗时和失败原因。
- `evals/results/summary.json`：覆盖率、证据通过率、分类统计和关键门禁失败。

状态口径：

- `passed`：映射的全部回归证据通过。
- `failed`：证据失败、报错或测试选择器失效。
- `blocked`：证据依赖的环境未启用，例如 PostgreSQL 集成测试被跳过。
- `unmapped`：Case 尚未绑定自动执行证据。

`mapping_coverage` 表示已绑定自动证据的 Case 比例；`execution_coverage` 排除 `blocked` 和 `unmapped`，表示本次实际执行完成的比例；`evidence_pass_rate` 只在实际执行完成的 Case 中计算。关键 Case 失败、阻塞或未映射时，`release_gate_passed` 为 `false`。这些指标都不等于真实 Prompt 准确率，也不评价自然语言质量。

PostgreSQL 集成测试创建随机 `travel_resource_test_*` schema，只写合成数据，并在结束时删除本次 schema；不得把测试开关指向无建删 schema 权限或不允许测试写入的数据库。

## 88 条候选的逐轮预期草稿（待审核）

`review/expected_drafts.jsonl` 覆盖全部 88 条候选的 230 个用户轮次，每轮包含必须行为、禁止行为、状态/工具/证据观察点、确定性检查与语义 Judge 检查。`review/expected_review.md` 提供逐例导航及相同输入分组。

```powershell
.\.venv\Scripts\python.exe -m evals.prepare_expected
.\.venv\Scripts\python.exe -m unittest tests.test_eval_expected tests.test_eval_annotations tests.test_eval_review
```

新增 73 条由现有夹具预期与自然语言场景推导，15 条先前标注原样保留；其中 4 条需先修订触发条件。16 组相同输入暂不合并，因为来源环境可能不同。历史验收结果不是逐轮金标，尤其不能自动确定政策金额。所有审批仍由 `review/decisions.jsonl` 人工完成；当前全部 `pending`、未运行真实 Agent，**不能直接将草稿上传到 Langfuse 当成 gold**。先确认版本、初态、故障注入和重复组，再绑定回放适配器及评分器。

## 后续分层

## 88 条候选的隔离开发试跑

`real_agent_batch.py` 使用真实模型和当前配置的政策后端；每条候选使用新的临时长期记忆与内存 checkpoint，多轮按输入顺序执行。真实差旅供应商与天气接口使用拒绝调用桩。结果逐例写入 JSONL，`.manifest.json` 记录候选哈希，意外中断后用 `--resume` 续跑未记录的案例。

```powershell
.\.venv\Scripts\python.exe -m evals.real_agent_batch --output .verification\eval-batch-20260928.jsonl
.\.venv\Scripts\python.exe -m evals.real_agent_batch --output .verification\eval-batch-20260928.jsonl --resume
```

这里观察的是空初态的实际模型表现，不是历史验收环境的重放；缺少的历史 checkpoint、故障注入和政策版本不会自动补造。每例 `scoring_status` 均为 `not_scored_pending_human_review`；`completed` 仅表示执行结束，不表示回答正确。中断、模型不可用和不能复现的前置条件须单独检查。输入和回答保存在本机，不自动上传 Langfuse 或晋升正式 gold。

## 长期偏好评测已删除

长期偏好保存、快照和对应 Judge 已从当前产品移除。历史实验报告保留，不再提供可执行入口。

1. 先做离线协议评测：解析 JSONL，运行确定性状态机和工具门禁断言。
2. 再做合成模型评测：固定模型输出，检查路由、Schema、revision、调用次数和失败分支。
3. 最后做小规模真实模型抽样：只评估语言理解和引用质量，不把一次真实模型通过当成整体可靠性证明。
4. RAG 结果应同时检查召回、引用连续片段、版本一致性和最终结论；不能只看答案字符串。

当前自动评分器提供的是“Case 到项目回归证据”的第一层闭环。真实模型评测仍需单独记录模型版本、Prompt 版本、知识库版本、随机性参数和 Trace，并对路由、工具、检索、答案忠实度分别评分。
