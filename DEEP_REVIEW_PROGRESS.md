# 逐项目深入审查进度

## 2026-11-09 效率复审与全仓回归

本轮按原书核对后，针对**随历史/数据量增长的热路径成本**逐章排查并修复（每项先读代码与调用方确认，再用HEAD副本对照测量）：

| 章 | 问题 → 修复 | 实测（旧→新） |
|---|---|---|
| 07 键值存储 | 启动重建Merkle索引O(N²/B) → `bulk_load`每桶排序/哈希一次 | 20万键 18.05秒→0.58秒 |
| 10 网页爬虫 | 每次`get()`对全部待抓取行窗口排序 → 按主机索引取队首 | 20万任务 427→11.3毫秒/次 |
| 11 通知系统 | 每次发送扫描去重窗口全部条目 → 从最旧端过期 | 5万条 1124.7→0.4微秒/次 |
| 12 新闻动态 | 内存存储每页排序作者全部历史 → 有序列表二分 | 2万帖翻完 468→36.8毫秒 |
| 13 聊天 | 新成员历史逐条探测入群前消息 → 记录入群位置 | SQLite 10万 约25→0.05毫秒 |
| 15 视频 | 认领任务OFFSET分页 → 键集分页 | 2万阻塞任务 6,798→107毫秒 |
| 16 文件同步 | 监视器每秒重读全部文件；配额每次求和全部修订 → stat快检、维护used计数 | 空闲周期395→25毫秒；配额16.5→0.017毫秒 |
| 17 附近商家 | 每次写入无索引清理replay → `replay(created)`索引 | 10万行 7.4→0.003毫秒 |
| 20 消息队列 | 每次`/sync`全量重扫日志 → 仅截断 | 2万条 361→0.7毫秒/批 |
| 21/22/24/26 | 热路径调用含COUNT(*)的完整status → 只读水位/检查点/heads | 例：指标告警评估23.5→1.26毫秒 |
| 25 对象存储 | 每次启动重哈希整个pack → 持久水位只扫尾部 | 2万写入 237→2.3毫秒 |
| 28 数字钱包 | 快照恢复仍全量重放×2、重放平方级、日志同步全量 → 快照＋尾部、原地重放、增量校验 | 3000事件重启 21.7秒→22毫秒 |
| 29 证券交易所 | 启动三遍平方级重放 → 单次线性遍历 | 1000命令重启 8.3秒→约0.3秒 |

全仓回归中修复的跨章问题：Python 3.14下未关闭的`HTTPError`（临时文件包装）在GC时告警，`-W error`使测试失败——在13个项目的生产传输代码中构造即关闭或在丢弃处关闭（保留可读body）；Python 3.14的`json`不再对4000层嵌套抛`RecursionError`，酒店/对象存储改为显式深度上限64；KV Merkle协议测试与后台反熵首轮竞态、对象存储实验端口抢占、邮件丢ACK用例等待短于90秒租约等测试/工具时序问题；限流器测试仅忽略Starlette≥1.10的`httpx2`弃用告警。

验证（WSL2/Python 3.14，逐项目串行，`-W error`）：27个项目中25个全部通过，全仓共4,058项通过（支付与限流器在修复后单独重跑：48项、265项）。其余2个为环境缺失而非代码失败：游戏排行榜4项需真实`redis-server`（`LB_REDIS_BINARY`），视频2项真实FFmpeg用例因`imageio-ffmpeg`静态构建在HLS/HTTP读取时段错误（-11），需系统FFmpeg。各章细节见对应`docs/chapterNN-review.md`的2026-11-09小节。

### 2026-11-09 第二轮：补齐各章可落地的未完成任务（只改生产代码）

| 章 | 完成的任务 | 要点 |
|---|---|---|
| 07 键值存储 | SSTable不再整文件驻留、流式压缩 | 稀疏索引＋字节上限LRU块缓存；k路归并；磁盘格式不变；压缩进程峰值611→49 MiB |
| 08 唯一ID | 持久重启高水位 | 可选高水位文件，每窗口fsync一次；重启前须越过上限，防回拨重复 |
| 09 短链接 | 点击明细保留 | 精确计数＋每链接有界deque（默认1000条/30天） |
| 10 网页爬虫 | RFC 9309 robots | 同名组合并、`*`回退、4xx放行/5xx禁止、500 KiB/24小时；网络失败默认放行（可配置disallow） |
| 16 文件同步 | 事件保留与重新快照 | 按空间限龄/限量增量清理；游标落后时返回`resnapshot`，客户端按冲突规则对齐 |
| 19 地图 | 字节缓存预算 | 瓦片32 MiB、路由邻接16 MiB，超大值不缓存，`/health`暴露统计 |
| 20 消息队列 | 分块增量追赶 | 从副本自身已校验位置分块`/sync`；落后2万条3轮追平（原先无法追平） |
| 21 监控告警 | 回执/告警元数据GC | 索引范围删除、每轮有界；不删待发送/租约中投递及活动告警 |
| 22 广告点击 | 去重/状态GC | 默认7天去重窗口、10万位置重放窗口；窗口外重复计入`expired`不重复计数（行为变化，已记录） |
| 23 酒店 | 回执GC与隐私脱敏 | 默认关闭；未知支付/待退款永不清理；过期回执重试返回409 |
| 25 对象存储 | pack轮转 | 默认256 MiB封存只读、活动pack尾部恢复、压缩仅处理已封存pack；崩溃顺序已逐步验证 |
| 28 数字钱包 | 按行投影 | 写入O(1)；3000笔写入17.9→1.52秒 |

共识/选主、TLS、真实供应商、CDN、跨机房等仍为各章记录的边界。除钱包一处直接篡改已删除内部表的测试行外，本轮未修改测试与基准代码。

## 当前快照（2026-11-06）

根README所列 **27个独立子项目均已有按原书、开源实现与CodeGraph核对的复审记录**。第2、3章已补齐统一流程；此前将第7章列为“进行中”、第24–29章列为“待审”的表格属于下方历史快照，不能继续作为当前待办。第1、4、30章为阅读/学习章节，不计入27项。

完成的是选定参考实现的复审流程，原书所有生产能力并未全部实现。自动供给/安全缩容、真正共识、跨机房容灾、真实供应商与规模验收等边界，仍按各章文档保留；详细清点标准见[复审覆盖清单](docs/review-coverage_cn.md)。

### 最近验证与计数规则

- **本轮重新执行第2、3章**：从零扩容77项通过、5个子例通过（51.35秒）；容量估算16项通过（0.52秒）。租约与写入竞态、非法TTL保留旧缓存、精确节点取整及备用节点大数相加已纳入回归。
- **第13章单独记录主套与HA补测**：11月5日主套收集203项，202项通过、1项跳过（6.612秒）；11月6日新建六进程Sentinel环境，独立补测该用例1项通过（JUnit 5.498秒，恢复2.319秒）。两轮覆盖203个不同用例，不能记为单次203项通过；[主套原始结果](chat-system/benchmarks/review-2026-11-05.json)和[补测原始结果](chat-system/benchmarks/sentinel-review-2026-11-06.json)分别保留。
- **其余25项沿用各自日期的既有验证**，本次文档同步没有重跑业务测试。下表不是本轮全仓执行结果，不跨重叠补测求和，也不把历史全仓1,949项直接更新为新的总数。第15章164个不同用例包含常规套件158项及PostgreSQL/S3补测6项，不能再重复加入重叠的分布式用例。

### 当前逐章证据索引

