# 第6章：GitHub源码对照与实现覆盖

复核日期：2026-10-31。阅读根目录《System Design Interview.md》第1508–1734行，
用CodeGraph追踪本地环、迁移规划、统计和测试，再对照以下公开源码。
结论：本章放置算法已覆盖；本轮补充未变拓扑的快速返回，并修正测试中的统计假设。

## 开源实现值得读哪里

| 实现与源码 | 对照内容 | 本地取舍 |
|---|---|---|
| [ultrabug/uhashring](https://github.com/ultrabug/uhashring)：[ring.py](https://github.com/ultrabug/uhashring/blob/master/uhashring/ring.py)、[ring_meta.py](https://github.com/ultrabug/uhashring/blob/master/uhashring/ring_meta.py) | `HashRing._get_pos`用右侧二分和回绕；`MetaRing`按虚拟节点数乘权重生成位置，并记录各节点环点 | 严格顺时针语义一致，但虚拟标识、默认哈希和碰撞协议不同，不能直接互换路由。继续保留全部碰撞条目，不用单个位置到节点的字典覆盖旧owner。 |
| [stathat/consistent](https://github.com/stathat/consistent)：[consistent.go](https://github.com/stathat/consistent/blob/master/consistent.go) | `search`查严格大于键的位置；`GetN`去重物理节点；读写由`RWMutex`保护 | 本地已提供去重候选；采用写者串行、短发布锁和不可变快照，慢写者构建时读者可继续路由。保留现有并发模型。 |
| [buraksezer/consistent](https://github.com/buraksezer/consistent)：[consistent.go](https://github.com/buraksezer/consistent/blob/master/consistent.go) | `FindPartitionID`先取模固定分区数，`distributePartitions`与`distributeWithLoad`再按负载上限选择成员；`DefaultReplicaKey`分隔名字和索引 | 固定分区加负载约束是不同方案。分区可被分给后续有容量的成员，不能直接替换本章键到虚拟节点查找。本地`node#index`也有明确分隔符。 |

以上为检索时默认分支的源码阅读，没有运行这些外部库，也没有复制其实现或引入
新的运行依赖。比较说明不代表对外部库的全面质量审计。

## 原书覆盖与验证入口

| 章节内容 | 本地实现 | 验证入口 |
|---|---|---|
| 取模重哈希问题 | 环与取模的扩容比较 | [chapter_comparison.py](../examples/chapter_comparison.py) |
| 哈希空间、服务器与键映射 | 完整SHA-1/MD5/SHA-256、虚拟标识 | [hash_functions.py](../src/consistent_hashing/hash_functions.py)、[test_hash_functions.py](../tests/test_hash_functions.py) |
| 顺时针查找、精确边界与回绕 | `RingSnapshot._start`的`bisect_right` | [ring.py](../src/consistent_hashing/ring.py)、新增线性参照测试 |
| 增删节点只影响局部归属 | 原子发布、碰撞条目独立保留 | [test_properties.py](../tests/test_properties.py)、[test_concurrency.py](../tests/test_concurrency.py) |
| 分区不均、虚拟节点与内存权衡 | 可配节点数、原子权重调整、分布统计 | [stats.py](../src/consistent_hashing/stats.py)、[实测](../benchmarks/REPORT.md) |
| 找受影响区间与键 | `affected_ranges`、`iter_migrations`、`get_keys` | [migration.py](../src/consistent_hashing/migration.py)、[test_chapter_coverage.py](../tests/test_chapter_coverage.py) |
| 应用案例及热点讨论 | 放置能力与边界 | [README_CN.md](../README_CN.md)及[学习指南](learning-guide_cn.md) |

书中列举Dynamo、Cassandra、Discord等是应用背景，不要求在哈希环库里重写这些
系统。单个热点键仍只有一个主owner；副本候选不是数据复制。

## 本轮具体改动

### 同一拓扑不用重新扫描

连续取得两个快照，期间没有成功发布新拓扑时，两者引用同一份不可变状态。
原实现仍合并位置并扫描所有环点；现在先完成类型与哈希函数身份校验，再按
状态身份直接返回空计划，成本O(1)。分别构造但内容相同的环仍执行原有扫描；
不增加全局缓存、不比较可变在线对象，也不改变任何键的路由。

`iter_migrations`复用该路径，无变化时依然不读取候选键。普通变化规划的扫描、
区间边界和筛选保持原协议。[基准工具](../tools/benchmark.py)同时测量未变与
变化拓扑，避免把快速路径的收益说成所有迁移规划都提速。

### 属性测试验证确切保证

原来三个Hypothesis用例要求迁移量不超过平均比例的三倍、所有服务器都有样本键、
单节点样本数不超过均值两倍。这些不是一致性哈希对任意输入的保证。现在验证：

- 加节点后改变的键只迁向新节点，移除该节点后恢复原分配。
- 计数保留所有物理成员，包括零计数成员，并正确累计重复样本。
- 样本计数、均值、总体标准差、最小/最大值及均衡比与独立计算一致。

[新增9项用例](../tests/test_open_source_review.py)用独立的线性顺时针参照，覆盖
三种内置哈希及有符号的小碰撞空间、中文及含分隔符的节点名、增删及权重变化、
精确环点键和副本候选顺序。迁移结果与参照路由差异逐项核对，旧快照仍保持原归属。
另有反例：100个不同候选键可全部迁向新节点；碰撞时一个成员可没有主键，
但仍可作为副本候选；未变及独立重建的等价拓扑均不消费候选。

## 验收证据与范围

- 基线154项通过；最终163项通过，Python3.13.9，`-W error`，无跳过和警告过滤。
  禁用无关pytest插件及缓存，使用工作区新临时目录。
- [四进程记录](../benchmarks/processes-2026-10-31.json)：每进程2002键，
  三种哈希、四个阶段，主节点与三个副本候选摘要一致，成员插入顺序不同。
- 两个示例通过；加节点迁移969/10000、取模9066、移除迁移848，增权迁移767，
  恢复权重后路由完全复原。
- [前后性能及限制](../benchmarks/REPORT.md)：5万条目未变计划约59.12ms降至
  0.01ms以内。这是消除扫描的单次调用微基准，短耗时接近计时开销，不宣称吞吐倍数。

CodeGraph用于定位`ConsistentHashRing → RingSnapshot`及迁移规划/统计调用关系。
code-review-graph首轮增量更新4个文件、29节点、335边，无构建错误；四个变更Python
文件的影响查询显示24个直接节点、1个附加文件，但有230处调用未解析。新增未跟踪
测试未被该图索引，已由实际测试补核；图结果不能替代行为验证。
收尾再次增量更新3个文件、30节点、284边，无构建错误。

本轮不增加成员发现、故障检测、共识、物理迁移或真实数据复制。本机多进程结果
验证相同拓扑的放置一致性，不代表跨机成员同步或生产吞吐能力。
