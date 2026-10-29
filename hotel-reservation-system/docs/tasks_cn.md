# 任务：酒店预订系统

来源：[System Design Interview.md](<../../System Design Interview.md>)第23章10507–11226行。
核心审查2026-10-26，最终测试/性能见[验证报告](../benchmarks/results.md)，原书覆盖/缺口见
[章节记录](chapter23-review.md)。2026-10-20结果保留历史日期。

## 已完成参考工作

- [x] 明确房型/日期库存、向下取整超售、日价、订单/支付状态、用户授权及事务拥有权。
- [x] catalog/房型增改、软删除、日历管理和订单价格快照。
- [x] 多夜原子预订、报价复核、持久幂等、取消/过期/拒付和未知支付成功补偿。
- [x] 两个独立分片、模拟支付账本、冻结操作契约、lease隔离、自有worker取消/排空及事件日志。
- [x] 拆schema/catalog/booking/saga/cache/transport/routes/lifecycle职责，锁前捕获输入、锁后校验时间。
- [x] 一致快照/revision缓存、固定条带冷合并、有索引的有限历史/jobs/expiry查询。
- [x] 酒店内所有活跃房型的完整日期/数量可用查询。
- [x] 显式滚动策略、有限后台补空日、不复用generation/version/cursor检查、同配置幂等和停用保留。
- [x] 网关显式method/path/query、正文前鉴权、严格JSON/framing、有界直连RPC和准入/关闭拥有权。
- [x] 对确认的持久化/并发/支付/网络缺陷增加行为回归，保留六进程和真实事务前退出验证。
- [x] 同步中英文README/design、本地requirements/tasks，最终执行数据统一引用报告。

## 未完成生产扩展

- [ ] 分片/账本复制、共识元数据、在线扩分片和自动failover。
- [ ] 真实分布式cache/CDC、全局用户索引及负载限流。
- [ ] 真实支付、酒店当地时区/取消政策、税费/多币种。
- [ ] TLS/租户员工ACL、隐私/回执GC、备份和灾难恢复。
- [ ] 百万房间持续/突发负载及目标文件系统掉电验收。

仅用户要求后提交/推送。requirements/tasks延续本地不提交约定，本轮不隐含commit。
已实现SQLite串行writer+条件更新/CHECK；行锁、乐观重试、2PC属于书中可选方案，未冒称全部实现。
