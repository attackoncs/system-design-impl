# 子项目复审覆盖清单

清点日期：2026-11-06。范围是根目录README列出的 **27个独立子项目**。第1章、第4章及第30章属于阅读/学习章节，不计作新增业务实现。

本次先核对既有证据，不将“有README”“测试通过”或“有同类GitHub仓库”单独当作完成标准。每项同时检查原书覆盖说明、实际开源文件/函数的对照、CodeGraph调用链记录、具体修复或不改代码的理由、测试/示例结果，以及双语文档和未实现边界。

## 清点结论

- **初次清点时25个项目已有上述复审记录**，无需因为文档日期较早就重复改写实现。本表链接指向已有证据，本次没有重新运行这25个项目的全部测试。
- **第2章和第3章现已补齐本轮统一流程**。第2章已有原书核对、图分析和73项测试记录，原先缺少独立的GitHub实际源码对照；本轮固定实读HAProxy/Redis，并修复租约检查与写入之间的竞态、非法TTL破坏旧缓存等边界，最终77项通过、5个子例通过（51.35秒）。第3章此前也已有职责拆分、Decimal上下文/并发隔离和14项测试的工程审查；本轮固定实读napkin-math，补齐章节对照，并修复精确节点向上取整/备用节点整数相加，最终16项通过（0.52秒）。
- **补齐后27个子项目均具相应流程的复审记录**。本次实际重新运行的是第2、3章；其余25项沿用表中各自日期的既有证据，不能把这些历史数字称为本次全仓重新验证结果。
- “已复审”表示已检查选定参考实现，不表示原书讨论的所有生产部署、可用性和规模目标均已实现。自动供给/安全缩容、真正共识、跨机房容灾、真实供应商等遗留项，应继续按各章的边界处理。

## 逐项目证据

