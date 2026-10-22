# 第 26 章：实时游戏排行榜

真实 Redis 哈希分片 sorted-set 实现月赛积分、同分竞赛名次、固定 Top 10 和前后四个显示位置。SQLite 保存权威比赛日志，Redis 丢失后可重放。实验共 7 个进程：权威服务、两个 HTTP 投影节点、两组 Redis 主备。

要求 Python 3.10+ 和真实 Redis 程序，已验证 Redis 8.0.5。客户端为标准库实现，无 Redis 模拟或前章源码依赖。

## 运行

在本目录指定已安装的 Redis：

```powershell
$env:LB_REDIS_BINARY = 'C:/path/to/redis-server.exe'
python examples/network_demo.py
python -m unittest discover -s tests -v
python tools/benchmark.py --output benchmarks/results.json
```

Linux 可使用 PATH 上的 Redis 或设置同名环境变量。Windows 使用 WSL 时，改用以下配置，不同时设置 LB_REDIS_BINARY：

```powershell
$env:LB_WSL_DISTRO = 'Ubuntu-26.04'
$env:LB_WSL_REDIS_BINARY = '/path/to/redis-server'
# 解包程序有独立动态库时才需要：
$env:LB_WSL_REDIS_LIB = '/path/to/libraries'
python examples/network_demo.py
```

替换为真实路径。实验生成私有密码/命名空间、四份独立 Redis AOF 数据和三项 HTTP 服务，只清理自有进程，停止 Redis 前核对 run ID。数据/日志保留在忽略提交的 `.runtime/lab-*`。系统临时目录受限时创建 `.runtime/tmp` 并设置绝对 TEMP/TMP 路径。不自动安装程序、不修改已有服务。见 [验证报告](benchmarks/results.md)。

## 配置及接口

```powershell
$env:PYTHONPATH = 'src'
python tools/init_config.py
# 先按私有配置准备两组真实 Redis 主备，再分别在独立终端启动：
python -m gaming_leaderboard.cli authority --config .runtime/secrets.json --port 9860
python -m gaming_leaderboard.cli projection --config .runtime/secrets.json --shard 0 --port 9861
python -m gaming_leaderboard.cli projection --config .runtime/secrets.json --shard 1 --port 9862
```

配置生成器拒绝覆盖，提供独立管理/读取/比赛/内部令牌、Redis 密码、命名空间、服务 URL、两组 Redis 端点及 48 小时迟到容忍。Redis 默认端口为 9863–9866；配置生成不等于启动 Redis，自动实验会自行准备实例。服务与 Redis 端点仅使用回环地址。

| 接口 | 权限与作用 |
| --- | --- |
| `PUT /admin/players/{user}` | 管理令牌，`{name}`，注册/更新显示名及当月成员 |
| `POST /v1/scores` | 比赛服务令牌，`{match_id,winner,players,ended_at}`，唯一胜局 +1 |
| `GET /v1/scores?month=YYYY-MM` | 读取令牌，Top 10，默认当前 UTC 月 |
| `GET /v1/scores/{user}?month=YYYY-MM&neighbors=4` | 个人及前后最多四个显示位置 |
| `GET /v1/seasons/{month}` | 赛季状态、截止时间与结算 |
| `POST /admin/close` | `{month}`，迟到期限后保存不可变结算 |
| `POST /admin/reconcile` | `{month}`，独立对账 SQL 与 Redis |
| `POST /admin/rebuild` | `{}`，只清理本应用投影键并重放 |

可运行的 Python 注册/加分示例见 [英文说明](README.md)。ID 为 1–64 个 ASCII 字母、数字、下划线或连字符；每局有 2–8 个不同已注册参与者，包含获胜者。不接受 points 自报加分。重复 ID 同内容返回原凭据、不同内容返回 409，重试需保留原结束时间。可信令牌不是比赛反作弊实现。

## 排名与月赛语义

- 所有注册玩家参加当月，包括零分。竞赛名次为 1+严格高分人数，如 `1、2、2、4`。同分按用户 ID 升序显示，Top 10 最多十人、不展开并列组。
- position 是唯一显示位置；前后四名扩展按显示行取邻居，不按四个不同分数组，跨分片精确求和与归并。
- 月份按 UTC，结束时间决定所属月，新月份从零开始而旧月份保留。不得早于注册、超过当前时间 30 秒或属于未来月份。
- 月末后 48 小时之前可接收迟到事件；截止/结算后拒绝新事件，但旧凭据仍可幂等重放。结算保存全榜摘要和 Top 10。
- 权威 SQL 同事务保存积分、事件和凭据；Redis 重放绝对积分及原子检查点，不重复增量。变更身份/偏移/校验错误会停止投影。
- 正常请求投影完成返回 200/projected=true；Redis 故障时仍可持久接收并返回 **202、durable=true、projected=false**。排名必须达到所有分片屏障，否则 503，不返回部分或未经标注的旧榜。
- 权威服务串行化公开写入与多阶段查询，内部事件读取不会被屏障阻塞；此设计需要单活并限制吞吐。
- 主备使用真实 AOF/异步复制。故障提升是人工且需先隔离旧主，返回旧主须重新设为副本；同时存在两个可达主节点时失败关闭。无 Sentinel/共识选主。

内部 `/internal/status` 提供头位置、投影身份/位置和轮询错误，需内部令牌。Redis 丢失可从权威日志重建，源身份改变需显式重建。实验只针对完全自有实例注入 FLUSHALL，不对已有 Redis 操作。

参考上限 10000 注册玩家、单人 10 亿积分。无权威 SQL 复制、自动切主、完整账号/TLS、反作弊、在线扩分片或历史自动清理；本机结果不代表 2500 万月活或 2500 峰值写 QPS。更多细节见 [设计文档](docs/design.md)。
