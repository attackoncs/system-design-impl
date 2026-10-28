from concurrent.futures import ThreadPoolExecutor
import copy
import threading
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from helpers_review import ExchangeCase
from stock_exchange.common import Error, checksum, encode


class MarketSafetyTests(ExchangeCase):
    def pending(self):
        self.submit("ask", "bob", side="SELL", quantity=2, publish=False)
        return self.engine.feed(1)["batches"][0]

    def test_changed_duplicate_payload_with_same_head_is_conflict(self):
        batch = self.pending()
        self.market.apply(batch)
        before = self.market.view()
        changed = copy.deepcopy(batch)
        changed["books"]["AAPL"]["asks"]["100"] = 99
        with self.assertRaises(Error) as error:
            self.market.apply(changed)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.market.view(), before)

    def test_invalid_level_update_rolls_back_cursor_and_private_reports(self):
        batch = self.pending()
        for changes in ({"100": True}, {"100": -1}, {"100": 5}, {"0100": 2}):
            value = copy.deepcopy(batch)
            value["books"]["AAPL"]["asks"] = changes
            before = self.market.view()
            with self.assertRaises(ValueError):
                self.market.apply(value)
            self.assertEqual(self.market.view(), before)
        self.market.apply(batch)
        self.assertEqual(self.market.view()["seq"], 2)

    def test_stale_snapshot_and_stale_epoch_cannot_rewind_market(self):
        old = self.engine.raw()
        self.pending()
        self.market.replace(self.engine.raw(), epoch=2)
        before = self.market.view()
        with self.assertRaises(Error):
            self.market.replace(old, epoch=2)
        self.submit("next", publish=False)
        batch = self.engine.feed(2)["batches"][0]
        with self.assertRaises(Error):
            self.market.apply(batch, epoch=1)
        self.assertEqual(self.market.view(), before)

    def test_same_prefix_valid_but_changed_snapshot_state_is_conflict(self):
        self.submit()
        snapshot = self.engine.raw()
        snapshot["state"]["settings"]["daily_limit"] = 999999
        snapshot["state_hash"] = checksum(snapshot["state"])
        with self.assertRaises(Error):
            self.market.replace(snapshot)

    def test_bad_snapshot_checksum_does_not_change_projection(self):
        snapshot = self.engine.raw()
        snapshot["state"]["accounts"]["alice"]["cash"]["available"] += 1
        before = self.market.view()
        with self.assertRaises(ValueError):
            self.market.replace(snapshot)
        self.assertEqual(self.market.view(), before)

    def test_old_unverifiable_duplicate_requires_snapshot(self):
        batch = self.pending()
        self.market.replace(self.engine.raw())
        with self.assertRaises(Error) as error:
            self.market.apply(batch)
        self.assertEqual(error.exception.status, 410)

    def test_legacy_market_projection_upgrades_only_from_verified_snapshot(self):
        self.submit("ask", "bob", side="SELL")
        self.submit("take")
        legacy = self.market.view()
        for key in ("source_hash", "epoch", "settings", "resyncs"):
            legacy.pop(key)
        for trade in legacy["trades"]:
            trade.pop("execution_seq")
        with self.market.transaction() as db:
            db.execute("UPDATE projection SET value=?", (encode(legacy),))
        with self.assertRaises(Error):
            self.market.view()
        self.market.replace(self.engine.raw(), epoch=1)
        self.assertEqual(self.market.view()["trades"][0]["execution_seq"], 1)

    def test_duplicate_execution_and_changed_order_identity_are_atomic_rejections(self):
        self.submit("ask", "bob", side="SELL", quantity=2)
        self.submit("take", publish=False)
        batch = self.engine.feed(2)["batches"][0]
        for field in ("trades", "orders"):
            changed = copy.deepcopy(batch)
            changed[field].append(copy.deepcopy(changed[field][0]))
            before = self.market.view()
            with self.assertRaises(ValueError):
                self.market.apply(changed)
            self.assertEqual(self.market.view(), before)

    def adapter(self, url, token, body=None, **kwargs):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path == "/internal/control":
            return self.sequencer.control()
        if parsed.path == "/internal/events":
            return {"events": self.sequencer.events(int(query["after"][0]))}
        if parsed.path == "/internal/feed":
            return self.engine.feed(int(query["after"][0]))
        return self.engine.raw()

    def test_promotion_during_poll_rejects_inflight_old_publisher_batch(self):
        self.pending()
        calls = 0

        def adapter(url, *args, **kwargs):
            nonlocal calls
            value = self.adapter(url, *args, **kwargs)
            if url.endswith("/internal/control"):
                calls += 1
                if calls > 1:
                    value.update(epoch=2, active="backup")
            return value

        before = self.market.view()
        with patch("stock_exchange.market.request", side_effect=adapter):
            with self.assertRaises(Error):
                self.market.poll(self.config)
        self.assertEqual(self.market.view(), before)

    def test_market_reads_remain_available_while_poll_rpc_is_blocked(self):
        entered, release = threading.Event(), threading.Event()

        def adapter(url, *args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test release missing")
            return self.adapter(url, *args, **kwargs)

        with (
            patch("stock_exchange.market.request", side_effect=adapter),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            future = pool.submit(self.market.poll, self.config)
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(pool.submit(self.market.view).result(0.5)["seq"], 1)
            finally:
                release.set()
            future.result(2)

    def test_poll_checkpoint_must_belong_to_sequencer_history(self):
        self.pending()

        def adapter(url, *args, **kwargs):
            value = self.adapter(url, *args, **kwargs)
            if "/internal/feed" in url:
                value["batches"][0]["head"] = "f" * 64
            return value

        with patch("stock_exchange.market.request", side_effect=adapter):
            with self.assertRaises(ValueError):
                self.market.poll(self.config)
        self.assertEqual(self.market.view()["seq"], 1)
