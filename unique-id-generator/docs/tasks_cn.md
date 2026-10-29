# 任务：唯一 ID 生成器

## 任务依赖图

```
任务 1 (项目脚手架)
  │
  ├──►任务 2 (配置)
  │      │
  ├──►任务 3 (时钟抽象层)
  │      │
  │      ▼
  │    任务 4 (Snowflake 生成器) ◄── 任务 2, 3
  │      │
  ├──►任务 5 (ID 解析器) ◄── 任务 2
  │      │
  ├──►任务 6 (备用策略)
  │      │
  │      ▼
  │    任务 7 (单元测试) ◄── 任务 2, 3, 4, 5, 6
  │      │
  │      ▼
  │    任务 8 (基于属性的测试) ◄── 任务 4, 5, 7
  │      │
  │      ▼
  └────►任务 9 (示例与文档) ◄── 任务 4, 5, 7
```

**依赖关系：**
- 任务 1：无依赖 (基础)
- 任务 2：依赖任务 1
- 任务 3：依赖任务 1
- 任务 4：依赖任务 2、3
- 任务 5：依赖任务 2
- 任务 6：依赖任务 1
- 任务 7：依赖任务 2、3、4、5、6
- 任务 8：依赖任务 4、5、7
- 任务 9：依赖任务 4、5、7

---

## 任务 1：项目脚手架

- [x] 1.1 使用 hatchling 构建系统创建 `pyproject.toml`
  - 文件：`unique-id-generator/pyproject.toml`
  - Python >= 3.9
  - 无运行时依赖 (仅使用标准库)
  - 开发依赖：`pytest`、`hypothesis`
  - 构建目标：`packages = ["src/unique_id"]`
  - Pytest 配置：`testpaths = ["tests"]`、`pythonpath = ["src"]`

- [x] 1.2 创建目录结构
  - 目录：
    - `unique-id-generator/src/unique_id/`
    - `unique-id-generator/tests/`
    - `unique-id-generator/examples/`
    - `unique-id-generator/docs/`
  - 为 `src/unique_id/` 和 `tests/` 创建 `__init__.py` 文件

- [x] 1.3 创建 `src/unique_id/__init__.py` 并导出公共 API
  - 文件：`unique-id-generator/src/unique_id/__init__.py`
  - 导出：`SnowflakeGenerator`、`SnowflakeConfig`、`IDParser`、`ParsedID`
  - 导出：`SystemClock`、`MonotonicClock`
  - 导出：`UUIDGenerator`、`TimestampRandomGenerator`
  - 导出：`ClockMovedBackwardsError`、`SequenceOverflowError`、`InvalidConfigError`
  - 版本号：`__version__ = "0.1.0"`

- [x] 1.4 创建包含自定义异常的 `src/unique_id/exceptions.py`
  - 文件：`unique-id-generator/src/unique_id/exceptions.py`
  - 类：`UniqueIDError` (基类)、`ClockMovedBackwardsError`、`SequenceOverflowError`、`InvalidConfigError`
  - `ClockMovedBackwardsError`：存储 `last_timestamp`、`current_timestamp`、`drift_ms`
  - `SequenceOverflowError`：存储 `timestamp`、`max_sequence`
  - `InvalidConfigError`：存储描述性消息

- [x] 1.5 验证项目可正确安装
  - 成功执行 `pip install -e ".[dev]"`
  - 运行 `pytest --collect-only` 验证测试可被发现

---

## 任务 2：配置

- [x] 2.1 实现 `SnowflakeConfig` dataclass
  - 文件：`unique-id-generator/src/unique_id/config.py`
  - 不可变 dataclass，字段：`timestamp_bits`、`datacenter_bits`、`machine_bits`、`sequence_bits`、`datacenter_id`、`machine_id`、`epoch_ms`
  - 默认值：41、5、5、12、0、0、1704067200000 (2024-01-01T00:00:00Z)
  - 计算属性：`max_datacenter_id`、`max_machine_id`、`max_sequence`、`max_timestamp`
  - 移位属性：`timestamp_shift`、`datacenter_shift`、`machine_shift`

- [x] 2.2 在 `__post_init__` 中实现配置校验
  - 校验总位数 == 63 (timestamp + datacenter + machine + sequence)
  - 校验所有位字段均为正整数
  - 校验 `datacenter_id` <= `max_datacenter_id`
  - 校验 `machine_id` <= `max_machine_id`
  - 校验 `epoch_ms` 为正数
  - 失败时抛出带有描述性消息的 `InvalidConfigError`

