# 实施计划：Web Crawler

## 概述

实现一个基于 Python asyncio 的网络爬虫库，遵循管道架构：URL Frontier → Robots 检查 → 抓取 → 解析 → 去重 → 提取链接 → 过滤 → 入队。项目采用 src 布局，零运行时依赖（仅标准库），使用 pytest + pytest-asyncio + hypothesis 进行测试，使用 hatchling 进行打包。

## 任务

- [ ] 1. 搭建项目结构与基础模块
  - [ ] 1.1 创建带有 pyproject.toml 的项目骨架及目录结构
    - 创建 `web-crawler/pyproject.toml`，使用 hatchling 构建系统，零运行时依赖，开发依赖包括 pytest、pytest-asyncio、hypothesis
    - 创建 `src/web_crawler/__init__.py`（初始为空）、`tests/__init__.py`、`docs/`、`examples/` 目录
    - _需求：NFR-1.1、NFR-1.2、NFR-1.3、NFR-1.4、NFR-1.5、NFR-1.6、NFR-1.7_

  - [ ] 1.2 实现自定义异常模块（`src/web_crawler/exceptions.py`）
    - 定义 `CrawlerError`、`ConfigurationError`、`FetchError`、`RobotsDisallowedError`、`ContentParseError`
    - 所有异常继承自 `CrawlerError`；`FetchError` 存储 url 和 reason
    - _需求：NFR-2.5_

  - [ ] 1.3 实现数据模型（`src/web_crawler/models.py`）
    - 定义 `CrawlStatus` 枚举（SUCCESS、ERROR、DUPLICATE、DISALLOWED、FILTERED）
    - 定义 `Priority` 枚举（HIGH=0、MEDIUM=1、LOW=2）
    - 定义带 url、priority、depth、host 的 `FrontierEntry` dataclass；实现 `__lt__` 以支持 heapq
    - 按设计定义包含所有字段的 `CrawlResult` dataclass
    - _需求：11.1、2.1、2.8_

  - [ ] 1.4 实现配置（`src/web_crawler/config.py`）
    - 定义包含所有设置及合理默认值的 `CrawlerConfig` dataclass
    - 实现 `__post_init__` 验证，对非正数抛出 ValueError
    - _需求：12.1、12.2、12.3、12.4_

- [ ] 2. 实现 URL Frontier
  - [ ] 2.1 实现 URL Frontier（`src/web_crawler/frontier.py`）
    - 实现基于 heapq 的前置队列和 `dict[host, deque]` 的后置队列的 `URLFrontier`
    - 实现 `put()`、`get()`、`is_empty()`、`size` 属性
    - 使用单调时间戳强制每主机的礼貌性延迟
    - _需求：2.1、2.2、2.3、2.4、2.5、2.6、2.7、2.8_

  - [ ] 2.2 为 Frontier 大小不变量编写属性测试
    - **Property 11：Frontier 大小不变量（Frontier size invariant）**
    - **验证：Requirement 2.7**

  - [ ] 2.3 为 Frontier 优先级排序编写属性测试
    - **Property 12：Frontier 优先级排序（Frontier priority ordering）**
    - **验证：Requirement 2.1、2.2**

- [ ] 3. 实现 Fetcher 接口与 HTTP 客户端
  - [ ] 3.1 实现 Fetcher ABC 和 URLLibFetcher（`src/web_crawler/fetcher.py`）
    - 定义 `FetchResponse` dataclass（status_code、headers、body、url）
    - 定义带有异步 `fetch()` 方法的 `Fetcher` ABC
    - 使用包装在 asyncio executor 中的 urllib.request 实现 `URLLibFetcher`
    - 处理 HTTPError、URLError、TimeoutError → 抛出 FetchError
    - _需求：9.1、9.2、9.3、9.4、9.5、3.1、3.2、3.5、3.6、NFR-4.4_

- [ ] 4. 实现 Robots.txt 检查器
  - [ ] 4.1 实现 RobotsChecker（`src/web_crawler/robots.py`）
    - 实现带可配置 TTL 的每主机缓存
    - 使用标准库 `RobotFileParser` 进行解析
    - 如果 Robots.txt 无法下载，则允许所有路径
    - _需求：4.1、4.2、4.3、4.4、4.5、4.6_

