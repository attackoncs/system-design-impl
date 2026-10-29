# 实施计划：URL 短链服务

## 概述

实现一个 Python URL 短链库，遵循策略模式，包含可插拔哈希策略（哈希+冲突解决、base-62 转换）、带内存默认实现的存储抽象、点击跟踪统计，以及基于 stdlib 的 HTTP 演示服务器。项目使用 src 布局，零运行时依赖。

## 任务清单

- [ ] 1. 项目脚手架与基础设施
  - [~] 1.1 创建带有 hatchling 构建系统的 `pyproject.toml`
    - 文件：`url-shortener/pyproject.toml`
    - Python >= 3.9，零运行时依赖
    - 开发依赖：`pytest`, `hypothesis`
    - 构建目标：`packages = ["src/url_shortener"]`
    - Pytest 配置：`testpaths = ["tests"]`, `pythonpath = ["src"]`
    - _需求来源：NFR-1.1, NFR-1.2, NFR-1.3, NFR-1.4, NFR-1.5_

  - [~] 1.2 创建目录结构与占位文件
    - 目录：`src/url_shortener/`, `tests/`, `examples/`, `docs/`
    - 为 `src/url_shortener/` 和 `tests/` 创建 `__init__.py`
    - _需求来源：NFR-1.6, NFR-1.7_

  - [~] 1.3 创建 `src/url_shortener/exceptions.py` 定义自定义异常层级
    - `URLShortenerError`（基类异常）
    - `URLValidationError`（无效输入 URL）
    - `ShortCodeNotFoundError`（不存在的短码）
    - `CollisionLimitExceededError`（哈希冲突解决失败）
    - 所有异常包含描述性错误消息
    - _需求来源：7.1, 7.2, 7.3, 7.4, 7.5_

  - [~] 1.4 创建 `src/url_shortener/models.py` 定义数据模型
    - `RedirectType` IntEnum：PERMANENT=301, TEMPORARY=302
    - `URLMapping` 冻结 dataclass：short_code, long_url, redirect_type, created_at
    - `ClickRecord` 冻结 dataclass：short_code, timestamp, client_id（可选）
    - 全程使用类型提示
    - _需求来源：1.5, 2.4, 6.1_

- [ ] 2. 存储层
  - [~] 2.1 在 `src/url_shortener/storage.py` 中实现 `StorageBackend` ABC 和 `InMemoryStorage`
    - 抽象方法：`save(mapping)`, `get_by_short_code(code)`, `get_by_long_url(url)`, `exists(code)`
    - `InMemoryStorage` 使用双字典实现双向查找
    - 两端访问均为 O(1)
    - 在每个映射旁存储 `RedirectType`
    - _需求来源：5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ]* 2.2 为存储层编写单元测试
    - 文件：`tests/test_storage.py`
    - 测试按短码保存和检索
    - 测试按长 URL 检索（双向查找）
    - 测试 `exists()` 对存在和不存在码的处理
    - 测试将重定向类型与映射一同存储
    - 测试覆盖行为
    - _需求来源：5.1, 5.2, 5.3, 5.4_

- [ ] 3. 哈希策略
  - [~] 3.1 在 `src/url_shortener/strategies.py` 中实现 base-62 编码工具和 `HashStrategy` ABC
    - `BASE62_CHARS` 常量，`SHORT_CODE_LENGTH = 7`
    - `encode_base62(number)` 和 `decode_base62(encoded)` 函数
    - `HashStrategy` ABC，包含 `generate(long_url, storage)` 方法
    - `IDGenerator` Protocol，包含 `next_id()` 方法
    - _需求来源：4.2, 8.1, 8.4_

  - [~] 3.2 实现 `HashCollisionStrategy`
    - 对 URL 做 CRC32 哈希，base-62 编码，取前 7 个字符
    - 冲突解决：追加后缀并重新哈希
    - 可配置最大重试次数（默认 10）
    - 耗尽时抛出 `CollisionLimitExceededError`
    - _需求来源：3.1, 3.2, 3.3, 3.4, 3.5_

  - [~] 3.3 实现 `Base62Strategy` 和 `AutoIncrementIDGenerator`
    - `AutoIncrementIDGenerator` 带可配置起始值
    - `Base62Strategy` 将唯一 ID 转换为 7 字符 base-62 码（前导零左填充）
    - 接受任何符合协议的 `IDGenerator`
    - _需求来源：4.1, 4.2, 4.3, 4.4, 4.5_

  - [ ]* 3.4 为策略编写单元测试
    - 文件：`tests/test_strategies.py`
    - 使用已知值测试 `encode_base62` / `decode_base62`
    - 测试 `HashCollisionStrategy` 生成 7 字符 base-62 码
    - 测试冲突解决追加后缀并重试
    - 测试超过最大重试后抛出 `CollisionLimitExceededError`
    - 测试 `Base62Strategy` 从顺序 ID 生成 7 字符码
    - 测试 `AutoIncrementIDGenerator` 生成顺序值
    - 测试自定义 `IDGenerator` 可被 `Base62Strategy` 使用
    - _需求来源：3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3, 4.4, 4.5_

