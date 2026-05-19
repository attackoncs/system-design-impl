# 唯一 ID 生成器

基于 Twitter Snowflake 算法的 Python 实现，生成 64 位唯一、时间可排序的 ID，适用于分布式系统。

> English documentation: [README.md](./README.md)

## 概述

本库使用以下组合生成全局唯一的 64 位整数 ID：
- **时间戳**（自定义纪元以来的毫秒数）
- **数据中心 ID**（标识数据中心）
- **机器 ID**（标识数据中心内的机器）
- **序列号**（区分同一毫秒内生成的 ID）

### 位布局（默认：41+5+5+12）

```
┌───┬──────────────────────────────────────────┬───────┬───────┬──────────────┐
│ 0 │            Timestamp (41 bits)            │ DC(5) │ M(5)  │ Sequence(12) │
└───┴──────────────────────────────────────────┴───────┴───────┴──────────────┘
 1位    41位(~69年)                               5位     5位      12位(4096/ms)
```

| 字段 | 位数 | 说明 |
|------|------|------|
| 符号位 | 1 | 始终为 0（正整数） |
| 时间戳 | 41 | 自纪元起约 69 年 |
| 数据中心 | 5 | 最多 32 个数据中心 |
| 机器 | 5 | 每个数据中心最多 32 台机器 |
| 序列号 | 12 | 每毫秒每台机器最多 4096 个 ID |

## 安装

```bash
# 从项目根目录
pip install -e ".[dev]"
```

环境要求：
- Python >= 3.9
- 无运行时依赖（仅使用标准库）

## 快速开始

```python
from unique_id import SnowflakeGenerator, IDParser, SnowflakeConfig

# 创建生成器（默认配置）
generator = SnowflakeGenerator()

# 生成单个 ID
id_val = generator.generate()
print(id_val)  # 例如: 123456789012345

# 批量生成 ID
ids = generator.generate_batch(100)

# 将 ID 解析回各组成部分
parser = IDParser()
parsed = parser.parse(id_val)
print(parsed.timestamp_ms)    # 绝对时间戳（毫秒）
print(parsed.datacenter_id)   # 0
print(parsed.machine_id)      # 0
print(parsed.sequence)        # 0
print(parsed.datetime_utc)    # datetime 对象
```

## 配置

```python
from unique_id import SnowflakeConfig, SnowflakeGenerator

# 自定义数据中心和机器 ID
config = SnowflakeConfig(
    datacenter_id=5,
    machine_id=10,
)
gen = SnowflakeGenerator(config=config)

# 自定义位布局（更多机器，更少数据中心）
config = SnowflakeConfig(
    timestamp_bits=42,    # ~139 年
    datacenter_bits=3,    # 8 个数据中心
    machine_bits=6,       # 64 台机器
    sequence_bits=12,     # 4096/ms
    datacenter_id=3,
    machine_id=42,
)
```

### 配置字段

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `timestamp_bits` | 41 | 时间戳位数（约 69 年） |
| `datacenter_bits` | 5 | 数据中心 ID 位数（最多 32） |
| `machine_bits` | 5 | 机器 ID 位数（最多 32） |
| `sequence_bits` | 12 | 序列号位数（4096/ms） |
| `datacenter_id` | 0 | 当前生成器的数据中心 ID |
| `machine_id` | 0 | 当前生成器的机器 ID |
| `epoch_ms` | 1704067200000 | 自定义纪元（2024-01-01） |

**约束**：`timestamp_bits + datacenter_bits + machine_bits + sequence_bits == 63`

## API 参考

### SnowflakeGenerator 生成器

```python
class SnowflakeGenerator:
    def __init__(self, config=None, clock=None): ...
    def generate(self) -> int: ...
    def generate_batch(self, count: int) -> list[int]: ...
```

| 方法 | 说明 |
|------|------|
| `generate()` | 生成单个唯一 ID |
| `generate_batch(count)` | 高效批量生成多个 ID |

### IDParser 解析器

```python
class IDParser:
    def __init__(self, config=None): ...
    def parse(self, id_value: int) -> ParsedID: ...
```

`ParsedID` 包含以下字段：
- `id_value` — 原始 ID 值
- `timestamp_ms` — 绝对时间戳（毫秒）
- `datacenter_id` — 数据中心 ID
- `machine_id` — 机器 ID
- `sequence` — 序列号
- `datetime_utc` — UTC 时间（datetime 对象）

### 替代策略

