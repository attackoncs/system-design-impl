"""Profile commits and lease promotion must share writer ownership."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
import threading

from helpers_review import StoreCase
from scale_from_zero.common import Error
from scale_from_zero.leadership import Fence


class LeadershipSafetyTests(StoreCase):
    def fences(self):
        path = self.root / "fence.sqlite"
        self.store.fence = Fence(path, "primary", lambda: self.now)
        self.replica.fence = Fence(path, "replica", lambda: self.now)
        self.store.fence.heartbeat(0)

    def test_expiry_while_waiting_for_profile_writer_cannot_commit_old_data(self):
        self.fences()
        entered = threading.Event()
        original = self.store.transaction

        @contextmanager
        def observed():
            entered.set()
            with original() as db:
                yield db

        self.store.transaction = observed
        with ThreadPoolExecutor(max_workers=2) as pool:
            with original():  # Hold the actual SQLite writer until the lease expires.
                old = pool.submit(self.store.update, "alice", self.body())
                self.assertTrue(entered.wait(2))
                self.now += 3
                promoted = pool.submit(self.replica.fence.promote, 0)
            with self.assertRaises(Error) as refused:
                old.result(3)
            self.assertEqual(refused.exception.status, 403)
            self.assertEqual(promoted.result(3)["holder"], "replica")
        with self.assertRaises(Error) as missing:
            self.store.get("alice")
        self.assertEqual(missing.exception.status, 404)
        with self.store.connection() as db:
            for table in ("users", "changes", "requests", "jobs"):
                self.assertEqual(
                    db.execute("SELECT count(*) FROM " + table).fetchone()[0], 0
                )
        self.assertEqual(self.replica.update("alice", self.body())["version"], 1)

    def test_promotion_waits_for_commit_and_rejects_its_old_cursor(self):
        self.fences()
        entered, release = threading.Event(), threading.Event()
        original = self.store.transaction

        @contextmanager
        def delayed_commit():
            with original() as db:
                yield db
                entered.set()
                assert release.wait(3)

        self.store.transaction = delayed_commit
        with ThreadPoolExecutor(max_workers=2) as pool:
            write = pool.submit(self.store.update, "alice", self.body())
            try:
                self.assertTrue(entered.wait(2))
                self.now += 3
                promoted = pool.submit(self.replica.fence.promote, 0)
                with self.assertRaises(TimeoutError):
                    promoted.result(0.05)
            finally:
                release.set()
            self.assertEqual(write.result(3)["version"], 1)
            with self.assertRaises(Error) as behind:
                promoted.result(3)
            self.assertIn("behind", str(behind.exception))
        self.assertEqual(self.store.fence.status()["cursor"], 1)
        self.replica.apply_feed(self.store.changes(0))
        self.assertEqual(self.replica.fence.promote(1)["holder"], "replica")
        self.assertEqual(
            self.replica.update("alice", self.body("next", 1))["version"], 2
        )

    def test_profile_rollback_does_not_publish_a_fence_cursor(self):
        self.fences()
        original = self.store.transaction

        @contextmanager
        def rollback():
            with original() as db:
                yield db
                raise RuntimeError("injected before profile commit")

        self.store.transaction = rollback
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.store.update("alice", self.body())
        self.assertEqual(self.store.fence.status()["cursor"], 0)
        with self.assertRaises(Error):
            self.store.get("alice")
        self.store.transaction = original
        self.assertEqual(self.store.update("alice", self.body())["version"], 1)
