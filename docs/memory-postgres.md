# PostgreSQL 记忆存储与迁移

## 当前方案

助手不保存长期偏好。座位、酒店和时间习惯只属于当次行程。会话检查点、账号身份和经确认的行程草案仍然保留。

默认入口 `travel_cli.py` 将已确认行程和对话状态存入 PostgreSQL，
不引入 MySQL，不自动回退到 SQLite。记忆与政策可共用数据库，但使用独立 schema。

| 数据 | 存储 | 隔离 |
| --- | --- | --- |
| 已确认行程 | `trips`，JSONB、保存时间、递增顺序 | 用户、草案 ID 复合主键 |
| 会话状态与中断 | 官方 `checkpoints`、`checkpoint_blobs`、`checkpoint_writes` | `user:session` |
| 导入记录 | `memory_imports`，源数据摘要及数量 | 用户、源摘要 |

记忆工具只读取最近若干条已确认行程草案，数据库保留全部行程记录。
同 ID 同内容行程重复保存不新增记录；同 ID 不同内容会报错，不覆盖旧记录。
保存行程仍必须先由用户确认。普通问答只留在会话检查点，不提取成长期事实。
学校政策始终优先于当次口头习惯。应用不再创建或读取长期偏好表，也没有保存、修改或忘记偏好的工具。

显式 `scripts/memory_admin.py init` 会 `DROP TABLE IF EXISTS user_preferences`，不重建该表，
也不清理行程、检查点或账号。普通启动不会连接数据库执行删除。

`read()` 只返回 `trip_history`。`query_memory` 和 `GET /api/memory` 同样只返回已确认行程草案。
旧 SQLite `users` 表和旧 JSON 里的偏好字段在迁移时解析后丢弃，不写入 PostgreSQL。

## 配置

程序自动读取根目录 `.env`。本轮未修改真实 `.env`，已经配置政策 PostgreSQL
连接的部署无需重复填写密码。

| 配置键 | 默认及说明 |
| --- | --- |
| `TRAVEL_MEMORY_DATABASE_URL` | 留空复用 `TRAVEL_POLICY_DATABASE_URL` |
| `TRAVEL_MEMORY_SCHEMA` | `travel_memory`；只能是小写字母开头的 SQL 标识，不得为 `public` 或 `pg_` 开头 |
| `TRAVEL_MEMORY_CONNECT_TIMEOUT` | 10 秒，允许 1 至 120 秒 |
| `TRAVEL_MEMORY_HISTORY_LIMIT` | 10 条，允许 1 至 100 条，仅控制查询展示条数 |

SQL 执行超时复用 `TRAVEL_TIMEOUT_SECONDS`。密码等特殊字符需按 PostgreSQL
连接串要求编码；生产环境使用 TLS 和密钥管理，不把完整连接串放进终端命令。
用户、会话标识允许 1 至 64 个 ASCII 字母、数字、下划线或连字符。

首次初始化需要 schema、表和索引创建权限；官方检查点 setup 需要自动提交模式。
生产应区分部署账号与运行账号，只给运行账号必要的表、序列权限。
本次没有自动修改数据库账号或授权。

```powershell
.\.venv\Scripts\python.exe scripts/memory_admin.py init
.\.venv\Scripts\python.exe scripts/memory_admin.py check
.\.venv\Scripts\python.exe travel_cli.py --check-config
```

`init` 可重复执行，不清理行程和检查点，但会删除已停用的 `user_preferences`。`check` 不再要求该表，只读检查其余表是否存在，
不代表已经验证每种写入权限，也不验证模型和供应商密钥。
遇到连接、权限或配置问题会停止，不自动创建 SQLite 兜底。

## 旧数据迁移

本工作区的既有数据已于 2026-09-08 迁移，详情见本轮验收报告。
下面用于其他部署或后续明确的旧数据导入：

1. 停止旧 CLI 和所有旧数据写入者；备份 SQLite（包括活动 WAL）和旧 JSON。
2. 运行 `init`，然后 `inspect`，确认旧用户属于本学校且不存在账号冲突。
3. 对每个用户先运行不带 `--apply` 的预检，再显式导入。
4. 用原来的 `--user`、`--session` 重启；存在未完成操作时先 `/resume`。

