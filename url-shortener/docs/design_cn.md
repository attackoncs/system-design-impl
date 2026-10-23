# 设计文档：URL 短链服务

> 第 09 章已补充 SQLite 持久化、LRU 缓存及 WSGI HTTP 入口；保存映射改为拒绝覆盖。使用方法和运行边界见[章节核对记录](chapter09-review.md)。

## 架构概述

URL 短链库采用策略模式（strategy pattern），支持可插拔的哈希策略与存储后端。核心协调器（`URLShortener`）将短码生成委托给 `HashStrategy`，将持久化委托给 `StorageBackend`，实现关注点的清晰分离。

```
┌─────────────────────────────────────────────────┐
│           Public API                             │
│   (URLShortener orchestrator)                   │
├─────────────────────────────────────────────────┤
│       Hash Strategy Layer                        │
│   (HashCollisionStrategy, Base62Strategy)        │
├─────────────────────────────────────────────────┤
│       Storage Layer                              │
│   (StorageBackend ABC + InMemoryStorage)         │
├─────────────────────────────────────────────────┤
│       Models & Exceptions                        │
│   (URLMapping, ClickRecord, RedirectType, etc.)  │
└─────────────────────────────────────────────────┘
```

## 项目结构

```
url-shortener/
├── pyproject.toml
├── README.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── src/
│   └── url_shortener/
│       ├── __init__.py          # 公共 API 导出
│       ├── exceptions.py        # 自定义异常层级
│       ├── models.py            # 数据模型（ClickRecord, URLMapping, RedirectType）
│       ├── storage.py           # 存储抽象（ABC + InMemoryStorage）
│       ├── strategies.py        # 哈希策略（ABC + 多种实现）
│       └── shortener.py         # 主 URLShortener 协调器
├── tests/
│   ├── __init__.py
│   ├── test_exceptions.py       # 异常单元测试
│   ├── test_models.py           # 数据模型单元测试
│   ├── test_storage.py          # 存储层单元测试
│   ├── test_strategies.py       # 策略单元测试
│   ├── test_shortener.py        # 协调器单元测试
│   └── test_properties.py       # 基于属性的测试（Hypothesis）
└── examples/
    └── demo_server.py           # 基于 stdlib http.server 的 HTTP 演示
```

## 组件设计

### 1. 自定义异常（`exceptions.py`）

```python
class URLShortenerError(Exception):
    """所有 URL 短链错误的基类异常。"""
    pass


class URLValidationError(URLShortenerError):
    """当提交无效 URL 进行短链化时抛出。"""
    pass


class ShortCodeNotFoundError(URLShortenerError):
    """当短码不存在于存储中时抛出。"""
    pass


class CollisionLimitExceededError(URLShortenerError):
    """当哈希冲突解决超过最大重试次数时抛出。"""
    pass
```

### 2. 数据模型（`models.py`）

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Optional


class RedirectType(IntEnum):
    """HTTP 重定向状态码。"""
    PERMANENT = 301
    TEMPORARY = 302


@dataclass(frozen=True)
class URLMapping:
    """短码与长 URL 之间的映射。"""
    short_code: str
    long_url: str
    redirect_type: RedirectType = RedirectType.TEMPORARY
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ClickRecord:
    """单次点击/重定向事件，用于统计分析。"""
    short_code: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    client_id: Optional[str] = None
```

### 3. 存储抽象（`storage.py`）

```python
from abc import ABC, abstractmethod
from typing import Optional

from url_shortener.models import URLMapping, RedirectType


