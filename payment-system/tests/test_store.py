from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from payment_system.common import Error
from payment_system.provider import MockPSP
from payment_system.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        (ROOT/".runtime"/"tmp").mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/".runtime"/"tmp")
        self.now=1000
        self.store=Store(Path(self.temp.name)/"store.sqlite",("seller","other"),lambda:self.now)

    def tearDown(self):
        self.temp.cleanup()

    def create(self,key="pay",amount=10000,kind="payin",**extra):
        fields={"order_id":"order_"+key} if kind=="payin" else {"destination":"test-bank-ok"} if kind=="payout" else {}
        return self.store.create("seller",kind,{"request_key":key,"amount":amount,"currency":"USD",**fields,**extra})

    def receipt(self,op,status="SUCCEEDED",revision=2):
        return {**{k:op[k] for k in ("id","merchant","kind","parent","amount","currency")},"provider_id":"mock_"+op["id"],"status":status,"revision":revision,"checkout":None}

    def fund(self):
        op=self.create()
        self.store.apply(self.receipt(op))
        return op

    def test_concurrent_idempotency_and_order_identity(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids=list(pool.map(lambda _:self.create()["id"],range(20)))
        self.assertEqual(len(set(ids)),1)
        with self.assertRaises(Error):
            self.create(amount=999)
        with self.assertRaises(Error):
            self.create(key="other",order_id="order_pay")
        self.assertEqual(self.store.status()["operations"],1)

    def test_balanced_immutable_ledger_and_atomic_rollback(self):
        self.fund()
        self.assertTrue(self.store.status()["audit"]["matches"])
        for sql in ("UPDATE lines SET debit=1", "DELETE FROM ledger", "UPDATE ledger SET posted=0", "DELETE FROM lines"):
            with self.assertRaises(sqlite3.IntegrityError),self.store.transaction(True) as db:
                db.execute(sql)
        with self.assertRaises(sqlite3.IntegrityError),self.store.transaction(True) as db:
            op=self.store.operation(next(iter(db.execute("SELECT id FROM operations")))[0],db=db)
            db.execute("INSERT INTO ledger VALUES('bad',?,'settle',NULL,0,0)",(op["id"],))
            db.execute("INSERT INTO lines VALUES('bad',1,'x',1,0)")
            db.execute("INSERT INTO lines VALUES('bad',2,'y',0,2)")
            db.execute("UPDATE ledger SET posted=1 WHERE id='bad'")
        before=self.store.status()
        def crash():
            raise RuntimeError("crash before commit")
        with self.assertRaises(RuntimeError):
            self.store.create("seller","payout",{"request_key":"rollback","amount":500,"currency":"USD","destination":"test-bank-ok"},crash)
        self.assertEqual(self.store.status(),before)
        self.assertEqual(self.store.wallet("seller")["available"],10000)

    def test_reservations_concurrent_overdraw_refund_caps_and_release(self):
        pay=self.fund()
        def reserve(index):
            try:
                return self.create(str(index),6000,"payout")
            except Error:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            ops=[r for r in pool.map(reserve,range(2)) if r]
        self.assertEqual(len(ops),1)
        self.assertEqual(self.store.wallet("seller")["held"],6000)
        self.store.apply(self.receipt(ops[0],"FAILED"))
        refund=self.create("refund",7000,"refund",payment_id=pay["id"])
        with self.assertRaises(Error):
            self.create("refund2",4000,"refund",payment_id=pay["id"])
        self.store.apply(self.receipt(refund))
        self.assertEqual(self.store.wallet("seller")["available"],3000)
        self.assertEqual(self.store.wallet("seller")["held"],0)
        self.assertTrue(self.store.status()["audit"]["matches"])

    def test_callbacks_dedup_reordering_conflicts_and_strict_amounts(self):
        op=self.create(amount=1)
        receipt=self.receipt(op)
        self.store.apply(receipt,"event1")
        self.assertTrue(self.store.apply(receipt,"event1")["duplicate"])
        self.assertEqual(self.store.apply(self.receipt(op,"PENDING",1))["ignored"],"older revision")
        self.assertIn("review",self.store.apply(self.receipt(op,"FAILED",3)))
        with self.assertRaises(Error):
            self.store.apply({**receipt,"amount":2},"event1")
        self.assertIn("review",self.store.apply({**receipt,"merchant":"other"},"mismatch"))
        with self.assertRaises(ValueError):
            self.store.apply({**receipt,"amount":True})
        self.assertEqual(self.store.wallet("seller")["available"],1)
        self.assertEqual(len(self.store.ledger("seller")["transactions"]),1)
        with self.assertRaises(Error):
            self.store.operation(op["id"],"other")

    def test_expired_lease_fencing_and_unknown_recovery(self):
        op=self.create()
        first=self.store.claim()
        self.now+=3
        newer=self.store.claim()
        self.assertFalse(self.store.finish_job(first,error="stale"))
        self.assertTrue(self.store.finish_job(newer,error="lost"))
        self.assertEqual(self.store.operation(op["id"])["status"],"UNKNOWN")
        pending=self.receipt(op,"PENDING",1)
        self.store.apply(pending)
        self.now+=10
        job=self.store.claim()
        self.store.finish_job(job,error="another lost query")
        self.assertTrue(self.store.apply(pending)["applied"])
        self.assertEqual(self.store.operation(op["id"])["status"],"PENDING")

    def test_dead_letter_retains_hold_and_retry_reuses_identity(self):
        self.fund()
        # Deliver the funding notification so the next claims select the payout.
        notice=self.store.claim()
        self.store.finish_job(notice)
        op=self.create("out",3000,"payout")
        for _ in range(5):
            job=self.store.claim()
            self.store.finish_job(job,error="provider unavailable")
            self.now+=100
        self.assertEqual(self.store.status()["dead_jobs"],1)
        self.assertEqual(self.store.wallet("seller")["held"],3000)
        self.assertEqual(self.store.operation(op["id"])["status"],"UNKNOWN")
        self.store.retry(op["id"])
        self.assertEqual(self.store.claim()["payload"]["operation"],op["id"])
        self.store.apply(self.receipt(op))
        self.assertEqual(self.store.wallet("seller")["available"],7000)
        self.assertEqual(self.store.wallet("seller")["held"],0)

    def test_mock_provider_commit_before_lost_reply_and_checkout_idempotency(self):
        config={"provider_url":"http://127.0.0.1:9871"}
        provider=MockPSP(Path(self.temp.name)/"provider.sqlite",config,lambda:self.now)
        op=self.create()
        intent={k:op[k] for k in ("id","merchant","kind","parent","amount","currency","destination")}
        provider.control({"lose_response":True,"webhooks_down":False,"query_down":False,"statement_delta":0})
        with self.assertRaises(Error):
            provider.create(intent)
        pending=provider.get(op["id"])
        self.assertEqual(pending["status"],"PENDING")
        provider.control({"lose_response":False,"webhooks_down":False,"query_down":False,"statement_delta":0})
        self.assertEqual(provider.create(intent),pending)
        token=pending["checkout"].rsplit("/",1)[1]
        paid=provider.checkout(token,"test-success")
        self.assertEqual(provider.checkout(token,"test-decline"),paid)
        self.assertEqual(provider.status()["unique_operations"],1)

    def test_reconciliation_detects_statement_and_wallet_drift(self):
        op=self.fund()
        receipt=self.receipt(op)
        def reconcile(rows):
            run=self.store.schedule_reconcile()["run"]
            return self.store.reconcile(run,{"operations":rows,"snapshot":"test"})
        self.assertTrue(reconcile([receipt])["matches"])
        self.assertFalse(reconcile([{**receipt,"amount":9999}])["matches"])
        with self.store.transaction(True) as db:
            db.execute("UPDATE wallets SET available=available+1 WHERE merchant='seller'")
        result=reconcile([receipt])
        self.assertFalse(result["audit"]["matches"])
        self.assertEqual(self.store.wallet("seller")["available"],10001)
        self.assertEqual(len(self.store.ledger("seller")["transactions"]),1)

    def test_dead_outbox_requeue_preserves_event_identity(self):
        op=self.fund()
        for _ in range(5):
            job=self.store.claim()
            self.assertEqual(job["kind"],"notify")
            self.store.finish_job(job,error="consumer down")
            self.now+=100
        self.assertEqual(self.store.status()["dead_jobs"],1)
        self.store.retry_job("notify:"+op["id"])
        recovered=self.store.claim()
        self.assertEqual(recovered["payload"]["event_id"],"terminal_"+op["id"])
        self.store.finish_job(recovered)
        self.assertEqual(self.store.status()["dead_jobs"],0)
        self.assertEqual(self.store.wallet("seller")["available"],10000)


if __name__=="__main__":
    unittest.main()