```python
from unique_id import UUIDGenerator, TimestampRandomGenerator

# UUID v4（128 位随机，无排序保证）
uuid_gen = UUIDGenerator()
uuid_str = uuid_gen.generate()  # "550e8400-e29b-41d4-..."

# 时间戳 + 随机数（64 位，概率唯一性）
ts_gen = TimestampRandomGenerator()
ts_id = ts_gen.generate()  # 64 位整数
```

### 时钟实现

```python
from unique_id import SystemClock, MonotonicClock, SnowflakeGenerator

# 默认：SystemClock（使用 time.time()）
gen = SnowflakeGenerator()

# MonotonicClock：保证不会回退
gen = SnowflakeGenerator(clock=MonotonicClock())
```

## 算法对比

| 特性 | Snowflake | UUID v4 | 时间戳-随机 |
|------|-----------|---------|-------------|
| 大小 | 64 位整数 | 128 位字符串 | 64 位整数 |
| 时间可排序 | ✓ | ✗ | 部分 |
| 单调递增 | ✓ | ✗ | ✗ |
| 无需协调 | 每台机器独立 | 全局 | 全局 |
| 唯一性保证 | 确定性 | 概率性 | 概率性 |
| 数据库索引性能 | 优秀 | 差 | 良好 |
| 机器身份 | 嵌入 ID 中 | 无 | 无 |

## 线程安全

`SnowflakeGenerator` 是线程安全的。使用 `threading.Lock` 保护序列号和最后时间戳状态。多个线程可以安全地并发调用 `generate()`。

`generate_batch()` 对整个批次只获取一次锁，相比循环调用 `generate()` 减少了锁开销。

## 时钟处理

- **SystemClock**：使用 `time.time()`。受 NTP 调整影响。如果时钟回退，抛出 `ClockMovedBackwardsError`。
- **MonotonicClock**：使用 `time.monotonic()`，初始化时锚定到系统时间。不会回退。推荐在时钟漂移环境中使用。

## 错误处理

| 错误 | 触发条件 | 恢复方式 |
|------|----------|----------|
| `ClockMovedBackwardsError` | 检测到时钟回退 | 等待，或使用 MonotonicClock |
| `InvalidConfigError` | 配置无效 | 修正配置参数 |
| `SequenceOverflowError` | 内部序列溢出 | 自动处理（等待下一毫秒） |
| `ValueError` | 解析输入无效 | 检查 ID 值 |

## 设计决策

| 决策 | 理由 | 权衡 |
|------|------|------|
| **时钟回退时快速失败** | 抛异常防止重复 ID | 调用方需实现重试或切换 MonotonicClock |
| **序列溢出时自旋等待** | 确保不丢失 ID | 延迟峰值可接受（仅在 >4096 ID/ms 时触发） |
| **可配置位布局** | 适应不同规模需求 | 需要理解位分配的含义 |
| **自定义纪元 2024-01-01** | 最大化可用时间戳范围 | 约 69 年后需要新纪元 |
| **冻结 dataclass 配置** | 防止构造后意外修改 | 需要创建新实例来更改配置 |
| **纯标准库实现** | 零依赖，部署简单 | 无外部优化（如 C 扩展） |

## 运行测试

```bash
# 运行全部测试
pytest tests/ -v

# 仅运行属性测试
pytest tests/test_properties.py -v

# 运行特定模块
pytest tests/test_snowflake.py -v
pytest tests/test_config.py -v
```

## 项目结构

```
unique-id-generator/
├── pyproject.toml              # 构建配置（hatchling）
├── README.md                   # 英文文档
├── README_CN.md                # 中文文档（本文件）
├── src/unique_id/
│   ├── __init__.py             # 包导出
│   ├── snowflake.py            # 核心 Snowflake 生成器
│   ├── config.py               # 配置（位布局、纪元）
│   ├── clock.py                # 时钟抽象（系统时钟、单调时钟）
│   ├── parser.py               # ID 解析/分解
│   ├── strategies.py           # 替代策略（UUID、时间戳-随机）
│   └── exceptions.py           # 自定义异常
├── tests/
│   ├── test_snowflake.py       # 生成器测试
│   ├── test_config.py          # 配置测试
│   ├── test_clock.py           # 时钟测试
│   ├── test_parser.py          # 解析器测试
│   ├── test_strategies.py      # 替代策略测试
│   └── test_properties.py      # 属性测试（Hypothesis）
├── examples/
│   ├── basic_usage.py          # 基本用法示例
│   └── multi_generator.py      # 多生成器示例
└── docs/
    ├── requirements.md         # 需求文档
    ├── design.md               # 设计文档
    └── tasks.md                # 任务分解
```

## License

MIT
