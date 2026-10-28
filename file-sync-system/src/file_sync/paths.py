"""Portable relative paths used by both server and device clients."""

import re
import unicodedata


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
