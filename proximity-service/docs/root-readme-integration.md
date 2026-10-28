# Shared root integration

Updated 2026-10-25. The original parallel task prepared this note without changing
shared root files. The coordinating review has now updated both root READMEs and
[deep review progress](../../DEEP_REVIEW_PROGRESS.md).

## Current entries

Chapter 17: [Proximity Service](../README.md) — core reviewed: geohash search,
owner CRUD with replay, background daily publication and CAS, isolated/coalesced
caches, bounded HTTP, independent replicas and region-confined read failover.
The complete suite has 76 passing tests. Dated measurements and limits are in
[validation](../benchmarks/results.md).

第17章：[附近商家服务](../README_CN.md)——核心链路已核对：后台每日发布与CAS、
隔离/合并缓存、有界HTTP与区域切换、独立副本；76项测试通过。
物理分片、云地域、数据库HA及书中规模仍为后续扩展。

## Submission convention

This review did not stage, commit, push or switch branches. The existing local
convention for requirements/tasks documents is retained. Any future submission
must follow the user's authorized scope and preserve unrelated local changes.
