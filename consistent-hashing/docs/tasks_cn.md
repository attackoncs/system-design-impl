# 任务：一致性哈希（Consistent Hashing）

## 任务 1：项目搭建与哈希函数

- [x] 1.1 创建 `consistent-hashing/pyproject.toml`，包含项目元数据、依赖项（pytest、hypothesis）、hatchling 构建配置，以及指向 `tests/` 并将 `src/` 加入 pythonpath 的 pytest 配置
- [x] 1.2 创建 `consistent-hashing/src/consistent_hashing/__init__.py`，包含公开 API 导出（ConsistentHashRing、HashFunction、sha1_hash、md5_hash、sha256_hash、DistributionStats、compute_distribution）
- [x] 1.3 创建 `consistent-hashing/src/consistent_hashing/hash_functions.py`，包含 `HashFunction` 协议以及 `sha1_hash`、`md5_hash`、`sha256_hash` 实现，返回整数哈希值
- [x] 1.4 创建 `consistent-hashing/tests/__init__.py` 和 `consistent-hashing/tests/test_hash_functions.py`，单元测试验证：输出具有确定性、不同输入产生不同哈希、输出为正整数

## 任务 2：核心哈希环实现

- [x] 2.1 创建 `consistent-hashing/src/consistent_hashing/ring.py`，实现 `ConsistentHashRing` 类，包含 `__init__`、`add_node`、`remove_node` 方法——使用有序位置列表、位置到节点的映射，以及 `"{node}#{index}"` 格式的虚拟节点生成
- [x] 2.2 实现 `get_node(key)` 方法，使用 `bisect_right` 实现 O(log N) 的顺时针查找与回绕
- [x] 2.3 实现 `get_nodes(key, count)` 方法，沿顺时针遍历收集不同的物理服务器，跳过已收集服务器的虚拟节点
- [x] 2.4 实现 `nodes` 属性、`total_virtual_nodes` 属性，并通过 `add_node` 的可选 `num_virtual_nodes` 参数支持按服务器设置虚拟节点数量（加权节点）
- [x] 2.5 创建 `consistent-hashing/tests/test_ring.py`，单元测试覆盖：添加/移除节点、get_node 的确定性、get_nodes 返回不同的服务器、回绕行为、空环抛出 RuntimeError、重复节点抛出 ValueError、不存在的节点抛出 KeyError

## 任务 3：统计与数据迁移分析

- [x] 3.1 创建 `consistent-hashing/src/consistent_hashing/stats.py`，包含 `DistributionStats` dataclass 和 `compute_distribution(ring, keys)` 函数，计算每台服务器的键数量、均值、标准差、最小值、最大值和均衡比
- [x] 3.2 实现 `compute_redistribution(keys, assignment_before, assignment_after)` 函数，识别两次服务器分配之间移动的键，并返回移动键的来源/目的地
- [x] 3.3 创建 `consistent-hashing/tests/test_stats.py`，单元测试覆盖：每台服务器的键计数正确、标准差计算、数据迁移检测

## 任务 4：基于属性的测试

- [x] 4.1 编写属性测试：确定性——对任何稳定的哈希环和任意键，`get_node(key)` 始终返回同一台服务器（PBT）
- [x] 4.2 编写属性测试：服务器添加时的最小化数据迁移（PBT）
- [x] 4.3 编写属性测试：完全覆盖——每个键恰好映射到一台服务器，并且对于大规模随机键集合，所有拥有虚拟节点的服务器都至少分到一些键（PBT）
- [x] 4.4 编写属性测试：均衡分布——在每台服务器 ≥100 个虚拟节点且键数量 ≥1000 时，没有服务器持有的键数量超过均值的 2 倍（PBT）

## 任务 5：演示与文档

- [x] 5.1 创建 `consistent-hashing/examples/demo.py`，演示：创建包含多台服务器的哈希环、键分配、添加服务器并展示数据迁移情况、移除服务器并展示数据迁移情况、打印变更前后的分布统计信息
- [x] 5.2 创建 `consistent-hashing/README.md`，包含概览、安装说明、快速上手用法、算法讲解（哈希环、虚拟节点、顺时针查找）、API 参考和示例输出
- [x] 5.3 将 requirements.md、design.md 和 tasks.md 复制到 `consistent-hashing/docs/` 目录


## 2026-10-27复审验收

- [x] 重读原书1508–1734及图依赖/影响，保留已有实现。
- [x] 批建环、原子权重调整、预算校验及哈希契约复用。
- [x] 流式分配/指定节点筛选/迁移候选，单向区间扫描。
- [x] 严格映射比较、输入所有权及同线程写重入处理。
- [x] 154项测试、4进程/3哈希/4阶段验证及两个原始示例。
- [x] 保留基线/首轮/最终性能，报告查找/新增回归。
- [x] 双语README/设计/需求、章节记录、报告及根进度同步。