| 章 | 子项目 | 最近记录的验证范围 | 章节 / 开源对照 / 验证 |
| --- | --- | --- | --- |
| 02 | 从零扩容 | 本轮77项、5个子例通过；生产级多地域HA仍待实现 | [章节](scale-from-zero/docs/chapter02-review.md) · [对照](scale-from-zero/docs/open-source-review_cn.md) · [报告](scale-from-zero/benchmarks/REPORT.md) |
| 03 | 容量估算 | 本轮16项通过；估算不等于实测容量 | [章节](capacity-estimation/docs/chapter03-review.md) · [对照](capacity-estimation/docs/open-source-review_cn.md) · [报告](capacity-estimation/benchmarks/REPORT.md) |
| 05 | 限流器 | 265项通过，含真实Redis与HTTP验证 | [章节](rate-limiter/docs/chapter05-review.md) · [对照](rate-limiter/docs/open-source-review_cn.md) · [报告](rate-limiter/benchmarks/REPORT.md) |
| 06 | 一致性哈希 | 163项通过，含四进程一致性；放置不等于复制 | [章节](consistent-hashing/docs/chapter06-review.md) · [对照](consistent-hashing/docs/open-source-review_cn.md) · [报告](consistent-hashing/benchmarks/REPORT.md) |
| 07 | 键值存储 | 428项通过；自动供给/安全下线尚未实现 | [章节](key-value-store/docs/chapter07-review.md) · [对照](key-value-store/docs/open-source-review_cn.md) · [报告](key-value-store/benchmarks/REPORT.md) |
| 08 | 唯一ID | 122项通过；身份与重启安全有部署前提 | [章节](unique-id-generator/docs/chapter08-review.md) · [对照](unique-id-generator/docs/open-source-review_cn.md) · [报告](unique-id-generator/benchmarks/REPORT.md) |
| 09 | 短链接 | 145项通过，含多进程创建与真实HTTP验证 | [章节](url-shortener/docs/chapter09-review.md) · [对照](url-shortener/docs/open-source-review_cn.md) · [报告](url-shortener/benchmarks/REPORT.md) |
| 10 | 网页爬虫 | 259项通过；连续取消与文件锁释放已补测 | [章节](web-crawler/docs/chapter10-review.md) · [对照](web-crawler/docs/open-source-review_cn.md) · [报告](web-crawler/benchmarks/REPORT.md) |
| 11 | 通知系统 | 466项通过；真实供应商与共享队列边界保留 | [章节](notification-system/docs/chapter11-review.md) · [对照](notification-system/docs/open-source-review_cn.md) · [报告](notification-system/benchmarks/REPORT.md) |
| 12 | 新闻动态 | 311项通过；深层JSON拒绝/恢复已验收 | [章节](news-feed-system/docs/chapter12-review.md) · [对照](news-feed-system/docs/open-source-review_cn.md) · [报告](news-feed-system/benchmarks/REPORT.md) |
| 13 | 聊天系统 | 主套202项通过、1项跳过；独立HA补测1项通过 | [章节](chat-system/docs/chapter13-review.md) · [对照](chat-system/docs/open-source-review_cn.md) · [报告](chat-system/benchmarks/results.md) |
| 14 | 搜索自动补全 | 188项通过 | [章节](search-autocomplete-system/docs/chapter14-review.md) · [对照](search-autocomplete-system/docs/open-source-review_cn.md) · [报告](search-autocomplete-system/benchmarks/results.md) |
| 15 | 视频流媒体 | 覆盖164个不同用例，常规与PostgreSQL/S3补测分开记录 | [章节](video-streaming-system/docs/chapter15-review.md) · [对照](video-streaming-system/docs/open-source-review_cn.md) · [报告](video-streaming-system/benchmarks/results.md) |
| 16 | 文件同步 | 122项通过 | [章节](file-sync-system/docs/chapter16-review.md) · [对照](file-sync-system/docs/open-source-review_cn.md) · [报告](file-sync-system/benchmarks/results.md) |
| 17 | 附近商家 | 76项通过 | [章节](proximity-service/docs/chapter17-review.md) · [对照](proximity-service/docs/open-source-review_cn.md) · [报告](proximity-service/benchmarks/results.md) |
| 18 | 附近好友 | 76项通过，含真实Redis验证 | [章节](nearby-friends-system/docs/chapter18-review.md) · [对照](nearby-friends-system/docs/open-source-review_cn.md) · [报告](nearby-friends-system/benchmarks/results.md) |
| 19 | 地图系统 | 113项通过；使用选定合成路网 | [章节](maps-system/docs/chapter19-review.md) · [对照](maps-system/docs/open-source-review_cn.md) · [报告](maps-system/benchmarks/results.md) |
| 20 | 分布式消息队列 | 121项通过 | [章节](distributed-message-queue/docs/chapter20-review.md) · [对照](distributed-message-queue/docs/open-source-review_cn.md) · [报告](distributed-message-queue/benchmarks/results.md) |
| 21 | 指标监控与告警 | 112项通过 | [章节](metrics-monitoring-system/docs/chapter21-review.md) · [对照](metrics-monitoring-system/docs/open-source-review_cn.md) · [报告](metrics-monitoring-system/benchmarks/results.md) |
| 22 | 广告点击聚合 | 78项通过 | [章节](ad-click-aggregation-system/docs/chapter22-review.md) · [对照](ad-click-aggregation-system/docs/open-source-review_cn.md) · [报告](ad-click-aggregation-system/benchmarks/results.md) |
| 23 | 酒店预订 | 100项通过；支付为本地模拟账本 | [章节](hotel-reservation-system/docs/chapter23-review.md) · [对照](hotel-reservation-system/docs/open-source-review_cn.md) · [报告](hotel-reservation-system/benchmarks/results.md) |
| 24 | 分布式邮件 | 99项通过；互联网SMTP投递边界保留 | [章节](distributed-email-service/docs/chapter24-review.md) · [对照](distributed-email-service/docs/open-source-review_cn.md) · [报告](distributed-email-service/benchmarks/results.md) |
| 25 | 类S3对象存储 | 138项通过 | [章节](object-storage-system/docs/chapter25-review.md) · [对照](object-storage-system/docs/open-source-review_cn.md) · [报告](object-storage-system/benchmarks/results.md) |
| 26 | 游戏排行榜 | 72项通过，含真实Redis验证 | [章节](gaming-leaderboard-system/docs/chapter26-review.md) · [对照](gaming-leaderboard-system/docs/open-source-review_cn.md) · [报告](gaming-leaderboard-system/benchmarks/results.md) |
| 27 | 支付系统 | 48项通过；全部为模拟资金 | [章节](payment-system/docs/chapter27-review.md) · [对照](payment-system/docs/open-source-review_cn.md) · [报告](payment-system/benchmarks/REPORT.md) |
| 28 | 数字钱包 | 2026-11-09：快照＋尾部恢复、线性重放、增量日志同步，70项通过（WSL）；Raft/账户事件副本尚未实现 | [章节](digital-wallet-system/docs/chapter28-review.md) · [对照](digital-wallet-system/docs/open-source-review_cn.md) · [报告](digital-wallet-system/benchmarks/REPORT.md) |
| 29 | 证券交易所 | 64项通过；定序器HA、mmap/FIX等尚未实现 | [章节](stock-exchange-system/docs/chapter29-review.md) · [对照](stock-exchange-system/docs/open-source-review_cn.md) · [报告](stock-exchange-system/benchmarks/REPORT.md) |

## 历史进度与实施记录（保留当时表述）

以下保留此前逐次审查的表格和原始记录。旧计数、“进行中/待审”、未实现项和“未提交/推送”等描述仅对应各自记录时点，当前复审覆盖与最新验证入口以上方快照为准。这里不根据后续结果改写旧测试数，也不据旧状态判断当前是否已处理。

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
| 11 | [Notification System](notification-system/) | 完成核心链路核对：447 项测试通过；外部渠道和部署边界见章节记录 |
| 12 | [News Feed System](news-feed-system/) | 2026-10-29复核：282项通过；补齐中英文需求/任务、有界读取并发及本地指标；部署待办见章节记录 |
| 13 | [Chat System](chat-system/) | 完成核心链路核对：真实 Redis 回归171项通过、1项明确跳过；部署边界见章节记录 |
| 14 | [Search Autocomplete](search-autocomplete-system/) | 完成核心链路核对：170项测试通过；浏览器缓存取舍与部署边界见章节记录 |
| 15 | [Video Streaming](video-streaming-system/) | 完成核心链路核对：真实PostgreSQL完整回归160项通过、无跳过；部署边界见章节记录 |
| 16 | [File Sync](file-sync-system/) | 完成核心链路核对：116项测试通过；自动同步与部署边界见章节记录 |
| 17 | [附近商家服务](proximity-service/) | 完成核心链路核对：76项测试通过；发布/缓存/网络及部署边界见章节记录 |
| 18 | [附近好友](nearby-friends-system/) | 完成核心链路核对：真实Redis完整回归76项通过，无跳过；扩展边界见章节记录 |
| 19 | [地图系统](maps-system/) | 完成核心链路核对：113项测试通过；预生成/主动导航及生产边界见章节记录 |
| 20 | [分布式消息队列](distributed-message-queue/) | 完成核心链路核对：116项测试通过；批处理/并发/过滤/扩容及生产边界见章节记录 |
| 21 | [监控与告警系统](metrics-monitoring-system/) | 完成核心链路核对：108项测试通过；采集/分组查询/降采样/告警及生产边界见章节记录 |
| 22 | [广告点击事件聚合](ad-click-aggregation-system/) | 完成核心链路核对：76项测试通过；查询/重放/检查点及生产边界见章节记录 |
| 23 | [酒店预订系统](hotel-reservation-system/) | 完成核心链路核对：99项测试通过；日历/库存/支付补偿及生产边界见章节记录 |
| 24 | [分布式邮件服务](distributed-email-service/) | 待逐项深入审查 |
| 25 | [类S3对象存储](object-storage-system/) | 待逐项深入审查 |
| 26 | [游戏排行榜](gaming-leaderboard-system/) | 待逐项深入审查 |
| 27 | [支付系统](payment-system/) | 待逐项深入审查 |
| 28 | [数字钱包](digital-wallet-system/) | 待逐项深入审查 |
| 29 | [证券交易系统](stock-exchange-system/) | 待逐项深入审查 |

