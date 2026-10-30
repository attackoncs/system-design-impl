# 分布式键值存储 — 第7章

<!-- LEARNING_GUIDE_START -->
## 从这里开始读代码

[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。
<!-- LEARNING_GUIDE_END -->

实现真实异步gRPC复制、可调quorum、向量时钟及siblings/墓碑、gossip、
持久hinted handoff、Merkle修复和WAL/MemTable/SSTable。
[English](README.md) · [章节复审](docs/chapter07-review.md) ·
[设计](docs/design_cn.md) · [实测](benchmarks/REPORT.md)。

本轮**417项测试通过**，覆盖实际RPC、故障与重启。负载驱动自动供给节点、
完整跨机房运行保障等仍未完成，不能标记为“原书所有部署要求均已实现”。

## 安装与运行

Python3.10+，先显式安装本地兄弟项目，再安装本项目：

```sh
pip install -e ../consistent-hashing
pip install -e ".[dev]"
python -m pytest tests -q
python examples/demo.py
python examples/repair_demo.py
python tools/benchmark.py --output benchmarks/local.json
```

实测Python3.13.9、grpcio/grpcio-tools1.84.0、protobuf7.36.2。
绑定要求grpcio>=1.84.0、protobuf>=7.35.1,<8，用
`python tools/generate_proto.py`再生成，不手工改写生成文件业务逻辑。
Windows原组件演示含Unicode输出，需PYTHONIOENCODING=utf-8；
该演示属于组件说明，repair_demo使用真实gRPC。

复审禁用无关pytest插件自动加载，显式加载pytest_asyncio.plugin；既有默认
缓存/临时目录不可访问，因此禁用cacheprovider并使用工作区新--basetemp。
以-Werror执行，两个asyncio默认loop_scope均设为function；最终没有跳过
任何项目测试，也没有过滤警告。

## 单节点API

每节点必须分别设置data_dir、wal_dir、sstable_dir，修改data_dir不会自动
派生另外两个目录。单节点需要N=W=R=1，默认3/2/2并不适用单节点。

```python
import asyncio
from pathlib import Path
from kv_store import KVNode, NodeConfig
from kv_store.config import StorageConfig, NetworkConfig, ReplicationConfig

async def main():
    data = Path("./data/single")
    node = KVNode(NodeConfig(
        node_id="single",
        storage=StorageConfig(str(data), str(data / "wal"), str(data / "sstables")),
        network=NetworkConfig("127.0.0.1", 0),
        replication=ReplicationConfig(1, 1, 1),
    ))
    await node.start()
    try:
        written = await node.put("hello", b"world")
        read = await node.get("hello")
        assert read.found and read.value == b"world"
        await node.delete("hello", read.vector_clock)
    finally:
        await node.stop()

asyncio.run(main())
```

值为最多10KiB的bytes，空bytes不等于删除；键为非空UTF-8字符串，最多256字节。
并发因果版本返回siblings，可能包含墓碑；客户端合并全部冲突时钟再写以解决
冲突。quorum失败、超时或取消仍可能留下效果，没有统一回滚或业务幂等键。

## 多节点与并发

使用唯一节点ID、独立目录及可达的host:port，通过ClusterConfig.seed_nodes
加入真实种子地址。默认N=3/W=2/R=2，需要足够不同物理副本；依赖稳定放置前
等待成员收敛。KVClient(ClientConfig(seed_nodes=[...]))是异步上下文管理器，
支持put/get/delete；节点API使用ConsistencyLevel，客户端用"all"等字符串。
[实验辅助器](tools/lab.py)统一负责本地集群建立与关闭。

node_virtual_nodes表示容量权重，node_data_centers优先放置不同故障域；
所有节点须使用相同映射，未知节点归为同一个未知域。这只是放置算法，
不是已验证的跨机房容灾运行保障。

W+R>N仅表示稳定副本集合上的交集，不等同线性一致或共识；因果冲突、
sloppy quorum和成员变化均影响结果。复制并发发出，达到quorum后其余RPC
继续并被跟踪；协调器在第一次await前预留容量，包含等待同键锁的请求，
准入预算为256个副本任务单位。

## 本轮修复

- Merkle根保持旧哈希格式，只更新O(log B)条内部路径；桶摘要仍随桶内键数增长。
- MerkleInspect每次最多比较64个树节点；差异桶按排序游标分页，每页最多
  128条、最多2MiB且受通道大小约束。单条过大明确失败，不静默丢记录。
- 远端root固定视图，变化返回ABORTED，下轮可安全重试；全部拉取完再推送，
  本地副本放置使用快照。这是最终一致修复，不是全局快照或拓扑共识。
- 取消请求前先完成已开始的WAL/内存/Merkle本地提交，重复取消也等待磁盘线程。
  SSTable先写临时文件并fsync，再原子替换发布，之后截WAL、淘汰压缩输入。
  支持的平台同步目录；Python没有可移植Windows目录fsync，未做真实断电实验。
- 内部存储时间按键单调递增，墙上时钟回退不再使LSM丢弃新因果版本；
  读取/恢复比较各候选层每键时间，不依赖文件创建时间，但读开销增大。
- hint输入在SQL锁前冻结，ACK必须匹配捕获的行ID/目标/键/原记录，避免
  SQLite复用ID时迟到ACK误删新hint。

真实双节点实验在64KiB消息上限下用5页同步196608字节应用数据，墓碑保留、
root收敛；第二轮仅比较一个根且不传数据。[原始输出](benchmarks/repair-2026-10-27.json)。

## 兼容与范围

新修复链路要求节点同步升级新增的两个RPC。旧MerkleTreeSync保留兼容，
仍受全量响应上限限制，运行时不再使用。磁盘envelope格式1未变，但校验
更严格：精确字段、拒绝重复JSON、有限0..1e12时间戳、可用protobuf有符号
64位表示的计数。记录上限1MiB/256个siblings，超限应由客户端解决冲突。
旧裸值目录必须显式导出导入，无法恢复旧版未存储的因果元数据。
损坏的最终SSTable改为拒绝启动，不能静默忽略；保留损坏文件用于恢复。
墓碑没有自动安全GC，WAL失败关闭后数据仍可能在恢复时生效。

当前SSTable仍缓存整个文件，启动/重建扫描数据，压缩未做流式处理；
树索引全部本地记录，副本职责不完全相同时可能重复比较非共有桶。
没有负载驱动自动供给/安全缩容屏障、拓扑epoch协议、认证/TLS或真实跨地域
故障验证，也不宣称海量数据容量。详情见[章节复审](docs/chapter07-review.md)。
