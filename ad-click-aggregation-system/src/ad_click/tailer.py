"""Tail complete JSONL records; local position follows a verified remote ACK.

A lost ACK replays stable IDs. Prefix hashing detects changed/truncated history.
"""

import hashlib
from pathlib import Path

from .checkpoints import checkpoint_lock, read_checkpoint, same_file, write_checkpoint
from .common import Error, http_json, integer, text
from .records import MAX_POSITION, strict_json


def _acknowledged(response, count, lineage):
    if not isinstance(response, dict):
        raise Error(503, "invalid ingress acknowledgement")
    current = text(response.get("lineage"), "lineage")
    if lineage is not None and current != lineage:
        raise Error(409, "ingress lineage changed; choose a new checkpoint")
    receipts = response.get("receipts")
    if not isinstance(receipts, list) or len(receipts) != count:
        raise Error(503, "incomplete ingress acknowledgement")
    for receipt in receipts:
        if not isinstance(receipt, dict) or type(receipt.get("duplicate")) is not bool:
            raise Error(503, "invalid ingress receipt")
        integer(receipt.get("partition"), "partition", 0, 63)
        integer(receipt.get("seq"), "sequence", 1, MAX_POSITION)
    return current


def ingest_file(config, source, checkpoint, max_batches=1000):
    integer(max_batches, "max_batches", 1, 10000)
    source, checkpoint = Path(source), Path(checkpoint)
    lock = checkpoint.with_name(checkpoint.name + ".lock")
    if same_file(source, checkpoint) or same_file(source, lock):
        raise ValueError("source and checkpoint files must be distinct")
    # Copy trusted endpoint configuration before queueing or waiting for I/O.
    url, token = config["ingress_url"], config["write_token"]
    if not isinstance(url, str) or not isinstance(token, str):
        raise ValueError("ingress endpoint and credential required")
    with checkpoint_lock(checkpoint):
        old = read_checkpoint(checkpoint)
        return _ingest(url, token, source, checkpoint, old, max_batches)


def _ingest(url, token, source, checkpoint, old, max_batches):
    offset, lineage = old["offset"], old.get("lineage")
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
        for _batch in range(max_batches):
            batch, end, next_hash = [], offset, hasher.copy()
            for _record in range(200):
                line = stream.readline(8193)
                if not line:
                    break
                if len(line) > 8192:
                    raise ValueError("JSONL record exceeds 8192 bytes")
                if not line.endswith(b"\n"):
                    break  # A partial last record is never checkpointed.
                batch.append(strict_json(line))
                next_hash.update(line)
                end += len(line)
            if not batch:
                return {"records": total, "offset": offset, "limit_reached": False}
            response = http_json(url.rstrip("/") + "/events", token, {"events": batch})
            lineage = _acknowledged(response, len(batch), lineage)
            write_checkpoint(
                checkpoint,
                {"offset": end, "prefix": next_hash.hexdigest(), "lineage": lineage},
            )
            hasher, offset = next_hash, end
            total += len(batch)
            # A partial line may have been read while filling this page.
            stream.seek(offset)
        return {"records": total, "offset": offset, "limit_reached": True}
