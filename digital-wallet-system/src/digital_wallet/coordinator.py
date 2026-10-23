import threading
import time

from .common import Error, request, shard_for
from .journal import Journal
from .shard import intent


def project(rows):
    state = {}
    for row in rows:
        event = row["event"]
        id = event["transaction_id"]
        if event["kind"] == "accepted":
            if id in state:
                raise ValueError("duplicate accepted command in journal")
            state[id] = {
                "intent": event["intent"],
                "decision": None,
                "status": "PENDING",
            }
        elif event["kind"] == "decision":
            if state[id]["decision"] is not None or event["decision"] not in (
                "COMMIT",
                "CANCEL",
            ):
                raise ValueError("invalid or duplicate decision")
            state[id]["decision"] = event["decision"]
        elif event["kind"] == "completed":
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


class Coordinator:
    def __init__(self, path, config, clock=time.time):
        self.config, self.clock = config, clock
        self.journal = Journal(path, config)
        self.lock = threading.RLock()
        self.journal.initialize(
            "CREATE TABLE IF NOT EXISTS retries(id TEXT PRIMARY KEY,failures INTEGER,due REAL,dead INTEGER,error TEXT);"
        )

    def state(self):
        return project(self.journal.records())

    def create(self, body, owner):
        intent(body)
        accounts = self.config["accounts"]
        if (
            body["from_account"] not in accounts
            or accounts[body["from_account"]]["owner"] != owner
        ):
            raise Error(403, "only owned source accounts may transfer")
        if body["to_account"] not in accounts:
            raise Error(404, "destination account missing")
        with self.lock:
            self.journal.sync()
            old = self.state().get(body["transaction_id"])
            if old:
                if old["intent"] != body:
                    raise Error(409, "transaction identity content conflict")
                return old
            if len(self.state()) >= 1000:
                raise Error(
                    429, "reference command history is limited to 1000 transfers"
                )
            self.journal.append(
                {
                    "kind": "accepted",
                    "transaction_id": body["transaction_id"],
                    "intent": body,
                }
            )
            return self.state()[body["transaction_id"]]

    def owned(self, id, owner):
        value = self.state().get(id)
        if not value or owner not in {
            self.config["accounts"][value["intent"][field]]["owner"]
            for field in ("from_account", "to_account")
        }:
            raise Error(404, "transfer missing")
        return value

    def shard(self, index, path, body=None):
        return request(
            self.config["shards"][index] + path,
            self.config["internal_token"],
            body,
            timeout=0.5,
        )

    def indices(self, body):
        return tuple(
            shard_for(body[key], len(self.config["shards"]))
            for key in ("from_account", "to_account")
        )

    def action(self, index, body, kind, role):
        # The shard also checks the persisted epoch inside its monetary transaction.
        return self.shard(
            index,
            "/internal/action",
            {**self.journal.credentials, "intent": body, "kind": kind, "role": role},
        )

    def append(self, id, kind, **fields):
        self.journal.append({"kind": kind, "transaction_id": id, **fields})

    def advance(self, id, value):
        body = value["intent"]
        source, destination = self.indices(body)
        left = self.shard(source, "/internal/view")["parts"].get(id)
        if source == destination:
            part = left or self.action(source, body, "local", "local")
            self.append(
                id,
                "completed",
                status="SUCCEEDED" if part["status"] == "CONFIRMED" else "REJECTED",
            )
            return
        right = self.shard(destination, "/internal/view")["parts"].get(id)
        if value["decision"] is None:
            if any(
                part and part["status"] in ("REJECTED", "CANCELED")
                for part in (left, right)
            ):
                self.append(id, "decision", decision="CANCEL")
            elif not left:
                self.action(source, body, "prepare", "source")
            elif not right:
                self.action(destination, body, "prepare", "destination")
            else:
                self.append(id, "decision", decision="COMMIT")
        elif value["decision"] == "COMMIT":
            if not left or left["status"] != "CONFIRMED":
                self.action(source, body, "confirm", "source")
            elif not right or right["status"] != "CONFIRMED":
                self.action(destination, body, "confirm", "destination")
            else:
                self.append(id, "completed", status="SUCCEEDED")
        else:
            if not left or left["status"] != "CANCELED":
                self.action(source, body, "cancel", "source")
            elif not right or right["status"] != "CANCELED":
                self.action(destination, body, "cancel", "destination")
            else:
                self.append(id, "completed", status="CANCELED")

    def step(self):
        with self.lock:
            self.journal.sync()  # No quorum => no participant action, even for a prior decision.
            with self.journal.connection() as db:
                retry = {
                    row["id"]: dict(row) for row in db.execute("SELECT * FROM retries")
                }
            for id, value in self.state().items():
                row = retry.get(id, {"dead": 0, "due": 0, "failures": 0})
                if (
                    value["status"] != "PENDING"
                    or row["dead"]
                    or row["due"] > self.clock()
                ):
                    continue
                try:
                    self.advance(id, value)
                    with self.journal.transaction() as db:
                        db.execute("DELETE FROM retries WHERE id=?", (id,))
                    return {"progress": id}
                except (Error, OSError, TimeoutError) as exc:
                    # Definitive pre-decision participant rejection selects CANCEL.
                    # A transport timeout can have committed: leave intent unresolved.
                    if (
                        isinstance(exc, Error)
                        and exc.status in (400, 404, 422)
                        and value["decision"] is None
                        and self.indices(value["intent"])[0]
                        != self.indices(value["intent"])[1]
                    ):
                        self.append(id, "decision", decision="CANCEL")
                        return {"progress": id}
                    failures = row["failures"] + 1
                    with self.journal.transaction() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO retries VALUES(?,?,?,?,?)",
                            (
                                id,
                                failures,
                                self.clock() + min(10, 0.1 * 2**failures),
                                int(failures >= 5),
                                str(exc)[:300],
                            ),
                        )
                    return {
                        "error": str(exc),
                        "transaction_id": id,
                        "dead": failures >= 5,
                    }
            return {"idle": True}

    def cancel(self, id):
        with self.lock:
            self.journal.sync()
            value = self.state().get(id)
            if not value:
                raise Error(404, "transfer missing")
            if (
                value["status"] != "PENDING"
                or value["decision"] == "COMMIT"
                or self.indices(value["intent"])[0] == self.indices(value["intent"])[1]
            ):
                raise Error(
                    409, "cannot cancel a committed, terminal or local transfer"
                )
            if value["decision"] is None:
                self.append(id, "decision", decision="CANCEL")
            self.retry(id)
            return self.state()[id]

    def retry(self, id):
        with self.lock:
            if id not in self.state():
                raise Error(404, "transfer missing")
            with self.journal.transaction() as db:
                db.execute("DELETE FROM retries WHERE id=?", (id,))
            return {"retry": id}

    def balance(self, account, owner, at=None):
        if (
            account not in self.config["accounts"]
            or self.config["accounts"][account]["owner"] != owner
        ):
            raise Error(404, "account missing")
        with self.lock:
            suffix = "" if at is None else "?at=" + str(at)
            result = self.shard(
                shard_for(account, len(self.config["shards"])),
                "/internal/view" + suffix,
            )
            return {
                "account": account,
                "currency": "USD",
                "shard": result["shard"],
                "sequence": result["seq"],
                **result["accounts"][account],
            }

    def status(self):
        with self.lock, self.journal.connection() as db:
            state = self.state()
            return {
                "transfers": len(state),
                "pending": sum(
                    value["status"] == "PENDING" for value in state.values()
                ),
                "events": len(self.journal.records()),
                "retries": [dict(row) for row in db.execute("SELECT * FROM retries")],
                "fixed_leader": self.journal.credentials,
            }

    def audit(self):
        with self.lock:
            quorum = self.journal.sync()
            views = [
                self.shard(i, "/internal/view")
                for i in range(len(self.config["shards"]))
            ]
            checks = [self.shard(i, "/internal/audit") for i in range(len(views))]
            total = sum(
                balance["available"] + balance["held"]
                for view in views
                for balance in view["accounts"].values()
            )
            transit = 0
            errors = []
            commands = self.state()
            for id, value in commands.items():
                source, destination = self.indices(value["intent"])
                if source == destination:
                    part = views[source]["parts"].get(id)
                    if value["status"] == "SUCCEEDED" and (
                        not part or part["status"] != "CONFIRMED"
                    ):
                        errors.append("missing local effect: " + id)
                    continue
                left = views[source]["parts"].get(id, {"status": "ABSENT"})["status"]
                right = views[destination]["parts"].get(id, {"status": "ABSENT"})[
                    "status"
                ]
                if left == "CONFIRMED" and right != "CONFIRMED":
                    if value["decision"] != "COMMIT":
                        errors.append("uncommitted source confirmation: " + id)
                    transit += value["intent"]["amount"]
                if right == "CONFIRMED" and (
                    left != "CONFIRMED" or value["decision"] != "COMMIT"
                ):
                    errors.append(
                        "destination credited before source confirmation: " + id
                    )
                if value["status"] == "SUCCEEDED" and (left, right) != (
                    "CONFIRMED",
                    "CONFIRMED",
                ):
                    errors.append("missing completed effect: " + id)
                if value["status"] == "CANCELED" and (left, right) != (
                    "CANCELED",
                    "CANCELED",
                ):
                    errors.append("missing cancellation: " + id)
            for view in views:
                for id, part in view["parts"].items():
                    if id not in commands or commands[id]["intent"] != part["intent"]:
                        errors.append("orphan or mismatched participant: " + id)
            expected = sum(row["opening"] for row in self.config["accounts"].values())
            if total + transit != expected:
                errors.append("supply mismatch")
            if not all(value["matches"] for value in checks):
                errors.append("event/projection mismatch")
            return {
                "matches": not errors,
                "errors": errors,
                "wallet_total": total,
                "in_transit": transit,
                "opening_supply": expected,
                "shards": checks,
                "quorum": quorum,
            }
