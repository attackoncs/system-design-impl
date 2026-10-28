from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab


class CompletionNetworkTests(unittest.TestCase):
    def test_same_request_can_wait_pending_timeout_terminal_and_restart(self):
        with Lab() as lab:
            lab.stop("worker")
            value = lab.call(
                "/v1/wallet/balance_transfer?wait_ms=30", lab.body("waited", 500)
            )
            self.assertEqual(value["status"], "PENDING")
            self.assertEqual(lab.balance()["held"], 0)
            lab.start("worker")
            value = lab.call("/v1/transfers/waited?wait_ms=2000")
            self.assertEqual(value["status"], "SUCCEEDED")
            # Exact retries retain the original money identity and terminal fact.
            self.assertEqual(
                lab.call(
                    "/v1/wallet/balance_transfer?wait_ms=2000", lab.body("waited", 500)
                ),
                value,
            )
            lab.stop("coordinator")
            lab.start("coordinator")
            self.assertEqual(lab.call("/v1/transfers/waited?wait_ms=2000"), value)
            self.assertEqual(lab.balance()["available"], 9500)
            self.assertTrue(lab.audit()["matches"])
