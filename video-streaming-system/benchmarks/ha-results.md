# 数据库高可用与跨主机存储验收

日期：2026-10-03。客户端为 Windows 11 / Python 3.13.9；数据库与对象服务运行在
Ubuntu 26.04 WSL。最终完整测试：**40 passed in 16.87s**，没有跳过 PostgreSQL
测试。独立进程使用各自工作目录；数据库和 S3 服务经 TCP/HTTP 访问。

## 数据库切换

实际启动 PostgreSQL 18.6 / Patroni 4.1.5 三节点和 etcd 3.5.16 三成员，启用
严格同步复制。上传三秒视频并持久化任务后，终止本次实验的主节点 Patroni 和
PostgreSQL 进程；保留 API 进程与连接，等待自动提升。

完整原始结果见 [failover.json](failover.json)，包含提升和 API 恢复耗时以及
实际 503 次数。最终代码验收：提升 **16.362 秒**，原 API 恢复 **16.582 秒**，
期间观察到 5 次 HTTP 503。验收确认：凭据、上传记录和任务保留；旧租约完成被拒绝；接管后
检查任务第二次尝试成功，完成事件仅处理一次；两档加密 HLS 均由真实 FFmpeg
经 HTTP 解密播放。报告不含密码、访问令牌或播放授权。

另行测试终止一个测试连接、连接存活但丢弃请求、远端对象写入失败及丢失提交确认。
丢弃请求时客户端在五秒响应期限内报错，之后可重连；存储失败回滚分片元数据；
提交确认丢失只报告结果不确定，不会自动重复 INSERT/COMMIT。

## 真实对象存储

使用官方 SeaweedFS 4.48 发布的 Linux 二进制，下载 SHA-256 校验值：
`4a7d108384d044d95212d1342cdda9533fa55842c1c9b41f606ca3c8a9561124`。
启用 S3 身份验证，测试通过 Boto3 访问真实私有桶。

API 上传后，两个独立 CLI 工作进程通过 PostgreSQL 抢占任务，从 S3 下载原视频，
执行 FFmpeg 并回传输出。第三节点有独立缓存，通过 HTTP 获取清单、密钥和分片，
真实解密播放 144p/240p。没有共享应用磁盘目录。

验证了校验和不符的远端对象被拒绝、上传失败不会确认成功、本地缓存不能掩盖远端
对象丢失、回收只删除本部署前缀下的无引用对象。离线 SQLite→PostgreSQL/S3
迁移保留凭据、签名授权、任务并恢复发布；拒绝非空目标。

## 复现与边界

在 Ubuntu 下先运行 `tools/prepare_pg_lab.py` 和 `tools/prepare_patroni_lab.py`，
再运行 `tools/launch_patroni_lab.py`；另一个终端运行 `tools/s3_lab.py`。
准备工具仅下载/解压到忽略目录或新建临时目录，不安装系统服务。
集群就绪后，按 README 的测试环境变量连接并运行 pytest；运行
`tools/verify_failover.py --output benchmarks/failover.json` 注入一次主节点故障。
实验控制文件位于 `.runtime`，凭据仅用于临时环境，不要提交这些文件。

停止实验：向 `.runtime/ha-control.json` 写入 `{"action":"stop"}`，创建
`.runtime/s3-stop`；工具停止其拥有的进程并删除发布的凭据配置。

这是单物理机器上的跨 Windows/WSL 网络与多进程验证，不是不同物理主机、跨机房
或真实网络分区演练。Compose 已解析核对拓扑，当前环境无 Docker，未运行该
容器编排。S3 服务为单节点；存储高可用需使用复制/托管对象存储。
数据库仍用全局事务锁串行化写状态机，没有宣称大规模吞吐。
严格同步模式在缺副本时牺牲写可用性；备份恢复、监控告警、TLS、故障域和节点
隔离属于部署责任，不能由此次主节点终止实验替代。

官方来源：[SeaweedFS 4.48](https://github.com/seaweedfs/seaweedfs/releases/tag/4.48)、
[Patroni 同步复制](https://patroni.readthedocs.io/en/latest/replication_modes.html)。