### 第 02 章：从零扩容

发现与修改：

- 负载均衡策略原来嵌在 HTTP Handler 中，任意带 request_id 的 POST 都被当作可重试。提取 balancer.py，把可重试写入限制为实现幂等契约的资料端点；轮询游标和计数加锁，网络调用不持锁。
- Cache 依赖 HTTP 层外部锁，直接复用时存在 LRU/计数竞态。锁移至缓存内部，TTL 判断复用一次时钟采样，拷贝放在锁外；HTTP 不再重复管理缓存锁。
- CLI 内嵌复制和消费循环且静默吞异常。提取 workers.py 中可独立测试的单轮操作和线程生命周期；异常记录日志，分片失败隔离，退出时停止并等待自有线程。
- Web 缓存失败计数和数据库读取计数使用自己的短锁，避免并发更新丢失。

新增回归覆盖：按端点决定重试、不重试业务冲突、慢上游不阻塞下一路由、并发缓存淘汰/准确计数/拷贝隔离、停止等待在途任务、故障分片不饿死其他分片。

边界：保留 SQLite 事务串行化；缓存仍是最终一致，失效与回填竞态需要 strong/min_version 读语义；未增加生产级连接数限制或声称吞吐提升。

### 第 03 章：容量估算

- 把显式单位、Decimal 边界验证移至 quantities.py，把场景默认值/字段校验移至 scenario.py；model.py 专注计算公式，保留原公开导入路径。
- 修复 localcontext 仅设置精度仍继承调用者舍入、异常陷阱和指数范围的问题：计算使用独立 80 位上下文，CLI 单位换算复用相同策略。
- sweep 对错误输入容器统一抛 ValueError，避免 CLI 暴露意外 TypeError。
- 14 项测试通过，新增调用方启用 Inexact 陷阱、收紧指数范围及并发线程不同精度场景；输入及调用方数值配置保持不变。
- 本项目是无 I/O 的纯计算，不人为引入锁或线程池；不宣称并发加速。

### 第 05 章：限流器

- 滑动日志原成员 ID 为时间戳加算法对象地址，同刻请求会覆盖 Redis ZSET 成员而少计数；改为每次独立 UUID。
- 五种算法共用结果解码，正确处理 bytes 数字标志；减少维护多个解析副本。
- 规则构造时校验正整数限制/窗口、算法枚举及 key_func，避免无效配置进入运行时 fail-open。
- 通用装饰器把规则构造移出请求路径；异步包装保留协程身份、在线程执行同步存储检查、拒绝时不调用业务函数。
- 205 项通过、1 条已有弃用警告。新增 14 项覆盖同刻并发的内存/fakeredis Lua一致性、异步响应性与拒绝路径、数值解码和无效配置。
- 中英文 README 和 design.md 已同步更新。明确普通字典不自动产生 HTTP 429、取消不能回滚扣额、多规则非事务、内存惰性回收和执行器容量边界；未声称真实 Redis 集群容量已验证。

### 第 06 章：一致性哈希（原书对照）

已阅读根目录《System Design Interview.md》第 06 章。原有哈希环、SHA-1、虚拟节点、局部重分配及分布统计已有实现；缺少无需全量键扫描的受影响区间接口和可执行的取模/虚拟节点数量对照。

- 新增公开不可变 RingSnapshot；compute_distribution 固定一份拓扑，避免并发增删时统计混合版本。
- migration.py 新增 affected_ranges 与 AffectedRange，比较前后主属，覆盖环回、碰撞、空环和精确端点；只生成计划，不执行物理迁移。
- 新增 chapter_comparison.py：固定 10,000 样本键扩容时，一致性哈希迁移 969 个、取模 9066 个；展示 1/100/200 虚拟节点的空间与分布取舍。
- 完整套件 108 项通过，其中新增 22 项章节/并发回归。原书中的实际系统案例只作背景，未冒充实现 Dynamo/CDN 等。
- 中英文 README、design.md/design_cn.md 更新覆盖表、示例、区间语义与真实复制排序复杂度；明确单个热点键不被拆分、有限样本不保证均衡比例、保留快照占用内存。

第 06 章依赖兼容验证：KV 子项目完整测试 **367 passed**，只作为此次哈希库修改的调用方回归，不表示第 07 章已经完成原书核对或深入审查。


### 第 07 章：键值存储（进行中）

已对照原书并修复副本 RPC 递归协调/错误地址、时钟和冲突丢失、删除等同空值、hint 接口未接通、Merkle 索引未更新及后台任务未连接等问题。新增持久记录层、网络协调器、故障域放置、SQLite hint 日志和双向修复；加入请求容量预留及完整关闭清理，并补充真实 loopback gRPC 回归。更新中英文 README、设计文档和依赖声明。

详细证据与兼容性见 [第 07 章审查记录](key-value-store/docs/chapter07-review.md)。自动扩缩容/安全下线、Merkle 网络逐层遍历与大差异分页等尚未完成，因此不增加已完成项目数，也不把章节标记为全部实现。

第 07 章本轮验证：完整测试 **383 passed（34.75 秒）**，Ruff E9/F 和 `git diff --check` 通过；未做吞吐压测，未提交或推送。

### 第 08 章：分布式唯一 ID

对照原书最终 Snowflake 方案，补充配置的严格整数校验、序列耗尽时的让出 CPU 与单调时间截止期限、等待中的回拨拒绝。超时和回拨不提交序列状态。新增配置错误、超时恢复、回拨和多线程共享实例回归；完整测试 **94 passed**，Ruff E9/F 通过。

本机单线程 100,000 次 generate 耗时 0.4861 秒，100,000 个 ID 无重复，约 205,710 ID/s；这是当前机器一次测量，未证明生产环境持续容量。中英文 README 和设计文档关联 [章节核对记录](unique-id-generator/docs/chapter08-review.md)，明确重启身份隔离、批量部分失败、时钟同步及高可用部署边界。

### 第 09 章：短链接服务

补齐 SQLite 事务持久化与唯一约束、有限容量 LRU 回源缓存、WSGI POST 和 HTTP 301/302 重定向入口。内存与 SQLite 存储拒绝覆盖已有映射；本地 ID 分配加锁，Base62 参数校验及 HTTP URL 输入限制。新增共享数据库并发去重、重启读取、冲突保留旧映射、HTTP 和缓存命中回归。完整测试 **109 passed（3.01 秒）**；Ruff E9/F 通过。中英文 README 和设计文档同步关联 [章节核对记录](url-shortener/docs/chapter09-review.md)。

尚未部署负载均衡、分片/复制、全局限流和分析服务；本地 ID 分配不提供跨实例唯一性。未做生产吞吐或海量容量验证。

### 第 10 章：网页爬虫

继续完成持久调度恢复、原子 HTML 快照及去重、有限窗口主机调度、有界重试和自适应周期重爬；接通一致性哈希分片与持久投递回调。补齐有界 DNS TTL/LRU、保留 Host/SNI 的 HTTP(S) 连接、响应大小与总 deadline、重定向重新检查 robots。修复相同 HTML 不同基准 URL 的链接漏发现、完成确认与礼貌时间非原子、并发/取消时任务及内容声明处理。

完整测试 **213 passed（12.69 秒）**，Ruff E9/F 通过。中英文 README 与设计文档同步；详见 [章节核对](web-crawler/docs/chapter10-review.md)。五年保留清理、复制/分片控制面、拓扑安全移交、动态渲染及互联网规模容量均明确为部署/扩展边界，无生产容量承诺。


### 第 11 章：通知系统

