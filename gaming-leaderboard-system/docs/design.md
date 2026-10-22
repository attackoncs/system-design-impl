# Monthly gaming leaderboard reference design

## Authority and process boundaries

The lab has seven processes: one authoritative SQLite/HTTP service, two HTTP projection nodes and four real Redis processes (two independently sharded primary/replica pairs). Projection nodes fetch checksummed per-shard events over HTTP and use a standard-library RESP2 client to issue actual Redis commands/Lua. There is no Redis mock, shared-memory ranking substitute or dependency on earlier chapter source code. Tests may reuse an already installed/unpacked Redis executable, which is not bundled.

SQLite WAL with synchronous FULL stores profiles, registration time, seasons, absolute authoritative scores, unique match receipts, and contiguous event sequences for each score shard. Score changes, events and receipt commit in one transaction. A pre-commit failure rolls back all of them. A source lineage UUID and immutable shard/grace configuration survive restart. Missing/recreated authority state is not silently treated as the same dataset.

The authority serializes public mutations and multi-phase reads with a process lock. Internal event feeds bypass that lock so projectors can catch up while a public request waits for its barrier. Thus source scores cannot change between the phases of one global rank query. This deliberately trades throughput for an assessable snapshot model; multiple active authority processes or independently writing clients bypassing this coordinator are unsupported. Authority SQLite has no replication or automatic failover.

## Trusted matches and monthly membership

Only a separate game-service bearer token may submit `{match_id,winner,players,ended_at}`. No points field is accepted; each unique win gives exactly one point. Participants must be distinct, registered before the match ended, and include the winner. This authenticates a trusted result producer; it does not implement game anti-cheat or cryptographic proof that the match occurred.

Match ID uniqueness is global and durable. Participant order is normalized before hashing. Same ID/content returns the original receipt even after a season closes; different content returns 409. The receipt's score is the score at original acceptance, not a new current ranking. Redis loss cannot cause another increment because replay sends absolute scores rather than increment operations.

Seasons are natural UTC months. All registered players, including zero-win players, are seeded into the current month. New month keys start with zero scores and retain older seasons. Historical creation includes only profiles registered before that month's end. Match timestamps determine the season, with at most 30 seconds future skew; future-month matches are rejected. New events are accepted until, but not including, month end + 48 hours. Existing receipts replay afterward without mutation. A later account cannot claim an earlier match.

After the acceptance deadline, an administrative close creates an immutable SQL settlement: full-board digest, player count and Top 10 with ranks/positions. Repeated close returns the same result. Late events after the deadline are rejected even if close has not been called yet. Settlement is authoritative SQL data and does not require trusting Redis. Profiles can change display names without changing historical numeric scores or settlement IDs/hash.

## Redis representation and exact ties

Users are assigned to `first_64_bits(SHA256(canonical_JSON(user_id))) % N`. Each month/shard has two sorted sets:

- Score set: member=user ID, score=negative wins. Ascending score sorts best wins first and ties by ASCII user ID.
- Order set: every score is zero; member=`%010d:(user_id)` using `1000000000 - wins` as the ten-digit prefix. Its lexicographic order is the full display order.

The second set permits global position/neighbor queries without fetching a giant equal-score group. Scores are capped at 10^9, within exact Redis-double integer representation and the fixed lex prefix width. User IDs use bounded ASCII letters/digits/underscore/hyphen so SQL/Python/Redis tie ordering agrees.

Competition rank is `1 + sum(ZCOUNT(score_set, -inf, exclusive(-wins)))` across **all** user shards. Display position is `1 + sum(ZLEXCOUNT(order_set, -, exclusive(target_member)))`. Tied users therefore share rank but occupy unique deterministic positions. `ZRANK` by itself is not used as competition rank.

Top 10 fetches local Top 10 from every shard and merges by descending wins/ascending ID, keeping exactly ten rows. This is exact because each user exists on only one shard; unlike salted aggregation keys, a candidate's score is never split across shards. All globally preceding users are in the merged prefix, so competition ranks can be computed from its first tied position. Top 10 does not expand a tie beyond the row limit.

For a neighbor window, every shard returns at most four entries above and below the target composite member. The service merges to the closest four per side, adds the target, and obtains strict-higher counts for all distinct returned scores. Result rows expose both rank and position. These are neighboring display rows, not whole tied groups or distinct rank levels. Missing any shard fails the global query rather than returning approximate or partial ranks.

