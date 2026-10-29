# 需求文档

## 引言

一个 Python URL 短链库，实现自 System Design Interview 第九章《Design A URL Shortener》中的设计。该库提供可插拔哈希策略（哈希+冲突解决，以及 base-62 转换）实现的 URL 短链化、带可配置重定向类型（301/302）的 URL 重定向、带内存默认实现的存储抽象，以及基础统计/点击跟踪。核心为纯库，零运行时依赖；轻量 stdlib HTTP 演示展示完整短链化/重定向流程。

## 术语表

- **URL_Shortener**：协调 URL 短链化与重定向操作的核心服务
- **Hash_Strategy**：定义短链 URL 生成算法接口的抽象基类
- **Hash_Collision_Strategy**：对长 URL 应用 CRC32/MD5/SHA-1，取 base-62 编码结果的前 7 个字符，若发生冲突则追加预定义字符串重新哈希的哈希策略
- **Base62_Strategy**：使用字符集 `[0-9, a-z, A-Z]` 将唯一数值 ID 转换为 7 字符 base-62 编码字符串的哈希策略
- **Storage_Backend**：定义 URL 映射持久化接口的抽象基类
- **In_Memory_Storage**：使用 Python 字典的默认存储后端实现
- **Short_Code**：来自字母表 `[0-9, a-z, A-Z]` 的 7 字符字符串，唯一标识一个短链化后的 URL
- **Redirect_Type**：HTTP 重定向状态码的枚举（301 永久，302 临时）
- **Click_Record**：捕获单次重定向事件统计信息的数据结构
- **ID_Generator**：生成唯一数值 ID 的抽象接口，供 Base62_Strategy 使用

## 需求

### 需求 1：URL 短链化

**用户故事：** 作为一名开发者，我希望能将长 URL 缩短为紧凑的短码，以便存储和分享更短的网络资源引用。

#### 验收标准

1.1 WHEN 提交一个合法的长 URL，THE URL_Shortener SHALL 返回一个恰好 7 个字符的 Short_Code，来自字母表 `[0-9, a-z, A-Z]`。

1.2 WHEN 提交长 URL 时指定了自定义域名，THE URL_Shortener SHALL 通过将自定义域名与生成的 Short_Code 组合来构造短链 URL。

1.3 WHEN 再次提交一个已经短链化的长 URL，THE URL_Shortener SHALL 返回该长 URL 已有的 Short_Code。

1.4 IF 提交了一个无效的 URL（空字符串、缺少协议、或格式错误），THEN THE URL_Shortener SHALL 抛出带有描述性消息的 URLValidationError。

1.5 THE URL_Shortener SHALL 在创建时接受一个可选的重定向类型参数（301 或 302），默认 302（临时重定向）。

1.6 WHEN 提交长 URL 时未指定重定向类型，THE URL_Shortener SHALL 以 302 重定向类型存储该映射。

### 需求 2：URL 重定向

**用户故事：** 作为一名开发者，我希望能将短码解析回原始长 URL，以便将用户重定向到正确的目标。

#### 验收标准

2.1 WHEN 提供一个合法的 Short_Code，THE URL_Shortener SHALL 返回原始长 URL 以及该映射所配置的 Redirect_Type。

2.2 IF 提供了一个不存在于存储中的 Short_Code，THEN THE URL_Shortener SHALL 抛出 ShortCodeNotFoundError。

2.3 WHEN 解析 Short_Code 时，THE URL_Shortener SHALL 为统计目的记录一个 Click_Record。

2.4 THE URL_Shortener SHALL 支持每个 URL 映射所存储的两种重定向类型：301（永久）与 302（临时）。

### 需求 3：哈希+冲突解决策略

**用户故事：** 作为一名开发者，我希望有一种能处理冲突的基于哈希的短链策略，以便无需唯一 ID 生成器即可生成短码。

#### 验收标准

3.1 WHEN 提供长 URL，THE Hash_Collision_Strategy SHALL 使用知名哈希函数（CRC32、MD5 或 SHA-1）计算 URL 的哈希值。

