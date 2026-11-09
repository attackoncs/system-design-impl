# 第25章数据节点重启效率评审 — 2026-11-09

**问题（已核实）。** `DataNode.__init__` 每次启动都调用 `recover()`；`_recover`
先 `DELETE FROM chunks`，再读取并 SHA-256 哈希打包文件中的每条记录（包括压缩前的
旧版本和删除墓碑）以及每个 `large/*.dat`。重启成本因此正比于全部写入历史，而
SQLite 块索引本身是持久的，且每次 PUT/回收后都已同步提交。书中数据节点是"追加
文件 + 偏移索引"，重启只应处理尚未进入索引的数据。

**修复（`src/object_storage/node.py`）。** 新增单行表 `pack_state(pack,last,end)`：
当前打包文件名、最后一条已索引记录的起点与末尾。`put()` 和 `collect()` 的墓碑在
写入块索引的同一事务中调用 `_advance()`，仅当新记录起点恰好等于已存末尾时才前进。
启动时 `recover(full=False)` 由 `_indexed()` 校验：文件名等于 CURRENT、起点处记录
头可解析、`last+头+长度+尾 == end`、CRC 正确（隐含文件长度不小于 `end`）；通过后只
从 `end` 开始扫描/哈希，与原代码相同地索引孤立记录、应用墓碑、截断不完整尾记录，
并在同一事务中写回新水位。水位缺失（旧索引）或不一致时原样走完整重建；显式
`recover()` 与压缩仍完整重建（压缩后新包只含存活数据，扫描成本与已付出的压缩写入
同阶，未改）。大文件：已有索引行的只核对 `stat` 大小，无索引行的才哈希补索引，
文件消失的行被删除；同尺寸位翻转留给每次 GET 的 SHA-256 校验发现并由 PUT 修复，
与包内已索引记录的处理一致。未改公共 API、线协议和打包/大文件格式。

**崩溃一致性。** `_append` 先 fsync 记录，块索引与水位在其后的同一 SQLite 事务
（`synchronous=FULL`）中提交，所以水位之前的每个字节都已持久且已反映在索引中，
"已索引块必然完整落盘"的不变量不变。追加后、提交前崩溃：水位停在该记录之前，
重启尾扫描会索引它。索引提交失败或回滚失败会置 `needs_recovery` 阻止后续写，且
`_advance` 要求连续，任何外部追加/空洞都不会被跳过，只会让下次重启多扫一些。
压缩在切换 CURRENT 后、重建提交前崩溃：水位仍指向旧包名，不匹配即完整重建。
CRC 损坏记录本身不推进 `last/end`：其后若有有效记录，水位越过它时它的删除效果
已在同一事务提交；若它是末尾记录，水位停在它之前，之后的 `_advance` 因不连续而
不前进，下次重启从该处重放，结果与完整重放相同。

**测量（一次性脚本，未入库）。** Linux WSL2，Python 3.14.4；4 KiB 小块，回收一半
留下墓碑，另有 20 个约 100 KB 大文件，5 次重启取中位数：
4000 次写入（16.7 MB 包）旧 54.6 ms → 新 1.3 ms；20000 次写入（83.5 MB 包）
旧 237.0 ms → 新 2.3 ms。新代码耗时只随未索引尾部与大文件数量（stat）增长。
另用旧代码生成的数据目录验证首次启动回退完整重建并写入水位、伪造水位回退完整
重建、外部追加 5 条记录加半条尾记录只扫描尾部、压缩后重启，结果均正确。

**既有失败的根因与修复（与本次 node.py 改动无关，HEAD 上同样出现）。**
- 深层嵌套 JSON（客户端 400 未返回、上游 409≠503）：Python 3.14 改为按 C 栈大小
  限制递归，`json.loads` 不再对 4000 层嵌套抛 `RecursionError`，而原设计依赖它拒绝。
  `transport.loads` 现在解析后迭代检查深度，超过 `MAX_JSON_DEPTH=64` 抛 `ValueError`，
  与运行时版本无关。
- `ResourceWarning: Implicitly cleaning up <HTTPError …>`：`HTTPError` 是 tempfile
  包装，Python 3.14 下未关闭即被回收会告警；副本/对端失败常被调用方丢弃，告警在
  任意后续用例（如 `test_network` 两例、`test_lost_metadata_ack…`）的 GC 时刻被
  `-W error` 记为失败，故表现为“偶发”。`transport.PeerRejected` 构造时即关闭占位
  文件，`code/reason/headers` 不变，`read()` 从普通 BytesIO 返回受限响应体。
