# 第16章：文件同步核对

## 2026-11-09 效率修复

问题一：`SyncWatcher.run_once` 每轮（默认1秒）对每个扫描到的文件调用
`client.push`，后者读全文件并对每块做 SHA-256 后才与上次全文件哈希比较，即每轮
读取整个同步目录。修复：客户端 `files` 表新增 `size`、`mtime_ns`（旧库按缺失
列 `ALTER TABLE ADD COLUMN`，NULL 表示未知需哈希）；`push` 哈希确认未变时记录
哈希前的 stat，`remember` 在任何远端更新时清空；watcher 遇到 stat 一致且无待恢复
会话的文件直接跳过。mtime 距今不足2秒的文件不记录（规避粗粒度时间戳下同一时间片
内的同大小改写），显式 `push` 总是重新哈希。同大小且同 mtime 的编辑会被 watcher
漏掉，这是 rsync/Dropbox 式 stat 检测公认的取舍，已写入 README 与设计文档。

问题二：每次上传 `begin` 调用 `quota`，对所有者全部空间的全部修订 SUM(size)，
且 `spaces(owner)` 无索引。修复：`users.used` 计数器在 commit（+size）与
prune（−删除修订 size）同一事务内维护；删除墓碑 size 为0不变；打开旧库时在
`BEGIN IMMEDIATE` 中一次性加列并回填；新增 `space_owner` 索引服务预留查询。

实测（/tmp 一次性脚本，旧代码取自 HEAD）：200个1MiB文件稳定状态一轮 watcher
由 395 ms 降至 25 ms（页缓存热，冷盘差距更大），编辑后仍正确上传；2万修订
（旧代码建库）`quota()` 由 16.53 ms 降至 0.017 ms，新代码打开旧库回填值与
SUM 一致，随后 prune 后仍一致。

验证：完整严格套件（`-p no:asyncio`，共享环境新装 pytest-asyncio 的弃用警告在
`-W error` 下中断收集）修改前后均为 **119 passed、3 failed**；3项为既有失败
（Python 3.14 下未关闭 HTTPError 的 ResourceWarning，及一项时序敏感的 HTTP
过载用例），新旧代码失败信息相同。

### 事件保留与重新快照（完成原后续项的保留部分，持续压测仍开放）

策略：事件日志按工作区有界，默认保留30天、每工作区最多10万行（`Drive(event_max_age,
event_max_per_space)` 与 CLI `--event-max-age`/`--event-max-per-space`，0/None 关闭）。
新表 `event_spaces(space, floor, retained)`；每次 `emit` 在同一写事务内最多裁剪本
工作区 `max(64, 2×新增行)` 行最旧前缀（`events(space,seq)` 索引读取 + 范围删除，
计数器替代 COUNT 扫描），`cleanup` 调用 `expire_events` 清扫空闲工作区。高水位改读
`sqlite_sequence`，修复全部裁剪后 `MAX(seq)` 回退导致"cursor beyond log"的隐患。
旧库一次性加 `events.created`（取打开时间）并回填计数。

一致性论证：裁剪只删前缀且同事务抬高 floor，故"seq ≤ floor"恰为已删集合。
`changes` 在同一读事务内比较游标与用户所在工作区的 floor：游标落后则返回空事件、
原游标与 `resnapshot: true`（长轮询立即返回），否则游标后的事件全部仍在。
`POST /snapshot` 在一个 `BEGIN IMMEDIATE` 事务内读 head、高水位 H 并记录投递 H；
H 之后被裁剪的事件会把 floor 抬过 H，下次轮询再次触发重新快照，不会静默丢失。
客户端 `DeviceClient.resnapshot` 只对本地版本更旧的 head 调用既有 `apply`
（脏文件保留为 local-conflict 副本，墓碑删除），成功后才持久化并确认 H。
检查偏保守：用户任一工作区的裁剪都会触发一次快照，版本未变的文件不发请求。
响应字段均为可选新增，事件线格式不含 `created`。

验证：/tmp 一次性脚本（未改测试/基准代码）——每工作区上限5行时连续21次上传后
`retained` 与实际行数均为5；离线设备收到 `resnapshot`，快照后得到最新内容且本地
未同步编辑被保留为冲突副本，随后增量事件恢复正常；按时钟推进后 `cleanup` 清空
空闲工作区事件，全部事件裁剪后游标仍有效；未绑定新设备通过全工作区快照取得所有
文件；HEAD 代码建立的旧库打开后正确回填并按策略裁剪；`EXPLAIN QUERY PLAN` 确认
裁剪读取、范围删除和 floor 检查均走索引。既有完整严格套件 **122 passed**（13.87秒）。

