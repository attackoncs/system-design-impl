# File Sync System

Chapter 16, Design Google Drive: encrypted block storage, resumable uploads,
immutable revisions, delta sync, conflict copies, sharing and durable notifications.
[中文说明](README_CN.md).

## Run

From this directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev]'
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python examples/network_demo.py
```

The demo starts an actual API process and separate device processes, uploads a
three-block file, changes only its middle block, verifies selective download and
restarts the API to verify durable device progress. It stops its own processes
and removes its own temporary directory. No cloud account is required.

For persistent use:

```powershell
python -m file_sync.cli --root .runtime/server token alice
python -m file_sync.cli --root .runtime/server api --port 8081
```

Keep the owner token private; provision it outside HTTP. Supply it in
`Authorization: Bearer TOKEN`. `POST /space` with `{"name":"Personal"}` creates a
workspace; save the returned workspace ID. Set `FILE_SYNC_TOKEN` for device CLI:

```powershell
python -m file_sync.cli push --folder .runtime/laptop --device laptop --space WORKSPACE_ID document.txt
python -m file_sync.cli pull --folder .runtime/desktop --device desktop
python -m file_sync.cli delete --folder .runtime/laptop --device laptop --space WORKSPACE_ID document.txt
```

Files live directly beneath each folder. The reserved `.sync` directory stores
plaintext device block cache and progress; encrypt the device disk if required.
Server objects are compressed and AES-256-GCM encrypted. The metadata database
contains the master key and needs protected, consistent backups. This is not
end-to-end encryption or KMS. Tokens are hashed in server metadata; device CLI
reads credentials from the environment.

## API

| Method/route | Purpose |
| --- | --- |
| GET /config | Authenticated block-size configuration |
| POST /begin | Declare space/path/size/checksum/blocks/base/request |
| GET /upload?upload=ID | Resume state and missing digests |
| PUT /block?upload=ID&digest=SHA256 | Raw bounded plaintext block |
| POST /commit or /cancel | Session publication or reservation release |
| GET /metadata?file=ID&revision=ID | Current or historical manifest |
| GET /block?file=ID&revision=ID&index=N | Authorized plaintext block |
| GET /files?space=ID or /revisions?file=ID | Files and revision history |
| POST /delete | Conditional tombstone: file/base |
| POST /restore | Historical content: file/revision/base/request |
| POST /share | Workspace space/recipient/role (read/write/revoke) |
| POST /device | Register a device identifier |
| GET /changes?device=ID&cursor=N&wait=25 | Durable paginated long poll |
| POST /ack | Acknowledge device/cursor after applying events |

POST bodies are JSON. Each manifest block has `digest` and `size`. Default chunks
are 4 MiB; files are capped at 10 GiB. `request` identifies a stable upload attempt;
reuse it with identical declarations after a lost response. `base` is the expected
revision or null for creation. Stale updates create separate conflict files; stale
deletes return 409. Incomplete uploads stay hidden until checksums validate and
publication, references and events commit together. Sharing is at workspace level.

Logical quota counts retained revision bytes plus open uploads, independently of
physical dedup savings. Dedup is scoped to workspace, with separate encryption
keys derived from the server master. Fixed-size blocks reuse aligned unchanged
chunks; byte insertions may shift every following block.

## Persistence and recovery

SQLite WAL transactions serialize reference API mutations across processes sharing
the database/storage. Metadata reads use consistent snapshots; no stale ACL cache.
Persistent device cursors and event delivery replay missed notifications. The client
resumes upload sessions, validates download hashes, writes files atomically and
preserves local dirty content before applying remote changes. Keep one client
process per device directory and use reasonably short roots on Windows.

Call `Drive.prune(token, file, keep=5)` explicitly to retain fewer revisions; pending
events for pruned revisions fall back to the current head. `cleanup` expires old
uploads and sweeps grace-aged unreferenced blocks/orphans. The administrative
`archive KEY` and `restore-block KEY` CLI demonstrate a local cold tier; archived
blocks return 423 until restored. There is no cloud Glacier integration.

The device is an explicit push/pull client, with no native OS watcher. Multiple
workspaces with overlapping paths should use separate device folders. Events remain
unbounded in this reference; production needs retention/resnapshot protocols and
bounded cache policy. The service uses local files/SQLite, not metadata HA or
replicated S3 storage. It does not establish the book's traffic or durability scale.
Remote deployment needs HTTPS; the demo binds to loopback by default.

See [design](docs/design.md) and [verified measurements](benchmarks/results.md).
Reproduce network evidence with `python tools/benchmark.py --output .runtime/sync.json`.
