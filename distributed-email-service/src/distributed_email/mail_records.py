"""Validate and freeze mailbox records before entering a write transaction."""

import json
import re

from .common import dumps, identifier, integer, terms, text


FIELDS = ("subject", "sender", "body")
FOLDERS = ("inbox", "sent", "spam", "quarantine")


def object_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid object key")
    return value


def local_address(value):
    text(value, "local address", 254)
    if value.count("@") != 1:
        raise ValueError("invalid local address")
    user, domain = value.split("@")
    identifier(user)
    if domain.lower() != "local.test":
        raise ValueError("local.test address required")
    return value


def attachment(value):
    if not isinstance(value, dict) or set(value) != {"key", "size", "filename"}:
        raise ValueError("attachment requires key, size and filename")
    object_key(value["key"])
    integer(value["size"], "attachment size", 0, 2 * 1024 * 1024)
    name = text(value["filename"], "filename", 200)
    if not name or any(c in name for c in ("/", "\\", "\r", "\n")):
        raise ValueError("attachment filename must be a plain basename")
    return json.loads(dumps(value))


def descriptor(value):
    required = {
        "subject",
        "sender",
        "recipients",
        "folder",
        "delivery_id",
        "body_key",
        "attachments",
        "scan",
        "scan_engine",
    }
    if not isinstance(value, dict) or set(value) not in (
        required,
        required | {"delivery"},
    ):
        raise ValueError("invalid message descriptor fields")
    value = json.loads(dumps(value))
    subject = text(value["subject"], "subject", 200)
    if "\r" in subject or "\n" in subject:
        raise ValueError("subject header injection")
    local_address(value["sender"])
    recipients = value["recipients"]
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 10:
        raise ValueError("1..10 recipients required")
    for recipient in recipients:
        local_address(recipient)
    if len(set(recipients)) != len(recipients):
        raise ValueError("distinct recipients required")
    if value["folder"] not in FOLDERS:
        raise ValueError("invalid message folder")
    identifier(value["delivery_id"])
    object_key(value["body_key"])
    if not isinstance(value["attachments"], list) or len(value["attachments"]) > 3:
        raise ValueError("at most three attachments")
    value["attachments"] = [attachment(a) for a in value["attachments"]]
    for field in ("scan", "scan_engine", "delivery"):
        if field in value:
            text(value[field], field, 80)
    return value


def indexed_message(value, body):
    value = descriptor(value)
    body = text(body, "body", 65536)
    postings = [
        (field, term)
        for field, content in (
            ("subject", value["subject"]),
            ("sender", value["sender"]),
            ("body", body),
        )
        for term in terms(content)
    ]
    if len(postings) > 4096:
        from .common import Error

        raise Error(422, "message exceeds 4096 indexed field terms")
    return value, postings
