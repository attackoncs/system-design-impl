from .common import Error, money


class LedgerMixin:
    def post(self, db, id, op, kind, debit, credit, amount, provider=None):
        amount = money(amount)
        source = self.operation(op, db=db)
        available, held = (
            "merchant:" + source["merchant"] + ":available",
            "merchant:" + source["merchant"] + ":held",
        )
        expected = {
            "reserve": (available, held),
            "release": (held, available),
            "settle": ("psp:clearing", available)
            if source["kind"] == "payin"
            else (held, "psp:clearing"),
        }
        if (
            kind not in expected
            or (debit, credit) != expected[kind]
            or amount != source["amount"]
            or (source["kind"] == "payin" and kind != "settle")
        ):
            raise Error(409, "ledger posting does not match operation")
        old = db.execute("SELECT * FROM ledger WHERE id=?", (id,)).fetchone()
        if old:
            lines = [
                tuple(row)
                for row in db.execute(
                    "SELECT account,debit,credit FROM lines WHERE tx=? ORDER BY line",
                    (id,),
                )
            ]
            if (old["operation"], old["kind"], old["provider_id"], old["posted"]) != (
                op,
                kind,
                provider,
                1,
            ) or lines != [(debit, amount, 0), (credit, 0, amount)]:
                raise Error(409, "ledger identity reused for a different posting")
            return False
        db.execute(
            "INSERT INTO ledger VALUES(?,?,?,?,0,?)",
            (id, op, kind, provider, self.clock()),
        )
        db.executemany(
            "INSERT INTO lines VALUES(?,?,?,?,?)",
            [(id, 1, debit, amount, 0), (id, 2, credit, 0, amount)],
        )
        for account, delta in ((debit, -amount), (credit, amount)):
            if account.startswith("merchant:"):
                _, merchant, field = account.split(":")
                if field not in ("available", "held"):
                    raise ValueError("invalid wallet ledger account")
                cursor = db.execute(
                    f"UPDATE wallets SET {field}={field}+? WHERE merchant=? AND {field}+?>=0",
                    (delta, merchant, delta),
                )
                if not cursor.rowcount:
                    raise Error(409, "insufficient available/held merchant funds")
        db.execute("UPDATE ledger SET posted=1 WHERE id=?", (id,))
        return True

    def wallet(self, merchant):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM wallets WHERE merchant=?", (merchant,)
            ).fetchone()
            if not row:
                raise Error(404, "merchant missing")
            return {**dict(row), "currency": "USD"}

    def audit(self, db):
        errors = []
        totals = dict(
            db.execute(
                "SELECT l.account,sum(l.credit-l.debit) FROM lines l JOIN ledger t ON t.id=l.tx WHERE t.posted=1 GROUP BY l.account"
            ).fetchall()
        )
        for row in db.execute("SELECT * FROM wallets"):
            for field in ("available", "held"):
                if row[field] != totals.get(
                    "merchant:" + row["merchant"] + ":" + field, 0
                ):
                    errors.append(
                        {
                            "merchant": row["merchant"],
                            "field": field,
                            "reason": "WALLET_LEDGER_MISMATCH",
                        }
                    )
        imbalance = db.execute(
            "SELECT coalesce(sum(l.debit-l.credit),0) FROM lines l JOIN ledger t ON t.id=l.tx WHERE t.posted=1"
        ).fetchone()[0]
        if (
            imbalance
            or db.execute("SELECT count(*) FROM ledger WHERE posted=0").fetchone()[0]
        ):
            errors.append({"reason": "UNBALANCED_OR_UNPOSTED_LEDGER"})
        cash = -totals.get("psp:clearing", 0)
        liability = db.execute(
            "SELECT coalesce(sum(available+held),0) FROM wallets"
        ).fetchone()[0]
        if cash != liability:
            errors.append({"reason": "ASSET_LIABILITY_MISMATCH"})
        return {
            "matches": not errors,
            "cash": cash,
            "merchant_liability": liability,
            "errors": errors,
        }

    def ledger(self, merchant):
        with self.transaction() as db:
            rows = []
            for row in db.execute(
                "SELECT l.* FROM ledger l JOIN operations o ON o.id=l.operation WHERE o.merchant=? AND l.posted=1 ORDER BY l.created,l.id LIMIT 101",
                (merchant,),
            ).fetchall():
                rows.append(
                    {
                        **dict(row),
                        "lines": [
                            dict(r)
                            for r in db.execute(
                                "SELECT account,debit,credit FROM lines WHERE tx=? ORDER BY line",
                                (row["id"],),
                            )
                        ],
                    }
                )
            if len(rows) > 100:
                raise Error(
                    422,
                    "ledger view exceeds reference response limit; use offline audit",
                )
            return {"transactions": rows, "currency": "USD"}
