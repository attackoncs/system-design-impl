# 第6章：GitHub源码对照与实现覆盖

## 2026-11-17 当前复核

结论：第6章要求的取模重哈希对照、哈希环、严格顺时针查找、增删节点、虚拟节点、
分布统计和受影响区间/键筛选均已有实现。本轮未发现需修复的放置或并发缺陷，
不新增功能，不修改业务代码、测试代码或benchmark代码。复用现有验证即可覆盖本轮范围。

### 章节到代码

| 章节要求 | 实现入口与核对结果 |
|---|---|
| 取模重哈希与迁移比较 | examples/chapter_comparison.py：同一万键扩容，环迁移969、取模迁移9066；这是指定样本，不是比例保证。 |
| 哈希空间、服务器和键 | hash_functions.py保留完整SHA-1/MD5/SHA-256整数空间；ring.py以node#index生成虚拟标识。 |
| 顺时针查找与回绕 | RingSnapshot._start使用bisect_right，精确相等跳过所有同位点；沿用既有路由协议。 |
| 新增/移除仅局部重分配 | add_node/remove_node先构造完整替代状态再发布；碰撞条目独立保留，哈希失败不发布。 |
| 虚拟节点、不同容量与内存权衡 | set_virtual_nodes原子增减权重，保留已有虚拟索引；总条目容量有上限，分布统计含零样本成员。 |
| 找受影响范围与键 | affected_ranges规划[start,end)区间，iter_migrations单次哈希并流式筛选外部候选；get_keys不声称发现真实存储键。 |
| 并发与工程结构 | 写者串行构造、短发布锁、不可变快照；统计和批处理固定一次快照，不持锁执行整批查找。 |

本章提到Dynamo/Cassandra等属于应用背景。当前库不执行数据传输、复制、成员发现、
共识或故障检测；虚拟节点改善统计分布，但不拆分单个热点键，也不保证任意样本均匀。
没有为扩大实现范围加入固定分区/有界负载算法或全成员原子替换API。

### GitHub源码参考

本轮实际读取了：

- [serialx/hashring/hashring.go](https://github.com/serialx/hashring/blob/master/hashring.go)，
  文件blob摘要ad2ac0188a43a47fe7766fc8e4e7131fd95763a7。
  对照权重生成、严格大于的二分查找、回绕和物理成员去重。上游也提供权重集合更新，
  但本章只要求逐节点增删，本轮不扩大公开API。上游使用位置到节点的map，本地继续
  保留全部碰撞条目；虚拟标识和默认哈希不同，不能直接互换路由。
- [buraksezer/consistent/consistent.go](https://github.com/buraksezer/consistent/blob/master/consistent.go)，
  文件blob摘要7bd648f01e2885bd69545c9ca2855a74aaf1d60b。
  对照RWMutex、固定分区与负载约束。该方案和本章直接键到环点放置有区别，
  不引入其分区协议或锁结构；保留本地快照发布模型。

链接指向检索时默认分支，可能随后变化，摘要标识所读内容。没有安装运行这些上游库、
复制实现或引入依赖；源码对照不是对上游项目的完整审计。

### CodeGraph与独立核对

使用Linux Node执行已有codegraph入口，指定CODEGRAPH_DIR=.codegraph-wsl，避免
Windows与WSL共享SQLite索引。索引18文件、306节点、813边。
affected_ranges的调用查询定位到RingSnapshot、AffectedRange和_owners_at_boundaries；
get_node影响查询区分同文件的两个定义：快照方法3节点/2边，在线环方法26节点/25边
（深度2）。静态分派、同名定义、查询深度均可能限制结果，不能用图替代行为证明。

两个独立只读审查分别检查章节覆盖、锁顺序和区间选择，没有确认中高风险缺陷。
补充的临时核对覆盖200组拓扑变化、3400个候选键，包括有符号位置、碰撞、空环和权重变化；
范围及流式迁移与前后实际路由相符，未把临时脚本加入项目测试或基准代码。

### 验证

Python3.14.8 / pytest9.2.0 / Hypothesis6.170.15：原有163项全部通过，13.02秒，
-Werror、无跳过、无警告过滤。coverage行/分支综合98%，80%阈值通过；
Ruff独立E4/E7/E9/F检查通过。源码语法目标仍为Python3.9+，未在每个版本执行。

复用tools/process_probe.py：4个独立进程、3种哈希、4个拓扑阶段、每阶段2002键，
主节点及3个候选节点摘要完全一致，约0.44秒；成员插入顺序不同也不影响放置。
这是本机进程间算法确定性验证，不等同于跨机成员同步或数据复制。
两个既有示例也通过；移除节点迁移848键，增权迁移767键，恢复权重后路由复原。
没有新增或改写基准，也不据此宣称吞吐提升。

在consistent-hashing目录复现：

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 HYPOTHESIS_STORAGE_DIRECTORY=/tmp/ch06-hypothesis python -m coverage run --branch --source=src/consistent_hashing -m pytest -q -W error -p no:cacheprovider --basetemp=/tmp/ch06-tests tests
python -m coverage report --fail-under=80
PYTHONPATH=src python tools/process_probe.py --output /tmp/ch06-processes.json
PYTHONPATH=src python examples/chapter_comparison.py
PYTHONPATH=src python examples/demo.py
```

coverage为额外验证工具（pip install coverage），其余使用现有dev依赖。进程探针需允许
共享屏障的运行环境；本轮在沙箱外运行，结果保存在/tmp，没有改写历史benchmark记录。

## 2026-10-31 历史复核

以下保留前轮记录，本轮结论和环境以本文当前复核为准。

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
