# URL 短链接服务

<!-- LEARNING_GUIDE_START -->
## 从这里开始读代码

[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。
<!-- LEARNING_GUIDE_END -->

> 第9章已对照原书、GitHub源码和 CodeGraph 核对；本轮补充持久 ID、并发读取与中文网址重定向。见[开源对照与验证](docs/open-source-review_cn.md)。

基于《System Design Interview》第 9 章"设计 URL 短链接系统"的 Python 实现。支持可插拔的哈希策略（哈希+冲突解决和 Base-62 转换）、存储抽象层（默认内存存储）、点击追踪分析，以及基于标准库的 HTTP 演示服务器。零运行时依赖。

## 功能特性

- **可插拔哈希策略** — 可选择哈希+冲突解决或 Base-62 转换
- **存储抽象层** — 默认内存或 SQLite 持久化；SQLite 同时提供事务内 ID 分配
- **点击追踪** — 内置分析功能，支持按 URL 统计点击次数和有界保留的时间戳记录
- **零运行时依赖** — 纯 Python 标准库实现；开发工具（pytest、hypothesis）为可选依赖
- **核心 API 类型注解** — 明确映射与重定向类型
- **Base62 短码** — 哈希码7位；ID码至少7位且随数值增长，不截断。`62**7` 是七位编码空间，不是 CRC32 的容量

## 架构

```text
HTTP → create_app → URLShortener.shorten / resolve
                        ├─ HashCollisionStrategy / Base62Strategy
                        ├─ InMemoryStorage / SQLiteStorage
                        │                     ├─ urls: unique code and URL
                        │                     └─ id_sequence: durable IDs
                        ├─ bounded instance LRU
                        └─ instance click records
```

### 组件职责

| 组件 | 职责 |
|------|------|
| `URLShortener` | 编排短链接生成、解析和点击追踪 |
| `HashCollisionStrategy` | CRC32 哈希 + 通过重新哈希解决冲突 |
| `Base62Strategy` | 唯一 ID → Base62；唯一性取决于共享发号器 |
| `StorageBackend` | URL 映射持久化的抽象接口 |
| `InMemoryStorage` | 默认后端，O(1) 双向查找 |
| `SQLiteStorage` | 不可变持久映射；`next_id()` 为协调的 Base62 写入者发号 |

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
print(short_url)

# 解析回原始 URL
code = short_url.rsplit("/", 1)[1]
long_url, redirect_type = shortener.resolve(code)
print(long_url)        # https://example.com/very/long/path
print(redirect_type)   # RedirectType.TEMPORARY (302)

# 查看点击分析
count = shortener.get_click_count(code)
records = shortener.get_click_records(code)
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
long_url, rtype = shortener.resolve(short_url.rsplit("/", 1)[1])
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
- 同一存储中的相同 URL 返回已有短码；候选哈希相同，最终短码仍取决于已占用的码

### Base-62 转换

将唯一数字 ID 转换为 Base62，左侧补零至至少7位；大于 `62**7-1` 的 ID 会产生更长短码。唯一性要求所有写入者协调发号；默认计数器仅适合单进程。

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

## SQLite 持久化与重启后发号

```python
from url_shortener import URLShortener, SQLiteStorage, Base62Strategy

storage = SQLiteStorage("urls.db")
shortener = URLShortener(
    storage=storage,
    strategy=Base62Strategy(id_generator=storage),
    domain="http://localhost:8000",
)
short_url = shortener.shorten("https://例子.中国/你好")
assert shortener.resolve(short_url.rsplit("/", 1)[1])[0] == "https://例子.中国/你好"
```

同一实例中，存储和发号器必须传入**同一个 storage 对象**；不同进程分别创建对象，指向同一个 SQLite 文件。分配和保存复用事务，相同 URL 不重复发号，失败回滚。已有映射先只读返回，未命中才申请写锁并重查。不要与不协调的其他生成器混用；旧外部 ID 数据需先迁移序列。

默认缓存容量1024，0禁用；映射不可变，SQLite 重启可恢复。点击总数是每条链接一个精确计数器；点击明细只在实例内存、未持久化，并按保留策略有界：默认每条链接保留最新1000条且不超过30天（`click_retention_max_records` / `click_retention_max_age`，传 `None` 关闭对应上限）。写入时均摊O(1)淘汰。库返回原始 URL，HTTP Location 则转换为 IDNA/百分号编码 URI；非法主机、端口、空白和控制字符被拒绝。

重定向保留显式空 `?` / `#` 和IPvFuture方括号。标准库IDNA若有损折叠Unicode域名（例如 `faß.de`），会要求显式ASCII ACE域名（如 `xn--fa-hia.de`），避免静默改变目标站点；详见[核对说明](docs/open-source-review_cn.md)。

## API 参考

### URLShortener

```python
URLShortener(
    strategy: HashStrategy | None = None,        # 默认: HashCollisionStrategy
    storage: StorageBackend | None = None,        # 默认: InMemoryStorage
    default_redirect_type: RedirectType = RedirectType.TEMPORARY,
    domain: str = "http://short.url",
    cache_capacity: int = 1024,
    click_retention_max_records: int | None = 1000,       # 每条链接上限；0 不保留明细
    click_retention_max_age: timedelta | None = timedelta(days=30),
)
```

**方法：**

| 方法 | 说明 |
|------|------|
| `shorten(long_url, redirect_type=None) -> str` | 缩短 URL，返回完整短链接 |
| `resolve(short_code, client_id=None) -> tuple[str, RedirectType]` | 解析短码为原始 URL |
| `get_click_count(short_code) -> int` | 获取短码的精确总点击次数（不受保留策略影响） |
| `get_click_records(short_code) -> list[ClickRecord]` | 获取保留窗口内的点击记录（由旧到新） |

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
| `StorageBackend` | ABC — 实现 `save`、`get_by_short_code`、`get_by_long_url`、`exists`；并发后端需原子实现 `get_or_create` |
| `InMemoryStorage` | 默认后端，双字典双向查找 |
| `SQLiteStorage(path)` | 持久双向映射；`next_id()` 可注入 Base62，复用同一对象的事务 |

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

使用标准库单线程 WSGI 服务器，复用库中的 `create_app`；生产并发请使用生产 WSGI 服务器。未传 `--database` 时使用内存与默认哈希策略，传入后使用持久 Base62 ID。

旧演示的 `/shorten`、`/stats` 已移除。创建接口遵循本章 WSGI 协议；统计使用库的 `get_click_count` / `get_click_records`。

```bash
python -m examples.demo_server --port 8000 --database urls.db
curl -X POST http://localhost:8000/api/v1/data/shorten -H "Content-Type: application/json" -d '{"longUrl":"https://example.com/long/path"}'
```

| Method | Path | Result |
|---|---|---|
| POST | `/api/v1/data/shorten` | 200, `{"shortURL":"http://localhost:8000/0000001"}` on a fresh Base62 database |
| GET | `/<code>` or `/api/v1/<code>` | 301/302 + Location;302 includes `Cache-Control: no-store` |

请求体限制16KiB，非法 URL/JSON 返回400，未知短码返回404。重定向类型在创建库实例或调用 `shorten` 时配置。不要猜测返回短码，使用创建响应。

## 测试

```bash
pip install -e ".[dev]"
pytest                              # 所有测试
pytest tests/test_shortener.py      # 编排器测试
pytest tests/test_strategies.py     # 策略测试
pytest tests/test_storage.py        # 存储测试
pytest tests/test_properties.py     # 属性测试（Hypothesis）
pytest --hypothesis-show-statistics # 详细 PBT 统计
python tools/verify_concurrency.py --output benchmarks/local.json
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
│       ├── sqlite_storage.py    # SQLite + durable IDs
│       ├── urls.py              # URL validation / ASCII URI
│       ├── http.py              # WSGI API
│       ├── strategies.py        # 哈希策略 (ABC + 实现)
│       └── shortener.py         # 主 URLShortener 编排器
├── tools/verify_concurrency.py  # Multiprocess + actual HTTP verification
├── benchmarks/                 # Raw verification output
├── tests/
│   ├── test_exceptions.py       # 异常单元测试
│   ├── test_models.py           # 数据模型单元测试
│   ├── test_storage.py          # 存储单元测试
│   ├── test_strategies.py       # 策略单元测试
│   ├── test_shortener.py        # 编排器单元测试
│   └── test_properties.py       # 属性测试（Hypothesis）
└── examples/
    └── demo_server.py           # 基于标准库 wsgiref 的 WSGI 演示
```

## License

MIT
