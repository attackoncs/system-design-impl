# 一致性哈希 — 第6章

实现虚拟节点哈希环、不可变读快照、原子权重调整和流式迁移键筛选，运行时
只依赖标准库。[English](README.md) · [章节复审](docs/chapter06-review.md) ·
[设计](docs/design_cn.md) · [实测](benchmarks/REPORT.md)。

本轮保留已有章节算法，补齐指定节点键筛选/一次性迭代批处理，增加安全权重
变更与资源校验，优化批量建环和区间规划。154项测试通过，另用四个独立进程
验证三种哈希函数的主节点/副本候选路由一致。

## 运行

```sh
pip install -e ".[dev]"
python -m pytest tests -q
python examples/demo.py
python examples/chapter_comparison.py
python tools/process_probe.py --output benchmarks/process-local.json
python tools/benchmark.py --output benchmarks/local.json
```

源码语法支持Python3.9+，实测3.13.9；pytest/Hypothesis为可选开发依赖。
复审环境既有pytest缓存/临时目录不可访问，使用-p no:cacheprovider、工作区
.test-tmp下新的--basetemp并禁用无关插件自动加载。154项均以-Werror执行，
没有跳过或过滤项目警告。

## 路由与批处理

```python
from consistent_hashing import ConsistentHashRing, compute_distribution
ring = ConsistentHashRing(["web-1", "web-2", "web-3"], num_virtual_nodes=150,
                          max_virtual_nodes=1_000_000)
print(ring.get_node("user:42"))
print(ring.get_nodes("user:42", 2))  # 不同物理节点候选，不执行复制
view = ring.snapshot()
stream = ring.iter_assignments(f"key-{i}" for i in range(1000))
ring.add_node("web-4")
print(list(stream))  # 调用方法时已捕获旧拓扑，消费时不会换新拓扑
print(view.get_keys("web-1", ["user:42", "user:43"]))
print(compute_distribution(view, (f"key-{i}" for i in range(1000))))
```

默认SHA-1完整160位，MD5/SHA-256为128/256位；自定义函数输入字符串，
返回严格整数（不接受bool），允许有符号位置，不自动取模。函数必须确定、
线程安全且在快照存活期间保持稳定，不能修改同一环或等待其写入线程。

虚拟节点为node#0至node#(count-1)，排序按位置/物理名/索引，碰撞条目全部
保留。保持历史严格顺时针bisect_right语义：选择严格大于键位置的首个节点，
越界回绕；相等时跳过该位置所有条目。改为包含等号会改变路由，因此本轮
保留已有协议，并相应使用[start,end)迁移区间。

读者只在捕获快照时短暂持锁，随后访问不可变元组；删除全部在线节点后旧
快照仍可用。快照/迭代器会保留旧拓扑内存，释放后才能回收。环不保存数据
键注册表。空环路由/批路由抛异常，get_nodes(key,0)直接返回空列表。

## 节点、权重与资源

```python
ring.add_node("large", num_virtual_nodes=300)
before = ring.snapshot()
ring.set_virtual_nodes("large", 600)
ring.remove_node("web-2")
```

构造接受唯一非空节点名的序列，拒绝字符串/集合等模糊容器；批量计算后
只排序一次。增删节点和改权重都在写锁下只发布一次完整拓扑；哈希失败或
容量越界保留旧状态。增权只补高索引，减权只删除高索引，既有位置不重算，
不经过先删除再添加的中间窗口；相同权重为no-op。

虚拟节点数为正整数，总条目受max_virtual_nodes限制，默认1000000，构造
与更新在哈希前校验，写者按最新状态检查总量。该上限限制单个拓扑，不限制
调用方保留的所有历史快照；自定义哈希同线程重入写入会显式报错而非死锁。

## 受影响区间与键

```python
from consistent_hashing import affected_ranges, iter_migrations
before = ring.snapshot()
ring.add_node("new")
after = ring.snapshot()
for interval in affected_ranges(before, after):
    print(interval.start, interval.end, interval.previous_owner, interval.new_owner)
for item in iter_migrations(before, after, candidates_from_storage):
    print(item.key, item.previous_owner, item.new_owner)
```

必须传入显式快照，且共享同一哈希函数实例；不能证明任意自定义函数等价。
区间互不重叠、[start,end)，start>end回绕，start==end表示全环；空环owner
为None。保留相邻区间，不合并。规划合并有序边界并单向扫描两个拓扑，
不读取数据键，不调用哈希函数。

iter_migrations在调用时规划，消费时每候选键只哈希一次并二分区间，流式返回
KeyMigration，不构建两份全量映射；重复候选保留，无变化时不消费候选。
get_keys(node,candidates)在同一快照筛选，未知节点抛KeyError；真实数据键
必须来自外部索引/扫描，本库不能自动发现存储数据。

compute_distribution接受一次性迭代器，重复键按样本计数。
compute_redistribution(...,strict=True)校验映射完整性，统计显式None与owner
之间的变化；默认False兼容旧行为，忽略缺失/未分配对，返回字典按唯一键
计迁移数量。调用方需保证输入映射在分析期间稳定。

## 复杂度与边界

设V为条目数、K为变更节点条目数：单owner二分O(log V)，多副本候选最差
扫描O(V)，批量建环O(V log V)。新增/增权复制并排序新增项、合并有序段；
移除O(V+K log K)，减权O(V)。区间规划先合并边界，再单向扫描条目/边界；
CPython采用原生自适应排序合并两个有序段，保守排序上界O(V log V)。
迁移键筛选在规划后每键一次哈希、二分变化区间。状态O(V)，写者和旧快照
可能同时保留多个版本。线程安全不代表多核吞吐线性增长。

均匀哈希/等权条件下，N节点加一节点的平均期望迁移约1/(N+1)，删除约1/N，
不是任意输入的硬上限。虚拟节点改善均衡是统计规律，单个热点键不会拆分，
键数量均衡不代表流量或字节均衡。本轮建环/规划加快，查找及新增节点略慢，
全部原始比较见[实测](benchmarks/REPORT.md)。

未实现物理数据迁移/复制、成员发现、故障检测、一致性协议或跨主机存储服务。
