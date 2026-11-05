# 27个子项目的GitHub编码实现对照

检索日期：**2026-10-30**。本表按本仓库的27个子项目分别检索，记录包含实际代码的同类业务项目、算法库、核心组件和教学案例。仓库链接同时是对应范围说明的来源。

这些项目不一定按照《System Design Interview》的章节逐项复刻。“找到同类实现”也不代表整章需求已全部覆盖。本次核查依据GitHub仓库页面、README和可见代码目录，未逐一部署、运行测试或审计实现质量。维护状态记录的是检索时的情况。

## 按章节查找

左侧章节链接进入本仓库的案例导读，GitHub链接进入外部实现。建议先用本地导读建立问题和数据流，再选择外部仓库的相同操作进行比较。

| 章节与本地导读 | GitHub编码实现 | 主要语言 | 对应范围 | 适合对照阅读的内容与边界 |
|---|---|---|---|---|
| [02 从零扩容](../scale-from-zero/docs/learning-guide_cn.md) | [haproxy/haproxy](https://github.com/haproxy/haproxy) | C | 核心组件 | 负载均衡、健康检查、连接管理；不能代替从单机到缓存、复制及分片的整章演进案例。 |
| [03 容量估算](../capacity-estimation/docs/learning-guide_cn.md) | [sirupsen/napkin-math](https://github.com/sirupsen/napkin-math) | Rust、Go等 | 估算辅助与测量代码 | 性能测量代码、数量级和估算依据；不是与本地项目相同的QPS／存储预算计算器。 |
| [05 限流器](../rate-limiter/docs/learning-guide_cn.md) | [uber-go/ratelimit](https://github.com/uber-go/ratelimit)、[envoyproxy/ratelimit](https://github.com/envoyproxy/ratelimit)、[go-redis/redis_rate](https://github.com/go-redis/redis_rate) | Go | 算法库／限流服务 | 分别对照等待式漏桶、规则与共享Redis、GCRA及Lua脚本复用；2026-10-31补充[源码对照与本地覆盖](../rate-limiter/docs/open-source-review_cn.md)，不能将任一仓库等同于本章所有能力。 |
| [06 一致性哈希](../consistent-hashing/docs/learning-guide_cn.md) | [ultrabug/uhashring](https://github.com/ultrabug/uhashring)、[stathat/consistent](https://github.com/stathat/consistent)、[buraksezer/consistent](https://github.com/buraksezer/consistent) | Python、Go | 算法库 | 对照虚拟节点、权重、严格顺时针边界、并发和碰撞；固定分区加负载约束属于不同路由方案。2026-10-31补充[源码对照与章节覆盖](../consistent-hashing/docs/open-source-review_cn.md)。 |
| [07 键值存储](../key-value-store/docs/learning-guide_cn.md) | [voldemort/voldemort](https://github.com/voldemort/voldemort)；[google/leveldb](https://github.com/google/leveldb) | Java；C++ | 分布式KV；本地LSM引擎 | Voldemort对照副本与因果版本，README说明已停止开发；LevelDB参考日志同步失败后停止写入。[2026-10-31源码对照与本地修复](../key-value-store/docs/open-source-review_cn.md)。 |
| [08 唯一ID](../unique-id-generator/docs/learning-guide_cn.md) | [bwmarrin/snowflake](https://github.com/bwmarrin/snowflake)；[sony/sonyflake](https://github.com/sony/sonyflake) | Go | 算法库 | 对照掩码预计算、序列等待、时间位宽和机器身份条件；[2026-10-31源码对照、修复与实测](../unique-id-generator/docs/open-source-review_cn.md)。 |
| [09 短链接](../url-shortener/docs/learning-guide_cn.md) | [YOURLS/YOURLS](https://github.com/YOURLS/YOURLS)、[shlinkio/shlink](https://github.com/shlinkio/shlink) | PHP | 业务服务 | 已阅读YOURLS创建源码，参考ID编码、去重与写入约束；Shlink核对README，见[范围与本地验证](../url-shortener/docs/open-source-review_cn.md)。 |
| [10 网页爬虫](../web-crawler/docs/learning-guide_cn.md) | [scrapy/scrapy](https://github.com/scrapy/scrapy)、[apache/nutch](https://github.com/apache/nutch) | Python / Java | 爬虫框架/系统 | 本轮实读Scrapy调度、下载slot与robots等待隔离的[固定源码](../web-crawler/docs/open-source-review_cn.md)，修复本地连续取消的数据库排空；Nutch保留为此前项目入口，未冒称本轮实读。 |
| [11 通知系统](../notification-system/docs/learning-guide_cn.md) | [novuhq/novu](https://github.com/novuhq/novu)、[notifo-io/notifo](https://github.com/notifo-io/notifo) | TypeScript / C# | 通知基础设施 | 已读队列重试、幂等说明与调度通道源码；固定提交及本地取舍见[核对记录](../notification-system/docs/open-source-review_cn.md)。 |
| [12 新闻动态](../news-feed-system/docs/learning-guide_cn.md) | [tschellenbach/Stream-Framework](https://github.com/tschellenbach/Stream-Framework) | Python | Feed框架 | 已实读发布/扇出、RedisFeed与时间线窗口的[固定提交源码](../news-feed-system/docs/open-source-review_cn.md)；本地正文源与Feed ID分离，进程内outbox分发不等于持久worker确认。 |
| [13 聊天系统](../chat-system/docs/learning-guide_cn.md) | [tinode/chat](https://github.com/tinode/chat) | Go | 聊天平台 | 服务端及配套客户端、WebSocket／gRPC、会话、频道和消息协议；协议与本地实现不同。 |
| [14 自动补全](../search-autocomplete-system/docs/learning-guide_cn.md) | [valeriansaliou/sonic](https://github.com/valeriansaliou/sonic)、[RediSearch/RediSearch](https://github.com/RediSearch/RediSearch) | Rust / C | 搜索后端 | 已读建议执行器、Trie排名堆与子树上界；排序语义和本地取舍见[固定提交核对](../search-autocomplete-system/docs/open-source-review_cn.md)。 |
| [15 视频流媒体](../video-streaming-system/docs/learning-guide_cn.md) | [Chocobozzz/PeerTube](https://github.com/Chocobozzz/PeerTube)、[mediacms-io/mediacms](https://github.com/mediacms-io/mediacms) | TypeScript、Python等 | 视频平台 | 已阅读上传、HLS依赖、转码重试/取消源码；[固定版本与本地核对](../video-streaming-system/docs/open-source-review_cn.md)。联邦/P2P及逐块编码等设计与本项目不同。 |
| [16 文件同步](../file-sync-system/docs/learning-guide_cn.md) | [syncthing/syncthing](https://github.com/syncthing/syncthing)、[haiwen/seafile](https://github.com/haiwen/seafile) | Go、C等 | 文件同步系统/同步守护进程 | 已阅读块校验、晚编辑和冲突替换源码；[固定版本与本地核对](../file-sync-system/docs/open-source-review_cn.md)。设备间同步与中心式网盘架构有差异。 |
| [17 附近商家](../proximity-service/docs/learning-guide_cn.md) | [tidwall/tile38](https://github.com/tidwall/tile38)、[redis/redis](https://github.com/redis/redis) | Go、C | 地理空间核心组件 | 已阅读近邻索引、精度/邻格覆盖与距离过滤源码；[固定版本和本地核对](../proximity-service/docs/open-source-review_cn.md)。不包含本项目次日发布等完整业务，纬度与分页语义有差异。 |
| [18 附近好友](../nearby-friends-system/docs/learning-guide_cn.md) | [Majdabbassi/FriendMap](https://github.com/Majdabbassi/FriendMap)、[owntracks/recorder](https://github.com/owntracks/recorder)、[bilde2910/Hauk](https://github.com/bilde2910/Hauk) | TypeScript/Vue、C、PHP等 | 业务项目/位置组件 | 保留FriendMap原业务项目索引；本轮实际阅读后两者的实时分发、限时共享和恢复源码，见[固定版本与本地核对](../nearby-friends-system/docs/open-source-review_cn.md)。权限与传输模型不同，非整章架构复刻。 |
| [19 地图系统](../maps-system/docs/learning-guide_cn.md) | [valhalla/valhalla](https://github.com/valhalla/valhalla)、[Project-OSRM/osrm-backend](https://github.com/Project-OSRM/osrm-backend) | C++ | 导航核心 | 已读分层瓦片/GraphReader/MLD候选源码；[固定版本对照及本地边界](../maps-system/docs/open-source-review_cn.md)。 |
| [20 消息队列](../distributed-message-queue/docs/learning-guide_cn.md) | [apache/kafka](https://github.com/apache/kafka)、[apache/rocketmq](https://github.com/apache/rocketmq) | Java等 | 事件流/消息队列 | 已读批处理、重试身份、挂起拉取与标签过滤源码；[固定版本、本地修复及边界](../distributed-message-queue/docs/open-source-review_cn.md)。 |
| [21 指标监控与告警](../metrics-monitoring-system/docs/learning-guide_cn.md) | [prometheus/prometheus](https://github.com/prometheus/prometheus)、[prometheus/alertmanager](https://github.com/prometheus/alertmanager) | Go | 采集/规则及通知系统 | 已读抓取生命周期、规则状态、通知分组/去重/重试源码；[固定版本与本地修复](../metrics-monitoring-system/docs/open-source-review_cn.md)。 |
| [22 广告点击聚合](../ad-click-aggregation-system/docs/learning-guide_cn.md) | [susmitpy/stream_analytics_adtech_ctr](https://github.com/susmitpy/stream_analytics_adtech_ctr)、[apache/flink](https://github.com/apache/flink) | Python/PyFlink、Java等 | 教学案例/窗口运行时 | 已读时间关联、distinct、事件时间触发/清理源码；[固定版本及语义边界](../ad-click-aggregation-system/docs/open-source-review_cn.md)。示例inner join分母不是全部曝光。 |
| [23 酒店预订](../hotel-reservation-system/docs/learning-guide_cn.md) | [DeathStarBench/hotelReservation](https://github.com/delimitrou/DeathStarBench/tree/master/hotelReservation)、[Eventuate订单Saga](https://github.com/eventuate-tram/eventuate-tram-sagas-examples-customers-and-orders) | Go、Java | 业务基准/Saga示例 | 已读预订/缓存与补偿步骤源码；[固定版本及本地边界](../hotel-reservation-system/docs/open-source-review_cn.md)。逐晚写入不证明多夜原子性，额度预留不等于真实扣款。 |
| [24 分布式邮件](../distributed-email-service/docs/learning-guide_cn.md) | [stalwartlabs/stalwart](https://github.com/stalwartlabs/stalwart)、[foxcpp/maddy](https://github.com/foxcpp/maddy) | Rust、Go | 邮件服务器 | 已读Blob/持久队列提交、逐收件人重试及退避源码；[固定版本及本地核对](../distributed-email-service/docs/open-source-review_cn.md)。不能据此推断互联网恰好一次或跨存储原子性。 |
| [25 对象存储](../object-storage-system/docs/learning-guide_cn.md) | [minio/minio](https://github.com/minio/minio)、[seaweedfs/seaweedfs](https://github.com/seaweedfs/seaweedfs) | Go | 对象存储/存储核心 | 已读分片完成校验、最终发布及压缩切换/恢复源码；[固定版本与本地核对](../object-storage-system/docs/open-source-review_cn.md)。MinIO检索时已归档；压缩版本检查不等于本地GC代数栅栏。 |
| [26 游戏排行榜](../gaming-leaderboard-system/docs/learning-guide_cn.md) | [WB-Games/leaderboard-python](https://github.com/WB-Games/leaderboard-python)、[redis/redis](https://github.com/redis/redis) | Python、C | 排行榜库/排序核心 | 已读竞争/稠密排名、邻居窗口及Redis8.0.5范围计数源码；[固定版本与本地核对](../gaming-leaderboard-system/docs/open-source-review_cn.md)。旧agoragames库已迁移且检索时归档；单key查询不证明全局一致排名。 |
| [27 支付系统](../payment-system/docs/learning-guide_cn.md) | [juspay/hyperswitch](https://github.com/juspay/hyperswitch)、[tigerbeetle/tigerbeetle](https://github.com/tigerbeetle/tigerbeetle) | Rust、Zig | 支付编排/记账引擎 | 已读稳定请求引用、Webhook验签及转账身份/借贷更新源码；[固定版本与本地核对](../payment-system/docs/open-source-review_cn.md)。v1/v2路径及职责有差异，不证明外部支付与本地账本原子提交。 |
| [28 数字钱包](../digital-wallet-system/docs/learning-guide_cn.md) | [formancehq/ledger](https://github.com/formancehq/ledger)、[Apache Seata](https://github.com/apache/incubator-seata) | Go / Java | 账本核心 / TCC分支防护 | 已读固定源码：[记账、确定性事实与TCC防重/空回滚/悬挂对照](../digital-wallet-system/docs/open-source-review_cn.md)。不是完整钱包或本地Raft实现；[本轮审查](../digital-wallet-system/docs/chapter28-review.md)修复审计身份及累计余额边界。 |
| [29 证券交易](../stock-exchange-system/docs/learning-guide_cn.md) | [exchange-core/exchange-core](https://github.com/exchange-core/exchange-core)、[dharmeshsing/CoinTossX](https://github.com/dharmeshsing/CoinTossX) | Java | 撮合/交易核心 | 已读价格时间优先、撤单、日志/恢复的[固定源码及边界](../stock-exchange-system/docs/open-source-review_cn.md)；局部ID检查不等于请求幂等，上游时钟/持久架构不等于本地HA。 |
| [30 持续学习](chapter30-learning-continues_cn.md) | [System Design Primer](https://github.com/donnemartin/system-design-primer)、[LevelDB](https://github.com/google/leveldb)、[Memcached](https://github.com/memcached/memcached) | 资料 / C++ / C | 学习集合及基础组件 | 第30章无新增业务实现；[固定阅读入口及本地学习路线](chapter30-learning-continues_cn.md)，未运行这些上游项目。 |

## 适合先做源码对照的项目

| 学习目标 | 本地实现 → 外部实现 | 建议选择的同一操作 |
|---|---|---|
| 先建立直接对应 | 唯一ID → snowflake | 生成一个ID，再解析时间、节点和序列字段 |
| 理解时间与共享状态 | 限流器 → ratelimit | 连续请求达到限额，观察等待／拒绝和时间推进 |
| 优先阅读Python代码 | 新闻动态 → Stream Framework | 一位作者发布后，关注者如何得到一页Feed |
| 理解Redis有序数据 | 排行榜 → leaderboard-python | 更新积分，查询个人名次及前后邻居 |
| 深入版本和恢复 | 文件同步 → Syncthing | 两个设备并发修改同一个文件，追踪版本和冲突 |
| 深入日志与顺序 | 消息队列 → Kafka | 向分区发送消息，读取后提交消费位置 |
| 深入业务状态变化 | 证券交易 → exchange-core | 两张同价挂单被部分成交，检查顺序、余额与剩余数量 |

这些是学习切入点，不表示不同实现应产生相同协议、算法细节或部署行为。

## 怎样比较两份实现

1. **固定一个操作和一组输入。** 例如生成一个ID、读取一页Feed、重复提交同一请求，先写下预期结果。
2. **找到双方的业务入口。** 跟踪输入校验、核心服务、状态存储和返回结果，画出案例所需的调用路径。
3. **对照状态与提交点。** 明确什么是事实，什么是缓存／投影，以及幂等、事务、锁、快照或租约分别保护什么。
4. **增加一个失败条件。** 例如重复请求、回执丢失、进程重启或并发修改，比较双方的恢复承诺及测试。
5. **记录取舍，而非只比较代码量。** 本地项目为了独立运行采用的方案，可能与外部项目的产品目标和基础设施前提不同。

后续新增或替换仓库时，应重新核对README、源码目录、对应业务范围和维护状态，更新检索日期；不要把仓库自述的吞吐或“生产级”标签直接当成已验证结论。

[返回学习导航](learning-guide_cn.md) · [返回仓库中文总览](../README_CN.md)
