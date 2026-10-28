"""Owned event and source-page contracts shared by log and aggregation."""

import json
import math
import re

from .common import Error, digest, event, integer, text

MAX_POSITION = 2**53


def timestamp(value, label="timestamp"):
    if (
        type(value) not in (int, float)
        or not 0 <= value <= MAX_POSITION
        or not math.isfinite(value)
    ):
        raise ValueError(f"invalid {label}")
    return value


def normalize_event(body):
    # Relative future admission is checked under the log writer; historical
    # normalization and checksum verification must not depend on replay time.
    return event(body, MAX_POSITION - 30)


def prepare_events(bodies):
    if not isinstance(bodies, list) or not 1 <= len(bodies) <= 200:
        raise ValueError("events must contain 1..200 items")
    return [normalize_event(body) for body in list(bodies)]


def prepare_batch(batch, partition):
    if not isinstance(batch, dict) or set(batch) != {
        "lineage",
        "partition",
        "head",
        "entries",
    }:
        raise ValueError("invalid source batch")
    if integer(batch["partition"], "partition", 0, 63) != partition:
        raise Error(409, "wrong partition")
    lineage = text(batch["lineage"], "lineage")
    head = integer(batch["head"], "head", 0, MAX_POSITION)
    entries = batch["entries"]
    if not isinstance(entries, list) or len(entries) > 200:
        raise ValueError("source page requires 0..200 entries")
    result = []
    previous = None
    for source in list(entries):
        if not isinstance(source, dict) or set(source) != {
            "seq",
            "event",
            "accepted",
            "hash",
        }:
            raise ValueError("invalid source entry")
        seq = integer(source["seq"], "seq", 1, MAX_POSITION)
        if seq > head or previous is not None and seq != previous + 1:
            raise Error(409, "source page offset gap or invalid head")
        accepted = timestamp(source["accepted"], "acceptance time")
        value = normalize_event(source["event"])
        if value["click_timestamp"] > int(accepted) + 30:
            raise Error(409, "source event exceeds its acceptance time")
        fingerprint = source["hash"]
        if (
            not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            or digest(value) != fingerprint
        ):
            raise Error(409, "raw log checksum mismatch")
        result.append(
            {"seq": seq, "event": value, "accepted": accepted, "hash": fingerprint}
        )
        previous = seq
    return {"lineage": lineage, "partition": partition, "head": head, "entries": result}


def strict_json(value):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("non-finite JSON number")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def parse_source_row(row):
    """Fail closed before returning corrupt persisted source data over HTTP."""
    try:
        raw = strict_json(row["payload"])
        normalized = normalize_event(raw)
        accepted = timestamp(row["accepted"], "acceptance time")
        if (
            normalized != raw
            or digest(normalized) != row["hash"]
            or normalized["click_timestamp"] > int(accepted) + 30
        ):
            raise ValueError("source data mismatch")
        integer(row["seq"], "seq", 1, MAX_POSITION)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise Error(503, "persisted raw log validation failed") from exc
    return {
        "seq": row["seq"],
        "event": normalized,
        "accepted": accepted,
        "hash": row["hash"],
    }