## Atomic projection and read barriers

The event feed has shard ownership, source lineage, contiguous local sequence and SHA256 payload checksums. Projectors validate the full page before applying. Lua updates the absolute score, removes the old composite order member, inserts its replacement, and commits lineage/position atomically. Old/stale pages do not add points. Lua prevalidates offsets and key types before mutations; unexpected key corruption stops progress. Idle polling avoids rewriting unchanged cursor/AOF state.

Redis progress is stored alongside its projected data. Lost Redis state starts at position zero and replays all retained authority events, including season registrations at zero. A changed source lineage fails closed until an explicit rebuild. Administrative rebuild clears only this application's namespace/month keys, then the normal projector replays. Old other applications' Redis data is not flushed; FLUSHALL is used only in a test against a wholly owned disposable instance.

Before a public read, the authority captures all current source heads and waits for matching Redis lineage/positions. The public authority lock prevents newer writes during its multi-phase read, so projections reaching those heads give a coherent source barrier across user shards. Normal write responses wait up to roughly one second for projection: 200 means projected, 202 means the match is durably accepted but not yet fully projected. Reads that cannot establish a complete barrier fail 503. Network deadlines are best-effort, not a hard response-time SLA.

Profile names are loaded from the SQL authority for the returned user IDs. There is no profile or Top 10 application cache masking freshness. Internal status exposes projection positions, observed source heads, poll time and errors. Independent reconciliation compares all projected `(user,score)` pairs with authoritative SQL, bounded by the same read barrier. It is an administrative full scan for the 10000-player reference limit, not a production distributed audit engine.

## Primary/replica and recovery semantics

Each Redis pair uses a password, AOF enabled with `appendfsync always`, and real asynchronous REPLICAOF replication. The tested WSL-mounted workspace required `repl-diskless-load swapdb`; pure AOF (`aof-use-rdb-preamble no`) avoids RDB-based disk loading in this lab. This is a per-instance test configuration, not a system-wide kernel change or an availability guarantee.

Projection nodes discover the roles of the two configured endpoints with short ROLE probes. Exactly one reachable master is required. There is no automatic election or Sentinel. To fail over safely, an operator first fences/stops the old primary, then promotes the replica with REPLICAOF NO ONE; a returning old primary must be configured as a replica of the promoted endpoint. Two reachable masters fail closed. The short role-probe deadline prevents an unavailable endpoint/WSL port forwarder from stalling every rank request.

Replication may lag. Promotion of a stale replica does not authorize stale ranks: its cursor must catch up to authority heads, and the journal restores missed absolute updates. AOF tests stop the projector, hard-exit/restart the promoted Redis and verify the saved score directly before restarting projection, distinguishing AOF recovery from journal repair. Separate tests flush an owned Redis dataset, reopen authority, detect deliberately corrupted indexes by reconciliation and rebuild them.

The lab checks the server run ID before stopping an owned Redis and uses its verified process ID for hard-exit injection. WSL children run only under an isolated workspace lab directory. Tests retain local disks; they do not demonstrate cross-host quorum, host/disk loss, AOF power-failure guarantees or authority recovery from lost SQLite files.

## Partitioning alternatives and limits

Score-range partitions can derive rank from counts of higher partitions but require moving a player when a score crosses a boundary; dense score clusters also need range rebalancing. Hashing users avoids migration for ordinary increments but requires scatter/gather global counts and candidates. The extra lex index solves deterministic tie positions efficiently. This is manual application sharding, not Redis Cluster hash-slot distribution of a single giant sorted-set key.

A NoSQL score-sort index would also need write sharding for a hot monthly partition and cross-shard rank aggregation; eventual secondary-index visibility complicates read barriers. No DynamoDB, Cassandra, score-range migration or Redis Cluster implementation/benchmark is claimed. The code implements and validates the hash-sharded algorithm only.

Static admin/read/game/internal credentials are distinct; players cannot directly assign points. Servers and Redis endpoints use loopback. No full account auth, TLS, rate-limit gateway, anti-cheat, authority HA, automatic failover, online topology changes or log/season retention policy is provided. Two indexes, retained receipts/events and historical seasons consume extra space. The lab's 10000-player/10^9-score limits, serialized coordinator and measured latency do not establish the book's 25M MAU/2500 peak-score-QPS target.
