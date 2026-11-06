# 系统设计面试共享术语表（第 17—29 章）

本表汇总英文第 17—29 章的常用术语，供阅读正文、图注和原图时对照。其中第 21、27 章已有纸书译文，第 27 章旧译保留作参考。中文译法优先采用技术书籍中的惯用表达；同一术语在同一技术语境中保持一致。

## 翻译约定

- 专有概念首次出现时，可使用“中文（English，缩写）”；产品名、协议名和通用缩写保留原名，例如 Redis、DynamoDB、SMTP、API。
- 标识符保持原样，包括 API 路径、SQL 表名和列名、JSON 键、变量名及命令参数，例如 `user_id`、`bucket_name`、`object_id`、`ZREVRANK`。代码块及其注释、URL、独立数学公式和原始数值不因翻译而改写。
- 根据语境区分含义：数据库中的 transaction 译为“事务”，金融业务中的 transaction 通常译为“交易”；rank 可在行文中自然写作“排名”或“名次”。
- 默认使用“关系型数据库”和消息“交付保证”；发送邮件等具体动作可使用“投递”。消息交付与业务处理的语义应分别表述。
- 原图沿用原链接，图内英文标注可对照本表阅读；图注和较长的图片替代文字使用中文。图内字段名及产品名仍按原标识符识别。
- 原文中的疑似错误通过单独的校勘记录说明，不在翻译时自行改动技术结论、编号或公式。

## 系统设计通用

| English | 中文 | 用法或区分 |
| --- | --- | --- |
| high-level design | 总体设计 | 描述主要组件及其交互。 |
| get buy-in | 达成共识 | 指与面试官确认设计方向。 |
| deep dive | 深入设计 | 展开关键组件和设计权衡。 |
| wrap up | 总结 | 用于设计过程的收尾步骤。 |
| reference materials | 参考资料 | 包括论文、文档及延伸阅读。 |
| back-of-the-envelope estimation | 粗略估算 | 估计容量、请求量和资源需求。 |
| scalability | 可扩展性 | 系统随负载增长而扩展的能力。 |
| availability | 可用性 | 服务能够正常提供功能的程度。 |
| reliability | 可靠性 | 系统在给定条件下持续正确工作的能力。 |
| durability | 持久性 | 数据在故障等情况下仍能保留的性质。 |
| persistence | 持久化 | 将数据保存到非易失性存储的机制或过程；与“持久性”区分。 |
| throughput | 吞吐量 | 单位时间完成的工作量。 |
| latency | 延迟 | 请求或操作从开始到完成的耗时。 |
| partition | 分区 | 指逻辑数据分区；partition key 为“分区键”。 |
| shard | 分片 | 指数据水平拆分后的分片；sharding 为“分片”，sharding key 为“分片键”。 |
| replication | 复制 | 创建并同步数据副本的机制或过程。 |
| replica | 副本 | 复制所得的数据实例；read replica 为“只读副本”。 |
| strong consistency | 强一致性 | 用于描述严格的一致性要求。 |
| eventual consistency | 最终一致性 | 更新传播后，各副本最终达到一致。 |
| fault tolerance | 容错 | 系统在部分组件故障时继续工作的能力。 |
| failover | 故障切换 | 故障后将职责切换到正常实例。 |
| consensus | 共识 | 分布式节点就某个值或操作顺序达成一致。 |
| idempotency | 幂等性 | 重复执行同一操作不产生额外效果；idempotency key 为“幂等键”。 |
| race condition | 竞态条件 | 结果取决于并发操作的执行时序。 |
| optimistic locking | 乐观锁 | 通常通过版本号等方式检测并发冲突。 |
| pessimistic locking | 悲观锁 | 先获取锁，再执行受保护的操作。 |
| relational database | 关系型数据库 | 本译文集统一采用此译法。 |
| key-value store | 键值存储 | 通过键保存和获取对应值。 |
| identifier | 标识符 | 术语可以翻译，具体标识符本身保持原样。 |
| transaction | 事务；交易 | 数据库语境用“事务”，支付和证券等业务语境按含义用“交易”。 |
| distributed transaction | 分布式事务 | 跨多个节点或服务的事务。 |
| snapshot | 快照 | 保存某一时刻的状态，用于恢复或重建。 |

## 地理空间与附近朋友

