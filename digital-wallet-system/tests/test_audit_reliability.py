"""Audit routing and accumulated balances use the configured supply boundary."""

from unittest.mock import patch

from digital_wallet.common import Error
from digital_wallet.coordinator import Coordinator
from digital_wallet.shard import Shard
from helpers_review import AuthorityCase


class AuditReliabilityTests(AuthorityCase):
    def test_swapped_shards_cannot_pass_equal_supply_audit(self):
        original = self.store.shard

        def swapped(index, path, body=None):
            return original(1 - index, path, body)

        with patch.object(self.store, "shard", side_effect=swapped):
            result = self.store.audit()
        self.assertFalse(result["matches"])
        self.assertEqual(result["wallet_total"], result["opening_supply"])
        self.assertTrue(any("shard identity" in error for error in result["errors"]))
        self.assertTrue(self.store.audit()["matches"])

    def test_equal_supply_wrong_account_is_detected(self):
        original = self.store.shard

        def wrong_account(index, path, body=None):
            value = original(index, path, body)
            if index == 0 and path == "/internal/view":
                value["accounts"]["unconfigured"] = value["accounts"].pop(self.source)
            return value

        with patch.object(self.store, "shard", side_effect=wrong_account):
            result = self.store.audit()
        self.assertFalse(result["matches"])
        self.assertEqual(result["wallet_total"], result["opening_supply"])
        self.assertIn("account distribution mismatch: 0", result["errors"])

    def test_malformed_audit_receipts_are_upstream_errors(self):
        original = self.store.shard
        mutations = (
            ("/internal/view", lambda value: value.update(shard=False)),
            ("/internal/view", lambda value: value.update(seq=True)),
            ("/internal/view", lambda value: value.pop("accounts")),
            (
                "/internal/view",
                lambda value: value["accounts"][self.source].update(held=True),
            ),
            (
                "/internal/view",
                lambda value: value["accounts"][self.source].update(available=-1),
            ),
            (
                "/internal/view",
                lambda value: value["accounts"][self.source].update(available=30001),
            ),
            ("/internal/audit", lambda value: value.update(matches=1)),
            ("/internal/audit", lambda value: value.update(events=True)),
            ("/internal/view", lambda value: value["parts"].update(transfer=True)),
            (
                "/internal/view",
                lambda value: value["parts"].update(transfer={"role": "source"}),
            ),
            (
                "/internal/view",
                lambda value: value["parts"].update(
                    transfer={
                        "intent": self.body(),
                        "role": "unknown",
                        "status": "PREPARED",
                    }
                ),
            ),
            (
                "/internal/view",
                lambda value: value["parts"].update(
                    transfer={
                        "intent": self.body(),
                        "role": "source",
                        "status": False,
                    }
                ),
            ),
        )
        for path, mutate in mutations:
            with self.subTest(path=path, mutate=mutate):

                def malformed(index, route, body=None):
                    value = original(index, route, body)
                    if index == 0 and route == path:
                        mutate(value)
                    return value

                with patch.object(self.store, "shard", side_effect=malformed):
                    with self.assertRaises(Error) as caught:
                        self.store.audit()
                self.assertEqual(caught.exception.status, 502)
        self.assertTrue(self.store.audit()["matches"])


class BalanceBoundaryTests(AuthorityCase):
    def setUp(self):
        super().setUp()
        self.config["accounts"][self.peer]["opening"] = 10**12
        self.store = Coordinator(
            self.root / "large-coordinator.sqlite", self.config, clock=lambda: 100
        )
        self.addCleanup(self.store.close)
        self.shards = [
            Shard(
                self.root / ("large-shard" + str(index) + ".sqlite"),
                index,
                2,
                {key: row["opening"] for key, row in self.config["accounts"].items()},
            )
            for index in range(2)
        ]
        self.store.shard = self.shard_rpc

    def test_accumulated_balance_can_exceed_single_account_opening_limit(self):
        self.store.create(self.body(local=True), "alice")
        self.assertEqual(self.finish()["status"], "SUCCEEDED")
        balance = self.store.balance(self.peer, "alice")
        self.assertEqual(balance["available"], 10**12 + 100)
        self.assertEqual(balance["held"], 0)
        self.assertTrue(self.store.audit()["matches"])

    def test_balance_receipt_still_rejects_invalid_or_excess_supply(self):
        original = self.store.shard
        supply = sum(row["opening"] for row in self.config["accounts"].values())
        for available in (True, -1, supply + 1):
            with self.subTest(available=available):

                def malformed(index, path, body=None):
                    return {**original(index, path, body), "available": available}

                with patch.object(self.store, "shard", side_effect=malformed):
                    with self.assertRaises(Error) as caught:
                        self.store.balance(self.peer, "alice")
                self.assertEqual(caught.exception.status, 502)
