# Redis 评估发现

- 项目为 Python/FastAPI/LangGraph 差旅助手，依赖 PostgreSQL checkpoint、业务 memory、会话、资源配额、认证与附件/发票持久化。
- `.env.example` 明确：长期偏好、历史行程、对话检查点统一使用 PostgreSQL；认证登录尝试限制也存 PostgreSQL；资源并发与每日配额依赖 PostgreSQL 事务和 advisory lock。
- `memory.py` 的 SQLite 仅是离线测试/迁移适配器；`memory_pg.py` 是正式业务记忆和 LangGraph checkpoint 后端。
- Redis 适合补充短 TTL、跨进程低延迟状态和队列/发布订阅；直接替换 PostgreSQL 会削弱审计、事务、恢复和长期数据可靠性。
- 编译检查：`business_travel`、`scripts`、`tests` 全部可编译。沙箱内 psycopg 受 `_ctypes` DLL 限制，切换到项目既有沙箱外验证后，迁移专项 7/7 通过。
- 运行时代码和对外文档不再要求 company_id；`single_school_migration.py`、`convert_school_bundle.py` 及迁移测试保留旧字段仅用于读取、转换和验证历史数据库。
- 正式 memory 只读预检仍为 `ready`，统计 148 个历史 thread 需要改写；未执行 `--apply`。正式 policy 预检明确返回 `policy_digest_mapping_required_convert_all_existing_bundles`，说明必须先为全部既有政策包生成摘要映射；同样未写入数据库。
