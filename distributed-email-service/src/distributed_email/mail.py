from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from abc import ABC, abstractmethod
import time

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


def send(store, config, user, body, *, deadline=None, cancel=None):
    deadline = time.monotonic() + 20 if deadline is None else deadline
    fields = {"request_key", "to", "subject", "body", "attachment_ids"}
    if not isinstance(body, dict) or set(body) != fields:
        raise ValueError("send requires request_key,to,subject,body,attachment_ids")
    identifier(body["request_key"])
    if not isinstance(body["to"], list) or not 1 <= len(body["to"]) <= 10:
        raise ValueError("1..10 distinct local recipients required")
    for recipient in body["to"]:
        local_user(recipient, config)
    if len(set(body["to"])) != len(body["to"]):
        raise ValueError("1..10 distinct local recipients required")
    subject = text(body["subject"], "subject", 200)
    content = text(body["body"], "body", 65536)
    if "\r" in subject or "\n" in subject:
        raise ValueError("subject header injection")
    old = store.prior(user, body["request_key"], digest(body))
    if old:
        return old
    attachments = store.attachments(user, body["attachment_ids"])
    objects = Objects(config)
    body_object = objects.put(content.encode(), deadline=deadline, cancel=cancel)
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
            objects.get(attachment["key"], deadline=deadline, cancel=cancel),
            maintype="application",
            subtype="octet-stream",
            filename=attachment["filename"],
        )
    if attachments:
        message.set_boundary("local-mime-" + delivery)
    raw = objects.put(message.as_bytes(), deadline=deadline, cancel=cancel)["key"]
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


def parse_received(
    config, raw, sender, recipients, delivery, *, deadline=None, cancel=None
):
    deadline = time.monotonic() + 20 if deadline is None else deadline
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 8 * 1024 * 1024:
        raise ValueError("RFC message exceeds 8 MiB")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except (RecursionError, LookupError) as exc:
        raise ValueError("malformed MIME structure") from exc
    if message.defects:
        raise ValueError("malformed MIME message")
    objects = Objects(config)
    attachments = []
    bodies = []
    scan_bytes = bytearray(raw)
    # An iterative traversal enforces a structural bound without recursively
    # walking untrusted MIME nesting.
    parts, pending = [], [message]
    while pending:
        part = pending.pop()
        parts.append(part)
        if len(parts) > 20:
            raise ValueError("too many MIME parts")
        if part.is_multipart():
            pending.extend(reversed(part.get_payload()))
    total_decoded = 0
    attachment_data = []
    for part in parts:
        for name in (
            "Subject",
            "From",
            "To",
            "Message-ID",
            "X-Delivery-ID",
            "Content-Type",
            "Content-Transfer-Encoding",
            "Content-Disposition",
        ):
            headers = part.get_all(name, [])
            if len(headers) > 1 or any(
                getattr(value, "defects", ()) for value in headers
            ):
                raise ValueError("duplicate or malformed MIME header")
        if part.defects:
            raise ValueError("malformed MIME part")
        if part.get_content_maintype() == "message" or (
            part.is_multipart() and part.get_content_disposition() == "attachment"
        ):
            raise ValueError(
                "nested message/container attachments are unsupported; use octet-stream"
            )
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True) or b""
        # Transfer decoding itself can discover defects (notably bad base64).
        if part.defects:
            raise ValueError("malformed MIME transfer encoding")
        total_decoded += len(payload)
        if total_decoded > 8 * 1024 * 1024:
            raise ValueError("decoded MIME exceeds 8 MiB")
        scan_bytes.extend(payload)
        if part.get_content_disposition() == "attachment" or part.get_filename():
            if len(payload) > 2 * 1024 * 1024 or len(attachment_data) >= 3:
                raise ValueError("attachment limits exceeded")
            filename = text(part.get_filename() or "attachment.bin", "filename", 200)
            attachment_data.append((filename, payload))
        elif part.get_content_type() == "text/plain":
            try:
                bodies.append(
                    payload.decode(
                        part.get_content_charset() or "utf-8", errors="strict"
                    )
                )
            except (LookupError, UnicodeError) as exc:
                raise ValueError("invalid text MIME charset or payload") from exc
    body = text("\n".join(bodies), "body", 65536)
    subject = text(str(message.get("Subject", "")), "subject", 200)
    scanner = (
        TestMarkerScanner()
        if config.get("scanner") == "test-markers"
        else UnconfiguredScanner()
    )
    folder, status, engine = scanner.scan(bytes(scan_bytes))
    for filename, payload in attachment_data:
        attachments.append(
            {
                **objects.put(payload, deadline=deadline, cancel=cancel),
                "filename": filename,
            }
        )
    descriptor = {
        "delivery_id": delivery,
        "sender": sender,
        "recipients": recipients,
        "subject": subject,
        "body_key": objects.put(body.encode(), deadline=deadline, cancel=cancel)["key"],
        "attachments": attachments,
        "folder": folder,
        "delivery": "received",
        "scan": status,
        "scan_engine": engine,
    }
    return descriptor, body
