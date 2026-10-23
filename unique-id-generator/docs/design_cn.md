# 设计：唯一 ID 生成器

## 架构概述

唯一 ID 生成器采用简洁的分层架构，核心由 Snowflake 生成器、可配置的位布局、用于单调时间跟踪的时钟抽象层，以及用于分解 ID 的解析器组成。备用策略提供回退选项。

```
┌─────────────────────────────────────────────────────────────────┐
│                      应用层                                       │
│         (basic_usage.py, multi_generator.py 示例)                 │
├─────────────────────────────────────────────────────────────────┤
│                      公共 API                                    │
│   (SnowflakeGenerator.generate / generate_batch / IDParser)     │
├──────────────────────┬──────────────────────────────────────────┤
│   核心生成器           │       备用策略                             │
│  (Snowflake 算法,     │   (UUIDGenerator,                        │
│   序列号管理,          │    TimestampRandomGenerator)             │
│   线程安全)           │                                          │
├──────────────────────┴──────────────────────────────────────────┤
│                    时钟抽象层                                       │
│        (SystemClock, MonotonicClock — 毫秒时间)                    │
├─────────────────────────────────────────────────────────────────┤
│                    配置                                           │
│   (SnowflakeConfig — 位布局, 起始时间, 数据中心/机器 ID)             │
├─────────────────────────────────────────────────────────────────┤
│                    ID 解析器                                       │
│   (将 ID 分解为 → 时间戳, 数据中心, 机器, 序列号)                     │
└─────────────────────────────────────────────────────────────────┘
```

### ID 生成流程

```
调用 generate()
        │
        ▼
┌──────────────────┐
│  获取锁           │ ◄── 线程安全 (threading.Lock)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐     ┌──────────────────┐
│  获取当前          │────▶│  时钟返回          │
│  时间戳 (毫秒)     │     │  毫秒值            │
└────────┬─────────┘     └──────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│  与 last_timestamp 比较                   │
│                                          │
│  • ts > last_ts → 序列号重置为 0          │
│  • ts == last_ts → 序列号自增             │
│  • ts < last_ts → ClockMovedBackwards!    │
└────────┬─────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│  检查序列号溢出                           │
│                                          │
│  • sequence > max → wait_next_millis()    │
│    然后将序列号重置为 0                    │
└────────┬─────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│  组合 64 位 ID:                           │
│                                          │
│  0 | timestamp | datacenter | machine | seq │
│  1    41 bits     5 bits     5 bits   12 bits │
│                                          │
│  id = (ts << 22) | (dc << 17) |          │
│       (machine << 12) | sequence         │
└────────┬─────────────────────────────────┘
         │
         ▼
┌──────────────────┐
│  释放锁           │
│  返回 ID          │
└──────────────────┘
```

## 项目结构

```
unique-id-generator/
├── pyproject.toml
├── README.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── src/
│   └── unique_id/
│       ├── __init__.py
│       ├── snowflake.py      # 核心 Snowflake 生成器
│       ├── config.py         # 配置 (位布局, 起始时间)
│       ├── clock.py          # 时钟抽象层 (单调时钟, NTP 感知)
│       ├── parser.py         # ID 解析/分解
│       ├── strategies.py     # 备用策略 (UUID, 时间戳-随机)
│       └── exceptions.py     # 自定义异常
├── tests/
│   ├── __init__.py
│   ├── test_snowflake.py
│   ├── test_config.py
│   ├── test_clock.py
│   ├── test_parser.py
│   ├── test_strategies.py
│   └── test_properties.py
└── examples/
    ├── basic_usage.py
    └── multi_generator.py
```

## 组件设计

### 1. 配置

