from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab
from stock_exchange.common import Error


class FeaturesNetworkTests(unittest.TestCase):
    def test_book_api_alias_execution_time_filters_bilateral_sequence_and_restart(self):
        with Lab() as lab:
            lab.order("ask", "SELL", 100, 3, "bob")
            lab.result("ask", "bob")
            body = {
                "request_id": "request_take",
                "order_id": "take",
                "symbol": "AAPL",
                "side": "BUY",
                "price": 110,
                "quantity": 2,
                "orderType": "limit",
            }
            accepted = lab.call("/v1/order", body)
            self.assertEqual(lab.call("/v1/order", body)["seq"], accepted["seq"])
            self.assertEqual(lab.result("take")["status"], "FILLED")
            lab.caught_up()
            lab.market_ready()
            stamp = lab.config["simulation_timestamp"]
            path = (
                "/execution?symbol=AAPL&orderId=take&startTime="
                + str(stamp)
                + "&endTime="
                + str(stamp + 1)
            )
            executions = lab.call(path, role="market")["executions"]
            self.assertEqual(len(executions), 1)
            self.assertEqual(
                (
                    executions[0]["execution_seq"],
                    executions[0]["side"],
                    executions[0]["quantity"],
                ),
                (1, "BUY", 2),
            )
            self.assertEqual(
                lab.call(
                    "/execution?symbol=AAPL&orderId=take", role="market", auth="carol"
                )["executions"],
                [],
            )
            self.assertEqual(
                lab.call("/execution?symbol=AAPL&endTime=" + str(stamp), role="market")[
                    "executions"
                ],
                [],
            )
            seller = lab.call(
                "/execution?symbol=AAPL&orderId=ask", role="market", auth="bob"
            )["executions"][0]
            self.assertEqual(seller["execution_seq"], executions[0]["execution_seq"])
            levels = lab.call(
                "/marketdata/orderBook/L2?symbol=AAPL&depth=1", role="market"
            )
            self.assertEqual(levels["asks"], [{"price": 100, "quantity": 1}])
            candles = lab.call(
                "/marketdata/candles?symbol=AAPL&resolution=60&startTime="
                + str(stamp)
                + "&endTime="
                + str(stamp + 60),
                role="market",
            )["candles"]
            self.assertEqual(candles[0]["volume"], 2)
            for path in (
                "/execution?symbol=AAPL&startTime=2&endTime=1",
                "/execution?symbol=AAPL&orderId=a&orderId=b",
            ):
                with self.assertRaises(Error) as error:
                    lab.call(path, role="market")
                self.assertEqual(error.exception.status, 400)
            lab.stop("market")
            lab.start("market")
            lab.market_ready()
            self.assertEqual(
                lab.call("/execution?symbol=AAPL&orderId=take", role="market")[
                    "executions"
                ],
                executions,
            )
            self.assertTrue(
                lab.call("/admin/audit", role="primary", auth="admin")["matches"]
            )
