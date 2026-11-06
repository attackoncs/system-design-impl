# 第2章复审验证（2026-11-06）

基线 **73项/51.29秒**；最终完整 **77项＋5个子用例/51.35秒，-W error，无跳过**。新增3项租约竞态/回滚测试及1项无效TTL测试；后者逐一覆盖True、inf、nan、0、-1。旧代码先复现租约竞态和缓存旧值丢失，再验证修复。将租约等待用例加强为实际SQLite写锁后，12项＋5子用例再次通过（0.55秒）。

```powershell
$env:PYTHONPATH = 'src'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -W error -m pytest tests -q
python examples/network_demo.py
python examples/resilience_demo.py
```

[本轮阶段示例](stages-2026-11-06.json)顺序运行single/balanced/sharded，分别1/8/10个独立进程，三次资料读取均version=1，任务和副本已追平。[本轮9进程韧性示例](resilience-2026-11-06.json)验证旧缓存回填被拒绝、version=2、Web池缩容/扩容、edge失效/故障回源及唯一2次消费。全部回环HTTP和合成资料，实验只清理自己的进程/临时文件；不是跨主机或线上迁移。

本轮未重跑性能样本，不用同时进行其他项目测试时的数字作提速比较。默认阶段仍未挂载提升Fence；新的共享Fence临界区是单机原盘实验保护，并不是跨两个SQLite文件的崩溃原子事务或生产选主。详见[章节覆盖/边界](../docs/chapter02-review.md)和[固定开源源码](../docs/open-source-review_cn.md)。

---

# 2026-10-27 历史复审实测

完整pytest66项/51.08秒，-W error、无跳过；隔离HEAD基线19项/33.87秒。旧unittest只跑11项，README已纠正。验证命令：

```powershell
$env:PYTHONPATH = 'src'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -W error -m pytest tests -q
python examples/network_demo.py
python examples/resilience_demo.py
python tools/benchmark.py --requests 30 --clients 4 --output benchmarks/review-2026-10-27.json
```

新增用例覆盖冻结竞态、过期ACK/负载源校验、长用户任务ID/单clock、源绑定/原子复制、字节预算/代际版本下限、错身份回执/未来版本、在线路由快照、严格协议/生命周期和客户端回源。原网络故障与会话重启继续通过。

每阶段30读/4客户端、暖缓存、Windows11/Python3.13.9。计时不含启动/写种子/追平/预热；最终采样未同时运行测试或图构建。HEAD通过git show复制隔离运行，未改变检出或索引。

| 阶段 | 基线QPS | 本轮QPS | 基线P95ms | 本轮P95ms | 本轮新增主库资料读 |
|---|---:|---:|---:|---:|---:|
| single | 147.03 | 145.58 | 55.204 | 61.489 | 30 |
| balanced | 70.33 | 69.46 | 89.021 | 67.816 | 0 |
| sharded | 74.90 | 67.30 | 77.660 | 75.340 | 0 |

不是全面提速：吞吐略降，single P95上升，distributed P95下降；小样本无法作受控归因或稳定尾延迟证明。新增协议/取消/校验有成本，分层的意义包括减少资料主库读；会话鉴权仍访问主库。百万用户/线性扩容没有证明。

[HEAD原始样本](baseline-2026-10-27.json)、[本轮样本](review-2026-10-27.json)、[原阶段演示](stages-2026-10-27.json)、[9进程新演示](resilience-2026-10-27.json)保留，Oct21的latest.json不覆盖。新演示含生命周期6.367074秒，拒绝旧填充/版本2/三Web同会话/失效0项/回源/唯一2事件均通过。

[章节对照与图盲区](../docs/chapter02-review.md)说明首个旧源绑定/旧inbox迁移、缓存最终一致、SQL不可强杀。2026-10-27 记录的主库 HA/GeoDNS 缺口里，本地 GeoDNS 目录和显式资料提升已在 2026-10-29 补上；生产级多地域 HA 与真实 GeoDNS 服务仍未实现。本报告的 66 项计时是当时的样本。

