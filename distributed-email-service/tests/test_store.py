import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from distributed_email.common import Error
from distributed_email.mail import TestMarkerScanner, UnconfiguredScanner
from distributed_email.objects import ObjectStore
from distributed_email.store import MailStore


def descriptor(delivery="d"):
    return {
        "subject": "Hello Project",
        "sender": "sender@local.test",
        "recipients": ["alice@local.test"],
        "folder": "inbox",
        "delivery_id": delivery,
        "body_key": "0" * 64,
        "attachments": [],
        "scan": "test-policy-pass",
        "scan_engine": "test-markers-only",
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000
        self.store = MailStore(self.root / "primary.sqlite", clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_delivery_dedup_search_and_delete_tombstone(self):
        first = self.store.receive("alice", "d", descriptor(), "plan alpha beta")
        again = self.store.receive("alice", "d", descriptor(), "plan alpha beta")
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(
            len(self.store.listing("alice", q="alpha beta", field="body")["messages"]),
            1,
        )
        self.assertEqual(
            len(self.store.listing("alice", q="hello", field="subject")["messages"]), 1
        )
        self.assertEqual(
            len(self.store.listing("alice", q="sender", field="sender")["messages"]), 1
        )
        self.assertEqual(
            self.store.listing("alice", q="hello", field="body")["messages"], []
        )
        with self.assertRaises(Error):
            self.store.receive("alice", "d", descriptor(), "changed")
        self.store.update("alice", first["id"], 1, delete=True)
        self.assertEqual(self.store.listing("alice", q="alpha")["messages"], [])
        self.assertTrue(
            self.store.receive("alice", "d", descriptor(), "plan alpha beta")[
                "duplicate"
            ]
        )
        self.assertEqual(self.store.listing("alice")["messages"], [])

    def test_read_version_conflict_and_authorization(self):
        mail = self.store.receive("alice", "d", descriptor(), "body")["id"]
        self.store.update("alice", mail, 1, True)
        with self.assertRaises(Error):
            self.store.update("alice", mail, 1, False)
        with self.assertRaises(Error):
            self.store.get("outsider", mail)
        self.assertEqual(self.store.listing("alice", read="false")["messages"], [])
        self.assertEqual(len(self.store.listing("alice", read="true")["messages"]), 1)

    def test_replica_gap_checksum_role_and_restart(self):
        replica = MailStore(self.root / "replica.sqlite", replica=True)
        mail = self.store.receive("alice", "d", descriptor(), "index words")["id"]
        self.store.update("alice", mail, 1, True)
        batch = self.store.changes(0)
        corrupt = json.loads(json.dumps(batch))
        corrupt["changes"][0]["payload"]["record"]["read"] = 1
        with self.assertRaises(Error):
            replica.apply(corrupt)
        with self.assertRaises(Error):
            replica.apply({**batch, "changes": batch["changes"][1:]})
        self.assertEqual(replica.status()["position"], 0)
        replica.apply(batch)
        replica = MailStore(replica.path, replica=True)
        replica.apply(batch)
        self.assertEqual(replica.get("alice", mail)["version"], 2)
        self.assertEqual(len(replica.listing("alice", q="words")["messages"]), 1)
        with self.assertRaises(Error):
            replica.update("alice", mail, 2, False)
        with self.assertRaises(Error):
            replica.apply({**batch, "lineage": "other"})
        self.store.update("alice", mail, 2, delete=True)
        replica.apply(self.store.changes(replica.status()["position"]))
        self.assertEqual(replica.listing("alice")["messages"], [])

    def test_send_receipt_jobs_lease_and_reopen(self):
        desc = {**descriptor(), "folder": "sent", "delivery": "queued"}
        first = self.store.enqueue(
            "alice", "key", "fingerprint", desc, "body", "raw-key"
        )
        self.assertTrue(
            self.store.enqueue("alice", "key", "fingerprint", desc, "body", "raw-key")[
                "duplicate"
            ]
        )
        with self.assertRaises(Error):
            self.store.enqueue("alice", "key", "changed", desc, "body", "raw-key")
        old = self.store.claim()
        self.now += 3
        self.store = MailStore(self.store.path, clock=lambda: self.now)
        current = self.store.claim()
        self.assertFalse(self.store.settle(old))
        self.assertTrue(self.store.settle(current))
        self.assertEqual(
            self.store.get("alice", first["id"])["descriptor"]["delivery"],
            "smtp-accepted",
        )
        self.assertEqual(self.store.status()["pending"], 0)

    def test_pagination_and_attachment_ownership(self):
        for i in range(55):
            self.store.receive("alice", str(i), descriptor(str(i)), "body")
        first = self.store.listing("alice")
        second = self.store.listing("alice", before=first["next_before"])
        self.assertEqual(len(first["messages"]), 50)
        self.assertEqual(len(second["messages"]), 5)
        self.assertFalse(
            {m["id"] for m in first["messages"]} & {m["id"] for m in second["messages"]}
        )
        upload = self.store.upload(
            "alice", {"key": "a" * 64, "size": 4, "filename": "x.bin"}
        )
        with self.assertRaises(Error):
            self.store.attachments("outsider", [upload["upload_id"]])

    def test_object_integrity_and_scanner_disclosure(self):
        objects = ObjectStore(self.root / "objects")
        result = objects.put(b"attachment")
        self.assertEqual(objects.get(result["key"]), b"attachment")
        self.assertEqual(objects.put(b"attachment"), result)
        objects.path(result["key"]).write_bytes(b"corrupted")
        with self.assertRaises(Error):
            objects.get(result["key"])
        with self.assertRaises(ValueError):
            objects.path("../../escape")
        self.assertEqual(
            TestMarkerScanner().scan(b"LOCAL_TEST_VIRUS_MARKER")[0], "quarantine"
        )
        self.assertEqual(TestMarkerScanner().scan(b"normal")[2], "test-markers-only")
        self.assertEqual(
            UnconfiguredScanner().scan(b"normal"), ("quarantine", "unscanned", "none")
        )


if __name__ == "__main__":
    unittest.main()
