# 设计：指标监控与告警

核心审查日期2026-10-26，来源原书8997–9663行。
最终测试/性能见[验证报告](../benchmarks/results.md)，覆盖与缺口见[章节记录](chapter21-review.md)。

## 采集与传输

推送或发现的JSON exporter先进入持久SQLite outbox，接受响应不代表已入TSDB。
默认16MiB积压通过事务字节计数控制，满载429；按metric或metric+规范标签分区，
每块最多100样本/12KiB。分块持久保存目标，配置变化不迁移旧积压。
owner/lease隔离claim，同分区FIFO，不同队首由有界Dispatcher并行发送。
未知发送结果保留原生产者序号/内容；重启新epoch可能重复传输，TSDB批回执承担幂等应用边界。

消费者轮流处理组分配，先应用再提交。无效样本/基数冲突进入持久拒绝账本；临时存储
故障不推进游标。队列offset过期明确失败，需要运维恢复。第20章真实HTTP broker和
分离磁盘提供传输，不模拟Kafka，并保留单协调器限制。

可重载JSON目标含labels/interval/timeout/retries。错误更新保留上次有效列表并报告；
静态一致性哈希按共享成员列表分配instance，所有collector必须使用同一成员配置。
这不是共识发现或自动故障接管。抓取重试共用目标时间预算，有界worker隔离慢目标。

## 存储、查询与保留

SQLite WAL保存序列、精确标签倒排索引、不可变原始样本、分钟/小时摘要和批回执。
输入在等待写锁前冻结，拿到写锁后采样时钟并检查范围；基数预算随库持久保存，冲突
回滚整批。不宣称生产TSDB压缩/编码或物理分片。

dirty表只跟踪被修改的分钟/小时桶。一致读快照提取源数据，聚合/编码在写锁外计算，
短事务以revision校验发布。最多3次尝试，每层20000桶、总200000源原子；超限/持续
冲突返回503，保留未裁剪源数据。旧库一次迁移seed可用低层桶，不改变原批指纹。

原始截断点floor((now−7d)/60)*60；分钟/小时截断点分别
floor((now−30d)/3600)*3600及floor((now−365d)/3600)*3600。重建完整低层桶再裁剪。
查询合并不重叠层级、披露粗桶边界包含整桶，step须对齐保留粒度。
gauge按sum/count；counter增量组合内部值及重置后的边界增量，rate按实际首末时间差，
单点无rate，不外推或隐式跨查询桶回看。

marker/revision和数据来自同一快照。查询最多200源序列/200000输入原子（每序列原始
点最多20000）/20000输出点。缓存键包含版本和所有选项，64固定条带合并冷加载，返回
对象隔离调用方修改。group_by不传逐序列；[]或标签列表聚合gauge avg/min/max，avg按
样本count加权。counter/last跨序列明确拒绝；不实现完整PromQL。

本地小时gzip归档要求完整冷小时，最多100000桶/10000序列，压缩与解压各64MiB。
读取检查SHA-256、结构、身份和桶/统计字段；独占创建、fsync，失败仅删除同身份新文件。
归档单独GC，不是云分层或在线保留pin。

## 告警与投递

规则ID不可变；--rules-file/--rules在worker启动前原子加载JSON数组或{"rules":[...]}，
上限1MiB/1000规则。静态labels可附severity，不改变指标selector。配置/评估/schema/
投递/provider分离；评估先做读快照计算，再以每条规则的evaluation-version CAS发布
状态和事件，不要求全局Store revision保持不变。

条件恢复重置pending或生成resolved；no_data重置pending并保留已firing，避免虚构恢复。
分组使用rule及指定标签；未分组的过时事件可取消，冻结批次保持原负载/幂等键重试。
同组FIFO防止恢复越过旧重试，lease隔离竞争结果，claim即累计尝试次数；有限退避、最多
5次，耗尽保留dead记录。静默期间继续评估，可能延迟整批并在解除后按序投递。

真实验证自有HTTP webhook。邮件/电话/PagerDuty须配置Provider，否则显式失败。
外部去重需要接收方配合，不宣称任意副作用恰好一次。告警使用本地持久outbox，
没有另建Kafka告警事件总线，只有指标流经第20章队列。

## 运行与生产边界

HTTP默认32处理名额/128 backlog/10秒绝对socket预算，正文1MiB/响应4MiB。
正文前鉴权，拒绝重复JSON、非有限值及歧义framing；直接HTTP不走环境代理或转发重定向，
HTTPS使用证书校验。先绑定再启动worker，非daemon处理线程和后台排空自有工作并暴露
失败重试。Socket期限不能终止DNS/SQLite/CPU任务。

API/storage/alerts共享单机SQLite，不是复制SQL HA。CPU是进程累计秒数，内存是Python
tracemalloc分配，不是整机利用率/RSS。生产TSDB压缩/分布、共识发现、流处理/优先级、
元数据HA、TLS/ACL、真实商业provider、云保留、备份、回执/告警GC及持续规模/掉电验证
仍为扩展，不由本机样本测量证明。
