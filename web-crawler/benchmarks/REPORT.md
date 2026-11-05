# 第10章：调度基准与真实HTTP验证

## 2026-11-05 本轮复验

Windows/Python3.13.9，修改前 **256项/13.04秒**，修复后 **259项/14.05秒，-W error**。新增[3个连续取消场景](../tests/test_cancellation_review.py)使用真实SQLite和受控事件，分别阻塞open/finish/close；旧实现均提前返回，修复后排空线程与子任务、释放文件锁、传播取消并允许实例复用。没有通过增加重试限额或忽略错误来规避失败。

```bash
python -W error -m pytest tests -q
python -W error tools/verify_http.py --output benchmarks/http-review-2026-11-05.json
python examples/basic_crawl.py
python tools/benchmark.py --output benchmarks/review-2026-11-05.json
```

[实际回环HTTP输出](http-review-2026-11-05.json)：SUCCESS2、DISALLOWED1，快照2份，重建Crawler后继续，禁止路径未请求且无未确认任务。请求为`/robots.txt`、`/index`、重建后的`/robots.txt`、`/assets/child`。这是本机真实HTTP；另一个basic_crawl示例使用MockFetcher，4页均SUCCESS，不冒称公网测试。

[本轮调度样本](review-2026-11-05.json)仍是10000条已保留主机积压/100个其他主机，以及5000个独立主机，各3次；中位耗时分别0.158300毫秒、8.542500毫秒。只计内存frontier出队，排除入队、HTTP、robots、解析、SQLite和磁盘。样本在本项目整套测试之后顺序运行；同机其他章节可能有复查负载，不与历史样本宣称提速或退化，也不推导端到端吞吐量。

源码与新增测试Ruff E9/F通过；新增测试格式检查通过。37个Python文件通过Python3.9语法解析，项目本地文档链接及JSON解析通过。随后加强3项回归：在手动兜底close之前验证自动释放owner及新实例接管，3项/0.45秒通过；生产代码未变。未改原有无关换行格式。保留以下历史对照与原始数据。

## 2026-11-01 历史测量

核对日期：2026-11-01，Python3.13.9、Windows。基准只衡量内存 `URLFrontier.get()`，不包含入队、HTTP、robots、解析、SQLite或磁盘，不代表爬虫端到端吞吐量。

## 同负载对照

基线来自提交 `18e44d1` 的 `frontier.py` 与 `models.py`，放入独立临时包，运行相同的[测量脚本](../tools/benchmark.py)。当前版本使用工作区源码。每种场景重复3次并取中位数。

| 场景 | 基线中位耗时 | 当前中位耗时 |
|---|---:|---:|
| 一个已保留主机积压10000条，取100个其他主机任务再空轮询一次 | 825.330毫秒 | 0.147毫秒 |
| 5000个独立可用主机依次出队 | 10.403毫秒 | 8.405毫秒 |

旧调度器每轮遍历被保留主机的积压URL，再重新入堆；当前版本只发布可用主机队首，延迟主机另用唤醒堆。第二个场景表明普通负载没有观察到退化；3次本地测量不能证明所有负载的性能。

原始数据：[基线](baseline-2026-11-01.json)、[当前](review-2026-11-01.json)。另保留[此前测量](open-source-review-2026-10-31.json)。

```bash
python tools/benchmark.py --output benchmarks/local.json
```

## 真实HTTP与恢复

[验证工具](../tools/verify_http.py)在回环地址启动自有HTTP服务，首次只抓主页，重建Crawler后接着处理两个发现的链接。断言：首个base href生效、坏链接不影响好链接、robots禁止地址未被下载、两个成功页面已持久保存、没有未确认任务。

```bash
python -W error tools/verify_http.py --output benchmarks/http-local.json
```

[2026-11-01结果](http-review-2026-11-01.json)：SUCCESS2条、DISALLOWED1条、快照2份，重建后恢复成功。该工具不会访问公网；生产集群、地域部署、动态渲染和五年内容保留仍需独立验证与部署。
