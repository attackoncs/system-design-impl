from contextlib import closing,contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import zlib

from .common import Error,ZERO,dumps,envelope,integer,name,rpc


class Coordinator:
    def __init__(self,root,brokers,cluster_token,lease_seconds=10,clock=time.time):
        if not isinstance(brokers,dict) or not 1<=len(brokers)<=9:
            raise ValueError("1..9 brokers required")
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/"metadata.sqlite"
        self.brokers=dict(brokers)
        self.token,self.lease_seconds,self.clock=cluster_token,lease_seconds,clock
        self.lock=threading.RLock()
        with closing(self.connect()) as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS topics(name TEXT PRIMARY KEY,partitions INTEGER,retention REAL,max_bytes INTEGER,segment_bytes INTEGER);
            CREATE TABLE IF NOT EXISTS parts(topic TEXT,partition INTEGER,leader TEXT,epoch INTEGER,hwm INTEGER,floor INTEGER,base_hash TEXT,PRIMARY KEY(topic,partition));
            CREATE TABLE IF NOT EXISTS records(topic TEXT,partition INTEGER,offset INTEGER,hash TEXT,timestamp REAL,size INTEGER,PRIMARY KEY(topic,partition,offset));
            CREATE TABLE IF NOT EXISTS producers(id TEXT PRIMARY KEY,epoch INTEGER);
            CREATE TABLE IF NOT EXISTS sequences(producer TEXT,epoch INTEGER,topic TEXT,partition INTEGER,last INTEGER,PRIMARY KEY(producer,epoch,topic,partition));
            CREATE TABLE IF NOT EXISTS receipts(producer TEXT,epoch INTEGER,topic TEXT,partition INTEGER,sequence INTEGER,hash TEXT,response TEXT,last_offset INTEGER,PRIMARY KEY(producer,epoch,topic,partition,sequence));
            CREATE TABLE IF NOT EXISTS pending(topic TEXT,partition INTEGER,body TEXT,PRIMARY KEY(topic,partition));
            CREATE TABLE IF NOT EXISTS groups(topic TEXT,name TEXT,generation INTEGER,PRIMARY KEY(topic,name));
            CREATE TABLE IF NOT EXISTS members(topic TEXT,group_name TEXT,id TEXT,session TEXT,expires REAL,PRIMARY KEY(topic,group_name,id));
            CREATE TABLE IF NOT EXISTS cursors(topic TEXT,group_name TEXT,partition INTEGER,next INTEGER,PRIMARY KEY(topic,group_name,partition));
            CREATE TABLE IF NOT EXISTS delivered(topic TEXT,group_name TEXT,partition INTEGER,generation INTEGER,next INTEGER,PRIMARY KEY(topic,group_name,partition));
            CREATE TABLE IF NOT EXISTS transactions(id TEXT PRIMARY KEY,hash TEXT,response TEXT);
            """)
            # Incomplete transactional output stages never independently become visible.
            for row in db.execute("SELECT topic,partition,body FROM pending").fetchall():
                if json.loads(row["body"]).get("transaction"):
                    db.execute("DELETE FROM pending WHERE topic=? AND partition=?",(row["topic"],row["partition"]))
            db.commit()

    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def topic(self,db,topic):
        name(topic)
        row=db.execute("SELECT * FROM topics WHERE name=?",(topic,)).fetchone()
        if not row:
            raise Error(404,"topic not found")
        return dict(row)

    def part(self,db,topic,partition):
        self.topic(db,topic)
        integer(partition,0,63)
        row=db.execute("SELECT * FROM parts WHERE topic=? AND partition=?",(topic,partition)).fetchone()
        if not row:
            raise Error(404,"partition not found")
        return dict(row)

    def expected_hash(self,db,part):
        if part["hwm"]<part["floor"]:
            return part["base_hash"]
        return db.execute("SELECT hash FROM records WHERE topic=? AND partition=? AND offset=?",(part["topic"],part["partition"],part["hwm"])).fetchone()[0]

    def create(self,topic,partitions=1,retention_seconds=14*86400,max_bytes=8*1024*1024,segment_bytes=1024*1024):
        name(topic)
        integer(partitions,1,64)
        integer(max_bytes,1024,16*1024*1024)
        integer(segment_bytes,256,16*1024*1024)
        if type(retention_seconds) not in (int,float) or not math.isfinite(retention_seconds) or not 0<retention_seconds<=365*86400:
            raise ValueError("invalid retention")
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM topics WHERE name=?",(topic,)).fetchone():
                raise Error(409,"topic already exists")
            db.execute("INSERT INTO topics VALUES(?,?,?,?,?)",(topic,partitions,retention_seconds,max_bytes,segment_bytes))
            nodes=sorted(self.brokers)
            db.executemany("INSERT INTO parts VALUES(?,?,?,0,-1,0,?)",[(topic,p,nodes[p%len(nodes)],ZERO) for p in range(partitions)])
        return {"topic":topic,"partitions":partitions}

    def producer_open(self,producer):
        name(producer)
        with self.transaction() as db:
            row=db.execute("SELECT epoch FROM producers WHERE id=?",(producer,)).fetchone()
            epoch=row[0]+1 if row else 0
            db.execute("INSERT INTO producers VALUES(?,?) ON CONFLICT(id) DO UPDATE SET epoch=excluded.epoch",(producer,epoch))
            return {"producer":producer,"epoch":epoch}

    def call(self,node,path,body):
        return rpc(self.brokers[node],path,body,self.token,timeout=0.7)

    def _leader(self,part):
        with self.transaction() as db:
            part=self.part(db,part["topic"],part["partition"])
            expected=self.expected_hash(db,part)
            options=self.topic(db,part["topic"])
        common={"topic":part["topic"],"partition":part["partition"],"committed":part["hwm"],"segment_bytes":options["segment_bytes"]}
        candidates={}
        for node in [part["leader"],*[n for n in sorted(self.brokers) if n!=part["leader"]]]:
            try:
                state=self.call(node,"/status",common)
                if state["hash"]==expected:
                    candidates[node]=state
                    if node==part["leader"]:
                        return part,node,expected
            except (OSError,ValueError):
                pass
        if not candidates:
            raise Error(503,"no replica contains committed prefix")
        leader=sorted(candidates)[0]
        with self.transaction() as db:
            db.execute("UPDATE parts SET leader=?,epoch=epoch+1 WHERE topic=? AND partition=?",(leader,part["topic"],part["partition"]))
            part=self.part(db,part["topic"],part["partition"])
        return part,leader,expected

    def _retained(self,part,leader):
        values=[]
        offset=part["floor"]
        while offset<=part["hwm"]:
            batch=self.call(leader,"/read",{"topic":part["topic"],"partition":part["partition"],"offset":offset,"limit":min(500,part["hwm"]-offset+1)})["records"]
            if not batch:
                raise Error(503,"committed prefix unavailable during catchup")
            values.extend(batch)
            offset+=len(batch)
        return values

    def _replicate(self,pending,leader_only=False):
        part,leader,expected=self._leader(pending)
        with self.transaction() as db:
            options=self.topic(db,part["topic"])
        common={"topic":part["topic"],"partition":part["partition"],"epoch":part["epoch"],"committed":part["hwm"],"committed_hash":expected,"base":part["floor"],"base_hash":part["base_hash"],"segment_bytes":options["segment_bytes"],"records":pending["values"]}
        synced=[]
        snapshot=None
        for node in [leader,*[n for n in sorted(self.brokers) if n!=leader]]:
            try:
                state=self.call(node,"/status",common)
                if state["hash"]==expected:
                    result=self.call(node,"/sync",common)
                else:
                    if snapshot is None:
                        snapshot=self._retained(part,leader)
                    result=self.call(node,"/install",{**common,"records":snapshot+pending["values"]})
                if result["hash"]==pending["values"][-1]["hash"]:
                    synced.append(node)
                    if leader_only and node==leader:
                        return synced
            except (OSError,ValueError):
                pass
        if len(synced)<len(self.brokers)//2+1:
            raise Error(503,"quorum unavailable; outcome pending, retry identical sequence")
        if leader_only and leader not in synced:
            raise Error(503,"leader persistence not confirmed")
        return synced

    def _finalize(self,db,pending,synced):
        last=pending["values"][-1]["record"]["offset"]
        for value in pending["values"]:
            record=value["record"]
            db.execute("INSERT INTO records VALUES(?,?,?,?,?,?)",(pending["topic"],pending["partition"],record["offset"],value["hash"],record["timestamp"],len(dumps(value).encode())))
        db.execute("UPDATE parts SET hwm=? WHERE topic=? AND partition=?",(last,pending["topic"],pending["partition"]))
        response={"topic":pending["topic"],"partition":pending["partition"],"offsets":[v["record"]["offset"] for v in pending["values"]],"committed":True,"replicas":synced}
        if not pending.get("transaction"):
            args=(pending["producer"],pending["epoch"],pending["topic"],pending["partition"])
            db.execute("INSERT INTO sequences VALUES(?,?,?,?,?) ON CONFLICT(producer,epoch,topic,partition) DO UPDATE SET last=excluded.last",(*args,pending["sequence"]))
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)",(*args,pending["sequence"],pending["fingerprint"],dumps(response),last))
        db.execute("DELETE FROM pending WHERE topic=? AND partition=?",(pending["topic"],pending["partition"]))
        return response

    def _flush(self,topic,partition):
        with self.transaction() as db:
            row=db.execute("SELECT body FROM pending WHERE topic=? AND partition=?",(topic,partition)).fetchone()
        if not row:
            return None
        pending=json.loads(row[0])
        if pending.get("transaction"):
            raise Error(409,"partition reserved by unfinished transaction")
        synced=self._replicate(pending)
        with self.transaction() as db:
            return self._finalize(db,pending,synced)

    def _values(self,part,messages,producer,epoch,sequence):
        if not isinstance(messages,list) or not 1<=len(messages)<=100:
            raise ValueError("batch requires 1..100 messages")
        with self.transaction() as db:
            previous=self.expected_hash(db,part)
        values=[]
        for i,message in enumerate(messages):
            if not isinstance(message,dict) or not isinstance(message.get("value"),str) or len(message["value"].encode())>16384:
                raise ValueError("text messages must be at most 16 KiB")
            key=message.get("key")
            if key is not None and (not isinstance(key,str) or len(key)>1000):
                raise ValueError("invalid message key")
            delay=message.get("delay_seconds",0)
            if type(delay) not in (float,int) or not math.isfinite(delay) or not 0<=delay<=86400:
                raise ValueError("delay must be 0..86400 seconds")
            record={"offset":part["hwm"]+1+i,"timestamp":self.clock(),"producer":producer,"producer_epoch":epoch,"sequence":sequence,"key":key,"value":message["value"],"not_before":self.clock()+delay}
            value=envelope(record,previous)
            values.append(value)
            previous=value["hash"]
        return values

    def produce(self,topic,producer,epoch,sequence,messages,partition=None,acks="all"):
        name(producer)
        integer(epoch)
        integer(sequence)
        if acks not in ("all","leader"):
            raise ValueError("acks must be all or leader")
        if not isinstance(messages,list) or not 1<=len(messages)<=100 or any(not isinstance(message,dict) for message in messages):
            raise ValueError("batch requires 1..100 message objects")
        with self.transaction() as db:
            options=self.topic(db,topic)
            if partition is None:
                key=messages[0].get("key",producer) if isinstance(messages,list) and messages else producer
                partition=zlib.crc32(str(key).encode())%options["partitions"]
            part=self.part(db,topic,partition)
            current=db.execute("SELECT epoch FROM producers WHERE id=?",(producer,)).fetchone()
            if not current or current[0]!=epoch:
                raise Error(409,"producer epoch fenced")
            fingerprint=hashlib.sha256(dumps(messages).encode()).hexdigest()
            receipt=db.execute("SELECT hash,response FROM receipts WHERE producer=? AND epoch=? AND topic=? AND partition=? AND sequence=?",(producer,epoch,topic,partition,sequence)).fetchone()
            if receipt:
                if receipt[0]!=fingerprint:
                    raise Error(409,"producer sequence payload conflict")
                return json.loads(receipt[1])
            pending_row=db.execute("SELECT body FROM pending WHERE topic=? AND partition=?",(topic,partition)).fetchone()
        if pending_row:
            existing=json.loads(pending_row[0])
            if existing.get("producer")==producer and existing.get("epoch")==epoch and existing.get("sequence")==sequence:
                if existing["fingerprint"]!=fingerprint:
                    raise Error(409,"pending producer sequence payload conflict")
                if acks=="leader":
                    self._replicate(existing,leader_only=True)
                    return {"topic":topic,"partition":partition,"offsets":[v["record"]["offset"] for v in existing["values"]],"committed":False}
                return self._flush(topic,partition)
            self._flush(topic,partition)
        with self.transaction() as db:
            part=self.part(db,topic,partition)
            last=db.execute("SELECT last FROM sequences WHERE producer=? AND epoch=? AND topic=? AND partition=?",(producer,epoch,topic,partition)).fetchone()
            expected=last[0]+1 if last else 0
            if sequence<expected:
                raise Error(410,"producer receipt expired; old sequence cannot be reused")
            if sequence!=expected:
                raise Error(409,"producer sequence gap")
        values=self._values(part,messages,producer,epoch,sequence)
        pending={"topic":topic,"partition":partition,"producer":producer,"epoch":epoch,"sequence":sequence,"fingerprint":fingerprint,"values":values}
        with self.transaction() as db:
            db.execute("INSERT INTO pending VALUES(?,?,?)",(topic,partition,dumps(pending)))
        if acks=="leader":
            self._replicate(pending,leader_only=True)
            return {"topic":topic,"partition":partition,"offsets":[v["record"]["offset"] for v in values],"committed":False}
        return self._flush(topic,partition)

    def fetch(self,topic,partition,offset=0,limit=100):
        integer(offset)
        integer(limit,1,1000)
        with self.transaction() as db:
            part=self.part(db,topic,partition)
        if offset<part["floor"]:
            raise Error(410,"offset expired; explicitly reset to earliest")
        if offset>part["hwm"]+1:
            raise Error(416,"offset beyond committed high water")
        if offset==part["hwm"]+1:
            return {"records":[],"next_offset":offset,"high_water":part["hwm"],"floor":part["floor"]}
        part,leader,_=self._leader(part)
        values=self.call(leader,"/read",{"topic":topic,"partition":partition,"offset":offset,"limit":min(limit,part["hwm"]-offset+1)})["records"]
        visible=[]
        for value in values:
            if value["record"]["not_before"]>self.clock():
                break
            visible.append(value["record"])
        return {"records":visible,"next_offset":offset+len(visible),"high_water":part["hwm"],"floor":part["floor"],"leader":leader,"epoch":part["epoch"]}

    def _expire(self,db,topic,group):
        count=db.execute("DELETE FROM members WHERE topic=? AND group_name=? AND expires<=?",(topic,group,self.clock())).rowcount
        if count:
            db.execute("UPDATE groups SET generation=generation+1 WHERE topic=? AND name=?",(topic,group))
            db.execute("DELETE FROM delivered WHERE topic=? AND group_name=?",(topic,group))

    def _membership(self,db,topic,group,member,session,generation=None,partition=None):
        self._expire(db,topic,group)
        metadata=db.execute("SELECT generation FROM groups WHERE topic=? AND name=?",(topic,group)).fetchone()
        row=db.execute("SELECT * FROM members WHERE topic=? AND group_name=? AND id=?",(topic,group,member)).fetchone()
        if not metadata or not row or row["session"]!=session:
            raise Error(409,"consumer session expired or replaced")
        if generation is not None and metadata[0]!=generation:
            raise Error(409,"consumer generation fenced; refresh assignments")
        members=[r[0] for r in db.execute("SELECT id FROM members WHERE topic=? AND group_name=? ORDER BY id",(topic,group))]
        options=self.topic(db,topic)
        assigned=[p for p in range(options["partitions"]) if members[p%len(members)]==member]
        if partition is not None and partition not in assigned:
            raise Error(409,"consumer no longer owns partition")
        return {"generation":metadata[0],"assignments":assigned,"session":session,"member":member}

    def join(self,topic,group,member):
        name(group)
        name(member)
        with self.transaction() as db:
            self.topic(db,topic)
            db.execute("INSERT OR IGNORE INTO groups VALUES(?,?,0)",(topic,group))
            self._expire(db,topic,group)
            session=uuid.uuid4().hex
            db.execute("INSERT INTO members VALUES(?,?,?,?,?) ON CONFLICT(topic,group_name,id) DO UPDATE SET session=excluded.session,expires=excluded.expires",(topic,group,member,session,self.clock()+self.lease_seconds))
            db.execute("UPDATE groups SET generation=generation+1 WHERE topic=? AND name=?",(topic,group))
            db.execute("DELETE FROM delivered WHERE topic=? AND group_name=?",(topic,group))
            return self._membership(db,topic,group,member,session)

    def heartbeat(self,topic,group,member,session):
        with self.transaction() as db:
            result=self._membership(db,topic,group,member,session)
            db.execute("UPDATE members SET expires=? WHERE topic=? AND group_name=? AND id=?",(self.clock()+self.lease_seconds,topic,group,member))
            return result

    def _cursor(self,db,topic,group,partition):
        row=db.execute("SELECT next FROM cursors WHERE topic=? AND group_name=? AND partition=?",(topic,group,partition)).fetchone()
        return row[0] if row else 0

    def group_fetch(self,topic,group,member,session,generation,partition,limit=100,semantics="at_least_once"):
        if semantics not in ("at_least_once","at_most_once"):
            raise ValueError("invalid delivery semantics")
        with self.transaction() as db:
            self._membership(db,topic,group,member,session,generation,partition)
            offset=self._cursor(db,topic,group,partition)
        result=self.fetch(topic,partition,offset,limit)
        with self.transaction() as db:
            self._membership(db,topic,group,member,session,generation,partition)
            old=db.execute("SELECT next FROM delivered WHERE topic=? AND group_name=? AND partition=? AND generation=?",(topic,group,partition,generation)).fetchone()
            delivered=max(result["next_offset"],old[0] if old else 0)
            db.execute("INSERT INTO delivered VALUES(?,?,?,?,?) ON CONFLICT(topic,group_name,partition) DO UPDATE SET generation=excluded.generation,next=excluded.next",(topic,group,partition,generation,delivered))
            if semantics=="at_most_once":
                db.execute("INSERT INTO cursors VALUES(?,?,?,?) ON CONFLICT(topic,group_name,partition) DO UPDATE SET next=excluded.next",(topic,group,partition,result["next_offset"]))
        return {**result,"semantics":semantics,"generation":generation}

    def _validate_commit(self,db,topic,group,member,session,generation,partition,next_offset,expected=None):
        integer(next_offset)
        self._membership(db,topic,group,member,session,generation,partition)
        current=self._cursor(db,topic,group,partition)
        bound=db.execute("SELECT next FROM delivered WHERE topic=? AND group_name=? AND partition=? AND generation=?",(topic,group,partition,generation)).fetchone()
        if next_offset<current or not bound or next_offset>bound[0] or expected is not None and expected!=current:
            raise Error(409,"commit exceeds delivery bound or input cursor changed")

    def commit(self,topic,group,member,session,generation,partition,next_offset):
        with self.transaction() as db:
            self._validate_commit(db,topic,group,member,session,generation,partition,next_offset)
            db.execute("INSERT INTO cursors VALUES(?,?,?,?) ON CONFLICT(topic,group_name,partition) DO UPDATE SET next=excluded.next",(topic,group,partition,next_offset))
        return {"next_offset":next_offset}

    def reset(self,topic,group,member,session,generation,partition,offset="earliest"):
        with self.transaction() as db:
            self._membership(db,topic,group,member,session,generation,partition)
            part=self.part(db,topic,partition)
            target=part["floor"] if offset=="earliest" else part["hwm"]+1 if offset=="latest" else integer(offset)
            if not part["floor"]<=target<=part["hwm"]+1:
                raise Error(416,"reset offset outside retained range")
            db.execute("INSERT INTO cursors VALUES(?,?,?,?) ON CONFLICT(topic,group_name,partition) DO UPDATE SET next=excluded.next",(topic,group,partition,target))
            db.execute("DELETE FROM delivered WHERE topic=? AND group_name=? AND partition=?",(topic,group,partition))
            return {"next_offset":target}

    def process(self,txid,source,expected_offset,next_offset,outputs):
        name(txid)
        fingerprint=hashlib.sha256(dumps([source,expected_offset,next_offset,outputs]).encode()).hexdigest()
        with self.transaction() as db:
            replay=db.execute("SELECT hash,response FROM transactions WHERE id=?",(txid,)).fetchone()
            if replay:
                if replay[0]!=fingerprint:
                    raise Error(409,"transaction ID payload conflict")
                return json.loads(replay[1])
            self._validate_commit(db,**source,next_offset=next_offset,expected=expected_offset)
            if next_offset<=expected_offset:
                raise ValueError("transaction must advance its input cursor")
        if not isinstance(outputs,list) or not 1<=len(outputs)<=10:
            raise ValueError("transaction requires 1..10 output batches")
        pending_batches=[]
        partitions=set()
        try:
            for output in outputs:
                topic,partition=output["topic"],output.get("partition",0)
                if (topic,partition) in partitions:
                    raise ValueError("combine outputs for each destination partition")
                partitions.add((topic,partition))
                self._flush(topic,partition)
                with self.transaction() as db:
                    part=self.part(db,topic,partition)
                values=self._values(part,output["messages"],"tx_"+txid,0,0)
                pending={"topic":topic,"partition":partition,"values":values,"transaction":txid}
                with self.transaction() as db:
                    db.execute("INSERT INTO pending VALUES(?,?,?)",(topic,partition,dumps(pending)))
                pending_batches.append(pending)
            synced=[self._replicate(pending) for pending in pending_batches]
            with self.transaction() as db:
                self._validate_commit(db,**source,next_offset=next_offset,expected=expected_offset)
                produced=[self._finalize(db,pending,nodes) for pending,nodes in zip(pending_batches,synced)]
                db.execute("INSERT INTO cursors VALUES(?,?,?,?) ON CONFLICT(topic,group_name,partition) DO UPDATE SET next=excluded.next",(source["topic"],source["group"],source["partition"],next_offset))
                response={"txid":txid,"outputs":produced,"next_offset":next_offset,"boundary":"queue output visibility and input cursor only"}
                db.execute("INSERT INTO transactions VALUES(?,?,?)",(txid,fingerprint,dumps(response)))
            return response
        except BaseException:
            with self.transaction() as db:
                for pending in pending_batches:
                    db.execute("DELETE FROM pending WHERE topic=? AND partition=?",(pending["topic"],pending["partition"]))
            raise

    def retain(self,topic):
        plans=[]
        with self.transaction() as db:
            options=self.topic(db,topic)
            for row in db.execute("SELECT * FROM parts WHERE topic=?",(topic,)).fetchall():
                part=dict(row)
                records=db.execute("SELECT * FROM records WHERE topic=? AND partition=? ORDER BY offset",(topic,part["partition"])).fetchall()
                size=sum(r["size"] for r in records)
                floor,base_hash=part["floor"],part["base_hash"]
                for record in records:
                    if record["timestamp"]>self.clock()-options["retention"] and size<=options["max_bytes"]:
                        break
                    floor,base_hash=record["offset"]+1,record["hash"]
                    size-=record["size"]
                if floor>part["floor"]:
                    db.execute("UPDATE parts SET floor=?,base_hash=? WHERE topic=? AND partition=?",(floor,base_hash,topic,part["partition"]))
                    db.execute("DELETE FROM records WHERE topic=? AND partition=? AND offset<?",(topic,part["partition"],floor))
                    db.execute("DELETE FROM receipts WHERE topic=? AND partition=? AND last_offset<?",(topic,part["partition"],floor))
                    plans.append({"topic":topic,"partition":part["partition"],"base":floor,"base_hash":base_hash})
        for plan in plans:
            for node in self.brokers:
                try:
                    self.call(node,"/retain",plan)
                except (OSError,ValueError):
                    pass
        return {"retention":plans}

    def maintenance(self):
        with self.lock:
            with self.transaction() as db:
                pending=[dict(row) for row in db.execute("SELECT * FROM pending")]
                topics=[row[0] for row in db.execute("SELECT name FROM topics")]
            for row in pending:
                if not json.loads(row["body"]).get("transaction"):
                    try:
                        self._flush(row["topic"],row["partition"])
                    except (Error,OSError,ValueError):
                        pass
            for topic in topics:
                self.retain(topic)

    def metadata(self):
        with self.transaction() as db:
            return {"role":"coordinator","metadata_ha":False,"brokers":sorted(self.brokers),"topics":[dict(r) for r in db.execute("SELECT * FROM topics")],"partitions":[dict(r) for r in db.execute("SELECT * FROM parts ORDER BY topic,partition")]}

    def dispatch(self,path,body):
        with self.lock:
            methods={"/topic":self.create,"/producer":self.producer_open,"/produce":self.produce,"/fetch":self.fetch,"/join":self.join,"/heartbeat":self.heartbeat,"/group-fetch":self.group_fetch,"/commit":self.commit,"/reset":self.reset,"/process":self.process,"/retain":self.retain}
            if path in ("/health","/metadata"):
                return self.metadata()
            if path not in methods:
                raise Error(404,"unknown coordinator endpoint")
            return methods[path](**body)
