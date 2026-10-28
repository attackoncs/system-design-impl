"""Regression coverage for durable SMTP jobs and strict MIME boundaries."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from email import policy
from email.message import EmailMessage
import hashlib
from pathlib import Path
import smtplib
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from distributed_email.common import Error
from distributed_email.mail import parse_received, send
from distributed_email.smtp import Spool, smtp_server
from distributed_email.worker import _smtp_send, run_sender

CONFIG = {
    "users": {"alice": "alice-token", "bob": "bob-token"},
    "primary_urls": ["http://127.0.0.1:1"],
    "internal_token": "internal-token",
    "smtp_token": "smtp-token",
    "scanner": "test-markers",
}


def raw_message(delivery="a" * 64, content="hello", **kwargs):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = "alice@local.test"
    message["To"] = "bob@local.test"
    message["Subject"] = "Project"
    message["X-Delivery-ID"] = delivery
    message.set_content(content, **kwargs)
    return message.as_bytes()


class MemoryObjects:
    def __init__(self):
        self.data, self.calls = {}, []

    def put(self, raw, **kwargs):
        key = hashlib.sha256(raw).hexdigest()
        self.data[key] = raw
        self.calls.append(("put", kwargs))
        return {"key": key, "size": len(raw)}

    def get(self, key, **kwargs):
        self.calls.append(("get", kwargs))
        return self.data[key]


class SpoolReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.path = Path(self.temp.name) / "spool.sqlite"
        self.spool = Spool(self.path, clock=lambda: self.now, lease_seconds=90)
        self.objects = MemoryObjects()
        for module in ("spool", "mail"):
            patcher = patch(
                f"distributed_email.{module}.Objects", return_value=self.objects
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def accept(self, raw=None, recipients=None):
        return self.spool.accept(
            CONFIG,
            raw_message() if raw is None else raw,
            "alice@local.test",
            ["bob@local.test"] if recipients is None else recipients,
        )

    def test_accept_lost_ack_is_durable_and_conflict_checked(self):
        self.spool.control({"lose_ack": True, "scanner_down": False})
        self.assertEqual(self.accept(), ("a" * 64, 1))
        self.assertEqual(self.accept(), ("a" * 64, 1))
        self.assertEqual(Spool(self.path).status()["unique_deliveries"], 1)
        with self.assertRaises(Error) as raised:
            self.accept(raw_message(content="changed"))
        self.assertEqual(raised.exception.status, 409)

    def test_accept_rejects_duplicate_delivery_header_and_invalid_envelope(self):
        raw = raw_message().replace(
            b"Subject: Project",
            b"X-Delivery-ID: " + b"b" * 64 + b"\r\nSubject: Project",
        )
        with self.assertRaises(ValueError):
            self.accept(raw)
        with self.assertRaises(ValueError):
            self.accept(recipients=["bob@local.test", "bob@local.test"])
        with self.assertRaises(Error):
            self.accept(recipients=["outside@example.com"])
        self.assertEqual(self.objects.data, {})

    def test_concurrent_claim_has_one_owner(self):
        self.accept()
        with ThreadPoolExecutor(max_workers=8) as executor:
            jobs = list(executor.map(lambda _: self.spool.claim(), range(8)))
        claimed = [job for job in jobs if job]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["attempts"], 1)

    def test_expired_and_reclaimed_attempts_cannot_settle(self):
        self.accept()
        old = self.spool.claim()
        self.now += 91
        self.assertFalse(self.spool.settle(old))
        current = self.spool.claim()
        self.assertNotEqual(old["token"], current["token"])
        self.assertTrue(self.spool.settle(current))
        self.assertFalse(self.spool.settle(old, "late error"))
        self.assertEqual(self.spool.status()["rows"][0]["status"], "done")

    def test_retry_waits_until_due_and_survives_restart(self):
        self.accept()
        first = self.spool.claim()
        self.assertTrue(self.spool.settle(first, "temporary"))
        self.assertIsNone(self.spool.claim())
        self.now += 1
        reopened = Spool(self.path, clock=lambda: self.now)
        self.assertEqual(reopened.claim()["attempts"], 2)

    def test_claim_clock_is_sampled_after_writer_lock(self):
        self.accept()
        started = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.spool.transaction(True):

                def claim():
                    started.set()
                    return self.spool.claim()

                future = executor.submit(claim)
                self.assertTrue(started.wait(1))
                # The transaction must be released before joining its waiter.
                self.now += 10
            self.assertEqual(future.result(timeout=2)["lease"], self.now + 90)

    def test_legacy_schema_migrates_without_losing_pending_receipt(self):
        legacy = Path(self.temp.name) / "legacy.sqlite"
        with closing(sqlite3.connect(legacy)) as db:
            db.execute(
                "CREATE TABLE spool(id TEXT PRIMARY KEY,hash TEXT,raw TEXT,sender TEXT,recipients TEXT,status TEXT,attempts INTEGER,due REAL,error TEXT)"
            )
            db.execute(
                "INSERT INTO spool VALUES('legacy','hash','key','alice','[]','pending',0,0,'')"
            )
            db.commit()
        migrated = Spool(legacy, clock=lambda: self.now)
        self.assertEqual(migrated.status()["unique_deliveries"], 1)
        self.assertEqual(migrated.claim()["id"], "legacy")

    def test_scanner_outage_retries_then_recovers(self):
        self.accept()
        self.spool.control({"lose_ack": False, "scanner_down": True})
        self.assertTrue(self.spool.process_once(CONFIG))
        self.assertEqual(self.spool.status()["rows"][0]["status"], "pending")
        self.now += 1
        self.spool.control({"lose_ack": False, "scanner_down": False})
        with patch("distributed_email.spool.http_json", return_value={}) as rpc:
            self.spool.process_once(CONFIG)
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(self.spool.status()["rows"][0]["status"], "done")
        self.assertEqual(self.spool.status()["rows"][0]["attempts"], 2)

    def test_partial_delivery_retries_same_recipient_ids(self):
        self.accept(recipients=["alice@local.test", "bob@local.test"])
        received, failed = [], [False]

        def receive(_url, _token, body, **kwargs):
            received.append((body["user"], body["delivery"]))
            if body["user"] == "bob" and not failed[0]:
                failed[0] = True
                raise OSError("temporary mailbox outage")
            self.assertIsNotNone(kwargs["deadline"])
            return {}

        with patch("distributed_email.spool.http_json", side_effect=receive):
            self.spool.process_once(CONFIG)
            self.now += 1
            self.spool.process_once(CONFIG)
        self.assertEqual(len(set(received)), 2)
        self.assertEqual(received[0], received[2])
        self.assertEqual(self.spool.status()["rows"][0]["status"], "done")

    def test_invalid_charset_becomes_terminal_rejection(self):
        raw = raw_message().replace(b'charset="utf-8"', b'charset="not-a-real-charset"')
        self.accept(raw)
        self.spool.process_once(CONFIG)
        self.assertEqual(self.spool.status()["rows"][0]["status"], "rejected")

    def test_stop_does_not_claim_new_receive_work(self):
        self.accept()
        stop = threading.Event()
        stop.set()
        self.assertFalse(self.spool.process_once(CONFIG, stop=stop))
        self.assertEqual(self.spool.status()["rows"][0]["attempts"], 0)


class MimeReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.objects = MemoryObjects()
        patcher = patch("distributed_email.mail.Objects", return_value=self.objects)
        patcher.start()
        self.addCleanup(patcher.stop)

    def parse(self, raw, config=CONFIG):
        return parse_received(
            config,
            raw,
            "alice@local.test",
            ["bob@local.test"],
            "a" * 64,
            deadline=123456.0,
        )

    def test_encoded_attachment_is_scanned_and_deadline_propagates(self):
        message = EmailMessage(policy=policy.SMTP)
        message["Subject"] = "Project"
        message.set_content("hello")
        message.add_attachment(
            b"LOCAL_TEST_VIRUS_MARKER",
            maintype="application",
            subtype="octet-stream",
            filename="test.bin",
        )
        descriptor, body = self.parse(message.as_bytes())
        self.assertEqual(descriptor["folder"], "quarantine")
        self.assertEqual(descriptor["scan_engine"], "test-markers-only")
        self.assertEqual(body.strip(), "hello")
        self.assertEqual(len(descriptor["attachments"]), 1)
        self.assertTrue(
            all(kwargs["deadline"] == 123456.0 for _, kwargs in self.objects.calls)
        )

    def test_unconfigured_scanner_always_quarantines(self):
        descriptor, _ = self.parse(
            raw_message(), {**CONFIG, "scanner": "quarantine-all"}
        )
        self.assertEqual(
            (descriptor["folder"], descriptor["scan"]), ("quarantine", "unscanned")
        )

    def test_bad_base64_discovered_during_decode_is_rejected(self):
        raw = b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n@@@@\r\n"
        with self.assertRaises(ValueError):
            self.parse(raw)
        self.assertEqual(self.objects.data, {})

    def test_duplicate_part_headers_are_rejected_before_object_writes(self):
        message = EmailMessage(policy=policy.SMTP)
        message.set_content("hello")
        message.add_attachment(
            b"a", maintype="application", subtype="octet-stream", filename="test.bin"
        )
        raw = message.as_bytes().replace(
            b"Content-Transfer-Encoding: base64",
            b"Content-Transfer-Encoding: base64\r\nContent-Transfer-Encoding: base64",
        )
        with self.assertRaises(ValueError):
            self.parse(raw)
        self.assertEqual(self.objects.data, {})

    def test_part_and_attachment_limits(self):
        for attachment_count in (4, 21):
            message = EmailMessage(policy=policy.SMTP)
            message.set_content("hello")
            for _ in range(attachment_count):
                message.add_attachment(
                    b"a",
                    maintype="application",
                    subtype="octet-stream",
                    filename="a.bin",
                )
            with self.assertRaises(ValueError):
                self.parse(message.as_bytes())
        self.assertEqual(self.objects.data, {})

    def test_nested_message_attachment_is_explicitly_rejected(self):
        message = EmailMessage(policy=policy.SMTP)
        message.set_content("hello")
        attachment = EmailMessage(policy=policy.SMTP)
        attachment.set_content("attached message")
        message.add_attachment(attachment)
        with self.assertRaises(ValueError):
            self.parse(message.as_bytes())

    def test_unhashable_recipient_is_validation_failure(self):
        with self.assertRaises(ValueError):
            send(
                None,
                CONFIG,
                "alice",
                {
                    "request_key": "key",
                    "to": [{}],
                    "subject": "",
                    "body": "",
                    "attachment_ids": [],
                },
            )


class SenderReliabilityTests(unittest.TestCase):
    def test_stopped_sender_never_claims(self):
        stop = threading.Event()
        stop.set()
        with patch("distributed_email.worker.http_json") as rpc:
            run_sender(CONFIG, 0, stop)
        rpc.assert_not_called()

    def test_sender_reuses_shared_budget_and_raw_and_stops_after_settle(self):
        stop = threading.Event()
        job = {
            "raw": "raw",
            "sender": "alice@local.test",
            "recipients": ["bob@local.test"],
            "token": "lease",
        }
        calls = []

        def rpc(url, _token, body, **kwargs):
            calls.append((url, body, kwargs))
            if url.endswith("/claim"):
                return {"job": job}
            stop.set()
            return {}

        objects = MemoryObjects()
        objects.data["raw"] = b"same raw bytes"
        with (
            patch("distributed_email.worker.http_json", side_effect=rpc),
            patch("distributed_email.worker.Objects", return_value=objects),
            patch("distributed_email.worker._smtp_send") as smtp,
        ):
            run_sender(CONFIG, 0, stop)
        self.assertEqual(calls[0][2]["deadline"], calls[1][2]["deadline"])
        self.assertEqual(objects.calls[0][1]["deadline"], calls[0][2]["deadline"])
        self.assertEqual(smtp.call_args.args[2], b"same raw bytes")
        self.assertEqual(calls[1][1], {"job": job})


class SmtpProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.spool = Spool(Path(self.temp.name) / "spool.sqlite")
        self.instance = smtp_server(
            CONFIG, self.spool, "127.0.0.1", 0, max_connections=1
        )
        self.port = self.instance.server_address[1]
        self.thread = threading.Thread(
            target=self.instance.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.instance.shutdown()
        self.instance.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def test_auth_identity_and_envelope_prefixes(self):
        with smtplib.SMTP("127.0.0.1", self.port, timeout=2) as smtp:
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                smtp.login("wrong-user", CONFIG["smtp_token"])
            self.assertEqual(smtp.docmd("MAIL", "FROM:<alice@local.test>")[0], 530)
            smtp.login("local-worker", CONFIG["smtp_token"])
            self.assertEqual(smtp.docmd("MAIL", "TO:<alice@local.test>")[0], 550)
            self.assertEqual(smtp.docmd("RCPT", "FROM:<bob@local.test>")[0], 550)
            self.assertEqual(smtp.mail("alice@local.test")[0], 250)
            self.assertEqual(smtp.rcpt("bob@local.test")[0], 250)
            # Failed MAIL discards its previous recipients.
            self.assertEqual(smtp.docmd("MAIL", "TO:<alice@local.test>")[0], 550)
            self.assertEqual(smtp.docmd("DATA")[0], 550)

    def test_connection_capacity_is_bounded(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as first:
            self.assertTrue(first.recv(1024).startswith(b"220"))
            with socket.create_connection(
                ("127.0.0.1", self.port), timeout=2
            ) as second:
                self.assertTrue(second.recv(1024).startswith(b"421"))

    def test_sender_smtp_deadline_closes_slow_connection(self):
        # A configured but stalled peer is interrupted by one total budget.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        release = threading.Event()

        def peer():
            connection, _ = listener.accept()
            with connection:
                connection.sendall(b"220 local.test\r\n")
                release.wait(2)

        thread = threading.Thread(target=peer)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaises((OSError, smtplib.SMTPException)):
                _smtp_send(
                    {**CONFIG, "smtp_port": listener.getsockname()[1]},
                    {"sender": "alice@local.test", "recipients": ["bob@local.test"]},
                    b"raw",
                    started + 0.15,
                    threading.Event(),
                )
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            release.set()
            listener.close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
