# 从一个案例读懂系统：27个子项目学习导航

每份导读用一个具体案例串起四件事：系统要保存什么事实、各组件如何协作、一次操作在哪些函数中改变状态、失败后依靠什么恢复。先建立整体认识，再按编号跳进代码，最后用已存在的测试核对预期。

## 对照GitHub上的实现

[27个子项目的GitHub编码实现对照](github-implementations_cn.md)按章节列出仓库、主要语言、对应范围和学习重点，区分业务系统、算法库、核心组件及教学案例。

## 选一条适合你的路线

| 你想先掌握什么 | 推荐路线 | 第一轮的目标 |
|---|---|---|
| 从函数读到完整小系统 | 唯一ID → 短链接 → 限流器 | 能解释输入、核心算法、共享状态和返回结果 |
| 缓存与异步业务 | 新闻动态 → 通知 → 聊天 → 消息队列 | 能区分受理、持久提交、后台完成与用户可见 |
| 存储与恢复 | 一致性哈希 → 键值存储 → 文件同步 → 对象存储 | 能解释数据归属、版本、复制与重启恢复 |
| 状态一致性与资金业务 | 酒店预订 → 支付 → 数字钱包 → 证券交易 | 能定位事务、幂等身份、状态推进和补偿 |
| 数据处理与派生视图 | 自动补全 → 广告聚合 → 指标告警 → 排行榜 | 能区分原始事实、处理进度、快照和查询结果 |

容量估算适合穿插学习：用它给设计列出假设、单位和资源预算，再回到具体实现判断瓶颈。

## 一份导读怎样读

1. **先读案例和整体图。** 暂时不点源码，先预测这次输入会产生哪些状态和输出。
2. **沿数据归属表找状态。** 明确哪份记录是事实，哪份是缓存／投影，哪份只存在于进程内。
3. **按编号打开源码。** 每站都有要回答的问题与输入／输出；先读指定函数和解释过的摘录，再决定是否展开辅助函数。
4. **对照失败分支。** 找出成功的提交点，判断重试会不会重复改变状态、崩溃后能恢复什么。
5. **核对测试和练习。** 每页提供明确测试入口、观察项和带答案自查。运行命令与依赖按对应 README，每个项目独立执行。

读完一个项目的标志：你能拿案例的数据，从入口讲到结果；能指着具体函数说明一次关键状态提交；能预测一个重复、失败或并发场景的结果。

## 按章节查找

