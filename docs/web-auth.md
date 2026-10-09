# Web 登录与账号管理

## 当前范围

FastAPI 提供同源静态页面与 JSON API。登录后可以与差旅助手聊天、新建和切换历史对话。
“差旅偏好”和“历史行程”展示区域及编辑弹窗已移除，已有数据和后台记忆能力保留。
本人明确的长期偏好在聊天中经内容复核自动保存，不再要求保存授权；
临时要求、他人信息、假设和明确拒绝保存的内容不应写入。可通过聊天查询或忘记偏好。
不提供公开注册、找回密码、短信、邮件、SSO 或网页管理员后台。
账号由有权操作服务器的管理员通过本机脚本创建。所有账号的 Web 权限相同。

复用现有 PostgreSQL 连接和 `TRAVEL_MEMORY_SCHEMA`。公司由部署配置确定，
用户 ID 创建时随机生成；浏览器不能自行选择公司或用户 ID。
新账号不会自动继承旧 CLI 用户的数据，避免按用户名误绑定旧记录。
旧数据的账号归属映射需要单独确认并迁移，本次未做。

## 首次运行

在项目根目录 `TourAgent` 中执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/auth_admin.py init
.\.venv\Scripts\python.exe scripts/auth_admin.py create-user --username yourname
.\.venv\Scripts\python.exe -m uvicorn business_travel.web:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers --no-access-log
```

打开 `http://127.0.0.1:8000`，用刚创建的账号登录。
账号名限 3 至 64 个英文字母、数字、下划线或连字符，忽略大小写。
密码要求 12 至 128 个字符，通过终端隐藏输入并二次确认。
无默认账号或默认密码，不要把密码发到聊天、写入参数、`.env` 或脚本。
创建操作必须使用支持隐藏密码输入的交互终端，不支持明文 stdin 回退。

`init` 幂等创建记忆、账号、会话和运行表，不删除已有业务记录；普通 Web 启动不自动建表。
本轮交付已经执行正式库初始化，无需重复，但保留命令便于新环境部署。

## 配置

实际 `.env` 不需要增加新密钥。数据库和公司复用上一轮记忆配置。
下列变量可写入项目 `.env`；进程环境变量优先，示例见 `.env.example`。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `TRAVEL_WEB_ORIGIN` | `http://127.0.0.1:8000` | 浏览器页面来源，不含末尾 `/` |
| `TRAVEL_AUTH_SESSION_SECONDS` | `28800` | 会话绝对有效期，5 分钟至 24 小时 |
| `TRAVEL_AUTH_RATE_SECONDS` | `300` | 登录 IP 限流窗口，10 至 3600 秒 |
| `TRAVEL_AUTH_RATE_LIMIT` | `30` | 窗口内最多登录尝试次数，1 至 100 |
| `TRAVEL_AUTH_MAX_FAILURES` | `10` | 账号连续失败锁定阈值，1 至 20 |
| `TRAVEL_AUTH_LOCK_SECONDS` | `900` | 临时锁定时长，60 至 86400 秒 |

端口占用时先确认已有服务，不要终止别人的进程。若改为 8001，
同时设置 `TRAVEL_WEB_ORIGIN=http://127.0.0.1:8001` 和启动命令 `--port 8001`。
`localhost` 与 `127.0.0.1` 是不同来源，应使用配置里的准确地址。

## 账号管理

```powershell
# 重置密码并撤销该账号所有登录会话
.\.venv\Scripts\python.exe scripts/auth_admin.py reset-password --username yourname

# 停用账号并撤销所有会话
.\.venv\Scripts\python.exe scripts/auth_admin.py disable --username yourname

# 启用账号，同时清除失败计数和锁定；必须重新登录
.\.venv\Scripts\python.exe scripts/auth_admin.py enable --username yourname

# 清理本部署公司已过期的会话与限流记录
.\.venv\Scripts\python.exe scripts/auth_admin.py prune
```

重置、停用及启用不会删除偏好或行程。重复创建账号会报错，不覆盖原账号。
过期会话即使未执行 `prune` 也不能使用；生产环境应配置运维清理任务。
本轮未创建定时任务。

