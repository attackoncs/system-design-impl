import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import re
import threading
import time
import uuid

from .common import CHUNK,Database,Error,checksum,digest,dumps,identifier,integer,object_key,request,sha,shard_for


class Catalog(Database):
    def __init__(self,path,clock=time.time):
        super().__init__(path)
        self.clock,self.lock=clock,threading.RLock()
        self.initialize("""
        CREATE TABLE IF NOT EXISTS buckets(name TEXT PRIMARY KEY,id TEXT UNIQUE,user TEXT);
        CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,bucket TEXT,user TEXT,key TEXT,request TEXT,
            sha TEXT,size INTEGER,state TEXT,expires REAL,result TEXT,manifest TEXT,UNIQUE(bucket,key,request));
        CREATE TABLE IF NOT EXISTS parts(upload TEXT,number INTEGER,sha TEXT,size INTEGER,manifest TEXT,PRIMARY KEY(upload,number));
        CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,user TEXT,bucket TEXT,prefix TEXT,
            versions INTEGER,page_size INTEGER,expires REAL,rows TEXT);
        """)

    def create(self,name,user):
        if not isinstance(name,str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]",name):
            raise ValueError("bucket requires 3..63 lowercase letters/digits/hyphens")
        with self.transaction(True) as db:
            old=db.execute("SELECT * FROM buckets WHERE name=?",(name,)).fetchone()
            if old:
                if old["user"]!=user:
                    raise Error(409,"bucket name unavailable")
                return dict(old)
            value={"name":name,"id":uuid.uuid4().hex,"user":user}
            db.execute("INSERT INTO buckets VALUES(?,?,?)",tuple(value.values()))
            return value

    def bucket(self,name,user):
        with self.transaction() as db:
            row=db.execute("SELECT * FROM buckets WHERE name=? AND user=?",(name,user)).fetchone()
            if not row:
                raise Error(404,"bucket missing")
            return dict(row)

    def start(self,bucket,user,body):
        if not isinstance(body,dict) or set(body)!={"key","request_key","sha256","size"}:
            raise ValueError("upload requires key,request_key,sha256,size")
        key=object_key(body["key"])
        identifier(body["request_key"])
        checksum(body["sha256"])
        integer(body["size"],"size",0,64*1024*1024)
        with self.transaction(True) as db:
            old=db.execute("SELECT * FROM uploads WHERE bucket=? AND key=? AND request=?",(bucket,key,body["request_key"])).fetchone()
            if old:
                if (old["sha"],old["size"])!=(body["sha256"],body["size"]):
                    raise Error(409,"upload idempotency content conflict")
                return dict(old)
            upload=uuid.uuid4().hex
            db.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?,?,'active',?,NULL,NULL)",
                       (upload,bucket,user,key,body["request_key"],body["sha256"],body["size"],self.clock()+3600))
        return self.upload(upload,user)

    def upload(self,upload,user):
        identifier(upload)
        with self.transaction() as db:
            row=db.execute("SELECT * FROM uploads WHERE id=? AND user=?",(upload,user)).fetchone()
            if not row:
                raise Error(404,"upload missing")
            value=dict(row)
            value["parts"]=[{**dict(r),"manifest":json.loads(r["manifest"])} for r in db.execute("SELECT * FROM parts WHERE upload=? ORDER BY number",(upload,))]
            return value

    def add_part(self,upload,user,number,data,manifest):
        integer(number,"part number",1,64)
        state=self.upload(upload,user)
        if state["state"]!="active" or state["expires"]<=self.clock():
            raise Error(409,"upload is not active")
        with self.transaction(True) as db:
            old=db.execute("SELECT * FROM parts WHERE upload=? AND number=?",(upload,number)).fetchone()
            if old:
                if (old["sha"],old["size"])!=(sha(data),len(data)):
                    raise Error(409,"part number content is immutable")
                return {"number":number,"sha256":old["sha"],"duplicate":True}
            db.execute("INSERT INTO parts VALUES(?,?,?,?,?)",(upload,number,sha(data),len(data),dumps(manifest)))
        return {"number":number,"sha256":sha(data),"duplicate":False}


