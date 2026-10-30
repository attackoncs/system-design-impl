# 附近好友（第18章）

<!-- LEARNING_GUIDE_START -->
## 从这里开始读代码

[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。
<!-- LEARNING_GUIDE_END -->

实现真实 WebSocket 位置上报与通知、Redis TTL缓存和按用户的Pub/Sub频道、
双向好友确认、共享开关、默认5英里半径、10分钟过期、多设备乱序抑制、
历史记录授权与保留期限、一致性哈希和节点变更重订阅。

## 运行

需要 Python 3.10+、websockets 14—16、redis客户端5—6。安装与单元测试：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e .
.venv/Scripts/python -m unittest discover -s tests -v
```

核心与可靠性单元测试不需要Redis服务；真实网络测试缺少Redis程序时会明确跳过。
本轮实际执行数量、是否跳过和结果集中记录在[验证报告](benchmarks/results.md)。
Linux设置 `NEARBY_REDIS_BINARY=/usr/bin/redis-server`；Windows使用WSL时：

```powershell
$env:NEARBY_WSL_DISTRO = 'Ubuntu-26.04'
$env:NEARBY_WSL_REDIS_BINARY = '/usr/bin/redis-server'
.venv/Scripts/python -m unittest discover -s tests -v
.venv/Scripts/python examples/network_demo.py
.venv/Scripts/python tools/benchmark.py --output .runtime/fanout.json
```

路径须指向实际安装的Redis。若用解包程序，可通过NEARBY_WSL_REDIS_LIB指定动态库
目录。实验启动两个隔离Redis和两个独立WebSocket进程，退出停止自有进程，日志与
数据库留在忽略目录 `.runtime/lab-<uuid>/`。每个Redis使用独立随机密码，清理前核对
其run ID，避免自由端口竞争时误操作已有服务；只停止本轮自有进程。

持久运行、凭据创建和完整协议见[英文说明](README.md)。两个WS节点共享同机SQLite
元数据文件和Redis地址；数据库不是跨主机HA。服务默认监听localhost，外部部署需要TLS。
凭据provision先独占创建token文件、写入并flush/fsync，再创建SQL用户；失败时仅清理
身份仍匹配的自有新文件，不覆盖已有凭据。

新增可配置容量（下面命令从项目目录、设置PYTHONPATH后运行）：

```powershell
.venv/Scripts/python -m nearby_friends.cli serve --port 8090 --max-connections 256 --workers 16 --send-timeout 3
```

默认256个已升级应用会话、最多16个已提交同步任务，单个响应或整批增量共享3秒发送预算。
应用会话上限不等于
TCP连接总量：握手有5秒期限、backlog为128，部署仍需入口连接限制。关闭停止准入、
关闭会话/订阅，并等待已接受的SQL/Redis操作结束后关连接池；系统DNS及正在执行的
SQLite调用不能被取消强制打断，因此排空可能等待。

## 语义

- 默认关闭位置共享，双方开启共享且确认好友后才可见。位置上报建议每30秒一次，
  600秒不活跃即隐藏；断开连接保留位置到TTL过期。距离为球面直线距离。
- 上报包含latitude/longitude/Unix timestamp和可选device；按用户而非设备抑制
  重复／乱序。时间不能早于TTL窗口，也不能超前服务器30秒；TTL使用取得SQLite写锁后的服务器接收时间；排队期间已过期的位置不会重新获得存活期。
- 连接时发送带Bearer的升级头，立即返回附近好友快照。默认每页20条，snapshot操作
  可调整radius_m/offset/limit，分页不覆盖实时增量基线。半径变化仍发送leave/enter/update；
  页面与增量按好友ID归并。新用户连接后需发送首条location才可见；
  分页是实时偏移视图，移动会改变页面，客户端应按ID去重。
- friend.request/accept/remove管理双向关系；sharing设置共享开关。delta包含
  enter/update/leave，其中leave只有ID，没有旧坐标。可选request_id为1..64字符。仅接收16KiB以内UTF-8 JSON文本，拒绝重复字段、
  未知业务字段、二进制消息和非有限数字；输出帧不超过256KiB。
- Pub/Sub只传用户ID提示，不传坐标，可能丢失。默认每秒核对当前快照，恢复漏消息、
  过期和权限变化。已在途的消息无法撤回；重连恢复当前快照，不回放丢失频道消息。
- history.consent默认关闭；开启后保留未来上报，默认24小时；关闭删除历史。
  history只能读取自己的记录。当前SQL位置在TTL后不可查询，后台按min(30秒, TTL)尝试清理；
  Redis旧副本到TTL失效。停机期间物理清理由下次启动恢复。
- 管理员ring操作使用expected_epoch原子更新broker ID/URL配置；一致性哈希重路由，
  会重建订阅并读快照。该共享配置不是etcd/ZooKeeper共识服务。

## 工程、实测与限制

本轮[章节核对](docs/chapter18-review.md)按原书映射需求，并说明models/routing/schema/
protocol/work职责。SQL报告读取使用一致事务；可选Redis操作结束后再次读取SQL，复核
双方TTL、位置、好友关系和共享状态，作为本次响应的授权时点。Redis为镜像，命中也不会
避免两次SQL读取；此后撤权仍可能与在途帧竞争。

缓存缺失修复使用一次MGET和每批最多128条pipeline，默认1秒预算在批间检查；不把缓存
故障放大为每好友一次网络超时。已在执行的Redis调用可能晚于预算结束。同步Redis客户端
连接池上限16、连接/读写超时0.5秒、无自动重试；最多32个broker客户端的LRU保留使用租约，
不淘汰正在发布的客户端。每会话的异步订阅另外为每broker组保留一个连接。URL只允许db查询
参数，不能覆盖网络预算。缺失提示由周期对账修复，返回数据仍以最终SQL快照为准。

最新结果见[报告](benchmarks/results.md)和[本轮原始测量](benchmarks/review-2026-10-25.json)。
2026-10-20历史实验为2个WS/2个Redis进程、21个连接、200/200次投递，p95 140.323ms；
保留原日期，不能视为当前版本速度或生产容量证明。

位置更新不再逐次执行全局清理；后台清理失败会重试，停机或任务等待会让物理删除延后。
历史关闭会删除已保留记录，但生产备份/磁盘仍需保护。SQLite写入串行、每会话订阅全部好友
并周期扫描完整集合，仍有规模成本。尚未部署Redis主备、etcd、分布式SQL、跨地域、TLS、
生产身份服务或证明千万连接/每秒33.4万上报。Erlang为替代路线，附近陌生人为原书额外功能，
当前好友授权不会扩展给陌生人。详见[中文设计](docs/design_cn.md)。
