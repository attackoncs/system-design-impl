# Chapter 2 implementation tasks

Status: Reference experiment complete. 73 tests pass; 1/8/10-process demo and measurements have been run. requirements.md / tasks.md stay local per existing convention.

- [x] Single-machine and balanced/sharded topologies, real HTTP, and independent persistent files.
- [x] Stateless web, shared sessions, load balancing, and request-level failure fallback.
- [x] TTL/LRU cache, strong reads / min-version reads, and consistency boundary.
- [x] Replication sequence / checksum, static sharding, restart recovery, and mapping-change protection.
- [x] Atomic outbox, leases, idempotent consumption, and ACK-loss recovery.
- [x] Local edge-cache model, versioned resources, and diagnostic counters.
- [x] Tests, bilingual README, design notes, and measured comparison report; root index updated.
- [ ] Production extensions: unattended database HA, cross-host regional disk sync, live migration, a real CDN/GeoDNS service, and million-user validation — not included in this reference experiment completion statement.

## 2026-10-29 chapter gaps

- [x] Local GeoDNS: location preference, full failover to the surviving center, stable unknown locations.
- [x] Explicit fenced promotion of a caught-up replica for profile writes; lagging replicas and live leases are refused; sessions stay on the old primary.
- [x] LRU default plus LFU/FIFO, chapter `set`/`get` text values, and a cache cluster that skips a failed member.
- [x] 73 pytest cases. The 2026-10-27 measurement of 66 remains the benchmark log; it was not re-run.

## 2026-10-27 review

- [x] 冻结输入/回执，单clock与未过期租约确认，源库隔离消费。
- [x] 缓存字节预算、代际回填栅栏/版本下限；源绑定/前缀校验及复制整批原子。
- [x] 在线Web池、边缘失效/客户端回源、严格协议/有界连接和退出排空。
- [x] 66项完整pytest、1/8/10阶段与9进程新示例、实测和双语文档。
- [ ] 真实主库HA/GeoDNS/多地域/CDN/自动伸缩及在线迁移、生产监控/容量。
