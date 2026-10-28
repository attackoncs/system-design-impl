"""Run pay-in, payout, refund and asynchronous reconciliation over real HTTP."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Lab

with Lab() as lab:
    pay = lab.pay(10000)
    payout = lab.request(
        "/v1/payouts",
        {
            "request_key": "out",
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
            "payment_id": pay["id"],
        },
    )
    lab.await_state(refund["id"])
    lab.settled()
    print(
        json.dumps(
            {
                "processes": 4,
                "simulated_only": True,
                "wallet": lab.request("/v1/wallet"),
                "ledger_transactions": len(lab.request("/v1/ledger")["transactions"]),
                "reconciliation": lab.reconcile(),
                "provider": lab.request("/admin/status", role="provider", auth="admin"),
                "consumer": lab.request("/admin/status", role="receiver", auth="admin"),
            },
            indent=2,
        )
    )
