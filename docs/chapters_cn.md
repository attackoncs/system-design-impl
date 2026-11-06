# 《System Design Interview》章节中文对照

来源：根目录 [`System Design Interview.md`](../System%20Design%20Interview.md)，共 31 个章节（00—30）。
全书可分三段：**面试基础（02—04）**、**第一卷经典系统设计（05—15）**、**第二卷真实系统设计（16—29）**，另加前言、社区与结语。

导航核对日期：2026-11-06。下表概括原书讨论范围；各子项目实现范围及生产缺口见[27项目复审覆盖清单](review-coverage_cn.md)，从架构进入调用链与实验见[案例学习导航](learning-guide_cn.md)，后续章节译文见[中文版目录](translations/system-design-interview/README.md)。

## 速览表

| 章 | 中文标题 | 原书标题 | 章节内容 |
| --- | --- | --- | --- |
| 00 | 前言 | Foreword | 说明写作初衷、准备方式与全书结构划分。 |
| 01 | 加入社区 | Join the Community | 介绍读者社区、交流与提问渠道。 |
| 02 | 从零扩展到百万用户 | Scale From Zero To Millions Of Users | 单服务器 → 数据库拆分 → 垂直/水平扩展 → 负载均衡 → 数据库复制 → 缓存 → CDN → 无状态 Web 层 → 多数据中心 → 消息队列 → 日志/监控/自动化 → 数据库分片。 |
| 03 | 粗略估算 | Back-of-the-envelope Estimation | 2 的幂、程序员必备延迟数字、可用性「几个 9」、以 Twitter 为例估算 QPS 与存储、答题技巧。 |
| 04 | 系统设计面试框架 | A Framework For System Design Interviews | 四步法：理解问题并划定范围 → 提出高层设计争取认可 → 深入设计 → 收尾总结，并给出各步时间分配。 |
| 05 | 设计限流器 | Design A Rate Limiter | 限流器放置位置、五种算法（令牌桶、漏桶、固定窗口计数器、滑动窗口日志、滑动窗口计数器）、高层架构、限流规则、分布式限流、性能优化与监控。 |
| 06 | 设计一致性哈希 | Design Consistent Hashing | 重哈希问题、哈希环、服务器与键的映射、增删节点、虚拟节点解决倾斜、受影响键的迁移。 |
| 07 | 设计键值存储 | Design A Key-value Store | CAP 定理、核心组件、数据分区、复制、一致性模型、版本化解决冲突、故障处理、读写路径。 |
| 08 | 设计分布式唯一 ID 生成器 | Design A Unique ID Generator In Distributed Systems | 多主复制、UUID、Ticket Server、Twitter Snowflake，以及时钟同步与深入设计。 |
| 09 | 设计短链服务 | Design A URL Shortener | API 设计、301/302 重定向、哈希 vs Base62、哈希冲突处理、数据模型与深入设计。 |
| 10 | 设计网络爬虫 | Design A Web Crawler | 爬虫算法、URL 前沿、HTML 下载与解析、robots.txt、爬虫陷阱、分布式爬取、去重与检查点。 |
| 11 | 设计通知系统 | Design A Notification System | 通知类型与渠道、APNs/FCM/SMS/Email 集成、可靠性（重试、幂等、去重）、限流、事件追踪。 |
| 12 | 设计新闻推送系统 | Design A News Feed System | 发布与拉取两条流程、Feed 缓存与 CDN、写扩散 vs 读扩散、分页与规模扩展。 |
| 13 | 设计聊天系统 | Design A Chat System | WebSocket 协议选型、消息模型、会话服务、消息同步、在线状态、多设备、端到端加密。 |
| 14 | 设计搜索自动补全 | Design A Search Autocomplete System | 前缀树、数据收集管道、查询服务（缓存与分片）、实时更新、限流与扩展。 |
| 15 | 设计 YouTube | Design YouTube | 视频上传与流式播放、转码 DAG、CDN 分发与分片、元数据存储、成本估算与可靠性。 |
| 16 | 设计 Google Drive | Design Google Drive | 上传/下载、跨设备同步（增量变更、冲突解决）、通知机制、块存储与元数据数据库、扩展与故障处理。 |
| 17 | 附近服务 | Proximity Service | 地理索引方案（Geohash、四叉树、Redis GEO）、按半径查询附近商户、地理精度、扩展与可用性。 |
| 18 | 附近好友 | Nearby Friends | 位置上报、WebSocket 推送、Redis 位置缓存、地理哈希、好友扇出广播、隐私与规模。 |
| 19 | 设计谷歌地图 | Google Maps | 地图瓦片与瓦片 CDN、路线规划算法（Dijkstra/A*）、ETA 预测、数据管道与可扩展性。 |
| 20 | 分布式消息队列 | Distributed Message Queue | 消息模型、分区与消费组、投递语义、可靠性、重平衡、WAL 存储、延迟消息。 |
| 21 | 指标监控与告警 | Metrics Monitoring and Alerting System | 指标采集与聚合、时序数据库、告警规则与去噪、可视化、横向扩展。 |
| 22 | 广告点击事件聚合 | Ad Click Event Aggregation | 实时聚合架构、批处理 vs 流处理、点击去重、数据准确性、延迟与规模权衡。 |
| 23 | 酒店预订系统 | Hotel Reservation System | 库存与预订流程、并发下单防超卖、事务与分布式锁、Saga 补偿、可靠性与一致性。 |
| 24 | 分布式邮件服务 | Distributed Email Service | SMTP/IMAP 协议、邮件存储与搜索、附件处理、投递队列、垃圾邮件过滤、扩展。 |
| 25 | 类 S3 对象存储 | S3-like Object Storage | 桶与对象语义、元数据服务、数据块存储、纠删码与复制、一致性、版本化、垃圾回收。 |
| 26 | 实时游戏排行榜 | Real-time Gaming Leaderboard | Redis 有序集合做排名、实时更新、分片与聚合、排名查询性能与一致性权衡。 |
| 27 | 支付系统 | Payment System | 幂等与重试、支付流程、对账、账本设计、防重复扣款、可观测性与容错。 |
| 28 | 数字钱包 | Digital Wallet | 钱包间余额转账、账户分片与分布式事务、事件溯源、CQRS、快照、共识复制与可重放审计。 |
| 29 | 设计证券交易所 | Stock Exchange | 撮合引擎与订单簿、低延迟架构、行情分发、风控与高可用。 |
| 30 | 学习仍在继续 | The Learning Continues | 结语与后续学习资料，不是独立系统实现。 |

