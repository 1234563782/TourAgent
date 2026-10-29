# 发现

- company_id 并非只有名称：用于多张表的主外键、查询条件、checkpoint thread_id、遥测匿名引用、浏览器选中会话键和政策包摘要。
- 原 resources 已只有全局执行槽位；保留该设计，配额维度改 school/user。
- 新 thread_id 为 user:session；三张 checkpoint 表需同时改写，保留 namespace、parent_checkpoint_id、blob、pending writes。
- 多个历史组织标识合并可能产生账号/业务数据冲突，工具拒绝自动合并，不删除或挑选记录。
- policy bundle 删除字段会改变摘要；须显式转换文件并映射旧/新摘要，防止已审阅版本导入冲突。
- 仓库在本任务前已有多个未提交改动（含并发、UI、发票附件）；基于当前内容修改，不能用 HEAD 直接回滚。
