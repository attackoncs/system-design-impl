# 第 25 章：类 S3 对象存储

标准库 Python 参考实现：1 个目录/API 协调器、2 个元数据分片、3 个数据节点，共 6 个真实 HTTP 进程。支持私有桶、版本/删除标记、校验和、多部分上传、小对象聚合、稳定列举、修复与垃圾回收。

要求 Python 3.10+，无额外依赖。不代表 AWS S3 协议兼容或生产耐久性 SLA。

## 运行

在本目录执行：

```powershell
$env:PYTHONPATH = 'src'
python examples/network_demo.py
python -m unittest discover -s tests -v
python tools/benchmark.py --output benchmarks/results.json
```

实验自动分配本机端口、生成私有凭据、启动并清理自有进程。忽略提交的 `.runtime/lab-*` 保留数据/日志。系统临时目录受限时创建 `.runtime/tmp`，将 `TEMP`/`TMP` 设为其绝对路径。见 [验证报告](benchmarks/results.md)。

## 手动启动

```powershell
$env:PYTHONPATH = 'src'
python tools/init_config.py
# 每条分别在独立终端运行，设置相同 PYTHONPATH：
python -m object_storage.cli data --config .runtime/secrets.json --index 0 --port 9853
python -m object_storage.cli data --config .runtime/secrets.json --index 1 --port 9854
python -m object_storage.cli data --config .runtime/secrets.json --index 2 --port 9855
python -m object_storage.cli metadata --config .runtime/secrets.json --index 0 --port 9851
python -m object_storage.cli metadata --config .runtime/secrets.json --index 1 --port 9852
python -m object_storage.cli api --config .runtime/secrets.json --port 9850
```

生成器拒绝覆盖，提供 alice/bob、内部/管理令牌和独立分页签名密钥。默认 `write_quorum=2`，可设为 3。服务仅绑定回环地址。单协调器、固定路由，不支持多活或直接改变分片数。

## 对象接口

用户 Bearer 令牌只能访问自己的桶：

| 接口 | 功能 |
| --- | --- |
| `PUT/GET/DELETE /v1/buckets/{bucket}` | 创建/详情/删除空桶，始终版本化 |
| `PUT /v1/buckets/{bucket}/object?key={key}` | 原始字节，最大 4 MiB；必需 Idempotency-Key、X-SHA256 |
| `GET /v1/buckets/{bucket}/object?key={key}&version={可选}` | 原始字节，响应含版本 ID、SHA256 |
| `DELETE /v1/buckets/{bucket}/object?key={key}` | 增加删除标记，需幂等键 |
| `DELETE /v1/buckets/{bucket}/object?key={key}&version={id}` | 永久移除指定版本/标记 |
| `GET /v1/buckets/{bucket}/objects` | 排序列举，prefix/versions/limit/token |
| `POST /v1/buckets/{bucket}/retention` | `{keep:N}` 保留每个键最新 N 个有效版本 |

键是最多 512 字节的平面 UTF-8 名称，可含 `/`，查询参数需 URL 编码。桶名为 3–63 个小写字母、数字、连字符。同桶/键/请求键同内容返回原版本，不同内容返回 409。旧凭据不会复活已移除版本。删除标记只隐藏最新对象，移除标记可显示上一版本。删桶前需移除所有有效版本/标记及未完成上传。

可直接运行的 Python 示例见 [英文说明](README.md)。

## 多部分上传

| 接口 | 含义 |
| --- | --- |
| `POST /v1/buckets/{bucket}/uploads` | `{key,request_key,sha256,size}`，活动期 1 小时 |
| `GET /v1/uploads/{id}` | 查看持久化状态和已接收部分 |
| `PUT /v1/uploads/{id}/parts/{number}` | 编号 1–64，每部分 1 字节–1 MiB，必需 X-SHA256 |
| `POST /v1/uploads/{id}/complete` | 校验连续编号、整对象大小和哈希后发布 |
| `DELETE /v1/uploads/{id}` | 终止活动上传，已冻结完成意图不可终止 |

总对象最多 64 MiB/256 块，块最大 256 KiB；同部分编号内容不可变。API 重启后可读状态续传，完成前对象不可见。完成意图先持久化再跨服务发布，异常时重试或维护可幂等恢复。过期只终止 active，不猜测取消 completing。

## 一致性与维护

- 每块尝试写入 3 个独立节点，默认至少 2 个持久化确认才发布元数据；未达门槛返回 503，不暴露半对象。2 确认不等于已有 3 份。
- 读取绕过损坏/不可用副本，验证整对象 SHA256 和长度；没有好副本则失败。节点同处本机，不代表独立硬件故障域。
- 跨分片列举冻结最多 10000 行，页大小 1–1000，签名 token 有效 5 分钟并绑定用户/桶/查询。新增或删除标记不改变分页序列；冻结行不保证已永久删除版本仍可下载。
- 小于等于 64 KiB 的块共享带校验/删除墓碑的聚合文件，偏移索引可重建，不完整尾记录可恢复。大块单独保存。压缩写新代际后原子切换 CURRENT。