## 章节与参考实现对照

| 章 | 中文标题 | 子项目目录 |
| --- | --- | --- |
| 02 | 从零扩展到百万用户 | [scale-from-zero](../scale-from-zero/README.md) |
| 03 | 粗略估算 | [capacity-estimation](../capacity-estimation/README.md) |
| 05 | 设计限流器 | [rate-limiter](../rate-limiter/README.md) |
| 06 | 设计一致性哈希 | [consistent-hashing](../consistent-hashing/README.md) |
| 07 | 设计键值存储 | [key-value-store](../key-value-store/README.md) |
| 08 | 设计分布式唯一 ID 生成器 | [unique-id-generator](../unique-id-generator/README.md) |
| 09 | 设计短链服务 | [url-shortener](../url-shortener/README.md) |
| 10 | 设计网络爬虫 | [web-crawler](../web-crawler/README.md) |
| 11 | 设计通知系统 | [notification-system](../notification-system/README.md) |
| 12 | 设计新闻推送系统 | [news-feed-system](../news-feed-system/README.md) |
| 13 | 设计聊天系统 | [chat-system](../chat-system/README.md) |
| 14 | 设计搜索自动补全 | [search-autocomplete-system](../search-autocomplete-system/README.md) |
| 15 | 设计 YouTube | [video-streaming-system](../video-streaming-system/README.md) |
| 16 | 设计 Google Drive | [file-sync-system](../file-sync-system/README.md) |
| 17 | 附近服务 | [proximity-service](../proximity-service/README.md) |
| 18 | 附近好友 | [nearby-friends-system](../nearby-friends-system/README.md) |
| 19 | 设计谷歌地图 | [maps-system](../maps-system/README.md) |
| 20 | 分布式消息队列 | [distributed-message-queue](../distributed-message-queue/README.md) |
| 21 | 指标监控与告警 | [metrics-monitoring-system](../metrics-monitoring-system/README.md) |
| 22 | 广告点击事件聚合 | [ad-click-aggregation-system](../ad-click-aggregation-system/README.md) |
| 23 | 酒店预订系统 | [hotel-reservation-system](../hotel-reservation-system/README.md) |
| 24 | 分布式邮件服务 | [distributed-email-service](../distributed-email-service/README.md) |
| 25 | 类 S3 对象存储 | [object-storage-system](../object-storage-system/README.md) |
| 26 | 实时游戏排行榜 | [gaming-leaderboard-system](../gaming-leaderboard-system/README.md) |
| 27 | 支付系统 | [payment-system](../payment-system/README.md) |
| 28 | 数字钱包 | [digital-wallet-system](../digital-wallet-system/README.md) |
| 29 | 设计证券交易所 | [stock-exchange-system](../stock-exchange-system/README.md) |

第 00、01、04、30 章为导读、社区与学习资料，无独立实现。其余 27 个子项目均有复审记录，逐章证据与保留边界见[复审覆盖清单](review-coverage_cn.md)；第 15—30 章的任务与学习路线见[后续章节任务路线图](chapter-roadmap.md)。