- `test_versions_atomic_failure_and_private_access` 偶发 503：`tools/lab.py` 先取空闲
  端口再由子进程绑定，存在竞态；data-2 绑定失败退出，而 `/health` 未鉴权，被同机
  其他进程应答，实验室误判就绪，该节点从未收到数据。现改为用本实验室密钥做鉴权
  探测并检测子进程是否已退出；启动阶段端口丢失时换一组端口重启全部角色（最多 3 次）。

**验证。** 指定命令需追加 `-o asyncio_default_fixture_loop_scope=function`（否则
pytest-asyncio 弃用警告被 `-W error` 升级为 INTERNALERROR）。修复前 HEAD：136 passed、
4 failed（另一次 135/5）。最终连续 3 次全量：138 passed、54 subtests passed，0 失败
（60.5–68.9 s）。`examples/network_demo.py`、`examples/listing_demo.py`、
`tools/benchmark.py --output .runtime/benchmark-check.json` 均成功，40 个对象逐字节
读回一致。未新增测试。

## 聚合文件滚动（pack rotation）— 2026-11-09

**目标。** 书中数据存储把小对象追加到读写文件，达到阈值后转为只读并新开读写文件，
映射表记录 object_id →（文件、偏移、大小），压缩针对只读文件。此前实现只有一个
随写入历史无限增长的打包文件。本次只实现滚动；放置共识、元数据 HA、每核追加流
仍未做。

**设计（`src/object_storage/node.py`）。**
- `CURRENT` 由单个文件名扩展为文件集合：每行一个名字，封存文件从旧到新，最后一行
  为唯一的活动文件。旧格式（单行）即"无封存文件"，天然兼容。`switch(names)` 写临时
  文件、fsync、`os.replace`，再 fsync 目录。
- `chunks` 新增 `pack` 列。迁移：启动时若缺该列，在同一事务中 `ALTER TABLE` 并把所有
  `kind='pack'` 行填为 CURRENT 中唯一的遗留文件名（旧布局只有一个包）；若旧索引本身
  已陈旧，随后的水位校验照常触发完整重建。新增 `sealed_packs(seq,name,size)`。
- `_append()` 在追加前检查：活动文件非空且追加后将超过 `pack_limit` 时调用
  `_rotate()`。阈值由 `DataNode(pack_limit=…)` 或配置项 `data_pack_bytes`（默认
  256 MiB，配置下限 1 MiB）给定，现有测试不会触发滚动。
- `get()` 按行中的 `pack` 打开文件，且该名必须属于当前集合；墓碑始终写入活动文件，
  即使目标块在封存文件中。
- 启动 `recover(full=False)`：`_sealed_indexed()` 要求 `sealed_packs` 与 CURRENT 的
  封存列表名称、顺序一致且文件大小未变，`_indexed()` 照旧校验活动文件水位；都通过
  才只扫描活动文件的未索引尾部。否则完整重建：按 CURRENT 顺序 `_scan()` 每个封存
  文件（只读打开，不截断；不完整记录直接 409 交运维）再扫描活动文件，重写
  `sealed_packs`。两种路径最后都删除 CURRENT 未列出的 `pack-*.bin`、残留
  `current-*.tmp`，并把封存文件 chmod 为只读。
- `compact()`：先封存非空活动文件，再把全部封存文件中的存活块（按 hash）写入新的
  封存文件（每个不超过 `pack_limit`，`_write_packs()`），fsync 文件与目录后发布
  CURRENT = 新封存文件 + 当前空活动文件，然后完整重建并删除旧输入。压缩只读不可变
  文件，从不改写正在追加的文件。一次合并全部封存文件，所以丢弃其中墓碑是安全的：
  被回收块的旧副本不会留在任何更早的文件里（若只压缩单个文件而丢掉墓碑，完整重建
  会从更早文件"复活"已删块，故未采用）。返回值保留原有字段，另加 `sealed_packs`；
  `/status` 另加 `sealed_packs`、`sealed_bytes`。

**封存不变量与验证。** 封存文件必须已被完整索引且此后不变。`_rotate()` 在写事务中
先用 `_indexed()` 确认水位有效（无效则置 `needs_recovery` 返回 503），再从水位处
`_scan()` 到文件末尾，应用水位之后的完整记录（如重启扫描时遇到的 CRC 损坏末尾记录，
它会让 `_advance` 永远不连续）；健康节点上不存在未确认的半条记录（回滚失败会阻止
服务）。随后记录的 `size` 即封存大小，启动时用 `stat` 比对。