```python
from dataclasses import dataclass


# 默认起始时间: 2024-01-01T00:00:00Z 对应的毫秒值
DEFAULT_EPOCH_MS = 1704067200000


@dataclass(frozen=True)
class SnowflakeConfig:
    """Snowflake ID 生成器的配置。

    定义位布局、自定义起始时间和机器身份标识。
    总位数 (timestamp + datacenter + machine + sequence) 必须等于 63
    (符号位始终为 0)。

    覆盖: FR-2.1, FR-2.2, FR-2.3, FR-2.4, FR-2.5, FR-2.6
    """

    # 位分配
    timestamp_bits: int = 41    # 从起始时间起约 69 年
    datacenter_bits: int = 5    # 32 个数据中心
    machine_bits: int = 5       # 每个数据中心 32 台机器
    sequence_bits: int = 12     # 每毫秒每台机器 4096 个 ID

    # 身份标识
    datacenter_id: int = 0
    machine_id: int = 0

    # 起始时间
    epoch_ms: int = DEFAULT_EPOCH_MS  # 2024-01-01T00:00:00Z

    def __post_init__(self) -> None:
        """创建时验证配置。

        抛出:
            InvalidConfigError: 如果位分配无效或 ID 超出容量。
        """
        ...

    @property
    def max_datacenter_id(self) -> int:
        """已配置位宽下的最大数据中心 ID (2^datacenter_bits - 1)。"""
        return (1 << self.datacenter_bits) - 1

    @property
    def max_machine_id(self) -> int:
        """已配置位宽下的最大机器 ID (2^machine_bits - 1)。"""
        return (1 << self.machine_bits) - 1

    @property
    def max_sequence(self) -> int:
        """已配置位宽下的最大序列号 (2^sequence_bits - 1)。"""
        return (1 << self.sequence_bits) - 1

    @property
    def max_timestamp(self) -> int:
        """已配置位宽下的最大时间戳值 (2^timestamp_bits - 1)。"""
        return (1 << self.timestamp_bits) - 1

    @property
    def timestamp_shift(self) -> int:
        """时间戳需要左移的位数。"""
        return self.datacenter_bits + self.machine_bits + self.sequence_bits

    @property
    def datacenter_shift(self) -> int:
        """数据中心 ID 需要左移的位数。"""
        return self.machine_bits + self.sequence_bits

    @property
    def machine_shift(self) -> int:
        """机器 ID 需要左移的位数。"""
        return self.sequence_bits
```

### 2. 异常

```python
class UniqueIDError(Exception):
    """唯一 ID 生成器错误的基类。"""
    pass


class ClockMovedBackwardsError(UniqueIDError):
    """当系统时钟回拨时抛出。

    这通常意味着存在 NTP 调整或时钟漂移。
    生成器会拒绝生成 ID 以防止重复。

    覆盖: FR-4.2
    """

    def __init__(self, last_timestamp: int, current_timestamp: int):
        self.last_timestamp = last_timestamp
        self.current_timestamp = current_timestamp
        self.drift_ms = last_timestamp - current_timestamp
        super().__init__(
            f"时钟回拨了 {self.drift_ms} 毫秒。"
            f"上次时间戳: {last_timestamp}, 当前: {current_timestamp}。"
            f"拒绝生成 ID 以避免重复。"
        )


class SequenceOverflowError(UniqueIDError):
    """当序列号在一个毫秒内溢出时抛出。

    正常情况下不应对外抛出 — 生成器会通过等待下一毫秒来处理溢出。
    该异常用于内部，或在禁用等待行为时使用。

    覆盖: FR-4.3
    """

    def __init__(self, timestamp: int, max_sequence: int):
        self.timestamp = timestamp
        self.max_sequence = max_sequence
        super().__init__(
            f"时间戳 {timestamp} 处序列号溢出。"
            f"最大序列号: {max_sequence}。"
            f"该毫秒内的全部 {max_sequence + 1} 个 ID 已耗尽。"
        )


class InvalidConfigError(UniqueIDError):
    """当配置无效时抛出。

    覆盖: FR-2.4, FR-2.5
    """

    def __init__(self, message: str):
        super().__init__(f"无效配置: {message}")
```

### 3. 时钟抽象层

```python
from abc import ABC, abstractmethod
import time


class Clock(ABC):
    """抽象时钟接口，以毫秒为单位返回时间。

    允许注入不同的时钟实现，便于测试，
    也支持在系统时钟和单调时钟之间切换。

    覆盖: FR-4.4
    """

    @abstractmethod
    def current_millis(self) -> int:
        """返回当前的毫秒时间。

        返回:
            自 Unix 起始时间以来的毫秒级当前时间戳。
        """
        ...


class SystemClock(Clock):
    """基于 time.time() 的系统时钟。

    使用系统壁钟时间，会受 NTP 调整和时钟漂移影响。
    适合大多数正确配置了 NTP 的生产环境使用。
    """

    def current_millis(self) -> int:
        """返回当前系统时间的毫秒值。"""
        return int(time.time() * 1000)


class MonotonicClock(Clock):
    """基于 time.monotonic() 的单调时钟。

    使用无法回退的单调时钟。在初始化时以系统时间为锚点，
    适合时钟漂移较为敏感的环境。

    注意：单调时钟本身是相对的 — 在创建时刻以系统时间为锚点，
    从而产生绝对的毫秒级时间戳。
    """

    def __init__(self) -> None:
        """初始化：将单调时钟锚定到当前系统时间。"""
        self._anchor_system_ms = int(time.time() * 1000)
        self._anchor_monotonic_ms = int(time.monotonic() * 1000)

    def current_millis(self) -> int:
        """返回当前时间的毫秒值 (单调递增)。

        计算: anchor_system_ms + (current_monotonic - anchor_monotonic)
        """
        elapsed = int(time.monotonic() * 1000) - self._anchor_monotonic_ms
        return self._anchor_system_ms + elapsed
```

