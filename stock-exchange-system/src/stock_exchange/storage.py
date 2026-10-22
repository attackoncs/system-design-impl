import json
import os
from pathlib import Path

from .common import Database,Error,checksum,encode,entry,integer,verify
from .matching import audit,book,reduce_command,validate


class Log(Database):
    def __init__(self,path):
        super().__init__(path)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,body TEXT);
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable journal'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable journal'); END;
        """)
        with self.connection() as db:
            verify(self.rows(db))

    def rows(self,db):
        return [json.loads(row[0]) for row in db.execute("SELECT body FROM events ORDER BY seq")]


class Sequencer(Log):
    def __init__(self,path,settings,timestamp):
        super().__init__(path)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS requests(owner TEXT,id TEXT,hash TEXT,seq INTEGER,PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER,active TEXT,timestamp INTEGER);
        """)
        with self.transaction() as db:
            rows=self.rows(db)
            genesis={"kind":"genesis","settings":settings}
            if not rows:
                db.execute("INSERT INTO events VALUES(1,?)",(encode(entry(1,"0"*64,genesis)),))
            elif rows[0]["event"]!=genesis:
                raise ValueError("opening accounts/rules changed; refusing non-deterministic replay")
            db.execute("INSERT OR IGNORE INTO control VALUES(1,1,'primary',?)",(integer(timestamp,1),))

    def submit(self,owner,kind,body):
        validate(body,kind)
        fingerprint=checksum([kind,body])
        with self.transaction() as db:
            old=db.execute("SELECT * FROM requests WHERE owner=? AND id=?",(owner,body["request_id"])).fetchone()
            if old:
                if old["hash"]!=fingerprint:
                    raise Error(409,"request identity content conflict")
                return {"seq":old["seq"],"request_id":body["request_id"],"status":"ACCEPTED","duplicate":True}
            rows=self.rows(db)
            if len(rows)>=1001:
                raise Error(429,"reference limit of 1000 commands reached")
            seq=len(rows)+1
            stamp=db.execute("SELECT timestamp FROM control").fetchone()[0]
            command={"seq":seq,"timestamp":stamp,"owner":owner,"kind":kind,"body":body}
            row=entry(seq,rows[-1]["hash"],command)
            db.execute("INSERT INTO events VALUES(?,?)",(seq,encode(row)))
            db.execute("INSERT INTO requests VALUES(?,?,?,?)",(owner,body["request_id"],fingerprint,seq))
        return {"seq":seq,"request_id":body["request_id"],"status":"ACCEPTED","duplicate":False}

    def lookup(self,owner,id):
        with self.connection() as db:
            row=db.execute("SELECT seq FROM requests WHERE owner=? AND id=?",(owner,id)).fetchone()
            if not row:
                raise Error(404,"owned request missing")
            return row[0]

    def control(self):
        with self.connection() as db:
            row=dict(db.execute("SELECT * FROM control").fetchone())
            rows=self.rows(db)
            return {"epoch":row["epoch"],"active":row["active"],"timestamp":row["timestamp"],"seq":len(rows),"head":rows[-1]["hash"]}

    def clock(self,timestamp):
        integer(timestamp,1)
        with self.transaction() as db:
            if timestamp<db.execute("SELECT timestamp FROM control").fetchone()[0]:
                raise Error(409,"simulation clock cannot move backwards")
            db.execute("UPDATE control SET timestamp=?",(timestamp,))
        return self.control()

    def promote(self,node,replica):
        if node not in ("primary","backup"):
            raise ValueError("unknown matching replica")
        with self.transaction() as db:
            rows=self.rows(db)
            if replica["seq"]!=len(rows) or replica["head"]!=rows[-1]["hash"]:
                raise Error(409,"promotion requires complete sequencer prefix")
            current=db.execute("SELECT active FROM control").fetchone()[0]
            if node!=current:
                db.execute("UPDATE control SET epoch=epoch+1,active=?",(node,))
        return self.control()


def market_batch(previous,state,row):
    symbols=state["settings"]["symbols"]
    updates={}
    for symbol in symbols:
        before=book(previous,symbol) if previous else {"bids":{},"asks":{}}
        after=book(state,symbol)
        sides={side:{price:after[side].get(price,0) for price in set(before[side])|set(after[side]) if before[side].get(price,0)!=after[side].get(price,0)} for side in ("bids","asks")}
        if any(sides.values()):
            updates[symbol]=sides
    return {"seq":row["seq"],"previous":row["previous"],"head":row["hash"],"books":updates,
            "orders":[value for key,value in state["orders"].items() if not previous or previous["orders"].get(key)!=value],
            "trades":state["trades"][len(previous["trades"]) if previous else 0:]}


