"""Four-process synthetic price-time matching, failover and reporting."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Lab

with Lab() as lab:
    lab.order("ask1", "SELL", 100, 5, "bob")
    lab.result("ask1", "bob")
    lab.order("ask2", "SELL", 100, 7, "carol")
    lab.result("ask2", "carol")
    lab.order("take", "BUY", 110, 8)
    lab.result("take")
    lab.caught_up()
    lab.stop("primary")
    promoted = lab.call("/admin/promote", {"node": "backup"}, auth="admin")
    lab.call(
        "/v1/cancels", {"request_id": "cancel_rest", "order_id": "ask2"}, auth="carol"
    )
    from lab import wait

    wait(lambda: "accepted" in lab.call("/v1/requests/cancel_rest", auth="carol"))
    lab.market_ready()
    audit = lab.call("/admin/audit", role="backup", auth="admin")
    assert audit["matches"] and audit["projection_matches"]
    stamp = lab.config["simulation_timestamp"]
    executions = lab.call(
        "/execution?symbol=AAPL&orderId=take&startTime="
        + str(stamp)
        + "&endTime="
        + str(stamp + 1),
        role="market",
    )["executions"]
    assert [row["execution_seq"] for row in executions] == [1, 2]
    assert sum(row["quantity"] for row in executions) == 8
    print(
        json.dumps(
            {
                "processes": 4,
                "synthetic_only": True,
                "active": promoted["active"],
                "epoch": promoted["epoch"],
                "buyer": lab.call("/v1/account"),
                "audit": audit,
                "book": lab.call("/market/book?symbol=AAPL", role="market"),
                "buyer_report": lab.call("/v1/report", role="market"),
                "filtered_executions": executions,
                "candles": lab.call("/market/candles?symbol=AAPL", role="market"),
            },
            indent=2,
        )
    )
