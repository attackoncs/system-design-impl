# 需求文档

## 引言

一个 Python 网络爬虫库，实现自《系统设计面试》第 10 章"Design A Web Crawler"中的设计。该库提供基于单进程、asyncio 的网络爬虫，包含 URL Frontier（Priority Queue 优先级前置队列 + Politeness Policy 礼貌性后置队列）、符合 Robots.txt 规范的 HTML 下载、基于 SHA-256 哈希的内容去重（Content Deduplication）、链接提取（Link Extractor）、URL 过滤（URL Filter）以及 URL 去重（URL Deduplication）。核心为纯库实现，零运行时依赖（仅使用标准库）；可配置的接口允许自定义 Fetcher、存储后端及内容处理钩子。

## 术语表

- **Crawler**：顶级编排器，协调整个爬取循环：URL 出队 → 下载 → 解析 → 提取链接 → 将新 URL 入队
- **URL_Frontier**：具有优先级感知与礼貌性强制的队列，管理待爬取 URL，结合前置队列（优先级）与后置队列（每主机礼貌性）
- **Front_Queue**：URL Frontier 内基于优先级的队列集合，决定 URL 被选中爬取的顺序（HIGH、MEDIUM、LOW）
- **Back_Queue**：URL Frontier 内按主机划分的队列集合，强制对同一主机的请求之间保持礼貌性延迟
- **HTML_Downloader**：负责通过 HTTP 下载网页内容的组件，遵守 Robots.txt 规则和速率限制
- **Robots_Checker**：解析并缓存 Robots.txt 文件，以确定某 URL 是否允许被爬取的组件
- **Content_Parser**：验证已下载 HTML 内容并提取文本/结构以供处理的组件
- **Content_Fingerprint**：页面内容的 SHA-256 哈希，用于检测重复页面（内容已见检查）
- **Link_Extractor**：解析 HTML 以从锚标签中提取超链接（href 属性）的组件
- **URL_Filter**：可配置组件，根据规则（扩展名黑名单、域名白/黑名单、最大深度）判断提取出的 URL 是否应加入 Frontier
- **URL_Seen_Store**：数据结构（内存集合），跟踪哪些 URL 已被访问或入队，以避免重复爬取
- **Crawl_Result**：表示单个 URL 爬取结果的数据结构（状态、内容、提取的链接、元数据）
- **Fetcher**：HTTP 抓取的抽象接口，允许自定义实现以用于测试或替代 HTTP 客户端
- **Content_Hook**：用于处理已爬取内容的回调接口（插件/钩子系统）

## 需求

### Requirement 1：爬取编排（Crawl Orchestration）

**用户故事：** 作为一名开发者，我希望有一个高层爬虫来协调整个爬取循环，这样我就可以通过提供种子 URL 和配置来爬取网站。

#### 验收标准

1.1 当提供种子 URL 时，Crawler 应将它们加入 URL_Frontier 并启动爬取循环。

1.2 Crawler 应反复从 URL_Frontier 中出队 URL、下载其内容、解析、提取链接、过滤，并将新 URL 入队，直到 Frontier 为空或满足停止条件。

1.3 Crawler 应支持可配置的最大爬取页数（max_pages），超过后爬取停止。

1.4 Crawler 应支持可配置的距种子 URL 的最大爬取深度，超过深度的 URL 不再入队。

1.5 Crawler 应支持使用 asyncio 的可配置最大并发下载数。

1.6 当 URL_Frontier 为空且所有进行中的下载已完成时，Crawler 应优雅终止。

1.7 Crawler 应提供一个异步的 `crawl()` 方法，返回所有成功爬取页面的 Crawl_Result 集合。

1.8 Crawler 应为每个成功下载并解析的页面调用已注册的 Content_Hook。

### Requirement 2：URL Frontier

**用户故事：** 作为一名开发者，我希望 URL Frontier 支持优先级与礼貌性，这样重要的 URL 会先被爬取，且主机不会被请求淹没。

#### 验收标准