class Engine(Log):
    def __init__(self,path,retention=16):
        super().__init__(path)
        self.retention=integer(retention,2,1000)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS projection(id INTEGER PRIMARY KEY CHECK(id=1),state TEXT);
        CREATE TABLE IF NOT EXISTS batches(seq INTEGER PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS fence(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER);
        INSERT OR IGNORE INTO fence VALUES(1,0);
        """)
        self.rebuild()

    def replay(self,rows):
        verify(rows)
        state={}
        for row in rows:
            state=reduce_command(state,row["event"])
        return state

    def rebuild(self):
        with self.transaction() as db:
            rows=self.rows(db)
            state=self.replay(rows)
            path=Path(str(self.path)+".snapshot.json")
            if path.exists():
                value=json.loads(path.read_text())
                payload=value["payload"]
                seq=integer(payload["seq"],1,len(rows))
                if value["checksum"]!=checksum(payload) or payload["head"]!=rows[seq-1]["hash"] or payload["state"]!=self.replay(rows[:seq]):
                    raise ValueError("snapshot checksum or replay mismatch")
                state=payload["state"]
                for row in rows[seq:]:
                    state=reduce_command(state,row["event"])
            db.execute("INSERT OR REPLACE INTO projection VALUES(1,?)",(encode(state),))

    def apply(self,row):
        if row["event"]["kind"]!="genesis" and row["event"]["seq"]!=row["seq"]:
            raise ValueError("command and journal sequence disagree")
        with self.transaction() as db:
            rows=self.rows(db)
            seq=row["seq"]
            if seq<=len(rows):
                if rows[seq-1]!=row:
                    raise Error(409,"matching replica prefix conflict")
                return
            if row!=entry(len(rows)+1,rows[-1]["hash"] if rows else "0"*64,row["event"]):
                raise Error(409,"command sequence/checksum gap")
            state=json.loads(db.execute("SELECT state FROM projection").fetchone()[0])
            updated=reduce_command(state,row["event"])
            batch=market_batch(state,updated,row)
            db.execute("INSERT INTO events VALUES(?,?)",(seq,encode(row)))
            db.execute("INSERT OR REPLACE INTO projection VALUES(1,?)",(encode(updated),))
            db.execute("INSERT INTO batches VALUES(?,?)",(seq,encode(batch)))
            db.execute("DELETE FROM batches WHERE seq<=?",(seq-self.retention,))

    def raw(self):
        with self.transaction() as db:
            rows=self.rows(db)
            state=json.loads(db.execute("SELECT state FROM projection").fetchone()[0])
            return {"seq":len(rows),"head":rows[-1]["hash"] if rows else "0"*64,"state":state,"state_hash":checksum(state)}

    def fence(self,epoch):
        with self.transaction() as db:
            previous=db.execute("SELECT epoch FROM fence").fetchone()[0]
            if epoch<previous:
                raise Error(409,"sequencer epoch regressed; recovery refused")
            db.execute("UPDATE fence SET epoch=?",(epoch,))

    def feed(self,after):
        integer(after)
        with self.transaction() as db:
            oldest=db.execute("SELECT min(seq) FROM batches").fetchone()[0]
            if oldest is not None and after<oldest-1:
                raise Error(410,"market delta gap; fetch a new snapshot")
            return {"batches":[json.loads(row[0]) for row in db.execute("SELECT body FROM batches WHERE seq>? ORDER BY seq",(after,))]}

    def snapshot(self):
        view=self.raw()
        if not view["seq"]:
            raise Error(503,"engine has not loaded genesis")
        payload={key:view[key] for key in ("seq","head","state")}
        path=Path(str(self.path)+".snapshot.json")
        temporary=path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            stream.write(encode({"payload":payload,"checksum":checksum(payload)}))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        return {"seq":view["seq"],"state_hash":view["state_hash"]}

    def audit(self):
        view=self.raw()
        with self.transaction() as db:
            replay=self.replay(self.rows(db))
        if not replay:
            raise Error(503,"engine not ready")
        return {"seq":view["seq"],"projection_matches":replay==view["state"],**audit(replay)}
