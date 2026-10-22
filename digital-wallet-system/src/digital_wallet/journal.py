import json

from .common import Database,Error,Fence,encode,entry,request,verify


class Replica(Fence):
    def __init__(self,path,epoch=1,leader="coordinator"):
        super().__init__(path)
        self.init_fence(epoch,leader)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS entries(seq INTEGER PRIMARY KEY,body TEXT);
        CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries BEGIN SELECT RAISE(ABORT,'immutable journal replica'); END;
        CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT,'immutable journal replica'); END;
        """)
        with self.connection() as db:
            verify(self.rows(db))

    def rows(self,db):
        return [json.loads(row[0]) for row in db.execute("SELECT body FROM entries ORDER BY seq")]

    def append(self,row,epoch,leader):
        with self.transaction() as db:
            self.check_fence(db,epoch,leader)
            rows=self.rows(db)
            seq=row["seq"]
            if type(seq) is not int or seq<1:
                raise ValueError("invalid sequence")
            if seq<=len(rows):
                if rows[seq-1]!=row:
                    raise Error(409,"log prefix conflict; operator recovery required")
            else:
                if seq!=len(rows)+1 or row!=entry(seq,rows[-1]["hash"] if rows else "0"*64,row["event"]):
                    raise Error(409,"log gap or checksum conflict")
                db.execute("INSERT INTO entries VALUES(?,?)",(seq,encode(row)))
        return {"accepted":seq,"hash":row["hash"]}

    def status(self,epoch=None,leader=None):
        with self.connection() as db:
            if epoch is not None:
                self.check_fence(db,epoch,leader)
            rows=self.rows(db)
            return {"count":len(rows),"head":rows[-1]["hash"] if rows else "0"*64}


class Journal(Database):
    """Fixed single writer: local durable intent plus 2-of-3 network replication.

    There is no election or alternate writer using a different authority disk.
    Retaining local pending intents is required for safe ambiguous-append recovery.
    """
    def __init__(self,path,config):
        super().__init__(path)
        self.config=config
        if len(config["replicas"])!=3 or len(set(config["replicas"]))!=3:
            raise ValueError("three distinct replica endpoints required")
        self.initialize("""
        CREATE TABLE IF NOT EXISTS entries(seq INTEGER PRIMARY KEY,body TEXT,committed INTEGER CHECK(committed IN(0,1)));
        CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE OF body,seq ON entries BEGIN SELECT RAISE(ABORT,'immutable journal intent'); END;
        CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT,'immutable journal intent'); END;
        """)
        with self.connection() as db:
            rows=[json.loads(row[0]) for row in db.execute("SELECT body FROM entries ORDER BY seq")]
            verify(rows)
            committed=[r[0] for r in db.execute("SELECT committed FROM entries ORDER BY seq")]
            if committed!=sorted(committed,reverse=True):
                raise ValueError("committed log must be a prefix")

    @property
    def credentials(self):
        return {"epoch":self.config["epoch"],"leader":self.config["leader"]}

    def records(self,committed=True):
        with self.connection() as db:
            sql="SELECT body FROM entries"+(" WHERE committed=1" if committed else "")+" ORDER BY seq"
            return [json.loads(row[0]) for row in db.execute(sql)]

    def sync(self):
        rows=self.records(False)
        acknowledgements=0
        for url in self.config["replicas"]:
            try:
                remote=request(url+"/internal/status",self.config["internal_token"],self.credentials,timeout=.4)
                if remote["count"]>len(rows) or remote["head"]!=(rows[remote["count"]-1]["hash"] if remote["count"] else "0"*64):
                    raise Error(409,"replica conflicts with retained authority history")
                for row in rows[remote["count"]:]:
                    ack=request(url+"/internal/append",self.config["internal_token"],{**self.credentials,"entry":row},timeout=.4)
                    if ack!={"accepted":row["seq"],"hash":row["hash"]}:
                        raise Error(409,"replica acknowledged another entry")
                acknowledgements+=1
            except (Error,OSError,TimeoutError):
                continue
        if acknowledgements<2:
            raise Error(503,"journal quorum unavailable; retain original transaction identity")
        with self.transaction() as db:
            db.execute("UPDATE entries SET committed=1")
        return {"acks":acknowledgements,"events":len(rows)}

    def append(self,event):
        self.sync()  # Resolve a previous ambiguous append before adding another event.
        with self.transaction() as db:
            old=db.execute("SELECT body FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
            previous=json.loads(old[0]) if old else {"seq":0,"hash":"0"*64}
            row=entry(previous["seq"]+1,previous["hash"],event)
            db.execute("INSERT INTO entries VALUES(?,?,0)",(row["seq"],encode(row)))
        self.sync()
        return row
