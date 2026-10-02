# Video Streaming System

Chapter 15's executable reference: resumable signed uploads, durable transcoding
DAGs, independent workers, real FFmpeg, encrypted multi-rendition HLS and authorized
HTTP playback. [中文说明](README_CN.md).

## Run

From this directory, in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev,media]'
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python examples\media_demo.py
```

The demo generates a three-second video, uploads two chunks, executes the durable
DAG, and decodes both encrypted renditions over actual HTTP. It removes its own
temporary files and stops its HTTP server on exit. No external account is required.
The optional media dependency supplies FFmpeg when it is absent from PATH.

For persistent operation, use separate terminals:

```powershell
.\.venv\Scripts\python -m video_streaming.cli --root .runtime/video token alice
.\.venv\Scripts\python -m video_streaming.cli --root .runtime/video api --port 8080
.\.venv\Scripts\python -m video_streaming.cli --root .runtime/video worker --name worker-a
.\.venv\Scripts\python -m video_streaming.cli --root .runtime/video worker --name worker-b
```

Keep the issued owner token private. API routes use `Authorization: Bearer TOKEN`:
`POST /uploads` accepts title, size and optional part_size; `PUT /part?grant=...`
accepts the corresponding binary chunk. `POST /renew?video=...` renews upload
grants; `POST /finish?video=...` finalizes; `GET /videos?video=...` reports state;
`POST /playback?video=...` issues a short-lived playback grant. Fetch
`GET /media?grant=...&resource=master.m3u8`; nested manifests and keys carry that
grant. `POST /remove?video=...` revokes access. POST bodies are JSON; optional
finalization checksum is SHA-256. Upload defaults to 4 MiB chunks and a 1 GB limit.

## Recovery and boundaries

SQLite persists upload parts, job dependencies, leases, retry state and a publication
outbox. Lease tokens fence late workers; publication waits for complete object sets.
Workers recover abandoned jobs after expiry. Run the `cleanup` CLI command to sweep
old orphan objects and attempts; live references and recent upload grants are retained.
Tests include a killed independent worker, metadata rollback, missing objects,
unauthorized requests and actual HTTP decryption of FFmpeg outputs.

Local mode uses SQLite and local files. Distributed mode supports PostgreSQL
primary discovery/reconnect and private S3-compatible storage; each API/worker has
its own disk directory. Actual PostgreSQL/Patroni/etcd promotion and Windows clients
against a Linux SeaweedFS server have been tested. See [HA results](benchmarks/ha-results.md).
CDN, GPU scheduling, KMS and commercial DRM remain follow-ups. Remote deployments
need HTTPS, database TLS and managed keys. The book's traffic scale is not established.

See [design](docs/design.md) and [measured results](benchmarks/results.md).
Reproduce the report with `python tools/benchmark.py --output .runtime/benchmark.json`.

## Distributed deployment

Install `.[distributed,media]`. Set `VIDEO_DATABASE_DSN` to a libpq multi-host DSN
with `target_session_attrs=read-write`; e.g. hosts pg-a,pg-b,pg-c with corresponding
ports, database/user/password. Use `sslmode=verify-full` and `sslrootcert` across
untrusted networks. Set `VIDEO_S3_BUCKET`, optional `VIDEO_S3_ENDPOINT` (omit for AWS),
`VIDEO_S3_PREFIX` (default video/), `AWS_DEFAULT_REGION` and SDK credentials.
Create a private bucket before starting. Run the same API/worker commands with
different local `--root` directories on different hosts; never share the SQLite file.

For a Docker Compose lab, generate secrets once (retain them with persisted volumes):

```powershell
python -c "import secrets; from pathlib import Path; p=Path('.runtime'); p.mkdir(exist_ok=True); (p/'ha.env').write_text('\n'.join(k+'='+secrets.token_hex(24) for k in ('VIDEO_DB_PASSWORD','VIDEO_REPLICATION_PASSWORD','VIDEO_PATRONI_PASSWORD','VIDEO_S3_USER','VIDEO_S3_PASSWORD'))+'\n')"
docker compose --env-file .runtime/ha.env -f compose.ha.yaml up --build -d
docker compose --env-file .runtime/ha.env -f compose.ha.yaml run --rm api token alice
```

The lab includes three etcd members, three PostgreSQL/Patroni nodes in strict
synchronous mode, an authenticated SeaweedFS S3 server, API and two workers with
separate volumes. Only the API is published to loopback. Startup retries until the
database is ready; token provisioning can be retried after bootstrap. The lab uses
database superuser/bucket admin credentials for simplicity; assign least-privilege
roles in deployments. The S3 server is a single storage failure domain; use a managed
or replicated S3 service for storage HA. Compose was parsed, but Docker execution was
unavailable here; actual equivalent PostgreSQL/S3 processes were tested via WSL.

All metadata mutations currently take one transaction advisory lock across the
deployment. This preserves invariants and limits write throughput. Lost operations
return 503 and the next request reconnects. In-flight commits may have succeeded;
the driver does not replay them. Clients reconcile status and retry only idempotent
part/finalization operations; credential/video creation is not automatically deduplicated.
Strict synchronous replication can stop writes without an eligible replica/quorum.
Choose failure domains and process fencing/watchdogs for your deployment.

Offline migration (stop every source/target API and worker first):

```powershell
python tools/migrate_metadata.py --source .runtime/video/metadata.sqlite3 --source-objects .runtime/video/objects
```

The target DSN/bucket come from environment. The target metadata must be empty.
Objects transfer before the atomic metadata import; original data stays intact.
Restart all target processes afterwards to reload signing keys. Make a backup first.
Signing/encryption secrets remain persisted metadata; this extension does not provide KMS.

Integration tests: install `.[integration]`, set `VIDEO_TEST_DATABASE_DSN` to a
disposable real cluster. Optionally set `VIDEO_TEST_S3_ENDPOINT`,
`VIDEO_TEST_S3_ACCESS_KEY`, `VIDEO_TEST_S3_SECRET_KEY` for a real S3 service; otherwise
tests start a Moto protocol emulator. Tests create/remove isolated schemas/buckets.
Without a PostgreSQL DSN its integration tests are explicitly skipped.
