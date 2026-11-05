# 第26章开源源码对照与审查记录

日期：2026-11-05。对应仓库《System Design Interview.md》第26章（12814–13481行）。
本页记录实际搜索与阅读的上游代码；本地最终验收与运行数据见
[章节记录](chapter26-review.md)及[验证报告](../benchmarks/results.md)。

## GitHub搜索与固定版本

实际搜索了 `redis leaderboard python`、`agoragames leaderboard-python`。
旧链接 `agoragames/leaderboard-python` 已迁移至 `WB-Games/leaderboard-python`；
检索时新仓库标为归档，适合作为历史源码对照，不直接等同当前可部署依赖。
另选择原章直接引用的 Redis 服务端作为第二份实现，固定到本地既有实测版本8.0.5，
避免把持续变化的 `unstable` 分支结构套用到本地版本。

| 项目 | 固定提交 | 实际阅读的主要文件 |
| --- | --- | --- |
| [WB-Games/leaderboard-python](https://github.com/WB-Games/leaderboard-python) | `4bc028164c259c3a31c7e6ccb2a8ecb83cfb1da2` | `leaderboard.py`、`competition_ranking_leaderboard.py`、`tie_ranking_leaderboard.py`，及竞争排名测试 |
| [redis/redis](https://github.com/redis/redis) 8.0.5 | `214a4905c3edb6b69ac9a9c5d769b7d3fb81ed54` | `src/t_zset.c` 的排名、计数和同分排序；`src/script.c` 的脚本写入状态检查 |

只阅读了所列源码，未安装、构建、执行上游库或复制其代码。本页不是完整上游审计；
本地是否正确仍要由自己的真实Redis、重放与并发测试验证。

## 先分清三种“排名”

假设显示分数为100、90、90、80：

| 语义 | 结果 | 含义 |
| --- | --- | --- |
| 唯一显示位置 `position` | 1、2、3、4 | 同分仍须有稳定先后，便于Top10和邻居窗口 |
| 竞争排名 `rank` | 1、2、2、4 | 严格高分玩家数量加1；本项目采用这一语义 |
| 稠密排名 | 1、2、2、3 | 严格更高的不同分数数量加1；不等于本项目竞争排名 |

同分共享rank不表示Top10自动扩展整个同分组。邻居窗口也可以按显示位置选行，
不等于返回上下四个不同分数层级或完整同分组。

## 参考一：排行榜库中的竞争排名与邻居窗口

普通 [`Leaderboard.rank_for_in`](https://github.com/WB-Games/leaderboard-python/blob/4bc028164c259c3a31c7e6ccb2a8ecb83cfb1da2/leaderboard/leaderboard.py#L462)
使用 `ZRANK` / `ZREVRANK` 加1，它得到的是唯一的排序位置，不能直接满足原章“同分同名次”。
[`CompetitionRankingLeaderboard.rank_for_in`](https://github.com/WB-Games/leaderboard-python/blob/4bc028164c259c3a31c7e6ccb2a8ecb83cfb1da2/leaderboard/competition_ranking_leaderboard.py#L8)
改用排他的分数范围计数；降序时统计严格大于该玩家分数的人数，再加1。
其[测试](https://github.com/WB-Games/leaderboard-python/blob/4bc028164c259c3a31c7e6ccb2a8ecb83cfb1da2/test/leaderboard/competition_ranking_leaderboard_test.py#L14)
明确展示了1、1、3、3、5的竞争排名。
另一个 [`TieRankingLeaderboard.rank_for_in`](https://github.com/WB-Games/leaderboard-python/blob/4bc028164c259c3a31c7e6ccb2a8ecb83cfb1da2/leaderboard/tie_ranking_leaderboard.py#L155)
在独立的分数集合上排名，表示不同分数的稠密排名；名称相近也不能混用。

[`top_in`](https://github.com/WB-Games/leaderboard-python/blob/4bc028164c259c3a31c7e6ccb2a8ecb83cfb1da2/leaderboard/leaderboard.py#L931)
委托按位置范围读取；[`around_me_in`](https://github.com/WB-Games/leaderboard-python/blob/4bc028164c259c3a31c7e6ccb2a8ecb83cfb1da2/leaderboard/leaderboard.py#L985)
先取成员位置，再计算有界范围。因此窗口位置和最终展示的竞争rank是两件事。
该方法按page size构造窗口，并不等同本项目严格“最多上四行、下四行”的边界规则。

值得借鉴的是明确区分位置、rank及窗口，而不是直接把每个用户的 `ZREVRANK+1` 展示成同分名次。
所读代码按单个leaderboard key操作，不能据此推断跨分片全局rank、
月度全员零分成员、跨请求一致快照或比赛去重。其增量更新路径
[`change_score_for_member_in`](https://github.com/WB-Games/leaderboard-python/blob/4bc028164c259c3a31c7e6ccb2a8ecb83cfb1da2/leaderboard/leaderboard.py#L539)
使用 `ZINCRBY`；这不自带本地比赛ID幂等及持久重放保障。
分数读取与后续计数也不是本地带源水位和epoch的多阶段一致查询，不能仅凭pipeline推断全部查询原子。

## 参考二：Redis 8.0.5中的分数范围与同分顺序

在固定8.0.5版本中，较大的有序集合使用字典与跳表，小集合有紧凑编码。
[`zslGetRank`](https://github.com/redis/redis/blob/214a4905c3edb6b69ac9a9c5d769b7d3fb81ed54/src/t_zset.c#L493)
累计跳表跨度查找成员，同时比较分数与同分成员字节序。
[`zrankGenericCommand`](https://github.com/redis/redis/blob/214a4905c3edb6b69ac9a9c5d769b7d3fb81ed54/src/t_zset.c#L3841)
返回单个成员的排序位置；倒序会同时反转同分成员顺序，所以直接降序读取不等于本地要求的“分数降序、用户ID升序”。

[`zcountCommand`](https://github.com/redis/redis/blob/214a4905c3edb6b69ac9a9c5d769b7d3fb81ed54/src/t_zset.c#L3369)
解析开闭区间；跳表编码通过区间端点的rank计算数量。
本地[查询脚本](../src/gaming_leaderboard/projection_scripts.py)以负分数索引统计严格更高分玩家，
再由[排名合并](../src/gaming_leaderboard/ranking.py)汇总每个分片的计数。
另一个全为零score的字典序索引编码“反向分数＋用户ID”，用于稳定显示位置和邻居候选。
这解释了为何本地需要区分两个索引，而不是简单再调用一次 `ZREVRANK`。

一次本地脚本可以组合一个Redis节点内的操作，但这不是跨分片或SQLite与Redis间的事务。
所读 [`scriptVerifyWriteCommandAllow`](https://github.com/redis/redis/blob/214a4905c3edb6b69ac9a9c5d769b7d3fb81ed54/src/script.c#L394)
还区分了脚本是否已经写过数据；本地仍需在第一次修改之前验证已知类型、位置与分数约束。
比赛收据、绝对分数事件、lineage/position/epoch以及完整读屏障是应用层补充，
不能从Redis的单命令实现推断全局一致性、自动故障切换或端到端恰好一次。

## 原章与本地选择

| 原章关注点 | 本地对应与边界 |
| --- | --- |
| 只由游戏服务加分、胜一场加1 | 独立game令牌、冻结比赛内容、比赛ID收据；不接受客户端任意points，未实现完整反作弊游戏逻辑 |
| 全体玩家和月度新榜 | 注册玩家零分成员、UTC月份、迟到窗口与结算；不是只记录获胜玩家 |
| Top10、指定玩家同分排名、上下邻居 | 跨用户哈希分片候选合并、严格高分计数及稳定位置；不返回缺失分片的部分完整榜 |
| Redis数据结构与分片 | 实际Redis双索引，应用层用户哈希分片；不是将单个巨型zset自动拆成Redis Cluster槽 |
| 分数区间分片、DynamoDB写分片 | 原章讨论的替代方案；本地没有为同时支持全部后端叠加迁移/索引复杂度 |
| 实时性与恢复 | SQL权威日志、Redis绝对分数投影、源水位屏障、重放/重建及独立核对；不是单纯失败后重复ZINCRBY |
| 用户资料与加分先后破平 | 有源修订号约束的进程内资料缓存；用户ID用于稳定显示，同分rank共享，不采用取得分数时间破平 |
| 高可用与原章规模 | Redis异步副本、手工且须隔离旧主的切换；权威SQL未复制，无自动选举、跨机房或2500加分QPS证明 |

原章倾向固定分数区间分片，本地选择了原章也讨论的用户哈希方案，并补足精确全局计数和窗口。
这是明确的工程取舍，不是宣称两个分片方案都已实现。当前10000玩家与每人最多10^9胜场
属于参考实现上限；25M月活、独立故障域可用性与持续吞吐仍需额外验证。

## 本次验收与文档修正

结合CodeGraph及日志、排名、投影、Lua/RESP和HTTP生命周期专项检查，本轮未发现需要修复的
确认生产代码缺陷，未修改生产代码或测试。完整严格验收 **72项通过，36.409秒**，
启用 `-W error`、无跳过，包含真实Redis8.0.5/WSL主备、AOF与重放测试。
七进程示例及60场比赛、每类40次查询的基准通过；具体结果与限制见本页开头的验证报告。

本轮纠正了旧“查询串行”说明：查询共享源读门，写入/重建/对账独占；
内部feed/status保留推进能力。同步更新中英文文档、GitHub迁移链接及宏观到源码导读，
3张图已实际渲染验证。不将单节点排行榜库、真实Redis单命令或本机基准外推为生产全局保证。
