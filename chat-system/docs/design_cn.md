# 设计：聊天系统

## 范围与架构

本地实现遵循第 13 章的单聊/群聊消息、设备收件箱、在线状态（Presence）和通知流程。原书将有状态的聊天/在线状态服务器与无状态的 API 服务及存储分离开来；本实现让它们运行在同一个 asyncio 事件循环中。接口与参考注册表用于体现这些边界，而不是假装部署了一个分布式集群。

```text
WebSocket 客户端（每个用户/设备一个会话）
                |
        JSON WebSocket 适配器
                |
          ChatService ------- TokenAuthenticator
          /    |     \
     sessions presence  异步推送适配器
          |
     MessageStore 协议
          |
     SQLiteMessageStore
     channels / members / messages / inbox

ServiceDiscovery：用于集成的独立区域/容量注册表
```

## 包结构

```text
chat-system/
  pyproject.toml
  README.md
  README_CN.md
  docs/{requirements,design,tasks}.md
  src/chat_system/
    __init__.py
    models.py       # 配置、不可变的消息/频道、异常
    store.py        # 存储协议与 SQLite 适配器
    service.py      # 认证、会话、扇出（Fanout）、在线状态、推送集成
    discovery.py    # 参考用的容量感知注册表
    transport.py    # 可选的 JSON/WebSocket 适配器
  examples/
    basic_chat.py
    websocket_server.py
  tests/
    test_chat.py
    test_transport.py
```

## 持久化与消息顺序

- `channels(id, owner, kind)` 存储单聊/群聊元数据。
- `members(channel, user)` 使用复合主键。
- `messages(id, channel, sender, content, created, client_id)` 使用 SQLite AUTOINCREMENT。
- `(sender, client_id)` 唯一，使重试在多设备/重启后保持幂等。
- `inbox(user, message)` 为每个成员（包括发送者）保存一条引用。
- `(channel, id)` 与 `(user, message)` 索引支持有序的历史查询和收件箱分页。

消息插入与收件箱扇出共享同一个事务。服务器在入队实时事件或调用推送之前先完成提交。在此存储中 ID 递增，并用于排序，而不是墙钟时间戳。分布式实现会用每频道序列（per-channel sequence）或协调式 ID 生成器以及持久化的扇出工作进程来替代。SQLite 操作是同步的且刻意保持很小；生产级存储适配器必须避免阻塞事件循环。

新加入群组的成员不会被回填（backfill）之前的收件箱条目。历史查询要求当前成员身份和收件箱条目，防止意外暴露加入前的消息。单聊频道的创建会复用既有的配对。

## 会话、背压与在线状态

会话绑定一个经过认证的用户、设备、有界 asyncio 队列以及最近一次心跳。同一用户/设备的第二次登录会替换第一个会话；其他设备保持连接。核心逻辑在每次操作时都会校验会话身份和凭据。

每条新消息都会入队到所有接收设备。队列溢出会关闭慢会话并发出一个恢复事件。持久化收件箱（而非易失性队列）才是事实来源。客户端重连后，从其最近一次完整处理的游标（Cursor）显式同步。不能仅仅因为收到了更晚的实时事件就推进该游标：更早的已提交消息可能尚未通过实时通道送达。

服务使用单调时钟使会话过期。传输层清理会周期性运行。显式登出会立即关闭；意外的连接丢失则让会话在心跳宽限期过后过期。只要任意一台设备是新鲜的，该用户就保持在线。在线状态订阅只发布状态转换；哪些联系人可以被观察属于嵌入应用的授权职责。

## 推送与失败语义

默认的推送适配器只记录调用。离线接收者会针对每条新提交的消息触发一次调用。提供方异常会被记录，并且不会改变发送结果。推送重试和持久化通知发件箱（outbox）属于未来的集成工作。在提交之后、实时/推送扇出之前发生的崩溃可能丢失该通知，但消息仍可通过同步找回。这并非精确一次（exactly-once）的网络投递；幂等写入和游标恢复才是明确的保证。

## WebSocket 协议

使用带有 `request_id`、`op` 及各操作特定参数的 JSON 对象。第一个请求：携带 `token` 和 `device_id` 的 `login`。响应使用 `type=response`，回显 `request_id`，并包含 `ok` 以及 `result` 或 `error`。主动推送的事件带有 `type=message`、`type=presence` 或 `type=closed`。

操作包括：`login`、`heartbeat`、`direct`、`group`、`add_member`、`send`、`sync`、`history`、`subscribe_presence`、`logout`。`send` 包含 `channel_id`、`content`、`client_message_id`；`sync` 包含 `after` 和 `limit`。成功的同步返回消息和 `next_cursor`，当没有消息时保留输入游标。WebSocket 客户端将请求/响应处理与事件处理分别串行化。

令牌通过带外方式创建，绝不通过未经认证的公开端点返回。示例绑定到 localhost。远程部署需要 TLS 和外部认证器；本演示不提供账号注册或密码存储。认证设有超时，WebSocket 帧大小有上限，同时仍允许有效的 Unicode 消息。未知操作和畸形数据会返回安全的错误。优雅的服务器退出会取消后台任务并关闭会话。

## 发现与扩展