2.1 URL_Frontier 应维护前置队列，将 URL 划分为 HIGH、MEDIUM、LOW 三个优先级。

2.2 当出队 URL 时，URL_Frontier 应在较低优先级的前置队列之前从较高优先级的前置队列中选择。

2.3 URL_Frontier 应维护后置队列，按主机（domain）对 URL 进行分组。

2.4 URL_Frontier 应强制执行可配置的每主机延迟（默认：1 秒），对同一主机的连续请求之间保持间隔。

2.5 当 URL 入队时，URL_Frontier 应根据其优先级分配到合适的前置队列，根据其主机分配到合适的后置队列。

2.6 当出队时，URL_Frontier 仅应返回距上次请求该主机的礼貌性延迟已过期的 URL。

2.7 URL_Frontier 应提供方法检查是否为空以及获取当前大小（待处理 URL 数）。

2.8 URL_Frontier 应支持添加带有关联优先级和爬取深度的 URL。

### Requirement 3：HTML 下载

**用户故事：** 作为一名开发者，我希望有一个通过 HTTP 抓取页面并遵守 Robots.txt 的 HTML 下载器，这样爬取既功能完善又合乎伦理。

#### 验收标准

3.1 HTML_Downloader 应通过配置的 Fetcher 接口使用 HTTP GET 请求下载网页。

3.2 HTML_Downloader 应使用 Python 标准库中的 `urllib.request` 作为默认 Fetcher 实现。

3.3 在下载 URL 之前，HTML_Downloader 应检查 Robots_Checker 以验证该 URL 是否被允许。

3.4 如果某个 URL 被 Robots.txt 禁止，则 HTML_Downloader 应跳过该 URL 并返回状态为 "disallowed" 的 Crawl_Result。

3.5 HTML_Downloader 应支持可配置的请求超时（默认：30 秒）。

3.6 HTML_Downloader 应在所有请求上设置可配置的 User-Agent 头（默认："WebCrawler/1.0"）。

3.7 如果发生 HTTP 错误（超时、连接错误、非 2xx 状态），则 HTML_Downloader 应返回状态为 "error" 并包含错误详情的 Crawl_Result。

3.8 HTML_Downloader 应接受任何符合 Fetcher 接口的实现，支持自定义 HTTP 客户端或用于测试的 Mock Fetcher。

### Requirement 4：Robots.txt 合规性

**用户故事：** 作为一名开发者，我希望爬虫遵守 Robots.txt 规则，这样它就能合规运行并避免爬取被禁止的路径。

#### 验收标准

4.1 Robots_Checker 应在首次向某主机发起请求之前下载并解析该主机的 Robots.txt。

4.2 Robots_Checker 应使用 Python 标准库中的 `urllib.robotparser.RobotFileParser` 来解析 Robots.txt 文件。

4.3 Robots_Checker 应按主机缓存已解析的 Robots.txt 结果，以避免重复下载。

4.4 当检查 URL 是否被允许时，Robots_Checker 应使用配置的 User-Agent 字符串来匹配规则。

4.5 如果 Robots.txt 无法下载（404、超时、错误），则 Robots_Checker 应假定该主机的所有路径都被允许。

4.6 Robots_Checker 应支持可配置的 Robots.txt 条目缓存 TTL（time-to-live，默认：3600 秒）。

### Requirement 5：内容解析与去重

**用户故事：** 作为一名开发者，我希望有内容解析与去重功能，这样爬虫可以处理有效的 HTML 并避免存储重复的页面内容。

#### 验收标准

5.1 Content_Parser 应验证已下载内容是否为 HTML（检查 Content-Type 头或进行内容嗅探）。

5.2 Content_Parser 应使用 Python 标准库中的 `html.parser.HTMLParser` 来解析 HTML 内容。

5.3 在下载页面之后，Crawler 应计算页面体的 Content_Fingerprint（SHA-256 哈希）。

5.4 如果 Content_Fingerprint 之前已被见过（存在于内容已见存储中），则 Crawler 应跳过该页面的进一步处理，并将 Crawl_Result 标记为 "duplicate"。

