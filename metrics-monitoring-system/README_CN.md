# 指标监控与告警（第21章）

实现持久化拉取／推送采集、真实队列传输、标签索引时序存储、分层降采样、
告警状态机、静默／分组／去重／重试，以及HTTP查询、SVG图表和简单仪表板。
不包含日志、分布式追踪或业务分析。

2026-10-26核心审查补充按指标/序列分区、并行抓取/发送、跨序列量规聚合、增量降采样、
规则JSON导入及有界网络/后台生命周期。最终执行结果见[验证报告](benchmarks/results.md)，
逐项原书覆盖和生产缺口见[章节记录](docs/chapter21-review.md)。

## 运行

Python 3.10+，标准库。在本目录执行：

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
python examples/network_demo.py
python examples/chapter_demo.py --output benchmarks/features-2026-10-26.json
python tools/benchmark.py --output .runtime/query.json
```

完整测试／示例使用第20章的真实HTTP消息队列：3个独立broker、1个协调器和3个TCP代理，
再加exporter、collector、查询API、存储consumer、告警worker及自有webhook接收器，
共13个独立进程。退出停止自有进程，私有配置和日志留在忽略目录.runtime。
没有模拟Kafka，也没有向外部邮箱／商业服务发送通知。

新增章节功能示例另用14进程，含两个静态ring归属互斥的collector；报告验证3个活跃
队列分区、push重放、量规加权聚合和自有webhook的severity标签，与原13进程故障fixture分开。

持久运行先启动第20章服务，然后：

```powershell
python tools/init_config.py --queue-config ../distributed-message-queue/.runtime/secrets.json
$env:PYTHONPATH = 'src'
python -m metrics_monitor.cli exporter --root .runtime/exporter --config .runtime/secrets.json --port 9822
```

配置私有targets.json后，在其他终端分别设置相同PYTHONPATH、运行collector/api/storage/
alerts角色，完整命令见[英文说明](README.md)。打开 `http://127.0.0.1:9821/dashboard`
并输入read_token。凭据只留页面内存，不能提交或嵌入页面。

## 数据与队列

- 样本含metric、labels、type（gauge/counter）、Unix timestamp、value；有限数值，
  计数器非负。同序列／时间戳相同值幂等，不同值整个批次回滚。
- exporter采集真实进程CPU累计秒数、tracemalloc内存分配字节和HTTP请求计数。
  内存不是系统RSS，CPU不是整机利用率。采集失败生成collector_up=0。
- collector先持久化outbox才接受推送，响应不表示已入TSDB。默认16MiB积压上限，
  事务字节计数超限429；队列每条不超过12KiB/100样本。按metric或metric+labels分区，
  租约claim隔离worker，同一分区FIFO，失败队首不阻止其他分区推进。Dispatcher使用有界
  执行器并排空已接收工作。队列适配器每分区独立生产者，未知结果保留原负载/序号重试。
  存储consumer轮流处理组分配，先幂等入库再提交位置，停止后可回放。
- 默认全局1万序列／每指标1000序列、每序列最多8标签、每标签值128字符。
  单次查询最多200序列和2万输出点。异步基数／无效批次进入可见拒绝账本。
  队列offset过期会停止推进，需运维显式重置，不静默跳过。

配置可设`queue_partitions`（默认1，最多64）、`partition_by=metric/series`、
`dispatch_workers`和`scrape_workers`（默认4，最多32）。队列已有分区不足时需显式扩容；
改变配置不移动旧outbox目标，需要全局顺序时由应用协调切换。

抓取使用可重载的JSON目标文件（最多1MiB/1000项），目标可设interval/timeout/retries/labels；
错误更新保留上次有效列表，并通过健康信息报告。`collector_id`和共享`collectors`列表以
每成员64个虚拟节点的一致性哈希分配instance，所有collector必须使用相同成员配置。
这是静态归属参考，不是etcd/ZooKeeper发现、动态共识或自动故障接管。

## 查询、保留和归档

