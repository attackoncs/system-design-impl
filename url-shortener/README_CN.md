# URL 短链接服务

基于《System Design Interview》第 9 章"设计 URL 短链接系统"的 Python 实现。支持可插拔的哈希策略（哈希+冲突解决和 Base-62 转换）、存储抽象层（默认内存存储）、点击追踪分析，以及基于标准库的 HTTP 演示服务器。零运行时依赖。

## 功能特性

- **可插拔哈希策略** — 可选择哈希+冲突解决或 Base-62 转换
- **存储抽象层** — 可在内存（默认）和自定义持久化后端之间切换
- **点击追踪** — 内置分析功能，支持按 URL 统计点击次数和时间戳记录
- **零运行时依赖** — 纯 Python 标准库实现；开发工具（pytest、hypothesis）为可选依赖
- **完整类型注解** — 所有公共接口均有类型提示
- **7 位短码** — Base-62 字母表 [0-9, a-z, A-Z]，支持约 3.5 万亿个唯一短码

## 架构

```
┌─────────────────────────────────────────────────┐
│           公共 API                               │
│   (URLShortener 编排器)                          │
├─────────────────────────────────────────────────┤
│       哈希策略层                                  │
│   (HashCollisionStrategy, Base62Strategy)        │
├─────────────────────────────────────────────────┤
│       存储层                                      │
│   (StorageBackend ABC + InMemoryStorage)         │
├─────────────────────────────────────────────────┤
│       模型与异常                                  │
│   (URLMapping, ClickRecord, RedirectType 等)     │
└─────────────────────────────────────────────────┘
```

### 组件职责

| 组件 | 职责 |
|------|------|
| `URLShortener` | 编排短链接生成、解析和点击追踪 |
| `HashCollisionStrategy` | CRC32 哈希 + 通过重新哈希解决冲突 |
| `Base62Strategy` | 唯一 ID → Base-62 转换，保证无冲突 |
| `StorageBackend` | URL 映射持久化的抽象接口 |
| `InMemoryStorage` | 默认后端，O(1) 双向查找 |

## 安装

```bash
cd url-shortener
pip install -e .
```

安装开发依赖（pytest + hypothesis）：

```bash
pip install -e ".[dev]"
```

### 环境要求

- Python >= 3.9

## 快速开始

```python
from url_shortener import URLShortener, Base62Strategy, RedirectType

# 使用默认设置创建短链接服务（哈希+冲突解决策略）
shortener = URLShortener(domain="https://short.io")

# 缩短 URL
short_url = shortener.shorten("https://example.com/very/long/path")
print(short_url)  # https://short.io/a3Bf92k

# 解析回原始 URL
long_url, redirect_type = shortener.resolve("a3Bf92k")
print(long_url)        # https://example.com/very/long/path
print(redirect_type)   # RedirectType.TEMPORARY (302)

# 查看点击分析
count = shortener.get_click_count("a3Bf92k")
records = shortener.get_click_records("a3Bf92k")
print(f"点击次数: {count}")

# 使用 Base-62 策略
shortener = URLShortener(
    strategy=Base62Strategy(),
    domain="https://short.io",
    default_redirect_type=RedirectType.PERMANENT,
)

short_url = shortener.shorten("https://example.com/another/page")
print(short_url)  # https://short.io/0000001
```

### 幂等性

对同一 URL 多次缩短会返回相同的短链接：

```python
url = "https://example.com/page"
result1 = shortener.shorten(url)
result2 = shortener.shorten(url)
assert result1 == result2
```

### 自定义重定向类型

```python
# 使用 301（永久）重定向存储
short_url = shortener.shorten(
    "https://example.com/moved",
    redirect_type=RedirectType.PERMANENT,
)

# 解析时返回存储的重定向类型
long_url, rtype = shortener.resolve("abc1234")
# rtype == RedirectType.PERMANENT (301)
```

## 策略

### 哈希+冲突解决（默认）

使用 CRC32 对长 URL 进行哈希，将结果编码为 Base-62，取前 7 个字符。如果发生冲突（不同 URL 映射到相同短码），则追加后缀并重新哈希，最多重试可配置次数。

```python
from url_shortener import URLShortener, HashCollisionStrategy

shortener = URLShortener(
    strategy=HashCollisionStrategy(max_retries=10),
)
```

**适用场景：**
- 无外部 ID 生成器
- 独立使用，无需协调
- URL 是主要输入（不需要顺序 ID）

**权衡：**
- 高负载下冲突解决会增加重试
- 相同 URL 始终产生相同短码（确定性）

### Base-62 转换

将唯一数字 ID 转换为 7 位 Base-62 字符串。保证唯一性，无需冲突处理。

```python
from url_shortener import URLShortener, Base62Strategy, AutoIncrementIDGenerator

shortener = URLShortener(
    strategy=Base62Strategy(id_generator=AutoIncrementIDGenerator(start=1000)),
)
```

**适用场景：**
- 有唯一 ID 来源（数据库序列、分布式 ID 生成器）
- 高吞吐量，零冲突开销
- 可接受可预测的顺序短码

**权衡：**
- 需要外部或内部 ID 生成器
- 顺序短码可能被猜测

### 自定义 ID 生成器

实现 `IDGenerator` 协议即可接入任何 ID 来源：

