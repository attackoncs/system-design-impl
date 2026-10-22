import copy
import json
from pathlib import Path

from .common import Error,Fence,checksum,encode,entry,identifier,integer,shard_for,verify


def intent(body):
    if not isinstance(body,dict) or set(body)!={"transaction_id","from_account","to_account","amount","currency"}:
        raise ValueError("exact transfer fields required")
    for field in ("transaction_id","from_account","to_account"):
        identifier(body[field])
    integer(body["amount"],1,100_000_000)
    if body["currency"]!="USD" or body["from_account"]==body["to_account"]:
        raise ValueError("USD transfer between distinct accounts required")
    return body


def reduce_event(state,event):
    """Pure deterministic projection: replay never invokes another service."""
    if event["kind"]=="genesis":
        if state:
            raise ValueError("duplicate genesis")
        return {"accounts":{key:{"available":integer(value),"held":0} for key,value in event["accounts"].items()},"parts":{}}
    state=copy.deepcopy(state)
    body=intent(event["intent"])
    id=body["transaction_id"]
    source,destination,amount=body["from_account"],body["to_account"],body["amount"]
    old=state["parts"].get(id)
    if old and old["intent"]!=body:
        raise ValueError("participant identity conflict")
    kind,role=event["kind"],event["role"]
    accounts=state["accounts"]
    if kind=="local":
        if old:
            raise ValueError("duplicate local event")
        valid=source in accounts and destination in accounts and accounts[source]["available"]>=amount
        status="CONFIRMED" if valid else "REJECTED"
        if valid:
            accounts[source]["available"]-=amount
            accounts[destination]["available"]+=amount
    elif kind=="prepare":
        if old:
            raise ValueError("duplicate prepare event")
        account=source if role=="source" else destination
        valid=account in accounts and (role=="destination" or accounts[account]["available"]>=amount)
        status="PREPARED" if valid else "REJECTED"
        if valid and role=="source":
            accounts[source]["available"]-=amount
            accounts[source]["held"]+=amount
    elif kind=="confirm":
        if not old or old["status"]!="PREPARED":
            raise ValueError("confirm requires durable prepare")
        if role=="source":
            accounts[source]["held"]-=amount
        else:
            accounts[destination]["available"]+=amount
        status="CONFIRMED"
    elif kind=="cancel":
        if old and old["status"]=="CONFIRMED":
            raise ValueError("confirmed participant cannot cancel")
        if old and old["status"]=="PREPARED" and role=="source":
            accounts[source]["held"]-=amount
            accounts[source]["available"]+=amount
        status="CANCELED"
    else:
        raise ValueError("unsupported shard event")
    if any(min(row.values())<0 for row in accounts.values()):
        raise ValueError("negative projection")
    state["parts"][id]={"intent":body,"role":role,"status":status}
    return state


class Shard(Fence):
    def __init__(self,path,index,count,accounts,epoch=1,leader="coordinator"):
        super().__init__(path)
        self.index,self.count=index,count
        self.init_fence(epoch,leader)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,body TEXT);
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
        CREATE TABLE IF NOT EXISTS projection(id INTEGER PRIMARY KEY CHECK(id=1),state TEXT);
        """)
        opening={key:integer(value) for key,value in accounts.items() if shard_for(key,count)==index}
        with self.transaction() as db:
            rows=self.rows(db)
            if not rows:
                self.append(db,{"kind":"genesis","accounts":opening},{})
            elif rows[0]["event"]!={"kind":"genesis","accounts":opening}:
                raise ValueError("opening configuration changed; refusing to mint new funds")
        self.rebuild()

    def rows(self,db):
        return [json.loads(row[0]) for row in db.execute("SELECT body FROM events ORDER BY seq")]

    def replay(self,rows):
        verify(rows)
        state={}
        for row in rows:
            state=reduce_event(state,row["event"])
        return state

    def append(self,db,event,state):
        previous=db.execute("SELECT body FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        tail=json.loads(previous[0]) if previous else {"seq":0,"hash":"0"*64}
        row=entry(tail["seq"]+1,tail["hash"],event)
        updated=reduce_event(state,event)
        db.execute("INSERT INTO events VALUES(?,?)",(row["seq"],encode(row)))
        db.execute("INSERT OR REPLACE INTO projection VALUES(1,?)",(encode(updated),))
        return updated

    def rebuild(self):
        with self.transaction() as db:
            rows=self.rows(db)
            state=self.replay(rows)
            snapshot=Path(str(self.path)+".snapshot.json")
            if snapshot.exists():
                value=json.loads(snapshot.read_text())
                payload=value["payload"]
                seq=integer(payload["seq"],1,len(rows))
                if checksum(payload)!=value["checksum"] or payload["head"]!=rows[seq-1]["hash"] or payload["state"]!=self.replay(rows[:seq]):
                    raise ValueError("snapshot checksum or projection mismatch")
                state=payload["state"]
                for row in rows[seq:]:
                    state=reduce_event(state,row["event"])
            db.execute("INSERT OR REPLACE INTO projection VALUES(1,?)",(encode(state),))
            return state

    def action(self,body,kind,role,epoch,leader):
        intent(body)
        if role not in ("source","destination","local") or kind not in ("local","prepare","confirm","cancel") or (kind=="local")!=(role=="local"):
            raise ValueError("invalid participant action")
        account=body["from_account"] if role!="destination" else body["to_account"]
        if shard_for(account,self.count)!=self.index or (role=="local" and shard_for(body["to_account"],self.count)!=self.index):
            raise Error(409,"wrong shard")
        with self.transaction() as db:
            self.check_fence(db,epoch,leader)
            state=json.loads(db.execute("SELECT state FROM projection").fetchone()[0])
            old=state["parts"].get(body["transaction_id"])
            if old:
                if old["intent"]!=body or old["role"]!=role:
                    raise Error(409,"participant command conflict")
                if kind in ("prepare","local") or (kind=="confirm" and old["status"]=="CONFIRMED") or (kind=="cancel" and old["status"]=="CANCELED"):
                    return old
                if kind=="confirm" and old["status"]!="PREPARED" or kind=="cancel" and old["status"]=="CONFIRMED":
                    raise Error(409,"participant terminal conflict")
            elif kind=="confirm":
                raise Error(409,"confirm without prepare")
            state=self.append(db,{"kind":kind,"role":role,"intent":body},state)
            return state["parts"][body["transaction_id"]]

    def view(self,at=None):
        with self.connection() as db:
            rows=self.rows(db)
            state=json.loads(db.execute("SELECT state FROM projection").fetchone()[0]) if at is None else self.replay(rows[:integer(at,1,len(rows))])
            return {"shard":self.index,"seq":rows[-1]["seq"] if at is None else at,"head":rows[-1]["hash"] if at is None else rows[at-1]["hash"],**state}

    def snapshot(self):
        with self.connection() as db:
            rows=self.rows(db)
            state=self.replay(rows)
            payload={"seq":len(rows),"head":rows[-1]["hash"],"state":state}
        path=Path(str(self.path)+".snapshot.json")
        temporary=path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            stream.write(encode({"payload":payload,"checksum":checksum(payload)}))
            stream.flush()
            import os
            os.fsync(stream.fileno())
        temporary.replace(path)
        return {"seq":payload["seq"],"head":payload["head"]}

    def audit(self):
        with self.connection() as db:
            replayed=self.replay(self.rows(db))
            projected=json.loads(db.execute("SELECT state FROM projection").fetchone()[0])
            return {"matches":replayed==projected,"events":db.execute("SELECT count(*) FROM events").fetchone()[0]}
