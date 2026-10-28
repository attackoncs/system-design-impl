# 视频流系统

第 15 章参考实现：签名分片上传、断点续传、持久化转码 DAG、独立工作进程、
真实 FFmpeg 转码、多清晰度加密 HLS、授权 HTTP 播放和故障恢复。

在本目录的 PowerShell 中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev,media]'
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python examples\media_demo.py
```

示例生成三秒视频，分片上传并完成转码，通过真实 HTTP 解密播放两档清晰度，
退出时关闭服务并清理自己的临时目录。可选媒体依赖在 PATH 没有 FFmpeg 时提供它。

持久运行时，在不同终端分别执行：

```powershell
.\.venv\Scripts\python -m video_streaming.cli --root .runtime/video token alice
.\.venv\Scripts\python -m video_streaming.cli --root .runtime/video api --port 8080
.\.venv\Scripts\python -m video_streaming.cli --root .runtime/video worker --name worker-a
.\.venv\Scripts\python -m video_streaming.cli --root .runtime/video worker --name worker-b
```

保存并保护生成的所有者令牌。上传、续期、完成、状态、播放授权、下架接口及参数见
[英文说明](README.md)。上传默认 4 MiB 分片，最大视频 1 GB。
任务租约过期后可由其他进程接管，过期工作进程不能覆盖新结果；完成事件持久化，
所有对象齐备后才发布视频。`cleanup` 命令保守回收过期上传和孤立文件。

测试覆盖幂等、授权、重试、进程退出接管、元数据事务回滚、对象缺失、
下架和真实 FFmpeg 加密播放。[实测报告](benchmarks/results.md)记录本机结果。

本地模式保留 SQLite；分布式模式已接入 PostgreSQL 主节点发现与重连、私有 S3
对象存储。真实 PostgreSQL/Patroni/etcd 主备切换，以及 Windows 节点访问 Linux
SeaweedFS 的上传、独立进程转码、加密播放均已验证，见[高可用报告](benchmarks/ha-results.md)。
真实 CDN、GPU 调度、KMS 和商业 DRM 尚未接入；本机实验不证明书中生产规模。

## 跨主机部署与迁移

安装 `.[distributed,media]`，配置 `VIDEO_DATABASE_DSN` 为多主机 libpq 连接串；
驱动强制 `target_session_attrs=read-write`。配置 `VIDEO_S3_BUCKET`、可选
`VIDEO_S3_ENDPOINT`、`VIDEO_S3_PREFIX`，通过 AWS SDK 环境变量/身份提供凭据。
桶必须预先创建且保持私有。每台机器使用自己的 `--root` 运行 API 或 worker。
远程部署启用数据库证书验证和 HTTPS。

[英文说明](README.md)提供完整 Compose 命令和环境变量。`compose.ha.yaml` 包含
三成员 etcd、三节点 PostgreSQL/Patroni 严格同步复制、私有 SeaweedFS、API 和
两个独立工作目录的 worker。当前环境没有 Docker，因此仅验证 Compose 结构；
实际故障实验使用 WSL 中相同类型的真实服务。S3 实验服务为单节点，存储高可用
需要替换为托管或复制的 S3 服务。真实跨物理主机网络分区尚未演练。

元数据写操作使用全局事务锁保证状态机正确性，吞吐扩展仍需优化。数据库故障返回
503，后续请求重新发现主节点；提交结果不确定时不会盲目重放。客户端先查询状态，
再重试幂等的分片/完成接口；创建视频、颁发令牌目前没有客户端幂等键。
严格同步模式在缺少同步副本时会暂停写入。生产部署需配置故障域与节点隔离。

迁移时先备份并停止源/目标的所有 API、worker，配置目标 DSN 和 S3 后运行：

```powershell
python tools/migrate_metadata.py --source .runtime/video/metadata.sqlite3 --source-objects .runtime/video/objects
```

目标元数据必须为空；对象先传输，元数据事务整体导入，保留源文件、凭据、签名密钥
和任务。完成后重新启动目标进程。集成测试环境变量及复现方式见英文说明和报告。

## 本轮章节核对

补齐配置式流水线、持久 GOP 预处理、独立元数据更新、任务续租和有限并行 Worker，
详见[章节覆盖与证据](docs/chapter15-review.md)。新增模块分离计划、队列、SQL 和
生命周期职责；保留原有 VideoService 接口及默认五任务计划。

```powershell
.\.venv\Scripts\python -m video_streaming.cli --root .runtime/video --pipeline examples/pipeline.json api --port 8080
.\.venv\Scripts\python -m video_streaming.cli --root .runtime/video worker --name worker-a --concurrency 2
```

[流水线示例](examples/pipeline.json)配置 144p/240p、GOP 预处理和缩略图。库接口也
接受 pipeline=Pipeline(...)，完成上传时持久计划，重启或显式重试不能替换它。
预处理按已有关键帧切块，两秒只是目标；编码并行粒度为清晰度，不是逐 GOP 分布式
编码/合并。Worker --kinds 可限制 inspect/preprocess/thumbnail/encode/publish，
需确保全部所需类型有 Worker；本地并发 1..32。--once 只清空当前就绪工作，不等待
延迟重试。API 的 --concurrency、--request-timeout 设置处理槽位和套接字期限。

POST /metadata?video=... 使用所有者 Bearer 令牌及 JSON {"title":"新标题"}，
上传、处理和 READY 状态均可调用。HEAD /media 返回媒体/范围响应头；finish、renew、
playback、remove 请求不带正文，上传创建与元数据更新使用 JSON。FAILED 状态增加
有限任务错误详情，原始 HLS 清单上限 1MiB。

心跳覆盖下载/转码/上传，首次领取持久确定尝试上限，其他 Worker 不会提前耗尽它。
冷对象加载和完成检查移出全局锁，资源映射使用有限元数据缓存；缓存不能绕过当前
状态、授权或远端存在性检查。停服等待活动处理器和操作。数据库/对象调用可能超过
套接字期限，断连后已接受写入仍可能提交；部分最终检查和 GC 仍持有写事务，慢存储
可能延迟写入和续租。

清理先提交过期状态，再删对象；失败不会恢复引用丢失对象的上传。READY/FAILED
的 GOP 中间块经过宽限后可回收，源与播放资源继续保留。宽限不是持久 pin，暂停的
未提交工作可能需要重试。离线迁移保留任务尝试策略，兼容没有新表的旧源数据库。

本轮最终 **160 项测试通过，无跳过**：全新独占 PostgreSQL 18 UTF8、Moto S3、
真实 FFmpeg 及独立 Worker 均实际执行，结束后清理自有服务。未配置 PostgreSQL
时正常跳过六项集成测试。[新实测](benchmarks/review-2026-10-24.json)仅为三秒素材、
一个 Worker 和十次顺序回环读取，不证明并发容量。本轮未重跑 Patroni 主库提升、
真实 S3 高可用和跨主机网络分区实验。