**滚动顺序与崩溃一致性。** 追加始终先 fsync 记录再提交索引（不变）。`_rotate()`：
(1) 写事务中让索引覆盖旧活动文件全部字节；(2) 创建新空文件并 fsync 文件和目录；
(3) 原子发布 CURRENT=[…封存, 旧活动, 新文件]；(4) 同一事务提交 `sealed_packs` 新行与
`pack_state=(新文件,NULL,0)`；(5) 旧文件设为只读。
- (3) 之前崩溃：CURRENT 与索引都还是旧布局，增量启动；新空文件未被列出，启动时删除。
- (3) 之后、(4) 之前：CURRENT 的封存列表多出旧活动文件、活动文件变为新文件，与
  `sealed_packs`/`pack_state` 不一致，完整重建新布局；旧活动文件已在 (1) 前 fsync，
  无数据丢失。
- (4) 之后、(5) 之前：一致，增量启动并补 chmod。
- 运行中 (3) 抛异常：`_publish()` 重读 CURRENT；未变则继续服务并删除新文件，已变则
  采用新集合并保持 `needs_recovery`，直到 `recover()` 成功（与原压缩栅栏一致）；(4)
  失败同样置 `needs_recovery`。触发滚动的 PUT 在滚动失败时不确认。
- 压缩：新文件 fsync 后才发布 CURRENT。发布前崩溃：旧布局完整，新文件未列出被删除；
  发布后、重建提交前：封存列表不一致，完整重建新布局；删除旧输入途中崩溃：残余文件
  未被列出，启动时删除。

**验证（一次性脚本 `/tmp/verify_rotation.py`，未入库）。** `pack_limit=300`：30 次
写入产生 7 个封存文件，均只读且不超过阈值，索引中每块的 `pack` 分布正确；重启只对
活动文件调用一次 `_scan` 且从水位开始；回收位于封存文件的块（墓碑写入活动文件）后
增量重启和完整重建均不复活，重新上传后再回收亦正确；压缩把 29 个存活块合并为 8 个
不超过阈值的封存文件、活动文件为空，重启与完整重建一致；用改动前的 node.py 生成的
单包目录启动：增量扫描、`pack` 列无 NULL，继续写入后正常滚动。崩溃模拟：在滚动和
压缩过程中每次 `os.fsync`/`os.replace`/`unlink` 前后复制整个数据目录（滚动 16 个、
压缩 40 个快照），逐个启动：已确认块全部可读、已回收块不复活、未列出文件被清理、
之后的写入+重启与完整重建均正确。另验证活动文件末尾为 CRC 损坏的未索引记录时
滚动不会卡死。全量测试（未改测试代码）：138 passed、54 subtests passed（54.99 s）。

---

# Chapter 25 source and error-boundary review — 2026-11-05

Read the complete local chapter (lines11925–12813), inspected actual pinned
MinIO multipart and SeaweedFS compaction/recovery source, and used CodeGraph
for local data, metadata, listing, publication and HTTP paths. See the
[source audit and chapter matrix](open-source-review_cn.md).

The existing reference covers the chapter's core bucket/read/write/version/list
paths within documented size and single-host limits. No confirmed new defect
was found in the node, manifest/quorum, catalog, metadata or listing reviews.
The confirmed HTTP defect was excessive nesting in an upstream error body:
`RecursionError` escaped from inside `except HTTPError`, so a valid client
request lost its connection without an API error response. The production fix
adds that exception to the existing neutral503 fallback. Valid upstream409
and invalid client JSON400 remain unchanged. No automatic write retry was added.

One new actual-HTTP regression uses real Service/Catalog/API with a controlled
metadata peer. Six cases cover malformed JSON, duplicate keys, NaN,
4000-level nested409/503 bodies, and valid409. Before the fix the two deep
cases raised a client connection error; after the fix they return503.
It verifies one `/publish`, retained pending intent and subsequent health.
The peer does not commit metadata, so this is not proof of a real lost-ACK
commit. Existing durable recovery tests remain separate. The existing client
JSON test also verifies deep input still returns400.

Baseline **137 tests passed in41.699s**. Final **138 passed in41.769s**, no skips,
`-W error`. HTTP suite:20 passed in2.422s. Both actual six-process examples passed:
1,000,000-byte multipart, historical-version and one-node-down reads match;
listing ownership, mixed one-item-page snapshot, concurrent completion and
stale generation409 are verified. Forty benchmark objects passed exact reads.
[Raw feature evidence](../benchmarks/features-2026-11-05.json),
[raw performance](../benchmarks/review-2026-11-05.json),
[measurement interpretation](../benchmarks/results.md).