---

# 初始实现历史（原数据保留）

# 第2章测试与实测报告

2026-10-21，Windows11（10.0.26100）、Python3.13.9、单机回环HTTP和SQLite WAL/FULL同步。全部为合成用户资料，未连接云服务或真实CDN。

## 重复执行

本目录设置 `PYTHONPATH=src`，TEMP/TMP设置到已创建的 `.runtime/tmp`：

```powershell
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --requests 30 --clients 4
```

完整测试 **11项通过，33.430秒**，包括7项组件测试及4项真实HTTP拓扑测试。随后完善worker的逐分片异常隔离，对应10进程故障测试单独再次通过，13.178秒。演示、压测与测试分开运行。

| 验证项 | 实际结果 |
|---|---|
| 单机持久化 | 同一会话在进程重启后可读取原版本资料，后台任务已确认 |
| 无状态Web | 同一会话交替访问web0/web1，资料一致，无粘性会话 |
| Web停机 | 轮询命中已停止web0时回退web1，fallback计数增长 |
| 缓存故障 | 缓存停止后绕过缓存读副本/主库，重启冷缓存不丢持久资料 |
| LRU/TTL/版本 | 正确淘汰最近最少使用项、到期失效、min_version排除旧缓存 |
| 主库读减压 | 命中缓存不增加主库profile_reads；会话校验仍依赖主库 |
| 分片 | 两个用户落到不同主库/读副本，每组各1人 |
| 读副本停机 | 对应读取回退主库；重启补齐后版本追平 |
| 主库故障 | 该分片强读503，不错误提升不完整读副本；另一分片继续读写和处理任务 |
| 主库重启 | 原会话和版本化资料保留 |
| 幂等与CAS | 同键12并发只写一次；不同键竞争同expected_version仅一笔成功 |
| 复制校验 | 跳序、冲突和校验和不符拒绝，重复相同变更不再应用 |
| 任务确认丢失 | 消费至少两次但唯一事件1条；重启worker后源队列排空；旧令牌不能确认新认领 |
| 映射变更保护 | 持久数据拒绝直接由1分片改2分片，需要迁移或新实验目录 |
| 边缘模型 | 版本化资源分别缓存，重复读取命中；停止edge后显式读取origin可用 |

## 演示输出

`network_demo.py` 分别启动1/8/10个独立进程。balanced主库含两个用户、序号2，消费者两个唯一事件；sharded两个主库各一用户、各序号1，消费者同样两个唯一事件。分层读取依次出现副本、缓存、缓存，Web节点交替。全部资料版本为1，任务排空；edge输出带“local cache; not a real CDN”标记。

## 小样本性能

原始结果：[latest.json](latest.json)。每阶段各30次GET、4个并发客户端，登录/写入/副本追平并预热缓存后计时：

| 阶段 | 进程 | 读取/秒 | p50毫秒 | p95毫秒 | 新增主库资料读取 |
|---|---:|---:|---:|---:|---:|
| single | 1 | 158.74 | 22.15 | 41.89 | 30 |
| balanced | 8 | 77.07 | 49.33 | 75.14 | 0 |
| sharded | 10 | 74.47 | 48.18 | 75.41 | 0 |

两个分层阶段各30次缓存命中、1次预热未命中，容量32/TTL2秒。本样本查询一个已预热用户；两个分片均已写入数据，但**不测量均衡分片总容量**。新增网络层在小流量下降低吞吐，不能据此声称扩容提速；同时确实减少了主库资料查询。

计时包含LB、Web、会话校验和资料读取，排除启动、用户生成、预热及最终诊断查询。profile_reads只统计资料查询，不代表所有数据库查询：会话验证仍访问主库。没有模拟高数据库延迟来人为制造加速结果。

未验证百万用户、长时间容量、跨主机/多地域、GeoDNS/真实CDN、自动数据库提升、复制会话、在线迁移或生产SLA。读副本、缓存和多个Web只处理已验证的部分故障，不让整个系统自动获得高可用。
