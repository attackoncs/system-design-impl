# 分布式消息队列（第20章）

本参考包含主题/分区、CRC和哈希链分段日志、独立HTTP broker复制、生产者epoch/
序列隔离、消费者组、持久游标及队列内原子消费输出。2026-10-26核心审查补充自动
攒批、未确认发送恢复、成员退出、分区扩容、标签过滤、定时消息与长轮询。

协调元数据仍由**一个持久化SQLite协调器**拥有；生产规模、共识及协调器HA尚未实现。

## 运行

Python 3.10+，标准库，无第三方运行依赖。在本目录执行：

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
python examples/network_demo.py
python examples/chapter_demo.py --output benchmarks/features-2026-10-26.json
python tools/benchmark.py --output .runtime/benchmark.json
```

真实网络fixture启动3个磁盘独立的broker、1个协调器和3个透明TCP故障代理。代理只
控制连通性，不实现队列逻辑。退出停止自有进程，数据、私有配置和日志保留在忽略的
`.runtime/lab-<uuid>/`。无需Kafka/Redis或云账户。最终回归和测量见
[验证报告](benchmarks/results.md)，逐项原书映射见[章节记录](docs/chapter20-review.md)。

持久运行先生成私有凭据，在4个终端分别设置 `$env:PYTHONPATH = 'src'`：

```powershell
python tools/init_config.py
python -m message_queue.cli broker --root .runtime/b0 --config .runtime/secrets.json --port 9711
python -m message_queue.cli broker --root .runtime/b1 --config .runtime/secrets.json --port 9712
python -m message_queue.cli broker --root .runtime/b2 --config .runtime/secrets.json --port 9713
python -m message_queue.cli coordinator --root .runtime/coordinator --config .runtime/secrets.json --port 9710
```

每个进程独占数据目录；不能共用broker磁盘或用同一数据库启动两个协调器。Windows
保持路径/主题名较短。默认localhost；client_token授权协调器全部操作，
cluster_token用于内部broker。外部部署需TLS、服务身份和topic ACL。
可选安装：`python -m pip install -e .`。

## 生产者和生命周期

可运行Python示例及完整字段表见[英文README](README.md)。

`Producer.send`发送一个分区的批次；未指定partition时，批中键必须映射到同一
分区，多键跨分区使用`send_many`。Python辅助方法无键随机选分区；底层`/produce`
省略partition也会拒绝键跨分区的批次；全无键时按producer确定性路由。
扩容或发送结果不确定时，需精确恢复原目标应显式指定partition。
`send_many`最多1000条，
按键分组、按1..100条发送，回执附`input_indexes`。前批成功后后批仍可能失败，
不能把它当作一个跨分区事务。

生产者用固定条带锁串行化分区序列，网络I/O不占全生产者锁；排队/重试前复制输入。
503或传输异常后保留未确认内容，拒绝以另一批覆盖相同sequence。重发完全相同请求，
或调用`retry_pending(topic, partition)`确认。重新打开同一ID会增加epoch并隔离旧
实例；跨epoch不防重复，内存sequence/pending不跨进程重启持久保存。

`producer.buffered(topic, ...)`拥有一个非daemon工作线程，默认100条批次、10ms
linger、最多1000条未完成消息；最大容量10000、linger最多5秒。`submit`返回Future，
满载抛BufferError，`flush/close`排空已接收任务，`Future.result()`报告各条失败。
批工作线程上的回调不能同步flush/close自身。先关闭buffer，再关闭Client。

`Client.fire_and_forget(request)`只发一次leader确认请求、不自动重试，改用有界
执行器（默认64个未完成调用、4个线程），返回Future。满载拒绝接收。`Client.close`
等待已接收的同步/异步操作。Future成功只表示选定的leader确认语义，进程终止仍可能
丢失未确认工作；它不是无需应答的裸网络协议。

## 消费、调度与扩容

所有接口是POST JSON + Bearer。既有`/topic`、`/producer`、`/produce`、`/fetch`、
`/join`、`/heartbeat`、`/group-fetch`、`/commit`、`/reset`、`/process`、
`/retain`、`/metadata`保留，新增`/leave`和`/expand`。

- 文本最多16KiB UTF-8，一批1..100条；可选key最长1000字符，只保证分区内顺序。
- 消息可带最多16个唯一标识符`tags`；fetch的非空tags按“命中任一个”过滤。
  limit限制扫描数，空结果也可能跨过不匹配消息而推进`next_offset`。使用返回游标；
  已提交后更换过滤条件不会找回跳过消息，除非显式reset。
- `delay_seconds`为0..86400，与绝对Unix秒`deliver_at`互斥。延迟队首阻塞本分区
  后续消息，包括过滤读取，保持顺序。`wait_seconds`为0..5，轮询等待期间释放分区/
  SQL锁；该值约束等待阶段，不强制打断已接收的broker RPC。
  调度不延长保留时间，消息可能在到期前已被保留清理。
- `/leave`显式移除当前会话并触发重平衡。加入/替换/退出/过期使generation变化；
  旧session、旧generation和旧所有者无法提交，提交不能超过扫描投递边界。
- `/expand`只允许增加分区数（最多64），建立空分区并使组generation变化，需要
  重新取分配。旧消息不搬迁；CRC32取模的键路由会改变，同键新旧消息可能位于不同
  分区。需要迁移期间顺序时由应用协调生产者；不支持分区缩容或动态broker成员。

`acks=all`默认要求配置的多数broker持久化，回执列实际确认副本。它是本操作追上
的副本集合，未实现Kafka完整动态ISR协议。少数可用时503结果可能已暂存，必须用
同序列/同内容重试。消费者看不到pending尾部。

`acks=leader`返回committed=false，表示leader和单协调器暂存，不等于多数确认或
立即可消费。后台在quorum恢复后提交，协调器pending日志自身没有复制。

至少一次：fetch后显式commit才推进；至多一次：响应前推进，允许丢失。offset表示
下一条记录。`/process`复制全部输出阶段，再用同一个SQLite事务推进输出高水位与
输入游标，txid/内容重试复用结果。“恰好一次”只限这条队列内边界，不包含外部
数据库/邮件/HTTP副作用、新txid或reset。重启中止未完成事务阶段，broker暂存尾部
在后续使用前修复。

## 工程改进、预算和边界

日志先验证整批再写，按分段合并追加，每批每个触及的数据/索引文件各fsync一次。
日志自己的锁保护读写/截断；broker合并同分区冷加载，不持全局锁做不同分区磁盘I/O。
完整CRC/哈希损坏失败关闭，只有不完整活动尾部可截断；不宣称所有Windows文件系统
具备相同掉电/目录fsync保证。

协调器拆分复制、组、保留及校验职责，采用128个有序固定条带和短SQLite WAL事务，
网络不持SQL写事务；不同分区可推进，条带碰撞或共享生产者/其他资源仍串行。服务端
对同一producer ID跨分区串行以隔离epoch变化，独立写者使用不同ID；SQLite仍只有一个元数据写者。
事务按统一顺序持有源/目标资源，保留清理失败持久重试并报告维护状态。

HTTP默认32个活跃处理线程、128监听队列、10秒绝对I/O预算、32MiB请求/响应上限。
CLI可设`--concurrency`（1..256）、`--request-timeout`（大于0且不超过3600秒）
和`--maintenance-interval`（默认1秒）。正文前鉴权，拒绝重复framing/JSON字段和
非有限数。RPC直接连接、不走环境代理或重定向，严格检查响应长度，HTTPS使用正常
证书校验。socket期限不能强杀DNS/SQL/CPU，容量保留至实际退出。关闭排空已接收
handler及自有维护线程；维护失败用不含敏感正文的日志报告，后续重试。

默认保留14天或每分区8MiB信封数据，先到者生效；删除完整旧分段，活动段可能多留。
过期offset返回410，需reset。旧回执过期后不能用旧sequence生成新写入；epoch和
事务回执仍缺完整GC。帧/批次/读取/追赶有本地预算，超出快照预算的副本不能自动追赶。

`retry`原子转入延迟重试/死信；`archive`独占创建本地JSON，最多100000条/64MiB，
延迟或并发保留可能使归档失败；`replay`验证后批量发送并分配新offset/新调度时间。
它们不是云存储分层或恰好一次恢复。

本轮最终结果见[验证报告](benchmarks/results.md)及[设计](docs/design_cn.md)。
2026-10-20的15项/34.21条每秒仅保留为历史数据。共识、协调器HA、动态broker放置、
增量复制、zero-copy、ACL/配额、云归档、跨地域恢复及持续生产规模验收仍待扩展。
