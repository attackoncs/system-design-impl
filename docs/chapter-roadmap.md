# 后续章节任务路线图

更新日期：2026-11-06。第 02、03、05—29 章的 27 个子项目均已有复审记录，逐章证据见[复审覆盖清单](review-coverage_cn.md)。本次重新运行的是第 2、3 章；其他章节沿用各自记录日期的验证结果。

补充基础章节：第2章[扩容演进实验](../scale-from-zero/docs/chapter02-review.md)与第3章[容量估算](../capacity-estimation/docs/chapter03-review.md)本轮补齐开源源码对照及回归验证，分别77项测试加5个子例通过、16项测试通过；真实本机拓扑与估算工具的能力边界见各自报告。第1/4章属于导读/方法，不是独立系统。

来源：根目录 `System Design Interview.md` 第 15—30 章。按原书需求与设计小节整理；第 15—29 章参考实现已完成，第 30 章为后续学习资料。

下表第 15—29 章均已核对原书、开源代码与调用链，并记录修复及验证结果。“已复审”指选定参考实现的核心链路，生产部署与规模目标的未实现项仍按各章报告保留。

requirements.md、design.md、tasks.md 应与实现同步；最新验证结果、证据日期及未实现边界以各章复审记录为准。

| 章节 | 原书章节 | 任务文档 | 复审证据 | 状态与保留边界 |
| --- | --- | --- | --- | --- |
| 15 | Design YouTube | [video-streaming-system](../video-streaming-system/docs/tasks.md) | [第15章](../video-streaming-system/docs/chapter15-review.md) | 核心链路已复审；云生产扩展待做 |
| 16 | Design Google Drive | [file-sync-system](../file-sync-system/docs/tasks.md) | [第16章](../file-sync-system/docs/chapter16-review.md) | 核心链路已复审；分布式生产扩展待做 |
| 17 | Proximity Service | [proximity-service](../proximity-service/docs/tasks.md) | [第17章](../proximity-service/docs/chapter17-review.md) | 核心链路已复审；独立HTTP复制已验证，生产扩展待做 |
| 18 | Nearby Friends | [nearby-friends-system](../nearby-friends-system/docs/tasks.md) | [第18章](../nearby-friends-system/docs/chapter18-review.md) | 核心链路已复审；双WS/双Redis已验证，生产扩展待做 |
| 19 | Google Maps | [maps-system](../maps-system/docs/tasks.md) | [第19章](../maps-system/docs/chapter19-review.md) | 核心链路已复审；合成路网验证，真实地图部署待扩展 |
| 20 | Distributed Message Queue | [distributed-message-queue](../distributed-message-queue/docs/tasks.md) | [第20章](../distributed-message-queue/docs/chapter20-review.md) | 核心链路已复审；独立broker故障已验证，协调器HA待扩展 |
| 21 | Metrics Monitoring and Alerting System | [metrics-monitoring-system](../metrics-monitoring-system/docs/tasks.md) | [第21章](../metrics-monitoring-system/docs/chapter21-review.md) | 核心链路已复审；采集/查询/告警已验证，存储HA待扩展 |
| 22 | Ad Click Event Aggregation | [ad-click-aggregation-system](../ad-click-aggregation-system/docs/tasks.md) | [第22章](../ad-click-aggregation-system/docs/chapter22-review.md) | 核心链路已复审；事务中断恢复已验证，日志复制HA待扩展 |
| 23 | Hotel Reservation System | [hotel-reservation-system](../hotel-reservation-system/docs/tasks.md) | [第23章](../hotel-reservation-system/docs/chapter23-review.md) | 核心链路已复审；并发/模拟支付补偿已验证，存储HA与真实支付待扩展 |
| 24 | Distributed Email Service | [distributed-email-service](../distributed-email-service/docs/tasks.md) | [第24章](../distributed-email-service/docs/chapter24-review.md) | 核心链路已复审；SMTP/副本已验证，真实扫描与复制对象待扩展 |
| 25 | S3-like Object Storage | [object-storage-system](../object-storage-system/docs/tasks.md) | [第25章](../object-storage-system/docs/chapter25-review.md) | 核心链路已复审；上传/修复已验证，元数据HA/跨机耐久性待扩展 |
| 26 | Real-time Gaming Leaderboard | [gaming-leaderboard-system](../gaming-leaderboard-system/docs/tasks.md) | [第26章](../gaming-leaderboard-system/docs/chapter26-review.md) | 核心链路已复审；真实Redis排名/恢复已验证，生产性能/自动HA待扩展 |
| 27 | Payment System | [payment-system](../payment-system/docs/tasks.md) | [第27章](../payment-system/docs/chapter27-review.md) | 核心链路已复审；模拟支付/复式账本/故障恢复已验证，真实PSP与权威库HA待扩展 |
| 28 | Digital Wallet | [digital-wallet-system](../digital-wallet-system/docs/tasks.md) | [第28章](../digital-wallet-system/docs/chapter28-review.md) | 核心链路已复审；TCC/日志多数派/在途恢复已验证，自动选主、账户复制与百万TPS未实现 |
| 29 | Stock Exchange | [stock-exchange-system](../stock-exchange-system/docs/tasks.md) | [第29章](../stock-exchange-system/docs/chapter29-review.md) | 核心链路已复审；撮合/主备重放/行情恢复已验证，定序器HA、自动切换和生产性能未实现 |

## 第 30 章：The Learning Continues

这一章是学习资料集合，不是一个独立系统实现。已有[中文翻译](translations/system-design-interview/30-the-learning-continues.zh-CN.md)及[学习方法与源码核对](chapter30-learning-continues_cn.md)。以下区分已建立的阅读入口与持续维护项。

- [x] L1. 建立[案例学习导航](learning-guide_cn.md)，提供按存储、消息、社交、媒体、可观测性等主题选择项目的入口。
- [ ] L2. 持续核验原书外部参考链接，以原始论文和官方工程文档补充过时材料；原书旧链接尚未全部复核。
- [x] L3. 建立[复审覆盖清单](review-coverage_cn.md)，链接各章设计取舍、验证结果与适用边界。
- [x] L4. 提供[宏观设计、调用链与失败练习的阅读步骤](chapter30-learning-continues_cn.md)，关联各项目实验，不将读过文章当成完成系统实现。

## 建议学习与扩展顺序

保持原书顺序可先阅读视频与文件同步；若优先理解基础设施，可先阅读第 20 章消息队列和第 25 章对象存储，再对照视频、邮件与文件同步中的队列和存储设计。

第 21 章监控可对照通知系统的投递语义；第 22 章聚合可对照消息队列的重放语义；第 27—29 章可比较幂等、账本、事件溯源和恢复实验。各子项目保留独立运行方式与章节语义，这些是学习和后续集成方向。
