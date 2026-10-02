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
