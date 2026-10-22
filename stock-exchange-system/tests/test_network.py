from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
from lab import Lab,wait
from stock_exchange.common import Error
from stock_exchange.matching import book


class NetworkTests(unittest.TestCase):
    def test_real_sequence_price_time_and_concurrent_request_dedup(self):
        with Lab() as lab:
            with ThreadPoolExecutor(max_workers=2) as pool:
                asks=list(pool.map(lambda owner:lab.order(owner+"_ask","SELL",100,5,owner),["bob","carol"]))
            lab.caught_up()
            first="bob_ask" if asks[0]["seq"]<asks[1]["seq"] else "carol_ask"
            lab.order("take","BUY",110,7)
            self.assertTrue(lab.result("take")["accepted"])
            with ThreadPoolExecutor(max_workers=6) as pool:
                duplicates=list(pool.map(lambda _:lab.order("duplicate","BUY",90,2),range(10)))
            self.assertEqual(len({row["seq"] for row in duplicates}),1)
            lab.result("duplicate")
            lab.caught_up()
            state=lab.raw()["state"]
            self.assertEqual(state["trades"][0]["sell_order"],first)
            self.assertEqual([row["quantity"] for row in state["trades"]],[5,2])
            self.assertEqual(state["accounts"]["alice"]["cash"],{"available":999120,"held":180})
            self.assertEqual(lab.raw()["state_hash"],lab.raw("backup")["state_hash"])
            with self.assertRaises(Error) as error:
                lab.order("duplicate","BUY",91,2)
            self.assertEqual(error.exception.status,409)
            with self.assertRaises(Error) as error:
                lab.call("/v1/orders/take",auth="bob")
            self.assertEqual(error.exception.status,404)

    def test_cancel_and_fill_race_has_one_sequenced_outcome(self):
        with Lab() as lab:
            lab.order("bid","BUY",100,5)
            lab.result("bid")
            with ThreadPoolExecutor(max_workers=2) as pool:
                cancel=pool.submit(lab.call,"/v1/cancels",{"request_id":"cancel_bid","order_id":"bid"})
                sell=pool.submit(lab.order,"sell","SELL",100,5,"bob")
                cancel_result,sell_result=cancel.result(),sell.result()
            lab.result("sell","bob")
            wait(lambda:"accepted" in lab.call("/v1/requests/cancel_bid"))
            lab.caught_up()
            state=lab.raw()["state"]
            self.assertEqual(len(state["trades"]),int(sell_result["seq"]<cancel_result["seq"]))
            self.assertIn(state["orders"]["bid"]["status"],("FILLED","CANCELED"))
            self.assertEqual(state["accounts"]["alice"]["cash"]["held"],0)
            self.assertTrue(lab.call("/admin/audit",role="primary",auth="admin")["matches"])
            self.assertEqual(lab.raw()["state_hash"],lab.raw("backup")["state_hash"])

    def test_manual_matching_failover_fences_old_publisher_and_replays_snapshots(self):
        with Lab() as lab:
            lab.stop("backup")
            lab.order("bid","BUY",100,10)
            lab.result("bid")
            with self.assertRaises(Error):
                lab.call("/admin/promote",{"node":"backup"},auth="admin")
            lab.start("backup")
            lab.caught_up()
            lab.call("/admin/snapshot",{},role="backup",auth="admin")
            lab.stop("primary")
            promoted=lab.call("/admin/promote",{"node":"backup"},auth="admin")
            self.assertEqual(promoted["epoch"],2)
            lab.order("sell","SELL",100,4,"bob")
            lab.result("sell","bob")
            lab.start("primary")
            lab.caught_up()
            with self.assertRaises(Error) as error:
                lab.call("/internal/serve?epoch=2",role="primary",auth="internal")
            self.assertEqual(error.exception.status,409)
            with self.assertRaises(Error) as error:
                lab.call("/internal/raw",role="backup",auth="alice")
            self.assertEqual(error.exception.status,401)
            lab.stop("backup")
            lab.start("backup")
            lab.caught_up()
            self.assertEqual(lab.raw()["state_hash"],lab.raw("backup")["state_hash"])
            self.assertEqual(len(lab.raw("backup")["state"]["trades"]),1)
            audit=lab.call("/admin/audit",role="backup",auth="admin")
            self.assertTrue(audit["matches"] and audit["projection_matches"])
            lab.stop("gateway")
            lab.start("gateway")
            self.assertEqual(lab.call("/admin/status",auth="admin")["epoch"],2)
            lab.market_ready()

    def test_market_gap_resync_candles_and_private_execution_reports(self):
        with Lab() as lab:
            lab.market_ready()
            before=lab.call("/admin/status",role="market",auth="admin")["resyncs"]
            lab.stop("market")
            for index in range(12):
                lab.order("bid_"+str(index),"BUY",100,1)
            lab.order("sell","SELL",100,5,"bob")
            lab.result("sell","bob")
            lab.caught_up()
            with self.assertRaises(Error) as error:
                lab.call("/internal/feed?after=1&epoch=1",role="primary",auth="internal")
            self.assertEqual(error.exception.status,410)
            lab.start("market")
            lab.market_ready()
            status=lab.call("/admin/status",role="market",auth="admin")
            self.assertGreater(status["resyncs"],before)
            snapshot=lab.call("/market/snapshot",role="market")
            state=lab.raw()["state"]
            self.assertEqual(snapshot["books"]["AAPL"],book(state,"AAPL"))
            level=lab.call("/market/book?symbol=AAPL&depth=1",role="market")
            self.assertEqual(level["bids"],[{"price":100,"quantity":7}])
            candle=lab.call("/market/candles?symbol=AAPL&resolution=60",role="market")["candles"][0]
            self.assertEqual((candle["open"],candle["close"],candle["volume"]),(100,100,5))
            alice=lab.call("/v1/report",role="market")
            bob=lab.call("/v1/report",role="market",auth="bob")
            self.assertEqual(len(alice["fills"]),5)
            self.assertEqual(len(bob["fills"]),5)
            self.assertTrue(all(row["side"]=="BUY" for row in alice["fills"]))
            self.assertTrue(all(row["owner"]=="bob" for row in bob["orders"]))
            before_sequence=snapshot["seq"]
            lab.order("new_sell","SELL",100,2,"carol")
            lab.result("new_sell","carol")
            lab.market_ready()
            deltas=lab.call("/market/deltas?after="+str(before_sequence),role="market")["deltas"]
            self.assertEqual(len(deltas),1)
            self.assertEqual(sum(row["quantity"] for row in deltas[0]["trades"]),2)
            self.assertTrue(all("buyer" not in row and "seller" not in row for row in deltas[0]["trades"]))
            with self.assertRaises(Error) as error:
                lab.call("/market/deltas?after=1",role="market")
            self.assertEqual(error.exception.status,410)


if __name__=="__main__":
    unittest.main()