- [ ] 5. 实现内容解析与链接提取
  - [ ] 5.1 实现 ContentParser 和 LinkExtractor（`src/web_crawler/parser.py`）
    - 实现 `normalize_url()` 函数（scheme/host 小写、移除片段）
    - 实现 `LinkExtractorParser`（HTMLParser 子类，从 `<a>` 标签提取 href）
    - 实现检查 Content-Type 或内容嗅探的 `ContentParser.is_html()`
    - 实现解析相对 URL 并规范化的 `LinkExtractor.extract()`
    - _需求：5.1、5.2、6.1、6.2、6.3、6.4、6.5_

  - [ ] 5.2 为 URL 规范化幂等性编写属性测试
    - **Property 1：URL 规范化幂等性（URL normalization idempotence）**
    - **验证：Requirement 6.4、8.5**

  - [ ] 5.3 为 URL 规范化移除片段与小写编写属性测试
    - **Property 2：URL 规范化移除片段并小写 scheme/host（URL normalization removes fragments and lowercases scheme/host）**
    - **验证：Requirement 6.4、8.5**

  - [ ] 5.4 为链接提取完整性编写属性测试
    - **Property 3：链接提取完整性（Link extraction completeness）**
    - **验证：Requirement 6.1、6.3、6.5**

- [ ] 6. 实现 URL 过滤
  - [ ] 6.1 实现 URLFilter（`src/web_crawler/filters.py`）
    - 实现过滤链：scheme → 长度 → 白名单 → 黑名单 → 扩展名 → 自定义
    - 支持可配置的扩展名黑名单、域名白/黑名单、最大 URL 长度、自定义过滤器
    - _需求：7.1、7.2、7.3、7.4、7.5、7.6、7.7_

  - [ ] 6.2 为 URL 过滤 scheme 拒绝编写属性测试
    - **Property 4：URL 过滤 scheme 拒绝（URL filter scheme rejection）**
    - **验证：Requirement 7.7**

  - [ ] 6.3 为 URL 过滤扩展名黑名单编写属性测试
    - **Property 5：URL 过滤扩展名黑名单（URL filter extension blacklist）**
    - **验证：Requirement 7.1**

  - [ ] 6.4 为 URL 过滤域名白/黑名单交互编写属性测试
    - **Property 6：URL 过滤域名白/黑名单交互（URL filter domain whitelist/blacklist interaction）**
    - **验证：Requirement 7.2、7.3、7.6**

  - [ ] 6.5 为 URL 过滤长度拒绝编写属性测试
    - **Property 7：URL 过滤长度拒绝（URL filter length rejection）**
    - **验证：Requirement 7.4**

- [ ] 7. 实现去重
  - [ ] 7.1 实现 ContentSeen 和 URLSeen（`src/web_crawler/dedup.py`）
    - 实现带 SHA-256 指纹集合的 `ContentSeen`
    - 实现带规范化 URL 集合的 `URLSeen`
    - _需求：5.3、5.4、5.5、5.6、8.1、8.2、8.3、8.4、8.5_

  - [ ] 7.2 为内容指纹确定性编写属性测试
    - **Property 8：内容指纹确定性（Content fingerprint determinism）**
    - **验证：Requirement 5.3**

  - [ ] 7.3 为内容去重正确性编写属性测试
    - **Property 9：内容去重正确性（Content deduplication correctness）**
    - **验证：Requirement 5.4、5.5**

  - [ ] 7.4 为带规范化的 URL 去重编写属性测试
    - **Property 10：带规范化的 URL 去重（URL deduplication with normalization）**
    - **验证：Requirement 8.2、8.3、8.5**

- [ ] 8. 检查点 —— 验证组件测试通过
  - 确保所有测试通过，若有疑问向用户确认。

- [ ] 9. 实现 Crawler 编排器
  - [ ] 9.1 实现 Crawler 类（`src/web_crawler/crawler.py`）
    - 实现异步 `crawl()` 方法，包含种子 URL 入队与主爬取循环
    - 实现 `_process_url()` 管道：robots → fetch → dedup → parse → extract → filter → enqueue
    - 实现带 URL 已见检查的 `_enqueue_url()`
    - 实现支持同步与异步钩子的 `_invoke_hooks()`，并进行异常隔离
    - 使用 asyncio.Semaphore 进行并发限制
    - 强制执行 max_pages 和 max_depth 停止条件
    - _需求：1.1、1.2、1.3、1.4、1.5、1.6、1.7、1.8、3.3、3.4、3.7、3.8、10.1、10.2、10.3、10.4、10.5、NFR-4.1、NFR-4.2、NFR-4.3_

  - [ ] 9.2 为配置验证编写属性测试
    - **Property 13：配置验证（Configuration validation）**
    - **验证：Requirement 12.4**

  - [ ] 9.3 为爬取结果状态正确性编写属性测试
    - **Property 14：爬取结果状态正确性（Crawl result status correctness）**
    - **验证：Requirement 11.2、11.3、11.4、11.5、3.4、3.7**

