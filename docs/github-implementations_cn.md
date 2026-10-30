# 27个子项目的GitHub编码实现对照

检索日期：**2026-10-30**。本表按本仓库的27个子项目分别检索，记录包含实际代码的同类业务项目、算法库、核心组件和教学案例。仓库链接同时是对应范围说明的来源。

这些项目不一定按照《System Design Interview》的章节逐项复刻。“找到同类实现”也不代表整章需求已全部覆盖。本次核查依据GitHub仓库页面、README和可见代码目录，未逐一部署、运行测试或审计实现质量。维护状态记录的是检索时的情况。

## 按章节查找

左侧章节链接进入本仓库的案例导读，GitHub链接进入外部实现。建议先用本地导读建立问题和数据流，再选择外部仓库的相同操作进行比较。

| 章节与本地导读 | GitHub编码实现 | 主要语言 | 对应范围 | 适合对照阅读的内容与边界 |
|---|---|---|---|---|
| [02 从零扩容](../scale-from-zero/docs/learning-guide_cn.md) | [haproxy/haproxy](https://github.com/haproxy/haproxy) | C | 核心组件 | 负载均衡、健康检查、连接管理；不能代替从单机到缓存、复制及分片的整章演进案例。 |
| [03 容量估算](../capacity-estimation/docs/learning-guide_cn.md) | [sirupsen/napkin-math](https://github.com/sirupsen/napkin-math) | Rust、Go等 | 估算辅助与测量代码 | 性能测量代码、数量级和估算依据；不是与本地项目相同的QPS／存储预算计算器。 |
| [05 限流器](../rate-limiter/docs/learning-guide_cn.md) | [uber-go/ratelimit](https://github.com/uber-go/ratelimit) | Go | 算法库 | 漏桶、时间计算、原子操作／互斥锁和基准测试；不是包含所有限流算法的完整网关。 |
| [06 一致性哈希](../consistent-hashing/docs/learning-guide_cn.md) | [buraksezer/consistent](https://github.com/buraksezer/consistent) | Go | 算法库 | 带负载约束的一致性哈希，可比较分区、节点变化和负载分配；算法和路由语义不一定与本地哈希环相同。 |
| [07 键值存储](../key-value-store/docs/learning-guide_cn.md) | [voldemort/voldemort](https://github.com/voldemort/voldemort) | Java | 分布式KV系统 | Dynamo风格的分区、副本及故障处理。README说明项目已停止开发，适合作为历史实现阅读。 |
| [08 唯一ID](../unique-id-generator/docs/learning-guide_cn.md) | [bwmarrin/snowflake](https://github.com/bwmarrin/snowflake) | Go | 算法库 | Snowflake生成与解析，可直接对照位布局、序列号及时间处理。 |
| [09 短链接](../url-shortener/docs/learning-guide_cn.md) | [shlinkio/shlink](https://github.com/shlinkio/shlink) | PHP | 业务服务 | 短链接创建、管理、重定向和访问统计；功能范围比本地参考实现更大。 |
| [10 网页爬虫](../web-crawler/docs/learning-guide_cn.md) | [apache/nutch](https://github.com/apache/nutch) | Java | 爬虫系统 | 抓取调度、解析、插件和抓取数据库；可从一次URL抓取流程切入。 |
| [11 通知系统](../notification-system/docs/learning-guide_cn.md) | [novuhq/novu](https://github.com/novuhq/novu) | TypeScript | 通知基础设施 | 工作流、通道集成及通知管理；比较时区分通知受理、后台执行和供应商返回。 |
| [12 新闻动态](../news-feed-system/docs/learning-guide_cn.md) | [tschellenbach/Stream-Framework](https://github.com/tschellenbach/Stream-Framework) | Python | Feed框架 | 新闻动态、活动流、异步fanout及Redis／Cassandra存储，与章节主题较接近。 |
| [13 聊天系统](../chat-system/docs/learning-guide_cn.md) | [tinode/chat](https://github.com/tinode/chat) | Go | 聊天平台 | 服务端及配套客户端、WebSocket／gRPC、会话、频道和消息协议；协议与本地实现不同。 |
| [14 自动补全](../search-autocomplete-system/docs/learning-guide_cn.md) | [valeriansaliou/sonic](https://github.com/valeriansaliou/sonic) | Rust | 搜索后端 | 包含查询自动补全能力；主要对应查询索引，不等于整章热门词采集、聚合和快照流水线。 |
| [15 视频流媒体](../video-streaming-system/docs/learning-guide_cn.md) | [Chocobozzz/PeerTube](https://github.com/Chocobozzz/PeerTube) | TypeScript等 | 视频平台 | 视频发布与播放链路；还包含联邦和P2P等额外设计，不应直接视为章节架构的逐项复刻。 |
| [16 文件同步](../file-sync-system/docs/learning-guide_cn.md) | [syncthing/syncthing](https://github.com/syncthing/syncthing) | Go | 文件同步系统 | 数据块、版本、冲突及设备同步；采用设备间同步架构，与中心式网盘存在区别。 |
| [17 附近商家](../proximity-service/docs/learning-guide_cn.md) | [tidwall/tile38](https://github.com/tidwall/tile38) | Go | 地理空间核心组件 | 附近搜索、空间索引及地理围栏；不包含完整商家管理业务。 |
| [18 附近好友](../nearby-friends-system/docs/learning-guide_cn.md) | [Majdabbassi/FriendMap](https://github.com/Majdabbassi/FriendMap) | TypeScript、Vue | 业务项目 | 好友实时位置共享、共享权限和在线状态，使用PostgreSQL、Redis及Socket.IO；部署和规模能力需另行验证。 |
| [19 地图系统](../maps-system/docs/learning-guide_cn.md) | [Project-OSRM/osrm-backend](https://github.com/Project-OSRM/osrm-backend) | C++ | 导航核心 | 路线规划、距离矩阵和GPS道路匹配；不是完整Google Maps类产品。 |
| [20 消息队列](../distributed-message-queue/docs/learning-guide_cn.md) | [apache/kafka](https://github.com/apache/kafka) | Java、Scala | 分布式事件流系统 | 分区日志、副本、生产者与消费组；建议每次只追踪一种操作。 |
| [21 指标监控与告警](../metrics-monitoring-system/docs/learning-guide_cn.md) | [prometheus/prometheus](https://github.com/prometheus/prometheus)、[prometheus/alertmanager](https://github.com/prometheus/alertmanager) | Go | 相互协作的系统 | Prometheus负责指标采集、存储和规则；Alertmanager负责告警去重、分组、路由、静默及抑制。 |
| [22 广告点击聚合](../ad-click-aggregation-system/docs/learning-guide_cn.md) | [susmitpy/stream_analytics_adtech_ctr](https://github.com/susmitpy/stream_analytics_adtech_ctr) | Go、Python／PyFlink | 教学案例 | 模拟曝光和点击，经Kafka、Flink做时间关联、窗口聚合和CTR计算；不是完整广告计费系统。 |
| [23 酒店预订](../hotel-reservation-system/docs/learning-guide_cn.md) | [DeathStarBench/hotelReservation](https://github.com/delimitrou/DeathStarBench/tree/master/hotelReservation) | Go | 微服务基准应用 | 酒店搜索、报价、推荐和预订；支付补偿等能力需另行核对，不能仅凭项目名称认定覆盖。 |
| [24 分布式邮件](../distributed-email-service/docs/learning-guide_cn.md) | [stalwartlabs/stalwart](https://github.com/stalwartlabs/stalwart) | Rust | 邮件服务器 | 邮件协议、存储和投递；可从一封邮件的受理、保存与发送流程切入。 |
| [25 对象存储](../object-storage-system/docs/learning-guide_cn.md) | [minio/minio](https://github.com/minio/minio) | Go | S3兼容对象存储 | 对象API及存储组织。检索时仓库页面标为归档、不再维护，可作为源码学习材料。 |
| [26 游戏排行榜](../gaming-leaderboard-system/docs/learning-guide_cn.md) | [agoragames/leaderboard-python](https://github.com/agoragames/leaderboard-python) | Python | Redis排行榜库 | 积分、排名及邻居查询；不包含完整比赛事实日志和投影恢复流水线。 |
| [27 支付系统](../payment-system/docs/learning-guide_cn.md) | [juspay/hyperswitch](https://github.com/juspay/hyperswitch) | Rust | 支付编排系统 | 支付连接器、渠道路由和操作流程；覆盖侧重支付编排，不能据此认定具有与本地相同的账本模型。 |
| [28 数字钱包](../digital-wallet-system/docs/learning-guide_cn.md) | [formancehq/ledger](https://github.com/formancehq/ledger) | Go | 账本核心 | 可编程复式记账、余额和资金流转；不是完整钱包产品，也不等同于章节的跨分片协调实现。 |
| [29 证券交易](../stock-exchange-system/docs/learning-guide_cn.md) | [exchange-core/exchange-core](https://github.com/exchange-core/exchange-core) | Java | 交易核心 | 撮合、风控与记账、日志、快照及API；不包含完整证券交易所的所有外围系统。 |

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
