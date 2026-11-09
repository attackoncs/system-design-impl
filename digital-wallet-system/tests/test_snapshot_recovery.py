"""Snapshot + tail recovery and incremental journal sync (book Ch.28 Figure 22)."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from digital_wallet import journal as journal_module
from digital_wallet import projection
from digital_wallet.common import checksum, encode, entry, shard_for
from digital_wallet.journal import Replica
from digital_wallet.shard import Shard
from helpers_review import AuthorityCase


class CountingApply:
    """Count events folded by the shared reducer while keeping its behaviour."""

    def __init__(self):
        self.calls = 0

    def __call__(self, state, event):
        self.calls += 1
        return APPLY(state, event)


APPLY = projection.apply_event


class SnapshotRecoveryTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".runtime" / "tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".runtime" / "tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        names = ["account_" + str(i) for i in range(30)]
        self.source, self.peer = [k for k in names if shard_for(k, 2) == 0][:2]
        self.accounts = {self.source: 10000, self.peer: 10000}
        self.path = self.root / "left.sqlite"
        self.shard = Shard(self.path, 0, 2, self.accounts)

    def transfer(self, id, amount=1):
        body = {
            "transaction_id": id,
            "from_account": self.source,
            "to_account": self.peer,
            "amount": amount,
            "currency": "USD",
        }
        return self.shard.action(body, "local", "local", 1, "coordinator")

    def counting(self):
        counter = CountingApply()
        return counter, patch.object(projection, "apply_event", counter)

    def snapshot_file(self):
        return json.loads(self.shard.snapshot_path.read_text())

    def write_snapshot(self, payload):
        self.shard.snapshot_path.write_text(
            encode({"payload": payload, "checksum": checksum(payload)})
        )

    def test_restart_replays_only_the_tail_after_a_snapshot(self):
        for i in range(20):
            self.transfer("t" + str(i))
        self.assertEqual(self.shard.snapshot()["seq"], 21)
        for i in range(3):
            self.transfer("tail" + str(i))
        counter, patched = self.counting()
        with patched:
            restored = Shard(self.path, 0, 2, self.accounts)
        self.assertEqual(counter.calls, 3)
        self.assertEqual(restored.view(), self.shard.view())
        self.assertEqual(restored.view()["accounts"][self.source]["available"], 9977)
        self.assertTrue(restored.audit()["matches"])

    def test_snapshots_are_incremental(self):
        for i in range(10):
            self.transfer("t" + str(i))
        self.shard.snapshot()
        self.transfer("next")
        counter, patched = self.counting()
        with patched:
            published = self.shard.snapshot()
        self.assertEqual(counter.calls, 1)
        self.assertEqual(published["seq"], 12)
        with self.shard.read_transaction() as db:
            replayed = self.shard.replay(self.shard.rows(db))
        self.assertEqual(self.snapshot_file()["payload"]["state"], replayed)

    def test_historical_reads_use_snapshot_only_when_it_precedes_them(self):
        for i in range(6):
            self.transfer("t" + str(i), 10)
        self.shard.snapshot()  # seq 7
        self.transfer("tail", 10)
        counter, patched = self.counting()
        with patched:
            after = self.shard.view(8)
        self.assertEqual(counter.calls, 1)
        self.assertEqual(after["accounts"][self.source]["available"], 9930)
        with patched:
            before = self.shard.view(3)
        self.assertEqual(counter.calls, 1 + 3)
        self.assertEqual(before["accounts"][self.source]["available"], 9980)

    def test_historical_reads_fall_back_to_verified_replay_on_bad_snapshot(self):
        self.transfer("one", 10)
        self.shard.snapshot()
        self.transfer("two", 10)
        self.shard.snapshot_path.write_text("{not json")
        self.assertEqual(
            self.shard.view(3)["accounts"][self.source]["available"], 9980
        )
        with self.assertRaises(ValueError):
            self.shard.rebuild()
        self.assertFalse(self.shard.audit()["matches"])

    def test_audit_detects_resealed_snapshot_that_fast_recovery_trusts(self):
        self.transfer("one", 10)
        self.shard.snapshot()
        payload = self.snapshot_file()["payload"]
        payload["state"]["accounts"][self.source]["available"] += 5
        self.write_snapshot(payload)  # checksum recomputed: looks authentic
        self.assertFalse(self.shard.audit()["matches"])
        self.assertEqual(self.shard.audit()["events"], 2)

    def test_snapshot_must_belong_to_this_log_and_not_run_ahead(self):
        self.transfer("one", 10)
        self.shard.snapshot()
        good = self.snapshot_file()["payload"]
        self.write_snapshot({**good, "head": "f" * 64})
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.shard.rebuild()
        self.write_snapshot({**good, "seq": 99})
        with self.assertRaisesRegex(ValueError, "ahead"):
            self.shard.rebuild()
        self.assertFalse(self.shard.audit()["matches"])
        self.write_snapshot(good)
        self.assertTrue(self.shard.audit()["matches"])

    def test_tail_corruption_after_snapshot_is_still_rejected(self):
        self.transfer("one", 10)
        self.shard.snapshot()
        self.transfer("two", 10)
        with self.shard.transaction() as db:
            db.execute("DROP TRIGGER events_no_update")
            row = json.loads(
                db.execute("SELECT body FROM events WHERE seq=3").fetchone()[0]
            )
            row["event"]["intent"]["amount"] = 1
            db.execute("UPDATE events SET body=? WHERE seq=3", (json.dumps(row),))
        with self.assertRaises(ValueError):
            self.shard.rebuild()

    def test_apply_event_is_in_place_and_reduce_event_stays_pure(self):
        genesis = {"kind": "genesis", "accounts": {self.source: 5, self.peer: 0}}
        state = projection.apply_event({}, genesis)
        frozen = json.loads(json.dumps(state))
        event = {
            "kind": "local",
            "role": "local",
            "intent": {
                "transaction_id": "x",
                "from_account": self.source,
                "to_account": self.peer,
                "amount": 2,
                "currency": "USD",
            },
        }
        reduced = projection.reduce_event(state, event)
        self.assertEqual(state, frozen)
        self.assertIs(projection.apply_event(state, event), state)
        self.assertEqual(state, reduced)


class IncrementalJournalTests(AuthorityCase):
    def test_sync_verifies_and_ships_only_rows_since_the_last_sync(self):
        journal = self.store.journal
        for i in range(8):
            journal.append({"kind": "test", "value": i})
        verified, real_verify = [], journal_module.verify

        def recording(rows, *args):
            verified.append(len(rows))
            return real_verify(rows, *args)

        with patch.object(journal_module, "verify", recording):
            journal.append({"kind": "test", "value": "tail"})
        self.assertTrue(verified)
        self.assertLessEqual(max(verified), 1)
        head = journal.records()[-1]["hash"]
        for replica in self.replicas.values():
            self.assertEqual(replica.status(), {"count": 9, "head": head})

    def test_lagging_replica_catches_up_from_its_own_position(self):
        journal = self.store.journal
        for i in range(4):
            journal.append({"kind": "test", "value": i})
        url = self.config["replicas"][0]
        self.replicas[url] = Replica(self.root / "fresh.sqlite")
        journal.append({"kind": "test", "value": "next"})
        rows = journal.records()
        self.assertEqual(
            self.replicas[url].status(), {"count": 5, "head": rows[-1]["hash"]}
        )

    def test_view_refuses_to_ship_a_suffix_that_does_not_link(self):
        journal = self.store.journal
        journal.append({"kind": "test", "value": 0})
        journal.append({"kind": "test", "value": 1})
        view = journal._view()
        with journal.transaction() as db:
            db.execute("DROP TRIGGER entries_no_update")
            forged = entry(2, "e" * 64, {"kind": "forged"})
            db.execute("UPDATE entries SET body=? WHERE seq=2", (encode(forged),))
        for known in (0, 1, 2):
            with self.assertRaises(ValueError):
                view.suffix(known)


if __name__ == "__main__":
    unittest.main()