`/query`与`/chart.svg`要求read/admin Bearer；参数metric/start/end，选填match JSON、
step、aggregation。量规支持avg/min/max/last，avg按样本数加权。计数器支持increase/
rate/last，识别重置，不把累计读数求和当请求数。rate使用每桶实际首末时间，单点返回null，
没有外推或隐式跨查询桶回看。标签只支持精确匹配，不是完整PromQL。

`group_by`不传仍逐序列返回；`group_by=[]`合并全部匹配序列，或传标签列表如`["zone"]`。
跨序列仅支持gauge的avg/min/max，avg按实际样本count加权；counter和last跨序列聚合明确拒绝。
分组不绕过200个源序列上限。

原始约7天、分钟约30天、小时约365天。为避免边界遗漏，原始最多多留一分钟，粗层最多
多留一小时。原始窗口内允许迟到样本，重新构建摘要后再清理；未来容差30秒。
查询合并不重叠年龄层及实时尾部；粗粒度部分桶会包含整桶摘要，并明确标记精度限制。

摄取在等待写锁前冻结输入，只记录受影响的分钟/小时桶；聚合在一致读快照上、SQL写锁外
计算，再以revision校验短事务发布，最多重试3次。每轮每层最多20000桶、总共200000源原子，
超限或快照持续变化返回503并保留原始数据。查询全局输入最多200000原子（每序列原始点
最多20000）、输出最多20000点，超限413。缓存固定条带合并冷加载，返回独立对象避免调用方
修改污染后续结果。基数预算随数据库持久保存。

本地冷归档使用gzip和SHA-256校验，可在实时数据过期后验证读取；archive CLI见英文README。
范围必须为早于30天的完整小时；最多100000桶/10000序列，压缩和解压各64MiB。
读取同时校验结构、序列身份、桶范围和统计字段；独占创建并fsync，失败只清理同身份的新文件。
不是云存储迁移，归档文件不自动按在线保留策略删除。部分告警／通知元数据尚无GC。

## 告警与通知

`POST /rules`、`/silences`和`/evaluate`要求admin；`GET /alerts`要求read/admin。
规则ID不可变，支持阈值、连续持续时间和分组标签；抖动恢复会重置pending。
缺少／陈旧数据重置pending，保留已firing状态并标no_data，不假装恢复正常。

API/storage/alerts角色可用`--rules-file path.json`（别名`--rules`）在启动worker前原子
导入规则数组或`{"rules":[...]}`。文件最多1MiB/1000规则，重复/冲突ID拒绝整批；
支持规则labels附severity等元数据。这不是完整YAML/PromQL解析器。

静默期间不发送，状态继续评估；未分组的过时事件可取消。已经冻结的通知批次保持同一
幂等ID／内容重试；分组FIFO防止恢复通知越过失败告警。30秒租约隔离worker，最多5次
指数退避，耗尽保留dead记录。接收方配合幂等才能避免外部重复。
静默可能推迟整批，已分组旧状态可能在解除后按序投递；也可能单独发送resolved。

实际验证的是自有HTTP webhook；邮件／电话／PagerDuty由可替换Provider接口扩展，
未配置会明确失败，不能宣称真实供应商投递完成。配置生成器不启用任何通知目的地。

## 验证与边界

HTTP默认32个处理名额、128 backlog、10秒绝对socket预算；CLI可调`--concurrency`及
`--request-timeout`。正文最多1MiB、响应最多4MiB；正文前鉴权，拒绝歧义framing、重复JSON
字段和非有限数。出站HTTP不经环境代理/重定向转发凭据。关闭排空自有handler/worker，
后台失败可观察并重试；socket期限不能强杀DNS/SQLite/CPU任务。

最终测试、真实进程示例和原始测量见[报告](benchmarks/results.md)与[设计](docs/design_cn.md)。
2026-10-20的15项/p95 67.171ms仅保留历史日期，不冒充本轮结果。SQLite和第20章单协调器
有规模/可用性限制；生产TSDB压缩/物理分片、共识发现、外部告警事件总线、Kafka/Flink、
完整PromQL/Grafana、TLS/ACL、真实供应商、云部署和千万序列仍待扩展。
本轮告警使用本地持久outbox，只有指标流经第20章队列。
