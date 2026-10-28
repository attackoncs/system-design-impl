"""Replay only coordinator facts; never reissue participant commands."""

import copy
from .common import identifier
from .contracts import fields, intent


def project(rows, initial=None):
    state = copy.deepcopy(initial) if initial is not None else {}
    for row in rows:
        event = row["event"]
        if not isinstance(event, dict):
            raise ValueError("coordinator event object required")
        id = identifier(event["transaction_id"])
        if event["kind"] == "accepted":
            fields(event, {"kind", "transaction_id", "intent"})
            body = intent(event["intent"])
            if body["transaction_id"] != id:
                raise ValueError("accepted identity mismatch")
            if id in state:
                raise ValueError("duplicate accepted command in journal")
            state[id] = {
                "intent": body,
                "decision": None,
                "status": "PENDING",
            }
        elif event["kind"] == "decision":
            fields(event, {"kind", "transaction_id", "decision"})
            if id not in state or state[id]["status"] != "PENDING":
                raise ValueError("decision requires pending command")
            if state[id]["decision"] is not None or event["decision"] not in (
                "COMMIT",
                "CANCEL",
            ):
                raise ValueError("invalid or duplicate decision")
            state[id]["decision"] = event["decision"]
        elif event["kind"] == "completed":
            fields(event, {"kind", "transaction_id", "status"})
            if id not in state:
                raise ValueError("completion without command")
            decision = state[id]["decision"]
            if (
                decision == "COMMIT"
                and event["status"] != "SUCCEEDED"
                or decision == "CANCEL"
                and event["status"] != "CANCELED"
                or decision is None
                and event["status"] == "CANCELED"
            ):
                raise ValueError("completion conflicts with durable decision")
            if state[id]["status"] != "PENDING" or event["status"] not in (
                "SUCCEEDED",
                "CANCELED",
                "REJECTED",
            ):
                raise ValueError("invalid terminal status")
            state[id]["status"] = event["status"]
        else:
            raise ValueError("unknown coordinator event")
    return state