- [ ] 10. 接入公共 API 与单元测试
  - [ ] 10.1 实现公共 API 导出（`src/web_crawler/__init__.py`）
    - 按设计导出所有公共类、函数和异常
    - _需求：NFR-1.8_

  - [ ] 10.2 为模型和配置编写单元测试（`tests/test_models.py`、`tests/test_config.py`）
    - 测试 CrawlStatus、Priority、FrontierEntry 排序、CrawlResult 字段
    - 测试 CrawlerConfig 默认值与验证
    - _需求：11.1、12.1、12.2、12.3、12.4_

  - [ ] 10.3 为 Frontier 编写单元测试（`tests/test_frontier.py`）
    - 测试 put/get、优先级排序、礼貌性延迟、empty/size
    - _需求：2.1、2.2、2.3、2.4、2.5、2.6、2.7_

  - [ ] 10.4 为 Fetcher 编写单元测试（`tests/test_fetcher.py`）
    - 测试 FetchResponse 创建、URLLibFetcher 错误处理（基于 mock）
    - _需求：9.1、9.2、3.7_

  - [ ] 10.5 为 Robots 检查器编写单元测试（`tests/test_robots.py`）
    - 测试允许/禁止路径、缓存、下载失败回退
    - _需求：4.1、4.2、4.3、4.4、4.5、4.6_

  - [ ] 10.6 为 Parser 和 Link Extractor 编写单元测试（`tests/test_parser.py`）
    - 测试 normalize_url、is_html、相对/绝对 URL 的链接提取
    - _需求：5.1、5.2、6.1、6.2、6.3、6.4、6.5_

  - [ ] 10.7 为 URL Filter 编写单元测试（`tests/test_filters.py`）
    - 测试 scheme 拒绝、扩展名黑名单、域名白/黑名单、长度、自定义过滤器
    - _需求：7.1、7.2、7.3、7.4、7.5、7.6、7.7_

  - [ ] 10.8 为去重编写单元测试（`tests/test_dedup.py`）
    - 测试 ContentSeen 指纹与 is_seen、URLSeen 规范化
    - _需求：5.3、5.4、5.5、5.6、8.1、8.2、8.3、8.4、8.5_

  - [ ] 10.9 为 Crawler 编排器编写单元测试（`tests/test_crawler.py`）
    - 测试使用 Mock Fetcher 的完整爬取循环、max_pages、max_depth、hooks、错误处理
    - _需求：1.1、1.2、1.3、1.4、1.5、1.6、1.7、1.8、10.1、10.2、10.3、10.4、10.5_

- [ ] 11. 检查点 —— 完整测试套件通过
  - 确保所有测试通过，若有疑问向用户确认。

- [ ] 12. 添加示例与文档
  - [ ] 12.1 创建示例脚本（`examples/basic_crawl.py`）
    - 实现带有预定义页面的 MockFetcher
    - 演示 Crawler 的使用，包含配置、种子 URL 及结果打印
    - _需求：NFR-1.7_

  - [ ] 12.2 将规格文档复制到 docs/ 目录
    - 将 requirements.md 和 design.md 复制到 `docs/`；创建 `docs/tasks.md` 摘要
    - _需求：NFR-1.6_

- [ ] 13. 最终检查点 —— 所有测试通过且包安装干净
  - 确保所有测试通过，若有疑问向用户确认。

## 备注

- 标记 `*` 的任务为可选，为加快 MVP 可跳过
- 每个任务引用具体需求以便追溯
- 检查点确保增量验证
- 属性测试验证设计文档中通用的正确性属性（共 14 个属性）
- 单元测试验证具体示例与边界情况
- 项目使用 Python 配合 asyncio；零运行时依赖（仅标准库）
- 所有属性测试放在 `tests/test_properties.py`；单元测试放在各自的测试文件

## 任务依赖图

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4"] },
    { "id": 2, "tasks": ["2.1", "3.1", "5.1", "6.1", "7.1"] },
    { "id": 3, "tasks": ["2.2", "2.3", "4.1", "5.2", "5.3", "5.4", "6.2", "6.3", "6.4", "6.5", "7.2", "7.3", "7.4"] },
    { "id": 4, "tasks": ["9.1"] },
    { "id": 5, "tasks": ["9.2", "9.3", "10.1"] },
    { "id": 6, "tasks": ["10.2", "10.3", "10.4", "10.5", "10.6", "10.7", "10.8", "10.9"] },
    { "id": 7, "tasks": ["12.1", "12.2"] }
  ]
}
```