```python
from url_shortener import Base62Strategy, URLShortener

class SnowflakeIDGenerator:
    def next_id(self) -> int:
        # 你的分布式 ID 逻辑
        ...

shortener = URLShortener(strategy=Base62Strategy(id_generator=SnowflakeIDGenerator()))
```

## API 参考

### URLShortener

```python
URLShortener(
    strategy: HashStrategy | None = None,        # 默认: HashCollisionStrategy
    storage: StorageBackend | None = None,        # 默认: InMemoryStorage
    default_redirect_type: RedirectType = RedirectType.TEMPORARY,
    domain: str = "http://short.url",
)
```

**方法：**

| 方法 | 说明 |
|------|------|
| `shorten(long_url, redirect_type=None) -> str` | 缩短 URL，返回完整短链接 |
| `resolve(short_code, client_id=None) -> tuple[str, RedirectType]` | 解析短码为原始 URL |
| `get_click_count(short_code) -> int` | 获取短码的总点击次数 |
| `get_click_records(short_code) -> list[ClickRecord]` | 获取短码的所有点击记录 |

### 策略

| 类 | 说明 |
|----|------|
| `HashStrategy` | ABC — 实现 `generate(long_url, storage) -> str` |
| `HashCollisionStrategy(max_retries=10)` | CRC32 哈希 + 冲突解决 |
| `Base62Strategy(id_generator=None)` | 唯一 ID 到 Base-62 转换 |
| `AutoIncrementIDGenerator(start=1)` | 默认顺序 ID 生成器 |

### 存储

| 类 | 说明 |
|----|------|
| `StorageBackend` | ABC — 实现 `save`、`get_by_short_code`、`get_by_long_url`、`exists` |
| `InMemoryStorage` | 默认后端，双字典双向查找 |

### 模型

| 类 | 说明 |
|----|------|
| `RedirectType` | IntEnum: `PERMANENT` (301)、`TEMPORARY` (302) |
| `URLMapping` | 冻结数据类: `short_code`、`long_url`、`redirect_type`、`created_at` |
| `ClickRecord` | 冻结数据类: `short_code`、`timestamp`、`client_id`（可选） |

### 异常

| 异常 | 触发条件 |
|------|----------|
| `URLShortenerError` | 所有 URL 短链接错误的基类 |
| `URLValidationError` | 无效 URL（空值、缺少协议、格式错误） |
| `ShortCodeNotFoundError` | 短码在存储中未找到 |
| `CollisionLimitExceededError` | 哈希冲突重试次数耗尽 |

### 工具函数

| 函数 | 说明 |
|------|------|
| `encode_base62(number: int) -> str` | 将非负整数编码为 Base-62 字符串 |
| `decode_base62(encoded: str) -> int` | 将 Base-62 字符串解码为整数 |

## 演示服务器

基于 Python 标准库（`http.server`）的轻量级 HTTP 演示，展示完整的缩短/重定向流程。

### 启动服务器

```bash
python examples/demo_server.py --port 8000
```

### 接口

| 方法 | 路径 | 说明 | 状态码 |
|------|------|------|--------|
| POST | `/shorten` | 缩短 URL | 201 Created |
| GET | `/<short_code>` | 重定向到原始 URL | 301/302 Redirect |
| GET | `/stats/<code>` | 点击统计 | 200 OK |

### 使用示例

**缩短 URL：**

```bash
curl -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/long/path", "redirect_type": 302}'

# 响应: {"short_url": "http://localhost:8000/a3Bf92k"}
```

**重定向：**

```bash
curl -L http://localhost:8000/a3Bf92k
# 跟随重定向到 https://example.com/long/path
```

**查看统计：**

```bash
curl http://localhost:8000/stats/a3Bf92k
# 响应: {"short_code": "a3Bf92k", "click_count": 5}
```

## 测试

```bash
pip install -e ".[dev]"
pytest                              # 所有测试
pytest tests/test_shortener.py      # 编排器测试
pytest tests/test_strategies.py     # 策略测试
pytest tests/test_storage.py        # 存储测试
pytest tests/test_properties.py     # 属性测试（Hypothesis）
pytest --hypothesis-show-statistics # 详细 PBT 统计
```

### 测试覆盖

- **单元测试**：核心操作、边界情况、错误处理、幂等性
- **属性测试**：格式不变量、往返正确性、冲突唯一性、点击追踪准确性

## 项目结构

```
url-shortener/
├── pyproject.toml
├── README.md
├── README_CN.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── src/
│   └── url_shortener/
│       ├── __init__.py          # 公共 API 导出
│       ├── exceptions.py        # 自定义异常层次
│       ├── models.py            # 数据模型 (ClickRecord, URLMapping, RedirectType)
│       ├── storage.py           # 存储抽象 (ABC + InMemoryStorage)
│       ├── strategies.py        # 哈希策略 (ABC + 实现)
│       └── shortener.py         # 主 URLShortener 编排器
├── tests/
│   ├── test_exceptions.py       # 异常单元测试
│   ├── test_models.py           # 数据模型单元测试
│   ├── test_storage.py          # 存储单元测试
│   ├── test_strategies.py       # 策略单元测试
│   ├── test_shortener.py        # 编排器单元测试
│   └── test_properties.py       # 属性测试（Hypothesis）
└── examples/
    └── demo_server.py           # 基于标准库 http.server 的 HTTP 演示
```

## License

MIT