class StorageBackend(ABC):
    """URL 映射持久化的抽象接口。"""

    @abstractmethod
    def save(self, mapping: URLMapping) -> None:
        """将 URL 映射保存到存储。

        Args:
            mapping: 要持久化的 URLMapping 对象。
        """
        ...

    @abstractmethod
    def get_by_short_code(self, short_code: str) -> Optional[URLMapping]:
        """通过短码检索 URL 映射。

        Args:
            short_code: 7 字符短码。

        Returns:
            若找到返回 URLMapping，否则返回 None。
        """
        ...

    @abstractmethod
    def get_by_long_url(self, long_url: str) -> Optional[URLMapping]:
        """通过原始长 URL 检索 URL 映射。

        Args:
            long_url: 原始长 URL。

        Returns:
            若找到返回 URLMapping，否则返回 None。
        """
        ...

    @abstractmethod
    def exists(self, short_code: str) -> bool:
        """检查短码是否存在于存储中。

        Args:
            short_code: 7 字符短码。

        Returns:
            存在返回 True，否则返回 False。
        """
        ...


class InMemoryStorage(StorageBackend):
    """基于 Python 字典的内存存储后端。

    维护双向索引，两端访问均为 O(1)。
    """

    def __init__(self) -> None:
        self._by_short_code: dict[str, URLMapping] = {}
        self._by_long_url: dict[str, URLMapping] = {}

    def save(self, mapping: URLMapping) -> None:
        """保存映射，同时建立双向索引。"""
        self._by_short_code[mapping.short_code] = mapping
        self._by_long_url[mapping.long_url] = mapping

    def get_by_short_code(self, short_code: str) -> Optional[URLMapping]:
        """O(1) 按短码查找。"""
        return self._by_short_code.get(short_code)

    def get_by_long_url(self, long_url: str) -> Optional[URLMapping]:
        """O(1) 按长 URL 查找。"""
        return self._by_long_url.get(long_url)

    def exists(self, short_code: str) -> bool:
        """O(1) 存在性检查。"""
        return short_code in self._by_short_code
```

### 4. 哈希策略（`strategies.py`）

```python
from abc import ABC, abstractmethod
from typing import Protocol
import hashlib
import struct

from url_shortener.storage import StorageBackend
from url_shortener.exceptions import CollisionLimitExceededError

# Base-62 字符集：0-9, a-z, A-Z
BASE62_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
SHORT_CODE_LENGTH = 7


class HashStrategy(ABC):
    """短码生成策略的抽象接口。"""

    @abstractmethod
    def generate(self, long_url: str, storage: StorageBackend) -> str:
        """为给定的长 URL 生成短码。

        Args:
            long_url: 要短链化的 URL。
            storage: 用于冲突检测的存储后端。

        Returns:
            由 base-62 字母表组成的 7 字符短码。
        """
        ...


class IDGenerator(Protocol):
    """唯一数值 ID 生成协议。"""

    def next_id(self) -> int:
        """返回下一个唯一数值 ID。"""
        ...


class AutoIncrementIDGenerator:
    """默认自增 ID 生成器。

    非线程安全；适合单线程使用和测试场景。
    """

    def __init__(self, start: int = 1) -> None:
        self._counter = start

    def next_id(self) -> int:
        """返回下一个顺序 ID。"""
        current = self._counter
        self._counter += 1
        return current


def encode_base62(number: int) -> str:
    """将非负整数编码为 base-62 字符串。

    Args:
        number: 要编码的非负整数。

    Returns:
        Base-62 编码后的字符串（长度可变）。
    """
    if number == 0:
        return BASE62_CHARS[0]

    result = []
    while number > 0:
        number, remainder = divmod(number, 62)
        result.append(BASE62_CHARS[remainder])
    return "".join(reversed(result))


def decode_base62(encoded: str) -> int:
    """将 base-62 字符串解码回整数。

    Args:
        encoded: Base-62 编码字符串。

    Returns:
        解码后的非负整数。

    Raises:
        ValueError: 字符串包含无效字符时抛出。
    """
    result = 0
    for char in encoded:
        index = BASE62_CHARS.index(char)
        if index == -1:
            raise ValueError(f"无效的 base-62 字符：{char}")
        result = result * 62 + index
    return result