原书核对后接通持久接受、租约领取/续租/恢复、队列延迟重试和审计；事件 ID 唯一账本与接受任务同事务，终态后保留，可跨实例与重启去重。修复模板正文未应用、端点快照与平台多设备、worker/provider/claim 异常退出、队列过载丢重试、取消领取竞态和旧凭据复活任务。混合设备失败中永久拒绝独立记录，暂时失败继续重试。接受和投递前检查退订，HTTP 示例具有鉴权并使用持续事件循环及 live worker。

完整项目测试 **447 passed（47.79 秒）**，Ruff E9/F、格式和 git diff --check 通过；中英文 README、设计和 [章节核对](notification-system/docs/chapter11-review.md) 已同步。Provider 使用模拟实现，不做真实第三方通知发送；SQLite 是本地至少一次参考方案，联系人/偏好/模板和跨进程限流、供应商幂等、自动扩容与容灾仍需部署集成。未宣称千万条日容量或恰好一次送达。


### 第 12 章：新闻动态系统

对照原书并结合 code-review-graph 调用/影响分析，补齐原先缺失的读取服务、NewsFeedAPI 和真实本地 HTTP 入口。SQLite 保存帖子、作者资料、关注及静音；帖子与待分发标记同事务接受，后台公平重放。读取合并普通作者推送与明星拉取，稳定游标、内容组装、隐私复核和冷热窗口修复完整接通。

移除 worker 无界中转，队列容量包括延迟与执行中任务；增加有限重试、旧凭据校验、幂等 Feed ID、取消归还和并发关闭。缓存、限流与计数增加同步，社交缓存加载合并和失效栅栏，作者及通知验证使用固定数量协程。2026-10-29再次按原章复核，补齐[需求](news-feed-system/docs/requirements_cn.md)/[任务](news-feed-system/docs/tasks_cn.md)及英文对应文档；内容和资料加载统一为保序固定协程，条带锁合并资料未命中，新增本地API计数/QPS/耗时快照，并修复旧Python的延迟队列超时异常捕获。完整回归 **282 passed、无跳过（-W error）**，Ruff E9/F、源码/新增测试格式及差异检查通过。详见[章节核对](news-feed-system/docs/chapter12-review.md)。

SQLite/outbox 提供本地源恢复，内存队列完成不具备持久 worker 确认语义；普通热 Feed 最终一致，通知仍为本地记录。共享缓存/队列、真实推送、CDN、分片复制、多机房及千万日活容量均为部署边界。索引已增量更新，新文件可查询；Python动态调用仍有缺口，空流程或测试关系缺失均以源码审查和执行测试补核。


### 第 13 章：聊天系统

对照原书并结合 code-review-graph 调用/上下文分析，修复 SQLite 跨实例事务竞态、私聊全表扫描、群容量和重试去重竞争。新增按 ID 的收件人授权查询；网络读写使用异步存储包装与流式字节预算。本地路由和在线状态采用用户/订阅反向索引，令牌按摘要查询，1500会话溢出级联改为迭代处理；撤销凭据投递、全服务推送并发、重复取消与发送顺序均有回归。

分布式节点的启动、接入和关闭统一管理任务所有权；推送续租和确认/重试/死信写入校验当前凭据，防止旧消费者覆盖。客户端总期限、取消关闭、完整页确认、原子游标写入和群创建不确定结果保护已接通；中英文 README、设计、示例和[章节核对](chat-system/docs/chapter13-review.md)同步更新。

最终使用新建独占 WSL Redis 实例运行完整回归 **171 passed，1 skipped（7.21秒）**，实验结束清理自有进程。唯一跳过项是需要独立受控环境的 Sentinel 主库崩溃实验；本轮未重跑千连接容量报告。单 Redis 哈希槽、多机房/容量、实际移动推送、外部身份管理和长期归档均有明确边界。


### 第 14 章：搜索自动补全

对照原书及 code-review-graph 导入、调用、长函数和影响分析，补齐稳定事件采样、
持久窗口聚合、可停止周期 Worker 及独立源清理。跨实例事件接收使用写事务；WAL
读取固定输入，Trie/JSON 构建移出在线写锁。构建票据和过滤修订阻止旧发布覆盖新
数据；屏蔽集合按修订缓存，冷加载分段合并，缓存命中也复核过滤版本。

Trie 完全冻结，过滤补位使用排名上界剪枝；HTTP 限制全部未结束分片任务，共享
排队/副本切换预算，慢响应可中断，停止时等待处理器和自有任务。完整回归
**170 passed（8.73秒）**，Ruff E9/F、格式和差异检查通过，两个 README 示例成功。
本地顺序测量 HTTP P99 26.17ms，不代表生产容量或吞吐提升。中英文 README、
设计和[章节核对](search-autocomplete-system/docs/chapter14-review.md)已更新。

浏览器保留 no-store，未采用会遮蔽即时移除的一小时缓存优化。历史服务快照和去重
凭据需另设保留；SQLite/Store 等待及系统 DNS 可能超过请求期限。多主机存储 HA、
实时热点、多语言、地域/CDN 和生产容量属于部署/后续范围。未提交或推送。


### 第 15 章：视频系统

对照原书及 code-review-graph，补齐不可变 Pipeline 配置、持久 GOP 预处理与复用、
独立元数据更新、有界并行和能力筛选。拆出调度、SQL 与生命周期模块；第一次领取
持久固定任务重试上限，锁后读取时间、慢输出检查后重新复核租约，心跳覆盖整个
下载/处理/上传过程。永久源错误检查全部帧，迟到结果不能恢复已下架视频。

分片写入、冷缓存读取、源组装和 outbox 检查移出全局锁，最终接受与 GC 复核；
过期先提交再删对象，GOP 中间块完成后可回收。HTTP 拆分路由、严格 framing/声明、
套接字期限和关闭排空；S3 下载合并、本地损坏校验及 PostgreSQL 事务所有权补齐。

新建独占 WSL PostgreSQL 18 UTF8 与 Moto S3，完整回归 **160 passed（48.16秒），
无跳过**，真实 FFmpeg、独立进程接管、黑洞重连、未知提交、迁移均执行；结束清理
自有服务。Ruff E9/F、格式、Python3.9语法和差异检查通过。中英文 README、设计、
实测及[章节记录](video-streaming-system/docs/chapter15-review.md)已同步。

本轮未重跑 Patroni 提升/真实 S3 HA/物理机分区。全局事务锁、部分最终存储检查和
保守 GC 仍有吞吐边界；CDN/国际上传中心、逐 GOP 分布式编码、GPU、KMS 和商用
DRM 为部署/后续范围。单 Worker 短视频与十次回环读取不代表生产容量。未提交或推送。


### 第16章：文件同步

对照原书与code-review-graph，补齐简单上传、上传中/取消通知、历史条数限制和有界
自动扫描同步；拆分声明、路径、SQL、生命周期、通知和客户端传输。大块I/O与全
文件校验移出全局锁，最终事务复核权限/会话/引用，GC先提交过期再物理删除。
客户端独占目录、绑定工作区、有界明文磁盘缓存、整页确认并保护晚编辑/冲突副本。

完整回归 **116 passed（21.08秒）**，含真实HTTP、独立设备进程、跨进程块锁、
自动双设备创建/修改/删除与离线重开。Ruff、格式、Python3.9语法及差异检查通过，
中英文README、设计和[章节记录](file-sync-system/docs/chapter16-review.md)已同步。
仅单主机SQLite/块存储；云复制、元数据HA、生产保留/重新快照与原生移动监控尚为
扩展，未实传10GiB、未证明生产吞吐。本轮未提交或推送。


### 第17章：附近商家

依据原书与code-review-graph核对，补齐无读流量的后台每日发布，将JSON/geohash构建
移出写事务，以head/draft_revision/日期复核安装；构建期间可读取旧完整版本。副本
拒绝相同版本的不同内容，冻结导入头部，迁移保留既有五表和已发布版本。

拆出缓存与模型校验；固定条带合并冷加载、热命中不等待慢加载，TTL从成功完成起算。
详情缓存不可变JSON文本，返回独立对象，候选详情每批256条读取。稳定球面距离与
严格游标校验补齐。HTTP线程、正文、网关准入和总期限有界，区域内故障切换，关闭
取消活跃网络工作并回收；拒绝截断响应、歧义framing和重定向凭据转发。

完整回归 **76项通过（17.436秒）**，含真实HTTP、独立主/双副本/网关及重启，
25,920个几何候选点与独立参考核对。Ruff、格式、Python3.10语法及本地链接检查通过。
中英文README、设计、任务、[章节记录](proximity-service/docs/chapter17-review.md)
和带日期[测量报告](proximity-service/benchmarks/results.md)同步。

