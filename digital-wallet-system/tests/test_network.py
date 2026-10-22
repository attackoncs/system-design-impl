from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
from lab import Lab,wait
from digital_wallet.common import Error


class NetworkTests(unittest.TestCase):
    def test_concurrent_idempotency_local_cross_insufficient_and_ownership(self):
        with Lab() as lab:
            with ThreadPoolExecutor(max_workers=6) as pool:
                result=list(pool.map(lambda _:lab.transfer("same",1000),range(12)))
            self.assertTrue(all(row["intent"]["transaction_id"]=="same" for row in result))
            lab.done("same")
            self.assertEqual(lab.balance()["available"],9000)
            self.assertEqual(lab.balance(lab.destination)["available"],11000)
            with self.assertRaises(Error) as error:
                lab.transfer("same",999)
            self.assertEqual(error.exception.status,409)
            with self.assertRaises(Error) as error:
                lab.call("/v1/wallet/balance_transfer",lab.body("stolen"),auth="bob")
            self.assertEqual(error.exception.status,403)
            with self.assertRaises(Error) as error:
                lab.call("/v1/accounts/"+lab.destination)
            self.assertEqual(error.exception.status,404)
            lab.transfer("local",500,True)
            lab.done("local")
            lab.transfer("too_much",10000)
            lab.done("too_much","CANCELED")
            self.assertEqual(lab.balance()["available"],8500)
            self.assertEqual(lab.balance()["held"],0)
            self.assertTrue(lab.audit()["matches"])

    def test_partial_commit_crash_recovery_transit_snapshot_and_history(self):
        with Lab() as lab:
            lab.stop("worker")
            lab.transfer(amount=500)
            for _ in range(3):
                lab.step()  # source prepare, destination prepare, durable COMMIT
            lab.call("/admin/snapshot",{},role="shard0",auth="admin")
            lab.stop("shard1")
            # Both participants are read before a step; unavailable destination blocks.
            failed=lab.step()
            self.assertIn("error",failed)
            self.assertEqual(lab.balance()["held"],500)
            lab.start("shard1")
            # Retry due time is durable; clear it without changing the transfer identity.
            lab.call("/admin/retry/transfer",{},auth="admin")
            lab.step()  # source confirmed, destination not yet credited
            audit=lab.audit()
            self.assertTrue(audit["matches"])
            self.assertEqual(audit["in_transit"],500)
            self.assertEqual(audit["wallet_total"],29500)
            self.assertEqual(lab.balance(lab.destination)["available"],10000)
            with self.assertRaises(Error) as error:
                lab.call("/admin/cancel/transfer",{},auth="admin")
            self.assertEqual(error.exception.status,409)
            for role in ("coordinator","shard0","shard1"):
                lab.stop(role)
            for role in ("shard0","shard1","coordinator"):
                lab.start(role)
            self.assertEqual(lab.audit()["in_transit"],500)
            lab.start("worker")
            lab.done()
            self.assertEqual(lab.balance()["available"],9500)
            self.assertEqual(lab.balance(lab.destination)["available"],10500)
            self.assertEqual(lab.balance(at=1)["available"],10000)
            self.assertEqual(lab.balance(at=2)["held"],500)
            self.assertEqual(lab.audit()["in_transit"],0)

    def test_prepare_rejection_cancellation_and_late_try_tombstone(self):
        with Lab() as lab:
            lab.stop("worker")
            lab.transfer(amount=700)
            lab.step()
            self.assertEqual(lab.balance()["held"],700)
            lab.control(1,reject_prepare=True)
            lab.step()  # durable CANCEL decision after definitive rejection
            lab.control(1)
            for _ in range(3):
                lab.step()
            lab.done(status="CANCELED")
            late=lab.call("/internal/action",{"epoch":1,"leader":"coordinator","kind":"prepare","role":"destination","intent":lab.body(amount=700)},role="shard1",auth="internal")
            self.assertEqual(late["status"],"CANCELED")
            self.assertEqual(lab.balance()["available"],10000)
            self.assertEqual(lab.balance(lab.destination)["available"],10000)
            lab.transfer("early_cancel",100)
            lab.call("/admin/cancel/early_cancel",{},auth="admin")
            for _ in range(3):
                lab.step()
            lab.done("early_cancel","CANCELED")
            self.assertTrue(lab.audit()["matches"])

    def test_real_quorum_loss_pending_intent_restart_and_replica_catchup(self):
        with Lab() as lab:
            lab.stop("worker")
            lab.stop("replica2")
            lab.transfer("two_of_three",100,True)
            lab.step()
            lab.done("two_of_three")
            lab.stop("replica1")
            # One replica can receive an intent but cannot commit it.
            with self.assertRaises(Error) as error:
                lab.transfer("ambiguous",200)
            self.assertEqual(error.exception.status,503)
            self.assertEqual(lab.balance()["available"],9900)
            lab.start("replica1")
            lab.start("replica2")
            lab.call("/admin/sync",{},auth="admin")
            for role in ("replica1","replica2"):
                lab.call("/admin/control",{"reject_append":True},role=role,auth="admin")
            # Status probes succeed, then only replica0 accepts the newly staged intent.
            with self.assertRaises(Error) as error:
                lab.transfer("ambiguous",200)
            self.assertEqual(error.exception.status,503)
            self.assertEqual(lab.call("/admin/status",auth="admin")["transfers"],1)
            lab.stop("coordinator")
            for role in ("replica1","replica2"):
                lab.call("/admin/control",{"reject_append":False},role=role,auth="admin")
            lab.stop("replica2")
            lab.start("coordinator")
            self.assertEqual(lab.transfer("ambiguous",200)["status"],"PENDING")
            lab.start("worker")
            lab.done("ambiguous")
            lab.start("replica2")
            lab.call("/admin/sync",{},auth="admin")
            statuses=[lab.call("/internal/status",{"epoch":1,"leader":"coordinator"},role="replica"+str(i),auth="internal") for i in range(3)]
            self.assertEqual(statuses[0],statuses[1])
            self.assertEqual(statuses[1],statuses[2])
            self.assertEqual(lab.balance()["available"],9700)
            self.assertTrue(lab.audit()["matches"])

    def test_persisted_epoch_fences_old_writer_and_manual_same_disk_recovery(self):
        with Lab() as lab:
            lab.stop("worker")
            for role in ("replica0","replica1","replica2","shard0","shard1"):
                lab.call("/admin/fence",{"epoch":2,"leader":"replacement"},role=role,auth="admin")
            with self.assertRaises(Error) as error:
                lab.transfer()
            self.assertEqual(error.exception.status,503)
            with self.assertRaises(Error) as error:
                lab.call("/internal/action",{"epoch":1,"leader":"coordinator","kind":"prepare","role":"source","intent":lab.body()},role="shard0",auth="internal")
            self.assertEqual(error.exception.status,409)
            self.assertEqual(lab.balance()["available"],10000)
            lab.stop("coordinator")
            lab.stop("shard0")
            lab.config.update(epoch=2,leader="replacement")
            lab.write_config()
            lab.start("shard0")
            lab.start("coordinator")
            lab.start("worker")
            lab.transfer()
            lab.done()
            self.assertTrue(lab.audit()["matches"])

    def test_lost_prepare_reply_dead_pending_and_manual_retry(self):
        with Lab() as lab:
            lab.stop("worker")
            lab.control(0,lose_reply=True)
            lab.transfer(amount=300)
            self.assertIn("error",lab.step())
            self.assertEqual(lab.balance()["held"],300)
            lab.control(0)
            lab.stop("shard1")
            lab.start("worker")
            wait(lambda:any(row["dead"] for row in lab.call("/admin/status",auth="admin")["retries"]),timeout=25)
            self.assertEqual(lab.balance()["held"],300)
            self.assertEqual(lab.call("/v1/transfers/transfer")["status"],"PENDING")
            lab.start("shard1")
            lab.call("/admin/retry/transfer",{},auth="admin")
            lab.done()
            self.assertEqual(lab.balance()["available"],9700)
            self.assertEqual(lab.balance(lab.destination)["available"],10300)
            self.assertTrue(lab.audit()["matches"])


if __name__=="__main__":
    unittest.main()
