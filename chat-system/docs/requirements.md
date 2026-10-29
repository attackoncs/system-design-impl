# Requirements Document: Chat System

## Introduction

Implement Chapter 13, **Design A Chat System**, from the repository's local
`System Design Interview.md`. Follow the independent Python package structure of
the preceding chapters. The book's 50 million daily active users is an architectural
design target, not a throughput claim for this single-process reference package.

## Glossary

- **Channel**: A two-user direct conversation or a group of at most 100 users.
- **Session**: An authenticated connection belonging to one user and device.
- **Inbox**: The durable ordered message references visible to a user.
- **Cursor**: The highest message ID successfully processed by a device.
- **Presence**: Whether at least one device has a fresh heartbeat.
- **Push adapter**: An asynchronous integration for notifying an offline recipient.

## Functional Requirements

### R1: Authentication and device sessions

**User story:** As a user, I can connect multiple devices without impersonating another user.

1. Tokens SHALL be provisioned outside the public WebSocket protocol.
2. An invalid or revoked token SHALL fail authentication.
3. Identity SHALL come from the authenticated session, never a supplied sender field.
4. Different devices SHALL receive independently; reconnecting the same device SHALL replace its old session.
5. Closed, replaced, and expired sessions SHALL be unable to send or retrieve messages.

### R2: Direct and group conversations

**User story:** As a participant, I can create private conversations and small groups.

1. A direct channel SHALL contain exactly two distinct users and be reused for the same pair.
2. A group SHALL have between two and 100 distinct members including its creator.
3. Only the group owner SHALL add members; duplicate additions SHALL be harmless.
4. Only members SHALL send messages or retrieve channel history.
5. New members SHALL receive messages sent after joining; earlier history SHALL remain private.

### R3: Text delivery, ordering, and retries

**User story:** As a sender, I can retry after network failure without duplicating messages.

1. Messages SHALL contain nonblank text of fewer than 100,000 characters.
2. A committed message SHALL have a unique, increasing ID and a timestamp.
3. The service SHALL atomically persist the message and each member's inbox entry before acknowledgement.
4. Reusing a sender's client message ID with identical content/channel SHALL return the existing message.
5. Reusing that ID with different content/channel SHALL fail without mutation.
6. Every connected device, including the sender's other devices, SHALL receive live events.
7. A full device queue SHALL close that session with an explicit resynchronization instruction.

### R4: Durable history and device synchronization

**User story:** As a returning user, I can retrieve missed messages after reconnect or server restart.

1. A file-backed store SHALL retain channels, membership, messages, and inboxes across restarts.
2. Each device SHALL supply its own cursor; the server SHALL return accessible messages with larger IDs.
3. Pages SHALL be ordered by ID and bounded to 1–1,000 messages; invalid cursors SHALL fail.
4. Device cursors SHALL advance only after the client processes a sync page; a live event alone SHALL not imply all preceding messages were processed.

### R5: Presence

**User story:** As a user, I can see subscribed contacts' status without offline flicker during brief network interruptions.

1. Presence SHALL aggregate all devices and use an injectable monotonic clock.
2. Missing heartbeats for the configurable timeout (default 30 seconds) SHALL expire the device.
3. Explicit logout SHALL close immediately; abrupt transport loss SHALL retain presence until timeout.
4. A client SHALL subscribe to at most 100 user IDs and receive online/offline transitions.
5. Presence subscription authorization is delegated to the embedding application's contact policy; the demo does not contain a social graph.

### R6: Offline notifications

**User story:** As an offline recipient, I can be notified of new messages.

1. An offline recipient other than the sender SHALL trigger the push adapter once per new message.
2. Retrying an existing message SHALL not duplicate the push invocation.
3. Push failure SHALL not undo persistence or prevent sender acknowledgement; failure SHALL be recorded.
4. The default adapter SHALL record invocations only. Real provider delivery and durable push retries are extension work.

### R7: Discovery and WebSocket transport

**User story:** As an integrator, I can expose the core over real WebSocket connections.

1. A reference registry SHALL prefer the requested region, then the least utilized available server.
2. Full or removed servers SHALL not be selected.
3. An optional adapter SHALL support login, heartbeat, conversation creation, member addition, send, sync, history, presence subscriptions, and logout through JSON.
4. Every request SHALL receive a response carrying its request ID; pushed events SHALL be distinguishable from responses.
5. Malformed or unauthorized requests SHALL produce controlled errors without exposing tokens or stack traces.
6. Transport message sizes, authentication timeout, and heartbeat cleanup SHALL be bounded.

## Nonfunctional Requirements