- [~] 4. 检查点 —— 验证基础设施
  - 确保所有测试通过，有疑问时询问用户。

- [ ] 5. URL 短链协调器
  - [~] 5.1 在 `src/url_shortener/shortener.py` 中实现 `URLShortener` 类
    - 构造函数：可选策略（默认 HashCollisionStrategy）、存储（默认 InMemoryStorage）、redirect_type（默认 302）、domain
    - `shorten(long_url, redirect_type)`：校验 URL、检查幂等性、生成码、存储映射、返回完整短链 URL
    - `resolve(short_code, client_id)`：查找映射、记录点击、返回 (long_url, redirect_type)
    - `get_click_count(short_code)`：返回总点击数
    - `get_click_records(short_code)`：返回 ClickRecord 列表
    - URL 校验：检查非空、有协议、有网络位置
    - _需求来源：1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4, 6.1, 6.2, 6.3, 6.4, 8.2, 8.3_

  - [ ]* 5.2 为 URLShortener 编写单元测试
    - 文件：`tests/test_shortener.py`
    - 测试 shorten 返回带有域名前缀的合法短链 URL
    - 测试同一 URL 调用两次 shorten 返回相同结果（幂等性）
    - 测试使用自定义 redirect_type 的 shorten 正确存储
    - 测试使用无效 URL 的 shorten 抛出 URLValidationError
    - 测试 resolve 返回正确的长 URL 和重定向类型
    - 测试 resolve 未知码抛出 ShortCodeNotFoundError
    - 测试 resolve 记录点击统计
    - 测试 get_click_count 返回正确计数
    - 测试 get_click_records 返回正确记录
    - 测试默认策略是 HashCollisionStrategy
    - 测试接受自定义策略
    - _需求来源：1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 6.1, 6.2, 6.3, 6.4, 8.2, 8.3_

- [ ] 6. 公共 API 与组装
  - [~] 6.1 完成 `src/url_shortener/__init__.py`，包含所有公共导出
    - 导出所有公共类、函数和异常
    - 定义 `__all__` 列表以显式声明公共 API
    - _需求来源：NFR-1.8_

  - [ ]* 6.2 为异常模块编写单元测试
    - 文件：`tests/test_exceptions.py`
    - 测试所有异常继承自 `URLShortenerError`
    - 测试异常包含描述性消息
    - 测试异常层级正确
    - _需求来源：7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ]* 6.3 为数据模型编写单元测试
    - 文件：`tests/test_models.py`
    - 测试 `RedirectType` 枚举值（301, 302）
    - 测试 `URLMapping` 冻结 dataclass 的创建与默认值
    - 测试 `ClickRecord` 冻结 dataclass 的创建及可选 client_id
    - _需求来源：1.5, 2.4, 6.1_

- [~] 7. 检查点 —— 确保核心库可运行
  - 确保所有测试通过，有疑问时询问用户。