### 4. Snowflake 生成器

```python
import threading
from typing import Optional


class SnowflakeGenerator:
    """核心 Snowflake ID 生成器。

    使用 Twitter Snowflake 算法生成 64 位唯一、可按时间排序的 ID。
    通过 threading 锁保护序列号和上次时间戳状态，实现线程安全。

    覆盖: FR-1, FR-3, FR-4, FR-5, FR-7
    """

    def __init__(
        self,
        config: Optional["SnowflakeConfig"] = None,
        clock: Optional["Clock"] = None,
    ):
        """初始化 Snowflake 生成器。

        参数:
            config: 生成器配置。为 None 时使用默认值。
            clock: 时钟实现。为 None 时使用 SystemClock。
        """
        self._config = config or SnowflakeConfig()
        self._clock = clock or SystemClock()
        self._sequence = 0
        self._last_timestamp = -1
        self._lock = threading.Lock()

    @property
    def config(self) -> "SnowflakeConfig":
        """获取生成器配置。"""
        return self._config

    def generate(self) -> int:
        """生成一个唯一的 Snowflake ID。

        线程安全。获取锁、读取当前时间戳、管理序列号，
        并组合出 64 位 ID。

        返回:
            一个 64 位唯一整数 ID。

        抛出:
            ClockMovedBackwardsError: 如果系统时钟发生回拨。
        """
        with self._lock:
            timestamp = self._current_millis()

            if timestamp < self._last_timestamp:
                raise ClockMovedBackwardsError(self._last_timestamp, timestamp)

            if timestamp == self._last_timestamp:
                self._sequence = (self._sequence + 1) & self._config.max_sequence
                if self._sequence == 0:
                    # 序列号溢出 — 等待下一毫秒
                    timestamp = self._wait_next_millis(self._last_timestamp)
            else:
                self._sequence = 0

            self._last_timestamp = timestamp

            # 组合 64 位 ID
            ts_offset = timestamp - self._config.epoch_ms
            id_value = (
                (ts_offset << self._config.timestamp_shift)
                | (self._config.datacenter_id << self._config.datacenter_shift)
                | (self._config.machine_id << self._config.machine_shift)
                | self._sequence
            )
            return id_value

    def generate_batch(self, count: int) -> list[int]:
        """高效生成多个唯一 ID。

        整批只获取一次锁，相比在循环中调用 generate()，
        减少了锁获取开销。

        参数:
            count: 要生成的 ID 数量，必须为正整数。

        返回:
            严格递增的唯一整数 ID 列表。

        抛出:
            ValueError: 如果 count 不是正整数。
            ClockMovedBackwardsError: 如果系统时钟发生回拨。
        """
        if count <= 0:
            raise ValueError(f"count 必须为正整数，实际为 {count}")

        ids: list[int] = []
        with self._lock:
            for _ in range(count):
                timestamp = self._current_millis()

                if timestamp < self._last_timestamp:
                    raise ClockMovedBackwardsError(self._last_timestamp, timestamp)

                if timestamp == self._last_timestamp:
                    self._sequence = (self._sequence + 1) & self._config.max_sequence
                    if self._sequence == 0:
                        timestamp = self._wait_next_millis(self._last_timestamp)
                else:
                    self._sequence = 0

                self._last_timestamp = timestamp

                ts_offset = timestamp - self._config.epoch_ms
                id_value = (
                    (ts_offset << self._config.timestamp_shift)
                    | (self._config.datacenter_id << self._config.datacenter_shift)
                    | (self._config.machine_id << self._config.machine_shift)
                    | self._sequence
                )
                ids.append(id_value)

        return ids

    def _wait_next_millis(self, last_timestamp: int) -> int:
        """自旋等待直到时钟推进到给定时间戳之后。

        当一个毫秒内序列号溢出时调用。

        参数:
            last_timestamp: 要等待越过的时间戳。

        返回:
            新的 (已推进) 毫秒级时间戳。
        """
        timestamp = self._current_millis()
        while timestamp <= last_timestamp:
            timestamp = self._current_millis()
        return timestamp

    def _current_millis(self) -> int:
        """从已配置的时钟获取当前毫秒时间。"""
        return self._clock.current_millis()
```