class HashCollisionStrategy(HashStrategy):
    """带冲突解决的哈希策略。

    对长 URL 应用 CRC32，将结果以 base-62 编码，取前 7 个字符。
    若发生冲突，则追加预定义字符串并重新哈希。
    """

    DEFAULT_MAX_RETRIES = 10
    COLLISION_SUFFIX = "~rehash"

    def __init__(self, max_retries: int = DEFAULT_MAX_RETRIES) -> None:
        self._max_retries = max_retries

    def generate(self, long_url: str, storage: StorageBackend) -> str:
        """通过冲突解决生成短码。

        Args:
            long_url: 要短链化的 URL。
            storage: 用于冲突检测的存储后端。

        Returns:
            唯一的 7 字符短码。

        Raises:
            CollisionLimitExceededError: 超过最大重试次数时抛出。
        """
        url_to_hash = long_url

        for attempt in range(self._max_retries + 1):
            candidate = self._compute_code(url_to_hash)

            # 检查此码是否空闲或已映射到我们的 URL
            existing = storage.get_by_short_code(candidate)
            if existing is None or existing.long_url == long_url:
                return candidate

            # 发生冲突：追加后缀后重试
            url_to_hash = url_to_hash + self.COLLISION_SUFFIX

        raise CollisionLimitExceededError(
            f"为 '{long_url}' 生成唯一短码失败，"
            f"已重试 {self._max_retries} 次"
        )

    def _compute_code(self, text: str) -> str:
        """使用 CRC32 从文本计算 7 字符 base-62 码。"""
        # 使用 CRC32 以获得速度；产生 32 位无符号整数
        crc = struct.unpack("I", struct.pack("i", hash_crc32(text)))[0]
        # 编码为 base-62，填充或截断为 7 字符
        encoded = encode_base62(crc)
        return encoded[:SHORT_CODE_LENGTH].ljust(SHORT_CODE_LENGTH, "0")


def hash_crc32(text: str) -> int:
    """计算字符串的 CRC32 哈希，返回有符号 32 位整数。"""
    import zlib
    return zlib.crc32(text.encode("utf-8"))


class Base62Strategy(HashStrategy):
    """利用唯一数值 ID 的 base-62 转换策略。

    将唯一 ID 转换为 base-62，产出保证唯一的短码，无需冲突处理。
    """

    def __init__(self, id_generator: IDGenerator | None = None) -> None:
        self._id_generator = id_generator or AutoIncrementIDGenerator()

    def generate(self, long_url: str, storage: StorageBackend) -> str:
        """从唯一数值 ID 生成短码。

        Args:
            long_url: 要短链化的 URL（仅用于去重检查）。
            storage: 存储后端（仅用于去重检查）。

        Returns:
            7 字符 base-62 短码。
        """
        unique_id = self._id_generator.next_id()
        encoded = encode_base62(unique_id)
        # 左侧补零至 7 字符
        return encoded.rjust(SHORT_CODE_LENGTH, "0")
```

### 5. URL 短链协调器（`shortener.py`）

```python
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from url_shortener.exceptions import (
    ShortCodeNotFoundError,
    URLValidationError,
)
from url_shortener.models import ClickRecord, RedirectType, URLMapping
from url_shortener.storage import InMemoryStorage, StorageBackend
from url_shortener.strategies import HashCollisionStrategy, HashStrategy