| English | 中文 | 用法或区分 |
| --- | --- | --- |
| proximity service | 邻近服务 | 根据位置和距离查找附近的地点或商家。 |
| nearby friends | 附近的朋友 | 展示活跃朋友的位置及距离的功能。 |
| geospatial | 地理空间 | 用于地理位置、区域及空间关系的描述。 |
| geospatial index | 地理空间索引 | 加速区域内、邻近位置等空间查询的索引。 |
| Geohash | 地理哈希 | 将地理位置或网格编码为字符串；区别于将地址转为坐标的地理编码。 |
| quadtree | 四叉树 | 递归地将二维空间划分为四个子区域的树结构。 |
| Google S2 | Google S2 几何库 | 基于球面单元组织地理区域的几何库，产品名保留。 |
| Hilbert curve | 希尔伯特曲线 | 将多维空间映射到一维次序的一种空间填充曲线。 |
| geofence | 地理围栏 | 为现实地理区域定义的虚拟边界。 |
| point of interest (POI) | 兴趣点 | 地图上的餐厅、加油站等可识别地点。 |
| location-based service (LBS) | 基于位置的服务 | 利用用户或对象的地理位置提供功能。 |
| persistent connection | 长连接（持久连接） | 建立后持续保持、用于多次通信的连接。 |
| connection draining | 连接排空 | 停止接收新连接，并等待现有连接结束，便于移除或更新服务器。 |
| WebSocket | WebSocket | 支持客户端与服务器双向通信的协议，名称保持原样。 |
| publish/subscribe (Pub/Sub) | 发布/订阅 | 发布者向频道或主题发送消息，由订阅者接收。 |
| channel | 频道 | Redis Pub/Sub 中发布和订阅消息的命名目标。 |
| time to live (TTL) | 生存时间 | 条目在过期前允许保留的时长。 |
| service discovery | 服务发现 | 维护服务实例信息，使客户端能够发现相应实例。 |
| consistent hashing | 一致性哈希 | 节点变动时尽量减少键重新映射的一种哈希分配方式。 |

## 地图与导航

| English | 中文 | 用法或区分 |
| --- | --- | --- |
| map tile | 地图瓦片 | 用于显示某个地理范围的地图数据块，可采用图像或矢量形式。 |
| routing tile | 路由瓦片 | 保存某个地理范围内的道路图及跨瓦片连接，供寻路使用；区别于显示地图的瓦片。 |
| map projection | 地图投影 | 将地球表面位置映射到二维平面的方式。 |
| geocoding | 地理编码 | 将地址转换为经纬度等地理坐标。 |
| reverse geocoding | 逆地理编码 | 将地理坐标转换为人类可读的地址。 |
| estimated time of arrival (ETA) | 预计到达时间 | 根据路线、当前交通及历史信息等估计到达时间。 |
| road segment | 道路路段 | 路网中交叉路口等节点之间的一段道路。 |
| intersection | 交叉路口 | 道路相交的位置，通常表示为道路图的节点。 |
| route planning | 路线规划 | 根据起终点、候选路径和用户偏好生成路线。 |
| rerouting | 路线重规划 | 因交通或位置等变化重新选择导航路线。 |
| shortest path | 最短路径 | 按给定边权代价计算得到的最优路径。 |
| adjacency list | 邻接表 | 为每个图节点记录相邻节点或边的数据结构。 |
| zoom level | 缩放级别 | 控制地图显示范围及细节层次。 |
| vector tile | 矢量瓦片 | 以路径、多边形等矢量信息保存的地图瓦片。 |
| raster image | 栅格图像 | 由像素组成的图像；与矢量图像区分。 |
| map rendering | 地图渲染 | 将地图数据绘制为用户可见的图像。 |

## 流处理与消息

