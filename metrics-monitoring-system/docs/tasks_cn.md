# 任务：指标监控与告警

来源[第21章](<../../System Design Interview.md>)8997–9663行，2026-10-26核心审查。
最终测试/测量见[验证报告](../benchmarks/results.md)，[覆盖和缺口](chapter21-review.md)。
本轮不隐含提交推送；2026-10-20历史测试总数不冒充本轮结果。

## 参考实现

- [x] T1. 拆分collection/outbox、discovery、queue、query/rollup/archive、告警配置/评估/
  投递、HTTP和生命周期，保留公共门面兼容。
- [x] T2. 有界目标重载/并行抓取、静态ring归属、可信标签、重试预算及可见失败；错误
  配置保留上次有效目标。
- [x] T3. 持久积压字节计数与分区FIFO lease、有界并行dispatch、每分区原负载不确定
  重试，消费者轮流处理分配。
- [x] T4. 等写锁前冻结摄取输入；基数/类型/不可变时间冲突回滚，临时故障不确认。
- [x] T5. 缓存隔离/冷加载合并、有界一致快照查询及显式group_by的加权gauge avg/min/max。
- [x] T6. dirty桶增量降采样、写锁外计算、revision校验发布，保持counter重置、迟到
  修复和raw/minute/hour接缝完整。
- [x] T7. 归档生成/解压/桶数有界、结构/身份/hash校验、fsync及同身份新文件失败清理。
- [x] T8. worker前原子导入不可变JSON规则；静态labels不改selector；有界快照状态评估。
- [x] T9. 通知claim/结果隔离，claim即计尝试，固定负载/幂等键、FIFO重试/死信。
- [x] T10. HTTP准入/framing/JSON/期限有界，直接HTTP/HTTPS校验、先绑定后启动、关闭排空
  自有handler/worker，暴露后台重试错误。
- [x] T11. 保留真实队列回放/丢ACK去重，新增故障/并发回归及双collector/分区/分组/severity示例。
- [x] T12. 同步中英文README/设计与本地需求/任务，链接真实执行证据并保留历史日期/生产限制。

## 验收

正确性、故障、并发可重复测试；网络能力用独立进程/真实socket证明，受控替代明确标记。
本轮测试总数和原始测量只取benchmarks/results.md。未配商业adapter不宣称真实供应商投递。
仅用户要求后提交，requirements/tasks保留本地约定，design可进入授权提交。

## 未完成生产扩展

- [ ] P1. 共识元数据/发现、自动接管、多主机TSDB HA。
- [ ] P2. 生产TSDB压缩/分片、优先级流处理、完整查询语言和独立可扩展告警事件总线。
- [ ] P3. TLS/ACL、商业通知provider、云冷存储和备份。
- [ ] P4. 完整回执/告警元数据GC、配额和目标规模持续压测。
- [ ] P5. 目标文件系统掉电/灾难恢复验收。