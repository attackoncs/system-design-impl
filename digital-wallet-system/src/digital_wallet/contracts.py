"""Frozen command values shared by live processing and deterministic replay."""

from .common import encode, identifier, integer


def freeze(value):
    from .transport import loads

    return loads(encode(value))


def intent(body):
    body = freeze(body)
    if not isinstance(body, dict) or set(body) != {
        "transaction_id",
        "from_account",
        "to_account",
        "amount",
        "currency",
    }:
        raise ValueError("exact transfer fields required")
    for field in ("transaction_id", "from_account", "to_account"):
        identifier(body[field])
    integer(body["amount"], 1, 100_000_000)
    if body["currency"] != "USD" or body["from_account"] == body["to_account"]:
        raise ValueError("USD transfer between distinct accounts required")
    return body


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("exact fields required: " + ", ".join(sorted(expected)))
    return value


def participant(value, body, role):
    """Never advance money from a reply for another intent or participant role."""
    if value is None:
        return None
    fields(value, {"intent", "role", "status"})
    received_intent = intent(value["intent"])
    if (
        received_intent != body
        or value["role"] != role
        or value["status"] not in {"PREPARED", "REJECTED", "CANCELED", "CONFIRMED"}
    ):
        raise ValueError("participant receipt conflicts with transfer")
    if role == "local" and value["status"] not in {"CONFIRMED", "REJECTED"}:
        raise ValueError("invalid local receipt")
    return value
