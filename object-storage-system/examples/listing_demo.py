"""Actual multi-process bucket ownership, directory projections and upload races."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Lab
from object_storage.common import sha


def run():
    with Lab() as lab:
        assert [bucket["name"] for bucket in lab.api("/v1/buckets")["buckets"]] == [
            "bucket-test"
        ]
        assert lab.api("/v1/buckets", role="bob")["buckets"] == []
        names = [
            "CA/cities/la.txt",
            "CA/cities/sf.txt",
            "NY/newyork.txt",
            "federal.txt",
        ]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda key: lab.put(key, key.encode()), names))
        first = lab.api("/v1/buckets/bucket-test/objects?delimiter=/&limit=1")
        assert first["common_prefixes"] == ["CA/"]
        lab.put("ZZ/after-snapshot.txt", b"new")
        prefixes, objects = list(first["common_prefixes"]), list(first["objects"])
        token = first["continuation"]
        while token:
            page = lab.api(
                "/v1/buckets/bucket-test/objects?"
                + urlencode({"delimiter": "/", "limit": 1, "token": token})
            )
            prefixes.extend(page["common_prefixes"])
            objects.extend(page["objects"])
            token = page["continuation"]
        assert prefixes == ["CA/", "NY/"] and [row["key"] for row in objects] == [
            "federal.txt"
        ]
        recursive = lab.api("/v1/buckets/bucket-test/objects?prefix=CA/")
        assert [row["key"] for row in recursive["objects"]] == names[:2]
        data = b"part-one" + b"part-two"
        upload = lab.api(
            "/v1/buckets/bucket-test/uploads",
            {
                "key": "multipart",
                "request_key": "multipart-demo",
                "size": len(data),
                "sha256": sha(data),
            },
        )["id"]

        def part(item):
            number, raw = item
            return lab.api(
                f"/v1/uploads/{upload}/parts/{number}",
                raw,
                "PUT",
                headers={"X-SHA256": sha(raw)},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(part, [(1, b"part-one"), (2, b"part-two")]))
        with ThreadPoolExecutor(max_workers=2) as pool:
            completed = list(
                pool.map(
                    lambda _: lab.api(f"/v1/uploads/{upload}/complete", {}), range(2)
                )
            )
        assert completed[0]["version"] == completed[1]["version"]
        assert lab.get("multipart") == data
        status = None
        try:
            lab.api(f"/v1/uploads/{upload}", method="DELETE")
        except HTTPError as exc:
            status = exc.code
            exc.close()
        assert status == 409
        generation = lab.call("data-0", "/status", role="internal")["gc_generation"]
        lab.put("after-gc-snapshot", b"protected-after-snapshot")
        stale_collect_status = None
        try:
            # Deliberately stale generation: the server must reject this before
            # considering its incomplete synthetic reference set.
            lab.call(
                "data-0",
                "/collect",
                {"live": [], "grace": 0, "generation": generation},
                role="internal",
            )
        except HTTPError as exc:
            stale_collect_status = exc.code
            exc.close()
        assert stale_collect_status == 409
        assert lab.get("after-gc-snapshot") == b"protected-after-snapshot"
        maintenance = lab.api("/admin/maintenance", {"grace": 60}, role="admin")
        assert not maintenance["unrecoverable"]
        return {
            "processes": 6,
            "owner_buckets": 1,
            "other_owner_buckets": 0,
            "parallel_puts": 4,
            "snapshot_common_prefixes": prefixes,
            "snapshot_objects": [row["key"] for row in objects],
            "later_prefix_excluded_from_snapshot": True,
            "recursive_prefix_matches": 2,
            "parallel_parts": 2,
            "concurrent_completions_same_version": True,
            "completed_abort_status": status,
            "stale_collect_status": stale_collect_status,
            "download_matches": True,
            "unrecoverable_chunks": len(maintenance["unrecoverable"]),
            "scope": "six localhost HTTP processes; no AWS/cloud credentials or external service",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="benchmarks/features-2026-10-26.json")
    args = parser.parse_args()
    result = run()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
