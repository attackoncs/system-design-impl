# 逐项目深入审查进度

本表跟踪用户要求的逐项目审查与修改。此前 ENGINEERING_REVIEW.md 的全仓库扫描、格式整理和回归是基础检查，不等同于本表所有项目已完成深入审查。

每个项目依次检查：领域与基础设施职责、公共接口和依赖、可复用逻辑、错误与资源生命周期、共享状态和并发边界；对确认的问题修改并增加行为回归，运行该项目完整测试。保留章节独立安装及一致性/持久化语义，不为统一外观强行抽公共包。

| 章节 | 项目 | 本轮状态 |
| --- | --- | --- |
| 02 | [从零扩容](scale-from-zero/) | 完成：19 项测试通过 |
| 03 | [容量估算](capacity-estimation/) | 完成：14 项测试通过 |
| 05 | [Rate Limiter](rate-limiter/) | 完成：205 项测试通过 |
| 06 | [Consistent Hashing](consistent-hashing/) | 完成：原书核对，108 项测试通过 |
| 07 | [Key-Value Store](key-value-store/) | 进行中：383 项测试通过；原书剩余项见章节审查记录 |
| 08 | [Unique ID Generator](unique-id-generator/) | 完成：原书核对，94 项测试通过 |
| 09 | [URL Shortener](url-shortener/) | 完成核心链路核对：109 项测试通过；部署扩展见章节记录 |
| 10 | [Web Crawler](web-crawler/) | 完成核心链路核对：213 项测试通过；部署边界见章节记录 |
| 11 | [Notification System](notification-system/) | 待逐项深入审查 |
| 12 | [News Feed System](news-feed-system/) | 待逐项深入审查 |
| 13 | [Chat System](chat-system/) | 待逐项深入审查 |
| 14 | [Search Autocomplete](search-autocomplete-system/) | 待逐项深入审查 |
| 15 | [Video Streaming](video-streaming-system/) | 待逐项深入审查 |
| 16 | [File Sync](file-sync-system/) | 待逐项深入审查 |
| 17 | [附近商家服务](proximity-service/) | 待逐项深入审查 |
| 18 | [附近好友](nearby-friends-system/) | 待逐项深入审查 |
| 19 | [地图系统](maps-system/) | 待逐项深入审查 |
| 20 | [分布式消息队列](distributed-message-queue/) | 待逐项深入审查 |
| 21 | [监控与告警系统](metrics-monitoring-system/) | 待逐项深入审查 |
| 22 | [广告点击事件聚合](ad-click-aggregation-system/) | 待逐项深入审查 |
| 23 | [酒店预订系统](hotel-reservation-system/) | 待逐项深入审查 |
| 24 | [分布式邮件服务](distributed-email-service/) | 待逐项深入审查 |
| 25 | [类S3对象存储](object-storage-system/) | 待逐项深入审查 |
| 26 | [游戏排行榜](gaming-leaderboard-system/) | 待逐项深入审查 |
| 27 | [支付系统](payment-system/) | 待逐项深入审查 |
| 28 | [数字钱包](digital-wallet-system/) | 待逐项深入审查 |
| 29 | [证券交易系统](stock-exchange-system/) | 待逐项深入审查 |

## 第 02 章：从零扩容

发现与修改：

- 负载均衡策略原来嵌在 HTTP Handler 中，任意带 request_id 的 POST 都被当作可重试。提取 balancer.py，把可重试写入限制为实现幂等契约的资料端点；轮询游标和计数加锁，网络调用不持锁。
- Cache 依赖 HTTP 层外部锁，直接复用时存在 LRU/计数竞态。锁移至缓存内部，TTL 判断复用一次时钟采样，拷贝放在锁外；HTTP 不再重复管理缓存锁。
- CLI 内嵌复制和消费循环且静默吞异常。提取 workers.py 中可独立测试的单轮操作和线程生命周期；异常记录日志，分片失败隔离，退出时停止并等待自有线程。
- Web 缓存失败计数和数据库读取计数使用自己的短锁，避免并发更新丢失。

新增回归覆盖：按端点决定重试、不重试业务冲突、慢上游不阻塞下一路由、并发缓存淘汰/准确计数/拷贝隔离、停止等待在途任务、故障分片不饿死其他分片。

边界：保留 SQLite 事务串行化；缓存仍是最终一致，失效与回填竞态需要 strong/min_version 读语义；未增加生产级连接数限制或声称吞吐提升。

## 第 03 章：容量估算

- 把显式单位、Decimal 边界验证移至 quantities.py，把场景默认值/字段校验移至 scenario.py；model.py 专注计算公式，保留原公开导入路径。
- 修复 localcontext 仅设置精度仍继承调用者舍入、异常陷阱和指数范围的问题：计算使用独立 80 位上下文，CLI 单位换算复用相同策略。
- sweep 对错误输入容器统一抛 ValueError，避免 CLI 暴露意外 TypeError。
- 14 项测试通过，新增调用方启用 Inexact 陷阱、收紧指数范围及并发线程不同精度场景；输入及调用方数值配置保持不变。
- 本项目是无 I/O 的纯计算，不人为引入锁或线程池；不宣称并发加速。

## 第 05 章：限流器

