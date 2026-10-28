import threading
import copy
import math
import time

from .common import Error, request, shard_for
from .journal import Journal
from .contracts import freeze, intent, participant
from .runtime import Runtime
from .audit import AuditMixin


from .coordinator_projection import project as project


class Coordinator(AuditMixin):
    def __init__(self, path, config, clock=time.time):
        self.config, self.clock = freeze(config), clock
        self.runtime = Runtime()
        self.journal = Journal(path, self.config)
        self.cache_lock = threading.Lock()
        self.cached, self.cached_seq = {}, 0
        self.changed = threading.Condition()
        self.wait_slots = threading.BoundedSemaphore(8)
        self.cursor = None
        self.journal.initialize(
            "CREATE TABLE IF NOT EXISTS retries(id TEXT PRIMARY KEY,failures INTEGER,due REAL,dead INTEGER,error TEXT);"
        )

    def state(self):
        with self.cache_lock:
            rows = self.journal.records(after=self.cached_seq)
            if rows:
                self.cached = project(rows, self.cached)
                self.cached_seq = rows[-1]["seq"]
            return copy.deepcopy(self.cached)

    def sync(self):
        return self.journal.sync(**self.runtime.context())

    def close(self):
        self.runtime.closing.set()
        with self.changed:
            self.changed.notify_all()
        self.journal.close()

    def wait_owned(self, id, owner, wait_ms=0):
        from .common import integer

        integer(wait_ms, 0, 2000)
        if not wait_ms:
            return self.owned(id, owner)
        if not self.wait_slots.acquire(False):
            raise Error(503, "completion waiter capacity exhausted")
        context = self.runtime.context()
        until = min(context["deadline"], time.monotonic() + wait_ms / 1000)
        try:
            with self.changed:
                while True:
                    self.runtime.live(**context)
                    value = self.owned(id, owner)
                    if value["status"] != "PENDING" or time.monotonic() >= until:
                        return value
                    # Notifications reduce latency; periodic checks recover a
                    # commit whose HTTP caller disconnected before notification.
                    self.changed.wait(min(0.25, max(0, until - time.monotonic())))
        finally:
            self.wait_slots.release()

    def create(self, body, owner):
        body = intent(body)
        accounts = self.config["accounts"]
        if (
            body["from_account"] not in accounts
            or accounts[body["from_account"]]["owner"] != owner
        ):
            raise Error(403, "only owned source accounts may transfer")
        if body["to_account"] not in accounts:
            raise Error(404, "destination account missing")
        with self.runtime.mutation():
            self.sync()
            old = self.state().get(body["transaction_id"])
            if old:
                if old["intent"] != body:
                    raise Error(409, "transaction identity content conflict")
                return old
            if len(self.state()) >= 1000:
                raise Error(
                    429, "reference command history is limited to 1000 transfers"
                )
            self.append(body["transaction_id"], "accepted", intent=body)
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
            timeout=0.75,
            **self.runtime.context(),
        )

    def indices(self, body):
        return tuple(
            shard_for(body[key], len(self.config["shards"]))
            for key in ("from_account", "to_account")
        )

    def action(self, index, body, kind, role):
        # The shard also checks the persisted epoch inside its monetary transaction.
        result = self.shard(
            index,
            "/internal/action",
            {**self.journal.credentials, "intent": body, "kind": kind, "role": role},
        )
        if result is None:
            raise Error(502, "participant action returned no receipt")
        return self.check_part(result, body, role)

    @staticmethod
    def check_part(result, body, role):
        try:
            return participant(result, body, role)
        except (ValueError, TypeError, KeyError):
            raise Error(502, "participant returned a conflicting receipt") from None

    def read_part(self, index, body, role):
        result = self.shard(index, "/internal/participants/" + body["transaction_id"])
        if not isinstance(result, dict) or set(result) != {"participant"}:
            raise Error(502, "invalid participant response")
        return self.check_part(result["participant"], body, role)

    def append(self, id, kind, **fields):
        self.journal.append(
            {"kind": kind, "transaction_id": id, **fields}, **self.runtime.context()
        )
        with self.changed:
            self.changed.notify_all()

    def advance(self, id, value):
        body = value["intent"]
        source, destination = self.indices(body)
        left = self.read_part(
            source, body, "local" if source == destination else "source"
        )
        if source == destination:
            part = left or self.action(source, body, "local", "local")
            self.append(
                id,
                "completed",
                status="SUCCEEDED" if part["status"] == "CONFIRMED" else "REJECTED",
            )
            return
        right = self.read_part(destination, body, "destination")
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
            elif left["status"] == right["status"] == "PREPARED":
                self.append(id, "decision", decision="COMMIT")
            else:
                raise Error(
                    502, "participant confirmed without durable coordinator decision"
                )
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
        with self.runtime.mutation():
            state = self.state()
            if not self.journal.has_pending() and not any(
                value["status"] == "PENDING" for value in state.values()
            ):
                return {"idle": True}
            # No quorum => no participant action, even for a prior decision.
            self.sync()
            now = self.clock()
            if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
                raise ValueError("finite nonnegative retry clock required")
            with self.journal.connection() as db:
                retry = {
                    row["id"]: dict(row) for row in db.execute("SELECT * FROM retries")
                }
            commands = list(self.state().items())
            ids = [key for key, _ in commands]
            offset = ids.index(self.cursor) + 1 if self.cursor in ids else 0
            commands = commands[offset:] + commands[:offset]
            for id, value in commands:
                row = retry.get(id, {"dead": 0, "due": 0, "failures": 0})
                if value["status"] != "PENDING" or row["dead"] or row["due"] > now:
                    continue
                self.cursor = id
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
                                now + min(10, 0.1 * 2 ** min(failures, 10)),
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
        with self.runtime.mutation():
            self.sync()
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
        with self.runtime.mutation():
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
        suffix = "" if at is None else "?at=" + str(at)
        result = self.shard(
            shard_for(account, len(self.config["shards"])),
            "/internal/accounts/" + account + suffix,
        )
        try:
            if (
                not isinstance(result, dict)
                or set(result)
                != {"account", "currency", "shard", "sequence", "available", "held"}
                or result["account"] != account
                or result["currency"] != "USD"
                or result["shard"] != shard_for(account, len(self.config["shards"]))
            ):
                raise ValueError("invalid balance receipt")
            from .common import integer

            integer(result["shard"], 0, len(self.config["shards"]) - 1)
            integer(result["sequence"], 1)
            integer(result["available"])
            integer(result["held"])
        except (ValueError, TypeError, KeyError):
            raise Error(502, "participant returned an invalid balance") from None
        return result

    def status(self):
        with self.runtime.mutation(), self.journal.connection() as db:
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
