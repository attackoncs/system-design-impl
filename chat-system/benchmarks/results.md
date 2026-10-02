# Local capacity and failover results

Measured 2026-10-02 on Windows 11 (16 logical CPUs), Python 3.13.9, Redis 8.0.5
in Ubuntu WSL. Two independent Python chat processes and 1,000 actual WebSocket
clients ran on the same computer. Redis used one primary, two replicas and three
Sentinels; after the separate crash test, the load runs used the promoted primary
and one surviving replica. Every confirmed write required one replica (`WAIT`).
All data servers used AOF with `appendfsync always`. This is a local reference
measurement, not a production capacity estimate or a multi-region failure test.

Each load run used 500 direct-chat pairs, 10,000 messages of 128 ASCII bytes,
20 concurrent publishers and application heartbeats. Confirmation throughput
counts completed send responses; it excludes initial connection admission and
final inbox verification. Fault-run duration includes forced reconnect time.
Live latency runs from client submission to receipt of the WebSocket message hint.
Every acknowledged message ID was checked in both recipient devices' durable inboxes.

| Metric | Normal load | Chat-node termination |
| --- | ---: | ---: |
| Admitted WebSockets | 1000 | 1000 |
| Acknowledged messages | 10000 | 10000 |
| Messages/second | 538.12 | 468.53 |
| Send p50 / p95 / p99 (ms) | 34.551 / 55.383 / 62.92 | 37.143 / 63.196 / 78.433 |
| Live delivery p50 / p95 / p99 (ms) | 47.107 / 78.151 / 94.08 | 57.928 / 100.893 / 122.918 |
| Missing durable deliveries | 0 | 0 |
| Missing / duplicate live hints | 0 / 0 | 0 / 0 |
| Send errors / retries | 0 / 0 | 0 / 0 |

The fault harness killed its own second chat process after 5,000 sends, then moved
its 500 clients to the surviving process in 0.906 seconds.
It controls that reconnect phase directly; it does not measure discovery timeout.
Independent regression tests separately validate discovery lease expiry and the
reconnecting client's durable cursor recovery. Live hints may be lost or replayed
during a crash; durable sync is the acceptance criterion.

A separate real Redis-primary process kill promoted a replica from port
17379 to 17380, with continued message confirmation after
2.41 seconds. Two-replica confirmation preceded the crash.
Existing authenticated sessions continued cross-node chat, retained their inboxes,
and received new live messages. Retrying the original message produced no new
message, broker event or push candidate. Pausing the surviving replica caused an
idempotent retry to return an uncertain result; after resumption the same retry
succeeded without duplication. This verifies one particular failure path and does
not guarantee zero loss for simultaneous primary/replica failures or partitions.

## Reproduction

Install `pip install -e ".[dev]"`. Start a disposable lab using `tools/redis_lab.py`
on Linux/macOS, or configure your existing Sentinel endpoints. On Windows/WSL,
Redis data must reside on a Linux filesystem; the optional control directory can
reside on the Windows filesystem. The lab must be fresh for each crash test.

```bash
python tools/load_test.py --sentinels 127.0.0.1:27379,127.0.0.1:27380,127.0.0.1:27381 --connections 1000 --messages 10000 --delivery-timeout 60 --output .runtime/load.json
python tools/load_test.py --sentinels 127.0.0.1:27379,127.0.0.1:27380,127.0.0.1:27381 --connections 1000 --messages 10000 --delivery-timeout 2 --kill-node --output .runtime/fault.json
```

With both `CHAT_TEST_REDIS_URL` and `CHAT_TEST_REDIS_LAB` configured, the final
suite completed **58 passed** (no skips). This includes atomic capacity rejection
under concurrent admission, device movement releasing previous-node capacity,
node-epoch separation, bounded session expiry, primary promotion, continued chat,
and replica-confirmation failure. Python 3.9 syntax parsing also passed; actual
runtime tests used Python 3.13. Compose YAML parsed with ten services and six
persistent Redis/Sentinel volumes. Docker is unavailable here, so Docker image
build/start and container restart persistence have not been executed.

Raw reports: [load](load-1000.json), [chat-node fault](node-fault-1000.json),
[Sentinel failover](sentinel-failover.json).

The shared namespace remains one Redis hash slot. This work does not implement
storage sharding, cross-region quorum, automatic retention, or prove the chapter's
50-million-daily-user target. Larger workloads can use the same configurable tool;
the measured throughput must not be extrapolated linearly to that scale.
