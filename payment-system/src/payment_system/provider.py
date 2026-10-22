from abc import ABC,abstractmethod
import json
import time
import uuid
from urllib.error import HTTPError

from .common import Database,Error,digest,dumps,http_json,identifier,money,signature


class PaymentProvider(ABC):
    @abstractmethod
    def query(self,operation):
        pass

    @abstractmethod
    def create(self,operation):
        pass


class MockHTTPProvider(PaymentProvider):
    def __init__(self,config):
        self.url,self.token=config["provider_url"],config["provider_token"]

    def query(self,operation):
        return http_json(self.url+"/operations/"+identifier(operation),self.token)

    def create(self,operation):
        body={k:operation[k] for k in ("id","merchant","kind","parent","amount","currency","destination")}
        return http_json(self.url+"/operations",self.token,body)


class MockPSP(Database):
    """Synthetic provider with an independent ledger, hosted tokens and callback queue."""
    def __init__(self,path,config,clock=time.time):
        super().__init__(path)
        self.config,self.clock=config,clock
        self.initialize("""
        CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,hash TEXT,merchant TEXT,kind TEXT,parent TEXT,amount INTEGER,
            currency TEXT,provider_id TEXT,status TEXT,revision INTEGER,checkout TEXT,token TEXT UNIQUE,expires REAL);
        CREATE TABLE IF NOT EXISTS webhooks(id TEXT PRIMARY KEY,payload TEXT,status TEXT,attempts INTEGER,due REAL,error TEXT);
        CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),lose_response INTEGER,webhooks_down INTEGER,query_down INTEGER,statement_delta INTEGER);
        INSERT OR IGNORE INTO control VALUES(1,0,0,0,0);
        """)

    @staticmethod
    def receipt(row):
        return {k:row[k] for k in ("id","merchant","kind","parent","amount","currency","provider_id","status","revision","checkout")}

    def queue_event(self,db,row):
        payload=self.receipt(row)
        id="event_"+row["id"]+"_"+str(row["revision"])
        db.execute("INSERT OR IGNORE INTO webhooks VALUES(?,?,'pending',0,?,'')",(id,dumps(payload),self.clock()))

    def control(self,body=None):
        with self.transaction(True) as db:
            if body is not None:
                if not isinstance(body,dict) or set(body)!={"lose_response","webhooks_down","query_down","statement_delta"}:
                    raise ValueError("invalid mock control fields")
                if any(type(body[k]) is not bool for k in ("lose_response","webhooks_down","query_down")) or type(body["statement_delta"]) is not int or abs(body["statement_delta"])>1000000:
                    raise ValueError("invalid mock control values")
                db.execute("UPDATE control SET lose_response=?,webhooks_down=?,query_down=?,statement_delta=?",tuple(body[k] for k in ("lose_response","webhooks_down","query_down","statement_delta")))
            return dict(db.execute("SELECT * FROM control").fetchone())

    def create(self,body):
        if not isinstance(body,dict) or set(body)!={"id","merchant","kind","parent","amount","currency","destination"}:
            raise ValueError("invalid mock provider intent")
        identifier(body["id"])
        identifier(body["merchant"])
        money(body["amount"])
        if body["currency"]!="USD" or body["kind"] not in ("payin","payout","refund"):
            raise ValueError("unsupported provider intent")
        if body["destination"] not in ("hosted","test-refund","test-bank-ok","test-bank-decline"):
            raise ValueError("test token references only; no card/bank numbers")
        with self.transaction(True) as db:
            old=db.execute("SELECT * FROM operations WHERE id=?",(body["id"],)).fetchone()
            if old:
                if old["hash"]!=digest(body):
                    raise Error(409,"provider idempotency conflict")
                row=old
            else:
                if db.execute("SELECT count(*) FROM operations").fetchone()[0]>=10000:
                    raise Error(429,"mock provider reference capacity reached")
                if body["kind"]=="refund":
                    parent=db.execute("SELECT * FROM operations WHERE id=? AND kind='payin' AND status='SUCCEEDED'",(body["parent"],)).fetchone()
                    used=db.execute("SELECT coalesce(sum(amount),0) FROM operations WHERE parent=? AND status='SUCCEEDED'",(body["parent"],)).fetchone()[0]
                    if not parent or parent["merchant"]!=body["merchant"] or used+body["amount"]>parent["amount"]:
                        raise Error(409,"provider refund exceeds successful original charge")
                token=uuid.uuid4().hex
                checkout=self.config["provider_url"]+"/checkout/"+token if body["kind"]=="payin" else None
                status="PENDING" if body["kind"]=="payin" else ("FAILED" if body["destination"]=="test-bank-decline" else "SUCCEEDED")
                db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)",
                           (body["id"],digest(body),body["merchant"],body["kind"],body["parent"],body["amount"],"USD","mock_"+uuid.uuid4().hex,status,checkout,token,self.clock()+600))
                row=db.execute("SELECT * FROM operations WHERE id=?",(body["id"],)).fetchone()
                self.queue_event(db,row)
            result=self.receipt(row)
            lost=db.execute("SELECT lose_response FROM control").fetchone()[0]
        if lost:
            raise Error(503,"mock response lost after durable provider commit")
        return result

    def get(self,id):
        if self.control()["query_down"]:
            raise Error(503,"mock query endpoint unavailable")
        with self.transaction(True) as db:
            row=db.execute("SELECT * FROM operations WHERE id=?",(id,)).fetchone()
            if not row:
                raise Error(404,"provider operation missing")
            if row["status"]=="PENDING" and row["expires"]<=self.clock():
                db.execute("UPDATE operations SET status='FAILED',revision=revision+1 WHERE id=?",(id,))
                row=db.execute("SELECT * FROM operations WHERE id=?",(id,)).fetchone()
                self.queue_event(db,row)
            return self.receipt(row)

    def checkout(self,token,choice=None):
        identifier(token)
        with self.transaction(True) as db:
            row=db.execute("SELECT * FROM operations WHERE token=? AND kind='payin'",(token,)).fetchone()
            if not row:
                raise Error(404,"mock checkout session missing")
            if choice is not None:
                if choice not in ("test-success","test-decline"):
                    raise ValueError("checkout accepts only test-success/test-decline, never card fields")
                if row["status"]=="PENDING":
                    status="SUCCEEDED" if choice=="test-success" and row["expires"]>self.clock() else "FAILED"
                    db.execute("UPDATE operations SET status=?,revision=revision+1 WHERE id=?",(status,row["id"]))
                    row=db.execute("SELECT * FROM operations WHERE id=?",(row["id"],)).fetchone()
                    self.queue_event(db,row)
            result=self.receipt(row)
            lost=db.execute("SELECT lose_response FROM control").fetchone()[0]
        if choice is not None and lost:
            raise Error(503,"mock checkout response lost after durable commit")
        return result

    def statements(self):
        with self.transaction() as db:
            rows=[self.receipt(r) for r in db.execute("SELECT * FROM operations ORDER BY id")]
            delta=db.execute("SELECT statement_delta FROM control").fetchone()[0]
            if delta:
                row=next((r for r in rows if r["status"]=="SUCCEEDED"),None)
                if row:
                    row["amount"]+=delta
            return {"operations":rows,"snapshot":digest(rows),"simulated":True}

    def dispatch_once(self):
        if self.control()["webhooks_down"]:
            return False
        with self.transaction() as db:
            row=db.execute("SELECT * FROM webhooks WHERE status='pending' AND due<=? ORDER BY due,id LIMIT 1",(self.clock(),)).fetchone()
        if not row:
            return False
        body={"event_id":row["id"],"receipt":json.loads(row["payload"])}
        timestamp=int(self.clock())
        try:
            http_json(self.config["api_url"]+"/webhooks/mock",body=body,headers={"X-Timestamp":str(timestamp),"X-Signature":signature(self.config["webhook_secret"],timestamp,dumps(body).encode())})
            with self.transaction(True) as db:
                db.execute("UPDATE webhooks SET status='done',attempts=attempts+1,error='' WHERE id=?",(row["id"],))
        except Exception as exc:
            with self.transaction(True) as db:
                db.execute("UPDATE webhooks SET attempts=attempts+1,due=?,error=? WHERE id=?",(self.clock()+min(30,.1*2**min(row["attempts"],8)),str(exc)[:300],row["id"]))
        return True

    def status(self):
        with self.transaction() as db:
            return {"simulated":True,"unique_operations":db.execute("SELECT count(*) FROM operations").fetchone()[0],
                    "succeeded":db.execute("SELECT count(*) FROM operations WHERE status='SUCCEEDED'").fetchone()[0],
                    "pending_webhooks":db.execute("SELECT count(*) FROM webhooks WHERE status!='done'").fetchone()[0]}
