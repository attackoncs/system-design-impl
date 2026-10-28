# 第21章核对：监控与告警系统

核对日期：2026-10-26（Asia/Shanghai）。依据
[《System Design Interview.md》](<../../System Design Interview.md>)8997–9663行。
范围是基础设施指标、查询、告警与可视化；不包含日志、分布式追踪或业务分析。

## 原书映射

| 原书内容 | 当前可执行实现 | 限制/未实现部分 |
| --- | --- | --- |
| 指标名、标签、值与时间序列 | 规范标签、metric/type/timestamp/value、不可变样本与批回执 | 仅gauge/counter，标签为精确匹配，不是完整PromQL |
| CPU/内存/请求等基础设施指标 | 实际进程CPU累计秒数、Python tracemalloc分配、HTTP请求counter | 不是整机CPU负载/系统RSS；没有完整宿主机/磁盘监控agent |
| pull/push两种采集 | HTTP exporter抓取及鉴权push，先持久outbox再接受 | 合成/自有进程验证，非完整短任务Pushgateway协议 |
| 发现目标、标签和抓取规则 | 有界JSON文件重载、可信instance/labels、interval/timeout/retries | 文件发现，未部署etcd/ZooKeeper共识与动态成员 |
| 多collector一致性归属 | 静态一致性哈希、虚拟节点、有界并行抓取 | 所有collector须使用相同成员/目标配置，未自动接管失联成员 |
| 指标传输与消息队列 | 第20章真实HTTP broker/独立磁盘、多数确认、存储先写后commit | 非Kafka部署；保留队列单协调器和有限分区容量 |
| 按指标/标签分片 | Collector按metric或metric+labels分区、每分区producer、consumer轮询组分配 | 传输分片，TSDB仍为单机SQLite；变更配置需协调路由切换 |
| 缓冲、批处理和可靠消费 | 有界outbox字节计数、partition FIFO lease claim、Dispatcher并行队首 | 同分区失败不越过，lease失效/重启可能重复，靠TSDB回执幂等 |
| 实时数据存储/倒排索引 | SQLite WAL、精确标签索引、基数限制、原始/分钟/小时表 | 有界参考引擎，未使用生产专业TSDB或实现Gorilla压缩 |
| 7天/30天/1年保留和降采样 | 对齐桶截断、dirty桶增量重建、迟到修复、counter重置组合 | 桶对齐可多留一个桶，不是实际一年持续运行验收 |
| 查询服务与缓存 | 有界一致快照、revision/全部选项键、冷加载合并、返回值隔离 | 200源序列/200000输入原子/20000输出点，非生产查询集群 |
| 同region多webserver平均值 | group_by标签或[]全组的gauge avg/min/max，avg按样本count加权 | 非时间加权；counter/last跨序列拒绝，逐序列counter保留 |
| 冷存储 | 有界本地完整小时gzip归档，checksum/结构/身份校验 | 非云分层，文件单独GC，不pin在线保留范围 |
| 配置规则、持续时间、severity | 不可变规则、原子JSON文件导入、静态labels、不改变selector | 使用JSON，不提供完整YAML/表达式语言或持续配置服务 |
| 告警inactive/pending/firing/resolved | 快照评估、每规则evaluation-version CAS发布、持续阈值、flapping/no_data | 缺失数据不虚构恢复；预算不足明确报告，不冒充无限状态规模 |
| 合并/去重/静默/重试 | 冻结分组负载/幂等ID、FIFO、lease fencing、claim计尝试、死信 | 本地durable deliveries表，不是原书独立Kafka告警事件管线 |
| Email/电话/PagerDuty/webhook | Provider接口及真实自有HTTP webhook | 商业渠道没有配置/真实投递；外部去重需接收方合作 |
| 图表/仪表板 | 鉴权查询、SVG及简单HTML UI | 非Grafana产品；未声称浏览器截图或人工交互验收 |
| 高可用与千万指标规模 | 有界并发/预算、失败可观察与独立进程故障验证 | 无TSDB物理分布/复制、共识HA、云地域或千万序列持续压测 |

