# System Design Interview - Python Implementations

<!-- LEARNING_GUIDE_START -->
## 从这里开始读代码

[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。
<!-- LEARNING_GUIDE_END -->

本仓库提供《System Design Interview》的27个Python参考子项目，包含设计文档、源代码、测试、使用示例及明确的部署边界。

## 已实现

[复审覆盖清单](docs/review-coverage_cn.md)汇总逐章证据、历史验证与本轮补查范围。

| 章节 | 主题 | 状态 | 说明 |
|------|------|------|------|
| 02 | [从零扩容](./scale-from-zero/) | ✅ 核心已复审 | HAProxy/Redis源码对照；资料提交与主库提升互斥、非法TTL保留旧缓存；77项测试；生产级多地域HA仍待实现 |
| 03 | [容量估算](./capacity-estimation/) | ✅ 核心已复审 | napkin-math源码对照；显式单位Decimal预算、精确节点取整与备用数量；16项测试；[本轮记录](./capacity-estimation/docs/chapter03-review.md) |
| 05 | [Rate Limiter](./rate-limiter/) | ✅ 核心复审完成 | 265项测试；文件规则/稳定身份/时钟水位/有界FIFO；真实4进程Redis及双HTTP验证 |
| 06 | [Consistent Hashing](./consistent-hashing/) | ✅ 核心复审完成 | 163项测试；GitHub源码对照、迁移局部性/统计属性、未变状态O(1)规划；四进程一致性验证 |
| 07 | [Key-Value Store](./key-value-store/) | 核心已实现，[开源复审428项测试](key-value-store/docs/open-source-review_cn.md)；自动供给/安全下线未完成 | Dynamo 风格、LSM-tree 存储、仲裁交集、Gossip 协议、gRPC |
| 08 | [Unique ID Generator](./unique-id-generator/) | 核心已实现，[开源复审122项测试](unique-id-generator/docs/open-source-review_cn.md)；身份/重启安全依赖部署 | Snowflake、位布局预计算、线程安全、精确UTC解析 |
| 09 | [URL Shortener](./url-shortener/) | ✅ 完成 | CRC32/Base62、SQLite事务发号、只读去重、LRU与中文网址重定向；[开源对照](./url-shortener/docs/open-source-review_cn.md) |
| 10 | [Web Crawler](./web-crawler/) | ✅ 核心已复审 | 固定Scrapy源码对照；连续取消时排空SQLite任务并释放文件锁；259项测试；[本轮记录](./web-crawler/docs/chapter10-review.md) |
| 11 | [Notification System](./notification-system/) | ✅ 核心已复审 | 466项测试；有界批量受理、领取凭据隔离、事务内租约计时与Worker复用；[开源对照](./notification-system/docs/open-source-review_cn.md) |
| 12 | [News Feed System](./news-feed-system/) | ✅ 核心已复审 | 固定Stream-Framework源码对照；混合推拉/outbox与有界组装；深层JSON在发布前返回400；311项测试；[本轮记录](./news-feed-system/docs/chapter12-review.md) |
| 13 | [Chat System](./chat-system/) | ✅ 核心已复审 | Tinode/Zulip源码对照；持久收件箱、多设备恢复与Redis隔离；修复WSL验证脚本及Sentinel就绪判断；主套202项＋独立切主1项通过；[本轮记录](./chat-system/docs/chapter13-review.md) |
| 14 | [Search Autocomplete](./search-autocomplete-system/) | ✅ 核心链路已核对 | 采样日志、不可变Top 5 Trie、有界副本切换与响应完整性；188项测试；[开源对照](./search-autocomplete-system/docs/open-source-review_cn.md) |
| 15 | [Video Streaming](./video-streaming-system/) | ✅ 核心链路已核对 | 分片组装校验与修复、有界租约DAG、真实HLS/PostgreSQL/S3验证；覆盖164项；[开源核对](./video-streaming-system/docs/open-source-review_cn.md) |
| 16 | [File Sync](./file-sync-system/) | ✅ 核心链路已核对 | 加密增量同步、冲突/权限/GC栅栏、响应完整性与游标恢复；122项测试；[开源核对](./file-sync-system/docs/open-source-review_cn.md) |
| 17 | [附近商家服务](./proximity-service/) | ✅ 核心链路已核对 | geohash与距离、每日CAS发布、有界缓存/HTTP及副本切换；76项测试；[Tile38/Redis源码核对](./proximity-service/docs/open-source-review_cn.md) |
| 18 | [附近好友](./nearby-friends-system/) | ✅ 核心链路已核对 | WS/Redis授权快照、TTL、有界工作与重订阅；76项测试、200/200投递；[开源源码核对](./nearby-friends-system/docs/open-source-review_cn.md) |
| 19 | [地图系统](./maps-system/) | ✅ 核心链路已核对 | 导航指令/编码路径、分级路由瓦片、持久预生成、动态导航/SSE；113项测试；[Valhalla/OSRM核对](./maps-system/docs/open-source-review_cn.md) |
| 20 | [分布式消息队列](./distributed-message-queue/) | ✅ 核心链路已核对 | 有界攒批、分区并发、标签过滤、定时长轮询与HTTP容量预留；121项测试；[Kafka/RocketMQ核对](./distributed-message-queue/docs/open-source-review_cn.md) |
| 21 | [监控与告警系统](./metrics-monitoring-system/) | ✅ 核心链路已核对 | 分区采集、加权查询、增量降采样、隔离告警与队列坏消息处理；112项测试；[Prometheus/Alertmanager核对](./metrics-monitoring-system/docs/open-source-review_cn.md) |
| 22 | [广告点击事件聚合](./ad-click-aggregation-system/) | ✅ 核心链路已核对 | 有界查询、精确Top N、迟到修正、重放/对账及源损坏处理；78项测试；[PyFlink/Flink核对](./ad-click-aggregation-system/docs/open-source-review_cn.md) |
| 23 | [酒店预订系统](./hotel-reservation-system/) | ✅ 核心链路已核对 | 滚动库存、原子预订/价格快照、模拟补偿及未知响应处理；100项测试；[开源源码核对](./hotel-reservation-system/docs/open-source-review_cn.md) |
| 24 | [分布式邮件服务](./distributed-email-service/) | ✅ 核心链路已核对 | Stalwart/maddy源码对照；通知准入保留写入容量；持久去重、读屏障及副本重放；99项测试；生产边界见章节记录 |
| 25 | [类S3对象存储](./object-storage-system/) | ✅ 核心链路已核对 | MinIO/SeaweedFS源码对照；上游错误响应边界、持久发布恢复与GC/聚合文件栅栏；138项测试；生产边界见章节记录 |
| 26 | [游戏排行榜](./gaming-leaderboard-system/) | ✅ 核心链路已核对 | WB-Games/Redis8.0.5源码对照；共享排名快照、分片/代际屏障、重放与资料缓存；72项测试含真实Redis；生产边界已记录 |
| 27 | [支付系统](./payment-system/) | ✅ 核心链路已核对 | Hyperswitch/TigerBeetle源码对照；可信checkout、账本/供应商身份、累计重试预算及对账租约栅栏；48项测试；全部模拟资金 |
| 28 | [数字钱包](./digital-wallet-system/) | ✅ 核心已审查 | Formance/Seata源码对照；TCC/重放/日志多数派与有界等待；修复分片/账户审计身份、累计余额边界；59项测试；Raft/账户副本待实现 |
| 29 | [证券交易系统](./stock-exchange-system/) | ✅ 核心已审查 | Exchange Core/CoinTossX源码对照；类型敏感重放/提升、坏投影阻断、行情身份与终态保护；64项测试；定序器HA/mmap待实现 |
| 30 | [持续学习](./docs/chapter30-learning-continues_cn.md) | ✅ 阅读导航 | 收尾章节无新增业务服务；核实GitHub入口，补充宏观图→调用链→故障实验的学习路线 |

## 计划实现

第1章社群介绍、第4章面试框架属于阅读章节，不列为独立系统实现。

## 章节文档索引

每个子项目的 `docs/` 目录包含设计、需求与任务三类规格文档，中文版以 `_cn` 后缀区分（如 `design_cn.md`）。下表按章节汇总所有文档入口。

| 章 | 英文文档 | 中文文档 |
|----|---------|---------|
| 02 | [design](./scale-from-zero/docs/design.md) · [requirements](./scale-from-zero/docs/requirements.md) · [tasks](./scale-from-zero/docs/tasks.md) | [设计](./scale-from-zero/docs/design_cn.md) · [需求](./scale-from-zero/docs/requirements_cn.md) · [任务](./scale-from-zero/docs/tasks_cn.md) |
| 03 | [design](./capacity-estimation/docs/design.md) · [requirements](./capacity-estimation/docs/requirements.md) · [tasks](./capacity-estimation/docs/tasks.md) | [设计](./capacity-estimation/docs/design_cn.md) · [需求](./capacity-estimation/docs/requirements_cn.md) · [任务](./capacity-estimation/docs/tasks_cn.md) |
| 05 | [design](./rate-limiter/docs/design.md) · [requirements](./rate-limiter/docs/requirements.md) · [tasks](./rate-limiter/docs/tasks.md) | [设计](./rate-limiter/docs/design_cn.md) · [需求](./rate-limiter/docs/requirements_cn.md) · [任务](./rate-limiter/docs/tasks_cn.md) |
| 06 | [design](./consistent-hashing/docs/design.md) · [requirements](./consistent-hashing/docs/requirements.md) · [tasks](./consistent-hashing/docs/tasks.md) | [设计](./consistent-hashing/docs/design_cn.md) · [需求](./consistent-hashing/docs/requirements_cn.md) · [任务](./consistent-hashing/docs/tasks_cn.md) |
| 07 | [design](./key-value-store/docs/design.md) · [requirements](./key-value-store/docs/requirements.md) · [tasks](./key-value-store/docs/tasks.md) | [设计](./key-value-store/docs/design_cn.md) · [需求](./key-value-store/docs/requirements_cn.md) · [任务](./key-value-store/docs/tasks_cn.md) |
| 08 | [design](./unique-id-generator/docs/design.md) · [requirements](./unique-id-generator/docs/requirements.md) · [tasks](./unique-id-generator/docs/tasks.md) | [设计](./unique-id-generator/docs/design_cn.md) · [需求](./unique-id-generator/docs/requirements_cn.md) · [任务](./unique-id-generator/docs/tasks_cn.md) |
| 09 | [design](./url-shortener/docs/design.md) · [requirements](./url-shortener/docs/requirements.md) · [tasks](./url-shortener/docs/tasks.md) | [设计](./url-shortener/docs/design_cn.md) · [需求](./url-shortener/docs/requirements_cn.md) · [任务](./url-shortener/docs/tasks_cn.md) |
| 10 | [design](./web-crawler/docs/design.md) · [requirements](./web-crawler/docs/requirements.md) · [tasks](./web-crawler/docs/tasks.md) | [设计](./web-crawler/docs/design_cn.md) · [需求](./web-crawler/docs/requirements_cn.md) · [任务](./web-crawler/docs/tasks_cn.md) |
| 11 | [design](./notification-system/docs/design.md) · [requirements](./notification-system/docs/requirements.md) · [tasks](./notification-system/docs/tasks.md) | [设计](./notification-system/docs/design_cn.md) · [需求](./notification-system/docs/requirements_cn.md) · [任务](./notification-system/docs/tasks_cn.md) |
| 12 | [design](./news-feed-system/docs/design.md) · [requirements](./news-feed-system/docs/requirements.md) · [tasks](./news-feed-system/docs/tasks.md) | [设计](./news-feed-system/docs/design_cn.md) · [需求](./news-feed-system/docs/requirements_cn.md) · [任务](./news-feed-system/docs/tasks_cn.md) |
| 13 | [design](./chat-system/docs/design.md) · [requirements](./chat-system/docs/requirements.md) · [tasks](./chat-system/docs/tasks.md) | [设计](./chat-system/docs/design_cn.md) · [需求](./chat-system/docs/requirements_cn.md) · [任务](./chat-system/docs/tasks_cn.md) |
| 14 | [design](./search-autocomplete-system/docs/design.md) · [requirements](./search-autocomplete-system/docs/requirements.md) · [tasks](./search-autocomplete-system/docs/tasks.md) | [设计](./search-autocomplete-system/docs/design_cn.md) · [需求](./search-autocomplete-system/docs/requirements_cn.md) · [任务](./search-autocomplete-system/docs/tasks_cn.md) |
| 15 | [design](./video-streaming-system/docs/design.md) · [requirements](./video-streaming-system/docs/requirements.md) · [tasks](./video-streaming-system/docs/tasks.md) | [设计](./video-streaming-system/docs/design_cn.md) · [需求](./video-streaming-system/docs/requirements_cn.md) · [任务](./video-streaming-system/docs/tasks_cn.md) |
| 16 | [design](./file-sync-system/docs/design.md) · [requirements](./file-sync-system/docs/requirements.md) · [tasks](./file-sync-system/docs/tasks.md) | [设计](./file-sync-system/docs/design_cn.md) · [需求](./file-sync-system/docs/requirements_cn.md) · [任务](./file-sync-system/docs/tasks_cn.md) |
| 17 | [design](./proximity-service/docs/design.md) · [requirements](./proximity-service/docs/requirements.md) · [tasks](./proximity-service/docs/tasks.md) | [设计](./proximity-service/docs/design_cn.md) · [需求](./proximity-service/docs/requirements_cn.md) · [任务](./proximity-service/docs/tasks_cn.md) |
| 18 | [design](./nearby-friends-system/docs/design.md) · [requirements](./nearby-friends-system/docs/requirements.md) · [tasks](./nearby-friends-system/docs/tasks.md) | [设计](./nearby-friends-system/docs/design_cn.md) · [需求](./nearby-friends-system/docs/requirements_cn.md) · [任务](./nearby-friends-system/docs/tasks_cn.md) |
| 19 | [design](./maps-system/docs/design.md) · [requirements](./maps-system/docs/requirements.md) · [tasks](./maps-system/docs/tasks.md) | [设计](./maps-system/docs/design_cn.md) · [需求](./maps-system/docs/requirements_cn.md) · [任务](./maps-system/docs/tasks_cn.md) |
| 20 | [design](./distributed-message-queue/docs/design.md) · [requirements](./distributed-message-queue/docs/requirements.md) · [tasks](./distributed-message-queue/docs/tasks.md) | [设计](./distributed-message-queue/docs/design_cn.md) · [需求](./distributed-message-queue/docs/requirements_cn.md) · [任务](./distributed-message-queue/docs/tasks_cn.md) |
| 21 | [design](./metrics-monitoring-system/docs/design.md) · [requirements](./metrics-monitoring-system/docs/requirements.md) · [tasks](./metrics-monitoring-system/docs/tasks.md) | [设计](./metrics-monitoring-system/docs/design_cn.md) · [需求](./metrics-monitoring-system/docs/requirements_cn.md) · [任务](./metrics-monitoring-system/docs/tasks_cn.md) |
| 22 | [design](./ad-click-aggregation-system/docs/design.md) · [requirements](./ad-click-aggregation-system/docs/requirements.md) · [tasks](./ad-click-aggregation-system/docs/tasks.md) | [设计](./ad-click-aggregation-system/docs/design_cn.md) · [需求](./ad-click-aggregation-system/docs/requirements_cn.md) · [任务](./ad-click-aggregation-system/docs/tasks_cn.md) |
| 23 | [design](./hotel-reservation-system/docs/design.md) · [requirements](./hotel-reservation-system/docs/requirements.md) · [tasks](./hotel-reservation-system/docs/tasks.md) | [设计](./hotel-reservation-system/docs/design_cn.md) · [需求](./hotel-reservation-system/docs/requirements_cn.md) · [任务](./hotel-reservation-system/docs/tasks_cn.md) |
| 24 | [design](./distributed-email-service/docs/design.md) · [requirements](./distributed-email-service/docs/requirements.md) · [tasks](./distributed-email-service/docs/tasks.md) | [设计](./distributed-email-service/docs/design_cn.md) · [需求](./distributed-email-service/docs/requirements_cn.md) · [任务](./distributed-email-service/docs/tasks_cn.md) |
| 25 | [design](./object-storage-system/docs/design.md) · [requirements](./object-storage-system/docs/requirements.md) · [tasks](./object-storage-system/docs/tasks.md) | [设计](./object-storage-system/docs/design_cn.md) · [需求](./object-storage-system/docs/requirements_cn.md) · [任务](./object-storage-system/docs/tasks_cn.md) |
| 26 | [design](./gaming-leaderboard-system/docs/design.md) · [requirements](./gaming-leaderboard-system/docs/requirements.md) · [tasks](./gaming-leaderboard-system/docs/tasks.md) | [设计](./gaming-leaderboard-system/docs/design_cn.md) · [需求](./gaming-leaderboard-system/docs/requirements_cn.md) · [任务](./gaming-leaderboard-system/docs/tasks_cn.md) |
| 27 | [design](./payment-system/docs/design.md) · [requirements](./payment-system/docs/requirements.md) · [tasks](./payment-system/docs/tasks.md) | [设计](./payment-system/docs/design_cn.md) · [需求](./payment-system/docs/requirements_cn.md) · [任务](./payment-system/docs/tasks_cn.md) |
| 28 | [design](./digital-wallet-system/docs/design.md) · [requirements](./digital-wallet-system/docs/requirements.md) · [tasks](./digital-wallet-system/docs/tasks.md) | [设计](./digital-wallet-system/docs/design_cn.md) · [需求](./digital-wallet-system/docs/requirements_cn.md) · [任务](./digital-wallet-system/docs/tasks_cn.md) |
| 29 | [design](./stock-exchange-system/docs/design.md) · [requirements](./stock-exchange-system/docs/requirements.md) · [tasks](./stock-exchange-system/docs/tasks.md) | [设计](./stock-exchange-system/docs/design_cn.md) · [需求](./stock-exchange-system/docs/requirements_cn.md) · [任务](./stock-exchange-system/docs/tasks_cn.md) |

## 项目结构

```
sdi-implement/
├── README.md                    # 本文件
├── System Design Interview.md   # 原书内容参考
├── scale-from-zero/             # 第02章：本机HTTP分阶段扩容实验
├── capacity-estimation/         # 第03章：显式单位容量与可用性估算
├── rate-limiter/                # 第05章：限流器
│   ├── README.md               # 英文使用文档
│   ├── README_CN.md            # 中文使用文档
│   ├── pyproject.toml          # 项目配置
│   ├── src/rate_limiter/       # 源代码
│   ├── tests/                  # 测试
│   ├── examples/               # 使用示例
│   └── docs/                   # 设计、需求与任务文档
├── consistent-hashing/          # 第06章：一致性哈希
│   ├── README.md               # 英文使用文档
│   ├── README_CN.md            # 中文使用文档
│   ├── pyproject.toml          # 项目配置
│   ├── src/consistent_hashing/ # 源代码
│   ├── tests/                  # 测试
│   ├── examples/               # 使用示例
│   └── docs/                   # 设计、需求与任务文档
├── key-value-store/             # 第07章：分布式键值存储
│   ├── README.md               # 英文使用文档
│   ├── README_CN.md            # 中文使用文档
│   ├── pyproject.toml          # 项目配置
│   ├── proto/                  # Protobuf 服务定义
│   ├── src/kv_store/           # 源代码
│   ├── tests/                  # 测试（单元、集成、属性测试）
│   ├── examples/               # 使用示例
│   └── docs/                   # 设计、需求与任务文档
├── unique-id-generator/         # 第08章：唯一 ID 生成器
│   ├── README.md               # 英文使用文档
│   ├── README_CN.md            # 中文使用文档
│   ├── pyproject.toml          # 项目配置
│   ├── src/unique_id/          # 源代码
│   ├── tests/                  # 测试（75 个测试）
│   ├── examples/               # 使用示例
│   └── docs/                   # 设计、需求与任务文档
├── url-shortener/               # 第09章：URL 短链接服务
│   ├── README.md               # 英文使用文档
│   ├── README_CN.md            # 中文使用文档
│   ├── pyproject.toml          # 项目配置
│   ├── src/url_shortener/      # 源代码
│   ├── tests/                  # 测试（单元 + 属性测试）
│   ├── examples/               # 演示服务器
│   └── docs/                   # 设计、需求与任务文档
├── web-crawler/                 # 第10章：网络爬虫
│   ├── README.md                # 使用文档
│   ├── pyproject.toml           # 项目配置
│   ├── src/web_crawler/         # 源代码
│   ├── tests/                   # 单元与属性测试
│   ├── examples/basic_crawl.py  # 爬取示例
│   └── docs/design.md           # 设计文档
├── notification-system/         # 第11章：通知系统
│   ├── README.md                # 使用文档
│   ├── pyproject.toml           # 项目配置
│   ├── src/notification_system/ # 源代码
│   ├── tests/                   # 单元与属性测试
│   ├── examples/demo_server.py  # 演示服务器
│   └── docs/design.md           # 设计文档
├── news-feed-system/            # 第12章：新闻动态系统
│   ├── README.md                # 英文使用文档
│   ├── README_CN.md             # 中文使用文档
│   ├── pyproject.toml           # 项目配置
│   ├── src/news_feed_system/    # 源代码
│   ├── tests/                   # 测试
│   └── examples/                # 后续示例占位目录
├── chat-system/                 # 第13章：聊天系统
│   ├── README.md                # 英文使用文档
│   ├── README_CN.md             # 中文使用文档
│   ├── pyproject.toml           # 项目配置
│   ├── src/chat_system/         # 核心代码与可选 WebSocket 适配层
│   ├── compose.yaml             # Redis、聊天节点、发现服务、推送工作进程
│   ├── tests/                   # 核心、WebSocket 及真实多进程测试
│   ├── examples/                # 本地示例与自动重连客户端
│   └── docs/design.md           # 设计文档
├── search-autocomplete-system/  # 第14章：搜索自动补全
│   ├── README.md                # 使用及 HTTP 部署说明
│   ├── README_CN.md             # 中文说明
│   ├── src/search_autocomplete/ # 批量快照、前缀树、分片副本
│   ├── tests/                  # 排名、持久化与进程故障恢复
│   ├── examples/               # 本地和真实 HTTP 示例
│   ├── benchmarks/             # 延迟实测及验证边界
│   └── docs/design.md          # 架构与限制
├── video-streaming-system/      # 第15章：视频系统
│   ├── README.md                # 使用与分布式部署说明
│   ├── README_CN.md             # 中文说明
│   ├── src/video_streaming/     # 上传、转码任务、PostgreSQL 与 S3
│   ├── compose.ha.yaml          # Patroni/etcd 主备与私有 S3 实验
│   ├── tests/                  # 40 项测试，含真实远端服务
│   ├── benchmarks/             # 媒体实测和故障恢复报告
│   └── docs/design.md          # 架构及部署边界
├── file-sync-system/            # 第16章：文件同步
│   ├── README.md                # 使用及协议说明
│   ├── README_CN.md             # 中文说明
│   ├── src/file_sync/           # 加密存储、修订与设备客户端
│   ├── tests/                  # 33 项正确性及故障测试
│   ├── examples/               # 独立进程和网络演示
│   ├── benchmarks/             # 增量带宽及同步实测
│   └── docs/design.md          # 架构及边界
├── proximity-service/          # 第17章：附近商家服务
├── nearby-friends-system/       # 第18章：实时授权附近好友
├── maps-system/                 # 第19章：路线、交通与地图瓦片
├── distributed-message-queue/   # 第20章：复制日志与消费者组
├── metrics-monitoring-system/   # 第21章：指标采集、查询与告警
├── ad-click-aggregation-system/  # 第22章：事件时间聚合与对账
├── hotel-reservation-system/    # 第23章：原子库存与支付补偿
├── distributed-email-service/   # 第24章：本机SMTP、元数据副本与邮箱搜索
├── object-storage-system/       # 第25章：版本、分段上传与数据副本修复
├── gaming-leaderboard-system/   # 第26章：Redis月赛排名与权威日志恢复
├── payment-system/              # 第27章：模拟支付、复式账本与对账恢复
├── digital-wallet-system/       # 第28章：分片转账、事件重放与TCC恢复
├── stock-exchange-system/       # 第29章：限价撮合、主备重放与行情恢复
└── .gitignore
```

## 设计原则

每个子项目遵循以下原则：

1. **独立可运行** — 每个子项目有自己的 `pyproject.toml`，可独立安装和测试
2. **回归验证** — 单元和集成测试分别记录，明确真实服务前提与跳过项
3. **文档齐全** — README、架构文档、设计规格
4. **工程化约束** — 类型注解、错误处理、有界资源及明确的一致性/持久性边界
5. **忠于原书** — 实现书中描述的核心概念和算法

## 一次性安装全部依赖

根目录提供两个汇总入口，便于一次性准备所有章节的运行环境：

- `pyproject.toml` — 汇总 27 个子项目的第三方依赖，按 `runtime`、`dev`、`demo`、`all` 分组
- `requirements-all.txt` — 以可编辑模式安装全部 27 个子项目及其 dev/test 依赖

```bash
# 只安装全部第三方依赖
pip install -e ".[all]"

# 安装第三方依赖 + 全部子项目（推荐）
pip install -r requirements-all.txt
```

完成后按各子项目文档运行测试或示例；真实服务测试仍需对应章节的服务和配置。其中 `key-value-store` 依赖本地包 `consistent-hashing`，`requirements-all.txt` 已将其排在前面优先安装。

## 快速开始

```bash
# 进入某个子项目
cd rate-limiter

# 安装依赖
pip install -e ".[dev]"

# 使用当前解释器运行测试
python -W error -m pytest tests -q

# 运行示例
python examples/basic_usage.py
```

## 环境要求

- 汇总环境要求Python >= 3.10，与根目录 `pyproject.toml` 一致
- 单独运行章节时，以该章节 `pyproject.toml` 和README的版本及依赖要求为准

## License

MIT

## 工程质量

当前证据见[复审覆盖清单](docs/review-coverage_cn.md)和[章节进度](DEEP_REVIEW_PROGRESS.md)，工程组织与验证约定见[CONTRIBUTING.md](CONTRIBUTING.md)。[ENGINEERING_REVIEW.md](ENGINEERING_REVIEW.md)保留注明日期的全仓基础审查，并链接最新记录。可运行 `python tools/audit_code.py` 生成源码检查报告。

27个子项目均有章节复审记录，记录来自不同日期和执行批次，不代表本次重新运行了全仓测试。安装各章节依赖后，可用 `python tools/run_project_tests.py --jobs 2 --output .test-tmp/review` 在独立进程中执行各章测试；选择性重跑的结果保留方式与真实服务前提见CONTRIBUTING。
