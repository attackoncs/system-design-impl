# 设计：一致性哈希（Consistent Hashing）

## 架构概览

一致性哈希模块采用简单的分层设计，以哈希环（Hash Ring）作为核心数据结构，支持可配置的哈希函数与虚拟节点（Virtual Node），以实现均衡的键分布。

```
┌─────────────────────────────────────────────────┐
│        公共 API（Public API）                   │
│   （ConsistentHashRing 类）                     │
├─────────────────────────────────────────────────┤
│           哈希环数据结构（Hash Ring）           │
│   （有序虚拟节点位置 + bisect）                 │
├─────────────────────────────────────────────────┤
│           哈希函数层（Hash Function）           │
│   （SHA-1、MD5、SHA-256、自定义）               │
├─────────────────────────────────────────────────┤
│      统计与工具（Statistics & Utilities）       │
│   （分布分析、可视化）                          │
└─────────────────────────────────────────────────┘
```

## 项目结构

```
consistent-hashing/
├── pyproject.toml
├── README.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── src/
│   └── consistent_hashing/
│       ├── __init__.py          # 公共 API 导出
│       ├── ring.py              # ConsistentHashRing 类
│       ├── hash_functions.py    # 哈希函数实现
│       └── stats.py             # 分布统计工具
├── tests/
│   ├── __init__.py
│   ├── test_ring.py             # 哈希环单元测试
│   ├── test_hash_functions.py   # 哈希函数单元测试
│   ├── test_stats.py            # 统计模块单元测试
│   └── test_properties.py      # 基于属性的测试（Hypothesis）
└── examples/
    └── demo.py                  # 演示脚本
```

## 组件设计

### 1. 哈希函数接口

```python
from typing import Protocol, Callable
import hashlib


class HashFunction(Protocol):
    """哈希环所用哈希函数的协议（Protocol）。"""

    def __call__(self, key: str) -> int:
        """将字符串键哈希为环上的整数位置。"""
        ...


def sha1_hash(key: str) -> int:
    """默认哈希函数，基于 SHA-1，返回完整的 160 位整数。"""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return int(digest, 16)


def md5_hash(key: str) -> int:
    """基于 MD5 的哈希函数，返回 128 位整数。"""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest, 16)


def sha256_hash(key: str) -> int:
    """基于 SHA-256 的哈希函数，返回 256 位整数。"""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest, 16)
```

### 2. ConsistentHashRing 类

```python
from bisect import bisect_right, insort
from dataclasses import dataclass, field
from typing import Optional, Callable


@dataclass
class NodeInfo:
    """物理服务器节点的元数据。"""
    name: str
    num_virtual_nodes: int
    weight: float = 1.0


class ConsistentHashRing:
    """带虚拟节点的一致性哈希环。

    使用 (position, node_name) 元组的有序列表和二分查找，
    实现 O(log N) 的键查找。
    """

    def __init__(
        self,
        nodes: Optional[list[str]] = None,
        num_virtual_nodes: int = 150,
        hash_function: Optional[Callable[[str], int]] = None,
    ):
        """初始化哈希环。

        Args:
            nodes: 初始的服务器节点标识符列表。
            num_virtual_nodes: 每台服务器默认的虚拟节点数量。
            hash_function: 将 str 映射为 int 的哈希函数。默认使用 SHA-1。
        """
        self._hash_function = hash_function or sha1_hash
        self._default_num_virtual_nodes = num_virtual_nodes

        # 用于二分查找的有序哈希位置列表
        self._sorted_positions: list[int] = []

        # 哈希位置到物理节点名称的映射
        self._position_to_node: dict[int, str] = {}

        # 物理节点名称到其元数据的映射
        self._nodes: dict[str, NodeInfo] = {}

        # 添加初始节点
        if nodes:
            for node in nodes:
                self.add_node(node)

    def add_node(self, node: str, num_virtual_nodes: Optional[int] = None) -> list[int]:
        """向环中添加一个服务器节点。

        Args:
            node: 服务器标识符字符串。
            num_virtual_nodes: 为该服务器覆盖默认的虚拟节点数量。

        Returns:
            虚拟节点被放置到的哈希位置列表。

        Raises:
            ValueError: 如果该节点已存在于环上。
        """
        ...

    def remove_node(self, node: str) -> list[int]:
        """从环中移除一个服务器节点及其所有虚拟节点。

        Args:
            node: 要移除的服务器标识符。

        Returns:
            被移除的哈希位置列表。

        Raises:
            KeyError: 如果该节点不存在于环上。
        """
        ...

    def get_node(self, key: str) -> str:
        """获取负责给定键的服务器。

        对键进行哈希，然后沿顺时针方向找到第一个服务器。

        Args:
            key: 要查找的键。

        Returns:
            负责该键的服务器节点标识符。

        Raises:
            RuntimeError: 如果哈希环为空（没有任何节点）。
        """
        ...

    def get_nodes(self, key: str, count: int) -> list[str]:
        """为一个键获取多个不同的服务器（用于副本/replication）。

        从键的位置开始沿顺时针遍历，收集不同的物理服务器
        （跳过已收集服务器的虚拟节点）。

        Args:
            key: 要查找的键。
            count: 要返回的不同服务器数量。

        Returns:
            不同服务器标识符的列表（至多 count 个，或服务器总数）。

        Raises:
            RuntimeError: 如果哈希环为空。
        """
        ...

    def get_redistribution_on_add(self, node: str, num_virtual_nodes: Optional[int] = None) -> dict[str, list[str]]:
        """预览如果添加某节点，哪些键会移动。

        不会修改哈希环。返回
        {source_server: [keys_that_would_move_away]} 形式的映射。

        注意：这需要在外部跟踪键；该方法基于提供的键集合工作。
        """
        ...

    def get_redistribution_on_remove(self, node: str) -> dict[str, list[str]]:
        """预览如果移除某节点，哪些键会移动。

        不会修改哈希环。
        """
        ...

    @property
    def nodes(self) -> list[str]:
        """环上所有物理节点名称的列表。"""
        return list(self._nodes.keys())

    @property
    def total_virtual_nodes(self) -> int:
        """环上虚拟节点的总数。"""
        return len(self._sorted_positions)

    def _hash(self, key: str) -> int:
        """将键哈希为环上的位置。"""
        return self._hash_function(key)

    def _virtual_node_key(self, node: str, index: int) -> str:
        """生成用于虚拟节点哈希的字符串。

        格式："{node}#{index}"
        """
        return f"{node}#{index}"
```