## API

| 方法及路径 | 行为 |
| --- | --- |
| `POST /api/auth/login` | JSON `{username, password}`，成功写入 Cookie |
| `POST /api/auth/logout` | 撤销当前会话、清除 Cookie，重复退出也返回 204 |
| `GET /api/auth/me` | 返回当前 `company_id`、`user_id`、`username` |
| `GET /api/memory` | 返回当前用户 `preferences`、`trip_history` |
| `GET /api/preferences` | 返回当前用户偏好记录 `items` 和快照 `revision` |
| `PATCH /api/preferences` | 提交 `revision` 和 `changes`，原子执行逐项增改删 |
| `GET /api/conversations?offset=0` | 本人会话列表，每页50条，返回 `items` 和 `next_offset` |
| `POST /api/conversations` | 服务端创建空白会话，返回会话 ID 和标题 |
| `GET /api/conversations/{id}/chat` | 本会话消息、检查点版本、待确认事项和标题元数据 |
| `POST /api/conversations/{id}/chat` | 提交 `{request_id, message}` 或 `{request_id, resume, revision}` |
| `POST /api/conversations/{id}/chat/stream` | 同上，以 SSE 返回真实执行阶段、已完成任务回答和最终会话快照 |
| `GET /api/conversations/{id}/runs` | 本会话最新运行的 ID、状态、可展示阶段及待核实标记 |
| `GET /api/conversations/{id}/runs/{request_id}` | 查询本人本会话的指定运行；不存在或越权返回 404 |

所有聊天 POST（包括原 `/api/chat` 路径）必须携带新的 `request_id`，
格式为 32 位小写十六进制随机 ID。一次逻辑操作只生成一次 ID；重复提交不得修改动作或确认版本。
同 ID 同内容只回读原终态检查点；同 ID 不同内容返回 409。
新 ID 代表新操作，不会按相同文本跨 ID 去重；不应通过换 ID 恢复结果未知的请求。

POST/PATCH 必须携带与 `TRAVEL_WEB_ORIGIN` 完全一致的 `Origin`。
浏览器同源调用会自动携带。没有开放 CORS，不能从任意前端域名调用。
不返回 bearer token，不使用 localStorage 保存登录凭据，也不是 OAuth 登录流程。
Cookie 名为 `travel_session`，属性为 HttpOnly、SameSite=Strict、Path=/；
HTTPS 来源强制 Secure，本机 HTTP 仅允许 loopback。

会话令牌随机生成，数据库只存 SHA-256 摘要。Cookie 失窃仍可能被使用，
应保护浏览器、使用 HTTPS，必要时重置密码或停用账号以撤销会话。
错误密码、不存在账号、停用或锁定账号均返回同一条 401 提示。
IP 限流返回 429；格式错误 422；非法来源 403；数据库不可用 503。
错误返回不回显密码、连接串或完整输入，普通写请求内容上限为 4096 字节。
聊天接口最多3000字符、20000字节，支持完整长度的中文消息。
偏好冲突返回 409；必须刷新后重新核对，不自动重试覆盖。

偏好操作示例（其中 revision 和 id 必须来自本人刚读取的快照）：

```json
{
  "revision": "<GET 返回的 64 位摘要>",
  "changes": [
    {"operation": "update", "id": "<记录 ID>", "preference": {"type": "座位", "value": "靠过道"}}
  ]
}
```

新增使用 `operation: add`、携带 `preference`，不指定 ID；
删除使用 `operation: delete` 和 ID，不携带 `preference`。
接口不接受公司/用户查询参数或请求体身份，记录归属由登录会话决定。
以上 API 仍保留鉴权和版本冲突保护，当前前端不再调用它们加载或编辑记录。
直接 PATCH 是用户操作，不调用模型；聊天自动写入则经过独立内容复核。
删除偏好不会清除历史对话检查点、备份或已经保存的行程。

## 对话持久化

