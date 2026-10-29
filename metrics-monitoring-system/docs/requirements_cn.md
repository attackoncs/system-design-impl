# 需求：指标监控与告警（第21章）

来源[System Design Interview.md](<../../System Design Interview.md>)8997–9663行。
2026-10-26审查；最终结果见[报告](../benchmarks/results.md)，缺口见[章节记录](chapter21-review.md)。
仅基础设施gauge/counter，不含日志、分布式追踪或业务分析。

## 可执行核心

- push及可重载pull目标，有界并发抓取、可信labels、目标间隔/重试总预算及up/错误状态。
  静态一致性哈希按显式共享成员分配目标，不冒充共识发现或自动接管。
- 有界持久outbox先接受后发送；按metric/series分片、partition FIFO lease claim、有界
  Dispatcher并行不同队首。稳定批次让传输重复可幂等入库，临时故障不确认未存储数据。
- 第20章真实HTTP broker/独立磁盘传输，组消费者轮询分配；offset过期显式恢复。
- SQLite参考引擎保存不可变样本、精确标签索引、基数预算、一致revision查询及隔离/合并
  冷加载缓存。dirty桶降采样在写锁外计算，以revision校验发布。
- 原始约7天、分钟约30天、小时约365天；迟到数据在raw窗口重建受影响摘要后裁剪。
  counter重置跨层正确组合；gauge按显式标签分组做样本数加权avg/min/max。
- 有界完整小时本地gzip归档，校验hash/结构/身份，独占/fsync及失败清理，不冒充云存储。
- 不可变规则/原子JSON导入、静态severity标签、有界快照评估、持续pending/firing/resolved/
  no_data语义、静默、冻结通知分组、lease/重试/退避/死信。
- 实际自有HTTP webhook；邮件/电话/PagerDuty由Provider扩展，外部去重依赖接收方配合。
- 鉴权query/SVG/dashboard；HTTP准入/协议/JSON/期限有界，出站直接传输，关闭排空自有后台。

## 限制和验收

默认总10000序列/单指标1000/最多8标签。查询最多200源序列/200000输入原子/20000输出点；
rollup每层20000键/总200000原子，revision最多3次尝试。归档最多100000桶/10000序列，
压缩和解压各64MiB。超限明确拒绝，本机测试不代表生产规模或查询SLA。

真实进程/网络验证与时钟/RPC受控故障分开。基础fixture13进程，章节功能fixture14进程
含两个静态ring归属的collector；验证重放、no_data、层级接缝、迟到、分组及丢ACK幂等ID。
最终数字只引用本轮执行，2026-10-20证据保留历史日期。配置目标不等于授权向外部发送消息。

## 生产扩展

生产TSDB压缩/分布、共识发现/HA、动态collector接管、优先级/Flink、独立告警事件总线、
完整PromQL/Grafana、TLS/ACL、商业provider凭据、云保留、元数据GC、备份、千万序列持续
规模及掉电验收均待完成。SQLite及队列单协调器是明确边界。

用户要求后才提交；requirements/tasks保持本地不提交约定，design可在后续授权提交中包含。