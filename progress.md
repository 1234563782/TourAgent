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
