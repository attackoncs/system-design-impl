from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from digital_wallet.common import Error,entry,shard_for
from digital_wallet.journal import Replica
from digital_wallet.shard import Shard,intent


class WalletTests(unittest.TestCase):
    def setUp(self):
        (ROOT/".runtime"/"tmp").mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/".runtime"/"tmp")
        self.root=Path(self.temp.name)
        names=["account_"+str(i) for i in range(30)]
        self.source,self.peer=[key for key in names if shard_for(key,2)==0][:2]
        self.destination=next(key for key in names if shard_for(key,2)==1)
        self.accounts={self.source:10000,self.peer:10000,self.destination:10000}
        self.left=Shard(self.root/"left.sqlite",0,2,self.accounts)
        self.right=Shard(self.root/"right.sqlite",1,2,self.accounts)

    def tearDown(self):
        self.temp.cleanup()

    def body(self,id="transfer",amount=100,local=False):
        return {"transaction_id":id,"from_account":self.source,"to_account":self.peer if local else self.destination,"amount":amount,"currency":"USD"}

    def action(self,store,body,kind,role):
        return store.action(body,kind,role,1,"coordinator")

    def test_atomic_local_concurrent_idempotency_and_no_overdraft(self):
        body=self.body(amount=6000,local=True)
        with ThreadPoolExecutor(max_workers=6) as pool:
            result=list(pool.map(lambda _:self.action(self.left,body,"local","local"),range(12)))
        self.assertTrue(all(row["status"]=="CONFIRMED" for row in result))
        self.assertEqual(self.left.view()["accounts"][self.source]["available"],4000)
        other=self.body("another",6000,True)
        self.assertEqual(self.action(self.left,other,"local","local")["status"],"REJECTED")
        self.assertEqual(self.left.audit()["events"],3)
        with self.assertRaises(Error):
            self.action(self.left,{**body,"amount":1},"local","local")

    def test_cross_prepare_confirm_preserves_pending_and_replays_once(self):
        body=self.body(amount=3000)
        self.action(self.left,body,"prepare","source")
        self.action(self.right,body,"prepare","destination")
        self.assertEqual(self.left.view()["accounts"][self.source],{"available":7000,"held":3000})
        self.assertEqual(self.right.view()["accounts"][self.destination]["available"],10000)
        self.action(self.left,body,"confirm","source")
        for _ in range(2):
            self.action(self.right,body,"confirm","destination")
        before=self.right.view()
        self.right.rebuild()
        self.assertEqual(self.right.view(),before)
        self.assertEqual(self.right.view()["accounts"][self.destination]["available"],13000)
        self.assertTrue(self.left.audit()["matches"])
        with self.assertRaises(Error):
            self.action(self.left,body,"cancel","source")

    def test_cancel_tombstones_prevent_late_prepare_and_release_holds(self):
        body=self.body(amount=3000)
        self.action(self.left,body,"prepare","source")
        self.action(self.left,body,"cancel","source")
        self.action(self.right,body,"cancel","destination")
        for shard,role in ((self.left,"source"),(self.right,"destination")):
            self.assertEqual(self.action(shard,body,"prepare",role)["status"],"CANCELED")
            with self.assertRaises(Error):
                self.action(shard,body,"confirm",role)
        self.assertEqual(self.left.view()["accounts"][self.source],{"available":10000,"held":0})

    def test_snapshot_tail_history_projection_rebuild_and_corruption(self):
        self.action(self.left,self.body(local=True),"local","local")
        self.left.snapshot()
        self.action(self.left,self.body("tail",200,True),"local","local")
        self.assertEqual(self.left.view(1)["accounts"][self.source]["available"],10000)
        self.assertEqual(self.left.view(2)["accounts"][self.source]["available"],9900)
        with self.left.transaction() as db:
            db.execute("UPDATE projection SET state='{}'")
        self.assertFalse(self.left.audit()["matches"])
        restored=Shard(self.root/"left.sqlite",0,2,self.accounts)
        self.assertEqual(restored.view()["accounts"][self.source]["available"],9700)
        snapshot=Path(str(restored.path)+".snapshot.json")
        value=json.loads(snapshot.read_text())
        value["payload"]["state"]["accounts"][self.source]["available"]+=1
        snapshot.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            restored.rebuild()

    def test_log_immutability_checksum_and_fixed_epoch_fencing(self):
        with self.assertRaises(sqlite3.IntegrityError),self.left.transaction() as db:
            db.execute("DELETE FROM events")
        self.left.fence(2,"replacement")
        with self.assertRaises(Error):
            self.action(self.left,self.body(),"prepare","source")
        self.left.action(self.body(),"prepare","source",2,"replacement")
        with self.assertRaises(Error):
            self.left.fence(1,"coordinator")
        # Simulate on-disk corruption outside the application boundary.
        with self.right.transaction() as db:
            db.execute("DROP TRIGGER events_no_update")
            row=json.loads(db.execute("SELECT body FROM events").fetchone()[0])
            row["event"]["accounts"][self.destination]+=1
            db.execute("UPDATE events SET body=?",(json.dumps(row),))
        with self.assertRaises(ValueError):
            self.right.rebuild()

    def test_replica_prefix_dedup_checksum_and_restart_fence(self):
        replica=Replica(self.root/"replica.sqlite")
        first=entry(1,"0"*64,{"kind":"test"})
        replica.append(first,1,"coordinator")
        replica.append(first,1,"coordinator")
        with self.assertRaises(Error):
            replica.append(entry(1,"0"*64,{"kind":"conflict"}),1,"coordinator")
        with self.assertRaises(Error):
            replica.append(entry(3,first["hash"],{}),1,"coordinator")
        replica.fence(2,"replacement")
        replica=Replica(self.root/"replica.sqlite")
        with self.assertRaises(Error):
            replica.status(1,"coordinator")
        self.assertEqual(replica.status(2,"replacement")["count"],1)

    def test_strict_money_and_routing(self):
        for value in (True,1.5,"1.00",0,-1):
            with self.assertRaises(ValueError):
                intent(self.body(amount=value))
        with self.assertRaises(Error):
            self.action(self.right,self.body(),"prepare","source")
        with self.assertRaises(ValueError):
            intent({**self.body(),"currency":"EUR"})

    def test_competing_debits_and_precommit_rollback(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda index:self.action(self.left,self.body("compete_"+str(index),6000,True),"local","local"),range(2)))
        self.assertEqual(sorted(row["status"] for row in results),["CONFIRMED","REJECTED"])
        before=self.left.view()
        with self.assertRaises(RuntimeError),self.left.transaction() as db:
            self.left.append(db,{"kind":"local","role":"local","intent":self.body("rollback",100,True)},self.left.replay(self.left.rows(db)))
            raise RuntimeError("crash before event and projection commit")
        self.assertEqual(self.left.view(),before)


if __name__=="__main__":
    unittest.main()
