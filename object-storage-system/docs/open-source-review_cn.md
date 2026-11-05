# 第25章开源源码对照与审查记录

日期：2026-11-05。对应仓库《System Design Interview.md》第25章（11925–12813行）。
本页记录本次实际搜索和阅读的开源代码，章节验收见[章节记录](chapter25-review.md)，
执行结果见[验证报告](../benchmarks/results.md)。

## GitHub搜索与选材

分别搜索 `S3 compatible object storage multipart`、`SeaweedFS small files object storage`，
再用 `minio`、`seaweedfs` 定位包含服务端实现的仓库。前两项也会返回客户端或空结果，
不能把“支持上传的客户端”当作对象存储服务端。本次选择以下两个真实存储项目：

| 项目 | 固定提交 | 实际阅读的源码范围 |
| --- | --- | --- |
| [MinIO](https://github.com/minio/minio) | `7aac2a2c5b7c882e68c1ce017d8256be2feea27f` | `cmd/erasure-multipart.go` 950–1565行：分片元数据仲裁读取、完成校验、命名空间锁及最终发布 |
| [SeaweedFS](https://github.com/seaweedfs/seaweedfs) | `683c5ad4d3ef63c9ebdd5fc81c717c09517becf0` | `weed/storage/volume_vacuum.go` 1–400、451–564行：压缩副本、增量补齐、提交标记、文件切换与启动恢复 |

检索时MinIO仓库标为归档，SeaweedFS未标为归档；这是检索时状态，固定提交链接用于复现观察。
只阅读列出的源码，未安装、构建或运行上游系统，也未复制实现。以下结论不是完整安全审计或生产可用性认证。

## 参考一：MinIO分片校验与对象发布

[`readParts`](https://github.com/minio/minio/blob/7aac2a2c5b7c882e68c1ce017d8256be2feea27f/cmd/erasure-multipart.go#L996)
并发读取多个磁盘的分片元数据，并按读取仲裁及一致的ETag选择有效信息。
[`CompleteMultipartUpload`](https://github.com/minio/minio/blob/7aac2a2c5b7c882e68c1ce017d8256be2feea27f/cmd/erasure-multipart.go#L1096)
确认upload身份，读取所选parts后[检查编号、ETag、校验值及大小](https://github.com/minio/minio/blob/7aac2a2c5b7c882e68c1ce017d8256be2feea27f/cmd/erasure-multipart.go#L1254)。
普通分片除最后一个外须满足最小大小；完成请求还可以校验组合校验值。校验完成后，
在[对象命名空间锁](https://github.com/minio/minio/blob/7aac2a2c5b7c882e68c1ce017d8256be2feea27f/cmd/erasure-multipart.go#L1443)
保护下将临时上传[发布到最终位置](https://github.com/minio/minio/blob/7aac2a2c5b7c882e68c1ce017d8256be2feea27f/cmd/erasure-multipart.go#L1462)，
使用write quorum，并执行后续rename提交。成功返回对象信息后，完成路径清理上传临时状态。

可以借鉴的是“先验证完整分片集合，再发布对象可见性”的边界；不能因某个分片上传成功就声称完整对象已经可读。
本项目的[`Service.complete`](../src/object_storage/service.py)验证连续分片、整体SHA256/长度，
先持久化`completing`意图，再发布版本。两者的数据组织和协议不同：本地采用三副本内容块，
分片最多1 MiB、总对象最多64 MiB；不复制MinIO的纠删码布局或S3完整协议。

本次没有继续审计MinIO的`renameData`、磁盘后端和全套故障恢复，因此不能从这一个调用路径证明所有文件系统故障点的原子性，
更不能推断本地SQLite元数据具备同等集群能力。MinIO的upload ID/ETag也不等于本项目的业务幂等键；
本地同key重放及未知发布结果必须由自己的持久收据和发布意图保证。

## 参考二：SeaweedFS压缩切换与恢复

[`CompactByIndex`](https://github.com/seaweedfs/seaweedfs/blob/683c5ad4d3ef63c9ebdd5fc81c717c09517becf0/weed/storage/volume_vacuum.go#L136)
记录索引偏移及压缩版本，并生成新的数据/索引文件。
[`CommitCompact`](https://github.com/seaweedfs/seaweedfs/blob/683c5ad4d3ef63c9ebdd5fc81c717c09517becf0/weed/storage/volume_vacuum.go#L182)
在提交锁下调用`makeupDiff`，将复制期间的更新补入新文件。
[`makeupDiff`](https://github.com/seaweedfs/seaweedfs/blob/683c5ad4d3ef63c9ebdd5fc81c717c09517becf0/weed/storage/volume_vacuum.go#L456)
检查原数据文件压缩版本，扫描开始复制后的索引更新，并在提交前同步新数据及索引。

随后先写入并fsync[`.cpc`提交标记](https://github.com/seaweedfs/seaweedfs/blob/683c5ad4d3ef63c9ebdd5fc81c717c09517becf0/weed/storage/volume_vacuum.go#L275)，
再[切换`.dat`和`.idx`](https://github.com/seaweedfs/seaweedfs/blob/683c5ad4d3ef63c9ebdd5fc81c717c09517becf0/weed/storage/volume_vacuum.go#L310)，
最后清除标记。启动时的[`reconcileCompactState`](https://github.com/seaweedfs/seaweedfs/blob/683c5ad4d3ef63c9ebdd5fc81c717c09517becf0/weed/storage/volume_vacuum.go#L363)
依据标记区分继续完成已决定的切换和清理尚未提交的临时文件。
这说明“复制活记录完成”与“新索引/数据成为权威状态”是不同阶段；两个rename本身也不是一个通用的原子操作。

本地[`DataNode`](../src/object_storage/node.py)把小块合并到pack，使用`CURRENT`选择权威代，
从pack重建物理索引；压缩时持有节点写锁，整体方案更简单。这里无需为小型参考实现照搬SeaweedFS的并发增量压缩。
同样不能把SeaweedFS的压缩版本检查等同本地标记清扫的`gc_generation`：前者用于压缩副本与原文件版本匹配，
后者防止扫描引用后才到达的旧清扫请求删除新接收的数据。本地这条跨HTTP不确定结果边界仍需独立测试。

所读代码也区分永久不可读记录与暂时I/O失败；但没有在本次审查中运行上游恢复测试，
不推断所有介质损坏都可恢复或任何平台上都有相同掉电保证。

## 原章与本地范围

| 原章关注点 | 本地实现与范围 |
| --- | --- |
| 创建桶、上传/下载、版本 | 私有桶、不可变内容块、版本与删除标记；不是AWS S3线协议兼容实现 |
| 用户桶列表、前缀同层及递归列表 | catalog与跨元数据分片收集；签名快照分页有行数/内存/有效期预算，不是十亿对象无界列表 |
| 小对象空间与IO效率 | 小块pack及可重建索引；未实现多pack轮转或每CPU写入流 |
| 大对象分片与恢复 | 持久分片/完成状态、整体校验及幂等发布；最大64 MiB，未实现GB流式读写 |
| 数据与元数据分离 | 两个元数据分片、三个HTTP数据节点；拓扑固定，catalog/元数据没有复制选主 |
| 副本与完整性 | 默认每块至少两个有效持久回执，读取验证块及整体SHA256；本地进程不代表独立AZ |
| GC、修复与压缩 | 管理维护扫描、引用保护、发布意图恢复、节点代数栅栏及pack压缩；不是自动分布式回收与灾备 |
| 100 PB、六个九持久性、四个九可用性 | 原章目标，未由本地测试证明；纠删码、共识放置、跨AZ修复、IAM/TLS和生命周期仍需生产工程 |

原章讨论复制和纠删码，并明确本次设计主要采用复制；因此本地没有引入纠删码不等于漏掉核心复制路径。
可靠性判断仍应以本地真实执行的校验、重启、未知发布结果和并发回收测试为准，不能拿开源项目名称替代验证。

## 本次本地修复与验收

CodeGraph核对后，确认了HTTP错误体处理的一个缺口：过深上游JSON在异常处理分支中
触发`RecursionError`，导致API断开连接。本次只扩展该分支的异常捕获，返回中性503；
正常409和客户端无效JSON的400保留，不自动重放写请求，pending发布意图仍供后续恢复。

新增真实HTTP回归包含六种上游响应，使用真实Service/Catalog与受控元数据peer，
验证一次发布、意图保留及服务恢复。peer未实际提交元数据，因此不是实际提交丢确认实验。
修复前两个过深错误体子场景断连；修复后 **138项完整测试通过，41.769秒**，
启用 `-W error`、无跳过。两个真实六进程示例及40对象并发基准通过，
参见本页开头的章节记录和验证报告。中英文文档及3张导读图已同步；
不从本地小样本推断生产耐久性或性能提升。
