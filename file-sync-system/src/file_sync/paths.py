"""Portable relative paths used by both server and device clients."""

import re
import unicodedata


def live_path_conflict(db, space, file, key):
    """Check the folded file name, file ancestors, and live descendants."""
    names = [key[:i] for i, char in enumerate(key) if char == "/"] + [key]
    placeholders = ",".join("?" for _ in names)
    # In binary order [key + '/', key + '0') contains exactly the descendants.
    collision = db.execute(
        f"SELECT 1 FROM files WHERE space=? AND live_path_key IN ({placeholders}) AND id<>? "
        "UNION ALL SELECT 1 FROM files WHERE space=? AND live_path_key>=? AND live_path_key<? "
        "AND id<>? LIMIT 1",
        (space, *names, file, space, key + "/", key + "0", file),
    ).fetchone()
    return collision is not None


def safe_path(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 240
        or value != unicodedata.normalize("NFC", value)
    ):
        raise ValueError("invalid relative path")
    parts = value.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)
    }
    if any(
        (p in ("", ".", "..") or p.casefold() == ".sync")
        or p.endswith((".", " "))
        or re.search(r'[<>:"\\|?*\x00-\x1f]', p)
        or p.split(".")[0].upper() in reserved
        for p in parts
    ):
        raise ValueError("unsafe relative path")
    return value
