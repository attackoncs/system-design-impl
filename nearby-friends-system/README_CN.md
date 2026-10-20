# 附近好友（第18章）

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

9项核心测试不需要Redis服务；3项网络测试缺少真实Redis时会明确跳过。
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
数据库留在忽略目录 `.runtime/lab-<uuid>/`。不会改动已有Redis服务。

持久运行、凭据创建和完整协议见[英文说明](README.md)。两个WS节点共享同机SQLite
元数据文件和Redis地址；数据库不是跨主机HA。服务默认监听localhost，外部部署需要TLS。

## 语义

- 默认关闭位置共享，双方开启共享且确认好友后才可见。位置上报建议每30秒一次，
  600秒不活跃即隐藏；断开连接保留位置到TTL过期。距离为球面直线距离。
- 上报包含latitude/longitude/Unix timestamp和可选device；按用户而非设备抑制
  重复／乱序。时间不能早于TTL窗口，也不能超前服务器30秒；TTL使用服务器接收时间。
- 连接时发送带Bearer的升级头，立即返回附近好友快照。默认每页20条，snapshot操作
  可调整radius_m/offset/limit。分页是实时偏移视图，移动会改变页面，客户端应按ID去重。
- friend.request/accept/remove管理双向关系；sharing设置共享开关。delta包含
  enter/update/leave，其中leave只有ID，没有旧坐标。每次输出重新检查权限。
- Pub/Sub只传用户ID提示，不传坐标，可能丢失。默认每秒核对当前快照，恢复漏消息、
  过期和权限变化。已在途的消息无法撤回；重连恢复当前快照，不回放丢失频道消息。
- history.consent默认关闭；开启后保留未来上报，默认24小时；关闭删除历史。
  history只能读取自己的记录。当前SQL位置在TTL后不可查询，活跃服务最多30秒后清理；
  Redis旧副本到TTL失效。停机期间物理清理由下次启动恢复。
- 管理员ring操作使用expected_epoch原子更新broker ID/URL配置；一致性哈希重路由，
  会重建订阅并读快照。该共享配置不是etcd/ZooKeeper共识服务。

## 实测与限制

12项测试（含3项真实网络测试）通过。2个WS进程、2个Redis进程、21个连接，
20个观察者接收10次更新，200/200次投递成功；p95 140.323ms。
详细证据见[报告](benchmarks/results.md)。这是本机小规模实验，不能证明书中千万
连接或每秒33.4万次上报。SQLite写入串行化、每连接订阅和周期快照仍有规模限制；
跨地域、分布式SQL、TLS、生产身份服务、备份与持续压测尚未落地。
