"""Canonical upload declarations shared by validation and commit paths."""

import json
import re


def normalize(size, checksum, blocks, block_size):
    if (
        type(size) is not int
        or not 0 <= size <= 10 * 1024**3
        or not isinstance(checksum, str)
        or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        or not isinstance(blocks, list)
        or len(blocks) > 10240
    ):
        raise ValueError("invalid upload declaration")
    normalized, lengths = [], {}
    for index, block in enumerate(blocks):
        if (
            not isinstance(block, dict)
            or set(block) != {"digest", "size"}
            or not isinstance(block["digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", block["digest"]) is None
            or type(block["size"]) is not int
            or not 0 < block["size"] <= block_size
            or (index < len(blocks) - 1 and block["size"] != block_size)
        ):
            raise ValueError("invalid block manifest")
        if block["digest"] in lengths and lengths[block["digest"]] != block["size"]:
            raise ValueError("one digest cannot declare different lengths")
        lengths[block["digest"]] = block["size"]
        normalized.append({"digest": block["digest"], "size": block["size"]})
    if sum(b["size"] for b in normalized) != size:
        raise ValueError("manifest size mismatch")
    return json.dumps(normalized, sort_keys=True)
