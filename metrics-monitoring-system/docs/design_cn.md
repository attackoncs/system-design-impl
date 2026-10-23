# 设计：指标监控与告警（Metrics Monitoring and Alerting）

## 流水线与进程边界
一个 HTTP 采集器接收推送批次，并周期性地发现/抓取 JSON 导出器。它在回复之前提交一个有界的 SQLite 发件箱（outbox）。稳定的分块 ID 和内容哈希可在重启后保留。调度器（dispatcher）通过第 20 章的 HTTP 协议以仲裁确认（quorum confirmation）发送 ≤12KiB 的队列记录。队列传输在本包外部：实验中使用了三个实际的 broker 和一个协调器；不模拟 Kafka/Redis 部署。存储工作进程（storage worker）使用带租约保护（fenced group lease）的组消费，在时序数据库（Time-Series DB, TSDB）中一次性应用批次，然后向队列确认。丢失的确认（lost acknowledgement）或新的生产者纪元（producer epoch）可能导致传输重复；持久化的批次回执（durable batch receipt）使得重复摄取具有幂等性。无效批次会被送入一个持久化的本地拒绝账本（hash/原因，而非无穷重试的毒消息）。

## 序列与聚合
SQLite WAL（Write-Ahead Logging）存储序列、倒排精确标签索引（inverted exact-label index）、不可变的原始样本、分钟/小时统计摘要以及摄取回执。写入事务强制实施序列预算并更新修订版本（revision）。并发冲突样本会使整个批次回滚。迟到的原始样本触发降采样（rollup）重新计算；完整的桶会在裁剪前从保留的较低分辨率数据重建。摘要保留 count/sum/min/max、首尾值及其时间戳，以及内部计数器增量（counter increase）。合并计数器摘要会加上边界增量，使用重置后的新值。量规（Gauge）均值使用 sum/count；计数器速率使用 increase/(last-first)，不做外推（extrapolation），且要求至少两个采样时间点。这并非完整的 PromQL。

原始数据截断点为 `floor((now−7d)/60)*60`；分钟级截断点为 `floor((now−30d)/3600)*3600`；小时级截断点为 `floor((now−365d)/3600)*3600`。保留策略可超出标称年龄一个较低层级的桶，以避免接缝丢失（seam loss）。查询选择不重叠的年龄层级，按请求步长合并，并披露粗略部分桶过度抓取（coarse partial-bucket overfetch）。粗略查询步长必须与保留粒度对齐。近期原始数据填充实时尾部（live tails）。缓存键包含存储修订版本以及完整的选择器/范围/选项。不宣称具备生产级 TSDB 编码/压缩能力。

## 告警与投递
规则是不可变的命名配置（允许相同重放）。每个匹配的序列拥有持久化状态和单调转换序列。假值评估会重置 pending 或解析 firing；缺失/过期数据会重置 pending 并使已处于 firing 的告警保持活跃且为无数据状态（no-data status），避免臆造的恢复。通知分组使用规则 ID 和配置的标签键。状态转换和待通知事件一起提交。静默（silence）延迟通知，但评估继续。尚未成批的事件如果被后续事件取代可以取消。一旦分组完成，一次投递拥有冻结的负载和稳定的幂等键；重试不会意外地在不同键下重新分组。每个组的 FIFO 防止解析事件追上较早的重试。租约（lease）隔离竞争的投递工作进程，退避有上限，耗尽的投递保留为死信（dead letter）可见。

Webhook 传输使用真实 HTTP，支持可选的目标凭据。提供者接口边界覆盖邮件/电话/PagerDuty；缺失的适配器会显式失败，绝不假装投递成功。接收端幂等性处理已测试的丢失 ACK 场景。端到端外部投递为至少一次（at-least-once），若无接收端配合则无法做到恰好一次（exactly-once）。API/worker 角色可共享同一个本地 TSDB 文件；这不是远程 SQL 集群。采集器根节点和调度器是单所有者参考组件。

## 运维与扩展
读取/推送/管理凭据是分离的。仪表盘请求读取令牌并保持在内存中；不嵌入密钥或持久化浏览器令牌存储。发现与通知目标是受信的运维配置，而非任意不可信的 URL。CPU 指进程 CPU 秒数，内存指 Python 分配追踪值（traced Python allocation），不是宿主机利用率/RSS。服务失败发出 `collector_up=0`。参考实现需要共识元数据、可扩展的 TSDB/流分区、真实提供者凭据、TLS/ACL、配额/回执 GC、磁盘压缩、查询规划和持续负载验证才能进入生产。队列的单协调器仍是已知的 SPOF（单点故障）。