- [ ] 8. 基于属性的测试
  - [ ]* 8.1 为短码格式不变性编写属性测试
    - 文件：`tests/test_properties.py`
    - **属性 1：短码格式不变性**
    - 对任何合法 URL，两种策略均生成恰好 7 字符、来自 [0-9, a-z, A-Z] 的码
    - 使用 `hypothesis.given` 配合 URL 策略
    - **校验：需求 1.1, 3.2, 4.2, 4.3**

  - [ ]* 8.2 为短链化/解析往返编写属性测试
    - 文件：`tests/test_properties.py`
    - **属性 2：短链化/解析往返**
    - 对任何已短链化的合法 URL，解析其短码返回原始 URL
    - **校验：需求 2.1, 5.3**

  - [ ]* 8.3 为短链化幂等性编写属性测试
    - 文件：`tests/test_properties.py`
    - **属性 3：短链化幂等性**
    - 多次短链化同一 URL 返回相同短链 URL
    - **校验：需求 1.3**

  - [ ]* 8.4 为无效 URL 拒绝编写属性测试
    - 文件：`tests/test_properties.py`
    - **属性 4：无效 URL 拒绝**
    - 空字符串、纯空白、缺少协议、缺少网络位置均抛出 URLValidationError
    - **校验：需求 1.4**

  - [ ]* 8.5 为不存在的码报错编写属性测试
    - 文件：`tests/test_properties.py`
    - **属性 5：不存在的码报错**
    - 任何不在存储中的 7 字符 base-62 字符串在 resolve 时抛出 ShortCodeNotFoundError
    - **校验：需求 2.2**

  - [ ]* 8.6 为重定向类型保留编写属性测试
    - 文件：`tests/test_properties.py`
    - **属性 6：重定向类型保留**
    - 指定重定向类型的短链化在 resolve 时保留该类型；默认值为 302
    - **校验：需求 1.5, 1.6, 2.4, 5.4**

  - [ ]* 8.7 为冲突解决唯一性编写属性测试
    - 文件：`tests/test_properties.py`
    - **属性 7：冲突解决唯一性**
    - 使用 HashCollisionStrategy 短链化的不同 URL 获得不同的短码
    - **校验：需求 3.3, 3.4**

  - [ ]* 8.8 为点击跟踪准确性编写属性测试
    - 文件：`tests/test_properties.py`
    - **属性 8：点击跟踪准确性**
    - 解析一个短码 N 次得到 click_count == N 和 N 条点击记录
    - **校验：需求 2.3, 6.1, 6.2, 6.3**

  - [ ]* 8.9 为 base-62 编码往返编写属性测试
    - 文件：`tests/test_properties.py`
    - **属性 9：Base-62 编码往返**
    - 对 [0, 62^7 - 1] 范围内的任何整数，编码再解码返回原始值
    - **校验：需求 4.2**

- [ ] 9. HTTP 演示服务器与文档
  - [~] 9.1 使用 stdlib 创建 `examples/demo_server.py` HTTP 演示
    - 使用 `http.server.BaseHTTPRequestHandler` 和 `HTTPServer`
    - POST `/shorten` 端点：接受 JSON body，返回 201 和短链 URL
    - GET `/<short_code>` 端点：执行 301/302 重定向
    - GET `/stats/<code>` 端点：返回 JSON 格式点击统计
    - 未知短码返回 404
    - 使用 `URLShortener` 作为核心逻辑（不重复实现）
    - _需求来源：9.1, 9.2, 9.3, 9.4, 9.5_

  - [~] 9.2 创建带项目文档的 `README.md`
    - 项目概述与架构描述
    - 安装说明（`pip install -e ".[dev]"`）
    - 带代码示例的快速上手指南
    - 所有公共类的 API 参考
    - 策略对比（哈希+冲突 vs base-62）
    - 演示服务器使用说明
    - _需求来源：NFR-1.6_

  - [~] 9.3 将规格文档复制到 `docs/` 目录
    - 将 requirements.md、design.md、tasks.md 复制到 `url-shortener/docs/`
    - _需求来源：NFR-1.6_

- [~] 10. 最终检查点 —— 确保所有测试通过
  - 确保所有测试通过，有疑问时询问用户。

## 备注

- 标记有 `*` 的任务为可选，可跳过以加快 MVP 交付
- 每个任务引用特定需求以保证可追溯性
- 检查点确保增量验证
- 属性测试校验设计文档中的通用正确性属性
- 单元测试校验具体示例与边界情况
- 项目使用 Python 搭配 pytest + hypothesis（无需语言选择）
- 零运行时依赖 —— 生产代码仅使用 stdlib 模块

## 任务依赖图

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "1.4"] },
    { "id": 2, "tasks": ["2.1", "3.1"] },
    { "id": 3, "tasks": ["2.2", "3.2", "3.3"] },
    { "id": 4, "tasks": ["3.4", "5.1"] },
    { "id": 5, "tasks": ["5.2", "6.1"] },
    { "id": 6, "tasks": ["6.2", "6.3"] },
    { "id": 7, "tasks": ["8.1", "8.2", "8.3", "8.4", "8.5", "8.6", "8.7", "8.8", "8.9"] },
    { "id": 8, "tasks": ["9.1", "9.2"] },
    { "id": 9, "tasks": ["9.3"] }
  ]
}
```
