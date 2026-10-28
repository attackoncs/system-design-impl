from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import patch

from helpers_review import AuthorityCase
from digital_wallet.common import Error


class CompletionTests(AuthorityCase):
    def test_timeout_preserves_pending_and_frozen_funds(self):
        self.store.create(self.body(), "alice")
        self.store.step()
        started = time.monotonic()
        self.assertEqual(
            self.store.wait_owned("transfer", "alice", 30)["status"], "PENDING"
        )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(self.shards[0].balance(self.source)["held"], 100)

    def test_terminal_fact_wakes_waiter_without_holding_money_writer(self):
        self.store.create(self.body(), "alice")
        entered = threading.Event()
        original = self.store.owned

        def owned(*args):
            entered.set()
            return original(*args)

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch.object(self.store, "owned", side_effect=owned),
        ):
            future = pool.submit(self.store.wait_owned, "transfer", "alice", 2000)
            self.assertTrue(entered.wait(1))
            self.finish()
            self.assertEqual(future.result(2)["status"], "SUCCEEDED")
        self.assertEqual(self.shards[0].balance(self.source)["available"], 9900)

    def test_waiters_have_separate_capacity_and_release_after_not_found(self):
        for _ in range(8):
            self.assertTrue(self.store.wait_slots.acquire(False))
        try:
            with self.assertRaises(Error) as caught:
                self.store.wait_owned("missing", "alice", 100)
            self.assertEqual(caught.exception.status, 503)
            self.store.create(self.body(), "alice")
        finally:
            for _ in range(8):
                self.store.wait_slots.release()
        with self.assertRaises(Error) as caught:
            self.store.wait_owned("missing", "bob", 100)
        self.assertEqual(caught.exception.status, 404)
        for _ in range(8):
            self.assertTrue(self.store.wait_slots.acquire(False))
        for _ in range(8):
            self.store.wait_slots.release()

    def test_cancelled_waiter_does_not_cancel_transfer(self):
        self.store.create(self.body(), "alice")
        stop, entered = threading.Event(), threading.Event()

        def wait():
            with self.store.runtime.operation(time.monotonic() + 5, stop):
                entered.set()
                return self.store.wait_owned("transfer", "alice", 2000)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(wait)
            self.assertTrue(entered.wait(1))
            stop.set()
            with self.assertRaises(Error):
                future.result(1)
        self.assertEqual(self.store.owned("transfer", "alice")["status"], "PENDING")
        self.assertEqual(self.finish()["status"], "SUCCEEDED")

    def test_balance_read_can_finish_while_money_writer_is_owned(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.runtime.lock:
                value = pool.submit(self.store.balance, self.source, "alice").result(1)
        self.assertEqual(value["available"], 10000)

    def test_writer_wait_uses_shared_deadline_and_retains_no_new_command(self):
        def create():
            with self.store.runtime.operation(time.monotonic() + 0.05):
                return self.store.create(self.body(), "alice")

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.runtime.lock:
                future = pool.submit(create)
                with self.assertRaises(Error):
                    future.result(1)
        self.assertEqual(self.store.state(), {})

    def test_close_wakes_waiters_and_drains_owned_replica_workers(self):
        self.store.create(self.body(), "alice")
        entered = threading.Event()

        def wait():
            entered.set()
            return self.store.wait_owned("transfer", "alice", 2000)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(wait)
            self.assertTrue(entered.wait(1))
            self.store.close()
            with self.assertRaises(Error):
                future.result(1)
        self.assertFalse(
            any(thread.is_alive() for thread in self.store.journal.pool._threads)
        )