| English | 中文 | 用法或区分 |
| --- | --- | --- |
| stream processing | 流处理 | 持续处理到达的数据流。 |
| batch processing | 批处理 | 对一批数据集中执行处理。 |
| aggregation | 聚合 | 对事件或记录执行计数、求和等汇总。 |
| event time | 事件时间 | 事件实际发生的时间。 |
| processing time | 处理时间 | 系统处理事件的时间。 |
| watermark | 水位线 | 用于衡量事件时间的推进并处理迟到事件。 |
| tumbling window | 滚动窗口 | 大小固定且相互不重叠的窗口。 |
| sliding window | 滑动窗口 | 按指定步长滑动、可以重叠的窗口。 |
| deduplication | 去重 | 识别并消除重复记录或事件。 |
| at-most-once | 至多一次 | 每项操作或消息最多生效、处理或交付一次，具体对象依上下文确定。 |
| at-least-once | 至少一次 | 允许重复，以保证至少完成一次。 |
| exactly-once | 恰好一次 | 需结合上下文区分恰好一次交付、处理及端到端效果。 |
| message queue | 消息队列 | 解耦生产者与消费者。 |
| offset | 偏移量 | 例如 Kafka 中的消费位置。 |
| replay | 重放 | 按已保存的事件重新执行或重建状态。 |
| dead letter queue | 死信队列 | 保存无法按正常流程处理的消息。 |
| delivery guarantee | 交付保证 | 与消息交付有关；不直接等同于业务处理保证。 |
| topic | 主题 | 对消息进行逻辑分类、供生产者发布和消费者订阅的对象。 |
| message broker | 消息代理（Broker） | 接收、存储或转发消息的服务端组件；此处 broker 不译为证券业务中的“经纪商”。 |
| producer | 生产者 | 向消息系统发送消息的客户端或服务。 |
| consumer | 消费者 | 从消息系统接收并处理消息的客户端或服务。 |
| consumer group | 消费者组 | 协同消费主题分区的一组消费者。 |
| consumer coordinator | 消费者协调器 | 管理组成员、分区分配及相关状态的组件。 |
| consumer rebalancing | 消费者再均衡 | 组成员或分区变化时重新分配消费职责，简称“再均衡”。 |
| heartbeat | 心跳 | 周期性报告活跃状态的消息。 |
| append-only log | 仅追加日志 | 新记录写入末尾，不就地修改已有记录的日志。 |
| write-ahead log (WAL) | 预写日志 | 先记录日志，再执行对应数据更新的日志机制。 |
| log segment | 日志段 | 大日志拆分得到的段文件；活动段接收新消息追加。 |
| zero-copy | 零拷贝 | 减少数据传输过程中的复制，尤其是内核空间与用户空间之间的复制。 |
| delivery semantics | 交付语义 | 描述消息交付次数等行为；至多一次、至少一次和恰好一次见既有条目。 |
| in-sync replicas (ISR) | 同步副本 | 与领导者保持在配置允许滞后范围内的副本集合。 |
| acknowledgment (ACK) | 确认 | 表示接收或处理进度的响应；`ACK=all`、`ACK=1`、`ACK=0` 等配置标识原样保留。 |
| high watermark (HWM) | 高水位线 | 日志已提交、可向消费者公开的进度边界；区别于流处理中的事件时间水位线。 |
| retention period | 保留期限 | 消息或日志允许保存的时长，不以某个消费者是否已读取为唯一依据。 |
| pull model | 拉取模型 | 由消费者主动请求消息。 |
| push model | 推送模型 | 由服务端主动向消费者发送消息。 |
| delayed message | 延迟消息 | 在指定延迟时长过后再交付的消息。 |
| scheduled message | 定时消息 | 在预定时间交付的消息。 |

## 对象存储

| English | 中文 | 用法或区分 |
| --- | --- | --- |
| object storage | 对象存储 | 以对象及其元数据组织数据。 |
| block storage | 块存储 | 向上层呈现可访问的数据块。 |
| file storage | 文件存储 | 以文件和目录组织数据。 |
| bucket | 存储桶 | 对象的逻辑容器。 |
| metadata | 元数据 | 描述对象或其他数据的信息。 |
| payload | 负载 | 请求、消息或对象所承载的实际数据。 |
| versioning | 版本控制 | 在同一存储桶中保留对象的多个版本。 |
| placement service | 放置服务 | 决定对象及副本存储到哪些节点。 |
| failure domain | 故障域 | 可能被同一次故障共同影响的物理或逻辑范围。 |
| erasure coding | 纠删码 | 通过数据块和校验块提供冗余及恢复能力。 |
| checksum | 校验和 | 用于检测数据是否发生变化或损坏。 |
| multipart upload | 分段上传 | 将大对象分为多个分段上传。 |
| garbage collection | 垃圾回收 | 回收不再使用的存储空间。 |
| compaction | 压实 | 整理有效数据并回收无效数据占用的空间。 |

## 电子邮件

| English | 中文 | 用法或区分 |
| --- | --- | --- |
| Simple Mail Transfer Protocol (SMTP) | 简单邮件传输协议 | 主要用于发送及转发邮件，协议缩写保留。 |
| Post Office Protocol version 3 (POP3) | 邮局协议第 3 版 | 用于客户端获取邮件。 |
| Internet Message Access Protocol (IMAP) | 互联网邮件访问协议 | 用于访问和管理服务器上的邮件。 |
| Multipurpose Internet Mail Extensions (MIME) | 多用途互联网邮件扩展 | 支持邮件中的多种内容类型及附件。 |
| mail exchange record (MX record) | 邮件交换记录 | DNS 中指向邮件接收服务器的记录。 |
| attachment | 附件 | 随邮件发送的文件或其他内容。 |
| inbox | 收件箱 | 接收邮件的文件夹。 |
| email deliverability | 邮件送达率 | 关注邮件进入收件人收件箱的能力。 |
| spam | 垃圾邮件 | 不需要或未经请求而发送的邮件。 |
| inverted index | 倒排索引 | 从词项查找包含该词项的文档。 |