### 5. ID 解析器

```python
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class ParsedID:
    """已分解的 Snowflake ID，包含所有提取出的组件。

    覆盖: FR-6.4
    """
    id_value: int
    timestamp_ms: int       # 自起始时间以来的毫秒数
    datacenter_id: int
    machine_id: int
    sequence: int
    datetime_utc: datetime  # 转换为 UTC 的 datetime 对象

    def __repr__(self) -> str:
        return (
            f"ParsedID(id={self.id_value}, "
            f"timestamp_ms={self.timestamp_ms}, "
            f"datacenter={self.datacenter_id}, "
            f"machine={self.machine_id}, "
            f"sequence={self.sequence}, "
            f"datetime={self.datetime_utc.isoformat()})"
        )


class IDParser:
    """将 Snowflake ID 解析回其构成组件。

    使用与生成该 ID 的生成器相同的配置 (位布局、起始时间)
    来正确提取各个字段。

    覆盖: FR-6.1, FR-6.2, FR-6.3
    """

    def __init__(self, config: Optional["SnowflakeConfig"] = None):
        """使用一份配置初始化解析器。

        参数:
            config: 与生成器匹配的配置。为 None 时使用默认值。
        """
        self._config = config or SnowflakeConfig()

    def parse(self, id_value: int) -> ParsedID:
        """将 Snowflake ID 解析为其组件。

        通过基于配置的位掩码和移位，提取时间戳、数据中心 ID、
        机器 ID 和序列号。

        参数:
            id_value: 要解析的 64 位 Snowflake ID。

        返回:
            包含所有已提取字段的 ParsedID。

        抛出:
            ValueError: 如果 id_value 为负数或超过 64 位。
        """
        if id_value < 0:
            raise ValueError(f"ID 必须为非负数，实际为 {id_value}")
        if id_value.bit_length() > 63:
            raise ValueError(f"ID 超过 63 位: {id_value}")

        config = self._config

        # 提取序列号 (最低位)
        sequence_mask = (1 << config.sequence_bits) - 1
        sequence = id_value & sequence_mask

        # 提取机器 ID
        machine_mask = (1 << config.machine_bits) - 1
        machine_id = (id_value >> config.machine_shift) & machine_mask

        # 提取数据中心 ID
        datacenter_mask = (1 << config.datacenter_bits) - 1
        datacenter_id = (id_value >> config.datacenter_shift) & datacenter_mask

        # 提取时间戳偏移量
        timestamp_mask = (1 << config.timestamp_bits) - 1
        ts_offset = (id_value >> config.timestamp_shift) & timestamp_mask

        # 转换为绝对时间戳
        timestamp_ms = ts_offset + config.epoch_ms

        # 转换为 datetime
        datetime_utc = datetime.fromtimestamp(
            timestamp_ms / 1000.0, tz=timezone.utc
        )

        return ParsedID(
            id_value=id_value,
            timestamp_ms=timestamp_ms,
            datacenter_id=datacenter_id,
            machine_id=machine_id,
            sequence=sequence,
            datetime_utc=datetime_utc,
        )
```

### 6. 备用策略

```python
import uuid
import time
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass


class IDGenerator(ABC):
    """ID 生成策略的抽象基类。

    为不同的 ID 生成方案提供统一接口。

    覆盖: FR-8.3
    """

    @abstractmethod
    def generate(self) -> int | str:
        """生成一个唯一 ID。

        返回:
            唯一标识符 (数值策略返回 int，UUID 返回 str)。
        """
        ...

    @abstractmethod
    def generate_batch(self, count: int) -> list[int | str]:
        """生成多个唯一 ID。

        参数:
            count: 要生成的 ID 数量。

        返回:
            唯一标识符列表。
        """
        ...


class UUIDGenerator(IDGenerator):
    """UUID v4 生成器 (128 位随机)。

    与 Snowflake 的权衡:
    - 优点: 无需协调，无需配置即可全局唯一
    - 优点: 不依赖时钟
    - 缺点: 128 位 (不是 64 位)，不可按时间排序
    - 缺点: 带连字符的字符串表示 (36 个字符)
    - 缺点: 数据库索引性能较差 (随机分布)

    覆盖: FR-8.1
    """

    def generate(self) -> str:
        """生成一个 UUID v4 字符串。"""
        return str(uuid.uuid4())

    def generate_batch(self, count: int) -> list[str]:
        """生成多个 UUID v4 字符串。"""
        return [str(uuid.uuid4()) for _ in range(count)]


class TimestampRandomGenerator(IDGenerator):
    """时间戳 + 随机位生成器 (64 位)。

    将毫秒级时间戳 (42 位) 与随机位 (22 位) 组合。
    比 Snowflake 更简单，但唯一性保证较弱。

    与 Snowflake 的权衡:
    - 优点: 无需机器/数据中心配置
    - 优点: 实现更简单
    - 缺点: 概率性唯一 (同一毫秒内可能冲突)
    - 缺点: 非严格单调 (随机后缀)
    - 缺点: 无法从 ID 中提取机器身份

    覆盖: FR-8.2
    """

    def __init__(self, epoch_ms: int = 1704067200000):
        """使用自定义起始时间初始化。

        参数:
            epoch_ms: 自定义起始时间的毫秒值 (默认: 2024-01-01)。
        """
        self._epoch_ms = epoch_ms
        self._random = random.SystemRandom()

    def generate(self) -> int:
        """生成一个 64 位时间戳-随机 ID。"""
        ts = int(time.time() * 1000) - self._epoch_ms
        random_bits = self._random.getrandbits(22)
        return (ts << 22) | random_bits

    def generate_batch(self, count: int) -> list[int]:
        """生成多个时间戳-随机 ID。"""
        return [self.generate() for _ in range(count)]
```