| 章 | 子项目 | 现有证据 | 清点状态 | 本轮处理/保留边界 |
|---|---|---|---|---|
| 02 | 从零扩容 | [章节](../scale-from-zero/docs/chapter02-review.md) · [源码对照](../scale-from-zero/docs/open-source-review_cn.md) · [验证](../scale-from-zero/benchmarks/REPORT.md) | 本轮补齐复审记录 | 修复租约TOCTOU及非法TTL保留旧缓存；77项、5子例通过；生产级多地域HA等边界保留 |
| 03 | 容量估算 | [章节](../capacity-estimation/docs/chapter03-review.md) · [源码对照](../capacity-estimation/docs/open-source-review_cn.md) · [验证](../capacity-estimation/benchmarks/REPORT.md) | 本轮补齐复审记录 | 修复离散节点向上取整/大数备用节点丢精度；16项严格测试通过，估算不等于实测容量 |
| 05 | 限流器 | [章节](../rate-limiter/docs/chapter05-review.md) · [源码对照](../rate-limiter/docs/open-source-review_cn.md) · [验证](../rate-limiter/benchmarks/REPORT.md) | 已见完整复审记录 | 历史上游分支链接可进一步固定版本；部署边界见章节 |
| 06 | 一致性哈希 | [章节](../consistent-hashing/docs/chapter06-review.md) · [源码对照](../consistent-hashing/docs/open-source-review_cn.md) · [验证](../consistent-hashing/benchmarks/REPORT.md) | 已见完整复审记录 | 历史上游分支链接可进一步固定版本；放置一致性不等于数据复制 |
| 07 | 键值存储 | [章节](../key-value-store/docs/chapter07-review.md) · [源码对照](../key-value-store/docs/open-source-review_cn.md) · [验证](../key-value-store/benchmarks/REPORT.md) | 已见完整复审记录 | 历史上游分支链接可进一步固定版本；自动供给/安全缩容等仍未实现 |
| 08 | 唯一ID | [章节](../unique-id-generator/docs/chapter08-review.md) · [源码对照](../unique-id-generator/docs/open-source-review_cn.md) · [验证](../unique-id-generator/benchmarks/REPORT.md) | 已见完整复审记录 | 历史上游分支链接可进一步固定版本；身份与重启安全仍有部署前提 |
| 09 | 短链接 | [章节](../url-shortener/docs/chapter09-review.md) · [源码对照](../url-shortener/docs/open-source-review_cn.md) · [验证](../url-shortener/benchmarks/REPORT.md) | 已见完整复审记录 | 历史上游分支链接可进一步固定版本；生产集群边界见章节 |
| 10 | 网页爬虫 | [章节](../web-crawler/docs/chapter10-review.md) · [源码对照](../web-crawler/docs/open-source-review_cn.md) · [验证](../web-crawler/benchmarks/REPORT.md) | 已见完整复审记录 | 连续取消/所有权修复已验收；分布式部署边界保留 |
| 11 | 通知系统 | [章节](../notification-system/docs/chapter11-review.md) · [源码对照](../notification-system/docs/open-source-review_cn.md) · [验证](../notification-system/benchmarks/REPORT.md) | 已见完整复审记录 | 真实供应商、共享队列及生产指标边界见章节 |
| 12 | 新闻动态 | [章节](../news-feed-system/docs/chapter12-review.md) · [源码对照](../news-feed-system/docs/open-source-review_cn.md) · [验证](../news-feed-system/benchmarks/REPORT.md) | 已见完整复审记录 | 深层JSON拒绝/恢复已验收；持久worker确认等仍未部署 |
| 13 | 聊天系统 | [章节](../chat-system/docs/chapter13-review.md) · [源码对照](../chat-system/docs/open-source-review_cn.md) · [验证](../chat-system/benchmarks/results.md) | 已见完整复审记录 | 主套202项与独立HA补测1项分开记录；真实推送/规模边界保留 |
| 14 | 搜索自动补全 | [章节](../search-autocomplete-system/docs/chapter14-review.md) · [源码对照](../search-autocomplete-system/docs/open-source-review_cn.md) · [验证](../search-autocomplete-system/benchmarks/results.md) | 已见完整复审记录 | 当前调用链及生产缺口见章节 |
| 15 | 视频流媒体 | [章节](../video-streaming-system/docs/chapter15-review.md) · [源码对照](../video-streaming-system/docs/open-source-review_cn.md) · [验证](../video-streaming-system/benchmarks/results.md) | 已见完整复审记录 | 常规套件与PostgreSQL/S3补测分开记录，不重复计数 |
| 16 | 文件同步 | [章节](../file-sync-system/docs/chapter16-review.md) · [源码对照](../file-sync-system/docs/open-source-review_cn.md) · [验证](../file-sync-system/benchmarks/results.md) | 已见完整复审记录 | 当前调用链及生产缺口见章节 |
| 17 | 附近商家 | [章节](../proximity-service/docs/chapter17-review.md) · [源码对照](../proximity-service/docs/open-source-review_cn.md) · [验证](../proximity-service/benchmarks/results.md) | 已见完整复审记录 | 当前调用链及生产缺口见章节 |
| 18 | 附近好友 | [章节](../nearby-friends-system/docs/chapter18-review.md) · [源码对照](../nearby-friends-system/docs/open-source-review_cn.md) · [验证](../nearby-friends-system/benchmarks/results.md) | 已见完整复审记录 | 当前调用链及生产缺口见章节 |
| 19 | 地图系统 | [章节](../maps-system/docs/chapter19-review.md) · [源码对照](../maps-system/docs/open-source-review_cn.md) · [验证](../maps-system/benchmarks/results.md) | 已见完整复审记录 | 选定合成路网与真实地图部署边界见章节 |
| 20 | 分布式消息队列 | [章节](../distributed-message-queue/docs/chapter20-review.md) · [源码对照](../distributed-message-queue/docs/open-source-review_cn.md) · [验证](../distributed-message-queue/benchmarks/results.md) | 已见完整复审记录 | 当前调用链及生产缺口见章节 |
| 21 | 指标监控与告警 | [章节](../metrics-monitoring-system/docs/chapter21-review.md) · [源码对照](../metrics-monitoring-system/docs/open-source-review_cn.md) · [验证](../metrics-monitoring-system/benchmarks/results.md) | 已见完整复审记录 | 当前调用链及生产缺口见章节 |
| 22 | 广告点击聚合 | [章节](../ad-click-aggregation-system/docs/chapter22-review.md) · [源码对照](../ad-click-aggregation-system/docs/open-source-review_cn.md) · [验证](../ad-click-aggregation-system/benchmarks/results.md) | 已见完整复审记录 | 当前调用链及生产缺口见章节 |
| 23 | 酒店预订 | [章节](../hotel-reservation-system/docs/chapter23-review.md) · [源码对照](../hotel-reservation-system/docs/open-source-review_cn.md) · [验证](../hotel-reservation-system/benchmarks/results.md) | 已见完整复审记录 | 合成支付及未知响应边界见章节 |
| 24 | 分布式邮件 | [章节](../distributed-email-service/docs/chapter24-review.md) · [源码对照](../distributed-email-service/docs/open-source-review_cn.md) · [验证](../distributed-email-service/benchmarks/results.md) | 已见完整复审记录 | 内部去重不等于互联网恰好一次；真实SMTP部署边界见章节 |
| 25 | 类S3对象存储 | [章节](../object-storage-system/docs/chapter25-review.md) · [源码对照](../object-storage-system/docs/open-source-review_cn.md) · [验证](../object-storage-system/benchmarks/results.md) | 已见完整复审记录 | 当前调用链及生产缺口见章节 |
| 26 | 游戏排行榜 | [章节](../gaming-leaderboard-system/docs/chapter26-review.md) · [源码对照](../gaming-leaderboard-system/docs/open-source-review_cn.md) · [验证](../gaming-leaderboard-system/benchmarks/results.md) | 已见完整复审记录 | 已有真实Redis记录；生产规模仍未证明 |
| 27 | 支付系统 | [章节](../payment-system/docs/chapter27-review.md) · [源码对照](../payment-system/docs/open-source-review_cn.md) · [验证](../payment-system/benchmarks/REPORT.md) | 已见完整复审记录 | 合成资金；供应商/银行原子性与生产边界见章节 |
| 28 | 数字钱包 | [章节](../digital-wallet-system/docs/chapter28-review.md) · [源码对照](../digital-wallet-system/docs/open-source-review_cn.md) · [验证](../digital-wallet-system/benchmarks/REPORT.md) | 已见完整复审记录 | 账户事件副本、Raft/自动选主等仍未实现 |
| 29 | 证券交易所 | [章节](../stock-exchange-system/docs/chapter29-review.md) · [源码对照](../stock-exchange-system/docs/open-source-review_cn.md) · [验证](../stock-exchange-system/benchmarks/REPORT.md) | 已见完整复审记录 | 定序器HA、mmap/FIX及生产清算等仍未实现 |

## 证据使用方式

第5–9章的旧对照虽多用上游分支链接，正文已说明具体阅读的函数/文件、本地取舍和行为验证，因此不能据此误记为“没处理”。其中第9章明确区分YOURLS源码实读与Shlink仅核对README；第5章也区分源码和配置文档。后续固定这些链接可改善可追溯性，但不会把已有审查变成未审查。

根README和旧总进度表可能保留较早的测试计数，应以各章最新的审查和原始验证记录为准。测试数量也不应跨重叠补测直接相加；本清点不生成一个未经重新执行的“全仓最新通过总数”。

本轮文档收尾验证：27份案例指南生成检查通过；第2、3章共52个Python文件通过Python3.10语法解析，相关源码/新增回归Ruff及7个修改Python文件格式检查通过。30份相关Markdown的691个本地链接、JSON与diff空白检查通过；两章共6张Mermaid学习图已本地渲染并查看。根目录同步更正了第5章旧测试计数和第12章文档缺失的过期说明。

[返回中文总览](../README_CN.md) · [GitHub实现索引](github-implementations_cn.md) · [案例学习导航](learning-guide_cn.md)
