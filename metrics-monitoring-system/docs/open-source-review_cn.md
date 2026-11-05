# 第21章：开源源码对照与实现核对

日期：2026-11-04。依据根目录《System Design Interview.md》第21章“Metrics Monitoring and Alerting System”。

结论：采集→队列→时序存储→查询/告警→自有webhook的核心闭环已有实现。本轮确认并
修复消费端的JSON解析/坏消息隔离缺口，112项完整回归通过。专业TSDB、共识HA、
商业通知渠道及千万序列规模仍列为后续扩展。

## GitHub开源参考

通过GitHub连接器分别检索Prometheus与Alertmanager，确认其采集、规则和通知实现，
并阅读以下固定提交的实际源码。选读范围包括抓取生命周期、pending/firing转换、
通知分组、去重及重试；未构建、运行或压测上游项目，未复制代码或增加运行依赖。

| 项目与固定版本 | 实际阅读的源码 | 可对照的机制 |
| --- | --- | --- |
| [Prometheus](https://github.com/prometheus/prometheus/tree/ac85e06f5a45bb80186bca49072f062863ba738b) | [scrapeLoop.run](https://github.com/prometheus/prometheus/blob/ac85e06f5a45bb80186bca49072f062863ba738b/scrape/scrape.go#L1470)、[scrapeAndReport](https://github.com/prometheus/prometheus/blob/ac85e06f5a45bb80186bca49072f062863ba738b/scrape/scrape.go#L1553) | 每个目标按interval循环，带初始偏移和取消；抓取请求使用timeout上下文。采集结果和报告通过appender处理，错误路径回滚，正常路径提交，提交失败另行报告。 |
| 同上 | [AlertingRule.Eval](https://github.com/prometheus/prometheus/blob/ac85e06f5a45bb80186bca49072f062863ba738b/rules/alerting.go#L387)、[pending到firing](https://github.com/prometheus/prometheus/blob/ac85e06f5a45bb80186bca49072f062863ba738b/rules/alerting.go#L526) | 查询先在状态锁外执行；按告警标签集合识别实例，保留ActiveAt，满足holdDuration后转firing。求值错误直接返回；成功结果中消失的实例进入移除/恢复路径，keepFiringFor可延长已触发状态。 |
| [Alertmanager](https://github.com/prometheus/alertmanager/tree/47403693800ba753b79f61c0fbca5e7d1d74281c) | [创建分组及初次等待](https://github.com/prometheus/alertmanager/blob/47403693800ba753b79f61c0fbca5e7d1d74281c/dispatch/dispatch.go#L709)、[分组刷新循环](https://github.com/prometheus/alertmanager/blob/47403693800ba753b79f61c0fbca5e7d1d74281c/dispatch/dispatch.go#L844) | 以路由及分组标签建立聚合组；首批等GroupWait，随后按GroupInterval刷新。每轮把统一的时间、组身份、接收者和RepeatInterval等信息传入通知流水线，并设置本轮上下文期限。 |
| 同上 | [通知阶段顺序](https://github.com/prometheus/alertmanager/blob/47403693800ba753b79f61c0fbca5e7d1d74281c/notify/notify.go#L254)、[DedupStage](https://github.com/prometheus/alertmanager/blob/47403693800ba753b79f61c0fbca5e7d1d74281c/notify/dedup_stage.go#L52)、[RetryStage](https://github.com/prometheus/alertmanager/blob/47403693800ba753b79f61c0fbca5e7d1d74281c/notify/retry_stage.go#L94)、[SetNotifiesStage](https://github.com/prometheus/alertmanager/blob/47403693800ba753b79f61c0fbca5e7d1d74281c/notify/set_notifies_stage.go#L64) | 按接收者/组查通知日志，比较firing、resolved集合及重复间隔；然后投递/重试，后续阶段才记录通知状态。可重试与不可重试失败有不同处理，退避从一次调用结束后开始，并服从上下文取消和期限。 |

## 用于本地审查的检查点

1. **持续时间属于告警实例。** 对照Prometheus保留ActiveAt的方式，检查本地是否按
   规则与标签身份保存pending起点；反复命中不应每轮重置持续时间，恢复正常或失去
   有效数据后也不能把先前中断的pending时间继续累计。
2. **无数据、求值错误与正常恢复要分开。** Prometheus的查询错误直接返回；成功
   求值后标签实例消失则走状态变更路径。本地有明确的no_data策略，需要验证其自身
   契约，不能把采集失败视为指标归零，也不能声称与上游空向量语义完全相同。
3. **分组、发送间隔和重复提醒不是一个计时器。** Alertmanager分别使用GroupWait、
   GroupInterval及RepeatInterval。本地应单独核对聚合等待、冻结批次、静默及重试
   的时间依据；恢复事件不能越过同组尚未投递的告警，重新计算也不能改掉重试内容。
4. **投递与本地成功记录之间仍有失败窗口。** 上游流水线先去重、再重试、后记录
   通知状态；本地同样应验证未成功的发送不会提前标为已发送，过期lease的worker
   不能回写新worker状态。接收者已处理而应答丢失时，重试可能再次抵达外部系统，
   必须靠稳定幂等身份及接收方合作处理重复。
5. **超时、调度与关闭需要一并检查。** Prometheus的抓取循环和请求上下文可取消。
   本地需要检查工作数量有界、重试共用预算、不占SQL写事务等待网络，关闭排空
   已接受任务；错误的目标配置也不能静默清空上一份有效目标。当前调度按一轮等待
   已接受任务，最慢目标会延迟下一轮；HTTP受理与outbox发送有各自的工作路径。
6. **读快照与发布时点分别校验。** 规则查询在锁外进行不代表可以直接发布结果。
   本地有并发评估、摄取及降采样，应核对revision/evaluation-version的接受条件，
   避免旧结果覆盖新状态，并保证预算耗尽时错误可见、源数据不被误清理。

## 与本地参考架构的差异

本地覆盖和待扩展项以[章节映射](chapter21-review.md)、[设计](design_cn.md)及
[验证报告](../benchmarks/results.md)为准；选读上游模块不能代替本地执行证据。

- 本地通过第20章独立HTTP消息队列传输指标，再写入单机SQLite原始/分钟/小时层。
  选读的Prometheus抓取appender和告警规则并不证明两者拥有相同的队列或存储协议；
  本轮没有审计Prometheus TSDB、压缩、PromQL或远程写完整实现。
- 本地只支持gauge/counter、精确标签选择器、阈值和持续时间。Prometheus选读代码
  对表达式返回的向量按标签身份管理告警，还包含keep_firing_for等机制；未将本地
  简化规则标成完整PromQL规则引擎。
- 本地缺少/陈旧数据重置pending，保留已firing状态并标no_data；Prometheus成功
  求值中消失的实例会按其恢复与keepFiringFor规则处理。二者是有意区分的契约，
  不能仅因为都有pending/firing就推断缺数据行为相同。
- 本地通知是SQLite持久outbox、冻结负载/ID、同组FIFO、租约隔离和有限尝试，
  不等于Alertmanager完整路由、抑制、通知日志或集群协议。上游RetryStage会对可恢复
  错误持续重试直到上下文取消；本地最多5次后保留dead记录，失败处置方式不同。
  本地分组身份包含规则ID、目标、通道和group_by标签值，不是完整跨规则路由树。
- Alertmanager中仅resolved且接收者不发送恢复通知等路径可以不进行外部投递，仍让
  后续阶段记录状态；不能把“更新通知日志”笼统理解为每次都发送了外部通知。
- 当前实际外部通道是自有HTTP webhook。邮件、电话、PagerDuty需独立配置和验证；
  稳定ID及本地去重不构成任意外部系统的恰好一次副作用保证。

生产TSDB压缩/物理分片与复制、共识发现/自动接管、独立告警事件总线、完整PromQL与
Grafana、TLS/ACL、云冷存储、商业通知供应商、元数据GC及千万序列持续验收，仍需独立
实现和验证。本轮本地代码、测试及文档的最终结论由同目录验证记录补充。

## 章节覆盖与源码路径

逐项覆盖见[章节映射](chapter21-review.md)。本轮通过CodeGraph定位当前源码，并分别
复核存储、告警和采集/消费/网络；保留现有模块边界，仅修改确认的输入解析缺口。

| 章节链路 | 实现与重点 | 验证范围 |
| --- | --- | --- |
| push/pull、目标归属与缓冲 | [Collector](../src/metrics_monitor/collector.py)持久受理、字节计数、分区FIFO claim；[discovery](../src/metrics_monitor/discovery.py)静态ring及有界抓取 | 独立输入/lease/分区并发、无效目标更新保留旧配置；双collector实际互斥归属 |
| 队列传输及消费 | [QueueAdapter](../src/metrics_monitor/queue_adapter.py)每分区独立序号/未知结果；[consume_once](../src/metrics_monitor/pipeline.py)先入库或持久拒绝，再提交 | 第20章真实HTTP队列；解析故障与拒绝写失败另外用受控页/真实SQLite验证 |
| 存储、降采样与查询 | [Store](../src/metrics_monitor/storage.py)、[rollups](../src/metrics_monitor/rollups.py)、[queries](../src/metrics_monitor/queries.py)的快照、dirty桶、revision发布和预算 | counter重置独立参考、迟到修复、年龄层接缝、gauge按样本数加权、缓存隔离 |
| 冷数据 | [archives](../src/metrics_monitor/archives.py)完整小时、有界gzip、身份/hash/结构验证 | 本地文件生成/读取/故障；非云分层或一年持续运行 |
| 规则与状态 | [Evaluation](../src/metrics_monitor/alert_evaluation.py)有效观测、持续时间/no_data、每规则version CAS | 旧评估不回退新状态；不把缺数据当恢复；精确标签阈值规则，非完整PromQL |
| 分组与通知 | [Delivery](../src/metrics_monitor/alert_delivery.py)冻结批次、稳定ID、FIFO、lease及claim计次数 | 自有HTTP webhook丢确认重试；同规则/目标/通道分组；商业provider未验证 |
| HTTP与生命周期 | [HTTP](../src/metrics_monitor/http.py)、[CLI](../src/metrics_monitor/cli.py)严格协议、容量、先绑定后启动及排空 | 实际本机socket/独立进程；CPU/SQL/DNS不受socket期限强制终止 |

### 修复：严格解析队列信封，避免坏输入阻塞分区

原消费端使用默认json.loads：重复键会取最后一个值，NaN/Infinity/1e400也可在未使用
的信封字段中通过。合法但过深的JSON还可能抛RecursionError，被后台当临时失败反复
重试。本机Python 3.13.9用4000层、约8KiB信封复现，仍低于collector的12KiB预算。

最小修改为复用已有transport.loads，统一拒绝重复字段及非有限数值，并将RecursionError
归入坏输入。拒绝账本保存标识/hash/原因，成功后才处理后续记录并提交；拒绝写失败、
SQL/网络和5xx继续上抛，不丢弃临时故障。正常输入格式与队列分区/存储协议保持一致。

新增[4项回归](../tests/test_pipeline_source_review.py)：深嵌套、重复字段、非有限信封
字段、拒绝账本写失败。前三类均在修前复现问题；验证坏记录后同页合法样本可入库，
同批重放不会重复样本/拒绝记录，拒绝账本失败时没有commit。这些用真实Store/SQLite
及受控队列页，不能替代下面的独立进程闭环证据。

### CodeGraph与独立复核

沿采集/队列/入库、查询/摘要、规则/投递、HTTP/CLI链路读取当前源码。存储与告警
专项未发现确认缺陷，保留原实现。code-review-graph增量重解析1个文件、更新3节点/
40边，无错误；这些是更新量，不是全库总量。pipeline限定直接调用影响分析为3个
文件内节点、12个两跳影响节点、额外4文件，本次结果无截断/未解析调用。

动态dispatch和新增未跟踪测试仍用源码及执行补核，不能将结构图当完整覆盖。前两章
未提交变更继续保留，全库差异列表不等于本章独立风险结论。

## 本轮执行证据

Python 3.13.9 / Windows 11，标准库/SQLite。基线108项通过、27.577秒；修后完整
**112项通过、26.271秒，无跳过，所有警告作为错误**。测试组仍按前次报告划分，
新增4项消费者解析/隔离回归。[完整命令与验收报告](../benchmarks/results.md)。

- 13进程network_demo：真实拉取、推送、队列、存储、告警和自有webhook；push至可查
  419.936ms，丢确认后2次发送尝试、1条唯一通知。
- [14进程章节示例](../benchmarks/features-2026-11-04.json)：两个collector对4目标互斥
  归属，3队列分区活跃，加权平均40/count3，push回放相同响应，通知severity=page。
- [查询原始测量](../benchmarks/review-2026-11-04.json)：100序列/6000样本直接入库，
  4客户端40次真实HTTP固定范围查询，0错误；cold266.157ms、warm p50/p95为
  70.148/91.971ms，54.27 QPS。重复查询预热revision缓存，不是队列摄取吞吐或TSDB规模证明。

各实验顺序执行，未与其他测试/图构建争用本机；没有将小样本变化解释为性能提升。
自建进程已停止，诊断配置、磁盘和日志保留在忽略的.runtime。中英文README、设计、
需求、任务、章节索引及学习指南同步更新，新增坏消息隔离/重试图。

静态验收：src及新增测试的Ruff E9/F、两个修改Python文件的格式检查通过；39个Python
文件通过3.10语法解析。16份Markdown的528个本地链接有效，基准JSON可解析，27份
学习指南生成校验通过。本章3张Mermaid图已实际渲染，限定范围的diff空白检查通过。
