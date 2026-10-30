# Redis 评估进度

- 2026-10-29：完成项目结构、依赖、环境变量、memory_pg、pending_turns、run_recovery、auth、resources、execution_events 初步检查。
- 当前未修改业务代码，等待根据评估结果决定是否实现 Redis 适配层。
- 2026-10-29：继续完成单学校迁移收尾。`compileall` 通过；沙箱外运行 `tests.test_single_school_migration`，7 项全部通过。
- 清理 `evals/intent_accuracy.py`、发票文档和发票接口测试中的运行时 company_id 残留；迁移兼容脚本与迁移测试中的旧字段按设计保留。
- 正式库只读预检：memory=`ready`（148 threads）；policy 因未提供完整摘要映射而安全停止，事务未写入。
- 2026-10-29：完成长期偏好提议与 tuniu.py、planning.py、candidate_input.py、contracts.py、memory_pg.py、graph.py、web.py、settings.py 对照；未修改业务代码。
- 评审确认身份边界、compact 候选投影、合规档位选择、长期偏好当前禁用、历史草稿可用性和硬过滤保底语义存在关键设计缺口，待整理给用户。
- 2026-10-29：完成身份映射/RAG Prompt 冲突只读诊断；确认当前 checkout 的底层规则仍不执行住宿映射，而 `ANSWER_PROMPT`、`REVIEW_PROMPT` 明确禁止该映射。`tests.test_university_policy` 39 项通过；`tests.test_rag_corrections` 因 `_ssl` DLL 环境错误无法导入。未修改业务代码。
- 2026-10-29：按用户确认清理旧交通类别→住宿档次映射残留：删除 `UniversityRules.lodging_categories` 与测试兼容分支；保留正式 `lodging_standard` 账号字段。compileall 通过，大学政策 39 项通过；计划引用测试受 `_ctypes` DLL 环境错误阻断。
- 2026-10-29：完成方案 A-D 只读评审：A 需安全复制和最小清洗；B 仅可在有逐条 evidence review 索引且重新审核时受限使用；C 简单 top_k 截断会破坏证据闭包；D 的 policy_query 继承已在 slots.py 实现，不建议在 graph.py 重复合并。未修改业务代码。
- 2026-10-29：复核用户提出的 A/B 纠偏与最终行动方案；确认 A 的 None 清洗和 B 的终止阶段受限修剪边界正确，补充 deepcopy、字段白名单、重新证据审核及普通审核不介入要求。C/D 维持不改。

- 2026-10-29：完成 .gitignore、酒店品牌筛选与审批/报销导出三项方案的证据复核；仅更新诊断记录，未修改业务代码或清理临时目录。

- 2026-10-29：完成用户出行偏好画像方案只读复核；确认身份/偏好分离与合规层内软排序正确，但长期偏好新增方案与当前退休边界和测试契约冲突，未修改业务代码。
