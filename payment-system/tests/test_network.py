from pathlib import Path
import sys
import time
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab, wait
from payment_system.common import dumps, http_json, signature


class NetworkTests(unittest.TestCase):
    def test_hosted_payin_payout_refund_reconciliation_and_isolation(self):
        with Lab() as lab:
            body = {
                "request_key": "first",
                "order_id": "order_first",
                "amount": 10000,
                "currency": "USD",
            }
            op = lab.request("/v1/payments", body)
            checkout = wait(lambda: lab.operation(op["id"])["checkout"])
            with urlopen(checkout, timeout=5) as response:
                self.assertIn(b"Simulated checkout", response.read())
                self.assertIn("text/html", response.headers["Content-Type"])
            self.assertEqual(lab.operation(op["id"])["status"], "PENDING")
            self.assertEqual(lab.request("/v1/wallet")["available"], 0)
            http_json(checkout, body={"choice": "test-success"})
            lab.await_state(op["id"])
            self.assertEqual(lab.request("/v1/payments", body)["id"], op["id"])
            with self.assertRaises(HTTPError) as error:
                lab.request("/v1/operations/" + op["id"], auth="other")
            self.assertEqual(error.exception.code, 404)
            with self.assertRaises(HTTPError) as error:
                lab.request("/admin/status")
            self.assertEqual(error.exception.code, 401)
            with self.assertRaises(HTTPError) as error:
                lab.request("/v1/payments", {**body, "card_number": "not-accepted"})
            self.assertEqual(error.exception.code, 400)
            payout = lab.request(
                "/v1/payouts",
                {
                    "request_key": "payout",
                    "amount": 3000,
                    "currency": "USD",
                    "destination": "test-bank-ok",
                },
            )
            lab.await_state(payout["id"])
            refund = lab.request(
                "/v1/refunds",
                {
                    "request_key": "refund",
                    "amount": 2000,
                    "currency": "USD",
                    "payment_id": op["id"],
                },
            )
            lab.await_state(refund["id"])
            lab.settled()
            self.assertEqual(
                lab.request("/v1/wallet"),
                {"merchant": "seller", "available": 5000, "held": 0, "currency": "USD"},
            )
            self.assertEqual(len(lab.request("/v1/ledger")["transactions"]), 5)
            self.assertEqual(
                lab.request("/admin/status", role="provider", auth="admin")[
                    "unique_operations"
                ],
                3,
            )
            self.assertEqual(
                lab.request("/admin/status", role="receiver", auth="admin")[
                    "unique_events"
                ],
                3,
            )
            self.assertTrue(lab.reconcile()["result"]["matches"])
            lab.control(statement_delta=1)
            self.assertFalse(lab.reconcile()["result"]["matches"])
            self.assertEqual(lab.request("/v1/wallet")["available"], 5000)
            lab.control()
            self.assertTrue(lab.reconcile()["result"]["matches"])

    def test_signed_duplicate_reordered_and_conflicting_callbacks(self):
        with Lab() as lab:
            op = lab.pay()
            receipt = lab.request(
                "/operations/" + op["id"], role="provider", auth="provider"
            )

            def callback(receipt, event="manual_event", valid=True):
                body = {"event_id": event, "receipt": receipt}
                stamp = int(time.time())
                return http_json(
                    lab.config["api_url"] + "/webhooks/mock",
                    body=body,
                    headers={
                        "X-Timestamp": str(stamp),
                        "X-Signature": signature(
                            lab.config["webhook_secret"] if valid else "wrong",
                            stamp,
                            dumps(body).encode(),
                        ),
                    },
                )

            with self.assertRaises(HTTPError) as error:
                callback(receipt, valid=False)
            self.assertEqual(error.exception.code, 401)
            callback(receipt)
            self.assertTrue(callback(receipt)["duplicate"])
            self.assertEqual(
                callback({**receipt, "revision": 1, "status": "PENDING"}, "old")[
                    "ignored"
                ],
                "older revision",
            )
            self.assertIn(
                "review", callback({**receipt, "amount": 10001}, "wrong_amount")
            )
            with self.assertRaises(HTTPError) as error:
                callback({**receipt, "amount": 10001})
            self.assertEqual(error.exception.code, 409)
            self.assertEqual(lab.request("/v1/wallet")["available"], 10000)
            self.assertEqual(len(lab.request("/v1/ledger")["transactions"]), 1)

    def test_lost_provider_commit_unknown_hold_dead_letter_and_restart(self):
        with Lab() as lab:
            lab.pay()
            lab.settled()
            lab.stop("worker")
            lab.control(lose_response=True, webhooks_down=True)
            payout = lab.request(
                "/v1/payouts",
                {
                    "request_key": "uncertain",
                    "amount": 3000,
                    "currency": "USD",
                    "destination": "test-bank-ok",
                },
            )
            # Commit at PSP while the delivery worker is offline, then lose the reply.
            intent = {
                k: payout[k]
                for k in (
                    "id",
                    "merchant",
                    "kind",
                    "parent",
                    "amount",
                    "currency",
                    "destination",
                )
            }
            with self.assertRaises(HTTPError) as error:
                lab.request("/operations", intent, role="provider", auth="provider")
            self.assertEqual(error.exception.code, 503)
            lab.control(webhooks_down=True, query_down=True)
            lab.start("worker")
            wait(
                lambda: lab.request("/admin/status", auth="admin")["dead_jobs"] == 1,
                timeout=15,
            )
            self.assertEqual(lab.operation(payout["id"])["status"], "UNKNOWN")
            self.assertEqual(lab.request("/v1/wallet")["held"], 3000)
            self.assertEqual(lab.request("/v1/wallet")["available"], 7000)
            for role in ("worker", "api", "provider"):
                lab.stop(role)
            for role in ("api", "provider"):
                lab.start(role)
            lab.control(webhooks_down=True)
            lab.request("/admin/retry/" + payout["id"], {}, auth="admin")
            lab.start("worker")
            lab.await_state(payout["id"])
            lab.settled()
            self.assertEqual(lab.request("/v1/wallet")["held"], 0)
            self.assertEqual(lab.request("/v1/wallet")["available"], 7000)
            self.assertEqual(
                lab.request("/admin/status", role="provider", auth="admin")[
                    "unique_operations"
                ],
                2,
            )
            self.assertEqual(len(lab.request("/v1/ledger")["transactions"]), 3)
            self.assertTrue(lab.reconcile()["result"]["matches"])

    def test_lost_consumer_ack_restart_and_failed_payout_releases_hold(self):
        with Lab() as lab:
            lab.request(
                "/admin/control", {"lose_ack": True}, role="receiver", auth="admin"
            )
            lab.pay()
            wait(
                lambda: (
                    lab.request("/admin/status", role="receiver", auth="admin")[
                        "delivery_attempts"
                    ]
                    >= 2
                )
            )
            lab.stop("worker")
            lab.request(
                "/admin/control", {"lose_ack": False}, role="receiver", auth="admin"
            )
            lab.start("worker")
            lab.settled()
            received = lab.request("/admin/status", role="receiver", auth="admin")
            self.assertEqual(received["unique_events"], 1)
            self.assertGreaterEqual(received["delivery_attempts"], 3)
            payout = lab.request(
                "/v1/payouts",
                {
                    "request_key": "declined",
                    "amount": 1000,
                    "currency": "USD",
                    "destination": "test-bank-decline",
                },
            )
            lab.await_state(payout["id"], "FAILED")
            lab.settled()
            self.assertEqual(lab.request("/v1/wallet")["available"], 10000)
            self.assertEqual(lab.request("/v1/wallet")["held"], 0)
            self.assertTrue(
                lab.request("/admin/status", auth="admin")["audit"]["matches"]
            )


if __name__ == "__main__":
    unittest.main()