逻辑分片、进程内缓存和全量快照仍有容量边界，未部署云地域、Redis集群、数据库HA
或验证书中2亿商家/5000QPS。密集查询仍扫描排序所有候选；本机小样本不证明生产
吞吐提升。套接字期限不能强制打断系统DNS或数据库/CPU工作。本轮未提交或推送。


### 第18章：附近好友

对照原书及code-review-graph，拆出models/routing/schema/protocol/work，严格校验
声明与路由，保留core导入。拿到写锁后校验报告时间并起算接收TTL；一致读只采样
一次时间。原地增加过期/反向好友索引，移除每次位置更新的全局清理，由后台扫除。

Redis故障直接SQL降级，缺失值每128条pipeline修复，预算在批次间检查；有界连接池
和broker客户端租约/LRU，关闭先等待活动I/O。可选网络操作后重新SQL复核权限、
TTL和双方位置；Redis镜像命中不会绕过SQL。默认256应用会话/16同步提交，取消
仍持有底层操作和容量直到退出；关闭停止准入、回收订阅/维护和线程。分页保留实时
增量基线，整批多帧通知共用发送期限，避免丢leave/更新或按帧放大等待。

使用真实Redis8.0.5与WebSocket，完整 **76项通过（21.360秒），无跳过**；包含
双节点/双Redis进程、故障/重连/重订阅、真实Lua版本保护与损坏值修复、SQLite
锁竞争/回滚和优雅关闭。Ruff、格式、Python3.10语法、本地文档链接及差异检查通过。
中英文README、设计/任务、[章节记录](nearby-friends-system/docs/chapter18-review.md)
和带日期[测量报告](nearby-friends-system/benchmarks/results.md)同步。

核心是单主机参考：共享SQLite、双SQL读取和每会话全量好友对账仍有成本。位置/
历史物理分片、Redis主备、共识发现、跨地域、生产身份/TLS及原书规模尚待实现；
陌生人模式为额外功能，Erlang为替代路线。应用会话上限不是TCP硬配额，DNS/SQLite
不可强行终止；有限本机扇出不证明生产容量。本轮未提交或推送。


### 第19章：地图系统

对照原书与code-review-graph，补齐逐段指令/polyline、邻居瓦片URL、三分类路由层级、
持久预生成瓦片和主动导航/SSE。路径仍准确细图Dijkstra，层级视图不冒充已验证的
shortcut加速；候选仍为有界单边排除。归档不可变、校验和/长度先行、原子有界批次。
私有导航以区域筛选/定期全量重算修复路况变化、到期及更快路径，CAS拒绝迟到结果。

拆分模型/目录/SQL/遥测/缓存/传输，图构建移出写锁；保留UUID哈希历史防ABA复用，
相同revision校验完整内容。锁后接收时间、索引/用户级清理、有效新样本与封路栅栏
修复交通链路。邻接缓存不可变JSON，冷加载合并且返回独立对象。HTTP有界准入与
绝对网络期限，区域内重试、复制取消排空；绑定后启动后台任务，关闭等待已接工作。

完整 **113项通过（12.918秒），无跳过**，含225组最短路oracle、真实主/副本/网关、
SSE与预生成重启复用。两个示例成功；900节点/3480边、4客户端60次HTTP零错误，
51.31混合QPS，小样本不证明生产容量或相对历史版本稳定提速。Ruff、格式、
Python3.10语法、本地链接和差异检查通过。中英文README、设计、任务、
[章节记录](maps-system/docs/chapter19-review.md)及[实测报告](maps-system/benchmarks/results.md)同步。

当前是合成道路/单主机SQLite参考。预生成归档与目录跨DB不能原子绑定head，但不会
混代；旧代需单独GC。缓存按条目而非RSS限制。真实道路、公交/转弯规则、ML ETA、
层级加速、物理分片、流式管线、云/CDN/写主HA和原书规模尚待实现；DNS/SQL/CPU
不能被套接字期限强制终止。本轮未提交或推送。

### 第20章：分布式消息队列

对照原书8020–8996行，补齐自动大小/linger攒批、有界Future、每条key分区分组、
未确认原批次恢复、tags过滤及扫描游标、主动退组、只增分区、绝对定时消息和长轮询。
低层隐式写入拒绝跨分区key批次；无key低层稳定后备便于重试，SDK随机选择显式目标。

拆分消费组、复制、保留、schema、共享消息契约和传输职责。移除协调器/broker包住
不同分区I/O的全局锁；按顺序获取固定条带、短SQL事务原子发布。相同producer身份
仍串行用于epoch隔离。日志整批验证、按段批刷盘、单调committed checkpoint、失败关闭
及快照安全换代；物理保留失败持久化重试。HTTP有界准入/绝对I/O预算、严格协议和关闭排空，
并修复Windows超载503因未读正文被RST覆盖的问题，保持严格断言。

最终 **116项通过（34.735秒），无跳过/ResourceWarning**。旧故障demo与新章节demo均通过；
3个独立磁盘broker、1协调器、3透明TCP代理实际测量120/120条提交并读回，
75.79条/秒、零错误，batch ACK p50/p95为253.544/266.547ms。该单次小样本不证明
生产容量，也不与历史并发测量推断稳定提速比。Ruff、32文件格式/Python3.10语法、
链接及差异检查通过。中英文README/设计、本地需求/任务、
[章节记录](distributed-message-queue/docs/chapter20-review.md)和
[实测报告](distributed-message-queue/benchmarks/results.md)同步。

code-review-graph全量及最终增量更新成功；7个既有核心文件影响分析102个直接改动节点、
24个受影响节点、额外5个文件，1933调用未解析。动态dispatch及新未跟踪模块有索引缺口，
已用源码/实际回归补核，不以空图边作为正确性证明。

仍为单协调器/SQLite单写者、静态broker及有限全量catchup参考。共识/HA、动态成员、
副本迁移/分区缩容、增量复制、完整ISR、zero-copy、云/ACL/配额和持续规模/掉电验收未完成。
Socket期限不能中断已接受的DNS/SQL/CPU工作。本轮未提交或推送。

### 第21章：监控与告警系统

对照原书8997–9663行，补齐按metric/series分区的采集与传输、分区FIFO lease claim、
持久积压字节计数和有界Dispatcher。每分区保留未知发送结果的原负载/序号，消费者轮流
处理组分配。ScrapeScheduler支持有界并行、目标间隔/重试总预算、错误重载保留有效配置，
静态一致性哈希让相同成员配置下的collector归属互斥；不冒充共识或动态接管。

拆分存储schema/query/rollup/archive及告警配置/评估/投递/provider。摄取输入锁前冻结、
时间锁后校验；dirty桶在一致快照上写锁外计算，以revision校验短事务发布。缓存合并冷加载并
隔离返回值，gauge跨序列avg/min/max按样本数加权。归档结构/身份/范围及压缩解压预算有界，
失败只清理同身份新文件。JSON规则原子导入、静态severity不改selector，通知claim计尝试并隔离
回写。HTTP严格协议、有界准入/期限，后台启动及关闭排空可观察，临时存储故障不误提交游标。

最终 **108项通过（26.333秒），无跳过，ResourceWarning严格**；基线15项20.412秒。
原13进程demo成功，push到可查559.953ms、webhook两次尝试一条唯一通知。新14进程demo
验证2个collector对4个目标各归属一次、3个队列分区均活跃、量规平均40/count3、push重放
相同响应及自有webhook severity=page。100序列/6000样本、4客户端40次固定范围HTTP测量
冷228.463ms、warm p50/p95 65.693/90.315ms、56.76 QPS、零错误；此测量与第22章基线
11项测试重叠，存在主机竞争，不推断受控提速或生产能力。中英文README/设计、本地需求/任务、
[章节记录](metrics-monitoring-system/docs/chapter21-review.md)及
[验证报告](metrics-monitoring-system/benchmarks/results.md)同步。图增量刷新24文件无构建错误；
本章6核心文件影响60个直接改动节点/33个受影响节点/额外6文件，761调用未解析、边截断。
新未跟踪collector.py明确未索引，动态边/新模块以源码和实际执行补核，不以空图边证明正确性。

SQLite是有界参考引擎，未采用真实专业TSDB；告警使用本地durable delivery表，非Kafka
事件总线。共识发现/动态failover、压缩/分片/复制、元数据HA、优先级流处理、完整PromQL/
Grafana、TLS/ACL、商业provider、云冷存储、GC/备份和千万序列持续/掉电验收仍待扩展。
实际通知仅自有localhost webhook；不宣称外部商业渠道已投递。本轮未提交或推送。

