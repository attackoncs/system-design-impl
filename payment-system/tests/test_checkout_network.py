from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from checkout_demo import scenario


class CheckoutNetworkTests(unittest.TestCase):
    def test_multi_order_idempotency_independent_outcomes_and_balanced_funds(self):
        result = scenario()
        self.assertEqual(result["order_states"], ["SUCCEEDED", "FAILED"])
        self.assertEqual(result["unique_provider_operations"], 4)
        self.assertTrue(result["reconciliation_matches"])
