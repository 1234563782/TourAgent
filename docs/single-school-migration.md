# 单学校存储迁移

项目只服务一所学校。运行时不再配置公司或学校 ID，也不再接受组织身份参数。
用户身份由登录会话决定，数据按 user_id 隔离；对话检查点地址为 user_id:session_id。
全校执行容量使用 global 槽位，配额账本使用 school/user 两个统计范围。

## 适用范围

迁移会删除业务表及政策文档表的 company_id 列，重建相关主键、唯一约束、外键和索引。
保留账号、密码哈希、会话令牌、行程、运行记录、附件原件和票据数据。
三张 LangGraph checkpoint 表的线程地址同步去掉组织前缀，保留检查点 ID、父关系、序列化状态和待确认操作。
resource_daily_usage 的 company 范围变为 school，subject_id 统一为 school，原计数保留。
若发现已停用的 user_preferences 表，迁移只去掉组织列并保留原记录；应用不读取此表，也不恢复偏好功能。

这是改变数据库结构的部署步骤，**不能把新代码直接启动在旧表结构上**。
新建空数据库可直接运行既有初始化命令；已有数据库须执行下面的迁移。

## 迁移前

1. 保存当前代码、原政策导入包和配置的受控副本。不要把 .env 或数据库备份提交到 Git。
2. 停止全部 CLI、Web、worker 和其他写入进程。工具遇到 running 记录会拒绝迁移，不能盲目改为完成。
3. 用数据库管理员现有的 pg_dump 流程备份会话 schema 和政策数据；用 pg_restore --list 校验，并在隔离数据库确认可恢复。
   连接凭据通过既有私密机制注入，不把包含密码的连接串复制到命令行或日志。
4. 单库时建议备份整个数据库。两库时分别备份，并在两库全部迁移前保持服务停止。

## 政策包与摘要

删除字段会改变导入包摘要，不能只改数据库列后继续使用旧包。
为每个已保存版本找到实际导入的原 JSON 包，转换到新路径：

    .\.venv\Scripts\python.exe scripts/convert_school_bundle.py --source <原包.json> --output <新包.json> --receipt <摘要映射.json>

转换工具不联网、不修改原文件，也不覆盖已有输出。映射文件包含政策键、版本和新旧摘要。
政策迁移会逐条核对数据库旧摘要；缺少任何版本的映射或摘要不匹配会停止，不猜测、不重新生成已审阅内容。
不要用同名但不同内容的包代替原包。原文件哈希、版本号、生效期、规则和引用内容保持不变。

## 预检和执行

项目使用现有 .venv 和 requirements.txt，无需新增依赖。
默认只读预检，输出表名、记录数和待改写的线程数量，不输出正文或凭据：

    .\.venv\Scripts\python.exe scripts/migrate_single_school.py memory
    .\.venv\Scripts\python.exe scripts/migrate_single_school.py policy --digest-map <摘要映射.json>

多个政策映射可重复指定 --digest-map。发现多个旧组织标识、非法线程、未知依赖、运行中请求或冲突时停止，须先人工核对。
不自动删除其他组织记录、不合并同名账号、不生成替代用户 ID。

停服并校验备份后：

    .\.venv\Scripts\python.exe scripts/migrate_single_school.py memory --apply --service-stopped --backup <会话备份.dump>
    .\.venv\Scripts\python.exe scripts/migrate_single_school.py policy --digest-map <摘要映射.json> --apply --service-stopped --backup <政策备份.dump>

工具只检查备份存在且非空；可恢复性须由管理员提前验证。
每个目标在单个事务中加锁、重复预检、迁移和核对行数，失败回滚当前目标全部变更。
两库之间没有分布式事务；第一库成功、第二库失败时保持服务停止，修复后重试第二库，或将两个库连同旧代码一并恢复。
完成目标可重复运行，返回 already_migrated。不要用 memory_admin init 代替迁移，该命令仍会清理停用的偏好表。

## 配置和前端

- 删除 .env 中 TRAVEL_POLICY_COMPANY_ID、TRAVEL_MEMORY_COMPANY_ID。
- 将 TRAVEL_RESOURCE_COMPANY_DAILY_REQUESTS 改为 TRAVEL_RESOURCE_SCHOOL_DAILY_REQUESTS，保留原值。
- 将 TRAVEL_RESOURCE_COMPANY_DAILY_CALLS 改为 TRAVEL_RESOURCE_SCHOOL_DAILY_CALLS，保留原值。
- 公司 ID 旧配置不再参与运行；旧配额键不再生效，必须迁移，避免自定义额度退回默认值。
- /api/auth/me 只返回 user_id、username。前端移除组织标识卡片。
- 当前会话 sessionStorage 键变为 aligo:conversation:{user_id}，首次升级可能重新选中会话；服务端历史未删除。
- 外部途牛字段 airlineCompany 和历史示例制度中的“公司”属于业务原始内容，保留。

## 验证与回滚

两次预检均确认 already_migrated，核对表记录数量、新主外键和旧线程地址已消失。
重新启动后验证登录、历史会话、中断继续、已保存行程、附件读取、个人并发限制、全校配额和政策检索。
验证另一个用户无法读取上述数据。迁移不自动重放失败请求或确认动作。

隔离数据库测试：

    $env:TRAVEL_TEST_POSTGRES='1'
    $env:PYTHONPATH='tests;.'
    .\.venv\Scripts\python.exe -m unittest tests.test_single_school_migration tests.test_auth_live tests.test_resources_live tests.test_chat_runs_live tests.test_university_policy_live -v

回滚需要停止新服务，恢复已验证的备份和对应旧代码/配置/导入包。不要在有新写入后直接反向补列；需先核对新增数据再决定恢复路径。
