# 第10章：调度基准与真实HTTP验证

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
