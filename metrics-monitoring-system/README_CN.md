# 指标监控与告警（第21章）

实现持久化拉取／推送采集、真实队列传输、标签索引时序存储、分层降采样、
告警状态机、静默／分组／去重／重试，以及HTTP查询、SVG图表和简单仪表板。
不包含日志、分布式追踪或业务分析。

## 运行

Python 3.10+，标准库。在本目录执行：

```powershell
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --output .runtime/query.json
```

完整测试／示例使用第20章的真实HTTP消息队列：3个独立broker、1个协调器和3个TCP代理，
再加exporter、collector、查询API、存储consumer、告警worker及自有webhook接收器，
共13个独立进程。退出停止自有进程，私有配置和日志留在忽略目录.runtime。
没有模拟Kafka，也没有向外部邮箱／商业服务发送通知。

持久运行先启动第20章服务，然后：

```powershell
python tools/init_config.py --queue-config ../distributed-message-queue/.runtime/secrets.json
$env:PYTHONPATH = 'src'
python -m metrics_monitor.cli exporter --root .runtime/exporter --config .runtime/secrets.json --port 9822
```

配置私有targets.json后，在其他终端分别设置相同PYTHONPATH、运行collector/api/storage/
alerts角色，完整命令见[英文说明](README.md)。打开 `http://127.0.0.1:9821/dashboard`
并输入read_token。凭据只留页面内存，不能提交或嵌入页面。

## 数据与队列

- 样本含metric、labels、type（gauge/counter）、Unix timestamp、value；有限数值，
  计数器非负。同序列／时间戳相同值幂等，不同值整个批次回滚。
- exporter采集真实进程CPU累计秒数、tracemalloc内存分配字节和HTTP请求计数。
  内存不是系统RSS，CPU不是整机利用率。采集失败生成collector_up=0。
- collector先持久化outbox才接受推送，响应不表示已入TSDB。默认16MiB积压上限，
  超限429；队列每条不超过12KiB。存储consumer先幂等入库再提交位置，停止后可回放。
- 默认全局1万序列／每指标1000序列、每序列最多8标签、每标签值128字符。
  单次查询最多200序列和2万输出点。异步基数／无效批次进入可见拒绝账本。
  队列offset过期会停止推进，需运维显式重置，不静默跳过。

## 查询、保留和归档

`/query`与`/chart.svg`要求read/admin Bearer；参数metric/start/end，选填match JSON、
step、aggregation。量规支持avg/min/max/last，avg按样本数加权。计数器支持increase/
rate/last，识别重置，不把累计读数求和当请求数。rate使用每桶实际首末时间，单点返回null，
没有外推或隐式跨查询桶回看。标签只支持精确匹配，不是完整PromQL。

原始约7天、分钟约30天、小时约365天。为避免边界遗漏，原始最多多留一分钟，粗层最多
多留一小时。原始窗口内允许迟到样本，重新构建摘要后再清理；未来容差30秒。
查询合并不重叠年龄层及实时尾部；粗粒度部分桶会包含整桶摘要，并明确标记精度限制。

本地冷归档使用gzip和SHA-256校验，可在实时数据过期后验证读取；archive CLI见英文README。
不是云存储迁移，归档文件不自动按在线保留策略删除。部分告警／通知元数据尚无GC。

## 告警与通知

`POST /rules`、`/silences`和`/evaluate`要求admin；`GET /alerts`要求read/admin。
规则ID不可变，支持阈值、连续持续时间和分组标签；抖动恢复会重置pending。
缺少／陈旧数据重置pending，保留已firing状态并标no_data，不假装恢复正常。

静默期间不发送，状态继续评估；未分组的过时事件可取消。已经冻结的通知批次保持同一
幂等ID／内容重试；分组FIFO防止恢复通知越过失败告警。30秒租约隔离worker，最多5次
指数退避，耗尽保留dead记录。接收方配合幂等才能避免外部重复。
静默可能推迟整批，已分组旧状态可能在解除后按序投递；也可能单独发送resolved。

实际验证的是自有HTTP webhook；邮件／电话／PagerDuty由可替换Provider接口扩展，
未配置会明确失败，不能宣称真实供应商投递完成。配置生成器不启用任何通知目的地。

## 验证与边界

15项测试通过，包括分层聚合／重置、迟到修复、积压／基数限制、持续告警／静默、
真实队列回放、目标失联、HTTP图表与丢确认通知去重。
示例push到可查询530.82ms，webhook尝试2次但只产生1条唯一通知。
100序列／6000样本的独立HTTP查询实验p95为67.171ms，这是缓存固定范围的小规模测试。

详见[报告](benchmarks/results.md)与[设计](docs/design.md)。SQLite和第20章单协调器均有
规模／可用性限制；TSDB分片、共识元数据、TLS、真实供应商、云部署和千万序列仍待扩展。
