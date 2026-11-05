# 第20章：开源源码对照与实现核对

日期：2026-11-04。依据根目录《System Design Interview.md》第20章“Distributed Message Queue”。

结论：核心教学链路已有实现，本轮补齐HTTP长轮询的容量预留，避免空等待占满写入和
心跳所需的处理线程。完整回归121项通过；章节中的分布式共识、协调器HA、动态broker
放置等仍未完成，按文档继续列为后续扩展。

## GitHub开源参考

通过GitHub连接器分别检索Apache Kafka与Apache RocketMQ，确认存在实际队列/事件流
实现，并按以下固定提交阅读源码。此次参考的是批处理、重试身份和长轮询/过滤机制；
未构建、运行或压测上游项目，未复制其实现或增加运行依赖。

| 项目与固定版本 | 实际阅读的源码 | 可对照的机制 |
| --- | --- | --- |
| [Apache Kafka](https://github.com/apache/kafka/tree/c13c30de24e4eb1c830cccdebfe8072636a4b078) | [RecordAccumulator.append](https://github.com/apache/kafka/blob/c13c30de24e4eb1c830cccdebfe8072636a4b078/clients/src/main/java/org/apache/kafka/clients/producer/internals/RecordAccumulator.java#L285)、[批次就绪条件](https://github.com/apache/kafka/blob/c13c30de24e4eb1c830cccdebfe8072636a4b078/clients/src/main/java/org/apache/kafka/clients/producer/internals/RecordAccumulator.java#L714)、[未解决序列与排空](https://github.com/apache/kafka/blob/c13c30de24e4eb1c830cccdebfe8072636a4b078/clients/src/main/java/org/apache/kafka/clients/producer/internals/RecordAccumulator.java#L939) | 每分区批队列分别同步；可能阻塞的缓冲分配在队列锁外，重新入锁后复查分区。满批、等待时间、内存压力、flush/close等共同决定就绪。未解决的生产者序列阻止新批发送，已分配身份/序列的重试批保持原值。 |
| [Apache RocketMQ](https://github.com/apache/rocketmq/tree/0e839a958bf3a7c613e9baec40acc606e4ba8fc2) | [挂起请求](https://github.com/apache/rocketmq/blob/0e839a958bf3a7c613e9baec40acc606e4ba8fc2/broker/src/main/java/org/apache/rocketmq/broker/longpolling/PullRequestHoldService.java#L45)、[到达/超时唤醒](https://github.com/apache/rocketmq/blob/0e839a958bf3a7c613e9baec40acc606e4ba8fc2/broker/src/main/java/org/apache/rocketmq/broker/longpolling/PullRequestHoldService.java#L123)、[消费队列过滤](https://github.com/apache/rocketmq/blob/0e839a958bf3a7c613e9baec40acc606e4ba8fc2/broker/src/main/java/org/apache/rocketmq/broker/filter/ExpressionMessageFilter.java#L60)、[提交日志属性过滤](https://github.com/apache/rocketmq/blob/0e839a958bf3a7c613e9baec40acc606e4ba8fc2/broker/src/main/java/org/apache/rocketmq/broker/filter/ExpressionMessageFilter.java#L117) | 请求按topic与queueId挂起；新offset出现且过滤匹配，或等待超时，交回处理器重新执行。不满足条件的请求重新登记。过滤区分消费队列中的标签/位图粗筛和属性表达式精筛。 |

### 用于本地审查的检查点

1. **未知结果保留原请求。** Kafka在排空时保持重试批的producer ID、epoch和sequence，
   并阻止未解决序列之后的新批。对照本地Producer，应检查超时/503后pending内容是否
   仍归原sequence所有；另一个批次不能占用同一序号，重试前不能重建成不同内容。
2. **批处理需要容量、时限和生命周期一起闭合。** Kafka按分区隔离队列，避免在队列锁
   内等待缓冲内存；flush/close也是发送就绪条件。对照本地buffered，需检查满载拒绝、
   linger到期、小批flush、Future完成和关闭排空，不能仅证明正常情况下能合并100条。
3. **长轮询通知只表示值得重查。** RocketMQ取出挂起请求后重新检查offset、过滤和
   超时，再执行请求；不满足则重新登记。对照本地长轮询，应确认等待释放分区/SQL锁，
   每次醒来重新检查已提交水位、延迟队首及组身份，不把“被唤醒”当作已经可投递。
4. **过滤和消费位置是两个约定。** RocketMQ把过滤置于消费队列/提交日志两阶段。
   本地的有界扫描、any-of标签和next_offset应分别验证，尤其是空结果页仍可能扫描了
   记录；提交此位置后改变过滤条件，不能假定旧记录会自动重新出现。
5. **生产者内存缓冲不等于持久投递。** 这些选读代码说明批次和发送状态的管理，不能
   单凭有Future或有flush就推定客户端进程强制退出也不丢消息。跨重启恢复必须另有
   明确协议和持久状态；本地内存pending不应标成持久spool。

## 与本地参考架构的差异

以下按当前[章节映射](chapter20-review.md)和[设计](design_cn.md)确定核对范围；
实际本地测试和本轮修复结果另以[验证报告](../benchmarks/results.md)为准。

- 本地消息经单个SQLite协调器转发到独立HTTP broker；Kafka的生产者按元数据组织
  发往目标broker的批次。本地batch_size/linger只提供同类取舍，不是Kafka完整发送器、
  字节缓冲池或自适应分区算法，也没有移除协调器这一额外跳转。
- 本地sequence按批次推进，pending保存在客户端内存；Kafka选读排空代码按recordCount
  增加序列并关联TransactionManager。二者协议单位不同，不能直接互换回执或声称具备
  相同幂等/事务协议。
- 本地长轮询使用有界HTTP等待和通知/兜底重查；RocketMQ按topic/queue维护挂起请求
  集合。本地标签为精确字符串any-of过滤，没有照搬tagsCode、Bloom位图或SQL表达式。
- 本地定时消息按分区队首阻塞以保序；本次没有阅读RocketMQ定时消息服务，不能用
  上述长轮询源码证明两者调度语义相同。
- 本地all确认要求配置broker多数持久化，leader确认仅暂存；未实现Kafka动态ISR。
  单协调器的持久元数据及队列内process原子边界不能替代分布式共识、协调器HA或
  外部数据库/HTTP副作用的恰好一次处理。

上游源码提供设计对照，不代表本地已经覆盖整套上游能力。零拷贝、动态broker放置、
多topic组协调、增量复制、TLS/ACL/配额、云归档与持续生产规模验收仍需独立实现和验证。

## 章节覆盖与本地源码

完整条目见[章节映射](chapter20-review.md)。本轮重点追踪以下路径，并由独立复核
分别检查存储/Broker、协调器/复制/组，以及HTTP/客户端；源码只修改确认的准入缺口。

| 章节链路 | 实现与检查重点 | 验证边界 |
| --- | --- | --- |
| 分段日志/校验/保留 | [PartitionLog](../src/message_queue/log.py)整批验证、按段批刷盘、失败关闭、先持久epoch后截尾、先建新代后切指针 | 损坏、刷盘/发布故障、恢复与并发测试；非所有文件系统的掉电证明 |
| 独立Broker与复制 | [Broker](../src/message_queue/broker.py)合并同分区冷加载；[Replication](../src/message_queue/coordinator_replication.py)检查前缀/尾hash，达到多数后发布 | 真实独立磁盘和HTTP；受控部分页/故障另外标识；无动态ISR/增量流式追赶 |
| 生产者及批处理 | [Producer/BufferedProducer](../src/message_queue/producer.py)分区条带、冻结pending、连续批sequence、容量/linger/Future/排空 | 相同内容重试、未知结果恢复、并发分区推进、部分成功不被后续错误抹去 |
| 拉取/消费组 | [Coordinator](../src/message_queue/coordinator.py)水位与延迟队首；[ConsumerGroups](../src/message_queue/coordinator_groups.py)读取后重验租约/session/generation与扫描边界 | 通知后实际重读、过滤空页推进、过期成员/旧代拒绝提交 |
| 投递语义/事务 | process先复制输出，再用同一SQLite事务发布输出水位与输入游标，txid/内容重试复用结果 | 仅队列边界的原子性；外部数据库/邮件/HTTP不在事务中 |
| 网络/生命周期 | [HTTP](../src/message_queue/http.py)严格解析、有界处理及长轮询额度；[Client](../src/message_queue/client.py)排空接受操作 | 实际本机HTTP加受控服务/慢响应；未部署TLS/ACL、多地域或生产流量 |

### 本轮修复：为写入及心跳保留HTTP容量

原先所有请求共享同一个handler额度。空长轮询可以占满它，produce及heartbeat只能
得到503；写入无法及时进入并唤醒消费者。获得分区锁后释放再等待，不能解决HTTP
线程仍被占用的问题。

最小修复仅在HTTP准入处增加共享的长轮询额度：

- 只对`/fetch`、`/group-fetch`中合法的正`wait_seconds`取名额；非法值保持400，
  鉴权与JSON/framing校验仍先行，零等待不占额度。
- 最多floor(3×concurrency/4)个，默认32个handler中允许24个长轮询；超额503。
  concurrency=1时额度为0，需零等待或提高配置。
- 额度持有到整个handler退出，覆盖响应写出、异常及deadline清理，避免慢响应时
  提前释放导致预留失效。该规则不保证所有慢正文、慢RPC或调度的公平性。

先新增[回归测试](../tests/test_http_reliability.py)复现两个入口未限流的问题，修复前
该测试的两个子场景均失败。修复后5项新增测试覆盖容量预留、零等待/心跳/写入、
异常复用、单handler和非法等待值、响应阶段继续持有额度。真实HTTP负责验证准入；
等待服务和响应暂停是明确的受控注入，不冒充完整队列部署。

### CodeGraph证据

使用CodeGraph读取当前源码并追踪批次→pending→复制→发布、fetch→组身份复查、
handler→响应→清理等链路。code-review-graph增量刷新成功，重解析3个文件，更新
129个节点/1475条边，无错误；这些是本次更新量，不是全库总量。

限定HTTP文件的直接调用影响分析报告17个文件内节点、27个两跳影响节点和3个额外
文件，另有42条边截断、255处未解析调用。CLI、HTTP及CLI测试在验证范围内；Python
动态dispatch和未解析调用仍需源码与执行补充，图结果不等于完整测试覆盖。第19章
保留的未提交文档也会进入全库差异列表，本轮只修改第20章及对应索引行。

## 本轮验证

Python 3.13.9 / Windows 11 build 26100，标准库/SQLite。修前基线116项通过、35.368秒；
修后完整 **121项通过、35.634秒，无跳过，全部警告视为错误**。最终仅调整测试请求
调度后，HTTP专项27项再通过，2.460秒。

两个真实独立进程示例通过：三个独立磁盘Broker、一个协调器和三个纯TCP代理；
leader停止后b1/epoch 1继续读取，少数副本写入503，恢复后提交offset 2；相同txid
只产生一条输出。批处理/过滤、退组/扩容及定时长轮询结果见
[功能原始报告](../benchmarks/features-2026-11-04.json)。自建进程已停止，诊断文件保留
在忽略的`.runtime/lab-<uuid>/`。

[最终基准](../benchmarks/review-2026-11-04.json)：2客户端、12批×10条、3分区，
120条多数确认且全部读回，0错误，生产阶段1.616秒/74.26条每秒。只有12批样本，
读取不在计时内；未做持续压测，不能据此推断全球吞吐或与上游比较性能。
本轮修复针对准入正确性，没有将小型基准差异声明为性能提升。

详细命令与历史结果见[验证报告](../benchmarks/results.md)。
中英文[设计](design_cn.md)、[需求](requirements_cn.md)、[任务](tasks_cn.md)、README及
[学习指南](learning-guide_cn.md)同步更新，导读增加长轮询容量与释放图。

静态验收：src及修改测试的Ruff E9/F、两个修改Python文件的格式检查通过；32个Python
文件通过3.10语法解析，16份Markdown中的527个本地链接有效，基准JSON可解析。
27份学习指南生成校验通过，本章3张Mermaid图已实际渲染，限定范围的diff空白检查通过。
