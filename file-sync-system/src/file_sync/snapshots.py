"""Keyset paging for device recovery. File paths are stable within a workspace."""

import re

from .paths import safe_path

PAGE_SIZE = 256


def validate_page(space, limit, after, cursor):
    if limit is not None and (type(limit) is not int or not 1 <= limit <= PAGE_SIZE):
        raise ValueError("invalid snapshot page size")
    if after is None and cursor is None:
        return
    if (
        limit is None
        or type(cursor) is not int
        or not 0 <= cursor < 2**63
        or not isinstance(after, list)
        or len(after) != 2
        or not isinstance(after[0], str)
        or not re.fullmatch(r"[0-9a-f]{32}", after[0])
        or (space is not None and after[0] != space)
    ):
        raise ValueError("invalid snapshot continuation")
    safe_path(after[1])


def file_heads(db, user, space, after, limit):
    """Read one extra head to distinguish the final page, using files(space,path)."""
    previous_space, previous_path = after or ("", "")
    spaces = (
        [(space,)]
        if space is not None
        else db.execute(
            "SELECT space FROM members WHERE user=? AND space>=? ORDER BY space",
            (user, previous_space),
        )
    )
    files = []
    for (current,) in spaces:
        start = previous_path if current == previous_space else ""
        rows = db.execute(
            "SELECT f.id AS file,f.space,f.path,f.head AS revision,r.version,r.deleted "
            "FROM files f JOIN revisions r ON r.id=f.head "
            "WHERE f.space=? AND f.path>? ORDER BY f.path LIMIT ?",
            (current, start, -1 if limit is None else limit + 1 - len(files)),
        )
        for row in rows:
            if limit is not None and len(files) == limit:
                return files, [files[-1]["space"], files[-1]["path"]]
            files.append({**row, "deleted": bool(row["deleted"])})
    return files, None


def decode_page(response, request, minimum_cursor):
    """Check the watermark and forward progress before applying a server page."""
    if not isinstance(response, dict):
        raise OSError("invalid snapshot")
    high, files = response.get("cursor"), response.get("files")
    if (
        type(high) is not int
        or not minimum_cursor <= high < 2**63
        or high != request.get("cursor", high)
        or not isinstance(files, list)
        or ("limit" in request and len(files) > request["limit"])
    ):
        raise OSError("invalid snapshot")
    after = response.get("next")
    if after is not None:
        try:
            validate_page(request.get("space"), request.get("limit"), after, high)
        except ValueError as exc:
            raise OSError("invalid snapshot continuation") from exc
        if (
            not files
            or not isinstance(files[-1], dict)
            or after != [files[-1].get("space"), files[-1].get("path")]
            or after <= request.get("after", [])
        ):
            raise OSError("snapshot continuation did not advance")
    return high, files, after