### 第22章：广告点击事件聚合

对照原书9664–10506行，核查事件时间分钟桶、M分钟过滤计数/精确全局Top N、热点加盐、
迟到修正、原始重放和独立批对账。拆分records/schema/checkpoints、共享查询池、recovery及
HTTP/worker生命周期。事件锁前冻结规范化，接收时间锁后采样；完整验证历史重放而非仅按
旧seq跳过。计数/去重/输入位点同事务，新增位点回执校验，旧库历史校验能力明确保留迁移限制。

Tailer有OS互斥、唯一临时文件/原子检查点、同文件及ACK/lineage检查、完整行和单次批预算。
API拥有默认8线程/8查询/5秒共享期限的QueryService，归并全部候选再heap选择，验证worker身份/
数值，故障不返回部分排名；已修worker候选预算422被误映射503。replay/reconcile验证固定范围页，
默认60秒/20万事件，独立维度上限10万；audit流式编码最多100万行。HTTP有界准入、严格协议和
资源排空，worker绑定后启动，故障可见重试；socket期限不强杀DNS/SQL/CPU。

最终 **76项通过（22.816秒），无跳过/ResourceWarning**。真实6进程demo100唯一/100重复，
hot80/other20、lag全零且对账一致。最终独立3000事件实测发布5631.68条/秒、处理完成0.778秒；
40查询4客户端p50/p95为60.902/78.452ms、65.8 QPS，全部点击及完整维度对账一致。
不将单次小样本视为稳定提速或生产容量。中英文README/设计、本地需求/任务、
[章节记录](ad-click-aggregation-system/docs/chapter22-review.md)和
[验证报告](ad-click-aggregation-system/benchmarks/results.md)同步，保留Oct20历史数据。

code-review-graph最终增量刷新成功；7核心文件57直接节点/25受影响/额外5文件，
1244调用未解析、边截断；新未跟踪query_service.py明确未索引，以源码/独立复核及执行补核。
仍是单日志/SQLite、固定分区，无复制/机器容灾/第二输出队列/分布式事务/自动重分区，
不是金融结算端到端exactly-once；云归档、隐私保留、欺诈识别、TLS/租户与十亿事件验收仍待扩展。
本轮未提交或推送。

### 第23章：酒店预订系统

对照原书10507–11226行，补齐酒店内房型日期/数量筛选和显式日历策略：默认未来730个UTC日，
每轮32策略/4096日期，仅填缺行、保留人工每日价格与占用。持久cursor可续跑，同配置PUT不重置
进度；generation/version/cursor防止删除重建ABA发布旧默认，停用/缩短horizon不清已有日历。

Store拆schema/catalog/bookings/saga/cache，输入writer前冻结、fresh事务writer后统一采时；
cache的revision/active/data同快照，冷加载合并且编解码不占LRU全局锁。保留多夜库存/价格快照/
幂等/支付意图同事务和向下取整超售约束，取消释放缺夜failclosed。支付job/receipt严格契约并
核对durable method，未首次尝试但过期的订单直接释放不发起charge；未知结果保持重试并可补偿退款。
Worker有总预算/cancel/close排空，网关显式路由、正文前角色鉴权、严格协议和有界准入，
CLI bind后启动维护。所有支付均为独立本地模拟账本，没有真实资金操作。

最终 **99项通过（22.901秒），无跳过/ResourceWarning**；基线13项18.699秒。原6进程demo
确认/退款成功；新demo后台730天×2房型、策略权限、数量筛选、44000价格快照、同ID重放及退款后
库存11。100热点请求/16客户端独立测量准确接受22、容量冲突78，两晚库存22、模拟unique charge22；
77.7是尝试请求/秒，结算成功8.65笔/秒，非77.7笔成功预订/秒。p50/p95为36.196/666.458ms。
单次小样本不证明稳定提速或生产容量；SQLite文件体积为容忍临时sidecar消失的近似采样。

中英文README/设计、本地需求/任务、[章节记录](hotel-reservation-system/docs/chapter23-review.md)
和[执行报告](hotel-reservation-system/benchmarks/results.md)同步，保留Oct20历史数据。
code-review-graph更新21文件无错误；6既有核心文件72直接节点/16受影响/额外4文件，
2122调用未解析、边截断；新calendar_policy.py未索引，已用源码/独立复核/实际执行补核。

仍是固定分片/SQLite单writer，无复制HA、在线扩分片、真实Redis/CDC、真实行锁/OCC后端、
当地时区/取消政策/税费/多币种、真实支付、员工ACL/完整UI、隐私GC与5000酒店/百万房间验收。
Socket期限不能强杀DNS/SQL/CPU或任意自定义适配器。当前未提交或推送。

### 第24章：分布式邮件服务

对照原书11227–11924行，补齐四固定文件夹统计/查询、按本人过滤的持久化变更通知，
最多100事件/页、2秒长轮询，lineage+position续取；旧副本无历史日志返回409要求refresh。
可选min_position/lineage读屏障，落后503/更换409；未带屏障仍允许陈旧副本回退。
按用户/筛选/精确水位绑定的进程内LRU最多128项/4MiB、返回值隔离，非真实Redis。

拆records/replication/spool/transport/routes/runtime；冻结输入，副本完整历史/shape/hash/
身份和版本校验，数据/索引/log/checkpoint同事务。sender30秒lease/20秒I/O预算、
receiver90秒lease/60秒预算，writer后采时，token/expiry栅栏；SMTP AUTH为local-worker。
HTTP/SMTP各默认32并发，direct RPC不proxy/redirect，严格JSON/framing、总deadline/cancel和
排空。Windows超时socket读取需底层关闭以即时取消，已有实际测试。MIME严格解码边界，
嵌套邮件容器附件拒绝。对象有界校验和读、receipt校验及64条带锁解决重复发布竞争。

最终96项通过30.741秒（-Werror，无跳过）。新增九进程demo通知342.560ms，唯一入箱、
同发送ID、未读计数、副本版本2屏障、续取2/3、其他用户零事件；原demo535.690ms。
40封/4客户端admission39.93条/秒，全部投递并副本可查8.04条/秒，40条唯一投递与匹配；
重复搜索p50/p95 43.571/58.355ms、93.04QPS。小样本非受控提速/生产容量验证。

中英文README/设计、需求/任务、[章节记录](distributed-email-service/docs/chapter24-review.md)
及[实测报告](distributed-email-service/benchmarks/results.md)已同步，保留Oct20历史。
图更新36文件无错误，6核心64直接/11影响/额外2文件，163边截断/1378未解析；
未跟踪mailbox_views.py未索引，源码/独立复核/实际执行补核。真实AV/垃圾与投递反馈、
Cc/Bcc/线程、完整文件夹、公网SMTP/MX/认证安全、共识切主/对象spool复制、备份/保留及
十亿用户跨机验收仍未实现。不把本地参考实现称为章节生产能力全部完成。未提交或推送。

### 第25章：类S3对象存储

对照原书11925–12813行，补齐本人桶列表、delimiter=/同层对象/common-prefixes列举，
默认保留递归；对象和前缀共同计页，token绑定owner/bucket/prefix/versions/limit/delimiter。
旧快照schema/递归token兼容。每用户1000桶，全局128/每用户32活动快照、每快照8MiB，
prefix过滤前聚合最多1万行，单页不存无用快照，预算不足明确失败不返回部分结果。

拆catalog/listing/metadata contracts/chunk I/O/runtime/routes/transport。输入writer前冻结，
writer后采时，引用扫描流式预算。单协调器8在途操作/12复制线程/64上传锁条带，HTTP32准入；
普通读/发布共享，GC/删桶/list capture独占。Node共享读写优先，追加/索引/CURRENT不确定时
needs_recovery，验证恢复后继续；短写/fsync失败回滚尾，阻止后续ACK被恢复截断。

交叉复核发现RPC超时早于远端handler结束，旧collect可回收新块、迟到metadata可形成悬挂引用。
新增durable publication intent：先落盘再发布，校验ACK后finish，GC前重放全部pending；
超过128/损坏/恢复失败停止回收。refs前捕获各node随机gc_generation，PUT含duplicate及
recover/compact/collect都旋转，获得writer后校验旧代际409不删。repair可导致本轮
collection_deferred，下轮完整重扫，不刷新代际偷偷接受旧快照。删桶事务同时拒绝pending
intent，防恢复发布到已删除桶。旧未知操作无intent可追溯，升级前须排空旧请求。

