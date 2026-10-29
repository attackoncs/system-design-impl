# Requirements: Proximity Service

Source: chapter 17 of the repository's System Design Interview.md.

## Contract
- Return every published business within the requested spherical radius, sorted by distance then ID. Radius in meters: 500, 1000, 2000, 5000 (default), 20000. Never expand it automatically.
- Validate finite latitude [-90,90], longitude [-180,180]; support cell borders, antimeridian and poles.
- Authenticated owners create, replace and delete their own businesses. Required idempotency keys survive restart; key reuse with different requests conflicts.
- Draft changes become visible together at the next UTC day publication; the primary CLI runs an owned background publisher without requiring read traffic. Search and public details use the same immutable version. Deleted/moved records remain visible in the old version until publication.
- Category and UTC weekly opening-time filters are optional. Pagination pins the snapshot and query; old snapshot cursors conflict after publication.
- Persist drafts, snapshots, indexes and replay records; transaction failure must leave the previous complete version readable.
- Bounded TTL caches hold geohash candidate IDs and versioned details, never precise user coordinates.
- Provide real HTTP primary, independently stored pull replicas and region-aware read routing with failover. Logical shards are implemented locally; physical shard distribution and cloud regional deployment remain explicit extensions.

## Acceptance
Use standard-library Python, SQLite and repeatable unittest tests. Validate brute-force spatial equivalence, restart, authorization, replay, snapshot rollback, cache TTL, HTTP errors, independent-process replication and replica outage. Measure synthetic dense/sparse datasets and report local evidence only. The book's 200 million businesses, 100 million DAU, 5000 QPS and production availability are design targets, not local acceptance claims.

## Boundaries
Development tokens represent owner identity; production identity/tenant verification, HTTPS, rate limiting, backups, database HA and legal review are required before public deployment. Requests are not access-logged; query coordinates are not persisted. Idempotency records expire after 7 days and are pruned on subsequent writes; old snapshots are removed at publication. No location history is collected. There is no public draft-reading API.
