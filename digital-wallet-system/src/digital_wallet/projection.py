"""Pure event reducers. Replay performs no clock, network or money commands."""

import copy

from .common import identifier, integer
from .contracts import fields, intent


def reduce_event(state, event):
    """Pure deterministic projection: replay never invokes another service."""
    if not isinstance(event, dict):
        raise ValueError("event object required")
    if event.get("kind") == "genesis":
        fields(event, {"kind", "accounts"})
        if not isinstance(event["accounts"], dict):
            raise ValueError("genesis accounts required")
        for account in event["accounts"]:
            identifier(account)
        if state:
            raise ValueError("duplicate genesis")
        return {
            "accounts": {
                key: {"available": integer(value), "held": 0}
                for key, value in event["accounts"].items()
            },
            "parts": {},
        }
    fields(event, {"kind", "role", "intent"})
    kind, role = event["kind"], event["role"]
    if (
        kind not in {"local", "prepare", "confirm", "cancel"}
        or role not in {"source", "destination", "local"}
        or (kind == "local") != (role == "local")
        or not state
    ):
        raise ValueError("invalid event kind/role or missing genesis")
    state = copy.deepcopy(state)
    body = intent(event["intent"])
    id = body["transaction_id"]
    source, destination, amount = (
        body["from_account"],
        body["to_account"],
        body["amount"],
    )
    old = state["parts"].get(id)
    if old and (old["intent"] != body or old["role"] != role):
        raise ValueError("participant identity conflict")
    kind, role = event["kind"], event["role"]
    accounts = state["accounts"]
    if kind == "local":
        if old:
            raise ValueError("duplicate local event")
        valid = (
            source in accounts
            and destination in accounts
            and accounts[source]["available"] >= amount
        )
        status = "CONFIRMED" if valid else "REJECTED"
        if valid:
            accounts[source]["available"] -= amount
            accounts[destination]["available"] += amount
    elif kind == "prepare":
        if old:
            raise ValueError("duplicate prepare event")
        account = source if role == "source" else destination
        valid = account in accounts and (
            role == "destination" or accounts[account]["available"] >= amount
        )
        status = "PREPARED" if valid else "REJECTED"
        if valid and role == "source":
            accounts[source]["available"] -= amount
            accounts[source]["held"] += amount
    elif kind == "confirm":
        if not old or old["status"] != "PREPARED":
            raise ValueError("confirm requires durable prepare")
        if role == "source":
            accounts[source]["held"] -= amount
        else:
            accounts[destination]["available"] += amount
        status = "CONFIRMED"
    elif kind == "cancel":
        if old and old["status"] in {"CONFIRMED", "CANCELED"}:
            raise ValueError("confirmed participant cannot cancel")
        if old and old["status"] == "PREPARED" and role == "source":
            accounts[source]["held"] -= amount
            accounts[source]["available"] += amount
        status = "CANCELED"
    else:
        raise ValueError("unsupported shard event")
    if any(min(row.values()) < 0 for row in accounts.values()):
        raise ValueError("negative projection")
    state["parts"][id] = {"intent": body, "role": role, "status": status}
    return state
