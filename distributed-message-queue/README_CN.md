# 分布式消息队列（第20章）

参考实现包含：主题／分区、CRC及哈希链分段日志、独立HTTP broker复制、生产者序列
与epoch隔离、消费者组／心跳／重平衡、持久消费位置、队列内原子消费输出，以及
重试／死信、延迟消息和本地归档回放。

协调元数据采用**单个持久化SQLite协调器**，没有伪装成Raft／etcd高可用集群。

## 运行

Python 3.10+，标准库，无第三方运行依赖。在本目录执行：

```powershell
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --output .runtime/benchmark.json
```

网络测试实际启动3个独立磁盘broker、1个协调器和3个透明TCP故障代理；代理只控制
连通性，不实现队列行为。退出停止自有进程，私有配置、日志和数据保留在忽略目录
`.runtime/lab-<uuid>/`。无需Kafka、Redis或云账户。

持久运行先生成私有凭据，然后在4个终端分别设置 `$env:PYTHONPATH = 'src'`：

```powershell
python tools/init_config.py
python -m message_queue.cli broker --root .runtime/b0 --config .runtime/secrets.json --port 9711
python -m message_queue.cli broker --root .runtime/b1 --config .runtime/secrets.json --port 9712
python -m message_queue.cli broker --root .runtime/b2 --config .runtime/secrets.json --port 9713
python -m message_queue.cli coordinator --root .runtime/coordinator --config .runtime/secrets.json --port 9710
```

每个进程独占数据目录；不能共用broker磁盘，也不能用同一元数据启动两个协调器。
Windows应保持根路径／主题名较短。默认localhost；外部部署需TLS、ACL、身份认证。
本实验共享client_token授权所有协调器操作，cluster_token只用于内部broker协议。

## 核心语义

- 每条文本最多16KiB，一批1—100条；CRC32消息键路由到固定分区。只保证分区内顺序。
- 生产者ID有epoch隔离，每个epoch／主题／分区使用连续批次sequence。完全相同请求
  重试复用offset，改变内容返回409；重新打开同一生产者会增加epoch并隔离旧实例。
  新epoch之间不防重复；跨客户端重启若需原身份恢复，应保存epoch和sequence并用底层API。
- `acks=all`默认要求多数副本持久化，返回实际确认副本列表。少数可用副本返回503，
  结果可能已暂存，必须用相同序列／内容重试，不能改新序号“绕过”。
- `acks=leader`返回committed=false，只表示leader和单协调器暂存，不表示已多数确认
  或可消费；后台恢复后才能提交。fire_and_forget只发一次、不重试，不保证完成。
- 消费者组默认10秒租约，需心跳。成员加入／替换／过期改变generation；旧session、
  旧代次、旧分区所有者无法提交。提交不能跳过未投递记录。offset表示下一条位置。
- 至少一次：拉取后显式提交；至多一次：响应前推进游标，允许丢失。
- `/process`在输出复制完成后，用同一元数据事务提交输出可见位置和输入游标，txid
  重试不重复。这个“恰好一次”只限队列边界，不包括邮件、外部数据库或任意HTTP副作用。
- delay_seconds的延迟记录阻塞本分区后续记录；retry辅助方法原子转入延迟重试／死信
  主题；archive/replay写本地JSON并产生新offset，不是云端归档或恰好一次恢复。

默认保留14天或每分区8MiB已提交信封数据，先达到者生效；这是本地参考容量限制。
物理删除完整旧分段，活动分段可能多留一段。过期offset返回410，需显式reset。
旧生产者回执过期后仍不能复用旧sequence生成新消息。事务回执和epoch水位暂未清理。

## 接口与验证

所有接口是POST JSON + Bearer。`/topic`、`/producer`、`/produce`、`/fetch`、
`/join`、`/heartbeat`、`/group-fetch`、`/commit`、`/reset`、`/process`、
`/retain`、`/metadata`。字段及Python示例见[英文README](README.md)。

15项测试通过，其中5项使用独立进程真实网络，验证leader崩溃、旧消费者隔离、
磁盘损坏、broker仍存活时的TCP分区、恢复重试、事务输出、重试死信与回放。
120条消息全部提交并读回，小规模实测34.21条／秒。逐条fsync、全量完整性扫描和
单协调器串行化成本较高，不能据此宣称生产吞吐达标。

详见[验证报告](benchmarks/results.md)及[设计](docs/design.md)。协调器是单点；
共识元数据、协调器HA、增量复制、TLS、跨地域、持续压测和灾难恢复仍待扩展。