内存注册表按区域亲和性、利用率，然后是稳定的服务器 ID 来选择一个未满的服务器。调用方注册更新后的连接数；选择并不预留容量。它不会自动发现故障，也不会跨进程路由消息。共享持久化存储、服务器租约、服务器间消息代理和全局在线状态是水平部署前的必要条件。

## 验证

需求映射到任务和测试。核心测试使用 `asyncio.run` 和假时钟，不使用基于 sleep 的断言。WebSocket 测试使用临时回环端口和可选的传输依赖。基于文件的重启测试验证持久性；授权与幂等测试验证重试和并发下的隔离性。

## 分布式扩展

分布式模式使用独立的 asyncio WebSocket 进程和一个共享的 Redis 后端。Redis Streams 日志取代仅限本地的扇出；每个节点独立消费并对其游标做检查点（checkpoint）。Redis 哈希存储频道/消息/重试键；有序集合（sorted set）为用户收件箱建立索引。一个 Lua 脚本检查成员关系快照，并原子地一并提交消息、所有收件箱引用、日志事件和推送候选（push candidates）。成员关系变更会触发快照重试，而不是对部分接收者扇出。

Redis 服务器时间是节点/设备租约的权威来源。节点实例 UUID 用于对重复的节点 ID 进行围栏（fence）；每个设备会话拥有独立 UUID，因此在另一节点上的重连会使之前的设备会话失效。原子准入会检查节点租约和容量。用户在线状态只考虑租约新鲜的节点上的新鲜会话。节点轮询共享在线状态以获取联系人订阅。

无状态发现 HTTP API 按区域和利用率选择存活节点。重连客户端在连接失败后调用它，在新的端点上认证，并重放其持久化收件箱游标。发现服务不迁移活动套接字。

推送候选使用 Streams 消费者组。工作进程回收被遗弃的待处理（pending）条目，复查共享在线状态，调用记录到 Redis 或 HTTP webhook 的适配器，并确认完成。失败元数据和重试截止时间是持久的。耗尽重试次数的任务会进入死信（dead-letter）流。HTTP 调用携带幂等键；由外部提供方决定重复抑制。

Redis 必须在启用 AOF 和持久化存储的情况下运行。脚本使用同一哈希标签（hash tag）命名空间中的键以保持原子性；该参考模式使用共享主库，并非分片的 5000 万用户部署。下文可选的 Sentinel 部署提供 Redis 主库故障转移。跨区域仲裁存储不在本参考实现范围内。Redis 中断会导致请求失败，而不是接受易失性写入。

分布式源码模块：`redis_backend.py`、`distributed.py`、`push.py`、`cluster.py` 和 `client.py`。CLI：`python -m chat_system.cluster`。现有的本地公共 API 保持可用。传输适配器既可以等待本地服务方法，也可以等待分布式服务方法；网络测试通过真实套接字演练独立进程。

Redis API 参考：[Lua 原子执行](https://redis.io/docs/latest/develop/programmability/eval-intro/)、[待处理流条目恢复](https://redis.io/docs/latest/commands/xautoclaim/) 和 [异步客户端生命周期](https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html)。

## Sentinel 与容量扩展

可选的 Sentinel 配置用异步 `master_for` 客户端取代直接的 Redis URL。读写使用被发现的主库；晋升（promotion）会导致连接池刷新其目标。三个 Sentinel 投票者（法定票数 quorum 为 2）和两个 Redis 副本伴随主库。三个数据节点均使用 AOF 和持久化卷。Sentinel 状态文件可写并被保留。生产环境的投票者必须运行在独立的故障域中；本地 compose 仅是可复现的拓扑。

所配置的副本确认使用非事务性流水线，在同一连接上包含写入、短暂的复制屏障（replication barrier）和 `WAIT`。当幂等重试未产生任何变更时，该屏障也会确认既有状态。确认失败是一种不确定的结果，而不是回滚：客户端用稳定键重试。这缩小了异步复制的丢失窗口，但不会让 Sentinel 成为共识数据库，也不会在同时发生故障时保证零丢失。保持至少一个健康副本可用，并为分区场景配置主库写入保护措施。

每节点容量索引按节点实例划分作用域，设备更换时从上一个节点原子移除。有序集合计数取代每次登录的扫描。过期会话记录以有界批次修剪；临时索引带有 TTL。发现机制检查存活节点记录并统计其索引，而不是遍历所有设备。消息扇出只查找属于频道成员的本地会话。每个流批次通过有界流水线验证不同的接收者，并且只有在该批次中的每个事件都入队之后才进行检查点。崩溃重放可能重复至多 100 条实时提示；客户端利用持久化收件箱进行去重和恢复。空闲会话校验同样使用有界流水线。

容量压测工具驱动真实的 WebSocket 端点，维持心跳，收集确认和投递 ID，并生成 JSON/Markdown 报告，内容包括已配置/已准入连接数、精确的成功/缺失计数、吞吐量以及 p50/p95/p99 延迟。故障测试会杀死自有 Redis 主库，并验证晋升和持续聊天。容量报告仅描述实测的本地规模。

参考：[Sentinel 故障转移](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/) 和 [WAIT 复制确认](https://redis.io/docs/latest/commands/wait/)。