class URLShortener:
    """URL 短链化与重定向的主协调器。

    在哈希策略、存储后端和点击跟踪之间进行协调，
    提供完整的短链化/重定向流程。
    """

    def __init__(
        self,
        strategy: Optional[HashStrategy] = None,
        storage: Optional[StorageBackend] = None,
        default_redirect_type: RedirectType = RedirectType.TEMPORARY,
        domain: str = "http://short.url",
    ) -> None:
        """初始化 URL 短链器。

        Args:
            strategy: 用于短码生成的哈希策略。默认 HashCollisionStrategy。
            storage: 用于持久化的存储后端。默认 InMemoryStorage。
            default_redirect_type: 新映射的默认重定向类型。默认 302。
            domain: 用于构造短链 URL 的自定义域名。
        """
        self._strategy = strategy or HashCollisionStrategy()
        self._storage = storage or InMemoryStorage()
        self._default_redirect_type = default_redirect_type
        self._domain = domain.rstrip("/")
        self._clicks: dict[str, list[ClickRecord]] = {}

    def shorten(
        self,
        long_url: str,
        redirect_type: Optional[RedirectType] = None,
    ) -> str:
        """将长 URL 短链化为短码。

        Args:
            long_url: 要短链化的 URL。
            redirect_type: 覆盖此映射的重定向类型。

        Returns:
            完整的短链 URL（domain + "/" + short_code）。

        Raises:
            URLValidationError: URL 无效时抛出。
        """
        self._validate_url(long_url)

        # 检查是否已短链化（幂等性）
        existing = self._storage.get_by_long_url(long_url)
        if existing is not None:
            return f"{self._domain}/{existing.short_code}"

        # 通过策略生成短码
        short_code = self._strategy.generate(long_url, self._storage)

        # 存储映射
        mapping = URLMapping(
            short_code=short_code,
            long_url=long_url,
            redirect_type=redirect_type or self._default_redirect_type,
        )
        self._storage.save(mapping)

        return f"{self._domain}/{short_code}"

    def resolve(
        self,
        short_code: str,
        client_id: Optional[str] = None,
    ) -> tuple[str, RedirectType]:
        """将短码解析回原始长 URL。

        Args:
            short_code: 要解析的 7 字符短码。
            client_id: 可选的客户端标识，用于统计。

        Returns:
            元组 (long_url, redirect_type)。

        Raises:
            ShortCodeNotFoundError: 短码不存在时抛出。
        """
        mapping = self._storage.get_by_short_code(short_code)
        if mapping is None:
            raise ShortCodeNotFoundError(
                f"短码 '{short_code}' 未找到"
            )

        # 记录点击用于统计
        click = ClickRecord(
            short_code=short_code,
            timestamp=datetime.now(timezone.utc),
            client_id=client_id,
        )
        self._clicks.setdefault(short_code, []).append(click)

        return mapping.long_url, mapping.redirect_type

    def get_click_count(self, short_code: str) -> int:
        """获取短码的总点击次数。

        Args:
            short_code: 要查询的短码。

        Returns:
            总点击次数（若无记录则为 0）。
        """
        return len(self._clicks.get(short_code, []))

    def get_click_records(self, short_code: str) -> list[ClickRecord]:
        """获取短码的所有点击记录。

        Args:
            short_code: 要查询的短码。

        Returns:
            ClickRecord 对象列表（若无则为空列表）。
        """
        return list(self._clicks.get(short_code, []))

    def _validate_url(self, url: str) -> None:
        """验证 URL 格式是否良好。

        Args:
            url: 要验证的 URL。

        Raises:
            URLValidationError: URL 无效时抛出。
        """
        if not url or not url.strip():
            raise URLValidationError("URL 不能为空")

        parsed = urlparse(url)
        if not parsed.scheme:
            raise URLValidationError(
                f"URL 缺少协议（http/https）：'{url}'"
            )
        if not parsed.netloc:
            raise URLValidationError(
                f"URL 缺少网络位置：'{url}'"
            )
```

### 6. 公共 API（`__init__.py`）

```python
"""URL Shortener —— 可插拔的 URL 短链化库。"""

from url_shortener.exceptions import (
    CollisionLimitExceededError,
    ShortCodeNotFoundError,
    URLShortenerError,
    URLValidationError,
)
from url_shortener.models import ClickRecord, RedirectType, URLMapping
from url_shortener.shortener import URLShortener
from url_shortener.storage import InMemoryStorage, StorageBackend
from url_shortener.strategies import (
    AutoIncrementIDGenerator,
    Base62Strategy,
    HashCollisionStrategy,
    HashStrategy,
    IDGenerator,
    encode_base62,
    decode_base62,
)