## 2026-11-02 开源源码复核

结合CodeGraph重新核对第16章，并阅读Syncthing、Seafile块校验、晚编辑和冲突替换
实际源码。本轮修复客户端误接受截断HTTP响应：固定长度未收齐或chunked截断转为
可重试I/O错误，失败不推进游标；同时显式关闭测试中4个错误响应。

新增6项真实HTTP回归，修复前4失败、2通过；修复后完整严格套件
**122 passed（20.06秒），无跳过**。独立API与设备进程实测再次确认仅传变化块和
服务重启后的游标恢复。完整来源、覆盖、命令和部署边界见
[本轮核对记录](open-source-review_cn.md)。

## 2026-10-24 历史核对

对照《System Design Interview.md》5369–5878行。本轮完成核心链路核对，部署范围
仍为单主机 SQLite/本地加密块，不把进程锁当跨地域复制。

| 原书要求 | 实现 |
| --- | --- |
| 任意类型、十GB、上传下载 | 最大10GiB声明；简单上传限一块，大文件用断点会话；未实传10GiB |
| 压缩、加密、增量 | 固定块、zlib、AES-256-GCM、SHA-256；只传变化块，非位移型rsync |
| 元数据和历史一致性 | WAL事务、不可变版本、配额预留、引用计数；ACL/文件头实时查询 |
| 首处理者获胜、两份冲突 | 条件修订、稳定冲突副本、晚到本地编辑保护 |
| 简单/可恢复上传 | PUT /file和begin/block/commit复用同一状态机 |
| 分享、修改/删除通知、离线追赶 | 持久事件、设备交付/确认游标、分享撤销及历史裁剪恢复 |
| 上传中/完成通知 | pending/upsert/cancelled与元数据同事务，未完成文件仍隐藏 |
| 自动多设备同步 | 有扫描上限、工作区绑定和失败退避的SyncWatcher，非原生OS watcher |
| 元数据缓存与强一致 | 只缓存不可变清单，128项/8MiB编码输入；仍查询当前权限和文件头 |
| 版本保留、冷层 | 显式prune、保守GC、幂等本地归档/恢复；非云Glacier |

## 工程和并发

拆分schema/manifests/paths/lifecycle/notifications/client_transport/watcher职责。
压缩、加密、全文件校验和下载移出全局锁，最终事务复核会话、ACL和对象存在性。
密文实际读取/解压有界；固定进程/线程键锁协调块读写、归档、恢复和GC。过期状态
先提交再删对象，失败不会恢复已取消预留；宽限不是持久pin。

空闲长轮询仅读，返回时写一次交付并复核可见性。本地唤醒配合跨进程定期读；
默认32个HTTP槽位最多24个长轮询，保留前台容量。严格framing/JSON、绝对套接字
期限、关闭取消轮询并排空处理器/定时器，Drive等待已接受I/O后关库。

客户端OS锁独占目录，绑定工作区、默认64MiB明文磁盘缓存，原子保护冲突副本；
下载后复核晚编辑，按接受的精确修订对账，整页应用成功才持久游标/确认。生成的
保护副本保持本地直到明确处理；自动循环有目录项上限和有上限退避。

## 图分析与验证

code-review-graph最小上下文、长函数、调用及增量影响分析定位commit/begin、
push/apply和嵌套HTTP入口。四个核心文件影响另外五个文件、15个节点，覆盖CLI、
网络示例和故障回归。新增未跟踪模块及Python动态调用有索引缺口，图风险和测试
缺口只作提示；以源码交叉复核和执行确认。

完整回归 **116 passed（21.08秒）**：真实HTTP、独立设备、跨进程块锁、自动创建/
修改/删除和离线重开、丢失响应、晚编辑、撤权/取消竞争、长轮询容量及关闭。
Ruff E9/F、改动代码格式、Python3.9语法和差异检查通过。

独立进程复验12,288字节/三块：首次同步890.183ms，修改中间块后106.635ms，仅
下载4,096字节，复用8,192字节，API重启后游标恢复。小样本不能证明生产吞吐。
[原始报告](../benchmarks/review-2026-10-24.json) · [测量说明](../benchmarks/results.md)

## 边界

未接入跨地域对象复制、元数据主备、生产事件保留/重新快照、TLS终止、KMS及原生
移动端监控。GC和元数据写仍串行，缓存预算不代表RSS；系统DNS、SQLite和文件调用
不能被套接字期限强制终止。设备明文缓存需磁盘保护，服务密钥随元数据备份，非端到端加密。

[英文设计](design.md) · [中文设计](design_cn.md)
