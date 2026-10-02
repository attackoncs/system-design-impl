"""Offline SQLite-to-PostgreSQL migration; stop API/workers before running."""
import argparse
import os
from pathlib import Path
import re
import sqlite3

from video_streaming import S3Objects, VideoService


TABLES = ("settings", "credentials", "videos", "parts", "jobs", "completions")


def migrate(source, target, objects=None, source_objects=None):
    connection = sqlite3.connect(source)
    try:
        # Snapshot all source tables while offline, including credentials/signing key.
        connection.execute("BEGIN")
        rows = {table: connection.execute("SELECT * FROM " + table).fetchall() for table in TABLES}
        if objects is not None:
            if source_objects is None:
                raise ValueError("source object directory required")
            for path in Path(source_objects).iterdir():
                if path.is_file() and re.fullmatch(r"[0-9a-f]{64}", path.name):
                    if objects.put_file(path) != path.name:
                        raise ValueError("source object checksum mismatch")
        with target.lock, target.db:
            if any(target.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in TABLES[1:]):
                raise ValueError("target must be empty")
            target.db.execute("DELETE FROM settings")
            for table in TABLES:
                for row in rows[table]:
                    target.db.execute("INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in row) + ")", row)
        target.secret = bytes.fromhex(target.db.execute("SELECT value FROM settings WHERE key='signing'").fetchone()[0])
        return {table: len(data) for table, data in rows.items()}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--source-objects", required=True)
    parser.add_argument("--root", default=".runtime/migration")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    objects = S3Objects(root / "objects", os.environ["VIDEO_S3_BUCKET"],
                        endpoint=os.environ.get("VIDEO_S3_ENDPOINT"),
                        prefix=os.environ.get("VIDEO_S3_PREFIX", "video/"))
    service = VideoService(os.environ["VIDEO_DATABASE_DSN"], objects)
    try:
        print(migrate(args.source, service, objects, args.source_objects))
    finally:
        service.close()


if __name__ == "__main__":
    main()