Graph increment re-parsed3 files, updating113 nodes and1199 edges without errors;
these are update counts, not repository totals. Explicit direct two-hop impact
for `http.py` reports11 directly changed nodes,23 impacted nodes and3 additional
files, without truncation;56 unresolved call sites remain. The shared handler's
structural risk is high, which guided tests and independent source review;
the graph alone does not prove runtime correctness or full coverage.

Only one production exception tuple changed; this review preserves the existing
bounded concurrent architecture. No real AWS services, commits or pushes were
used. GB streaming, placement consensus/heartbeats, independent AZs, metadata/
catalog HA, multi-pack/per-core append streams, erasure coding, autonomous repair,
online resharding, IAM/TLS/encryption and100PB/SLA validation remain gaps.

---

# Historical Chapter 25 review — 2026-10-26

Read local book lines11925–12813. Private buckets, immutable bytes/version IDs,
delete markers, user-key metadata sharding, checksums, replicated data, packing,
multipart and GC existed. Added owner-bucket enumeration and same-level prefix
listing. Preserved recursive listing and migrated legacy signed snapshots.

Catalog/listing/metadata contracts/chunk I/O/runtime/transport are separate.
Metadata freezes manifests before lock waits, samples clocks after writer
ownership and scans bounded references. Data nodes share reads, serialize writes
and fail closed after uncertain append/index/CURRENT failures until recovery.
HTTP admission32, coordinator operations8, shared replication threads12, upload
stripes64. Ordinary operations overlap; list capture/delete/GC are exclusive.

A cross-review found an actual correctness gap: local RPC timeout did not stop
an accepted remote collect or metadata publish. Durable catalog publication
intents now cover unknown metadata results; maintenance replays them before
scanning references. The node's random generation is captured before that scan
and checked under its writer lock at sweep time. Every accepted PUT (including
duplicate content) invalidates an old sweep. Repairs can defer collection until
the next full scan. No cross-machine clock assumption is required for this
fence. Historical pre-upgrade unknown operations cannot be reconstructed: drain
old work before upgrading. APIs still require one catalog/coordinator owner;
direct metadata writers or incomplete node collect references are unsupported.

Baseline10 tests/29.840s. Final **137 passed/40.784s**, no skips, `-W error`.
Coverage includes writer waits and caller mutation, replay/tombstones, corrupt
references, partial append/fsync/index failures, CURRENT publish interruption,
concurrent reads/compaction, upload complete/abort, exact quorum receipts,
accepted RPC drain, durable lost metadata ACK across restart, pending-recovery
failure blocking GC, and old generation rejection after a duplicate PUT.

The six-process feature demo verified owner buckets1/other owner0; CA/ and NY/
common prefixes plus federal.txt in an unchanged one-item-page snapshot despite
a new ZZ/ object; two recursive CA matches; concurrent multipart completions
returning one version; completed abort409; stale sweep409; verified downloads;
zero unrecoverable chunks. [Feature JSON](../benchmarks/features-2026-10-26.json).
Original demo:1,000,000-byte multipart, historical version and one-node-down reads
all matched, no unrecoverable data; elapsed3986.690ms.

Four-client40-object sample:PUT1.046s/38.23ops/s,p50/p9599.655/124.262ms;
GET0.520s/76.98ops/s,p50/p9548.046/69.440ms. All objects verified byte-for-byte,
40 packed chunks/node,one pack/node,166310pack bytes/node,no large files and no
unrecoverable chunks. [Raw results](../benchmarks/review-2026-10-26.json).
One localhost burst is not a controlled speedup comparison, sustained throughput,
independent-failure-domain durability or100PB/six-nine/four-nine SLA proof.

Graph: final incremental update6files/no errors after initial15file refresh.
Six existing core files79direct nodes/20impacted nodes/5additional files,
344edges truncated,2093unresolved calls. Newly untracked chunk_io.py explicitly
not indexed; source/independent review/execution covered new modules. No git
staging was performed to alter graph coverage.

Remaining: placement heartbeats/consensus, independent AZs, catalog/metadata HA,
multi-pack rotation/per-core streams, GB streaming, erasure coding, autonomous
repair, online resharding, IAM/TLS/encryption and lifecycle/backup/scale testing.
No AWS service, commit or push was used.

Bucket deletion also rejects pending publication intents with409, preventing
recovery from publishing an inaccessible version into an already removed bucket.

The final delete-guard update refreshed2 additional graph files with no errors.
Static acceptance:62 Python3.10 syntax parses,62 Ruff checks/format checks,
20 chapter Markdown files with zero missing local links, and git diff --check.