最终137项通过40.784秒（-Werror，无跳过）；基线10项29.840秒。新六进程demo验证本人桶1/
他人0、CA/NY前缀+federal.txt冻结分页、递归2匹配、2部分并发、并发complete同版本、
完成后abort409、旧collect409及字节一致；原demo百万字节multipart/历史版本/单节点停机
读取均匹配、3986.690ms。最终4客户端40对象PUT38.23次/秒，p50/p95 99.655/124.262ms，
GET76.98次/秒、48.046/69.440ms，40packedchunks/node、166310bytes/node、0unrecoverable。
新增catalog意图有持久写入成本；非受控提速/持续容量或故障域SLA证明。

中英文README/设计、需求/任务、[章节记录](object-storage-system/docs/chapter25-review.md)
与[实测报告](object-storage-system/benchmarks/results.md)同步，保留Oct20历史。图刷新15文件后
再6文件、删桶保护最终2文件无错误；6既有核心79直接/20影响/额外5文件，344边截断、
2093调用未解析，新未跟踪chunk_io.py未索引，以源码/独立交叉复核/执行补核。
两章共62个Python3.10 AST、Ruff/format通过，20篇本地Markdown零缺失链接、diffcheck通过。

仍是单协调器/固定放置、无心跳共识、独立AZ/目录元数据HA、多pack滚动/每核WAL、GB流式、
纠删码、自动跨机修复、在线迁移、IAM/TLS/加密/备份保留和100PB/6nines/4nines实证。
未使用真实AWS或外部云；未提交或推送，保留此前章节工作。

### 第26章：实时游戏排行榜（2026-10-27）

原书12814–13481行核心Top10/同分竞赛排名/个人位置和前后4行已覆盖。
拆journal contracts/RESP/Lua/ranking/cache/runtime/transport；source输入锁前冻结、writer后
统一采时，首次新月批量补零，普通胜局不扫描全玩家。资料缓存1024项/1MiB按revision绑定，
改名失效，积分Top10不缓存。8在途操作/16RPC线程，排名共享源读，写/重建/对账独占；
分片/精确heads/稳定epoch校验贯穿全部阶段，旧reset/poll被隔离，不返回混合榜。
HTTP32、严格framing/JSON、direct RPC/RESP总deadline/cancel与线程排空。

72项通过39.110秒（-Werror、无跳过）；HEAD隔离基线8项38.686秒。
真实Redis8.0.5/WSL七进程：crash/manualpromotion/AOF、FLUSH重放/对账、Lua写前校验/
旧epoch拒绝及空轮询AOF不增长均通过。演示742.600ms、rank[1,1,3,3]、重复去重与对账一致。
60逐笔投影7.36胜局/秒、p50/p95 141.863/197.049ms；4客户端40请求Top10 32.93QPS/
118.700/146.256ms，个人邻居18.68QPS/207.717/240.098ms；非受控提速或25M/2500QPS证明。
[章节记录](gaming-leaderboard-system/docs/chapter26-review.md)、中英文README/设计/需求/任务与实测已同步。

### 第27章：支付系统（2026-10-27）

原书13482–14170行，补1..20订单可信checkout及pay-in查询别名；内部令牌才能跨商户编排，
受理操作/任务SQL原子、固定ID幂等，各外部授权/结算独立。拆schema/operations/jobs/ledger/
reconciliation/checkouts/contracts；冻结输入、writer后单clock。账本重放核对全分录，
provider revision指纹跨UNKNOWN保留；provider交易ID跨本地操作唯一，冲突入复核不入账。

默认4执行线程、30秒claim/20秒总I/O，PSP明确404才用原身份创建，回执必须匹配当前job。
finish校验token及到期，通知ACK匹配事件；PSP回调15秒lease/5秒预算，旧ACK不覆盖新claim。
对账验证完整摘要/去重/receipt、live租约与已完成结果；差异不自动改余额。
HTTP32/15秒、普通body1MiB/statement8MiB、无proxy/redirect/严格JSON，bind后后台启动与取消排空。

48项通过21.561秒（-Werror，无跳过），基线13项16.541秒；5项真实四进程集成。
新演示两订单SUCCEEDED/FAILED、同ID重试、出款/退款后500可用/0冻结、另一商户0、5分录/
4PSP操作及对账一致，原演示5000/5分录通过。最终20笔/4客户端2.996590秒，
6.674253笔完成/秒，p50/p95 537.585/697.339ms；20唯一PSP/20消费者事件、回调待发0，
审计一致。计时不含启动及最终排空，不证明生产容量，仍低于原书10TPS示例。
[章节记录](payment-system/docs/chapter27-review.md)、中英文README/设计/需求/任务/实测已同步，保留Oct21历史。

两章初步code-review-graph缓存上下文可用（6863nodes/62421edges/544files，Oct26 20:03:03），
最终build/impact被工具审批策略拒绝：requires approval,but approval policy is never。
没有宣称图已刷新或新模块已索引，已用源码调用链和实际执行补核，未绕过审批。
两章63个Python3.10 AST、Ruff/format与文档链接/diff检查验收；未提交或推送。

仍有单authority/SQLite与手工Redis切主、固定分片/规模/保留等限制；支付全部模拟，
无真实PSP/AML/反欺诈/PCI认证/银行结算/HA/FX税费拒付/复核结案与流式对账。
历史未记录revision无法追溯，旧重复provider绑定升级会失败，需先审计。

### 第28章：数字钱包（2026-10-27）

阅读原书14171–15038行；下一章第29章Stock Exchange。固定单权威/2账户分片/3日志副本，全部合成余额。
补POST/GET wait_ms=0..2000终态完成通知，8等待者不持资金写锁，超时/取消保持原ID和未决资金；重启查询恢复。
拆contracts/纯projection/runtime/audit/routes/transport/server_runtime，旧导入兼容；输入在等锁前冻结，回执核对完整意图/角色/状态。
追加/sync共用写锁，3RPC并行，精确提交已确认前缀，异常适配器仍排空；副本索引尾部/重复查询，协调者已提交事实增量缓存。
余额/seq/审计固定读事务；唯一临时文件/独立发布锁/有限Windows替换重试修复并发快照WinError5。
资金步骤/审计仍串行，查询可并行，命令轮转；32连接/16操作/8等待/3RPC，统一I/O截止时间、严格协议、无代理/重定向、取消排空。
账户属主/初始余额/分片数持久绑定；旧库首次升级前配置不可追溯。CLI绑定/关闭异常清理已验证。

最终54项/66.208秒（-Werror、无跳过），7项真实七进程集成；基线14项/56.549秒。
演示700在途守恒、重启后终态，最终9000/10300/10700与0冻结，对账一致，含实验生命周期5.966893秒。
20笔/4客户端混合同片跨片：基线1.091笔/秒、P50/P95 3644.260/3812.493ms；
本轮轮询1.517笔/秒、978.709/4910.774ms；有界等待1.566笔/秒、1107.182/4793.258ms。
吞吐提升但P95退步，未宣称全面提速；顺序小样本不证明生产容量。保留基线、探索性样本、最终原始JSON及Oct21历史。

code-review-graph本轮成功刷新，最终源码刷新2026-10-27T14:11:48/errors=[]；八文件影响92直接节点/23影响节点/5附加文件，
1662未解析调用且边截断；新未跟踪模块contracts查询明确未索引，源码/执行测试补核，不把零结果当无依赖。
[审查映射](digital-wallet-system/docs/chapter28-review.md)、英/中README/设计/需求/任务/实测与根状态同步。
28个Python3.10 AST/Ruff/format与文档链接/diff检查通过；未暂存、提交或推送。

剩余：真正Raft/自动选主、账户事件复制/原盘丢失提升、独立CQRS读副本、快照加速/裁剪、分片迁移、TLS/身份及生产规模。
100账户/1000命令、完整日志/JSON投影/全量审计/串行资金权威不适用于百万TPS；未连接银行/真实资金。

### 第29章：证券交易所（2026-10-27）

