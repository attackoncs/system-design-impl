from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

from helpers_review import AuthorityCase
from digital_wallet.common import Error, encode, entry
from digital_wallet.contracts import intent
from digital_wallet.coordinator import Coordinator
from digital_wallet.coordinator_projection import project
from digital_wallet.projection import reduce_event


class SafetyTests(AuthorityCase):
    def test_create_freezes_before_writer_wait_and_returns_detached_values(self):
        body, frozen = self.body(), threading.Event()

        def validate(value):
            result = intent(value)
            frozen.set()
            return result

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch("digital_wallet.coordinator.intent", side_effect=validate),
        ):
            with self.store.runtime.lock:
                future = pool.submit(self.store.create, body, "alice")
                self.assertTrue(frozen.wait(1))
                body["amount"] = 999
            value = future.result(3)
        self.assertEqual(value["intent"]["amount"], 100)
        value["intent"]["amount"] = 777
        self.assertEqual(self.store.owned("transfer", "alice")["intent"]["amount"], 100)

    def test_configuration_is_frozen_and_ownership_change_requires_migration(self):
        self.config["accounts"][self.source]["owner"] = "bob"
        self.assertEqual(self.store.config["accounts"][self.source]["owner"], "alice")
        with self.assertRaisesRegex(ValueError, "ownership"):
            Coordinator(self.root / "coordinator.sqlite", self.config)

    def test_caller_owned_shard_command_is_frozen_before_sql_writer_wait(self):
        body, frozen = self.body(), threading.Event()

        def validate(value):
            result = intent(value)
            frozen.set()
            return result

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch("digital_wallet.shard.intent", side_effect=validate),
        ):
            with self.shards[0].transaction():
                future = pool.submit(
                    self.shards[0].action, body, "prepare", "source", 1, "coordinator"
                )
                self.assertTrue(frozen.wait(1))
                body["amount"] = 999
            self.assertEqual(future.result(3)["intent"]["amount"], 100)
        self.assertEqual(self.shards[0].balance(self.source)["held"], 100)

    def test_replay_rejects_wrong_role_and_duplicate_cancel(self):
        genesis = {"kind": "genesis", "accounts": {self.source: 10000}}
        prepared = reduce_event(
            reduce_event({}, genesis),
            {"kind": "prepare", "role": "source", "intent": self.body()},
        )
        for event in (
            {"kind": "confirm", "role": "destination", "intent": self.body()},
            {"kind": "local", "role": "source", "intent": self.body()},
        ):
            with self.assertRaises(ValueError):
                reduce_event(prepared, event)
        canceled = reduce_event(
            prepared, {"kind": "cancel", "role": "source", "intent": self.body()}
        )
        with self.assertRaises(ValueError):
            reduce_event(
                canceled, {"kind": "cancel", "role": "source", "intent": self.body()}
            )

    def test_coordinator_replay_rejects_invalid_id_and_decision_completion(self):
        accepted = {
            "kind": "accepted",
            "transaction_id": "transfer",
            "intent": self.body(),
        }
        for events in (
            [{**accepted, "transaction_id": "other"}],
            [
                accepted,
                {
                    "kind": "decision",
                    "transaction_id": "transfer",
                    "decision": "COMMIT",
                },
                {
                    "kind": "completed",
                    "transaction_id": "transfer",
                    "status": "CANCELED",
                },
            ],
            [
                accepted,
                {
                    "kind": "completed",
                    "transaction_id": "transfer",
                    "status": "CANCELED",
                },
            ],
        ):
            with self.assertRaises(ValueError):
                project([{"event": value} for value in events])

    def test_wrong_participant_receipt_does_not_choose_commit_or_release_hold(self):
        self.store.create(self.body(), "alice")
        self.store.step()
        self.assertEqual(self.shards[0].balance(self.source)["held"], 100)
        original = self.store.shard

        def wrong(index, path, body=None):
            value = original(index, path, body)
            if index == 0 and path.startswith("/internal/participants/"):
                value["participant"]["intent"]["amount"] = 999
            return value

        with patch.object(self.store, "shard", side_effect=wrong):
            self.assertIn("error", self.store.step())
        self.assertIsNone(self.store.owned("transfer", "alice")["decision"])
        self.assertEqual(self.shards[0].balance(self.source)["held"], 100)

    def test_empty_action_reply_is_unknown_and_remains_pending(self):
        self.store.create(self.body(local=True), "alice")
        original = self.store.shard

        def lost(index, path, body=None):
            value = original(index, path, body)
            return None if path == "/internal/action" else value

        with patch.object(self.store, "shard", side_effect=lost):
            self.assertIn("error", self.store.step())
        self.assertEqual(self.store.owned("transfer", "alice")["status"], "PENDING")
        self.store.retry("transfer")
        self.assertEqual(self.finish()["status"], "SUCCEEDED")
        self.assertEqual(self.shards[0].balance(self.source)["available"], 9900)

    def test_idle_does_not_probe_quorum_but_pending_local_intent_is_recovered(self):
        original = self.store.journal._replicate

        def lose_new_append(url, rows, deadline, cancel):
            return False if rows else original(url, rows, deadline, cancel)

        with patch.object(
            self.store.journal, "_replicate", side_effect=lose_new_append
        ):
            self.assertTrue(self.store.step()["idle"])
            with self.assertRaises(Error):
                self.store.journal.append(
                    {
                        "kind": "accepted",
                        "transaction_id": "transfer",
                        "intent": self.body(),
                    }
                )
        self.assertEqual(self.store.state(), {})
        self.store.step()
        self.assertEqual(self.store.owned("transfer", "alice")["status"], "PENDING")
        self.assertEqual(self.shards[0].balance(self.source)["held"], 100)

    def test_incremental_projection_reads_only_new_committed_suffix(self):
        self.store.create(self.body(), "alice")
        seq = self.store.cached_seq
        with patch.object(
            self.store.journal, "records", wraps=self.store.journal.records
        ) as read:
            self.store.owned("transfer", "alice")
            self.assertEqual(read.call_args.kwargs, {"after": seq})
        self.store.cancel("transfer")
        self.assertEqual(self.finish()["status"], "CANCELED")
        self.assertTrue(self.store.audit()["matches"])

    def test_round_robin_progress_does_not_starve_later_commands(self):
        for name in ("a", "b", "c"):
            self.store.create(self.body(name), "alice")
        self.assertEqual(
            [self.store.step()["progress"] for _ in range(3)], ["a", "b", "c"]
        )
        for name in ("a", "b", "c"):
            self.assertEqual(self.finish(name)["status"], "SUCCEEDED")
        self.assertTrue(self.store.audit()["matches"])

    def test_audit_detects_equal_supply_wrong_participant_role(self):
        self.store.create(self.body(), "alice")
        self.finish()
        original = self.store.shard

        def wrong(index, path, body=None):
            value = original(index, path, body)
            if index == 1 and path == "/internal/view":
                value["parts"]["transfer"]["role"] = "source"
            return value

        with patch.object(self.store, "shard", side_effect=wrong):
            result = self.store.audit()
        self.assertFalse(result["matches"])
        self.assertEqual(result["wallet_total"], result["opening_supply"])

    def test_concurrent_snapshots_and_writes_publish_valid_whole_files(self):
        def write():
            for index in range(12):
                self.shards[0].action(
                    self.body("write_" + str(index), local=True),
                    "local",
                    "local",
                    1,
                    "coordinator",
                )

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(write)] + [
                pool.submit(self.shards[0].snapshot) for _ in range(8)
            ]
            for future in futures:
                future.result(5)
        before = self.shards[0].view()
        self.shards[0].rebuild()
        self.assertEqual(self.shards[0].view(), before)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_balance_and_sequence_are_from_one_read_snapshot(self):
        def write():
            for index in range(35):
                self.shards[0].action(
                    self.body("write_" + str(index), 1, True),
                    "local",
                    "local",
                    1,
                    "coordinator",
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(write)
            for _ in range(60):
                value = self.shards[0].view()
                self.assertEqual(
                    value["accounts"][self.source]["available"],
                    10000 - (value["seq"] - 1),
                )
            future.result(5)

    def test_journal_parallel_fanout_and_concurrent_append_prefix(self):
        barrier = threading.Barrier(3)
        original = self.store.journal._replicate

        def replicate(url, rows, deadline, cancel):
            barrier.wait(timeout=2)
            return original(url, rows, deadline, cancel)

        with (
            patch.object(self.store.journal, "_replicate", side_effect=replicate),
            ThreadPoolExecutor(max_workers=3) as pool,
        ):
            list(
                pool.map(
                    lambda i: self.store.journal.append({"kind": "test", "value": i}),
                    range(3),
                )
            )
        rows = self.store.journal.records()
        self.assertEqual([row["seq"] for row in rows], [1, 2, 3])
        self.assertTrue(
            all(
                replica.status()["head"] == rows[-1]["hash"]
                for replica in self.replicas.values()
            )
        )

    def test_sync_only_marks_the_exact_acknowledged_snapshot(self):
        journal = self.store.journal
        first = entry(1, "0" * 64, {"kind": "test"})
        second = entry(2, first["hash"], {"kind": "other"})
        with journal.transaction() as db:
            db.execute("INSERT INTO entries VALUES(?,?,0)", (1, encode(first)))
        added = threading.Event()
        original = journal._replicate

        def replicate(url, rows, deadline, cancel):
            if url == journal.config["replicas"][0]:
                with journal.transaction() as db:
                    db.execute("INSERT INTO entries VALUES(?,?,0)", (2, encode(second)))
                added.set()
            self.assertTrue(added.wait(2))
            return original(url, rows, deadline, cancel)

        with patch.object(journal, "_replicate", side_effect=replicate):
            journal.sync()
        self.assertEqual(len(journal.records()), 1)
        self.assertTrue(journal.has_pending())

    def test_bad_replica_counts_and_acks_never_commit(self):
        original = self.replica_rpc
        for result in (
            {"count": True, "head": "0" * 64},
            {"count": -1, "head": "0" * 64},
            {"count": 0, "head": "wrong"},
        ):
            with patch("digital_wallet.journal.request", return_value=result):
                with self.assertRaises(Error):
                    self.store.journal.sync()

        def bad_ack(url, token, body=None, **kwargs):
            return (
                {"accepted": True, "hash": body["entry"]["hash"]}
                if url.endswith("/internal/append")
                else original(url, token, body, **kwargs)
            )

        with patch("digital_wallet.journal.request", side_effect=bad_ack):
            with self.assertRaises(Error):
                self.store.journal.append({"kind": "test"})
        self.assertEqual(self.store.journal.records(), [])

    def test_unexpected_replica_failure_still_drains_all_accepted_tasks(self):
        entered, release = threading.Event(), threading.Event()

        def fail_or_block(url, rows, deadline, cancel):
            if url == self.config["replicas"][0]:
                raise RuntimeError("adapter failed")
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release adapter")
            return True

        with (
            patch.object(self.store.journal, "_replicate", side_effect=fail_or_block),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(self.store.journal.sync)
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(future.done())
                self.assertFalse(self.store.journal.lock.acquire(False))
            finally:
                release.set()
            with self.assertRaisesRegex(RuntimeError, "adapter failed"):
                future.result(2)
