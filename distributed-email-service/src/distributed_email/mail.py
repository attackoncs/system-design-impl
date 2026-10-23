from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from abc import ABC, abstractmethod

from .common import Error, digest, identifier, text
from .objects import Objects


def address(user):
    return identifier(user) + "@local.test"


def local_user(value, config):
    if not isinstance(value, str) or value.count("@") != 1:
        raise ValueError("invalid local address")
    user, domain = value.split("@")
    if domain.lower() != "local.test" or user not in config["users"]:
        raise Error(403, "only configured local.test test recipients are allowed")
    return user


def send(store, config, user, body):
    fields = {"request_key", "to", "subject", "body", "attachment_ids"}
    if not isinstance(body, dict) or set(body) != fields:
        raise ValueError("send requires request_key,to,subject,body,attachment_ids")
    identifier(body["request_key"])
    if (
        not isinstance(body["to"], list)
        or not 1 <= len(body["to"]) <= 10
        or len(set(body["to"])) != len(body["to"])
    ):
        raise ValueError("1..10 distinct local recipients required")
    for recipient in body["to"]:
        local_user(recipient, config)
    subject = text(body["subject"], "subject", 200)
    content = text(body["body"], "body", 65536)
    if "\r" in subject or "\n" in subject:
        raise ValueError("subject header injection")
    old = store.prior(user, body["request_key"], digest(body))
    if old:
        return old
    attachments = store.attachments(user, body["attachment_ids"])
    objects = Objects(config)
    body_object = objects.put(content.encode())
    delivery = digest([user, body["request_key"], "delivery"])
    message = EmailMessage(policy=policy.SMTP)
    message["From"], message["To"], message["Subject"] = (
        address(user),
        ", ".join(body["to"]),
        subject,
    )
    message["Message-ID"] = "<" + delivery + "@local.test>"
    message["X-Delivery-ID"] = delivery
    message.set_content(content)
    for attachment in attachments:
        message.add_attachment(
            objects.get(attachment["key"]),
            maintype="application",
            subtype="octet-stream",
            filename=attachment["filename"],
        )
    if attachments:
        message.set_boundary("local-mime-" + delivery)
    raw = objects.put(message.as_bytes())["key"]
    descriptor = {
        "delivery_id": delivery,
        "sender": address(user),
        "recipients": body["to"],
        "subject": subject,
        "body_key": body_object["key"],
        "attachments": attachments,
        "folder": "sent",
        "delivery": "queued",
        "scan": "not-scanned-outbound",
        "scan_engine": "none",
    }
    return store.enqueue(
        user, body["request_key"], digest(body), descriptor, content, raw
    )


class Scanner(ABC):
    @abstractmethod
    def scan(self, raw):
        """Return folder/status; never claim a test marker policy is virus detection."""


class TestMarkerScanner(Scanner):
    def scan(self, raw):
        if b"LOCAL_TEST_VIRUS_MARKER" in raw:
            return "quarantine", "test-marker-blocked", "test-markers-only"
        if b"LOCAL_TEST_SPAM_MARKER" in raw:
            return "spam", "test-marker-spam", "test-markers-only"
        return "inbox", "test-policy-pass", "test-markers-only"


class UnconfiguredScanner(Scanner):
    def scan(self, raw):
        return "quarantine", "unscanned", "none"


def parse_received(config, raw, sender, recipients, delivery):
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("RFC message exceeds 8 MiB")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.defects:
        raise ValueError("malformed MIME message")
    objects = Objects(config)
    attachments = []
    bodies = []
    scan_bytes = bytearray(raw)
    parts = list(message.walk())
    if len(parts) > 20:
        raise ValueError("too many MIME parts")
    for part in parts:
        if part.defects:
            raise ValueError("malformed MIME part")
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True) or b""
        scan_bytes.extend(payload)
        if part.get_content_disposition() == "attachment" or part.get_filename():
            if len(payload) > 2 * 1024 * 1024 or len(attachments) >= 3:
                raise ValueError("attachment limits exceeded")
            filename = text(part.get_filename() or "attachment.bin", "filename", 200)
            attachments.append({**objects.put(payload), "filename": filename})
        elif part.get_content_type() == "text/plain":
            bodies.append(
                payload.decode(part.get_content_charset() or "utf-8", errors="strict")
            )
    body = text("\n".join(bodies), "body", 65536)
    scanner = (
        TestMarkerScanner()
        if config.get("scanner") == "test-markers"
        else UnconfiguredScanner()
    )
    folder, status, engine = scanner.scan(bytes(scan_bytes))
    descriptor = {
        "delivery_id": delivery,
        "sender": sender,
        "recipients": recipients,
        "subject": text(str(message.get("Subject", "")), "subject", 200),
        "body_key": objects.put(body.encode())["key"],
        "attachments": attachments,
        "folder": folder,
        "delivery": "received",
        "scan": status,
        "scan_engine": engine,
    }
    return descriptor, body
