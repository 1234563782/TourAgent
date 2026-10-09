# Evaluation Coverage Inventory

Static evidence only. No current model accuracy or complete coverage is claimed.

| Category | Engineering scenarios | Proposed dialogues | Current model run |
| --- | ---: | ---: | --- |
| concurrency_budget | 11 | 0 | not_run |
| memory_revision | 9 | 3 | not_run |
| policy_rag | 10 | 4 | not_run |
| recovery_idempotency | 6 | 0 | not_run |
| tool_state | 5 | 1 | not_run |

## Unverified Dimensions

- 真实模型多轮理解、重复运行稳定性和跨模型差异
- 人工标注相关条款后的真实检索Recall@K及排序质量
- 政策版本切换后的旧缓存、索引与引用一致性端到端验证
- 提示注入、伪造工具结果及敏感信息泄露的系统化对抗集
- 真实供应商429、网络中断、MCP故障与取消传播端到端验证
- 多Worker持续压力、延迟分位数、队列公平性及数据库故障切换
- 长会话压缩后事实保留与短期记忆容量边界
- 人工审核后的独立holdout、评审一致性和Langfuse实验关联

## Interpretation

- Static AST extraction only; tests, agents and databases were not executed.
- Assertion locations are evidence, not independently reviewed gold contracts.
- Full source files retain setup/import dependencies; no portable replay fixture is claimed.
- Report sections are linked, not automatically classified as product badcases.
- Duplicate groups cover exact conversations only, not semantic or prefix duplicates.
- Other reports remain in the original inventory backlog.

## Rebuild

```powershell
.\.venv\Scripts\python.exe -X utf8 -m evals.build_coverage
```
