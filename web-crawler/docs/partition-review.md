# 第 10 章：按主机分配抓取工作

`web_crawler.partition.ConsistentHashPartition` 提供进程内的一致性哈希路由。
工作节点 ID 和虚拟节点数量相同时，各进程得到相同分配，不受 Python 随机哈希种子或节点输入顺序影响。
SHA-256 计算虚拟节点及主机位置；哈希位置相同的虚拟节点仍分别保留。

```python
from web_crawler.partition import ConsistentHashPartition

partition = ConsistentHashPartition(
    ["worker-a", "worker-b", "worker-c"],
    local_node_id="worker-a",
    virtual_nodes=128,
)
owner = partition.owner_for_url("https://example.com/news")
should_fetch_locally = partition.owns("https://example.com/news")
```

同一个规范化 hostname 的所有路径、HTTP/HTTPS 协议和端口分配给同一个节点。
主机名采用小写 IDNA 表示，IPv6 地址采用压缩表示，忽略 DNS 根域末尾的点。
这种分组有助于维持主机抓取节奏；同一服务器的不同域名仍分别分配。
`owners_for_url(url, count)` 返回顺时针的不同物理节点，供上层选择备用节点；该方法本身不复制任务。
空集群无法分配，抛出 `LookupError`；不合法的 URL 或参数抛出 `ValueError`，
将字符串直接当作节点列表时抛出 `TypeError`。

`add_node`、`remove_node` 在锁内发布新的不可变环。`snapshot()` 捕获一个拓扑，
批量路由过程中可以一直使用它；并发成员变化不会改变已有快照。
正常哈希情况下，增加节点只把部分主机转给新节点，删除节点只重分配该节点拥有的主机。
该模块是有虚拟节点的等权路由，不提供容量权重或集群成员发现。

## 分布式边界与安全移交

应用层调度器需要将种子、解析发现的 URL 和重试任务投递到当前 owner 的持久队列。
仅在本地丢弃非 owner URL 会丢失抓取工作，不能作为跨进程分片实现。
节点间传输、投递确认、失败重试、租约和网络分区处理需要独立的调度或消息系统。
配置变更也需要外部控制面统一分发；不同成员视图会产生重复抓取或遗漏。

成员变化应采用带拓扑版本的移交流程：冻结旧 owner 的相关主机调度，等待或撤销在途任务，
转移待抓取任务、重爬时间、robots 状态及下次允许抓取时间，并确认新 owner 的持久接收。
完成后再激活新 owner；请求中的旧拓扑版本应转发或拒绝，由调度器重新路由。
直接调用 `remove_node` 不会执行这些步骤，也不保证跨进程礼貌间隔。
多个进程同时持有相同 worker ID 时仍需要租约或隔离机制。

## 验证范围

专用测试覆盖同主机归一化、稳定路由、节点增删的映射边界、哈希碰撞、
不同物理备用节点、空集群、无效参数，以及并发更新期间快照的一致性。
这些测试验证本地路由，不代表跨机器消息交付或拓扑安全移交已经实现。