## 设计决策

| 决策项 | 选择 | 依据 |
|--------|------|------|
| 语言 & 运行时 | Python >= 3.9 | 现代 Python 特性 (类型提示、dataclass)；与仓库中其他项目保持一致 |
| 构建系统 | hatchling (pyproject.toml) | 现代 Python 打包标准，与仓库中其他项目保持一致 |
| 项目布局 | src 布局 (`src/unique_id/`) | 防止从项目根目录意外导入；打包最佳实践 |
| 线程安全 | `threading.Lock` | 简单、有效，适用于单进程场景；GIL 已提供一定安全保障，但锁能确保正确性 |
| 时钟抽象层 | 基于接口 (ABC) | 支持注入 mock 时钟进行测试；为时钟漂移敏感场景提供单调时钟实现 |
| 配置 | 不可变 dataclass | 创建后不可变，防止意外修改；`__post_init__` 在构造时进行校验 |
| 位布局 | 可配置且带默认值 | 默认与 Twitter Snowflake 一致 (41+5+5+12)，但允许按需调整以适配不同规模需求 |
| 序列号溢出 | 等待下一毫秒 (自旋) | 确保不丢失 ID；罕见溢出场景 (>4096 ID/ms) 下可接受短暂的延迟突增 |
| 时钟漂移 | 抛出异常 | 快速失败，防止重复 ID；调用方可自行实现重试/退避策略 |
| ID 类型 | 纯 `int` | Python 64 位整数；无字符串转换开销；可直接作为数据库主键 |
| 批量生成 | 单次获取锁 | 减少批量 ID 生成时的开销；保持顺序性保证 |
| 备用策略 | 独立类 + 公共接口 | 清晰解耦；便于比较；为不同需求提供回退选项 |
| 起始时间 | 2024-01-01T00:00:00Z | 较近的起始时间最大化可用时间戳范围 (从 2024 起约 69 年) |
| 解析器 | 独立类 | 单一职责；可独立于生成器使用；支持对 ID 进行溯源分析 |

## 错误处理

| 错误场景 | 处理策略 |
|----------|----------|
| **时钟回拨** | 抛出带有漂移时长的 `ClockMovedBackwardsError`。调用方应等待或使用单调时钟。生成器拒绝继续产生 ID 以防止重复。 |
| **序列号溢出** | 自旋等待直到下一毫秒 (`_wait_next_millis`)。对调用方透明。仅在默认配置下每毫秒每台机器生成超过 4096 个 ID 时才会触发。 |
| **无效配置** | 在构造时抛出 `InvalidConfigError`。快速失败并给出描述性消息 (指明违反了哪条约束)。 |
| **数据中心/机器 ID 过大** | 在配置校验阶段抛出 `InvalidConfigError`。消息包含当前配置位宽下的最大允许值。 |
| **位分配总和不等于 63** | 在配置校验阶段抛出 `InvalidConfigError`。消息展示实际总和与期望的 63。 |
| **解析时传入负数或超大 ID** | `IDParser.parse()` 抛出 `ValueError`。在尝试提取之前先校验输入。 |
| **批量生成 count <= 0** | `generate_batch()` 抛出 `ValueError`。必须传入正整数。 |
| **时间戳超过最大值** | 会在起始时间之后约 69 年出现。抛出 `OverflowError` 并提示重新配置起始时间。 |