- [x] 2.3 编写配置测试
  - 文件：`unique-id-generator/tests/test_config.py`
  - 测试：默认配置创建有效实例
  - 测试：自定义位布局 (例如 42+4+4+13) 可被接受
  - 测试：位数总和不等于 63 时抛出 `InvalidConfigError`
  - 测试：datacenter_id 超过最大值时抛出 `InvalidConfigError`
  - 测试：machine_id 超过最大值时抛出 `InvalidConfigError`
  - 测试：位值为负数时抛出 `InvalidConfigError`
  - 测试：计算属性返回正确值
  - 测试：默认和自定义配置下移位属性均正确

---

## 任务 3：时钟抽象层

- [x] 3.1 实现 `Clock` 抽象基类
  - 文件：`unique-id-generator/src/unique_id/clock.py`
  - 抽象方法：`current_millis() -> int`
  - 从 `abc` 模块导入

- [x] 3.2 实现 `SystemClock`
  - 文件：`unique-id-generator/src/unique_id/clock.py`
  - 使用 `time.time() * 1000` 转为 int
  - 简单实现，无状态

- [x] 3.3 实现 `MonotonicClock`
  - 文件：`unique-id-generator/src/unique_id/clock.py`
  - 在初始化时将 `time.monotonic()` 锚定到 `time.time()`
  - `current_millis()` 返回：`anchor_system_ms + (current_monotonic - anchor_monotonic)`
  - 保证单调递增的值

- [x] 3.4 编写时钟测试
  - 文件：`unique-id-generator/tests/test_clock.py`
  - 测试：`SystemClock.current_millis()` 返回合理值 (与 `time.time() * 1000` 最近几秒内一致)
  - 测试：`MonotonicClock.current_millis()` 返回合理值
  - 测试：多次调用 `MonotonicClock` 返回值单调递增
  - 测试：`SystemClock` 的值与系统时间近似相等
  - 测试：两种时钟均实现了 `Clock` 接口

---

## 任务 4：Snowflake 生成器

- [x] 4.1 实现 `SnowflakeGenerator.__init__`
  - 文件：`unique-id-generator/src/unique_id/snowflake.py`
  - 接受可选 `config` (默认 `SnowflakeConfig()`) 和 `clock` (默认 `SystemClock()`)
  - 初始化：`_sequence = 0`、`_last_timestamp = -1`、`_lock = threading.Lock()`

- [x] 4.2 实现 `SnowflakeGenerator.generate()`
  - 获取锁
  - 从时钟获取当前时间戳
  - 时钟漂移检查：如果 `timestamp < last_timestamp`，抛出 `ClockMovedBackwardsError`
  - 同一毫秒：递增序列号，检查是否溢出 → `_wait_next_millis()`
  - 新毫秒：序列号重置为 0
  - 组合 ID：`(ts_offset << timestamp_shift) | (dc << dc_shift) | (machine << machine_shift) | sequence`
  - 更新 `_last_timestamp`，释放锁，返回 ID

- [x] 4.3 实现 `SnowflakeGenerator.generate_batch(count)`
  - 校验 count > 0
  - 整批只获取一次锁
  - 循环 `count` 次，使用与 `generate()` 相同的逻辑
  - 返回 ID 列表

- [x] 4.4 实现 `SnowflakeGenerator._wait_next_millis(last_timestamp)`
  - 自旋等待：循环调用 `_current_millis()` 直到结果 > `last_timestamp`
  - 返回新的时间戳

- [x] 4.5 编写 Snowflake 生成器测试
  - 文件：`unique-id-generator/tests/test_snowflake.py`
  - 测试：`generate()` 返回正的 64 位整数
  - 测试：连续 ID 严格递增
  - 测试：来自不同生成器 (不同 dc/machine) 的 ID 互不相同
  - 测试：序列号在同一毫秒内递增 (使用 mock 时钟)
  - 测试：序列号溢出触发等待下一毫秒 (使用 mock 时钟)
  - 测试：时钟回拨抛出 `ClockMovedBackwardsError` (使用 mock 时钟)
  - 测试：`generate_batch(n)` 返回 `n` 个唯一、递增的 ID
  - 测试：`generate_batch(0)` 抛出 `ValueError`
  - 测试：线程安全 — 并发生成不产生重复
  - 使用 `FakeClock` 类进行确定性测试