## 游戏排行榜

| English | 中文 | 用法或区分 |
| --- | --- | --- |
| leaderboard | 排行榜 | 展示玩家积分及排名。 |
| score | 积分 | 本游戏场景用“积分”；统计或算法语境也可用“分数”。 |
| rank | 排名；名次 | 应区分展示名次与数据结构返回的从 0 开始的位置。 |
| sorted set | 有序集合 | Redis 数据类型，成员唯一且关联分数。 |
| skip list | 跳表 | 利用多级索引加速查找的有序链表结构。 |
| hash slot | 哈希槽 | Redis Cluster 中用于分配键的槽。 |
| scatter-gather | 分散—汇集 | 向多个分区发起查询，再汇总各自结果。 |
| global secondary index | 全局二级索引 | DynamoDB 中采用不同键组织数据的索引。 |

## 支付与数字钱包

| English | 中文 | 用法或区分 |
| --- | --- | --- |
| pay-in | 入款 | 平台代表卖方向买家收款的流程。 |
| pay-out | 出款 | 平台向卖家支付资金的流程。 |
| payment service provider (PSP) | 支付服务提供商 | 例如承担外部支付处理的第三方服务。 |
| payment order | 支付订单 | 一项具体支付操作对应的订单。 |
| ledger | 账本 | 记录资金变动的会计记录体系。 |
| double-entry bookkeeping | 复式记账 | 每笔会计事项同时记入相应借方和贷方。 |
| clearing | 清算 | 核对交易并确定各方应收、应付义务的过程；与结算区分。 |
| settlement | 结算 | 履行资金或证券交付义务的过程。 |
| reconciliation | 对账 | 比较不同来源的记录，查找和处理差异；与清算、结算区分。 |
| debit | 借记 | 会计方向，不应脱离账户类型直接解释为余额减少。 |
| credit | 贷记 | 会计方向，不应脱离账户类型直接解释为余额增加。 |
| digital wallet | 数字钱包 | 管理账户余额并支持资金转账的服务。 |
| two-phase commit (2PC) | 两阶段提交 | 包含准备阶段及提交或中止阶段。 |
| Try-Confirm/Cancel (TC/C) | 尝试—确认/取消 | 依业务逻辑实现的分布式事务方式；保留原文章节使用的缩写。 |
| Saga transaction | Saga 事务 | 由本地事务和相应补偿操作组成。 |
| event sourcing | 事件溯源 | 以事件序列作为重建状态的依据。 |
| state machine | 状态机 | 按输入及规则执行状态转换。 |
| Command Query Responsibility Segregation (CQRS) | 命令查询职责分离 | 将状态修改与查询职责分开。 |
| reproducibility | 可重现性 | 使用相同事件及规则重现历史状态的能力。 |

## 证券交易所

| English | 中文 | 用法或区分 |
| --- | --- | --- |
| stock exchange | 证券交易所 | 本章指提供证券交易和撮合的场所及系统。 |
| broker | 经纪商 | 代表客户向交易所提交订单。 |
| market maker | 做市商 | 持续提供买卖报价的市场参与者。 |
| order book | 订单簿 | 记录尚未成交的买卖订单。 |
| matching engine | 撮合引擎 | 根据规则匹配买单和卖单。 |
| sequencer | 序列器 | 为输入或输出分配有序的序列 ID。 |
| execution report | 成交回报 | 向相关方报告订单执行结果。 |
| market data | 行情数据 | 包括订单簿、成交和价格等市场信息。 |
| market data publisher (MDP) | 行情数据发布器 | 向订阅方发布行情数据。 |
| limit order | 限价单 | 以指定价格或更优价格执行的订单。 |
| market order | 市价单 | 按当前可获得的市场价格执行的订单。 |
| bid-ask spread | 买卖价差 | 最优卖价与最优买价之间的差。 |
| candlestick chart | K 线图 | 按时间区间展示开盘、最高、最低和收盘价格。 |
| colocation | 主机托管 | 将交易系统服务器部署在交易所提供的托管环境中。 |