5.5 如果 Content_Fingerprint 是新的，则 Crawler 应将其加入内容已见存储，并继续进行链接提取。

5.6 内容已见存储应默认使用内存中的 SHA-256 十六进制摘要集合作为实现。

### Requirement 6：链接提取

**用户故事：** 作为一名开发者，我希望能从已爬取页面中提取链接，这样爬虫就能发现并跟随新的 URL。

#### 验收标准

6.1 Link_Extractor 应解析 HTML 内容并从 `<a>` 标签中提取所有 `href` 属性值。

6.2 Link_Extractor 应使用 Python 标准库中的 `html.parser.HTMLParser`。

6.3 Link_Extractor 应使用页面的基准 URL 将相对 URL 解析为绝对 URL。

6.4 Link_Extractor 应通过移除片段（#section）并将 scheme/host 规范化为小写来规范化所提取的 URL。

6.5 Link_Extractor 应返回绝对、已规范化 URL 字符串的列表。

### Requirement 7：URL 过滤

**用户故事：** 作为一名开发者，我希望有可配置的 URL 过滤器，这样爬虫只跟随符合我条件的 URL 并避免不想要的内容。

#### 验收标准

7.1 URL_Filter 应支持可配置的扩展名黑名单（默认：.jpg、.jpeg、.png、.gif、.svg、.pdf、.zip、.exe、.mp3、.mp4、.avi）以跳过非 HTML 资源。

7.2 URL_Filter 应支持可配置的域名白名单 —— 设置后，仅接受来自所列域名的 URL。

7.3 URL_Filter 应支持可配置的域名黑名单 —— 来自所列域名的 URL 被拒绝。

7.4 URL_Filter 应支持最大 URL 长度过滤（默认：2048 字符）。

7.5 URL_Filter 应支持自定义过滤函数，该函数接受 URL 字符串并返回 True（接受）或 False（拒绝）。

7.6 当白名单和黑名单都配置时，URL_Filter 应先应用白名单（URL 必须在白名单中且不在黑名单中）。

7.7 URL_Filter 应拒绝 scheme 不是 http 和 https 的 URL。

### Requirement 8：URL 去重

**用户故事：** 作为一名开发者，我希望有 URL 去重功能，这样爬虫就不会多次访问同一 URL。

#### 验收标准

8.1 在将 URL 入队到 URL_Frontier 之前，Crawler 应先检查 URL_Seen_Store。

8.2 如果该 URL 已被见过，则 Crawler 应跳过入队。

8.3 如果该 URL 未被见过，则 Crawler 应将其加入 URL_Seen_Store 并入队。

8.4 URL_Seen_Store 应默认使用内存中的 Python set 作为实现。

8.5 URL_Seen_Store 应在规范化后（scheme/host 小写、移除片段）再比较 URL。

### Requirement 9：Fetcher 接口

**用户故事：** 作为一名开发者，我希望有一个可插拔的 Fetcher 接口，这样我就可以使用自定义 HTTP 客户端或用于测试的 Mock Fetcher。

#### 验收标准

9.1 Fetcher 应定义一个抽象接口，包含一个异步方法，该方法接受 URL 并返回响应（状态码、头、体）。

9.2 默认的 Fetcher 实现应使用包装在 asyncio 中的 `urllib.request.urlopen`（在 executor 中运行）来进行 HTTP 抓取。

9.3 Fetcher 接口应支持设置请求头（User-Agent 等）。

9.4 Fetcher 接口应支持每个请求可配置的超时。

9.5 任何符合 Fetcher 接口的实现都应能被 HTML_Downloader 使用，而无需修改。

### Requirement 10：内容处理钩子

**用户故事：** 作为一名开发者，我希望有一个用于内容处理的钩子/插件系统，这样我就能用自定义逻辑扩展爬虫（例如，索引、数据提取）。

#### 验收标准

10.1 Crawler 应支持注册 Content_Hook 回调，每个成功爬取的页面都会调用该回调。

10.2 每个 Content_Hook 应接收 URL、HTTP 状态、响应头、页面内容以及提取的链接。

