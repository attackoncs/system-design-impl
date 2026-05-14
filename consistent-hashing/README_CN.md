# Consistent Hashing 一致性哈希

一个 Python 实现的一致性哈希算法库，通过虚拟节点实现跨服务器的均衡键分布。适用于分布式缓存、数据库和负载均衡器等场景。

> English documentation: [README.md](./README.md)

## 核心特性

- **O(log N) 键查找** — 使用二分查找在排序的虚拟节点位置中查找
- **虚拟节点** — 跨异构服务器实现均衡分布
- **最小重分布** — 添加/删除服务器仅移动约 1/N 的键
- **可配置哈希函数** — SHA-1（默认）、MD5、SHA-256 或自定义
- **服务器权重** — 为高容量服务器分配更多环空间
- **副本支持** — `get_nodes(key, count)` 返回多个不同的服务器
- **分布统计** — 分析均衡性和重分布影响

## 系统架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                             用户接口层                                    │
│                                                                         │
│   ConsistentHashRing • compute_distribution • compute_redistribution   │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          哈希环数据结构                                   │
│                                                                         │
│   排序的虚拟节点位置 + bisect_right 实现 O(log N) 查找                   │
│   位置到节点映射 • 虚拟节点生成                                          │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                            哈希函数层                                     │
│                                                                         │
│           SHA-1（默认）• MD5 • SHA-256 • 自定义                          │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         统计与工具层                                      │
│                                                                         │
│   DistributionStats • 均衡分析 • 重分布追踪                              │
└─────────────────────────────────────────────────────────────────────────┘
```

### 组件职责

| 组件 | 职责 |
|------|------|
| `ConsistentHashRing` | 核心数据结构、服务器管理、键查找 |
| `Hash Functions` | 将字符串映射到环位置（0 到 2^160 - 1） |
| `DistributionStats` | 键分布分析和均衡指标 |
| `compute_redistribution` | 追踪拓扑变化时的键移动 |

## 安装

```bash
cd consistent-hashing
pip install -e .
```

安装开发依赖：

```bash
pip install -e ".[dev]"
```

### 环境要求

- Python >= 3.9

## 快速开始

```python
from consistent_hashing import ConsistentHashRing, compute_distribution

# 创建包含 3 个服务器的哈希环
ring = ConsistentHashRing(
    nodes=["web-1", "web-2", "web-3"],
    num_virtual_nodes=150,
)

# 查找键对应的服务器
server = ring.get_node("user:12345")
print(f"user:12345 -> {server}")

# 获取多个服务器用于副本
replicas = ring.get_nodes("user:12345", count=2)
print(f"副本: {replicas}")

# 动态添加服务器
ring.add_node("web-4")

# 移除服务器
ring.remove_node("web-2")

# 检查分布统计
keys = [f"key:{i}" for i in range(1000)]
stats = compute_distribution(ring, keys)
print(f"均衡比率: {stats.balance_ratio:.4f}")
```

## 算法原理

### 哈希环

一致性哈希环将服务器和键都映射到一个环形哈希空间（SHA-1 为 0 到 2^160 - 1）。每个服务器通过哈希其标识符被放置在环上的一个或多个位置。

查找键对应的服务器：
1. 对键进行哈希，获取其在环上的位置
2. 顺时针遍历直到找到第一个服务器位置
3. 该服务器负责此键

### 虚拟节点

每个物理服务器在环上由多个"虚拟节点"表示。对于名为 `"web-1"` 的服务器，如果有 150 个虚拟节点，则通过哈希以下字符串生成位置：
- `"web-1#0"`, `"web-1#1"`, ..., `"web-1#149"`

虚拟节点的优势：
- **更好的均衡性** — 更多位置意味着更均匀的键分布
- **服务器权重** — 高容量服务器获得更多虚拟节点
- **平滑重分布** — 添加服务器从多个服务器各取一小部分

### 顺时针查找 (O(log N))

键查找使用 `bisect_right` 在排序的虚拟节点位置列表上：

```python
idx = bisect_right(sorted_positions, hash(key))
if idx == len(sorted_positions):
    idx = 0  # 绕回开头
