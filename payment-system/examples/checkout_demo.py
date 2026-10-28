"""Independent merchant orders, followed by simulated payout and refund."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Lab, wait
from payment_system.common import http_json


def scenario():
    with Lab() as lab:
        body = {
            "checkout_id": "checkout-demo",
            "orders": [
                {
                    "merchant": merchant,
                    "order_id": "order",
                    "amount": amount,
                    "currency": "USD",
                }
                for merchant, amount in (("seller", 1000), ("other", 2000))
            ],
        }
        first = lab.request("/internal/checkouts", body, auth="internal")
        again = lab.request("/internal/checkouts", body, auth="internal")
        assert [op["id"] for op in first["orders"]] == [
            op["id"] for op in again["orders"]
        ]
        for op, choice in zip(first["orders"], ["test-success", "test-decline"]):
            checkout = wait(
                lambda: lab.request("/v1/operations/" + op["id"], auth=op["merchant"])[
                    "checkout"
                ]
            )
            http_json(checkout, body={"choice": choice})

        def completed():
            value = lab.request("/internal/checkouts/checkout-demo", auth="internal")
            return (
                value
                if all(
                    op["status"] in ("SUCCEEDED", "FAILED") for op in value["orders"]
                )
                else None
            )

        batch = wait(completed)
        payin = next(op for op in batch["orders"] if op["merchant"] == "seller")
        payout = lab.request(
            "/v1/payouts",
            {
                "request_key": "out",
                "amount": 300,
                "currency": "USD",
                "destination": "test-bank-ok",
            },
        )
        lab.await_state(payout["id"])
        refund = lab.request(
            "/v1/refunds",
            {
                "request_key": "refund",
                "amount": 200,
                "currency": "USD",
                "payment_id": payin["id"],
            },
        )
        lab.await_state(refund["id"])
        lab.settled()
        wallet = lab.request("/v1/wallet")
        other = lab.request("/v1/wallet", auth="other")
        assert (
            wallet["available"] == 500
            and wallet["held"] == 0
            and other["available"] == other["held"] == 0
        )
        audit = lab.reconcile()["result"]
        assert audit["matches"] and audit["audit"]["matches"]
        assert len(lab.request("/v1/ledger")["transactions"]) == 5
        assert lab.request("/v1/payments/" + payin["id"])["id"] == payin["id"]
        provider = lab.request("/admin/status", role="provider", auth="admin")
        assert provider["unique_operations"] == 4
        return {
            "processes": 4,
            "executor_threads": 4,
            "checkout_orders": 2,
            "same_operation_ids_on_retry": True,
            "order_states": [op["status"] for op in batch["orders"]],
            "wallet_available": 500,
            "wallet_held": 0,
            "other_wallet_available": 0,
            "posted_transactions": 5,
            "unique_provider_operations": 4,
            "reconciliation_matches": True,
            "balanced_ledger": True,
            "scope": "localhost simulated PSP only; no real funds or card data",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="benchmarks/features-2026-10-27.json")
    args = parser.parse_args()
    result = scenario()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
