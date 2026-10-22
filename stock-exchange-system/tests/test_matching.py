import copy
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
from lab import configuration
from stock_exchange.common import Error,entry
from stock_exchange.market import Market,candles,report
from stock_exchange.matching import audit,book,reduce_command,validate
from stock_exchange.storage import Engine,Sequencer


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.config=configuration()
        self.state=reduce_command({},{"kind":"genesis","settings":self.config["settings"]})
        self.seq=1
        self.stamp=self.config["simulation_timestamp"]

    def command(self,id,owner="alice",side="BUY",price=100,quantity=10,symbol="AAPL",kind="new"):
        self.seq+=1
        body={"request_id":"request_"+id,"order_id":id}
        if kind=="new":
            body.update(symbol=symbol,side=side,price=price,quantity=quantity)
        return {"seq":self.seq,"timestamp":self.stamp,"owner":owner,"kind":kind,"body":body}

    def apply(self,*args,**kwargs):
        self.state=reduce_command(self.state,self.command(*args,**kwargs))
        self.assertTrue(audit(self.state)["matches"])
        return self.state["results"][str(self.seq)]

    def test_price_time_partial_fill_and_price_improvement(self):
        self.apply("ask1","bob","SELL",100,5)
        self.apply("ask2","carol","SELL",100,7)
        self.apply("take","alice","BUY",110,8)
        self.assertEqual([row["sell_order"] for row in self.state["trades"]],["ask1","ask2"])
        self.assertEqual([row["quantity"] for row in self.state["trades"]],[5,3])
        self.assertEqual(self.state["orders"]["ask2"]["remaining"],4)
        self.assertEqual(self.state["accounts"]["alice"]["cash"],{"available":999200,"held":0})
        self.assertEqual(book(self.state,"AAPL")["asks"],{"100":4})

    def test_better_price_beats_arrival_and_maker_price(self):
        self.apply("high","bob","SELL",105,2)
        self.apply("low","carol","SELL",100,2)
        self.apply("take","alice","BUY",110,3)
        self.assertEqual([row["price"] for row in self.state["trades"]],[100,105])
        rows=candles(self.state["trades"])
        self.assertEqual((rows[0]["open"],rows[0]["close"],rows[0]["high"],rows[0]["low"],rows[0]["volume"]),(100,105,105,100,3))
        self.assertEqual(len(report({**self.state,"seq":self.seq},"alice")["fills"]),2)

    def test_cancel_releases_remaining_and_cancel_fill_race_order(self):
        self.apply("ask","bob","SELL",100,10)
        self.apply("take","alice","BUY",100,4)
        self.apply("ask","bob",kind="cancel")
        self.assertEqual(self.state["accounts"]["bob"]["positions"]["AAPL"],{"available":996,"held":0})
        self.apply("late","carol","BUY",100,6)
        self.assertEqual(len(self.state["trades"]),1)
        self.assertEqual(self.state["accounts"]["carol"]["cash"]["held"],600)
        self.apply("late","carol",kind="cancel")
        self.assertEqual(self.state["accounts"]["carol"]["cash"]["held"],0)

    def test_self_trade_prevention_and_risk_checks(self):
        self.apply("ownask","alice","SELL",100,5)
        result=self.apply("ownbid","alice","BUY",100,5)
        self.assertEqual(result["status"],"CANCELED")
        self.assertEqual(len(self.state["trades"]),0)
        self.assertEqual(self.state["accounts"]["alice"]["cash"]["held"],0)
        self.assertEqual(self.apply("cash","bob","BUY",100000,11)["reason"],"INSUFFICIENT_CASH")
        self.assertEqual(self.apply("short","bob","SELL",100,1001)["reason"],"INSUFFICIENT_SHARES")
        self.assertEqual(self.apply("unknown",symbol="OTHER")["reason"],"UNKNOWN_SYMBOL")

    def test_daily_volume_session_close_and_lazy_day_expiry(self):
        self.state["settings"]["daily_limit"]=20
        self.apply("bid",quantity=15)
        self.apply("bid",kind="cancel")
        self.assertEqual(self.apply("more",quantity=6)["reason"],"DAILY_SUBMITTED_VOLUME_LIMIT")
        self.apply("hold","bob",quantity=10)
        self.stamp=int(datetime(2026,10,21,20,tzinfo=timezone.utc).timestamp())
        self.assertEqual(self.apply("closed","carol")["reason"],"SESSION_CLOSED")
        self.stamp+=18*3600
        self.assertTrue(self.apply("next",quantity=10)["accepted"])
        self.assertEqual(self.state["orders"]["hold"]["reason"],"DAY_EXPIRED")
        self.assertEqual(self.state["accounts"]["bob"]["cash"]["held"],0)

    def test_strict_integer_fields_and_owned_cancel(self):
        for value in (True,1.5,"1.00",0,-1):
            body=self.command("bad")["body"]
            body["price"]=value
            with self.assertRaises(ValueError):
                validate(body,"new")
        self.apply("bid")
        self.assertEqual(self.apply("bid","bob",kind="cancel")["reason"],"OWNED_ORDER_NOT_FOUND")
        self.assertEqual(self.apply("bid")["reason"],"ORDER_ID_EXISTS")

    def test_storage_replay_snapshot_tail_immutability_and_gap(self):
        (ROOT/".runtime"/"tmp").mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/".runtime"/"tmp") as temp:
            root=Path(temp)
            engine=Engine(root/"engine.sqlite",2)
            genesis=entry(1,"0"*64,{"kind":"genesis","settings":self.config["settings"]})
            engine.apply(genesis)
            previous=genesis
            for index in range(3):
                row=entry(index+2,previous["hash"],self.command("bid"+str(index)))
                engine.apply(row)
                engine.apply(row)
                if index==0:
                    engine.snapshot()
                previous=row
            before=engine.raw()
            with engine.transaction() as db:
                db.execute("UPDATE projection SET state='{}'")
            engine=Engine(root/"engine.sqlite",2)
            self.assertEqual(engine.raw(),before)
            self.assertTrue(engine.audit()["matches"])
            with self.assertRaises(Error) as error:
                engine.feed(0)
            self.assertEqual(error.exception.status,410)
            with self.assertRaises(sqlite3.IntegrityError),engine.transaction() as db:
                db.execute("DELETE FROM events")
            path=Path(str(engine.path)+".snapshot.json")
            value=json.loads(path.read_text())
            value["payload"]["seq"]+=1
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                engine.rebuild()

    def test_sequencer_idempotency_and_promotion_catchup_gate(self):
        (ROOT/".runtime"/"tmp").mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/".runtime"/"tmp") as temp:
            store=Sequencer(Path(temp)/"seq.sqlite",self.config["settings"],self.stamp)
            body=self.command("bid")["body"]
            first=store.submit("alice","new",body)
            self.assertEqual(store.submit("alice","new",body)["seq"],first["seq"])
            with self.assertRaises(Error):
                store.submit("alice","new",{**body,"price":101})
            with self.assertRaises(Error):
                store.promote("backup",{"seq":0,"head":"0"*64})
            control=store.control()
            self.assertEqual(store.promote("backup",control)["epoch"],2)
            with self.assertRaises(Error):
                store.clock(self.stamp-1)

    def test_market_duplicate_batches_and_out_of_order_gap(self):
        (ROOT/".runtime"/"tmp").mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/".runtime"/"tmp") as temp:
            engine=Engine(Path(temp)/"engine.sqlite")
            genesis=entry(1,"0"*64,{"kind":"genesis","settings":self.config["settings"]})
            engine.apply(genesis)
            market=Market(Path(temp)/"market.sqlite")
            market.replace(engine.raw())
            first=entry(2,genesis["hash"],self.command("ask","bob","SELL",100,5))
            second=entry(3,first["hash"],self.command("take","alice","BUY",100,5))
            engine.apply(first)
            engine.apply(second)
            batches=engine.feed(1)["batches"]
            with self.assertRaises(Error) as error:
                market.apply(batches[1])
            self.assertEqual(error.exception.status,410)
            for batch in batches:
                market.apply(batch)
                market.apply(batch)
            self.assertEqual(len(market.view()["trades"]),1)
            self.assertEqual(market.view()["books"]["AAPL"],book(engine.raw()["state"],"AAPL"))


if __name__=="__main__":
    unittest.main()
