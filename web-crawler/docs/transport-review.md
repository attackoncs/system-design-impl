# 第 10 章：HTTP 下载与 DNS 缓存

本记录对照本地《System Design Interview.md》网页爬虫章节的 HTML
Downloader、Cache DNS Resolver、Short timeout 与健壮性要求。

## 已实现

- `URLLibFetcher` 使用独立 urllib opener 和专用 HTTP(S) connection，DNS
  缓存只改变 socket 连接地址。请求 URL、HTTP Host、TLS SNI 与证书验证保留原始域名。
  不修改全局 `socket.getaddrinfo` 或 urllib 全局 opener；默认使用直接连接，忽略环境代理。
- `DNSCache(ttl=300, capacity=1024)` 使用有界 LRU、单调时钟 TTL 和分段锁合并并发解析。
  到期后由下一次访问刷新；不创建额外刷新线程。`getaddrinfo` 不提供权威 TTL，
  此 TTL 是应用刷新间隔。失败不缓存，IPv4/IPv6 地址按系统返回顺序尝试。
- `max_response_bytes` 默认 10 MiB。先检查 Content-Length，再最多读取上限加一字节，
  因而未声明大小的响应也受约束。超限与截断响应报 `FetchError`，不会保存部分页面。
  不自动解压内容，此限制作用于收到的响应体字节。
- `timeout` 覆盖等待下载槽、DNS、建连、响应头和响应体。取消或超时会关闭该请求的 socket。
  下载槽在工作线程实际退出后释放，防止反复超时导致后台工作无限累积。
  `max_concurrent_downloads` 默认 32；Crawler 的并发上限仍由其自身配置约束。
- 默认将所有 HTTP 3xx 原样返回，由 Crawler 将 Location 重新送入过滤、robots 与礼貌调度。
  这样同站重定向到禁止路径也会重新检查。显式设置
  `follow_same_origin_redirects=True` 可以自动跟随同源跳转，此时由 `max_redirects=5`
  限制跳数；跨源始终返回调度器。自动跟随关闭前一跳响应而不无界读取其 body。
- 成功响应、HTTP 错误以及重定向响应都会关闭；底层网络、协议与超时错误统一为 `FetchError`。

## 使用示例

```python
from web_crawler.dns import DNSCache
from web_crawler.fetcher import URLLibFetcher

fetcher = URLLibFetcher(
    max_response_bytes=2 * 1024 * 1024,
    max_concurrent_downloads=16,
    dns_cache=DNSCache(ttl=60, capacity=2048),
)
response = await fetcher.fetch("https://example.com/", timeout=5)
```

## 验证与边界

专用测试使用本地回环 HTTP 服务验证实际下载、Host、响应上限、截断、重定向、
滴流超时和取消后的下载槽回收；单元测试验证 TLS SNI 保留原始域名、DNS TTL、LRU、
并发 miss 合并与 DNS 阻塞期间的下载槽保留。TLS 证书验证由标准库默认 SSL context
负责；SNI 测试检查连接调用参数，没有将 mock 测试描述为公网 TLS 集成验证。

系统 `getaddrinfo` 调用无法从 Python 安全强制中断。调用方按总 deadline 返回，
仍在运行的解析继续占有有限下载槽直到系统返回；极端情况下全部槽可能暂时不可用。
需要严格控制 DNS 自身时限的部署应替换解析实现，或采用独立解析进程。

该实现没有连接池、HTTP/2、多服务器 DNS 缓存同步或地域部署调度。
自动同源跳转属于显式 opt-in，逐跳 robots 与礼貌检查应使用默认配置。