server = position_to_node[sorted_positions[idx]]
```

时间复杂度为 O(log N)，其中 N 是虚拟节点总数。

## API 参考

### ConsistentHashRing

```python
ConsistentHashRing(
    nodes: list[str] | None = None,
    num_virtual_nodes: int = 150,
    hash_function: Callable[[str], int] | None = None,
)
```

**方法：**

| 方法 | 说明 |
|------|------|
| `add_node(node, num_virtual_nodes=None)` | 向环添加服务器 |
| `remove_node(node)` | 从环移除服务器 |
| `get_node(key) -> str` | 获取负责某键的服务器 |
| `get_nodes(key, count) -> list[str]` | 获取多个不同服务器用于副本 |

**属性：**

| 属性 | 说明 |
|------|------|
| `nodes` | 物理服务器名称列表 |
| `total_virtual_nodes` | 环上虚拟节点总数 |

### 哈希函数

```python
from consistent_hashing import sha1_hash, md5_hash, sha256_hash

# 使用不同的哈希函数
ring = ConsistentHashRing(hash_function=md5_hash)
```

| 函数 | 输出范围 | 说明 |
|------|----------|------|
| `sha1_hash` | 0 到 2^160 - 1 | 默认，分布优秀 |
| `md5_hash` | 0 到 2^128 - 1 | 更快，哈希空间较小 |
| `sha256_hash` | 0 到 2^256 - 1 | 加密强度更高 |

### 统计函数

```python
from consistent_hashing import compute_distribution
from consistent_hashing.stats import compute_redistribution

# 分布分析
stats = compute_distribution(ring, keys)
# 返回: DistributionStats(total_keys, num_servers, keys_per_server,
#         mean, std_dev, min_keys, max_keys, balance_ratio)

# 重分布分析
before = {k: ring.get_node(k) for k in keys}
ring.add_node("new-server")
after = {k: ring.get_node(k) for k in keys}
result = compute_redistribution(keys, before, after)
# 返回: {"moved": {key: {"from": ..., "to": ...}}, "total_moved": int}
```

## 最小重分布

添加或删除服务器时，只有落在受影响位置之间的键会移动：

```
添加前: [S1] ---- [S2] ---- [S3] ---- [S1]  (绕回)
键:    k1        k2        k3

添加 S4 后:
       [S1] -- [S4] -- [S2] ---- [S3] ---- [S1]
键:    k1      k4      k2        k3

只有 k4 移动了（从 S1 到 S4）。其他不变。
```

理论界限：添加/删除服务器重分布约 1/N 的键（N 为新的服务器总数）。

## 示例输出

```
$ python examples/demo.py

============================================================
  1. 创建包含 3 个服务器的哈希环
============================================================

  服务器: ['web-1', 'web-2', 'web-3']
  虚拟节点总数: 450

============================================================
  3. 分布统计（3 个服务器）
============================================================

  总键数: 1000
  每服务器平均键数: 333.3
  标准差: 18.9
  均衡比率: 0.0568（越低越好）
  每服务器键数:
           web-1:  358 #######
           web-2:  330 ######
           web-3:  312 ######

============================================================
  4. 添加服务器 'web-4'
============================================================

  重分布键数: 249/1000 (24.9%)
  理论理想值: ~250 (25.0%)

============================================================
  6. 移除服务器 'web-2'
============================================================

  重分布键数: 239/1000 (23.9%)
```

## 性能

| 操作 | 时间复杂度 | 说明 |
|------|-----------|------|
| `get_node(key)` | O(log N) | 二分查找，N = 虚拟节点数 |
| `add_node(node)` | O(K log N) | K = 该服务器的虚拟节点数 |
| `remove_node(node)` | O(K log N) | K = 该服务器的虚拟节点数 |
| 每服务器空间 | O(K) | K = 虚拟节点数 |

## 运行测试

```bash
pip install -e ".[dev]"
pytest                              # 所有测试
pytest tests/test_ring.py           # 哈希环测试
pytest tests/test_stats.py          # 统计测试
pytest tests/test_properties.py     # 属性测试
pytest --hypothesis-show-statistics # 详细 PBT 统计
```

### 测试覆盖

- **单元测试**：核心操作、边界情况、错误处理
- **属性测试**：确定性、最小重分布、均衡分布、覆盖性

## 项目结构

```
consistent-hashing/
├── pyproject.toml
├── README.md
├── README_CN.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── src/
│   └── consistent_hashing/
│       ├── __init__.py
│       ├── ring.py              # ConsistentHashRing 类
│       ├── hash_functions.py    # SHA-1, MD5, SHA-256
│       └── stats.py             # 分布分析
├── tests/
│   ├── test_ring.py
│   ├── test_hash_functions.py
│   ├── test_stats.py
│   └── test_properties.py      # Hypothesis 属性测试
└── examples/
    └── demo.py
```

## 参考文献

- Karger et al., "Consistent Hashing and Random Trees" (1997)
- DeCandia et al., "Dynamo: Amazon's Highly Available Key-value Store" (2007)

## License

MIT
