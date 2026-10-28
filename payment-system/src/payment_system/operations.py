import json
import uuid
from contextlib import nullcontext
from .common import Error, digest, dumps, identifier, integer, money
from .contracts import freeze, receipt_copy


class OperationMixin:
    def operation(self, op, merchant=None, db=None):
        if db is None:
            with self.transaction() as connection:
                return self.operation(op, merchant, connection)
        row = db.execute("SELECT * FROM operations WHERE id=?", (op,)).fetchone()
        if not row or (merchant is not None and row["merchant"] != merchant):
            raise Error(404, "operation missing")
        value = dict(row)
        value.pop("hash")
        return value

    def create(self, merchant, kind, body, before_commit=None, *, db=None):
        body = freeze(body)
        identifier(merchant)
        fields = {"request_key", "amount", "currency"} | (
            {"order_id"}
            if kind == "payin"
            else {"destination"}
            if kind == "payout"
            else {"payment_id"}
        )
        if (
            kind not in ("payin", "payout", "refund")
            or not isinstance(body, dict)
            or set(body) != fields
        ):
            raise ValueError("invalid operation fields; no card or bank data accepted")
        key = identifier(body["request_key"])
        amount = money(body["amount"])
        if body["currency"] != "USD":
            raise ValueError("only USD integer cents supported")
        order = identifier(body["order_id"]) if kind == "payin" else None
        parent = identifier(body["payment_id"]) if kind == "refund" else None
        destination = body.get(
            "destination", "hosted" if kind == "payin" else "test-refund"
        )
        if kind == "payout" and destination not in (
            "test-bank-ok",
            "test-bank-decline",
        ):
            raise ValueError("only simulated destination token references supported")
        fingerprint = digest([kind, body])
        with self.transaction(True) if db is None else nullcontext(db) as db:
            old = db.execute(
                "SELECT * FROM operations WHERE merchant=? AND request_key=?",
                (merchant, key),
            ).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "request key content conflict")
                return self.operation(old["id"], merchant, db)
            if db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 10000:
                raise Error(429, "reference operation capacity reached")
            if (
                order
                and db.execute(
                    "SELECT 1 FROM operations WHERE merchant=? AND kind='payin' AND order_id=?",
                    (merchant, order),
                ).fetchone()
            ):
                raise Error(
                    409, "order already has a payment; reuse its original request key"
                )
            if not db.execute(
                "SELECT 1 FROM wallets WHERE merchant=?", (merchant,)
            ).fetchone():
                raise Error(404, "merchant missing")
            if parent:
                source = self.operation(parent, merchant, db)
                if source["kind"] != "payin" or source["status"] != "SUCCEEDED":
                    raise Error(409, "refund requires a successful owned pay-in")
                used = db.execute(
                    "SELECT coalesce(sum(amount),0) FROM operations WHERE parent=? AND status!='FAILED'",
                    (parent,),
                ).fetchone()[0]
                if used + amount > source["amount"]:
                    raise Error(
                        409,
                        "refund amount exceeds original payment after pending refunds",
                    )
            op = uuid.uuid4().hex
            db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,'CREATED',NULL,0,NULL,?)",
                (
                    op,
                    merchant,
                    kind,
                    order,
                    parent,
                    amount,
                    "USD",
                    destination,
                    key,
                    fingerprint,
                    self.clock(),
                ),
            )
            if kind != "payin":
                self.post(
                    db,
                    "reserve:" + op,
                    op,
                    "reserve",
                    "merchant:" + merchant + ":available",
                    "merchant:" + merchant + ":held",
                    amount,
                )
            self.enqueue(db, "execute:" + op, "execute", {"operation": op})
            if before_commit:
                before_commit()
            return self.operation(op, merchant, db)

    def apply(self, receipt, event_id=None):
        receipt = receipt_copy(receipt)
        if event_id is not None:
            identifier(event_id)
        required = {
            "id",
            "merchant",
            "kind",
            "parent",
            "amount",
            "currency",
            "provider_id",
            "status",
            "revision",
            "checkout",
        }
        if not isinstance(receipt, dict) or set(receipt) != required:
            raise ValueError("invalid PSP receipt")
        integer(receipt["revision"], "provider revision", 1)
        money(receipt["amount"])
        for field in ("id", "merchant", "provider_id"):
            identifier(receipt[field])
        if receipt["parent"] is not None:
            identifier(receipt["parent"])
        if receipt["status"] not in ("PENDING", "SUCCEEDED", "FAILED"):
            raise ValueError("invalid provider status")
        fingerprint = digest(receipt)
        with self.transaction(True) as db:
            if event_id:
                old = db.execute(
                    "SELECT * FROM inbox WHERE id=?", (identifier(event_id),)
                ).fetchone()
                if old:
                    if old["hash"] != fingerprint:
                        raise Error(409, "webhook event ID payload conflict")
                    return {**json.loads(old["result"]), "duplicate": True}
            row = db.execute(
                "SELECT * FROM operations WHERE id=?", (receipt["id"],)
            ).fetchone()
            result = {"applied": False, "duplicate": False}
            if not row:
                result["review"] = self.review(
                    db, receipt["id"], "UNKNOWN_PROVIDER_OPERATION", receipt
                )
            else:
                previous_version = db.execute(
                    "SELECT hash FROM provider_versions WHERE operation=? AND revision=?",
                    (row["id"], receipt["revision"]),
                ).fetchone()
                frozen = (
                    row["merchant"],
                    row["kind"],
                    row["parent"],
                    row["amount"],
                    row["currency"],
                )
                incoming = tuple(
                    receipt[k]
                    for k in ("merchant", "kind", "parent", "amount", "currency")
                )
                if frozen != incoming or (
                    row["provider_id"] and row["provider_id"] != receipt["provider_id"]
                ):
                    result["review"] = self.review(
                        db, row["id"], "RECEIPT_MISMATCH", receipt
                    )
                elif db.execute(
                    "SELECT 1 FROM operations WHERE provider_id=? AND id!=?",
                    (receipt["provider_id"], row["id"]),
                ).fetchone():
                    result["review"] = self.review(
                        db, row["id"], "PROVIDER_ID_REUSED", receipt
                    )
                elif previous_version and previous_version["hash"] != fingerprint:
                    result["review"] = self.review(
                        db, row["id"], "REVISION_CONFLICT", receipt
                    )
                elif receipt["revision"] < row["revision"]:
                    result["ignored"] = "older revision"
                elif row["status"] in ("SUCCEEDED", "FAILED"):
                    if receipt["status"] != row["status"]:
                        result["review"] = self.review(
                            db, row["id"], "TERMINAL_CONFLICT", receipt
                        )
                    else:
                        result["ignored"] = "already terminal"
                elif receipt["revision"] == row["revision"] and row["status"] not in (
                    "UNKNOWN",
                    "CREATED",
                ):
                    if (
                        receipt["status"] != row["status"]
                        or receipt["checkout"] != row["checkout"]
                    ):
                        result["review"] = self.review(
                            db, row["id"], "REVISION_CONFLICT", receipt
                        )
                    else:
                        result["ignored"] = "already applied revision"
                else:
                    op, merchant = row["id"], row["merchant"]
                    if receipt["status"] == "SUCCEEDED":
                        debit = (
                            "psp:clearing"
                            if row["kind"] == "payin"
                            else "merchant:" + merchant + ":held"
                        )
                        credit = (
                            "merchant:" + merchant + ":available"
                            if row["kind"] == "payin"
                            else "psp:clearing"
                        )
                        self.post(
                            db,
                            "settle:" + op,
                            op,
                            "settle",
                            debit,
                            credit,
                            row["amount"],
                            receipt["provider_id"],
                        )
                    elif receipt["status"] == "FAILED" and row["kind"] != "payin":
                        self.post(
                            db,
                            "release:" + op,
                            op,
                            "release",
                            "merchant:" + merchant + ":held",
                            "merchant:" + merchant + ":available",
                            row["amount"],
                            receipt["provider_id"],
                        )
                    db.execute(
                        "UPDATE operations SET status=?,provider_id=?,revision=?,checkout=? WHERE id=?",
                        (
                            receipt["status"],
                            receipt["provider_id"],
                            receipt["revision"],
                            receipt["checkout"],
                            op,
                        ),
                    )
                    if receipt["status"] != "PENDING":
                        db.execute(
                            "UPDATE jobs SET status='done' WHERE id=?",
                            ("execute:" + op,),
                        )
                        self.enqueue(
                            db,
                            "notify:" + op,
                            "notify",
                            {
                                "event_id": "terminal_" + op,
                                "operation": op,
                                "merchant": merchant,
                                "status": receipt["status"],
                                "amount": row["amount"],
                                "currency": "USD",
                            },
                        )
                    result["applied"] = True
                if not result.get("review"):
                    db.execute(
                        "INSERT OR IGNORE INTO provider_versions VALUES(?,?,?)",
                        (row["id"], receipt["revision"], fingerprint),
                    )
            if event_id:
                db.execute(
                    "INSERT INTO inbox VALUES(?,?,?)",
                    (event_id, fingerprint, dumps(result)),
                )
            return result