### 3. 键查找算法（二分查找）

```python
def get_node(self, key: str) -> str:
    """使用 bisect 实现 O(log N) 查找。"""
    if not self._sorted_positions:
        raise RuntimeError("Hash ring is empty — no nodes available.")

    hash_value = self._hash(key)

    # 找到第一个 >= hash_value 的位置（顺时针遍历）
    idx = bisect_right(self._sorted_positions, hash_value)

    # 如果越过最后一个位置，则回绕到起点
    if idx == len(self._sorted_positions):
        idx = 0

    position = self._sorted_positions[idx]
    return self._position_to_node[position]
```

### 4. 统计模块

```python
from dataclasses import dataclass


@dataclass
class DistributionStats:
    """键在各服务器间分布情况的统计信息。"""
    total_keys: int
    num_servers: int
    keys_per_server: dict[str, int]
    mean: float
    std_dev: float
    min_keys: int
    max_keys: int
    balance_ratio: float  # std_dev / mean（越低越好）


def compute_distribution(ring: "ConsistentHashRing", keys: list[str]) -> DistributionStats:
    """计算一组键在哈希环上的分布统计信息。

    Args:
        ring: 一致性哈希环。
        keys: 要分析的键列表。

    Returns:
        包含各服务器计数与聚合指标的 DistributionStats。
    """
    ...


def compute_redistribution(
    keys: list[str],
    assignment_before: dict[str, str],
    assignment_after: dict[str, str],
) -> dict[str, dict[str, list[str]]]:
    """计算两次分配之间哪些键发生了移动。

    Args:
        keys: 所有键。
        assignment_before: 变更前的 {key: server} 映射。
        assignment_after: 变更后的 {key: server} 映射。

    Returns:
        {"moved": {key: {"from": old_server, "to": new_server}}, "total_moved": int}
    """
    ...
```

### 5. 服务器增删与最小化数据迁移

**添加节点：**
1. 生成 K 个虚拟节点键：`"{node}#{i}"`，其中 i 遍历 range(K)
2. 对每个键哈希得到位置
3. 将位置插入有序列表（使用 `insort`）
4. 将每个位置映射到物理节点
5. 只有位于新位置与其前一位置（逆时针方向）之间的键会受影响

**移除节点：**
1. 查找该节点的所有位置
2. 从有序列表和位置到节点的映射中移除每个位置
3. 原本分配给被移除节点的键现在顺延到下一个顺时针节点

### 6. 虚拟节点命名约定

虚拟节点使用 `"{node}#{index}"` 格式生成：
- 服务器 "web-1" 拥有 3 个虚拟节点 → hash("web-1#0")、hash("web-1#1")、hash("web-1#2")
- 这确保了放置结果具有确定性和可复现性，不受插入顺序影响

## 设计决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 数据结构 | 有序列表 + bisect | O(log N) 查找，实现简单，基于 Python 标准库 |
| 哈希函数 | 默认 SHA-1 | 160 位哈希空间提供出色的分布特性；支持配置以保证灵活性 |
| 虚拟节点格式 | `"{node}#{index}"` | 简单、确定性强，避免节点之间冲突 |
| 默认虚拟节点数量 | 每台服务器 150 个 | 在分布均匀性与内存占用之间取得良好平衡 |
| 查找方向 | 顺时针（升序） | 沿用原始论文的标准约定 |
| 回绕方式 | 取模（越过末尾回到第一个节点） | 确保所有键无论位置如何都能映射到某台服务器 |
| 按服务器设置虚拟节点 | 通过权重（weight）支持 | 允许异构的服务器容量 |
| 统计功能 | 独立模块 | 保持核心哈希环逻辑简洁；统计为可选功能 |
| 基于属性的测试 | Hypothesis 库 | 在随机输入下验证不变量（确定性、最小化数据迁移） |

## 错误处理

- **哈希环为空**：`get_node()` 和 `get_nodes()` 抛出带有描述性信息的 `RuntimeError`。
- **节点重复**：如果节点已存在，`add_node()` 抛出 `ValueError`。
- **节点不存在**：如果节点不存在，`remove_node()` 抛出 `KeyError`。
- **无效的 count**：`get_nodes(key, count)` 返回至多 `min(count, total_physical_nodes)` 台服务器，不会报错。
- **哈希冲突**：如果两个虚拟节点哈希到同一位置，后写入的会覆盖前一个（在 SHA-1 的 160 位哈希空间中这种情况极其罕见，但仍会被妥善处理）。
