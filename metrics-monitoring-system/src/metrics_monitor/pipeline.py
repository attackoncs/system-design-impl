from contextlib import closing
import json
import time
from urllib.error import HTTPError
import uuid

from .common import Database,Error,digest,dumps,http_json,identifier,sample


class Collector(Database):
    def __init__(self,path,max_bytes=16*1024*1024,clock=time.time):
        super().__init__(path)
        self.max_bytes,self.clock=max_bytes,clock
        with closing(self.connect()) as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,hash TEXT,response TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,payload TEXT,status TEXT,attempts INTEGER,due REAL);
            CREATE TABLE IF NOT EXISTS scrapes(target TEXT PRIMARY KEY,updated REAL,error TEXT);
            """)
            db.commit()

    def enqueue(self,samples,request_id):
        identifier(request_id)
        if not isinstance(samples,list) or not 1<=len(samples)<=1000:
            raise ValueError("push requires 1..1000 samples")
        normalized=[sample(body) for body in samples]
        fingerprint=digest(normalized)
        chunks=[]
        current=[]
        for body in normalized:
            if len(dumps({"batch_id":"0"*64,"samples":[body]}).encode())>12000:
                raise ValueError("sample too large for queue transport")
            if len(current)>=100 or len(dumps({"batch_id":"0"*64,"samples":current+[body]}).encode())>12000:
                chunks.append(current)
                current=[]
            current.append(body)
        if current:
            chunks.append(current)
        payloads=[(digest([request_id,i]),dumps({"batch_id":digest([request_id,i]),"samples":chunk})) for i,chunk in enumerate(chunks)]
        with self.transaction(True) as db:
            replay=db.execute("SELECT * FROM receipts WHERE id=?",(request_id,)).fetchone()
            if replay:
                if replay["hash"]!=fingerprint:
                    raise Error(409,"push ID payload conflict")
                return json.loads(replay["response"])
            used=db.execute("SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM outbox WHERE status='pending'").fetchone()[0]
            if used+sum(len(payload.encode()) for _,payload in payloads)>self.max_bytes:
                raise Error(429,"durable collector backlog capacity exceeded")
            db.executemany("INSERT INTO outbox VALUES(?,?,'pending',0,?)",[(id,payload,self.clock()) for id,payload in payloads])
            result={"request_id":request_id,"queued_chunks":len(payloads),"samples":len(normalized),"stored_in_tsdb":False}
            db.execute("INSERT INTO receipts VALUES(?,?,?,?)",(request_id,fingerprint,dumps(result),self.clock()))
            db.execute("DELETE FROM receipts WHERE created<?",(self.clock()-14*86400,))
            db.execute("DELETE FROM outbox WHERE status='sent' AND due<?",(self.clock()-14*86400,))
            return result

    def flush_once(self,queue):
        with self.transaction() as db:
            item=db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
        if not item or item["due"]>self.clock():
            return False
        try:
            queue.publish(item["payload"])
        except Exception:
            with self.transaction(True) as db:
                db.execute("UPDATE outbox SET attempts=attempts+1,due=? WHERE id=?",(self.clock()+min(60,2**min(item["attempts"],6)),item["id"]))
            return False
        with self.transaction(True) as db:
            db.execute("UPDATE outbox SET status='sent',payload=NULL,due=? WHERE id=?",(self.clock(),item["id"]))
        return True

    def scrape(self,target):
        url=target["url"]
        instance=identifier(target["instance"])
        try:
            result=http_json(url,token=target.get("token"))
            samples=result["samples"]
            if not isinstance(samples,list) or not 1<=len(samples)<=1000:
                raise ValueError("exporter sample list required")
            # Exporter-provided labels may not impersonate the discovered instance.
            samples=[sample({**body,"labels":{**body.get("labels",{}),"instance":instance}}) for body in samples]
            error=""
            up=1
        except Exception as exc:
            samples=[]
            error=type(exc).__name__
            up=0
        samples.append({"metric":"collector_up","labels":{"instance":instance},"type":"gauge","timestamp":self.clock(),"value":up})
        result=self.enqueue(samples,uuid.uuid4().hex)
        with self.transaction(True) as db:
            db.execute("INSERT INTO scrapes VALUES(?,?,?) ON CONFLICT(target) DO UPDATE SET updated=excluded.updated,error=excluded.error",(instance,self.clock(),error))
        return result

    def health(self):
        with self.transaction() as db:
            return {"pending_chunks":db.execute("SELECT count(*) FROM outbox WHERE status='pending'").fetchone()[0],"pending_bytes":db.execute("SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM outbox WHERE status='pending'").fetchone()[0],"scrapes":[dict(row) for row in db.execute("SELECT * FROM scrapes")]}


class QueueAdapter:
    def __init__(self,url,token,topic="metrics"):
        self.url,self.token,self.topic=url.rstrip("/"),token,topic
        self.producer=None
        self.sequence=0
        self.member=None
        self.member_id="storage_"+uuid.uuid4().hex

    def call(self,path,**body):
        return http_json(self.url+path,"POST",body,self.token,timeout=15)

    def ensure(self):
        try:
            self.call("/topic",topic=self.topic)
        except HTTPError as exc:
            if exc.code!=409:
                raise

    def publish(self,payload):
        if self.producer is None:
            self.ensure()
            self.producer=self.call("/producer",producer="metrics_"+uuid.uuid4().hex)
        result=self.call("/produce",topic=self.topic,partition=0,**self.producer,sequence=self.sequence,messages=[{"value":payload}],acks="all")
        if not result["committed"]:
            raise RuntimeError("queue did not confirm commit")
        self.sequence+=1
        return result

    def fetch(self):
        if self.member is None:
            self.ensure()
            self.member=self.call("/join",topic=self.topic,group="metrics-storage",member=self.member_id)
        else:
            self.member=self.call("/heartbeat",topic=self.topic,group="metrics-storage",member=self.member_id,session=self.member["session"])
        source={"topic":self.topic,"group":"metrics-storage","member":self.member_id,"session":self.member["session"],"generation":self.member["generation"],"partition":0}
        if 0 not in self.member["assignments"]:
            return source,{"records":[]}
        return source,self.call("/group-fetch",**source,limit=10)


def consume_once(store,queue):
    source,batch=queue.fetch()
    if not batch["records"]:
        return False
    for record in batch["records"]:
        payload=record["value"]
        batch_id=digest(payload)
        try:
            body=json.loads(payload)
            batch_id=body["batch_id"]
            store.ingest(batch_id,body["samples"])
        except (ValueError,TypeError,KeyError,Error) as exc:
            store.reject(digest(str(batch_id)),payload,str(exc))
    queue.call("/commit",**source,next_offset=batch["next_offset"])
    return True


def storage_loop(store,queue,stop):
    last_compact=0
    while not stop.is_set():
        error=""
        try:
            worked=consume_once(store,queue)
            if time.monotonic()-last_compact>=30:
                store.compact()
                last_compact=time.monotonic()
        except Exception as exc:
            worked=False
            error=type(exc).__name__
            if isinstance(exc,HTTPError) and exc.code==409:
                queue.member=None
        with store.transaction(True) as db:
            db.execute("INSERT INTO workers VALUES('storage',?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,error=excluded.error",(store.clock(),error))
        if not worked:
            stop.wait(0.2)
