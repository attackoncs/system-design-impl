# 设计：唯一 ID 生成器

> 2026-10-31：[开源对照、章节覆盖与122项验证](open-source-review_cn.md)；[历史审查](chapter08-review.md)。组件实现以源码链接为准，避免维护第二份过期代码。

## 架构概述

唯一 ID 生成器采用简洁的分层架构，核心由 Snowflake 生成器、可配置的位布局、用于单调时间跟踪的时钟抽象层，以及用于分解 ID 的解析器组成。备用策略用于显式比较，不提供自动故障切换。

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

[SnowflakeConfig](../src/unique_id/config.py)

### 2. 异常

[Exceptions](../src/unique_id/exceptions.py)

### 3. 时钟抽象层

[Clock](../src/unique_id/clock.py)

### 4. Snowflake 生成器

[SnowflakeGenerator](../src/unique_id/snowflake.py)

### 5. ID 解析器

[IDParser](../src/unique_id/parser.py)

### 6. 备用策略

[UUIDGenerator / TimestampRandomGenerator](../src/unique_id/strategies.py)

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
| 序列号溢出 | 有界等待并sleep | 默认1秒单调截止期限；超时抛SequenceOverflowError，不重置已用状态 |
| 时钟漂移 | 抛出异常 | 快速失败，防止重复 ID；调用方可自行实现重试/退避策略 |
| ID 类型 | 纯 `int` | Python int的Snowflake输出限制为0..2**63-1，可存入有符号64位列 |
| 批量生成 | 单次获取锁 | 减少批量 ID 生成时的开销；保持顺序性保证 |
| 备用策略 | 独立类 + 公共接口 | 清晰解耦；便于比较；为不同需求提供回退选项 |
| 起始时间 | 2024-01-01T00:00:00Z | 较近的起始时间最大化可用时间戳范围 (从 2024 起约 69 年) |
| 解析器 | 独立类 | 单一职责；可独立于生成器使用；支持对 ID 进行溯源分析 |

## 错误处理

| 错误场景 | 处理策略 |
|----------|----------|
| **时钟回拨** | 抛出带有漂移时长的 `ClockMovedBackwardsError`。调用方应等待或使用单调时钟。生成器拒绝继续产生 ID 以防止重复。 |
| **序列号溢出** | sleep等待下一毫秒；回拨或超时即失败。SequenceOverflowError可对外抛出，不保证固定等待延迟。 |
| **无效配置** | 在构造时抛出 `InvalidConfigError`。快速失败并给出描述性消息 (指明违反了哪条约束)。 |
| **数据中心/机器 ID 过大** | 在配置校验阶段抛出 `InvalidConfigError`。消息包含当前配置位宽下的最大允许值。 |
| **位分配总和不等于 63** | 在配置校验阶段抛出 `InvalidConfigError`。消息展示实际总和与期望的 63。 |
| **解析时传入负数或超大 ID** | `IDParser.parse()` 抛出 `ValueError`。在尝试提取之前先校验输入。 |
| **批量生成 count <= 0** | `generate_batch()` 抛出 `ValueError`。必须传入正整数。 |
| **时间戳超过最大值** | 时间偏移超出配置位宽时抛ValueError；既有ID空间不能直接切换epoch，须规划迁移。 |


## 当前不变量与边界

不可变配置拥有位布局、epoch和节点身份。构造时预计算序列/时间上限、时间位移
及节点前缀，逐ID只读取时钟、校验并推进状态、组合字段。同一把锁覆盖读时钟、
有界等待与状态发布；批量调用整批持锁，中途失败会消耗之前已生成的ID。

解析仅接受0..2**63-1内的真正整数，并使用相同布局/epoch。UTC通过Unix起点
加整数毫秒timedelta得到，避免浮点秒误差和系统时间转换限制。数值ID可以合法，
但其日期超出Python datetime范围，此时明确抛ValueError。
UUID和时间戳-随机批量接口也要求正整数count；后者要求正整数epoch、42位时间
偏移，生成的是无符号64位概率性ID，不能替代Snowflake的有符号范围唯一ID。

全局唯一性依赖统一布局/epoch、不同活跃身份和重启后不复用旧时间/序列。
未实现身份租约、持久化时间高水位、跨进程锁、NTP管理或自动故障切换。
MonotonicClock仅保护本对象生命周期内不回拨，不同步机器、不阻止身份重用。
不同布局或epoch不构成统一的全局唯一命名空间。见[实测](../benchmarks/REPORT.md)。
