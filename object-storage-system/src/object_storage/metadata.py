import json
import time
import uuid

from .common import Database,Error,checksum,digest,dumps,identifier,integer,object_key,shard_for


class Metadata(Database):
    def __init__(self,path,shard=0,shards=1,clock=time.time):
        super().__init__(path)
        self.shard,self.shards,self.clock=shard,shards,clock
        integer(shards,"shards",1,32)
        integer(shard,"shard",0,shards-1)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS topology(id INTEGER PRIMARY KEY CHECK(id=1),shard INTEGER,shards INTEGER);
        CREATE TABLE IF NOT EXISTS versions(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,bucket TEXT,key TEXT,
            marker INTEGER,manifest TEXT,size INTEGER,sha TEXT,created REAL,removed REAL);
        CREATE INDEX IF NOT EXISTS lookup ON versions(bucket,key,seq);
        CREATE TABLE IF NOT EXISTS receipts(bucket TEXT,key TEXT,request TEXT,hash TEXT,version TEXT,PRIMARY KEY(bucket,key,request));
        """)
        with self.transaction(True) as db:
            db.execute("INSERT OR IGNORE INTO topology VALUES(1,?,?)",(shard,shards))
            row=db.execute("SELECT * FROM topology").fetchone()
            if (row["shard"],row["shards"])!=(shard,shards):
                raise Error(409,"metadata shard topology changed")

    def owner(self,bucket,key):
        if shard_for(bucket,key,self.shards)!=self.shard:
            raise Error(409,"wrong metadata shard")

    def publish(self,bucket,key,request,manifest,size,sha,marker=False):
        self.owner(bucket,key)
        identifier(request)
        checksum(sha)
        integer(size,"size",0,64*1024*1024)
        if type(marker) is not bool or not isinstance(manifest,list) or len(manifest)>256:
            raise ValueError("invalid manifest")
        for chunk in manifest:
            if not isinstance(chunk,dict) or set(chunk)!={"hash","size"}:
                raise ValueError("invalid chunk manifest")
            checksum(chunk["hash"])
            integer(chunk["size"],"chunk size",1,256*1024)
        if sum(c["size"] for c in manifest)!=size or (marker and (manifest or size)):
            raise ValueError("manifest size mismatch")
        fingerprint=digest([manifest,size,sha,marker])
        with self.transaction(True) as db:
            prior=db.execute("SELECT * FROM receipts WHERE bucket=? AND key=? AND request=?",(bucket,key,request)).fetchone()
            if prior:
                if prior["hash"]!=fingerprint:
                    raise Error(409,"idempotency key content conflict")
                return {"version":prior["version"],"duplicate":True}
            version=uuid.uuid4().hex
            db.execute("INSERT INTO versions(id,bucket,key,marker,manifest,size,sha,created) VALUES(?,?,?,?,?,?,?,?)",
                       (version,bucket,key,marker,dumps(manifest),size,sha,self.clock()))
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)",(bucket,key,request,fingerprint,version))
            return {"version":version,"duplicate":False}

    def get(self,bucket,key,version=None):
        self.owner(bucket,key)
        with self.transaction() as db:
            sql="SELECT * FROM versions WHERE bucket=? AND key=? AND removed IS NULL"
            args=[bucket,key]
            if version:
                sql+=" AND id=?"
                args.append(version)
            row=db.execute(sql+" ORDER BY seq DESC LIMIT 1",args).fetchone()
            if not row:
                raise Error(404,"object version missing")
            result=dict(row)
            result["manifest"]=json.loads(result["manifest"])
            return result

    def remove(self,bucket,key,version):
        self.owner(bucket,key)
        with self.transaction(True) as db:
            row=db.execute("SELECT id FROM versions WHERE bucket=? AND key=? AND id=?",(bucket,key,version)).fetchone()
            if not row:
                raise Error(404,"object version missing")
            db.execute("UPDATE versions SET removed=coalesce(removed,?) WHERE id=?",(self.clock(),version))
        return {"removed":version}

    def listing(self,bucket,versions=False):
        with self.transaction() as db:
            if versions:
                rows=db.execute("SELECT * FROM versions WHERE bucket=? AND removed IS NULL ORDER BY key,seq DESC LIMIT 10001",(bucket,)).fetchall()
            else:
                rows=db.execute("""SELECT v.* FROM versions v JOIN
                    (SELECT key,max(seq) seq FROM versions WHERE bucket=? AND removed IS NULL GROUP BY key) x
                    ON v.key=x.key AND v.seq=x.seq WHERE v.marker=0 ORDER BY v.key LIMIT 10001""",(bucket,)).fetchall()
            if len(rows)>10000:
                raise Error(422,"listing exceeds reference budget")
            return {"rows":[{k:r[k] for k in ("key","id","seq","marker","size","sha","created")} for r in rows]}

    def references(self,grace=60):
        integer(grace,"grace",0,86400)
        with self.transaction() as db:
            rows=db.execute("SELECT manifest FROM versions WHERE removed IS NULL OR removed>?",(self.clock()-grace,)).fetchall()
            hashes=sorted({c["hash"] for r in rows for c in json.loads(r[0])})
            if len(hashes)>50000:
                raise Error(422,"maintenance reference budget exceeded")
            return {"hashes":hashes}

    def prune(self,bucket,keep):
        integer(keep,"keep",1,1000)
        count=0
        with self.transaction(True) as db:
            keys=[r[0] for r in db.execute("SELECT DISTINCT key FROM versions WHERE bucket=? AND removed IS NULL",(bucket,))]
            for key in keys:
                rows=db.execute("SELECT id FROM versions WHERE bucket=? AND key=? AND removed IS NULL ORDER BY seq DESC",(bucket,key)).fetchall()
                for row in rows[keep:]:
                    db.execute("UPDATE versions SET removed=? WHERE id=?",(self.clock(),row[0]))
                    count+=1
        return {"pruned":count}