__all__ = [
    # Core
    "URLShortener",
    # Models
    "ClickRecord",
    "RedirectType",
    "URLMapping",
    # Storage
    "InMemoryStorage",
    "StorageBackend",
    # Strategies
    "AutoIncrementIDGenerator",
    "Base62Strategy",
    "HashCollisionStrategy",
    "HashStrategy",
    "IDGenerator",
    "encode_base62",
    "decode_base62",
    # Exceptions
    "CollisionLimitExceededError",
    "ShortCodeNotFoundError",
    "URLShortenerError",
    "URLValidationError",
]
```

### 7. HTTP 演示服务器（`examples/demo_server.py`）

```python
"""仅使用 Python 标准库的轻量 HTTP 演示服务器。

端点：
    POST /shorten         - Body: {"url": "...", "redirect_type": 301|302}
                            Returns: 201 with {"short_url": "..."}
    GET  /<short_code>    - Redirects (301/302) to the original URL
    GET  /stats/<code>    - Returns click stats as JSON

Usage:
    python -m examples.demo_server --port 8000
"""

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import urlparse

from url_shortener import URLShortener, RedirectType, ShortCodeNotFoundError


class URLShortenerHandler(BaseHTTPRequestHandler):
    """URL 短链演示的 HTTP 请求处理器。"""

    shortener: URLShortener  # 类级共享实例

    def do_POST(self) -> None:
        """处理 POST /shorten 请求。"""
        if self.path != "/shorten":
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return

        long_url = data.get("url", "")
        redirect_type_code = data.get("redirect_type", 302)
        redirect_type = RedirectType(redirect_type_code)

        try:
            short_url = self.shortener.shorten(long_url, redirect_type)
        except Exception as e:
            self._send_error(HTTPStatus.BAD_REQUEST, str(e))
            return

        self._send_json(HTTPStatus.CREATED, {"short_url": short_url})

    def do_GET(self) -> None:
        """处理 GET /<short_code> 和 GET /stats/<code> 请求。"""
        path = self.path.lstrip("/")

        if path.startswith("stats/"):
            short_code = path[len("stats/"):]
            count = self.shortener.get_click_count(short_code)
            self._send_json(HTTPStatus.OK, {
                "short_code": short_code,
                "click_count": count,
            })
            return

        # 作为短码重定向处理
        short_code = path
        try:
            long_url, redirect_type = self.shortener.resolve(short_code)
        except ShortCodeNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "Short code not found")
            return

        self.send_response(redirect_type.value)
        self.send_header("Location", long_url)
        self.end_headers()

    def _send_json(self, status: HTTPStatus, data: dict) -> None:
        """发送 JSON 响应。"""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        """发送错误 JSON 响应。"""
        self._send_json(status, {"error": message})


def run_server(port: int = 8000, domain: str = "http://localhost:8000") -> None:
    """启动演示 HTTP 服务器。"""
    shortener = URLShortener(domain=domain)
    URLShortenerHandler.shortener = shortener

    server = HTTPServer(("", port), URLShortenerHandler)
    print(f"URL Shortener demo running on http://localhost:{port}")
    print("Endpoints:")
    print(f"  POST /shorten  - Shorten a URL")
    print(f"  GET /<code>    - Redirect to original URL")
    print(f"  GET /stats/<code> - View click stats")
    server.serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="URL Shortener Demo Server")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    run_server(port=args.port, domain=f"http://localhost:{args.port}")
