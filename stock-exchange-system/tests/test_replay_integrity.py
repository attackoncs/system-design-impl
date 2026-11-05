"""Typed replay identity and irreversible market order updates."""

import copy
import json
from pathlib import Path

from helpers_review import ExchangeCase
from stock_exchange.common import Error, checksum, encode
from stock_exchange.storage import Engine


class ReplayIntegrityTests(ExchangeCase):
    def test_promotion_rejects_equal_python_values_with_different_json_types(self):
        before = self.sequencer.control()
        for execution_seq in (False, 0.0):
            with self.subTest(execution_seq=execution_seq):
                candidate = self.engine.raw()
                candidate["state"]["execution_seq"] = execution_seq
                candidate["state_hash"] = checksum(candidate["state"])
                with self.assertRaises(Error) as caught:
                    self.sequencer.promote("backup", candidate)
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(self.sequencer.control(), before)

    def test_projection_audit_detects_type_drift(self):
        state = self.engine.raw()["state"]
        state["execution_seq"] = False
        with self.engine.transaction() as db:
            db.execute("UPDATE projection SET state=?", (encode(state),))
        self.assertFalse(self.engine.audit()["projection_matches"])
        self.engine.rebuild()
        self.assertTrue(self.engine.audit()["projection_matches"])
        self.assertIs(type(self.engine.raw()["state"]["execution_seq"]), int)

    def test_self_consistent_snapshot_cannot_change_integer_to_boolean(self):
        self.engine.snapshot()
        path = Path(str(self.engine.path) + ".snapshot.json")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot["payload"]["state"]["execution_seq"] = False
        snapshot["checksum"] = checksum(snapshot["payload"])
        path.write_text(encode(snapshot), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "snapshot"):
            Engine(self.engine.path)

    def test_non_integer_balance_blocks_matching_until_rebuild(self):
        state = self.engine.raw()["state"]
        state["accounts"]["alice"]["cash"]["available"] = 1_000_000.0
        with self.engine.transaction() as db:
            db.execute("UPDATE projection SET state=?", (encode(state),))
        self.assertFalse(self.engine.audit()["projection_matches"])
        self.sequencer.submit("alice", "new", self.body())
        row = self.sequencer.events(1)[0]
        with self.assertRaises(Error) as caught:
            self.engine.apply(row)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.engine.cursor()["seq"], 1)
        self.engine.rebuild()
        self.engine.apply(row)
        self.assertEqual(self.engine.cursor()["seq"], 2)
        self.assertTrue(self.engine.audit()["matches"])

    def test_canonical_state_comparison_ignores_dictionary_insertion_order(self):
        candidate = self.engine.raw()
        candidate["state"] = dict(reversed(list(candidate["state"].items())))
        candidate["state_hash"] = checksum(candidate["state"])
        result = self.sequencer.promote("backup", candidate)
        self.assertEqual((result["active"], result["epoch"]), ("backup", 2))


class MarketTransitionTests(ExchangeCase):
    def test_unfilled_order_cannot_change_owner_in_later_batch(self):
        self.submit("ask", owner="bob", side="SELL")
        before = self.market.view()
        self.submit("other", symbol="MSFT", publish=False)
        batch = self.engine.feed(before["seq"])["batches"][0]
        changed = copy.deepcopy(before["orders"]["ask"])
        changed["owner"] = "carol"
        batch["orders"].append(changed)
        with self.assertRaisesRegex(ValueError, "order update"):
            self.market.apply(batch)
        self.assertEqual(self.market.view(), before)

    def test_canceled_order_cannot_reopen_even_with_matching_book_levels(self):
        self.submit("ask", owner="bob", side="SELL")
        self.submit("ask", owner="bob", kind="cancel")
        before = self.market.view()
        self.submit("other", owner="carol", price=90, publish=False)
        batch = self.engine.feed(before["seq"])["batches"][0]
        reopened = copy.deepcopy(before["orders"]["ask"])
        reopened.update(status="OPEN", reason="")
        batch["orders"].append(reopened)
        batch["books"]["AAPL"]["asks"]["100"] = 1
        with self.assertRaisesRegex(ValueError, "order update"):
            self.market.apply(batch)
        self.assertEqual(self.market.view(), before)

    def test_new_order_sequence_must_match_its_batch(self):
        self.submit("first")
        before = self.market.view()
        self.submit("other", symbol="MSFT", publish=False)
        batch = self.engine.feed(before["seq"])["batches"][0]
        batch["orders"][0]["seq"] = before["seq"]
        with self.assertRaisesRegex(ValueError, "order sequence"):
            self.market.apply(batch)
        self.assertEqual(self.market.view(), before)
