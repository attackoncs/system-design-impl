# 第20章：从一次安全重试理解分布式消息队列

消息队列最重要的不是put/get接口，而是**什么算已经提交、重复请求怎样识别、消费者进度由谁确认**。项目用协调器保存元数据和生产回执，Broker保存分区日志，消费组单独管理偏移。

本文先选一个topic、一个partition、一个producer，acks=all。多个Broker通过本地真实HTTP协作；协调器元数据仍是单点参考方案，不能把副本日志等同于元数据高可用。

## 用同一个批次走完全程

producer=p、epoch=0、sequence=0，向input分区0发送一条value=hello。

1. 首次提交获得offset；把这个响应记为R。
2. 同一生产身份、序号及内容并发重试，所有成功响应都应等于R，fetch只得到一条hello。
3. 同一序号改成另一个内容，应冲突，不能覆盖原日志。
4. 消费后但提交offset前崩溃，重读可能再次拿到hello；业务处理需考虑幂等。
5. 如果选择acks=leader，返回的committed=False只是暂存确认，不能当作acks=all的已提交结果。

先理解一个分区内的这组保证，再看分区扩容、消费组重平衡和原子process。

## 宏观：日志与进度分开放

```mermaid
flowchart TB
  P["Producer SDK"] -->|"生产身份与sequence"| C["Coordinator"]
  C --> M[("元数据、pending与receipts")]
  C -->|"同步复制协调"| B["Broker"]
  B --> L[("分区日志与索引")]
  C --> R["其他Broker副本"]
  U["Consumer"] -->|"fetch"| C
  U -->|"commit进度"| G["ConsumerGroups"]
  G --> M
```

| 数据 | 关键字段 | 含义 |
|---|---|---|
| 生产身份 | producer、epoch、sequence | 新epoch隔离旧生产者，sequence约束批次顺序 |
| 分区日志 | offset、value、hash链 | 分区内有序的持久记录 |
| pending | 尚待完成的批次 | 未完成确认可继续恢复 |
| receipts | 批次fingerprint与response | 相同内容重试返回原结果 |
| parts.hwm | 已提交高水位 | fetch不能越过它读取未提交尾部 |
| parts.floor | 最早保留偏移 | 过旧消费者必须显式重置 |
| 消费组进度 | session、generation、提交offset | 旧成员不能提交新一代的进度 |

高水位表示可读提交边界；消费者提交offset表示业务消费进度。二者不是同一个数字。

## 七站源码路线

1. {{symbol:src/message_queue/producer.py:Producer.send}}：客户端怎样持有待确认批次，未知结果时为什么不能换一个新payload。
2. {{symbol:src/message_queue/coordinator.py:Coordinator.produce}}：规范化消息，确定分区；分区锁与producer锁保护哪两个维度。
3. {{symbol:src/message_queue/coordinator.py:Coordinator._produce}}：检查epoch、回执、序号；输入相同身份和内容，输出原响应或新的批次。
4. {{symbol:src/message_queue/coordinator.py:Coordinator._flush}}：恢复pending、复制并finalize；这是暂存到提交的桥。
5. {{symbol:src/message_queue/log.py:PartitionLog.append}}：观察offset、哈希链与磁盘追加，区分完整损坏和可恢复尾部。
6. {{symbol:src/message_queue/coordinator.py:Coordinator._fetch}}：只读floor到hwm范围，延迟头消息如何影响后续消息。
7. {{symbol:src/message_queue/coordinator_groups.py:ConsumerGroups.commit}}：检查会话及generation后再前移组进度。

## 微观一：幂等不是只记一个请求ID

{{excerpt:src/message_queue/coordinator.py:Coordinator._produce:0:17}}

按代码顺序理解：

- epoch不匹配先被fence，旧生产者不能靠旧序号继续写。
- fingerprint绑定消息内容，同一序号携带不同payload返回409。
- 有合法receipt时返回保存的response；不再分配新offset。
- 没有receipt还要继续检查pending和expected sequence，不能直接追加。

所以“客户端超时后重新生成一个sequence”并不是安全重试方式。先恢复原批次的结果，才能知道下一批的身份。

```mermaid
sequenceDiagram
  participant P as Producer
  participant C as Coordinator
  participant M as pending／receipt
  participant B as Broker副本
  P->>C: produce(p,epoch,seq=0,hello)
  C->>M: 校验身份并保存pending
  C->>B: 复制同一批次
  B-->>C: 达到确认条件
  C->>M: finalize高水位与receipt
  C-->>P: R
  Note over P,C: 假设响应丢失
  P->>C: 相同身份、序号、内容重试
  C->>M: 查询receipt并核对fingerprint
  M-->>C: 保存的原响应R
  C-->>P: 原响应R
```

网络复制与SQLite事务不是同一个事务。pending让中间阶段可以被恢复；读者仍受高水位限制，不能把未完成事务尾部当作已提交消息。

## 微观二：延迟消息不能被后面的消息超车

{{excerpt:src/message_queue/coordinator.py:Coordinator._fetch:22:8}}

例子把同一批次设置成第一条later延迟10秒，第二条behind立即可投递。在10秒前，fetch返回空，而不是跳过later先给behind。

原因是遇到not_before尚未到期的头消息就break。与tags过滤不同，延迟头要保留分区投递顺序；过滤掉的标签消息则仍可能推进扫描位置。

## 两类失败不要混淆

**生产响应丢失**：原批次可能已经提交，按原身份重试，receipt或pending负责收敛。

**消费后未提交进度**：消息事实没有丢失，但业务可能再次处理它。队列不能自动把外部数据库写入与消费commit变成一个事务。

**保留窗口已删除旧偏移**：fetch返回410，要求显式选择重置位置；不能假装旧消息仍存在而无声跳过。

## 对照测试看保证

- {{symbol:tests/test_coordinator_reliability.py:CoordinatorReliabilityTests.test_direct_same_sequence_concurrent_retry_is_one_committed_batch}}：六个并发重试结果相同，分区只出现一条消息。
- {{symbol:tests/test_state.py:StateTests.test_delayed_head_preserves_order}}：10秒前为空，之后顺序为later、behind。
- {{symbol:tests/test_cluster.py:ClusterTests.test_transport_partition_provisional_ack_and_quorum_recovery}}：区分leader暂存确认与quorum恢复后的提交。
- {{symbol:tests/test_state.py:StateTests.test_retention_floor_receipt_expiry_and_new_append_chain}}：理解过期offset与已失效回执为何不能被随意复用。

## 自查与练习

**相同key的消息就全局有序吗？** key帮助选择分区；保证首先是分区内顺序，不是所有topic的全局顺序。

**acks=leader返回offset就能消费吗？** 不能据此推导已提交，应检查committed与高水位契约。

**同序号不同内容能作为更新吗？** 不能，这是身份冲突，消息日志不是按序号覆盖的可变表。

练习：提交hello后原样重试，再用同序号提交world。预期原样重试得到同一响应，world冲突，fetch始终只有原hello；再换合法下一序号提交world，观察offset连续增长。
