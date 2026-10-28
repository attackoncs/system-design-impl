from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_store import descriptor
from distributed_email.common import Error
from distributed_email.mailbox_views import MailboxViews
from distributed_email.store import MailStore


class MailboxViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MailStore(self.root / "primary.sqlite")
        self.views = MailboxViews(self.store)

    def receive(self, user="alice", delivery="a"):
        return self.store.receive(user, delivery, descriptor(delivery), "body")

    def test_folders_count_unread_delete_and_copy_isolation(self):
        mail = self.receive()["id"]
        first = self.views.folders("alice")
        self.assertEqual(first["folders"][0]["unread"], 1)
        first["folders"].clear()
        self.assertEqual(len(self.views.folders("alice")["folders"]), 4)
        self.store.update("alice", mail, 1, True)
        self.assertEqual(self.views.folders("alice")["folders"][0]["unread"], 0)
        self.store.update("alice", mail, 2, delete=True)
        self.assertEqual(self.views.folders("alice")["folders"][0]["total"], 0)
        self.assertGreater(self.views.stats()["hits"], 0)

    def test_cache_users_are_isolated_and_bounded(self):
        views = MailboxViews(self.store, cache_entries=2, cache_bytes=4096)
        self.receive()
        for user in ("alice", "bob", "charlie"):
            result = views.folders(user)
            self.assertEqual(result["folders"][0]["total"], int(user == "alice"))
        self.assertLessEqual(views.stats()["entries"], 2)
        self.assertLessEqual(views.stats()["bytes"], 4096)

    def test_listing_cache_invalidates_at_write_watermark(self):
        mail = self.receive()["id"]
        self.views.listing("alice")
        self.views.listing("alice")["messages"].clear()
        self.assertEqual(len(self.views.listing("alice")["messages"]), 1)
        self.store.update("alice", mail, 1, delete=True)
        self.assertEqual(self.views.listing("alice")["messages"], [])

    def test_changes_scope_versions_and_tombstones(self):
        mail = self.receive()["id"]
        self.receive("bob", "b")
        self.store.update("alice", mail, 1, True)
        self.store.update("alice", mail, 2, delete=True)
        value = self.views.changes("alice")
        self.assertEqual([e["version"] for e in value["events"]], [1, 2, 3])
        self.assertEqual({e["id"] for e in value["events"]}, {mail})
        self.assertTrue(value["events"][-1]["deleted"])
        self.assertNotIn("body", value["events"][0])
        empty = self.views.changes("alice", value["position"], value["lineage"])
        self.assertEqual(empty["events"], [])

    def test_change_pagination_cannot_skip_user_events(self):
        for index in range(105):
            self.receive(delivery=str(index))
        first = self.views.changes("alice")
        self.assertEqual(len(first["events"]), 100)
        self.assertTrue(first["more"])
        last = self.views.changes("alice", first["position"], first["lineage"])
        self.assertEqual(len(last["events"]), 5)
        self.assertFalse(last["more"])
        self.assertEqual(len({e["id"] for e in first["events"] + last["events"]}), 105)

    def test_long_poll_wakes_for_new_committed_mail(self):
        ready = threading.Event()

        def poll():
            ready.set()
            return self.views.changes("alice", wait=2)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(poll)
            self.assertTrue(ready.wait(1))
            mail = self.receive()["id"]
            result = future.result(timeout=2)
        self.assertEqual(result["events"][0]["id"], mail)

    def test_long_poll_can_cancel(self):
        cancel = threading.Event()
        cancel.set()
        start = time.monotonic()
        self.assertEqual(
            self.views.changes("alice", wait=2, cancel=cancel)["events"], []
        )
        self.assertLess(time.monotonic() - start, 0.5)

    def test_cursor_lineage_future_and_strict_limits(self):
        for params in (
            {"after": True},
            {"after": 1},
            {"wait": float("nan")},
            {"wait": True},
            {"wait": 3},
            {"lineage": ""},
        ):
            with self.assertRaises(ValueError):
                self.views.changes("alice", **params)
        for params in (
            {"lineage": "wrong"},
            {"after": 1, "lineage": self.views.watermark()["lineage"]},
        ):
            with self.assertRaises(Error) as caught:
                self.views.changes("alice", **params)
            self.assertEqual(caught.exception.status, 409)

    def test_read_barrier_rejects_lag_and_replacement_lineage(self):
        self.receive()
        current = self.views.watermark()
        self.views.ensure(current["position"], current["lineage"])
        for position, lineage, status in (
            (2, current["lineage"], 503),
            (1, "other", 409),
        ):
            with self.assertRaises(Error) as caught:
                self.views.ensure(position, lineage)
            self.assertEqual(caught.exception.status, status)

    def test_replica_changes_and_read_barrier_follow_apply(self):
        replica = MailStore(self.root / "replica.sqlite", replica=True)
        views = MailboxViews(replica)
        with self.assertRaises(Error):
            views.watermark()
        mail = self.receive()["id"]
        replica.apply(self.store.changes(0))
        self.assertEqual(views.changes("alice")["events"][0]["id"], mail)
        self.store.update("alice", mail, 1, True)
        current = self.views.watermark()
        with self.assertRaises(Error):
            views.ensure(current["position"], current["lineage"])
        replica.apply(self.store.changes(1))
        views.ensure(current["position"], current["lineage"])

    def test_legacy_replica_missing_history_requires_refresh(self):
        self.receive()
        with self.store.transaction(True) as db:
            db.execute("DELETE FROM changes")
        with self.assertRaises(Error) as caught:
            self.views.changes("alice")
        self.assertEqual(caught.exception.status, 409)
        current = self.views.watermark()
        self.assertEqual(
            self.views.changes("alice", current["position"], current["lineage"])[
                "events"
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()