- 新建、历史选择和刷新恢复均使用服务端生成的会话 ID；不是重新登录。
- `TRAVEL_MEMORY_SCHEMA` 下的 `conversations` 表保存公司、用户、标题及活动时间。
  消息、槽位、草案和待确认状态仍存入 LangGraph PostgreSQL checkpoint。
- 线程地址为 `company_id:user_id:conversation_id`；访问会话前校验归属，
  同一会话使用数据库锁防止并发执行，确认还必须匹配该会话当前检查点版本。
- 原网页对话继续使用末尾为 `web` 的线程，在列表中显示为“原有对话”。
  不删除、搬迁或重写旧检查点。原 `/api/chat` 路径仍可访问，但写请求也必须携带 request_id，
  不保留无幂等键的旧请求格式；同步及流式入口统一使用同一执行逻辑。
- 长期偏好和已保存行程仍按公司和用户共享，新建对话不会清空它们。
- 当前选中 ID 存于按公司和用户区分的 `sessionStorage`，不存登录凭据。
  待收尾的请求 ID 也暂存于此，恢复终态后清除；不存问题正文。
  未发送草稿仅在本页切换时保留，刷新或退出后清空；断流后刷新已完成答案时清除已发送草稿。
- SSE 的 `progress` 来自真实执行阶段，`delta` 只包含已完成任务的可展示回答，
  `done` 携带最终会话快照；前端用最终快照替换临时文字。不是模型逐 token 实时生成。
- 只输出白名单事件，不发送原始工具参数、槽位提案、中间模型消息或供应商响应。
- 缺少 `done`、网络异常或收到 `error` 时，前端禁止继续发送及确认，要求刷新会话核实。
  在收到最终快照后，才重新开放当前会话操作；刷新失败不会清除这一保护。
- 断线停止事件交付，不代表取消业务。当前工作线程继续完成并释放会话锁与检查点连接；
  刷新只能读取当时已持久化的状态，执行仍占用锁时可能需要稍后再刷新。
- `chat_runs` 保存身份、请求摘要、运行状态和检查点引用，不复制消息正文。
  `running` 允许刷新查询但禁止继续发送；`completed` 恢复会话操作，
  `awaiting_confirmation` 恢复确认界面；确认仍需当前 revision。
- 只有取得原会话锁后，才能把遗留 `running` 标记为 `interrupted`，不能按超时时长猜测。
  `failed` 或 `interrupted` 显示执行前历史并阻断该会话的新操作；点击“核实并恢复”，
  查看个人记录、勾选确认后建立恢复对话，原会话保持阻断和原失败状态。
  这不代表已经发生的写入被回滚，也不自动重放历史节点。
- 检查点与运行终态不是原子事务，提交间故障保守待核实；不承诺全局 exactly-once。
  本地工具不共享网页会话锁，不应同时修改同一个网页线程。运行表没有自动清理策略。
  详见 [运行记录与请求幂等验收](运行记录与请求幂等验收_2026-09-13.md)。

已有部署可只执行新增会话表的幂等初始化，不需新增密钥或修改 `.env`：

```powershell
.\.venv\Scripts\python.exe -c "from business_travel.settings import Settings; from business_travel.conversations import setup_conversations; setup_conversations(Settings.from_env())"
```

然后重启当前项目的 Web 服务。此步骤不修改偏好和历史消息。

已有会话表的部署升级运行记录时，先执行：

```powershell
.\.venv\Scripts\python.exe scripts/auth_admin.py init-runs
```

再重启 Web 服务并刷新旧页面。此命令幂等增加运行表、恢复关联表和索引，不更改账号或已有检查点。

### 执行中断恢复

- `GET /api/conversations/{conversation_id}/recovery`：核实当前偏好及失败请求开始后保存的行程草案。
  这些草案可能含其他会话的写入，不能精确归因，也不代表预订成功。
- `POST /api/conversations/{conversation_id}/recovery`：提交 GET 返回的 `run_id`、
  `review_revision` 和严格布尔值 `confirmed: true`；沿用登录、同源、归属和会话锁校验。
  记录或运行状态变化返回409，须重新核实，不能静默确认。