class Service:
    def __init__(self,config,catalog):
        self.config,self.catalog=config,catalog

    def meta(self,bucket,key,path,body=None,method=None):
        index=shard_for(bucket,key,len(self.config["metadata_urls"]))
        return request(self.config["metadata_urls"][index]+path,self.config["internal_token"],body,method)

    def publish(self,bucket,key,key_id,manifest,size,checksum_value,marker=False):
        return self.meta(bucket,key,"/publish",{"bucket":bucket,"key":key,"request":key_id,
                         "manifest":manifest,"size":size,"sha":checksum_value,"marker":marker})

    def write_chunk(self,data):
        key=sha(data)
        def put(url):
            try:
                result=request(url+"/chunks/"+key,self.config["internal_token"],data,"PUT")
                return result.get("hash")==key and result.get("size")==len(data)
            except Exception:
                return False
        with ThreadPoolExecutor(max_workers=3) as pool:
            acknowledgements=sum(pool.map(put,self.config["data_urls"]))
        if acknowledgements<self.config.get("write_quorum",2):
            raise Error(503,"insufficient durable data replicas; object not published")
        return {"hash":key,"size":len(data)}

    def store_bytes(self,data):
        return [self.write_chunk(data[offset:offset+CHUNK]) for offset in range(0,len(data),CHUNK)]

    def read_chunk(self,key):
        for url in self.config["data_urls"]:
            try:
                data=request(url+"/chunks/"+key,self.config["internal_token"],raw=True)
                if sha(data)==key:
                    return data
            except Exception:
                pass
        raise Error(503,"no verified replica available")

    def put(self,bucket,key,key_id,data,expected):
        object_key(key)
        identifier(key_id)
        if sha(data)!=checksum(expected):
            raise Error(400,"whole-object checksum mismatch")
        manifest=self.store_bytes(data)
        return self.publish(bucket,key,key_id,manifest,len(data),expected)

    def get(self,bucket,key,version=None):
        row=self.meta(bucket,key,"/get",{"bucket":bucket,"key":key,"version":version})
        if row["marker"]:
            raise Error(404,"latest version is a delete marker")
        data=b"".join(self.read_chunk(c["hash"]) for c in row["manifest"])
        if len(data)!=row["size"] or sha(data)!=row["sha"]:
            raise Error(409,"whole-object integrity mismatch")
        return row,data

    def part(self,upload,user,number,data,expected):
        if len(data)>1024*1024 or not data:
            raise Error(413,"part requires 1..1048576 bytes")
        if sha(data)!=checksum(expected):
            raise Error(400,"part checksum mismatch")
        state=self.catalog.upload(upload,user)
        if state["state"]!="active" or state["expires"]<=self.catalog.clock():
            raise Error(409,"upload is not active")
        integer(number,"part number",1,64)
        return self.catalog.add_part(upload,user,number,data,self.store_bytes(data))

    def complete(self,upload,user):
        state=self.catalog.upload(upload,user)
        if state["state"]=="complete":
            return json.loads(state["result"])
        if state["state"]=="active":
            if state["expires"]<=self.catalog.clock():
                raise Error(409,"upload expired")
            if [p["number"] for p in state["parts"]]!=list(range(1,len(state["parts"])+1)):
                raise Error(409,"part numbers must be contiguous from one")
            manifest=[chunk for part in state["parts"] for chunk in part["manifest"]]
            if len(manifest)>256:
                raise Error(422,"manifest exceeds 256 chunks")
            whole=hashlib.sha256()
            size=0
            for chunk in manifest:
                data=self.read_chunk(chunk["hash"])
                if len(data)!=chunk["size"]:
                    raise Error(409,"chunk size mismatch")
                whole.update(data)
                size+=len(data)
            if size!=state["size"] or whole.hexdigest()!=state["sha"]:
                raise Error(409,"completion size/checksum mismatch")
            # Freeze intent before cross-service publication. Expiry cleanup must
            # not abort this state; maintenance can safely resume it after a crash.
            with self.catalog.transaction(True) as db:
                db.execute("UPDATE uploads SET state='completing',manifest=? WHERE id=?",(dumps(manifest),upload))
            state["state"],state["manifest"]="completing",dumps(manifest)
        if state["state"]!="completing":
            raise Error(409,"upload cannot complete")
        result=self.publish(state["bucket"],state["key"],state["request"],json.loads(state["manifest"]),state["size"],state["sha"])
        with self.catalog.transaction(True) as db:
            db.execute("UPDATE uploads SET state='complete',result=? WHERE id=?",(dumps(result),upload))
        return result

    def listing(self,bucket,user,prefix="",versions=False,limit=100,token=None):
        integer(limit,"limit",1,1000)
        if not isinstance(prefix,str) or len(prefix.encode())>512:
            raise ValueError("invalid prefix")
        secret=self.config["continuation_secret"].encode()
        if token:
            try:
                encoded,signature=token.split(".")
                if not hmac.compare_digest(signature,hmac.new(secret,encoded.encode(),hashlib.sha256).hexdigest()):
                    raise ValueError("signature")
                payload=json.loads(base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4)))
                snapshot,index=payload["id"],payload["index"]
                integer(index,"cursor")
            except Exception:
                raise Error(400,"invalid continuation token") from None
            with self.catalog.transaction() as db:
                row=db.execute("SELECT * FROM snapshots WHERE id=?",(snapshot,)).fetchone()
            if not row or (row["user"],row["bucket"],row["prefix"],bool(row["versions"]),row["page_size"])!=(user,bucket,prefix,versions,limit) or row["expires"]<=self.catalog.clock():
                raise Error(409,"continuation expired or query/owner changed")
            rows=json.loads(row["rows"])
        else:
            rows=[]
            for url in self.config["metadata_urls"]:
                rows.extend(request(url+"/list",self.config["internal_token"],{"bucket":bucket,"versions":versions})["rows"])
            rows=[r for r in rows if r["key"].startswith(prefix)]
            if len(rows)>10000:
                raise Error(422,"global listing budget exceeded")
            rows.sort(key=lambda r:(r["key"],-r["seq"]))
            snapshot,index=uuid.uuid4().hex,0
            with self.catalog.transaction(True) as db:
                db.execute("DELETE FROM snapshots WHERE expires<=?",(self.catalog.clock(),))
                db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)",(snapshot,user,bucket,prefix,versions,limit,self.catalog.clock()+300,dumps(rows)))
        page=rows[index:index+limit]
        next_token=None
        if index+limit<len(rows):
            encoded=base64.urlsafe_b64encode(dumps({"id":snapshot,"index":index+limit}).encode()).decode().rstrip("=")
            next_token=encoded+"."+hmac.new(secret,encoded.encode(),hashlib.sha256).hexdigest()
        return {"objects":page,"continuation":next_token,"snapshot":snapshot,"snapshot_ttl_seconds":300}

    def maintenance(self,grace=60):
        integer(grace,"grace",0,86400)
        resumed=0
        with self.catalog.transaction() as db:
            completing=[dict(r) for r in db.execute("SELECT id,user FROM uploads WHERE state='completing'")]
        for row in completing:
            self.complete(row["id"],row["user"])
            resumed+=1
        with self.catalog.transaction(True) as db:
            expired=db.execute("UPDATE uploads SET state='aborted' WHERE state='active' AND expires<=?",(self.catalog.clock(),)).rowcount
        live=set()
        for url in self.config["metadata_urls"]:
            live.update(request(url+"/references",self.config["internal_token"],{"grace":grace})["hashes"])
        with self.catalog.transaction() as db:
            for row in db.execute("SELECT p.manifest FROM parts p JOIN uploads u ON u.id=p.upload WHERE u.state IN('active','completing')"):
                live.update(c["hash"] for c in json.loads(row[0]))
        if len(live)>50000:
            raise Error(422,"global maintenance reference budget exceeded")
        repaired,unrecoverable,copy_failures=0,[],[]
        for key in sorted(live):
            try:
                data=self.read_chunk(key)
            except Error:
                unrecoverable.append(key)
                continue
            for url in self.config["data_urls"]:
                try:
                    good=request(url+"/chunks/"+key,self.config["internal_token"],raw=True)
                    if sha(good)==key:
                        continue
                except Exception:
                    pass
                try:
                    request(url+"/chunks/"+key,self.config["internal_token"],data,"PUT")
                    repaired+=1
                except Exception:
                    copy_failures.append({"hash":key,"url":url})
        nodes=[]
        for url in self.config["data_urls"]:
            try:
                result=request(url+"/collect",self.config["internal_token"],{"live":sorted(live),"grace":grace})
                nodes.append({"url":url,**result})
            except Exception:
                nodes.append({"url":url,"unavailable":True})
        return {"expired_uploads":expired,"resumed_completions":resumed,"live_chunks":len(live),
                "repaired_copies":repaired,"unrecoverable":unrecoverable,"copy_failures":copy_failures,"nodes":nodes}