```

## 设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 策略模式 | 基于 ABC | 哈希算法清晰分离；易于添加新策略 |
| 默认策略 | HashCollisionStrategy | 无需外部 ID 生成器；可独立工作 |
| 哈希函数 | CRC32 | 快速、stdlib 可用；对 7 字符码足够 |
| 冲突解决 | 追加后缀 + 重新哈希 | 简单、确定性；符合 System Design Interview 方法论 |
| 最大重试次数 | 10 | 实用限制；62^7 空间使耗尽几乎不可能 |
| Base-62 字符集 | `[0-9, a-z, A-Z]` | 标准排序；62 字符 × 7 位 = 约 3.5 万亿种编码 |
| 存储接口 | ABC 加 4 个方法 | 最小接口面；双向查找实现幂等性 |
| 内存存储 | 双字典 | 两端 O(1)；测试/演示场景简单快速 |
| 点击跟踪 | 每码一个内存列表 | 简单统计；生产环境应使用持久化存储 |
| URL 校验 | `urllib.parse.urlparse` | stdlib；检查协议与网络位置是否存在 |
| 数据模型 | 冻结 dataclass | 不可变值对象；可哈希且安全 |
| ID 生成器 | Protocol（结构类型） | 最大灵活性；任何带有 `next_id()` 的可调用对象都可用 |
| 演示服务器 | `http.server` | 零依赖；展示完整流程 |
| 重定向默认值 | 302（临时） | 更安全的默认值；支持统计与 URL 更新 |

## 错误处理

| 错误条件 | 异常 | 何时抛出 |
|----------|------|----------|
| URL 为空/格式错误 | `URLValidationError` | `shorten()` 输入无效 |
| 缺少协议 | `URLValidationError` | `shorten()` 无 http/https |
| 未知短码 | `ShortCodeNotFoundError` | `resolve()` 码不存在 |
| 冲突次数达上限 | `CollisionLimitExceededError` | `HashCollisionStrategy.generate()` 超过最大重试次数 |
| base-62 字符非法 | `ValueError` | `decode_base62()` 输入错误 |

所有自定义异常均继承自 `URLShortenerError`，便于统一捕获。

## 正确性属性（Correctness Properties）

*属性（Property）是指系统所有合法执行中应当成立的特征或行为——本质上是关于系统应做什么的形式化表述。属性作为可读规格说明与机器可验证正确性保证之间的桥梁。*

### 属性 1：短码格式不变性

*对于任何* 提交给任意哈希策略（HashCollisionStrategy 或 Base62Strategy）的合法长 URL，生成的短码 SHALL 恰好为 7 个字符，且仅包含字母表 `[0-9, a-z, A-Z]` 中的字符。

**校验：需求 1.1, 3.2, 4.2, 4.3**

### 属性 2：短链化/解析往返

*对于任何* 被成功短链化的合法长 URL，解析其短码 SHALL 返回原始长 URL（不变）。

**校验：需求 2.1, 5.3**

### 属性 3：短链化幂等性

*对于任何* 合法长 URL，多次短链化 SHALL 始终返回相同的短链 URL。

**校验：需求 1.3**

### 属性 4：无效 URL 拒绝

*对于任何* 空字符串、仅含空白字符、缺少 URL 协议或缺少网络位置的字符串，短链器 SHALL 抛出 `URLValidationError`。

**校验：需求 1.4**

### 属性 5：不存在的码报错

*对于任何* 未作为映射存储的 7 字符 base-62 字符串，解析它 SHALL 抛出 `ShortCodeNotFoundError`。

**校验：需求 2.2**

### 属性 6：重定向类型保留

*对于任何* 指定了重定向类型（301 或 302）进行短链化的合法长 URL，解析短码 SHALL 返回相同的重定向类型。未指定时，存储的类型 SHALL 为 302。

**校验：需求 1.5, 1.6, 2.4, 5.4**

### 属性 7：冲突解决唯一性

*对于任何* 一组使用 HashCollisionStrategy 短链化的、互不相同的长 URL，每个 SHALL 获得一个互不相同的短码（不会有两个不同 URL 映射到同一码）。

**校验：需求 3.3, 3.4**

### 属性 8：点击跟踪准确性

*对于任何* 被解析 N 次的短码，其点击次数 SHALL 等于 N，且点击记录列表 SHALL 恰好包含 N 条记录，每条记录具有正确的短码和有效时间戳。

**校验：需求 2.3, 6.1, 6.2, 6.3**

### 属性 9：Base-62 编码往返

*对于任何* 在可表示范围内（0 到 62^7 - 1）的非负整数，编码为 base-62 再解码回来 SHALL 得到原始整数。

**校验：需求 4.2**