- 新会话仅继承最近 `completed` 且实际存在的终止检查点中的消息、槽位和任务记录。
  不继承失败轮、执行队列、草案、待确认操作或未提交原话；没有完整轮次时建立空白对话。
  恢复对话首次执行又失败时，可继续从原恢复起点分叉。
- 新会话、检查点、`chat_recoveries` 关联在同一数据库事务提交；重复确认返回同一会话。
  确认响应丢失时重新打开核实窗口，可直接打开已创建的恢复对话。
- 不自动重试模型或工具，不回滚已保存个人记录，不解除原会话阻断，不承诺外部订单核实、
  补偿或全局 exactly-once。检查点缺失时拒绝继承，返回503。
- 复验使用 `scripts/verify_auth_browser.py --run-recovery`，同时传入下文的三个运行时路径参数。
  采用故障注入图和隔离真实数据库，不将此测试当成真实模型理解或供应商预订验收。
  详见 [核实分叉验收](执行中断核实分叉验收_2026-09-16.md)。

## 部署边界

- 本机启动绑定 `127.0.0.1`，默认不对局域网或互联网开放。
- 生产部署必须配置 HTTPS 来源、反向代理、数据库最小权限及运维监控。
- 示例禁用代理头；后端直接看到的 IP 用于限流。经反向代理时可能多个用户共用
  一个限流桶。上线前必须配置可信代理边界与边缘限流，不能直接信任客户端伪造的转发头。
- 数据库账号应仅授予所需 schema 权限，初始化与运行权限建议分离。
  当前并未部署数据库 RLS、企业 SSO、MFA、细粒度 RBAC 或完整安全审计。
- 同步数据库操作使用 FastAPI 同步端点的线程池；本轮不包含压测、连接池调优或公网渗透测试。
- 限流及锁定是基础防护；账号锁定本身可能被恶意触发，需要后续审计与告警配合。
- `travel_cli.py --user` 和管理脚本仍属于可信本机工具，不经过网页登录。
  不得把 CLI 直接包成可由外部传入用户 ID 的公共接口。
- 网页聊天执行现有 Agent 图，沿用其人工确认和保存机制；不新增预订、企业审批或支付权限。
  多会话验收不代表先前业务理解和真实模型连接问题已经解决。

## 验证

```powershell
# 离线测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_auth.py -v

# 全量，包含隔离真实 PostgreSQL
$env:TRAVEL_TEST_POSTGRES = '1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

真实鉴权测试创建随机 `travel_auth_test_*` schema，并在结束后清理。
浏览器复验使用 `scripts/verify_auth_browser.py`，需要通过 `--node`、
`--node-modules`、`--browser` 显式传入本机 Node、含 Playwright 的模块目录和浏览器路径。
该脚本创建临时 schema、随机密码和隔离服务器，结束后停止并清理；
不把数据库、供应商凭据传给浏览器子进程。
追加 `--conversations` 验证多会话网页，使用测试图而非真实模型；
覆盖分页、刷新、确认隔离、用户隔离、加载失败、缺终态恢复、状态冲突门禁及桌面/手机布局。

2026-09-13 运行记录批次：全量 Python 428 项通过、0 跳过；Node 5 项通过；
扩展浏览器 8 组验收通过。真实南京酒店查询在执行中刷新后恢复，数据库只有一条完成记录。
运行中状态的完整门禁由隔离浏览器注入及真实 PostgreSQL 并发测试验证，
该次真实查询刷新后已完成，没有采样到刷新页面中的 `running` 状态。

此前多会话批次验收：开启真实 PostgreSQL 后全量354项测试全部通过，无跳过。
多会话浏览器验收通过，已检查1440px桌面和320px手机截图，无横向溢出和输入框遮挡。
会话表已在本地配置的 `travel_memory` schema 初始化，8000服务已重启并完成匿名接口检查。
这些结果证明会话管理和存储边界，不代表真实模型理解、供应商查询或逐 token 流式已验收。