3.2 THE Hash_Collision_Strategy SHALL 使用 base-62 编码对哈希输出进行编码，并取前 7 个字符作为候选 Short_Code。

3.3 IF 候选 Short_Code 已在存储中存在，且映射到另一个不同的长 URL，THEN THE Hash_Collision_Strategy SHALL 向原始 URL 追加一个预定义字符串并重新哈希。

3.4 THE Hash_Collision_Strategy SHALL 重复进行冲突解决，直到找到唯一的 Short_Code 或达到最大重试次数。

3.5 IF 在冲突解决过程中超过了最大重试次数，THEN THE Hash_Collision_Strategy SHALL 抛出 CollisionLimitExceededError。

### 需求 4：Base-62 转换策略

**用户故事：** 作为一名开发者，我希望有一种使用唯一 ID 的 base-62 转换策略，以便在保证唯一性的前提下生成短码，无需冲突处理。

#### 验收标准

4.1 WHEN 提供长 URL，THE Base62_Strategy SHALL 从已配置的 ID_Generator 获取一个唯一的数值 ID。

4.2 THE Base62_Strategy SHALL 使用字符集 `[0-9, a-z, A-Z]` 将数值 ID 转换为 base-62 字符串。

4.3 THE Base62_Strategy SHALL 对 base-62 字符串进行填充或截断，生成恰好 7 个字符的 Short_Code。

4.4 THE Base62_Strategy SHALL 支持任何符合 ID_Generator 接口的 ID_Generator 实现。

4.5 THE Base62_Strategy SHALL 提供一个默认的自增 ID_Generator，供无外部依赖的独立使用。

### 需求 5：存储抽象

**用户故事：** 作为一名开发者，我希望有一个可插拔的存储后端，以便在测试时切换为内存存储、生产环境切换为持久化存储。

#### 验收标准

5.1 THE Storage_Backend SHALL 定义一个抽象接口，包含以下方法：保存 URL 映射、按 Short_Code 检索长 URL、按长 URL 检索 Short_Code、检查 Short_Code 是否存在。

5.2 THE In_Memory_Storage SHALL 使用 Python 字典实现 Storage_Backend 接口。

5.3 THE In_Memory_Storage SHALL 维护双向查找：Short_Code → 长 URL，以及长 URL → Short_Code。

5.4 THE Storage_Backend SHALL 在每个 URL 映射旁存储 Redirect_Type。

5.5 THE Storage_Backend SHALL 相对于哈希策略保持无状态（任何策略都可使用任何存储后端）。

### 需求 6：统计与点击跟踪

**用户故事：** 作为一名开发者，我希望在重定向时进行基础的点击跟踪，以便分析 URL 使用模式。

#### 验收标准

6.1 WHEN Short_Code 被解析进行重定向时，THE URL_Shortener SHALL 记录一个包含 Short_Code、时间戳以及可选客户端标识符的 Click_Record。

6.2 THE URL_Shortener SHALL 提供一个方法，用于查询给定 Short_Code 的总点击次数。

6.3 THE URL_Shortener SHALL 提供一个方法，用于获取给定 Short_Code 的 Click_Record 列表。

6.4 IF 对一个没有任何点击记录的 Short_Code 请求点击跟踪，THEN THE URL_Shortener SHALL 返回计数为零以及空列表。

### 需求 7：自定义异常

**用户故事：** 作为一名开发者，我希望对错误条件有清晰、具体的异常，以便在应用代码中优雅地处理失败。

#### 验收标准

7.1 THE URL_Shortener SHALL 定义一个基类 URLShortenerError，所有自定义异常均继承自它。

7.2 THE URL_Shortener SHALL 为无效输入 URL 定义 URLValidationError。

7.3 THE URL_Shortener SHALL 为不存在短码的查找定义 ShortCodeNotFoundError。

7.4 THE URL_Shortener SHALL 为哈希冲突解决失败定义 CollisionLimitExceededError。

