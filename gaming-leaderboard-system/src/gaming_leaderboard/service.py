from concurrent.futures import ThreadPoolExecutor
import threading
import time

from .common import Error,digest,http_json,shard_for


class Service:
    def __init__(self,config,journal):
        self.config,self.journal=config,journal
        self.lock=threading.RLock()

    def node(self,shard,body):
        return http_json(self.config["projection_urls"][shard]+"/internal/query",self.config["internal_token"],body)

    def scatter(self,month,heads,**body):
        def query(p):
            return self.node(p,{"month":month,"lineage":self.journal.lineage,"minimum":heads[str(p)],**body})
        with ThreadPoolExecutor(max_workers=len(heads)) as pool:
            return list(pool.map(query,range(len(heads))))

    def ready(self,heads,timeout=1.5):
        end=time.monotonic()+timeout
        while True:
            try:
                for shard,url in enumerate(self.config["projection_urls"]):
                    status=http_json(url+"/internal/status",self.config["internal_token"])
                    if status["position"]<heads[str(shard)] or status["lineage"]!=self.journal.lineage:
                        raise Error(503,"projection catching up")
                return True
            except Exception:
                if time.monotonic()>=end:
                    return False
                time.sleep(.02)

    def query(self,month=None,user=None,radius=0):
        season=self.journal.month(month)
        month=season["month"]
        heads=self.journal.status()["heads"]
        if not self.ready(heads):
            raise Error(503,"ranking is unavailable or rebuilding; no partial results")
        if user is None:
            parts=self.scatter(month,heads,mode="top",limit=10)
            rows=sorted([r for part in parts for r in part["rows"]],key=lambda r:(-r["score"],r["user"]))[:10]
            rank,previous=0,None
            for pos,row in enumerate(rows,1):
                if row["score"]!=previous:
                    rank=pos
                row.update(rank=rank,position=pos)
                previous=row["score"]
        else:
            owner=shard_for(user,len(heads))
            target=self.node(owner,{"month":month,"lineage":self.journal.lineage,"minimum":heads[str(owner)],"mode":"target","user":user}).get("target")
            if not target:
                raise Error(404,"player is not in this season")
            around=self.scatter(month,heads,mode="around",pivot=target["lex"],score=target["score"],radius=radius)
            position=1+sum(part["before"] for part in around)
            above=sorted([r for p in around for r in p["above"]],key=lambda r:r["lex"])[-radius:] if radius else []
            below=sorted([r for p in around for r in p["below"]],key=lambda r:r["lex"])[:radius]
            rows=above+[target]+below
            counts=self.scatter(month,heads,mode="counts",scores=sorted({r["score"] for r in rows}))
            for pos,row in enumerate(rows,position-len(above)):
                row.update(position=pos,rank=1+sum(p["counts"][str(row["score"])] for p in counts))
        names=self.journal.names([r["user"] for r in rows])
        for row in rows:
            row.pop("lex",None)
            row["name"]=names[row["user"]]
        return {"month":month,"season_status":season["status"],"rows":rows,"heads":heads,
                "lineage":self.journal.lineage,"ties":"competition rank; user ID ascending display order"}

    def reconcile(self,month):
        self.journal.month(month)
        heads=self.journal.status()["heads"]
        if not self.ready(heads):
            raise Error(503,"projection not ready")
        actual=sorted([(r["user"],r["score"]) for part in self.scatter(month,heads,mode="dump") for r in part["rows"]])
        expected=sorted((r["user"],r["score"]) for r in self.journal.rows(month))
        return {"month":month,"matches":actual==expected,"expected_sha256":digest(expected),"redis_sha256":digest(actual),"players":len(expected)}
