# 第 06 章一致性哈希：设计与原书核对

对照根目录《System Design Interview.md》第 06 章。原书列举的 Dynamo、Cassandra、Discord、Akamai、Maglev 是应用案例，不是要求在本章节重新实现这些系统。

## 章节覆盖

| 原书主题 | 对应实现/证据 |
| --- | --- |
| hash(key) % N 扩缩容导致大量重映射 | examples/chapter_comparison.py 对照新增节点前后映射 |
| SHA-1 哈希空间与环回 | hash_functions.py 的完整 160 位 SHA-1；二分查找后取索引环回 |
| 同一函数映射节点与键，顺时针查找 | ring.py，节点标识为 node#index；默认不对哈希值取服务器数量的模 |
| 加入/移除只改变局部归属 | add_node/remove_node；既有属性测试及新增区间逐位置校验 |
| 分区大小不均、虚拟节点改善分布 | 可配置全局/每节点虚拟节点数；统计平均数、标准差及变异系数 |
| 虚拟节点数量与内存取舍 | 对照脚本展示 1/100/200 个虚拟节点的条目数与分布 |
| Find affected keys：定位受影响区间 | migration.py 的 affected_ranges(before, after)，无需枚举全部键 |
| 水平扩容及热点讨论 | 只提供路由与迁移计划；单个热点键不会被拆开，不提供实际数据传输 |

## 模块与接口

- ring.py：不可变 _RingState、公开只读 RingSnapshot、可写 ConsistentHashRing。已有 get_node/get_nodes/add_node/remove_node 接口保持兼容。
- migration.py：不可变 AffectedRange 与纯拓扑比较；计算旧/新主属节点，不比较副本集合。
- stats.py：分布统计固定一个快照；既可传 live ring，也可传 RingSnapshot。compute_redistribution 仍负责比较已知键的映射。
- hash_functions.py：确定性字符串到整数函数。自定义函数必须确定、线程安全，且调用期间行为不变。

```python
from consistent_hashing import ConsistentHashRing, affected_ranges

ring = ConsistentHashRing(["a", "b"], num_virtual_nodes=150)
before = ring.snapshot()
ring.add_node("c")
after = ring.snapshot()
for interval in affected_ranges(before, after):
    print(interval.start, interval.end, interval.previous_owner, interval.new_owner)
# 对本地存储键的哈希值使用 interval.contains(position) 筛选迁移对象。
# 本库不会实际复制、删除数据或切换分布式客户端路由。
```

## 区间与碰撞语义

保留既有严格顺时针 bisect_right：键哈希等于虚拟节点位置时归属于下一个更大位置。因此受影响区间采用 **[start, end)**，起点包含、终点不含。start > end 表示跨越环尾，start == end 表示整个环；这与本库历史路由规则一致，原书未指定相等位置的端点细节。

规划器合并前后位置的边界，逐段比较归属，仅返回归属变化的段；相邻段不强制合并。同位置虚拟节点按节点名称/虚拟节点序号稳定排序，不会覆盖彼此；只有实际主属变化才产生迁移段。空环归属为 None：空→非空表示分配，非空→空表示撤销，两个空环没有区间。

前后快照必须使用同一哈希 callable 对象，防止混用不同哈希空间。快照之间若发生多次拓扑变更，规划器比较的是这两个端点的净变化，不假设只有一次增删。调用方负责协调真正的数据迁移和拓扑发布。

## 并发与复杂度

写者串行构建替代状态，只在发布时持有短锁；失败不发布部分状态。读取只短暂获取快照，随后在不可变数据上搜索。持有公开 snapshot 后，整批查询和统计不再受后续写入影响，也不逐键获取发布锁。旧快照仍占用其拓扑内存，调用方不应无限保留历史快照。锁保障线程安全，不代表 CPU 并行加速。

N 为环条目数、K 为新增节点的虚拟节点数、B 为前后不同位置的总数：查询 O(log N)；distinct 副本扫描最坏 O(N)；新增采用复制排序，保守上界 O((N+K) log(N+K))；删除扫描、排序保守上界 O(N log N)；状态 O(N)。迁移规划排序边界并逐段二分，O(B log B + B log(N_before+N_after))，结果 O(B)。这些是保守上界，未进行本轮吞吐压测。

## 验证与实验

```sh
python -m pytest tests -q -p no:cacheprovider
python examples/chapter_comparison.py
```

本轮完整套件 **108 passed**。新增 22 项覆盖增删、混合拓扑变更、环回、端点、强制碰撞、空环转换、快照存活、不同哈希函数拒绝，以及统计期间并发删除全部节点。保留既有属性测试与并发写入测试。

确定性实验：10 台节点、10,000 个 key-N 样本。每节点 1/100/200 个虚拟节点分别占 10/1000/2000 个条目，标准差/均值约 1.01794/0.05789/0.06078。10→11 台时一致性哈希迁移 969 个键，取模迁移 9066 个键。增加虚拟节点改善总体均匀性，但有限样本不保证每次单调改善；原书的均值比例不是硬性 SLA。

本实现未包括成员发现、故障探测、共识、数据复制/迁移执行器或真实跨机负载；get_nodes 仅提供不同物理节点候选，不产生副本。均匀键样本不等于请求流量，单个热点键仍归属一个主节点。