```powershell
.\.venv\Scripts\python.exe scripts/memory_admin.py inspect
.\.venv\Scripts\python.exe scripts/memory_admin.py migrate --user employee_demo
.\.venv\Scripts\python.exe scripts/memory_admin.py migrate --user employee_demo --apply
.\.venv\Scripts\python.exe travel_cli.py --user employee_demo --session default
```

默认读取 `TRAVEL_STORAGE_DIR` 下的 `memory.sqlite` 和 `checkpoints.sqlite`，
以及 `TRAVEL_LEGACY_MEMORY_DIR` 下的用户 JSON。命令可显式传入
`--source-dir` 和 `--legacy-dir`；不要把其他部署的旧数据混入本学校。
本地 SQLite 的用户归属必须由管理员确认。

预检不连接目标数据库，因此不能提前证明目标没有冲突。正式导入采用每用户一个事务：

- SQLite 以只读方式打开，不运行旧 saver 的建表或日志模式切换。
- 导入全部行程，不受“最近十条”的展示限制。
- 旧偏好字段会被解析后丢弃，不导入，也不因格式异常阻断行程导入。
- 缺 ID 行程、同 ID 不同内容、缺父检查点、非已验证 v4 检查点均停止处理。
- 通过官方 saver 存储检查点；保留父关系、元数据和中间写入原始索引及序列化值。
- 目标行程内容冲突、目标会话已存在，均回滚本用户导入。
- 提交前逐项回读比较行程、检查点、父关系、元数据和中间写入。
- 相同源摘要重复执行返回 `already_imported`，不会覆盖迁移后更新的行程或会话。

维护事务会锁定记忆表，阻止其他写入；应在维护窗口运行，不适合在线批量迁移。
当前会把单用户源记录读入内存，用于本项目的小规模旧库；大规模迁移需另外设计分批与断点。
旧 SQLite 的 saver 没有保存 `task_path`，迁移保留其语义为空值，不编造原路径。
当前迁移已验证本项目图的检查点；修改图的节点、状态结构或更换序列化器后需重新验收。

迁移不是双向同步。旧文件保留，但不会包含切换后产生的新消息。
如需回退，先停止新入口、备份 PostgreSQL，再制定新增数据回迁方案；
不要简单切回旧文件而静默丢失新记录。

## 身份边界

业务 SQL 固定绑定用户，模型工具不接受任意用户标识。
官方 saver 外层检查每次同步读写的会话归属，包括历史列表、删除和 delta 历史读取；
不允许无范围列出整个数据库检查点。Agent 构建时还检查记忆与检查点身份一致。

这些存储接口提供应用层隔离，本身不验证登录，也不是数据库 RLS。
新增 FastAPI 工作台通过 PostgreSQL 登录会话构建身份，再调用这些接口，见 `web-auth.md`。
CLI 的 `--user` 仍由操作者自行指定，不能直接公开成多用户接口。
未来 HTTP 服务必须从验证后的登录凭据构建 `MemoryIdentity`，
核对账号与学校人员的关系，再创建该身份的仓库和检查点；不能相信前端或模型传入的用户 ID。
对话检查点当前仅接同步 CLI；Web 工作台可读已确认行程，不读取长期偏好。
未来异步 Web 对话服务需另外接入并验证 AsyncPostgresSaver。

`/clear` 只切换会话，CLI 会显示新的 session ID，不删除长期记忆和旧检查点。
按用户导出、删除、保留期限、审计、并发同会话请求串行化和生产备份恢复，
仍需在正式多用户服务上线前补齐。不要将本轮存储迁移当成生产安全验收。

## 验证

离线回归默认跳过真实数据库测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

显式运行真实 PostgreSQL 测试：

```powershell
$env:TRAVEL_TEST_POSTGRES = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_memory_pg_live.py -v
Remove-Item Env:TRAVEL_TEST_POSTGRES
```

测试复用连接但创建随机 `travel_memory_test_*` schema，仅写合成数据，
完成后只删除本次生成的测试 schema，不修改正式记忆或政策表。
测试需要建 schema 和删 schema 权限。测试采用脚本化模型，不调用模型、
途牛、天气、地图或 Embedding 服务，也不能证明真实模型回答质量。

已有 PostgreSQL 组织字段迁移见 [单学校迁移说明](single-school-migration.md)。
