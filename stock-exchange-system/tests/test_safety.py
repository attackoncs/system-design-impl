from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import random
import threading
from unittest.mock import patch

from helpers_review import ExchangeCase
from stock_exchange.common import Error, checksum, encode
from stock_exchange.contracts import validate
from stock_exchange.matching import audit, book
from stock_exchange.storage import Engine, Sequencer


class SafetyTests(ExchangeCase):
    def test_sequencer_freezes_input_before_sql_writer_wait(self):
        original = self.body()
        frozen = threading.Event()

        def freeze(value, kind):
            result = validate(value, kind)
            frozen.set()
            return result

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch("stock_exchange.storage.validate", side_effect=freeze),
        ):
            with self.sequencer.transaction():
                future = pool.submit(self.sequencer.submit, "alice", "new", original)
                self.assertTrue(frozen.wait(1))
                original["quantity"] = 999
            future.result(2)
        self.assertEqual(self.sequencer.events(1)[0]["event"]["body"]["quantity"], 1)

    def test_settings_are_frozen_and_unknown_owner_cannot_poison_command_log(self):
        self.config["settings"]["accounts"]["alice"]["cash"] = 0
        self.assertEqual(self.sequencer.settings["accounts"]["alice"]["cash"], 1000000)
        with self.assertRaises(Error):
            self.sequencer.submit("unknown", "new", self.body())
        self.assertEqual(self.sequencer.control()["seq"], 1)

    def test_dedup_index_damage_fails_restart_instead_of_readmitting_request(self):
        self.submit()
        with self.sequencer.transaction() as db:
            db.execute("DELETE FROM requests")
        with self.assertRaisesRegex(ValueError, "deduplication"):
            Sequencer(
                self.root / "sequencer.sqlite",
                self.config["settings"],
                self.config["simulation_timestamp"],
            )

    def test_same_prefix_candidate_with_changed_balances_cannot_be_promoted(self):
        self.submit()
        value = self.engine.raw()
        value["state"]["accounts"]["alice"]["cash"]["available"] -= 10
        value["state"]["accounts"]["bob"]["cash"]["available"] += 10
        value["state_hash"] = checksum(value["state"])
        with self.assertRaises(Error):
            self.sequencer.promote("backup", value)
        self.assertEqual(self.sequencer.control()["epoch"], 1)
        self.assertFalse(audit(value["state"])["matches"])

    def test_fifo_middle_cancel_partial_head_and_json_round_trip(self):
        for name in ("z", "m", "a"):
            self.submit(name, "bob", side="SELL", quantity=4)
        self.submit("m", "bob", kind="cancel")
        self.submit("take", quantity=5)
        state = self.engine.raw()["state"]
        self.assertEqual(
            [(row["sell_order"], row["quantity"]) for row in state["trades"]],
            [("z", 4), ("a", 1)],
        )
        self.assertEqual(book(state, "AAPL")["asks"], {"100": 3})
        self.assertTrue(audit(json.loads(encode(state)))["matches"])

    def test_price_priority_against_independent_sorted_open_order_oracle(self):
        rng = random.Random(29)
        for i in range(45):
            self.submit(
                "ask_" + str(i),
                "bob" if i % 2 else "carol",
                side="SELL",
                price=rng.randint(98, 109),
                quantity=rng.randint(1, 4),
                publish=False,
            )
        for i in range(0, 45, 5):
            self.submit(
                "ask_" + str(i),
                "bob" if i % 2 else "carol",
                kind="cancel",
                publish=False,
            )
        before = self.engine.raw()["state"]
        remaining, expected = 40, []
        for order in sorted(
            before["orders"].values(), key=lambda row: (row["price"], row["seq"])
        ):
            if order["status"] != "OPEN" or order["price"] > 105 or not remaining:
                continue
            quantity = min(remaining, order["remaining"])
            expected.append((order["order_id"], quantity, order["price"]))
            remaining -= quantity
        self.submit("take", price=105, quantity=40, publish=False)
        state = self.engine.raw()["state"]
        self.assertEqual(
            [
                (row["sell_order"], row["quantity"], row["price"])
                for row in state["trades"]
            ],
            expected,
        )
        self.assertTrue(audit(state)["matches"])

    def test_cancelled_price_reactivation_preserves_best_level_priority(self):
        self.submit("old", "bob", side="SELL", price=99)
        self.submit("old", "bob", kind="cancel")
        self.submit("high", "bob", side="SELL", price=101)
        self.submit("new", "carol", side="SELL", price=99)
        self.submit("take", price=105, quantity=2)
        self.assertEqual(
            [row["sell_order"] for row in self.engine.raw()["state"]["trades"]],
            ["new", "high"],
        )

    def test_dense_outbound_execution_sequence_is_shared_by_both_reports(self):
        self.submit("ask", "bob", side="SELL", quantity=2)
        self.submit("rejected", price=100000000)
        self.submit("take", quantity=1)
        self.submit("take2", quantity=1)
        trades = self.engine.raw()["state"]["trades"]
        self.assertEqual([row["execution_seq"] for row in trades], [1, 2])
        self.assertEqual(
            [
                row["execution_seq"]
                for row in self.market.executions(
                    self.market.view(), "alice", {"symbol": "AAPL"}
                )
            ],
            [1, 2],
        )

    def test_legacy_snapshot_is_verified_then_upgraded_to_current_projection(self):
        self.submit("ask", "bob", side="SELL")
        self.submit("take")
        current = self.engine.raw()
        legacy = copy.deepcopy(current["state"])
        for key in ("book_index", "execution_seq", "last_seq", "last_timestamp"):
            legacy.pop(key)
        for row in legacy["trades"]:
            row.pop("execution_seq")
        payload = {"seq": current["seq"], "head": current["head"], "state": legacy}
        Path(str(self.engine.path) + ".snapshot.json").write_text(
            encode({"payload": payload, "checksum": checksum(payload)}),
            encoding="utf-8",
        )
        restored = Engine(self.engine.path)
        self.assertEqual(restored.raw(), current)
        self.assertTrue(restored.audit()["projection_matches"])

    def test_concurrent_engine_reads_keep_head_sequence_and_state_coherent(self):
        def write():
            for i in range(15):
                self.submit("bid_" + str(i), publish=False)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(write)
            for _ in range(50):
                value = self.engine.raw()
                self.assertEqual(value["seq"], value["state"]["last_seq"])
                self.assertEqual(value["state_hash"], checksum(value["state"]))
            future.result(5)

    def test_concurrent_snapshot_publications_and_tail_recovery(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.engine.snapshot) for _ in range(8)]
            self.submit("tail", publish=False)
            for future in futures:
                future.result(3)
        before = self.engine.raw()
        restored = Engine(self.engine.path)
        self.assertEqual(restored.raw(), before)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_command_rollback_keeps_projection_events_and_batches_together(self):
        result = self.sequencer.submit("alice", "new", self.body())
        row = self.sequencer.events(result["seq"] - 1)[0]
        before = self.engine.raw()
        with patch(
            "stock_exchange.storage.market_batch", side_effect=RuntimeError("injected")
        ):
            with self.assertRaises(RuntimeError):
                self.engine.apply(row)
        self.assertEqual(self.engine.raw(), before)
        self.engine.apply(row)
        self.assertTrue(self.engine.audit()["matches"])

    def test_index_drift_and_equal_supply_account_drift_are_detected(self):
        self.submit(quantity=2)
        state = self.engine.raw()["state"]
        state["book_index"]["AAPL"]["BUY"]["levels"]["100"]["quantity"] = 1
        self.assertFalse(audit(state)["matches"])
        state = self.engine.raw()["state"]
        state["accounts"]["alice"]["cash"]["available"] -= 1
        state["accounts"]["bob"]["cash"]["available"] += 1
        self.assertFalse(audit(state)["matches"])

    def test_projection_index_corruption_blocks_new_funds_until_rebuild(self):
        self.submit("ask", "bob", side="SELL", price=100)
        sequence = self.sequencer.submit("alice", "new", self.body("take"))["seq"]
        row = self.sequencer.events(sequence - 1)[0]
        state = self.engine.raw()["state"]
        state["book_index"]["AAPL"]["SELL"]["heap"] = []
        with self.engine.transaction() as db:
            db.execute("UPDATE projection SET state=?", (encode(state),))
        with self.assertRaises(Error):
            self.engine.apply(row)
        self.assertEqual(self.engine.cursor()["seq"], sequence - 1)
        self.engine.rebuild()
        self.engine.apply(row)
        self.assertEqual(len(self.engine.raw()["state"]["trades"]), 1)
        self.assertTrue(self.engine.audit()["projection_matches"])
