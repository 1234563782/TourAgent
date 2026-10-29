# Redis 评估进度

- 2026-10-29：完成项目结构、依赖、环境变量、memory_pg、pending_turns、run_recovery、auth、resources、execution_events 初步检查。
- 当前未修改业务代码，等待根据评估结果决定是否实现 Redis 适配层。
- 2026-10-29：继续完成单学校迁移收尾。`compileall` 通过；沙箱外运行 `tests.test_single_school_migration`，7 项全部通过。
- 清理 `evals/intent_accuracy.py`、发票文档和发票接口测试中的运行时 company_id 残留；迁移兼容脚本与迁移测试中的旧字段按设计保留。
- 正式库只读预检：memory=`ready`（148 threads）；policy 因未提供完整摘要映射而安全停止，事务未写入。