管理令牌调用 `POST /admin/maintenance`，正文 `{"grace":60}`，先恢复 completing、终止过期 active，再读取全部元数据/上传引用、巡检修复并回收超宽限的无引用块。缺失任一元数据来源即停止回收。返回 repaired_copies、copy_failures、unrecoverable 等；最近删除版本在宽限内受保护，其他版本/上传仍引用的共享块不会删。最多 50000 活跃块，超预算明确失败。测试显式用宽限 0，正常默认 60 秒。

节点 `/status`、`/chunks/{hash}`、POST `/collect`、POST `/compact` 需内部令牌。修复/回收后可压缩；不得绕过协调器使用不完整引用集合触发回收。

生产边界：目录和元数据无复制/选主，普通操作在单协调器内有界并发，GC、删桶与列表快照受独占保护。无 AWS 签名、IAM、TLS、加密、纠删码、在线迁移或自动跨机修复；下载在内存组装，不是 GB 流式服务；凭据/历史元数据无自动归档。文件/目录故障注入和保留磁盘重启不能证明 100 PB、六个九耐久性或四个九可用性。详见 [设计文档](docs/design.md)。

## 第25章工程复核（2026-10-26）

新增 `GET /v1/buckets` 返回本人桶；`GET /v1/buckets/{bucket}/objects?delimiter=/`
列出同层对象和 `common_prefixes`，默认空delimiter仍递归列举。对象与前缀共同计入页大小，
续页须保持owner/bucket/prefix/versions/limit/delimiter一致，签名token有效5分钟；
后续写入不改变已有快照。旧snapshots表自动迁移，兼容旧递归token。
每用户最多1000桶，列举在prefix过滤前最多聚合1万原始行；活动快照全局128/每用户32，
每快照8 MiB，单页不落无用快照。预算不足明确失败，不返回部分结果。

拆分catalog/listing/记录契约/chunk I/O/runtime/路由/传输。单协调器默认8个在途操作、
12个共享复制线程，HTTP最多32请求；普通读取/发布可重叠，GC、删桶及列表快照独占。
multipart使用64个固定上传锁条带，持久completing意图、条件状态检查、幂等版本及复制门槛保留。
数据节点支持共享读、写优先；追加/索引/CURRENT切换不确定时置 `needs_recovery`，验证恢复后再服务。

RPC超时不能证明远端停止。因此发布元数据前先持久化catalog intent，未知结果保留pending，
维护须先幂等重放全部pending再扫描引用；超过128个pending、损坏或恢复失败都停止回收。
只有校验元数据回执后才标完成。旧版没有intent历史，升级前须排空旧请求，无法追溯旧未知操作。
维护在扫描引用前读取各节点 `gc_generation`；每次接受PUT（含重复块）、恢复、压缩、回收
都会更换generation。内部HTTP `/collect` 必须带 `{live,grace,generation}`，获得写锁后比对，
过期请求在删除前返回409。修复PUT也可能使本轮回收失效，`collection_deferred` 表示下轮
重新收集完整引用再扫，不静默刷新代际接受旧快照。节点重启使用新随机代际，无需跨机对时。
本地direct DataNode.collect保留无代际的可信兼容调用，外部须使用协调器受栅栏保护的协议。

direct HTTP不走代理/重定向，严格framing/JSON/校验和、共享期限与取消。
普通请求总预算15秒、维护60秒，每段RPC默认最多5秒；退出排空自有请求和线程池。
不能强杀任意SQL/CPU/DNS或自定义阻塞RPC。

最终 **137项通过，40.784秒**，无跳过，启用 `-W error`；原/新增六进程演示通过。
4客户端40对象逐字节校验，PUT 38.23次/秒、GET 76.98次/秒，
PUT p50/p95=99.655/124.262ms，GET=48.046/69.440ms。
单次本机小样本不是受控提速或生产SLA证明。详见[章节记录](docs/chapter25-review.md)
和[实测报告](benchmarks/results.md)。

放置心跳/共识、独立AZ、目录/元数据HA、多聚合文件滚动/每核追加、GB流式、纠删码、
自动跨机修复、在线迁移、IAM/TLS/加密、生命周期/备份及100PB耐久性验收仍未实现。
未使用真实AWS服务，未提交或推送。

删桶同时检查未完成发布意图，存在则返回409，防止恢复时向已删除桶发布无法访问的孤儿版本。
