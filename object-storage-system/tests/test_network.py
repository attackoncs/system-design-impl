from pathlib import Path
import sqlite3
import sys
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab
from object_storage.common import sha, shard_for


class NetworkTests(unittest.TestCase):
    def test_versions_atomic_failure_and_private_access(self):
        with Lab() as lab:
            first = lab.put("path/key", b"one", "same")
            self.assertEqual(
                lab.put("path/key", b"one", "same")["version"], first["version"]
            )
            second = lab.put("path/key", b"two")
            self.assertNotEqual(first["version"], second["version"])
            self.assertEqual(lab.get("path/key"), b"two")
            self.assertEqual(lab.get("path/key", first["version"]), b"one")
            marker = lab.api(
                "/v1/buckets/bucket-test/object?key=path%2Fkey",
                method="DELETE",
                headers={"Idempotency-Key": "delete"},
            )
            with self.assertRaises(HTTPError) as absent:
                lab.get("path/key")
            self.assertEqual(absent.exception.code, 404)
            lab.api(
                "/v1/buckets/bucket-test/object?"
                + urlencode({"key": "path/key", "version": marker["version"]}),
                method="DELETE",
            )
            self.assertEqual(lab.get("path/key"), b"two")
            with self.assertRaises(HTTPError) as forbidden:
                lab.api("/v1/buckets/bucket-test/objects", role="bob")
            self.assertEqual(forbidden.exception.code, 404)
            lab.stop("data-0")
            lab.stop("data-1")
            with self.assertRaises(HTTPError) as quorum:
                lab.put("failed", b"unpublished")
            self.assertEqual(quorum.exception.code, 503)
            self.assertNotIn(
                "failed",
                [
                    r["key"]
                    for r in lab.api("/v1/buckets/bucket-test/objects")["objects"]
                ],
            )
            self.assertEqual(lab.get("path/key"), b"two")

    def test_multipart_resume_restart_expiry_and_checksum(self):
        with Lab() as lab:
            data = b"A" * 700000 + b"B" * 500000
            upload = lab.api(
                "/v1/buckets/bucket-test/uploads",
                {
                    "key": "large",
                    "request_key": "multi",
                    "sha256": sha(data),
                    "size": len(data),
                },
            )["id"]
            lab.api(
                f"/v1/uploads/{upload}/parts/1",
                data[:700000],
                "PUT",
                headers={"X-SHA256": sha(data[:700000])},
            )
            with self.assertRaises(HTTPError):
                lab.get("large")
            lab.stop("api")
            lab.spawn("api")
            self.assertEqual(len(lab.api(f"/v1/uploads/{upload}")["parts"]), 1)
            with self.assertRaises(HTTPError):
                lab.api(f"/v1/uploads/{upload}/complete", {}, "POST")
            lab.api(
                f"/v1/uploads/{upload}/parts/2",
                data[700000:],
                "PUT",
                headers={"X-SHA256": sha(data[700000:])},
            )
            bucket = lab.api("/v1/buckets/bucket-test")
            owner = shard_for(bucket["id"], "large", 2)
            lab.stop(f"metadata-{owner}")
            with self.assertRaises(HTTPError) as unavailable:
                lab.api(f"/v1/uploads/{upload}/complete", {}, "POST")
            self.assertEqual(unavailable.exception.code, 503)
            self.assertEqual(lab.api(f"/v1/uploads/{upload}")["state"], "completing")
            lab.stop("api")
            lab.spawn("api")
            lab.spawn(f"metadata-{owner}")
            recovered = lab.api(
                "/admin/maintenance", {"grace": 0}, "POST", role="admin"
            )
            self.assertEqual(recovered["resumed_completions"], 1)
            version = lab.api(f"/v1/uploads/{upload}/complete", {}, "POST")
            self.assertEqual(
                lab.api(f"/v1/uploads/{upload}/complete", {}, "POST")["version"],
                version["version"],
            )
            self.assertEqual(lab.get("large"), data)
            expired = lab.api(
                "/v1/buckets/bucket-test/uploads",
                {
                    "key": "expired",
                    "request_key": "expire",
                    "sha256": sha(b"x"),
                    "size": 1,
                },
            )["id"]
            lab.api(
                f"/v1/uploads/{expired}/parts/1",
                b"x",
                "PUT",
                headers={"X-SHA256": sha(b"x")},
            )
            # Explicit fault: advance this owned upload deadline in the catalog.
            db = sqlite3.connect(lab.root / "catalog.sqlite")
            try:
                db.execute("UPDATE uploads SET expires=0 WHERE id=?", (expired,))
                db.commit()
            finally:
                db.close()
            result = lab.api("/admin/maintenance", {"grace": 0}, "POST", role="admin")
            self.assertEqual(result["expired_uploads"], 1)
            self.assertEqual(lab.get("large"), data)
            self.assertEqual(lab.api(f"/v1/uploads/{expired}")["state"], "aborted")

    def test_corrupt_replica_missing_disk_repair_gc_and_compaction(self):
        with Lab() as lab:
            data = b"replicated small object"
            lab.put("keep", data)
            old = lab.put("garbage", b"unreferenced")
            key = sha(data)
            db = sqlite3.connect(lab.root / "data-0/index.sqlite")
            try:
                offset = db.execute(
                    "SELECT offset FROM chunks WHERE hash=?", (key,)
                ).fetchone()[0]
            finally:
                db.close()
            pack = (
                lab.root / "data-0" / (lab.root / "data-0/CURRENT").read_text().strip()
            )
            with pack.open("r+b") as handle:
                handle.seek(offset)
                handle.write(b"X")
            self.assertEqual(lab.get("keep"), data)
            lab.stop("data-1")
            source = (lab.root / "data-1").resolve()
            target = (lab.root / "data-1-unavailable").resolve()
            if (
                source.parent != lab.root.resolve()
                or target.parent != lab.root.resolve()
            ):
                raise AssertionError("fault path escaped lab")
            source.rename(
                target
            )  # Logical disk-loss simulation; original data preserved.
            lab.spawn("data-1")
            repaired = lab.api("/admin/maintenance", {"grace": 0}, "POST", role="admin")
            self.assertGreaterEqual(repaired["repaired_copies"], 2)
            self.assertEqual(repaired["unrecoverable"], [])
            self.assertEqual(repaired["copy_failures"], [])
            for i in range(3):
                self.assertEqual(
                    lab.call(f"data-{i}", "/chunks/" + key, role="internal", raw=True),
                    data,
                )
            lab.api(
                "/v1/buckets/bucket-test/object?"
                + urlencode({"key": "garbage", "version": old["version"]}),
                method="DELETE",
            )
            collected = lab.api(
                "/admin/maintenance", {"grace": 0}, "POST", role="admin"
            )
            self.assertEqual(sum(n.get("removed", 0) for n in collected["nodes"]), 3)
            for i in range(3):
                lab.call(f"data-{i}", "/compact", {}, "POST", role="internal")
                lab.stop(f"data-{i}")
                lab.spawn(f"data-{i}")
            self.assertEqual(lab.get("keep"), data)

    def test_cross_shard_frozen_pagination_and_token_binding(self):
        with Lab() as lab:
            keys = [f"key-{i:02}" for i in range(12)]
            for key in keys:
                lab.put(key, key.encode())
            page = lab.api("/v1/buckets/bucket-test/objects?limit=3")
            result = [r["key"] for r in page["objects"]]
            lab.put("key-new", b"new")
            lab.api(
                "/v1/buckets/bucket-test/object?key=key-10",
                method="DELETE",
                headers={"Idempotency-Key": "delete-later"},
            )
            token = page["continuation"]
            with self.assertRaises(HTTPError):
                lab.api(
                    "/v1/buckets/bucket-test/objects?"
                    + urlencode({"limit": 4, "token": token})
                )
            with self.assertRaises(HTTPError):
                lab.api(
                    "/v1/buckets/bucket-test/objects?"
                    + urlencode({"limit": 3, "token": token + "x"})
                )
            while token:
                page = lab.api(
                    "/v1/buckets/bucket-test/objects?"
                    + urlencode({"limit": 3, "token": token})
                )
                result.extend(r["key"] for r in page["objects"])
                token = page["continuation"]
            self.assertEqual(result, keys)


if __name__ == "__main__":
    unittest.main()
