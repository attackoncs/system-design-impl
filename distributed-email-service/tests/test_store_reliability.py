import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from distributed_email.common import Error, digest
from distributed_email.store import MailStore


def descriptor(delivery="d", folder="inbox"):
    return {
        "subject": "Project meeting",
        "sender": "sender@local.test",
        "recipients": ["alice@local.test"],
        "folder": folder,
        "delivery_id": delivery,
        "body_key": "a" * 64,
        "attachments": [],
        "scan": "test-policy-pass",
        "scan_engine": "test-markers-only",
    }


class StoreReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.store = MailStore(self.root / "primary.sqlite", clock=lambda: self.now)

    def enqueue(self, key="key", delivery="d"):
        return self.store.enqueue(
            "alice", key, "fingerprint", descriptor(delivery, "sent"), "body", "raw-key"
        )

    def replica(self):
        return MailStore(self.root / "replica.sqlite", replica=True)

    def rehash(self, batch, index=0):
        batch["changes"][index]["hash"] = digest(batch["changes"][index]["payload"])
        return batch

    def test_claim_reports_actual_attempt_and_custom_lease(self):
        self.enqueue()
        job = self.store.claim(lease_seconds=30)
        self.assertEqual(
            (job["attempts"], job["lease"], job["status"]), (1, 1030, "inflight")
        )
        self.now = 1029
        self.assertIsNone(self.store.claim())
        self.now = 1030
        self.assertEqual(self.store.claim()["attempts"], 2)

    def test_expired_settlement_without_reclaim_is_rejected(self):
        receipt = self.enqueue()
        job = self.store.claim()
        self.now = job["lease"]
        self.assertFalse(self.store.settle(job))
        self.assertEqual(self.store.get("alice", receipt["id"])["version"], 1)
        self.assertEqual(self.store.status()["pending"], 1)

    def test_expired_failure_cannot_reschedule_a_job(self):
        self.enqueue()
        job = self.store.claim()
        self.now += 2
        self.assertFalse(self.store.settle(job, "old failure"))
        new = self.store.claim()
        self.assertNotEqual(new["token"], job["token"])
        self.assertTrue(self.store.settle(new))

    def test_retry_backoff_and_old_token_fencing(self):
        self.enqueue()
        old = self.store.claim()
        self.assertTrue(self.store.settle(old, "temporary"))
        self.assertIsNone(self.store.claim())
        self.now += 0.21
        current = self.store.claim()
        self.assertEqual(current["attempts"], 2)
        self.assertFalse(self.store.settle(old))
        self.assertTrue(self.store.settle(current))

    def test_replica_cannot_settle_even_an_unknown_job(self):
        with self.assertRaises(Error) as raised:
            self.replica().settle({"id": "d", "token": "token"})
        self.assertEqual(raised.exception.status, 409)

    def test_lease_arguments_reject_bool_float_and_out_of_bounds(self):
        for lease in (True, 1.5, 0, 61, "30"):
            with self.subTest(lease=lease), self.assertRaises(ValueError):
                self.store.claim(lease)

    def test_claim_samples_clock_after_writer_lock(self):
        self.enqueue()
        entered = threading.Event()
        clock_called = threading.Event()

        def clock():
            clock_called.set()
            return self.now

        self.store.clock = clock
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.transaction(True):

                def claim():
                    entered.set()
                    return self.store.claim()

                future = pool.submit(claim)
                self.assertTrue(entered.wait(1))
                self.assertFalse(clock_called.wait(0.05))
                self.now = 1010
            self.assertEqual(future.result(timeout=5)["lease"], 1012)

    def test_concurrent_delivery_has_one_row_index_and_history_record(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: self.store.receive(
                        "alice", "d", descriptor(), "unique token"
                    ),
                    range(16),
                )
            )
        self.assertEqual(sum(not item["duplicate"] for item in results), 1)
        self.assertEqual(len({item["id"] for item in results}), 1)
        self.assertEqual(self.store.status()["position"], 1)
        self.assertEqual(len(self.store.listing("alice", q="unique")["messages"]), 1)

    def test_concurrent_cas_has_one_winner(self):
        mail = self.store.receive("alice", "d", descriptor(), "body")["id"]

        def write(_):
            try:
                self.store.update("alice", mail, 1, True)
                return 200
            except Error as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(write, range(8)))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), 7)
        self.assertEqual(self.store.status()["position"], 2)

    def test_concurrent_settlement_is_single_effect(self):
        receipt = self.enqueue()
        job = self.store.claim()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.settle(job), range(8)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.store.get("alice", receipt["id"])["version"], 2)

    def test_write_receipts_carry_transaction_watermarks(self):
        receipt = self.store.receive("alice", "d", descriptor(), "body")
        self.assertEqual(receipt["position"], 1)
        self.assertEqual(receipt["lineage"], self.store.status()["lineage"])
        update = self.store.update("alice", receipt["id"], 1, True)
        self.assertEqual(update["position"], 2)
        duplicate = self.store.receive("alice", "d", descriptor(), "body")
        self.assertEqual(duplicate["position"], 2)
        sent = self.enqueue()
        self.assertEqual(sent["position"], 3)
        self.assertEqual(self.store.prior("alice", "key", "fingerprint")["position"], 3)

    def test_input_descriptor_is_copied_and_upload_input_is_owned(self):
        desc = descriptor()
        receipt = self.store.receive("alice", "d", desc, "body")
        desc["subject"] = "changed"
        self.assertEqual(
            self.store.get("alice", receipt["id"])["descriptor"]["subject"],
            "Project meeting",
        )
        upload = {"key": "b" * 64, "size": 2, "filename": "safe.bin"}
        result = self.store.upload("alice", upload)
        upload["filename"] = "changed"
        self.assertEqual(
            self.store.attachments("alice", [result["upload_id"]])[0]["filename"],
            "safe.bin",
        )

    def test_malformed_attachment_ids_fail_before_database_binding(self):
        for ids in ([{}], [["nested"]], [None], ["a", "a"]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.store.attachments("alice", ids)

    def test_metadata_and_body_limits_are_atomic(self):
        invalid = [
            {**descriptor(), "subject": "header\ninjection"},
            {
                **descriptor(),
                "attachments": [{"key": "b" * 64, "size": True, "filename": "x"}],
            },
            {**descriptor(), "recipients": [{}]},
            {**descriptor(), "body_key": "../escape"},
            {**descriptor(), "folder": "sent"},
            {**descriptor(), "unknown": True},
        ]
        for desc in invalid:
            with self.subTest(desc=desc), self.assertRaises(ValueError):
                self.store.receive("alice", "d", desc, "body")
        with self.assertRaises(ValueError):
            self.store.receive("alice", "d", descriptor(), "x" * 65537)
        with self.assertRaises(Error) as raised:
            self.store.receive(
                "alice", "d", descriptor(), " ".join(f"w{i}" for i in range(4097))
            )
        self.assertEqual(raised.exception.status, 422)
        self.assertEqual(self.store.status()["position"], 0)

    def test_replica_retains_full_change_log_for_user_feeds(self):
        self.store.receive("alice", "d", descriptor(), "indexed body")
        batch = self.store.changes(0)
        replica = self.replica()
        replica.apply(batch)
        self.assertEqual(replica.changes(0), batch)

    def test_replica_validates_checksum_even_for_old_sequences(self):
        self.store.receive("alice", "d", descriptor(), "body")
        batch = self.store.changes(0)
        replica = self.replica()
        replica.apply(batch)
        batch["changes"][0]["payload"]["record"]["read"] = 1
        with self.assertRaises(Error):
            replica.apply(batch)
        self.assertEqual(replica.status()["position"], 1)

    def test_replica_rejects_rehashed_conflicting_replays(self):
        self.store.receive("alice", "d", descriptor(), "body")
        batch = self.store.changes(0)
        replica = self.replica()
        replica.apply(batch)
        batch["changes"][0]["payload"]["record"]["read"] = 1
        self.rehash(batch)
        with self.assertRaises(Error):
            replica.apply(batch)
        self.assertEqual(replica.listing("alice")["messages"][0]["read"], 0)

    def test_bad_later_change_rolls_back_message_index_and_checkpoint(self):
        self.store.receive("alice", "d", descriptor(), "alpha")
        self.store.receive("alice", "other", descriptor("other"), "beta")
        batch = self.store.changes(0)
        batch["changes"][1]["payload"]["record"]["version"] = 5
        self.rehash(batch, 1)
        replica = self.replica()
        with self.assertRaises(Error):
            replica.apply(batch)
        self.assertEqual(replica.status()["position"], 0)
        self.assertEqual(replica.listing("alice")["messages"], [])
        self.assertEqual(replica.changes(0)["changes"], [])

    def test_replica_rejects_boolean_head_shard_and_sequence(self):
        self.store.receive("alice", "d", descriptor(), "body")
        original = self.store.changes(0)
        for field in ("head", "shard", "shards"):
            batch = copy.deepcopy(original)
            batch[field] = True
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.replica().apply(batch)
        batch = copy.deepcopy(original)
        batch["changes"][0]["seq"] = True
        with self.assertRaises(ValueError):
            self.replica().apply(batch)

    def test_replica_rejects_head_behind_page_and_duplicate_sequences(self):
        self.store.receive("alice", "d", descriptor(), "body")
        original = self.store.changes(0)
        with self.assertRaises(Error):
            self.replica().apply({**original, "head": 0})
        with self.assertRaises(Error):
            self.replica().apply({**original, "changes": original["changes"] * 2})

    def test_replica_rejects_index_corruption_even_with_rehashed_payload(self):
        self.store.receive("alice", "d", descriptor(), "body")
        original = self.store.changes(0)
        for postings in (
            [["unknown", "body"]],
            [["subject", "wrong"]],
            [["body", "NOT_NORMALIZED"]],
        ):
            batch = copy.deepcopy(original)
            batch["changes"][0]["payload"]["terms"] = postings
            self.rehash(batch)
            with self.subTest(postings=postings), self.assertRaises(ValueError):
                self.replica().apply(batch)

    def test_replica_identity_and_version_cannot_change(self):
        receipt = self.store.receive("alice", "d", descriptor(), "body")
        replica = self.replica()
        replica.apply(self.store.changes(0))
        self.store.update("alice", receipt["id"], 1, True)
        original = self.store.changes(1)
        for field, value in (("user", "bob"), ("local_seq", 2), ("version", 3)):
            batch = copy.deepcopy(original)
            batch["changes"][0]["payload"]["record"][field] = value
            self.rehash(batch)
            with self.subTest(field=field), self.assertRaises(Error):
                replica.apply(batch)
        self.assertEqual(replica.status()["position"], 1)

    def test_deleted_sent_mail_can_settle_without_resurrection(self):
        receipt = self.enqueue()
        job = self.store.claim()
        self.store.update("alice", receipt["id"], 1, delete=True)
        self.assertTrue(self.store.settle(job))
        replica = self.replica()
        replica.apply(self.store.changes(0))
        self.assertEqual(replica.listing("alice", folder="sent")["messages"], [])
        self.assertEqual(replica.status()["position"], 3)

    def test_replica_rejects_immutable_body_reference_and_body_index_changes(self):
        receipt = self.store.receive("alice", "d", descriptor(), "body")
        replica = self.replica()
        replica.apply(self.store.changes(0))
        self.store.update("alice", receipt["id"], 1, True)
        original = self.store.changes(1)
        altered_reference = copy.deepcopy(original)
        record = altered_reference["changes"][0]["payload"]["record"]
        desc = __import__("json").loads(record["descriptor"])
        desc["body_key"] = "b" * 64
        record["descriptor"] = __import__("json").dumps(desc)
        self.rehash(altered_reference)
        with self.assertRaises(Error):
            replica.apply(altered_reference)
        altered_index = copy.deepcopy(original)
        altered_index["changes"][0]["payload"]["terms"].append(["body", "injected"])
        self.rehash(altered_index)
        with self.assertRaises(Error):
            replica.apply(altered_index)
        self.assertEqual(replica.status()["position"], 1)

    def test_replica_restart_replay_is_idempotent(self):
        receipt = self.store.receive("alice", "d", descriptor(), "body")
        self.store.update("alice", receipt["id"], 1, True)
        batch = self.store.changes(0)
        self.replica().apply(batch)
        replica = self.replica()
        self.assertEqual(replica.apply(batch), 2)
        self.assertEqual(len(replica.changes(0)["changes"]), 2)


if __name__ == "__main__":
    unittest.main()
