import base64
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
import hashlib
from pathlib import Path
import smtplib
import sys
import unittest
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
from lab import Lab,wait_for


class NetworkTests(unittest.TestCase):
    def test_real_smtp_attachment_search_read_conflict_and_delete(self):
        with Lab() as lab:
            upload=lab.api("/v1/uploads",{"filename":"data.bin","data":base64.b64encode(b"payload\x00binary").decode()})
            first=lab.send(attachments=[upload["upload_id"]])
            self.assertEqual(lab.send(attachments=[upload["upload_id"]])["id"],first["id"])
            wait_for(lab.settled)
            inbox=lab.api("/v1/messages?q=searchable&field=body",user=lab.recipient)["messages"]
            self.assertEqual(len(inbox),1)
            mail=inbox[0]
            downloaded=lab.api(f"/v1/messages/{mail['id']}/attachments/0",user=lab.recipient)
            self.assertEqual(base64.b64decode(downloaded["data"]),b"payload\x00binary")
            self.assertEqual(lab.api("/v1/messages?q=Network&field=subject",user=lab.recipient)["messages"][0]["id"],mail["id"])
            self.assertEqual(len(lab.api("/v1/messages?q=alice&field=sender",user=lab.recipient)["messages"]),1)
            def update(read):
                try:
                    lab.api("/v1/messages/"+mail["id"],{"version":mail["version"],"read":read},lab.recipient,"PUT")
                    return 200
                except HTTPError as exc:
                    return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(update,[True,False])),[200,409])
            with self.assertRaises(HTTPError) as unauthorized:
                lab.api(f"/v1/messages/{mail['id']}/attachments/0",user="outsider")
            self.assertEqual(unauthorized.exception.code,404)
            current=lab.api("/v1/messages/"+mail["id"],user=lab.recipient)
            lab.api(f"/v1/messages/{mail['id']}?version={current['version']}",user=lab.recipient,method="DELETE")
            wait_for(lab.caught_up)
            self.assertEqual(lab.api("/v1/messages?q=searchable&replica=true",user=lab.recipient)["messages"],[])
            self.assertEqual(lab.call("smtp","/admin/status",user="admin")["unique_deliveries"],1)

    def test_lost_smtp_ack_scanner_retry_restart_and_read_failover(self):
        with Lab() as lab:
            lab.call("smtp","/admin/control",{"lose_ack":True,"scanner_down":True},"admin")
            sent=lab.send("recover")
            wait_for(lambda:lab.call("smtp","/admin/status",user="admin")["unique_deliveries"]==1)
            self.assertEqual(lab.api("/v1/messages",user=lab.recipient)["messages"],[])
            lab.stop("worker-0")
            lab.stop("smtp")
            lab.spawn("smtp")
            lab.call("smtp","/admin/control",{"lose_ack":False,"scanner_down":False},"admin")
            lab.spawn("worker-0")
            wait_for(lab.settled)
            self.assertEqual(len(lab.api("/v1/messages",user=lab.recipient)["messages"]),1)
            self.assertEqual(lab.call("smtp","/admin/status",user="admin")["unique_deliveries"],1)
            wait_for(lab.caught_up)
            lab.stop("primary-1")
            fallback=lab.api("/v1/messages",user=lab.recipient)
            self.assertEqual(fallback["source"],"replica")
            with self.assertRaises(HTTPError) as failure:
                lab.api("/v1/messages/"+fallback["messages"][0]["id"],{"version":1,"read":True},lab.recipient,"PUT")
            self.assertEqual(failure.exception.code,503)
            lab.spawn("primary-1")
            wait_for(lab.caught_up)
            lab.stop("replica-1")
            lab.spawn("replica-1")
            wait_for(lab.caught_up)
            self.assertEqual(len(lab.api("/v1/messages?replica=true",user=lab.recipient)["messages"]),1)

    def test_quarantine_integrity_and_external_recipient_rejection(self):
        with Lab() as lab:
            upload=lab.api("/v1/uploads",{"filename":"marker.bin","data":base64.b64encode(b"LOCAL_TEST_VIRUS_MARKER").decode()})
            lab.send("quarantine",attachments=[upload["upload_id"]])
            wait_for(lab.settled)
            mail=lab.api("/v1/messages?folder=quarantine",user=lab.recipient)["messages"][0]
            self.assertEqual(mail["descriptor"]["scan_engine"],"test-markers-only")
            with self.assertRaises(HTTPError) as blocked:
                lab.api(f"/v1/messages/{mail['id']}/attachments/0",user=lab.recipient)
            self.assertEqual(blocked.exception.code,403)
            normal=lab.api("/v1/uploads",{"filename":"normal.bin","data":base64.b64encode(b"ordinary").decode()})
            lab.send("corrupt",attachments=[normal["upload_id"]])
            wait_for(lab.settled)
            inbox=lab.api("/v1/messages",user=lab.recipient)["messages"][0]
            key=normal["key"]
            (lab.root/"objects"/key[:2]/key).write_bytes(b"corruption")
            with self.assertRaises(HTTPError) as integrity:
                lab.api(f"/v1/messages/{inbox['id']}/attachments/0",user=lab.recipient)
            self.assertEqual(integrity.exception.code,409)
            with self.assertRaises(HTTPError) as external:
                lab.api("/v1/messages",{"request_key":"external","to":["someone@example.com"],"subject":"no","body":"no","attachment_ids":[]})
            self.assertEqual(external.exception.code,403)
            with smtplib.SMTP("127.0.0.1",lab.config["smtp_port"],timeout=5) as smtp:
                smtp.login("test",lab.config["smtp_token"])
                smtp.mail("alice@local.test")
                self.assertEqual(smtp.rcpt("someone@example.com")[0],550)


if __name__=="__main__":
    unittest.main()
