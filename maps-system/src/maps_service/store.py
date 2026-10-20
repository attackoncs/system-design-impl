from collections import OrderedDict
from contextlib import closing,contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .geo import boxes,cell,coordinates,distance,match_point,nearby_boxes

MODES = {"driving","walking","cycling"}


class Error(Exception):
    def __init__(self,status,message):
        self.status,self.message=status,message
        super().__init__(message)


def dumps(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False)


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",value):
        raise ValueError("invalid identifier")
    return value


def positive(value,maximum=1e8):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<value<=maximum:
        raise ValueError("invalid positive numeric value")
    return float(value)


class Cache:
    def __init__(self,capacity=128):
        self.capacity,self.items,self.lock=capacity,OrderedDict(),threading.Lock()
        self.hits=self.misses=0

    def get(self,key,loader):
        with self.lock:
            if key in self.items:
                self.hits+=1
                self.items.move_to_end(key)
                return self.items[key]
            self.misses+=1
            value=loader()
            self.items[key]=value
            while len(self.items)>self.capacity:
                self.items.popitem(last=False)
            return value


class Store:
    def __init__(self,path,readonly=False,clock=time.time):
        self.path,self.readonly,self.clock=str(path),readonly,clock
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.adjacency,self.tiles=Cache(64),Cache(128)
        with closing(self.connect()) as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY,lat REAL,lon REAL,tx INTEGER,ty INTEGER);
            CREATE INDEX IF NOT EXISTS nodes_tile ON nodes(tx,ty);
            CREATE INDEX IF NOT EXISTS nodes_lat_lon ON nodes(lat,lon);
            CREATE TABLE IF NOT EXISTS edges(id TEXT PRIMARY KEY,source TEXT,target TEXT,length REAL,speeds TEXT,toll INTEGER);
            CREATE INDEX IF NOT EXISTS edges_source ON edges(source);
            CREATE TABLE IF NOT EXISTS addresses(name TEXT PRIMARY KEY,node TEXT);
            CREATE TABLE IF NOT EXISTS edge_boxes(id INTEGER PRIMARY KEY,edge TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS spatial USING rtree(id,west,east,south,north);
            CREATE TABLE IF NOT EXISTS traffic(edge TEXT PRIMARY KEY,speed REAL,closed INTEGER,expires REAL);
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,token TEXT UNIQUE,admin INTEGER);
            CREATE TABLE IF NOT EXISTS events(user TEXT,event TEXT,hash TEXT,result TEXT,received REAL,PRIMARY KEY(user,event));
            CREATE TABLE IF NOT EXISTS positions(user TEXT PRIMARY KEY,seq INTEGER,timestamp REAL,body TEXT,received REAL);
            CREATE TABLE IF NOT EXISTS samples(user TEXT,event TEXT,edge TEXT,speed REAL,received REAL,PRIMARY KEY(user,event));
            CREATE INDEX IF NOT EXISTS samples_time ON samples(received);
            """)
            for key,value in {"graph":str(uuid.uuid4()),"lineage":str(uuid.uuid4()),"revision":0,"traffic_version":0,"ready":False,"license":"unspecified","graph_hash":""}.items():
                db.execute("INSERT OR IGNORE INTO config VALUES(?,?)",(key,dumps(value)))
            db.commit()

    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        return db

    @contextmanager
    def transaction(self,write=False):
        with closing(self.connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def meta(self,db):
        return {row[0]:json.loads(row[1]) for row in db.execute("SELECT key,value FROM config")}

    def set_meta(self,db,**values):
        for key,value in values.items():
            db.execute("UPDATE config SET value=? WHERE key=?",(dumps(value),key))

    def writable(self):
        if self.readonly:
            raise Error(403,"replica is read only")

    def provision(self,user,token,admin=False):
        self.writable()
        identifier(user)
        if not isinstance(token,str) or len(token)<8:
            raise ValueError("credential too short")
        with self.transaction(True) as db:
            db.execute("INSERT INTO users VALUES(?,?,?)",(user,hashlib.sha256(token.encode()).hexdigest(),int(admin)))

    def authenticate(self,token,admin=False):
        with self.transaction() as db:
            row=db.execute("SELECT id,admin FROM users WHERE token=?",(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not row or admin and not row["admin"]:
                raise Error(403,"invalid credential or permission")
            return row["id"]

    def _install_graph(self,db,graph):
        if not isinstance(graph,dict) or set(graph)-{"nodes","edges","addresses","license"}:
            raise ValueError("invalid graph document")
        license=graph.get("license","unspecified")
        if not isinstance(license,str) or not 1<=len(license)<=2048:
            raise ValueError("invalid dataset license")
        self.set_meta(db,license=license)
        nodes=graph["nodes"]
        if not isinstance(nodes,list) or not 1<=len(nodes)<=100000:
            raise ValueError("graph requires 1..100000 nodes in reference importer")
        for table in ("spatial","edge_boxes","edges","nodes","addresses","traffic","samples"):
            db.execute("DELETE FROM "+table)
        points={}
        for node in nodes:
            name=identifier(node["id"])
            lat,lon=coordinates(node["latitude"],node["longitude"])
            tx,ty=cell(lat,lon)
            db.execute("INSERT INTO nodes VALUES(?,?,?,?,?)",(name,lat,lon,tx,ty))
            points[name]=(lat,lon)
        if not isinstance(graph["edges"],list) or len(graph["edges"])>500000:
            raise ValueError("invalid edge count")
        for edge in graph["edges"]:
            name=identifier(edge["id"])
            source,target=edge["source"],edge["target"]
            if source==target or source not in points or target not in points:
                raise ValueError("invalid road endpoints")
            length=positive(edge.get("length_m",distance(points[source],points[target])))
            speeds=edge["speeds"]
            if not isinstance(speeds,dict) or not speeds or set(speeds)-MODES:
                raise ValueError("invalid travel modes")
            speeds={mode:positive(speed,100) for mode,speed in speeds.items()}
            toll=edge.get("toll",False)
            if type(toll) is not bool:
                raise ValueError("toll must be boolean")
            db.execute("INSERT INTO edges VALUES(?,?,?,?,?,?)",(name,source,target,length,dumps(speeds),int(toll)))
            for west,east,south,north in boxes(points[source],points[target]):
                row=db.execute("INSERT INTO edge_boxes(edge) VALUES(?)",(name,)).lastrowid
                db.execute("INSERT INTO spatial VALUES(?,?,?,?,?)",(row,west,east,south,north))
        for name,node in graph.get("addresses",{}).items():
            if not isinstance(name,str) or not name.strip() or len(name)>500 or node not in points:
                raise ValueError("invalid fixture address")
            db.execute("INSERT INTO addresses VALUES(?,?)",(name.strip().casefold(),node))

    def import_graph(self,graph,fail_hook=None):
        self.writable()
        with self.transaction(True) as db:
            meta=self.meta(db)
            self._install_graph(db,graph)
            if fail_hook:
                fail_hook()
            graph_hash=hashlib.sha256(dumps(self.graph_document(db)).encode()).hexdigest()
            self.set_meta(db,graph=str(uuid.uuid4()),revision=meta["revision"]+1,traffic_version=0,ready=True,graph_hash=graph_hash)
        return self.health()

    def health(self):
        with self.transaction() as db:
            return {**self.meta(db),"role":"replica" if self.readonly else "primary","nodes":db.execute("SELECT count(*) FROM nodes").fetchone()[0],"edges":db.execute("SELECT count(*) FROM edges").fetchone()[0]}

    def ready(self,db):
        meta=self.meta(db)
        if not meta["ready"]:
            raise Error(503,"no road catalog installed")
        return meta

    def graph_document(self,db):
        return {"nodes":[{"id":r["id"],"latitude":r["lat"],"longitude":r["lon"]} for r in db.execute("SELECT * FROM nodes ORDER BY id")],"edges":[{"id":r["id"],"source":r["source"],"target":r["target"],"length_m":r["length"],"speeds":json.loads(r["speeds"]),"toll":bool(r["toll"])} for r in db.execute("SELECT * FROM edges ORDER BY id")],"addresses":dict(db.execute("SELECT name,node FROM addresses ORDER BY name")),"license":self.meta(db)["license"]}

    def export(self):
        with self.transaction() as db:
            meta=self.ready(db)
            return {"meta":meta,"graph":self.graph_document(db),"traffic":[dict(row) for row in db.execute("SELECT * FROM traffic WHERE expires>?",(self.clock(),))]}

    def install_snapshot(self,snapshot):
        if not self.readonly:
            raise Error(403,"snapshot import requires replica")
        incoming=snapshot["meta"]
        uuid.UUID(incoming["lineage"])
        uuid.UUID(incoming["graph"])
        if type(incoming["revision"]) is not int or incoming["revision"]<0 or type(incoming["traffic_version"]) is not int or incoming["traffic_version"]<0:
            raise ValueError("invalid catalog revision")
        with self.transaction(True) as db:
            old=self.meta(db)
            if old["ready"]:
                if old["lineage"]!=incoming["lineage"] or incoming["revision"]<old["revision"]:
                    raise Error(409,"foreign lineage or catalog rollback")
                if incoming["revision"]==old["revision"]:
                    return False
            self._install_graph(db,snapshot["graph"])
            graph_hash=hashlib.sha256(dumps(self.graph_document(db)).encode()).hexdigest()
            if graph_hash!=incoming["graph_hash"] or old["ready"] and old["graph"]==incoming["graph"] and old["graph_hash"]!=graph_hash:
                raise Error(409,"catalog checksum or immutable graph changed")
            for record in snapshot["traffic"]:
                self._traffic_record(db,record["edge"],record["speed"],bool(record["closed"]),record["expires"])
            self.set_meta(db,**{key:incoming[key] for key in ("lineage","graph","revision","traffic_version","graph_hash")},ready=True)
            return True

    def resolve(self,db,value,max_snap=1000):
        if isinstance(value,str):
            row=db.execute("SELECT id,lat,lon,tx,ty FROM nodes WHERE id=?",(value,)).fetchone()
            if not row:
                row=db.execute("SELECT n.* FROM addresses a JOIN nodes n ON n.id=a.node WHERE a.name=?",(value.strip().casefold(),)).fetchone()
            if not row:
                raise Error(404,"node/address not found in fixture")
            return dict(row)
        point=coordinates(value["latitude"],value["longitude"])
        choices={}
        for west,east,south,north in nearby_boxes(*point,max_snap):
            for row in db.execute("SELECT * FROM nodes WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? LIMIT 10001",(south,north,west,east)):
                choices[row["id"]]=dict(row)
            if len(choices)>10000:
                raise Error(413,"snap candidate budget exceeded")
        ranked=sorted((distance(point,(row["lat"],row["lon"])),name,row) for name,row in choices.items())
        if not ranked or ranked[0][0]>max_snap+1e-6:
            raise Error(404,"no road node within snap limit")
        return ranked[0][2]

    def geocode(self,value,reverse=False):
        with self.transaction() as db:
            meta=self.ready(db)
            row=self.resolve(db,value)
            names=[r[0] for r in db.execute("SELECT name FROM addresses WHERE node=? ORDER BY name",(row["id"],))]
            return {"graph":meta["graph"],"node":row["id"],"latitude":row["lat"],"longitude":row["lon"],"addresses":names,"method":"nearest fixture node" if reverse else "exact fixture lookup"}

    def outgoing(self,db,node,graph,loaded):
        tx,ty=node["tx"],node["ty"]
        loaded.add((tx,ty))
        def load():
            adjacency={}
            for row in db.execute("SELECT e.* FROM edges e JOIN nodes n ON n.id=e.source WHERE n.tx=? AND n.ty=? ORDER BY e.id",(tx,ty)):
                record=dict(row)
                record["speeds"]=json.loads(record["speeds"])
                adjacency.setdefault(record["source"],[]).append(record)
            return adjacency
        return self.adjacency.get((graph,tx,ty),load).get(node["id"],[])

    def spatial_edges(self,db,west,east,south,north):
        return db.execute("""SELECT DISTINCT e.*,a.lat AS alat,a.lon AS alon,b.lat AS blat,b.lon AS blon
          FROM spatial s JOIN edge_boxes eb ON eb.id=s.id JOIN edges e ON e.id=eb.edge
          JOIN nodes a ON a.id=e.source JOIN nodes b ON b.id=e.target
          WHERE s.west<=? AND s.east>=? AND s.south<=? AND s.north>=?""",(east,west,north,south)).fetchall()

    def match(self,db,lat,lon,mode,heading=None):
        if heading is not None and (type(heading) not in (int,float) or not math.isfinite(heading) or not 0<=heading<360):
            raise ValueError("invalid heading")
        candidates={}
        for box in nearby_boxes(lat,lon,100):
            for row in self.spatial_edges(db,*box):
                if mode not in json.loads(row["speeds"]):
                    continue
                gap,fraction,bearing=match_point((lat,lon),(row["alat"],row["alon"]),(row["blat"],row["blon"]))
                angle=abs((bearing-heading+180)%360-180) if heading is not None else 0
                if gap<=100 and angle<=60:
                    candidates[row["id"]]=(gap,angle,row["id"],fraction,row["length"])
        ranked=sorted(candidates.values())
        if not ranked:
            return None
        best=ranked[0]
        ambiguous=heading is None or len(ranked)>1 and abs(ranked[1][0]-best[0])<1 and abs(ranked[1][1]-best[1])<5
        return {"edge":best[2],"fraction":best[3],"gap_m":best[0],"length_m":best[4],"traffic_eligible":not ambiguous}

    def _prune(self,db):
        db.execute("DELETE FROM events WHERE received<=?",(self.clock()-86400,))
        db.execute("DELETE FROM positions WHERE received<=?",(self.clock()-86400,))
        db.execute("DELETE FROM samples WHERE received<=?",(self.clock()-300,))

    def prune(self):
        if not self.readonly:
            with self.transaction(True) as db:
                self._prune(db)

    def ingest(self,user,reports):
        self.writable()
        if not isinstance(reports,list) or not 1<=len(reports)<=100:
            raise ValueError("batch requires 1..100 reports")
        output=[]
        with self.transaction(True) as db:
            meta=self.ready(db)
            self._prune(db)
            for body in reports:
                event=identifier(body["event_id"])
                fingerprint=hashlib.sha256(dumps(body).encode()).hexdigest()
                replay=db.execute("SELECT hash,result FROM events WHERE user=? AND event=?",(user,event)).fetchone()
                if replay:
                    if replay["hash"]!=fingerprint:
                        raise Error(409,"event ID reused with different report")
                    output.append(json.loads(replay["result"]))
                    continue
                seq=body["sequence"]
                stamp=body["timestamp"]
                if type(seq) is not int or not 0<=seq<2**53 or type(stamp) not in (float,int) or not math.isfinite(stamp) or not self.clock()-86400<stamp<=self.clock()+30:
                    raise ValueError("invalid report sequence/timestamp")
                lat,lon=coordinates(body["latitude"],body["longitude"])
                mode=body.get("mode","driving")
                if mode not in MODES:
                    raise ValueError("invalid report mode")
                matched=self.match(db,lat,lon,mode,body.get("heading"))
                previous=db.execute("SELECT * FROM positions WHERE user=?",(user,)).fetchone()
                advances=previous is None or seq>previous["seq"] and stamp>previous["timestamp"]
                result={"event_id":event,"sequence":seq,"current":advances,"graph":meta["graph"],"match":matched}
                if advances:
                    position={"latitude":lat,"longitude":lon,"timestamp":stamp,"graph":meta["graph"],"mode":mode,"match":matched}
                    if previous and mode=="driving" and matched and matched["traffic_eligible"]:
                        old=json.loads(previous["body"])
                        old_match=old.get("match")
                        interval=stamp-previous["timestamp"]
                        if old["graph"]==meta["graph"] and old.get("mode")=="driving" and old_match and old_match["traffic_eligible"] and old_match["edge"]==matched["edge"] and 1<=interval<=120:
                            speed=(matched["fraction"]-old_match["fraction"])*matched["length_m"]/interval
                            if 0.5<=speed<=50:
                                db.execute("INSERT INTO samples VALUES(?,?,?,?,?)",(user,event,matched["edge"],speed,self.clock()))
                    db.execute("INSERT INTO positions VALUES(?,?,?,?,?) ON CONFLICT(user) DO UPDATE SET seq=excluded.seq,timestamp=excluded.timestamp,body=excluded.body,received=excluded.received",(user,seq,stamp,dumps(position),self.clock()))
                stored={**result,"report":body}
                db.execute("INSERT INTO events VALUES(?,?,?,?,?)",(user,event,fingerprint,dumps(stored),self.clock()))
                output.append(stored)
        return {"accepted":len(output),"reports":output}

    def history(self,user):
        with self.transaction() as db:
            return {"reports":[json.loads(r[0]) for r in db.execute("SELECT result FROM events WHERE user=? AND received>? ORDER BY received DESC,event LIMIT 100",(user,self.clock()-86400))]}

    def _traffic_record(self,db,edge,speed,closed,expires):
        if not db.execute("SELECT 1 FROM edges WHERE id=?",(edge,)).fetchone():
            raise Error(404,"traffic edge missing")
        if type(closed) is not bool or type(expires) not in (float,int) or not math.isfinite(expires):
            raise ValueError("invalid traffic state")
        if speed is not None:
            speed=positive(speed,100)
        if not closed and speed is None:
            raise ValueError("traffic speed required unless closed")
        db.execute("INSERT INTO traffic VALUES(?,?,?,?) ON CONFLICT(edge) DO UPDATE SET speed=excluded.speed,closed=excluded.closed,expires=excluded.expires",(edge,speed,int(closed),expires))

    def traffic(self,records,ttl=300):
        self.writable()
        ttl=positive(ttl,3600)
        if not isinstance(records,list) or not 1<=len(records)<=100:
            raise ValueError("traffic batch requires 1..100 edges")
        with self.transaction(True) as db:
            meta=self.ready(db)
            for record in records:
                self._traffic_record(db,record["edge"],record.get("speed_mps"),record.get("closed",False),self.clock()+ttl)
            self.set_meta(db,revision=meta["revision"]+1,traffic_version=meta["traffic_version"]+1)
            return {"revision":meta["revision"]+1,"traffic_version":meta["traffic_version"]+1}

    def aggregate(self,min_users=2):
        self.writable()
        if type(min_users) is not int or not 2<=min_users<=100:
            raise ValueError("at least two distinct users required")
        with self.transaction(True) as db:
            meta=self.ready(db)
            by_edge={}
            for row in db.execute("SELECT * FROM samples WHERE received>? ORDER BY received,event",(self.clock()-300,)):
                by_edge.setdefault(row["edge"],{})[row["user"]]=row["speed"]
            records=[]
            for edge,users in by_edge.items():
                if len(users)>=min_users:
                    speed=sum(users.values())/len(users)
                    self._traffic_record(db,edge,speed,False,self.clock()+300)
                    records.append({"edge":edge,"speed_mps":speed,"distinct_users":len(users)})
            if records:
                self.set_meta(db,revision=meta["revision"]+1,traffic_version=meta["traffic_version"]+1)
            return {"updated":records,"traffic_version":meta["traffic_version"]+int(bool(records))}
