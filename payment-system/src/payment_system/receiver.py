from .common import Database,Error,digest,dumps


class Receiver(Database):
    """Local accounting-consumer test double with durable event deduplication."""
    def __init__(self,path):
        super().__init__(path)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,hash TEXT,payload TEXT,attempts INTEGER);
        CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),lose_ack INTEGER);
        INSERT OR IGNORE INTO control VALUES(1,0);
        """)

    def receive(self,body):
        id=body["event_id"]
        with self.transaction(True) as db:
            row=db.execute("SELECT * FROM events WHERE id=?",(id,)).fetchone()
            if row:
                if row["hash"]!=digest(body):
                    raise Error(409,"internal event idempotency conflict")
                db.execute("UPDATE events SET attempts=attempts+1 WHERE id=?",(id,))
            else:
                db.execute("INSERT INTO events VALUES(?,?,?,1)",(id,digest(body),dumps(body)))
            lost=db.execute("SELECT lose_ack FROM control").fetchone()[0]
        if lost:
            raise Error(503,"simulated internal consumer ACK lost after commit")
        return {"received":id}

    def control(self,body):
        if not isinstance(body,dict) or set(body)!={"lose_ack"} or type(body["lose_ack"]) is not bool:
            raise ValueError("control requires boolean lose_ack")
        with self.transaction(True) as db:
            db.execute("UPDATE control SET lose_ack=?",(body["lose_ack"],))
        return {"updated":True}

    def status(self):
        with self.transaction() as db:
            return {"unique_events":db.execute("SELECT count(*) FROM events").fetchone()[0],
                    "delivery_attempts":db.execute("SELECT coalesce(sum(attempts),0) FROM events").fetchone()[0]}