| 章节 | 导读 | 主要学习问题 |
|---|---|---|
| 02 | [从零扩容](../scale-from-zero/docs/learning-guide_cn.md) | 理解负载均衡、缓存、主从复制和异步任务怎样组成可扩展服务。 |
| 03 | [容量估算](../capacity-estimation/docs/learning-guide_cn.md) | 把业务假设转换为带明确单位的QPS、存储、带宽和节点预算。 |
| 05 | [限流器](../rate-limiter/docs/learning-guide_cn.md) | 沿规则匹配、算法决策和原子状态更新理解请求准入。 |
| 06 | [一致性哈希](../consistent-hashing/docs/learning-guide_cn.md) | 通过不可变环快照理解键路由、虚拟节点和拓扑变化。 |
| 07 | [键值存储](../key-value-store/docs/learning-guide_cn.md) | 从节点API深入副本协调、仲裁读写与LSM持久存储。 |
| 08 | [唯一ID生成器](../unique-id-generator/docs/learning-guide_cn.md) | 用位布局、时钟与序列号理解Snowflake唯一ID的生成条件。 |
| 09 | [短链接](../url-shortener/docs/learning-guide_cn.md) | 串起短码生成、唯一映射持久化和重定向读取。 |
| 10 | [网页爬虫](../web-crawler/docs/learning-guide_cn.md) | 从有界调度深入礼貌抓取、内容去重、链接发现和重启恢复。 |
| 11 | [通知系统](../notification-system/docs/learning-guide_cn.md) | 理解通知受理、通道队列、异步投递与至少一次重试。 |
| 12 | [新闻动态系统](../news-feed-system/docs/learning-guide_cn.md) | 把帖子受理、混合推拉Feed和有界资料补全串成完整读写路径。 |
| 13 | [聊天系统](../chat-system/docs/learning-guide_cn.md) | 从会话鉴权到消息持久化与增量同步，理解实时连接和离线恢复。 |
| 14 | [搜索自动补全](../search-autocomplete-system/docs/learning-guide_cn.md) | 理解查询快照与采集聚合分离，以及Trie如何返回热门前缀候选。 |
| 15 | [视频流媒体](../video-streaming-system/docs/learning-guide_cn.md) | 沿分片上传、异步转码和播放授权理解媒体数据与元数据分离。 |
| 16 | [文件同步](../file-sync-system/docs/learning-guide_cn.md) | 从块上传和版本提交深入增量同步、冲突副本与设备确认。 |
| 17 | [附近商家服务](../proximity-service/docs/learning-guide_cn.md) | 从Geohash候选过滤到精确距离计算，理解商家查询与每日发布。 |
| 18 | [附近好友](../nearby-friends-system/docs/learning-guide_cn.md) | 串起位置授权、持久上报、Redis提示和周期快照校正。 |
| 19 | [地图系统](../maps-system/docs/learning-guide_cn.md) | 从路网和定位报告理解路线计算、交通聚合及导航更新。 |
| 20 | [分布式消息队列](../distributed-message-queue/docs/learning-guide_cn.md) | 从生产到分区日志再到消费确认，理解顺序、复制和重放。 |
| 21 | [指标监控与告警](../metrics-monitoring-system/docs/learning-guide_cn.md) | 沿采集、持久排队、时序存储和告警投递理解监控数据生命周期。 |
| 22 | [广告点击聚合](../ad-click-aggregation-system/docs/learning-guide_cn.md) | 理解事件去重、时间窗口聚合和检查点重放的关系。 |
| 23 | [酒店预订](../hotel-reservation-system/docs/learning-guide_cn.md) | 从库存事务和预订状态深入异步支付与补偿，理解防超卖。 |
| 24 | [分布式邮件](../distributed-email-service/docs/learning-guide_cn.md) | 从SMTP受理与收件处理深入邮箱存储、对象正文和异步发送。 |
| 25 | [对象存储](../object-storage-system/docs/learning-guide_cn.md) | 理解对象元数据、分块数据副本和分段上传发布之间的边界。 |
| 26 | [游戏排行榜](../gaming-leaderboard-system/docs/learning-guide_cn.md) | 把比赛事实日志、Redis排序投影和跨分片排名查询串起来。 |
| 27 | [支付系统](../payment-system/docs/learning-guide_cn.md) | 从持久受理到供应商确认与复式记账，理解未知结果和对账。 |
| 28 | [数字钱包](../digital-wallet-system/docs/learning-guide_cn.md) | 沿协调日志和分片参与者理解跨账户转账、恢复与审计。 |
| 29 | [证券交易](../stock-exchange-system/docs/learning-guide_cn.md) | 通过有序命令日志和确定性撮合理解订单、成交与行情投影。 |

## 图和代码如何保持一致

导读原稿位于 `tools/learning_guides/<project>.md`，解释与图由阅读实际实现后编写，源码定义和摘录由工具校验。调用图只保留案例所需路径；后台任务、回调与替代后端会在文中说明。

```powershell
python tools/build_learning_guides.py
python tools/build_learning_guides.py --check
```

`--check` 检查27个项目覆盖、真实源码／测试定义、摘录范围、本地链接和生成内容同步。它不能证明案例解释、动态调用或生产容量正确。继续深入代码时可用 CodeGraph 追踪调用，用 code-review-graph 查看影响范围；先确认索引与工作区代码同步。