- 滑动日志原成员 ID 为时间戳加算法对象地址，同刻请求会覆盖 Redis ZSET 成员而少计数；改为每次独立 UUID。
- 五种算法共用结果解码，正确处理 bytes 数字标志；减少维护多个解析副本。
- 规则构造时校验正整数限制/窗口、算法枚举及 key_func，避免无效配置进入运行时 fail-open。
- 通用装饰器把规则构造移出请求路径；异步包装保留协程身份、在线程执行同步存储检查、拒绝时不调用业务函数。
- 205 项通过、1 条已有弃用警告。新增 14 项覆盖同刻并发的内存/fakeredis Lua一致性、异步响应性与拒绝路径、数值解码和无效配置。
- 中英文 README 和 design.md 已同步更新。明确普通字典不自动产生 HTTP 429、取消不能回滚扣额、多规则非事务、内存惰性回收和执行器容量边界；未声称真实 Redis 集群容量已验证。

## 第 06 章：一致性哈希（原书对照）

已阅读根目录《System Design Interview.md》第 06 章。原有哈希环、SHA-1、虚拟节点、局部重分配及分布统计已有实现；缺少无需全量键扫描的受影响区间接口和可执行的取模/虚拟节点数量对照。

- 新增公开不可变 RingSnapshot；compute_distribution 固定一份拓扑，避免并发增删时统计混合版本。
- migration.py 新增 affected_ranges 与 AffectedRange，比较前后主属，覆盖环回、碰撞、空环和精确端点；只生成计划，不执行物理迁移。
- 新增 chapter_comparison.py：固定 10,000 样本键扩容时，一致性哈希迁移 969 个、取模 9066 个；展示 1/100/200 虚拟节点的空间与分布取舍。
- 完整套件 108 项通过，其中新增 22 项章节/并发回归。原书中的实际系统案例只作背景，未冒充实现 Dynamo/CDN 等。
- 中英文 README、design.md/design_cn.md 更新覆盖表、示例、区间语义与真实复制排序复杂度；明确单个热点键不被拆分、有限样本不保证均衡比例、保留快照占用内存。

第 06 章依赖兼容验证：KV 子项目完整测试 **367 passed**，只作为此次哈希库修改的调用方回归，不表示第 07 章已经完成原书核对或深入审查。


## 第 07 章：键值存储（进行中）

已对照原书并修复副本 RPC 递归协调/错误地址、时钟和冲突丢失、删除等同空值、hint 接口未接通、Merkle 索引未更新及后台任务未连接等问题。新增持久记录层、网络协调器、故障域放置、SQLite hint 日志和双向修复；加入请求容量预留及完整关闭清理，并补充真实 loopback gRPC 回归。更新中英文 README、设计文档和依赖声明。

详细证据与兼容性见 [第 07 章审查记录](key-value-store/docs/chapter07-review.md)。自动扩缩容/安全下线、Merkle 网络逐层遍历与大差异分页等尚未完成，因此不增加已完成项目数，也不把章节标记为全部实现。

第 07 章本轮验证：完整测试 **383 passed（34.75 秒）**，Ruff E9/F 和 `git diff --check` 通过；未做吞吐压测，未提交或推送。

## 第 08 章：分布式唯一 ID

对照原书最终 Snowflake 方案，补充配置的严格整数校验、序列耗尽时的让出 CPU 与单调时间截止期限、等待中的回拨拒绝。超时和回拨不提交序列状态。新增配置错误、超时恢复、回拨和多线程共享实例回归；完整测试 **94 passed**，Ruff E9/F 通过。

本机单线程 100,000 次 generate 耗时 0.4861 秒，100,000 个 ID 无重复，约 205,710 ID/s；这是当前机器一次测量，未证明生产环境持续容量。中英文 README 和设计文档关联 [章节核对记录](unique-id-generator/docs/chapter08-review.md)，明确重启身份隔离、批量部分失败、时钟同步及高可用部署边界。

## 第 09 章：短链接服务

补齐 SQLite 事务持久化与唯一约束、有限容量 LRU 回源缓存、WSGI POST 和 HTTP 301/302 重定向入口。内存与 SQLite 存储拒绝覆盖已有映射；本地 ID 分配加锁，Base62 参数校验及 HTTP URL 输入限制。新增共享数据库并发去重、重启读取、冲突保留旧映射、HTTP 和缓存命中回归。完整测试 **109 passed（3.01 秒）**；Ruff E9/F 通过。中英文 README 和设计文档同步关联 [章节核对记录](url-shortener/docs/chapter09-review.md)。

尚未部署负载均衡、分片/复制、全局限流和分析服务；本地 ID 分配不提供跨实例唯一性。未做生产吞吐或海量容量验证。

## 第 10 章：网页爬虫

继续完成持久调度恢复、原子 HTML 快照及去重、有限窗口主机调度、有界重试和自适应周期重爬；接通一致性哈希分片与持久投递回调。补齐有界 DNS TTL/LRU、保留 Host/SNI 的 HTTP(S) 连接、响应大小与总 deadline、重定向重新检查 robots。修复相同 HTML 不同基准 URL 的链接漏发现、完成确认与礼貌时间非原子、并发/取消时任务及内容声明处理。

完整测试 **213 passed（12.69 秒）**，Ruff E9/F 通过。中英文 README 与设计文档同步；详见 [章节核对](web-crawler/docs/chapter10-review.md)。五年保留清理、复制/分片控制面、拓扑安全移交、动态渲染及互联网规模容量均明确为部署/扩展边界，无生产容量承诺。