- Python 3.9+; standard-library-only core, optional `websockets` transport.
- Independent src-layout package with English/Chinese README, design, requirements, tasks, examples, and pytest tests.
- SQLite is the local persistence adapter. A separate Redis-backed distributed mode
  SHALL implement cross-server routing and shared leases; neither mode claims
  demonstrated 50-million-user scale.
- Tests SHALL cover persistence restart, authorization, retries, concurrent sends, cursor pagination, slow clients, presence, push failure, discovery, and actual WebSocket communication.
- Documentation SHALL identify local-only defaults, token provisioning, TLS deployment requirements, and recovery semantics.
- Do not commit or push without a subsequent user instruction. Preserve the earlier rule excluding requirements/tasks from commits.

## Distributed extension (user-requested scope)

### R8: Shared message storage and broker

1. Independent chat processes SHALL share Redis-backed channels, membership,
   messages, ordered per-user inboxes, and sender retry keys.
2. Message write, inbox fanout, and durable broker publication SHALL occur atomically.
3. Writes from different nodes SHALL produce unique ordered IDs; retries on another
   node SHALL not duplicate messages or notification jobs.
4. Every chat node SHALL consume the durable event log independently and route events
   to its local authenticated devices. Persist its event cursor after enqueueing.
5. Redis calls SHALL be asynchronous so storage I/O does not block the event loop.

### R9: Shared presence, fencing, and service discovery

1. Node registrations SHALL expire without lease renewal, so failed nodes are
   automatically removed from discovery and online presence.
2. Presence SHALL aggregate fresh device sessions on live nodes, including devices
   connected to different servers.
3. Reconnecting the same device on another node SHALL fence the older connection.
   Its subsequent send/heartbeat/logout SHALL not alter the replacement session.
4. Capacity checks and session registration SHALL be atomic.
5. A discovery API SHALL return the available region/capacity-preferred WebSocket endpoint.
6. On node failure, clients SHALL discover a replacement, authenticate, and synchronize
   from their saved cursor. Supply an executable reconnecting client.

### R10: Durable offline notification processing

1. Message commits SHALL create persistent push candidates. Separate workers SHALL
   check shared presence before delivery and acknowledge completed jobs.
2. Failed notification jobs SHALL be reclaimable after worker failure and retried
   with a bounded delay; exhausted jobs SHALL move to a durable dead-letter stream.
3. Provide a working HTTP webhook delivery adapter with stable idempotency keys;
   credentials and provider endpoint are configured externally.
4. Default recording delivery SHALL persist its records in Redis across worker restart.
5. External delivery is at least once; providers must honor idempotency keys to
   prevent duplicates after crash between delivery and acknowledgement.

### R11: Deployment and distributed validation

1. Provide runnable node, discovery, push worker, token provisioning, and reconnect
   client commands plus a container composition with persistent Redis storage.
2. Verify independent OS processes against a real Redis broker: cross-node direct
   and group chat, device synchronization, shared presence, concurrent retry writes,
   stale session fencing, lease failover, reconnect recovery, and worker retries.
3. Preserve all local-mode tests. Document Redis availability/durability and untested
  capacity limits without claiming automatic Redis-primary failover.

## High availability and capacity extension

### R12: Automatic Redis failover

1. Backend clients SHALL optionally discover the writable primary through multiple
   Sentinel endpoints, reconnect after promotion, and never read history from a stale replica.
2. Supply one primary, two AOF-enabled replicas, and three Sentinel instances with quorum two.
3. A configurable replication confirmation SHALL execute on the same connection as
   the write; confirmation timeout SHALL return an uncertain/unavailable result,
   allowing a retry with the same message idempotency key.
4. Credential, channel, membership, and message writes SHALL support this confirmation.
5. Verify actual automatic replica promotion after primary process termination,
   retained acknowledged messages, continued sends, and idempotent retry after recovery.
6. Document asynchronous-replication loss windows and avoid a zero-loss/strong-consistency claim.

### R13: Capacity and fault-tolerance validation

1. Provide a configurable real-WebSocket load tool supporting multiple nodes,
   connection count, message count, text size, and concurrent publishers.
2. Record actual admitted connections, successful/error message counts, throughput,
   send acknowledgement latency percentiles, end-to-end delivery percentiles,
   received message IDs and missing/duplicate counts.
3. Load tests SHALL use isolated namespaces, bounded queues, and finite request timeouts.
4. Capacity admission and discovery SHALL avoid scanning all sessions per login or selection.
5. Verify controlled rejection at capacity, recovery through a Redis primary outage,
   and no false acknowledgement without replica confirmation.
6. Report tested hardware/process layout and measured scale. A laptop test SHALL NOT
   be described as proof of 50-million-user production capacity.