10.3 Crawler 应支持多个 Content_Hook，按注册顺序依次调用。

10.4 如果 Content_Hook 抛出异常，则 Crawler 应记录该错误并继续处理（钩子不会中断爬取）。

10.5 Content_Hook 接口应同步和异步两种钩子实现都支持。

### Requirement 11：爬取结果

**用户故事：** 作为一名开发者，我希望有结构化的爬取结果，这样我就能检查每个已爬取 URL 的结果。

#### 验收标准

11.1 每个 Crawl_Result 应包含：URL、HTTP 状态码、内容长度、Content_Fingerprint、提取链接列表、爬取深度以及状态指示器（success、error、duplicate、disallowed、filtered）。

11.2 当页面成功爬取时，Crawl_Result 的状态应为 "success"。

11.3 当页面下载失败时，Crawl_Result 的状态应为 "error" 并附带错误消息。

11.4 当页面为内容重复时，Crawl_Result 的状态应为 "duplicate"。

11.5 当页面被 Robots.txt 禁止时，Crawl_Result 的状态应为 "disallowed"。

### Requirement 12：配置

**用户故事：** 作为一名开发者，我希望爬虫有一个单一的配置对象，这样我可以轻松调整爬取行为。

#### 验收标准

12.1 Crawler 应接受一个配置对象，包含以下设置：max_pages、max_depth、max_concurrent、per_host_delay、request_timeout、user_agent、url_filters 和 content_hooks。

12.2 配置应提供合理默认值：max_pages=100、max_depth=3、max_concurrent=10、per_host_delay=1.0 秒、request_timeout=30 秒、user_agent="WebCrawler/1.0"。

12.3 配置应为带类型提示的 dataclass。

12.4 配置应验证数值为正数，并在无效输入时抛出 ValueError。

## 非功能性需求

### NFR-1：项目结构

- **NFR-1.1**：项目应使用 `pyproject.toml` 配合 hatchling 构建系统。
- **NFR-1.2**：项目应采用 src 布局（`src/web_crawler/`）。
- **NFR-1.3**：项目要求 Python >= 3.9。
- **NFR-1.4**：项目应具有零运行时依赖（仅标准库）。
- **NFR-1.5**：项目应包含开发依赖：pytest、pytest-asyncio 和 hypothesis。
- **NFR-1.6**：项目应包含一个 `docs/` 目录用于文档。
- **NFR-1.7**：项目应包含一个 `examples/` 目录，提供用法演示。
- **NFR-1.8**：项目应提供清晰的 `__init__.py`，导出公共 API。

### NFR-2：代码质量

- **NFR-2.1**：项目应在所有公共接口中包含类型提示。
- **NFR-2.2**：项目应包含使用 pytest 编写的单元测试，覆盖所有公共方法。
- **NFR-2.3**：项目应包含使用 Hypothesis 编写的属性测试，用于 URL 规范化、过滤和去重正确性验证。
- **NFR-2.4**：项目应使用抽象基类（ABC）或 Protocol 定义所有可插拔接口。
- **NFR-2.5**：项目应为所有错误类型定义自定义异常模块。

### NFR-3：性能

- **NFR-3.1**：URL 去重查找应使用哈希集合，平均时间复杂度为 O(1)。
- **NFR-3.2**：内容指纹计算的时间复杂度应为 O(n)，其中 n 为内容长度。
- **NFR-3.3**：URL Frontier 的出队操作的时间复杂度应为 O(log n) 或更好。
- **NFR-3.4**：爬虫应支持至少 100 个页面的并发爬取而不降低性能。

### NFR-4：并发

- **NFR-4.1**：爬虫应使用 asyncio 进行并发下载。
- **NFR-4.2**：爬虫应使用 asyncio.Semaphore 将并发下载限制为配置的最大值。
- **NFR-4.3**：爬虫应适合单线程异步执行（核心操作无需多线程）。
- **NFR-4.4**：默认 Fetcher 应使用 `asyncio.get_event_loop().run_in_executor()` 包装阻塞的 urllib 调用。