## 工程修复

采集、发现、队列适配、存储schema/query/rollup/archive、告警配置/evaluation/delivery/provider
及HTTP/生命周期分开。原公共Store/Collector/QueueAdapter/Alerts入口保持兼容。

Collector在事务中维护积压字节，避免每次接受扫描全部payload；claim持久owner/lease、按分区
FIFO，不同分区队首可以并发。队列未知结果保留原sequence/内容，旧待发分块保留原目标。
临时存储错误不误入毒消息拒绝账本，也不提交消费位置。ScrapeScheduler只提交有界自有工作，
错误配置保留上次有效目标，重试共用总时间预算，关闭排空接受任务。

Store在等写锁前冻结输入，锁后采样时间并校验保留窗口。dirty分钟/小时桶从一致读快照取数，
聚合与编码在writer外计算，再以revision校验短事务发布，避免每次查询持写锁全量重建。
每层最多20000桶、总200000源原子、最多3次发布尝试；超限503不删除源数据。查询全局输入
预算也在SQL LIMIT读取中生效；缓存64固定条带合并冷加载并返回隔离副本。

本地归档要求完整小时，最多100000桶/10000序列，压缩与解压各64MiB；读取校验hash和结构、
身份、桶范围、统计字段。独占创建/fsync，失败仅清理同身份的新文件。规则JSON导入1MiB/1000
规则原子提交；severity静态标签用于告警metadata，不改变指标selector。通知claim即计尝试，
避免崩溃反复领取而绕过重试上限；同组FIFO和稳定负载/ID覆盖丢确认重试。

HTTP默认32处理名额/128 backlog/10秒绝对socket预算、正文1MiB/响应4MiB，正文前鉴权，
拒绝重复/歧义framing、重复JSON字段及非有限数。出站直接HTTP不经环境代理/重定向转发凭据；
绑定成功再启动非daemon worker，失败可见并重试，关闭排空自有任务。socket期限不能强杀
DNS/SQLite/CPU，处理容量保留到实际工作退出。

## 验证证据

修改前基线15项通过（20.412秒）；最终完整 **108项通过（26.333秒），无跳过**，
ResourceWarning作为错误。分组为原15+存储23+告警22+流水线18+HTTP20+CLI10。
完整命令、原始数据及适用范围见[验证报告](../benchmarks/results.md)。

原13进程network_demo实际完成采集、队列、存储及通知闭环：push到可查559.953ms，
自有webhook尝试2次、唯一通知1条。新增
[章节示例](../examples/chapter_demo.py)使用14个独立进程；
[原始结果](../benchmarks/features-2026-10-26.json)记录两个collector对4个目标互斥归属、
3个队列分区均活跃、group平均值40/count3、push重放相同响应及owned webhook severity=page。

100序列/6000样本、4客户端40次固定范围真实HTTP查询：冷228.463ms，warm p50/p95
65.693/90.315ms、56.76 QPS、零错误，见[原始测量](../benchmarks/review-2026-10-26.json)。
测量与第22章11项旧回归有主机竞争，不能视为受控前后对照或生产能力证明。

## 图分析与剩余范围

本轮使用code-review-graph最小上下文及最终增量刷新，24文件更新成功，无构建错误。
限定第21章六个既有核心文件的影响分析：60个直接改动节点、33个受影响节点、额外6个
文件，761个调用未解析，边输出有截断。新未跟踪collector.py的file_summary明确返回
target not indexed；新模块已用源码和执行证据补核。动态Python调用也可能缺边，空图边
不等于没有调用/测试。其他章节未提交改动不能当作本项目独立风险结论。

专业TSDB压缩/物理分片与复制、共识发现和自动接管、优先级流处理、独立告警事件总线、
完整PromQL/Grafana、TLS/ACL、商业provider、云冷存储、备份与元数据GC、千万序列持续
负载和目标文件系统掉电验证仍为后续。本轮没有提交或推送。