原书15039–15986行；第30章是学习建议/收尾，无下一个系统实现项目。仅模拟账户/持仓/时钟，无外部交易。
补原书/v1/order、L2/candles别名，/execution按股票/订单/时间认证筛选；K线按完整桶起点筛选，连续execution_seq双边共享。
价格堆/聚合价位/FIFO双向节点替代历史订单扫描排序；链接O(1)，价格堆O(logP)，稀疏整理偶尔O(PlogP)，全量复制/JSON/审计仍O(N)。
拆contracts/policies/order_book/audit/publication/queries/admission/routes/transport/server_runtime，兼容旧导出；输入冻结在等锁前。
索引尾部/重复查找/后缀查询，账户/订单/请求只传目标对象；固定SQL读快照，行情轮询不持读锁等待网络。
逐账户现金/股份流量、成交量、FIFO/堆审计，投影漂移阻断后续撮合；事件/投影/批次原子，旧数据先验证再升级，派生批次重建。
提升对照完整定序日志重放状态；发布前后epoch复查，消费者核验定序前缀。行情倒退/冲突/错误数量拒绝，原子游标/报告/重复指纹。
HTTP32/15秒、公开网关16、每账户100提交/秒突发100；RPC2秒/轮询5秒、1MiB请求/8MiB响应，严格协议、无代理/重定向。
绑定后才启动非daemon轮询，stop/close/join排空；Windows实验清理WinError32仅在自身已验证临时目录有限重试。

56项/25.013秒（-Werror，无跳过），5项真实四进程集成；基线13项/17.226秒。
功能演示停主提升epoch2，成交5+3、序号1/2、买方999200/0冻结，现金3000000/各股票3000与重放一致，含生命周期3.304018秒。
20主动单/4客户端基线12.988单/秒、P50/P95/P99 272.819/370.592/371.557ms；
本轮17.106单/秒、190.137/300.240/377.021ms。吞吐/P50/P95改善但P99略升，20点p99是最大值，非生产证明。

code-review-graph构建/影响查询成功，最近源码刷新2026-10-27T15:19:47/errors=[]；8文件范围86直接节点/25影响节点/5附加文件，
1143未解析调用及边截断。order_book新未跟踪模块明确未索引，源码/AST/Ruff/执行测试补核，不把零结果当无依赖。
[章节映射](stock-exchange-system/docs/chapter29-review.md)、英/中README/设计/需求/任务/实测与根状态已更新。
31个Python3.10 AST/Ruff/format及文档链接/diff检查通过。未暂存、提交或推送。

剩余：定序器复制/真正共识选举/跨机HA、独立出站事件总线、mmap/SBE/FIX/CPU绑核、等时多播/L3、分片购买力、快照加速/裁剪及真实日历/清算。
100账户/股票、1000指令、全量状态与日志限制仍在，十亿订单/日/低毫秒尾延迟/四个九未证明；不支持混合reducer滚动升级。

### 第2章再次复审（2026-10-27）

原书26–523行，scale-from-zero。拆contracts/profiles/sessions/jobs/replication/consumer/schema/runtime/routes/transport；旧导入兼容。
输入在写锁前冻结、单clock；claim5秒/ACK同时查到期/token/原payload/source，长用户ID可用，消费按源库隔离。
复制绑定源ID/after前缀/完整有界后缀和逐用户版本，整批与绑定原子；历史immutable，旧身份首次绑定不可追溯。
缓存32/512KiB/1000版本下限、实例代际填充令牌，拒绝失效前填充/低版本；默认最终一致，失效失败/重启/元数据逐出仍有边界。
Web池1–8预配置节点可在线增减，保存每请求候选快照；edge失效及客户端回源。HTTP32/15秒、64KiB/8MiB，严格协议、无代理/重定向、non-daemon排空。
66项/51.08秒-Werror无跳过，基线19项/33.87秒；旧unittest只执行11项，完整pytest命令已修复。
5网络实验，原1/8/10阶段通过；新9进程拒绝旧填充/版本2/三Web同会话/失效0项/回源/唯一2事件。
30读/4客户端，基线single/balanced/sharded QPS147.03/70.33/74.90，本轮145.58/69.46/67.30；主库资料额外读30/0/0，非全面提速。
图2026-10-27T17:00:42构建成功；十文件79直接/31影响节点、6附加文件、2217未解析调用/截断；新replication未索引，源码/执行补核。
[章节记录](scale-from-zero/docs/chapter02-review.md)、双语README/设计/需求/任务、报告与根状态已同步；不提交推送。
主库HA、GeoDNS/多地域/真实CDN、自动伸缩/迁移/生产监控仍未实现。用户追加第5章，随后继续审查限流器。

### 第2章缺口补齐（2026-10-29）

原书数据中心故障切换与从库提升，外加缓存 LFU/FIFO、`set`/`get` 和多缓存成员。`regions.GeoDNS` 在中心不健康时把解析顺延到下一个健康中心。`leadership.Fence` 只在主库租约过期且副本游标已追上时提升，提升后只接受资料写入，会话/任务留在原主库；默认阶段不挂 Fence。`CacheCluster` 跳过失败成员。新增 `tests/test_chapter02_gaps.py`。完整 pytest 73 项/51.68 秒。未重跑 2026-10-27 性能样本。生产级多地域 HA、真实 GeoDNS、在线迁移仍未实现。


### 第5章再次按原书复审 — 2026-10-27

原书923–1507行；rate-limiter，未提交推送，保留其他章节工作。
五算法补独立capacity，漏桶修复小数占用/重试；Redis原子TIME/回退高水位，
内存锁内采样/monotonic TTL，严格结果协议。规则路径/键类型生效、稳定身份、
v2状态键与快照，严格文件加载/有界轮询、固定指标、独立有界FIFO。
内存100000键/64常规巡查/128压力预算，Redis64连接/2秒可配超时/无自动扣额重试。
双框架共用状态和头开关/整数Retry-After；默认对端IP，凭证SHA256跨进程稳定。
基线205项10.10秒，最终249项11.50秒；实际Redis8.0.5四进程屏障下各算法
128次恰好允许25/错误0，两HTTP32次允许9拒绝23；TTL均存在，owned服务已停止。
原示例及新规则/FIFO演示通过。十组微基准保留基线/优化前/最终原始数据，
整体吞吐低于旧基线；清理热点优化降低单线程开销，不宣称全面加速。
中途图38文件484节点更新无错，八文件90直接/106影响/26附加文件，1815未解析且截断。
新增模块未索引，由源码/AST/执行补核；最终图状态见随后记录。
双语README/设计/需求、tasks、章节映射、benchmarks及根状态同步。
升级v2重置旧配额；多规则无统一回滚，日志仅记接纳请求；HA/Cluster/多地域全球
配额、外部监控平台、持久执行队列均未实现，不作为已完成宣称。

最终图2026-10-27T17:37:53增量8文件/64节点/542边更新，无错误；八文件89直接/106影响/26附加文件，1815未解析且有截断。独立Redis桶容量两算法速率2/10秒、容量5验证第6次拒绝。AST/静态检查覆盖第2章40与第5章49个Python文件；两项目20份MD及根3份MD全部本地链接有效。

示例加载回归又定位到Flask包名被扩展实例覆盖，修复并纳入两框架启动测试；最终249项/11.50秒通过。

示例修复后源码图17:41:05再增量更新6文件/62节点/502边，无错误；rule_files/queue/responses/clocks四文件分别查询确认为未索引，已用源码及执行补核。


### 第6章再次按原书复审 — 2026-10-27

原书1508–1734行，consistent-hashing，保留历史strict-clockwise路由协议。
批量构造一次排序、原子set_virtual_nodes保留原位置、默认1000000条目预算，
严格序列/可调用对象/键/哈希校验，同线程写重入报错。增加iter_assignments/
get_keys、一次性迭代统计、strict映射比较，以及iter_migrations流式候选选择。
区间单向扫描，CPython原生两个有序段归并，避免首轮Python堆循环开销。
基线108项14.93秒，最终154项18.56秒-Werror无跳过；四不同PID屏障同步、
2002键/3哈希/4拓扑阶段/三候选摘要一致；这是本机算法证明，无网络数据服务。
原demo/章节比较通过：加969对取模9066、删除848、权重增迁767并可恢复原路由。
10000条目建环212.27→21.46ms，50000规划82.14→44.52ms；lookup/add略慢，
保存基线/首轮/最终所有样本，未把局部改善写成全面加速。
图19:04:38增量30文件419节点2593边无错误；五源码54直接/51影响节点、
13附加文件296未解析、164边截断；新增测试未索引，以源码/AST/执行补核。
双语README/设计/需求/tasks、章节记录、报告、原始JSON和根状态同步。
物理迁移/复制、成员协议/故障检测、跨机集群及单热键拆分未实现；未提交推送。

第6章源码图最终19:10:54更新8文件/98节点/749边，无构建错误；17个Python文件与双语文档/根链接收尾检查通过。原demo已用批快照/严格统计替代逐键在线读并重跑，四进程最终结果仍一致。