7.5 THE URL_Shortener SHALL 在所有自定义异常中包含描述性的错误消息。

### 需求 8：策略可插拔性

**用户故事：** 作为一名开发者，我希望能在运行时选择和切换哈希策略，以便挑选最适合当前场景的方案。

#### 验收标准

8.1 THE Hash_Strategy SHALL 定义一个抽象接口，包含接受长 URL 并返回 Short_Code 的方法。

8.2 THE URL_Shortener SHALL 在构造时接受任意 Hash_Strategy 实现。

8.3 WHEN 构造 URL_Shortener 时未指定策略，THE URL_Shortener SHALL 默认使用 Hash_Collision_Strategy。

8.4 THE Hash_Strategy 接口 SHALL 独立于 Storage_Backend 接口，通过依赖注入接收存储后端用于冲突检查。

### 需求 9：HTTP 演示服务器

**用户故事：** 作为一名开发者，我希望有一个使用 stdlib 的轻量 HTTP 演示，以便在不增加运行时依赖的情况下看到完整的短链化/重定向流程。

#### 验收标准

9.1 THE HTTP 演示 SHALL 仅使用 Python 标准库模块（`http.server`）。

9.2 THE HTTP 演示 SHALL 暴露一个端点，用于短链化长 URL 并返回短链 URL。

9.3 THE HTTP 演示 SHALL 暴露一个端点，接受 Short_Code 并对原始长 URL 执行 HTTP 重定向。

9.4 THE HTTP 演示 SHALL 将 URL_Shortener 库作为核心逻辑使用，不重复实现短链化或存储功能。

9.5 THE HTTP 演示 SHALL 返回合适的 HTTP 状态码：成功创建返回 201，重定向返回 301 或 302（按存储配置），未知短码返回 404。

## 非功能性需求

### NFR-1：项目结构

- **NFR-1.1**：项目 SHALL 使用 `pyproject.toml` 搭配 hatchling 构建系统。
- **NFR-1.2**：项目 SHALL 使用 src 布局（`src/url_shortener/`）。
- **NFR-1.3**：项目 SHALL 要求 Python >= 3.9。
- **NFR-1.4**：项目 SHALL 零运行时依赖（仅 stdlib）。
- **NFR-1.5**：项目 SHALL 包含开发依赖：pytest 和 hypothesis。
- **NFR-1.6**：项目 SHALL 包含 `docs/` 目录用于文档。
- **NFR-1.7**：项目 SHALL 包含 `examples/` 目录，放置 HTTP 演示服务器。
- **NFR-1.8**：项目 SHALL 提供干净的 `__init__.py`，包含公共 API 导出。

### NFR-2：代码质量

- **NFR-2.1**：项目 SHALL 在所有公共接口上使用类型提示。
- **NFR-2.2**：项目 SHALL 包含使用 pytest 的单元测试。
- **NFR-2.3**：项目 SHALL 包含使用 Hypothesis 进行哈希策略正确性验证的基于属性的测试。
- **NFR-2.4**：项目 SHALL 为所有可插拔策略接口使用抽象基类（ABC）。
- **NFR-2.5**：项目 SHALL 定义一个自定义异常模块，包含所有错误类型。

### NFR-3：性能

- **NFR-3.1**：对内存存储后端，单次 URL 短链化 SHALL 在 10 毫秒内完成。
- **NFR-3.2**：对内存存储后端，URL 重定向解析 SHALL 在 1 毫秒内完成。
- **NFR-3.3**：Hash_Collision_Strategy SHALL 默认在 10 次重试尝试内解决冲突。

### NFR-4：短码属性

- **NFR-4.1**：短码 SHALL 恰好 7 个字符长度。
- **NFR-4.2**：短码 SHALL 仅使用集合 `[0-9, a-z, A-Z]` 中的字符（共 62 个字符）。
- **NFR-4.3**：7 字符 base-62 空间 SHALL 支持约 3.5 万亿种唯一短码（62^7）。