---

## 任务 5：ID 解析器

- [x] 5.1 实现 `ParsedID` dataclass
  - 文件：`unique-id-generator/src/unique_id/parser.py`
  - 不可变 dataclass，字段：`id_value`、`timestamp_ms`、`datacenter_id`、`machine_id`、`sequence`、`datetime_utc`
  - 自定义 `__repr__` 以提供可读输出

- [x] 5.2 实现 `IDParser.parse(id_value)`
  - 文件：`unique-id-generator/src/unique_id/parser.py`
  - 校验：id_value >= 0 且不超过 63 位
  - 提取序列号：`id_value & sequence_mask`
  - 提取 machine_id：`(id_value >> machine_shift) & machine_mask`
  - 提取 datacenter_id：`(id_value >> datacenter_shift) & datacenter_mask`
  - 提取时间戳偏移：`(id_value >> timestamp_shift) & timestamp_mask`
  - 转换时间戳：`offset + epoch_ms`
  - 转换为 datetime：`datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)`
  - 返回 `ParsedID` 实例

- [x] 5.3 编写解析器测试
  - 文件：`unique-id-generator/tests/test_parser.py`
  - 测试：解析一个已知 ID 并验证所有组件
  - 测试：先生成 ID 再解析 — 组件与生成器配置匹配
  - 测试：解析提取正确的时间戳 (可转换回 datetime)
  - 测试：解析提取正确的数据中心和机器 ID
  - 测试：解析提取正确的序列号
  - 测试：负数 ID 抛出 `ValueError`
  - 测试：超过 63 位的 ID 抛出 `ValueError`
  - 测试：自定义配置下解析器能正确提取

---

## 任务 6：备用策略

- [x] 6.1 实现 `IDGenerator` 抽象基类
  - 文件：`unique-id-generator/src/unique_id/strategies.py`
  - 抽象方法：`generate()`、`generate_batch(count)`

- [x] 6.2 实现 `UUIDGenerator`
  - 文件：`unique-id-generator/src/unique_id/strategies.py`
  - `generate()`：返回 `str(uuid.uuid4())`
  - `generate_batch(count)`：返回 UUID 字符串列表

- [x] 6.3 实现 `TimestampRandomGenerator`
  - 文件：`unique-id-generator/src/unique_id/strategies.py`
  - 构造函数：接受 `epoch_ms` (默认 2024-01-01)
  - `generate()`：`(timestamp_offset << 22) | random_22_bits`
  - `generate_batch(count)`：返回时间戳-随机 ID 列表
  - 使用 `random.SystemRandom()` 提供加密级随机数

- [x] 6.4 编写备用策略测试
  - 文件：`unique-id-generator/tests/test_strategies.py`
  - 测试：`UUIDGenerator.generate()` 返回有效的 UUID v4 字符串
  - 测试：`UUIDGenerator.generate_batch(10)` 返回 10 个唯一 UUID
  - 测试：`TimestampRandomGenerator.generate()` 返回正的 64 位整数
  - 测试：`TimestampRandomGenerator.generate_batch(10)` 返回 10 个 ID
  - 测试：时间戳-随机 ID 的时间戳分量随时间递增
  - 测试：两种生成器均实现 `IDGenerator` 接口

---

## 任务 7：单元测试

- [x] 7.1 实现 `FakeClock` 测试辅助类
  - 文件：`unique-id-generator/tests/test_snowflake.py` (或 `tests/conftest.py`)
  - 类 `FakeClock(Clock)`，`current_millis()` 返回值可控制
  - 方法：`set_millis(ms)`、`advance(ms)` 用于确定性测试

- [x] 7.2 运行完整测试套件并验证全部通过
  - 命令：`pytest tests/ -v`
  - 任务 2.3、3.4、4.5、5.3、6.4 中的所有测试均应通过
  - 验证无导入错误或缺失依赖

- [x] 7.3 添加边界条件测试
  - 文件：`unique-id-generator/tests/test_snowflake.py`
  - 测试：最大序列号 (默认配置下 4095) 之后的溢出
  - 测试：起始时间边界处的时间戳 (offset = 0)
  - 测试：最大数据中心和机器 ID (默认配置下 31, 31)
  - 测试：使用不同位布局的自定义配置生成有效 ID
  - 测试：两个配置相同但机器 ID 不同的生成器产生不重叠的 ID

