"""Read complete JSONL records; durable local source checkpoint follows remote ACK.

On a lost ACK the same event IDs replay safely. File prefix hashing detects rotation
or mutation rather than silently skipping a replaced/truncated file.
"""

import hashlib
import json
from pathlib import Path
import os

from .common import Error, dumps, http_json


def ingest_file(config, source, checkpoint):
    source, checkpoint = Path(source), Path(checkpoint)
    old = (
        json.loads(checkpoint.read_text())
        if checkpoint.exists()
        else {"offset": 0, "prefix": hashlib.sha256(b"").hexdigest()}
    )
    offset = old["offset"]
    hasher = hashlib.sha256()
    with source.open("rb") as stream:
        remaining = offset
        while remaining:
            chunk = stream.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise Error(409, "source truncated; choose a new checkpoint")
            hasher.update(chunk)
            remaining -= len(chunk)
        if hasher.hexdigest() != old["prefix"]:
            raise Error(409, "source prefix changed; choose a new checkpoint")
        total = 0
        while True:
            batch, end, next_hash = [], offset, hasher.copy()
            for _ in range(200):
                line = stream.readline(8193)
                if not line:
                    break
                if len(line) > 8192:
                    raise ValueError("JSONL record exceeds 8192 bytes")
                if not line.endswith(b"\n"):
                    # Do not checkpoint the partial last record.
                    break
                batch.append(json.loads(line))
                next_hash.update(line)
                end += len(line)
            if not batch:
                return {"records": total, "offset": offset}
            http_json(
                config["ingress_url"] + "/events",
                config["write_token"],
                {"events": batch},
            )
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            tmp = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
            with tmp.open("w") as handle:
                handle.write(dumps({"offset": end, "prefix": next_hash.hexdigest()}))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, checkpoint)
            hasher, offset = next_hash, end
            total += len(batch)
            # A partial record may have been consumed while filling this batch.
            stream.seek(offset)