---

## 任务 8：基于属性的测试

- [x] 8.1 编写唯一性属性测试
  - 文件：`unique-id-generator/tests/test_properties.py`
  - **属性**：同一个生成器生成 N 个 ID 得到 N 个唯一值
  - **属性**：来自两个不同 (dc, machine) 对的生成器不会产生重复
  - 使用 `hypothesis.given`，count 策略为 1-1000

- [x] 8.2 编写单调性属性测试
  - 文件：`unique-id-generator/tests/test_properties.py`
  - **属性**：同一个生成器的 ID 严格单调递增
  - **属性**：对任意两个 ID，如果 id_a 在 id_b 之前生成，则 id_a < id_b
  - 使用 `hypothesis.given`，批量大小采用策略生成

- [x] 8.3 编写时间顺序属性测试
  - 文件：`unique-id-generator/tests/test_properties.py`
  - **属性**：较晚生成 ID 的解析时间戳 >= 较早生成 ID 的解析时间戳
  - **属性**：在时间 T1 < T2 生成的 ID，其解析时间戳满足 T1_parsed <= T2_parsed
  - 结合 `FakeClock` 和 hypothesis 生成的时间戳

- [x] 8.4 编写位布局正确性属性测试
  - 文件：`unique-id-generator/tests/test_properties.py`
  - **属性**：对任意已生成 ID，解析后可恢复出原始 datacenter_id 和 machine_id
  - **属性**：对任意已生成 ID，其值可放入 63 位 (正数，< 2^63)
  - **属性**：对任意有效配置 (位总和为 63)，生成后再解析可正确往返
  - 使用 `hypothesis.given`，datacenter_id 策略为 0-31，machine_id 策略为 0-31

- [x] 8.5 编写解析器往返属性测试
  - 文件：`unique-id-generator/tests/test_properties.py`
  - **属性**：generate(config) → parse(id, config) → datacenter_id 与 config.datacenter_id 匹配
  - **属性**：generate(config) → parse(id, config) → machine_id 与 config.machine_id 匹配
  - **属性**：generate(config) → parse(id, config) → sequence 落在 [0, max_sequence] 范围内
  - **属性**：generate(config) → parse(id, config) → timestamp_ms >= config.epoch_ms

---

## 任务 9：示例和文档

- [x] 9.1 创建基本用法示例
  - 文件：`unique-id-generator/examples/basic_usage.py`
  - 演示：使用默认配置创建生成器
  - 演示：生成单个 ID
  - 演示：生成一批 ID
  - 演示：将 ID 解析回各个组件
  - 演示：使用 MonotonicClock
  - 包含解释每一步的注释
  - 可通过 `python examples/basic_usage.py` 直接运行

- [x] 9.2 创建多生成器示例
  - 文件：`unique-id-generator/examples/multi_generator.py`
  - 演示：为不同数据中心/机器创建生成器
  - 演示：从多个生成器生成 ID
  - 演示：验证跨生成器的唯一性
  - 演示：解析 ID 以识别来源数据中心/机器
  - 演示：自定义位布局配置
  - 包含解释分布式部署场景的注释

- [x] 9.3 创建 `README.md`
  - 文件：`unique-id-generator/README.md`
  - 项目概述和 Snowflake 算法说明
  - 位布局示意图 (64 位结构的可视化表示)
  - 安装说明 (`pip install -e ".[dev]"`)
  - 快速入门指南 (生成、解析、配置)
  - 配置参考 (所有 `SnowflakeConfig` 字段)
  - API 参考 (SnowflakeGenerator、IDParser、备用策略)
  - 设计决策与权衡
  - 对比表：Snowflake vs UUID v4 vs timestamp-random
  - 线程安全说明
  - 时钟处理和 NTP 注意事项

- [ ] 9.4 将文档复制到 docs/ 目录
  - 文件：`unique-id-generator/docs/requirements.md` — 从 `.kiro/specs/unique-id-generator/requirements.md` 复制
  - 文件：`unique-id-generator/docs/design.md` — 从 `.kiro/specs/unique-id-generator/design.md` 复制
  - 文件：`unique-id-generator/docs/tasks.md` — 从 `.kiro/specs/unique-id-generator/tasks.md` 复制
