import json
import time
import uuid

from .common import Database,Error,digest,dumps,identifier,integer,money


class Store(Database):
    def __init__(self,path,merchants=("seller",),clock=time.time):
        super().__init__(path)
        self.clock=clock
        self.initialize("""
        CREATE TABLE IF NOT EXISTS wallets(merchant TEXT PRIMARY KEY,available INTEGER NOT NULL CHECK(available>=0),held INTEGER NOT NULL CHECK(held>=0));
        CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,merchant TEXT,kind TEXT,order_id TEXT,parent TEXT,
            amount INTEGER NOT NULL CHECK(amount>0),currency TEXT NOT NULL CHECK(currency='USD'),destination TEXT,
            request_key TEXT,hash TEXT,status TEXT,provider_id TEXT,revision INTEGER,checkout TEXT,created REAL,
            UNIQUE(merchant,request_key),UNIQUE(merchant,kind,order_id),FOREIGN KEY(merchant) REFERENCES wallets(merchant));
        CREATE TABLE IF NOT EXISTS ledger(id TEXT PRIMARY KEY,operation TEXT,kind TEXT,provider_id TEXT,posted INTEGER DEFAULT 0,created REAL,
            FOREIGN KEY(operation) REFERENCES operations(id));
        CREATE TABLE IF NOT EXISTS lines(tx TEXT,line INTEGER,account TEXT,debit INTEGER NOT NULL,credit INTEGER NOT NULL,
            CHECK((debit>0 AND credit=0) OR (credit>0 AND debit=0)),PRIMARY KEY(tx,line),FOREIGN KEY(tx) REFERENCES ledger(id));
        CREATE TRIGGER IF NOT EXISTS ledger_start_unposted BEFORE INSERT ON ledger WHEN NEW.posted!=0 BEGIN SELECT RAISE(ABORT,'ledger must start unposted'); END;
        CREATE TRIGGER IF NOT EXISTS finalize_balanced BEFORE UPDATE OF posted ON ledger WHEN NEW.posted=1 AND OLD.posted=0
        BEGIN SELECT CASE WHEN (SELECT count(*) FROM lines WHERE tx=OLD.id)<2 OR
            (SELECT sum(debit)-sum(credit) FROM lines WHERE tx=OLD.id)!=0 THEN RAISE(ABORT,'unbalanced ledger') END; END;
        CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger WHEN OLD.posted=1 BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
        CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger WHEN OLD.posted=1 BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
        CREATE TRIGGER IF NOT EXISTS lines_no_insert BEFORE INSERT ON lines WHEN (SELECT posted FROM ledger WHERE id=NEW.tx)=1 BEGIN SELECT RAISE(ABORT,'immutable lines'); END;
        CREATE TRIGGER IF NOT EXISTS lines_no_update BEFORE UPDATE ON lines WHEN (SELECT posted FROM ledger WHERE id=OLD.tx)=1 OR (SELECT posted FROM ledger WHERE id=NEW.tx)=1 BEGIN SELECT RAISE(ABORT,'immutable lines'); END;
        CREATE TRIGGER IF NOT EXISTS lines_no_delete BEFORE DELETE ON lines WHEN (SELECT posted FROM ledger WHERE id=OLD.tx)=1 BEGIN SELECT RAISE(ABORT,'immutable lines'); END;
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,payload TEXT,status TEXT,attempts INTEGER,failures INTEGER,due REAL,lease REAL,token TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS executions(seq INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT,action TEXT,detail TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,hash TEXT,result TEXT);
        CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,operation TEXT,reason TEXT,detail TEXT,state TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS reconciliations(id TEXT PRIMARY KEY,status TEXT,result TEXT,created REAL);
        """)
        with self.transaction(True) as db:
            for merchant in merchants:
                db.execute("INSERT OR IGNORE INTO wallets VALUES(?,0,0)",(identifier(merchant),))

    def operation(self,op,merchant=None,db=None):
        if db is None:
            with self.transaction() as connection:
                return self.operation(op,merchant,connection)
        row=db.execute("SELECT * FROM operations WHERE id=?",(op,)).fetchone()
        if not row or (merchant is not None and row["merchant"]!=merchant):
            raise Error(404,"operation missing")
        value=dict(row)
        value.pop("hash")
        return value

    def enqueue(self,db,id,kind,payload):
        db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?,'pending',0,0,?,0,'','')",(id,kind,dumps(payload),self.clock()))

    def review(self,db,op,reason,detail):
        id=digest([op,reason,detail])
        db.execute("INSERT OR IGNORE INTO reviews VALUES(?,?,?,?,'open',?)",(id,op,reason,dumps(detail),self.clock()))
        return id

    def post(self,db,id,op,kind,debit,credit,amount,provider=None):
        old=db.execute("SELECT * FROM ledger WHERE id=?",(id,)).fetchone()
        if old:
            return False
        db.execute("INSERT INTO ledger VALUES(?,?,?,?,0,?)",(id,op,kind,provider,self.clock()))
        db.executemany("INSERT INTO lines VALUES(?,?,?,?,?)",[(id,1,debit,amount,0),(id,2,credit,0,amount)])
        for account,delta in ((debit,-amount),(credit,amount)):
            if account.startswith("merchant:"):
                _,merchant,field=account.split(":")
                if field not in ("available","held"):
                    raise ValueError("invalid wallet ledger account")
                cursor=db.execute(f"UPDATE wallets SET {field}={field}+? WHERE merchant=? AND {field}+?>=0",(delta,merchant,delta))
                if not cursor.rowcount:
                    raise Error(409,"insufficient available/held merchant funds")
        db.execute("UPDATE ledger SET posted=1 WHERE id=?",(id,))
        return True

    def create(self,merchant,kind,body,before_commit=None):
        identifier(merchant)
        fields={"request_key","amount","currency"}|({"order_id"} if kind=="payin" else {"destination"} if kind=="payout" else {"payment_id"})
        if kind not in ("payin","payout","refund") or not isinstance(body,dict) or set(body)!=fields:
            raise ValueError("invalid operation fields; no card or bank data accepted")
        key=identifier(body["request_key"])
        amount=money(body["amount"])
        if body["currency"]!="USD":
            raise ValueError("only USD integer cents supported")
        order=identifier(body["order_id"]) if kind=="payin" else None
        parent=identifier(body["payment_id"]) if kind=="refund" else None
        destination=body.get("destination","hosted" if kind=="payin" else "test-refund")
        if kind=="payout" and destination not in ("test-bank-ok","test-bank-decline"):
            raise ValueError("only simulated destination token references supported")
        fingerprint=digest([kind,body])
        with self.transaction(True) as db:
            old=db.execute("SELECT * FROM operations WHERE merchant=? AND request_key=?",(merchant,key)).fetchone()
            if old:
                if old["hash"]!=fingerprint:
                    raise Error(409,"request key content conflict")
                return self.operation(old["id"],merchant,db)
            if order and db.execute("SELECT 1 FROM operations WHERE merchant=? AND kind='payin' AND order_id=?",(merchant,order)).fetchone():
                raise Error(409,"order already has a payment; reuse its original request key")
            if not db.execute("SELECT 1 FROM wallets WHERE merchant=?",(merchant,)).fetchone():
                raise Error(404,"merchant missing")
            if parent:
                source=self.operation(parent,merchant,db)
                if source["kind"]!="payin" or source["status"]!="SUCCEEDED":
                    raise Error(409,"refund requires a successful owned pay-in")
                used=db.execute("SELECT coalesce(sum(amount),0) FROM operations WHERE parent=? AND status!='FAILED'",(parent,)).fetchone()[0]
                if used+amount>source["amount"]:
                    raise Error(409,"refund amount exceeds original payment after pending refunds")
            op=uuid.uuid4().hex
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,'CREATED',NULL,0,NULL,?)",
                       (op,merchant,kind,order,parent,amount,"USD",destination,key,fingerprint,self.clock()))
            if kind!="payin":
                self.post(db,"reserve:"+op,op,"reserve","merchant:"+merchant+":available","merchant:"+merchant+":held",amount)
            self.enqueue(db,"execute:"+op,"execute",{"operation":op})
            if before_commit:
                before_commit()
            return self.operation(op,merchant,db)

    def apply(self,receipt,event_id=None):
        required={"id","merchant","kind","parent","amount","currency","provider_id","status","revision","checkout"}
        if not isinstance(receipt,dict) or set(receipt)!=required:
            raise ValueError("invalid PSP receipt")
        integer(receipt["revision"],"provider revision",1)
        money(receipt["amount"])
        for field in ("id","merchant","provider_id"):
            identifier(receipt[field])
        if receipt["parent"] is not None:
            identifier(receipt["parent"])
        if receipt["status"] not in ("PENDING","SUCCEEDED","FAILED"):
            raise ValueError("invalid provider status")
        fingerprint=digest(receipt)
        with self.transaction(True) as db:
            if event_id:
                old=db.execute("SELECT * FROM inbox WHERE id=?",(identifier(event_id),)).fetchone()
                if old:
                    if old["hash"]!=fingerprint:
                        raise Error(409,"webhook event ID payload conflict")
                    return {**json.loads(old["result"]),"duplicate":True}
            row=db.execute("SELECT * FROM operations WHERE id=?",(receipt["id"],)).fetchone()
            result={"applied":False,"duplicate":False}
            if not row:
                result["review"]=self.review(db,receipt["id"],"UNKNOWN_PROVIDER_OPERATION",receipt)
            else:
                frozen=(row["merchant"],row["kind"],row["parent"],row["amount"],row["currency"])
                incoming=tuple(receipt[k] for k in ("merchant","kind","parent","amount","currency"))
                if frozen!=incoming or (row["provider_id"] and row["provider_id"]!=receipt["provider_id"]):
                    result["review"]=self.review(db,row["id"],"RECEIPT_MISMATCH",receipt)
                elif receipt["revision"]<row["revision"]:
                    result["ignored"]="older revision"
                elif row["status"] in ("SUCCEEDED","FAILED"):
                    if receipt["status"]!=row["status"]:
                        result["review"]=self.review(db,row["id"],"TERMINAL_CONFLICT",receipt)
                    else:
                        result["ignored"]="already terminal"
                elif receipt["revision"]==row["revision"] and row["status"] not in ("UNKNOWN","CREATED"):
                    result["ignored"]="already applied revision"
                else:
                    op,merchant=row["id"],row["merchant"]
                    if receipt["status"]=="SUCCEEDED":
                        debit="psp:clearing" if row["kind"]=="payin" else "merchant:"+merchant+":held"
                        credit="merchant:"+merchant+":available" if row["kind"]=="payin" else "psp:clearing"
                        self.post(db,"settle:"+op,op,"settle",debit,credit,row["amount"],receipt["provider_id"])
                    elif receipt["status"]=="FAILED" and row["kind"]!="payin":
                        self.post(db,"release:"+op,op,"release","merchant:"+merchant+":held","merchant:"+merchant+":available",row["amount"],receipt["provider_id"])
                    db.execute("UPDATE operations SET status=?,provider_id=?,revision=?,checkout=? WHERE id=?",
                               (receipt["status"],receipt["provider_id"],receipt["revision"],receipt["checkout"],op))
                    if receipt["status"]!="PENDING":
                        db.execute("UPDATE jobs SET status='done' WHERE id=?",("execute:"+op,))
                        self.enqueue(db,"notify:"+op,"notify",{"event_id":"terminal_"+op,"operation":op,"merchant":merchant,"status":receipt["status"],"amount":row["amount"],"currency":"USD"})
                    result["applied"]=True
            if event_id:
                db.execute("INSERT INTO inbox VALUES(?,?,?)",(event_id,fingerprint,dumps(result)))
            return result

    def claim(self):
        with self.transaction(True) as db:
            now=self.clock()
            row=db.execute("SELECT * FROM jobs WHERE (status='pending' AND due<=?) OR (status='inflight' AND lease<=?) ORDER BY due,id LIMIT 1",(now,now)).fetchone()
            if not row:
                return None
            token=uuid.uuid4().hex
            db.execute("UPDATE jobs SET status='inflight',attempts=attempts+1,lease=?,token=? WHERE id=?",(now+2,token,row["id"]))
            db.execute("INSERT INTO executions(job,action,detail,created) VALUES(?,'claim',?,?)",(row["id"],token,now))
            return {**dict(row),"token":token,"payload":json.loads(row["payload"])}

    def finish_job(self,job,pending=False,error=None):
        with self.transaction(True) as db:
            row=db.execute("SELECT * FROM jobs WHERE id=? AND token=? AND status='inflight'",(job["id"],job["token"])).fetchone()
            if not row:
                return False
            failures=row["failures"]+int(error is not None)
            payload=json.loads(row["payload"])
            status="pending" if pending or error is not None else "done"
            delay=.2 if pending and error is None else min(30,.1*2**min(failures,8))
            if error is not None and row["kind"]=="execute":
                db.execute("UPDATE operations SET status='UNKNOWN' WHERE id=? AND status NOT IN('SUCCEEDED','FAILED')",(payload["operation"],))
            if failures>=5:
                status="dead"
                self.review(db,payload.get("operation",row["id"]),"RETRY_EXHAUSTED",{"job":row["id"],"error":str(error)[:300]})
                if row["kind"]=="reconcile":
                    db.execute("UPDATE reconciliations SET status='failed',result=? WHERE id=?",(dumps({"error":str(error)[:300]}),payload["run"]))
            db.execute("UPDATE jobs SET status=?,failures=?,due=?,error=? WHERE id=?",(status,failures,self.clock()+delay,str(error)[:300] if error else "",row["id"]))
            db.execute("INSERT INTO executions(job,action,detail,created) VALUES(?,?,?,?)",(row["id"],status,str(error)[:300] if error else "",self.clock()))
        return True

    def retry(self,op):
        with self.transaction(True) as db:
            value=self.operation(op,db=db)
            if value["status"] in ("SUCCEEDED","FAILED"):
                raise Error(409,"terminal operation cannot execute again")
            db.execute("UPDATE jobs SET status='pending',failures=0,due=?,lease=0,token='' WHERE id=?",(self.clock(),"execute:"+op))
            return {"retry_requested":op}

    def retry_job(self,id):
        """Requeue a dead job with its original operation/event identity."""
        with self.transaction(True) as db:
            row=db.execute("SELECT * FROM jobs WHERE id=?",(id,)).fetchone()
            if not row:
                raise Error(404,"job missing")
            if row["status"]!="dead":
                raise Error(409,"only dead jobs can be manually requeued")
            db.execute("UPDATE jobs SET status='pending',failures=0,due=?,lease=0,token='',error='' WHERE id=?",(self.clock(),id))
            if row["kind"]=="reconcile":
                db.execute("UPDATE reconciliations SET status='pending',result=NULL WHERE id=?",(json.loads(row["payload"])["run"],))
        return {"retry_requested":id}

    def wallet(self,merchant):
        with self.transaction() as db:
            row=db.execute("SELECT * FROM wallets WHERE merchant=?",(merchant,)).fetchone()
            if not row:
                raise Error(404,"merchant missing")
            return {**dict(row),"currency":"USD"}

    def audit(self,db):
        errors=[]
        totals=dict(db.execute("SELECT l.account,sum(l.credit-l.debit) FROM lines l JOIN ledger t ON t.id=l.tx WHERE t.posted=1 GROUP BY l.account").fetchall())
        for row in db.execute("SELECT * FROM wallets"):
            for field in ("available","held"):
                if row[field]!=totals.get("merchant:"+row["merchant"]+":"+field,0):
                    errors.append({"merchant":row["merchant"],"field":field,"reason":"WALLET_LEDGER_MISMATCH"})
        imbalance=db.execute("SELECT coalesce(sum(l.debit-l.credit),0) FROM lines l JOIN ledger t ON t.id=l.tx WHERE t.posted=1").fetchone()[0]
        if imbalance or db.execute("SELECT count(*) FROM ledger WHERE posted=0").fetchone()[0]:
            errors.append({"reason":"UNBALANCED_OR_UNPOSTED_LEDGER"})
        cash=-totals.get("psp:clearing",0)
        liability=db.execute("SELECT coalesce(sum(available+held),0) FROM wallets").fetchone()[0]
        if cash!=liability:
            errors.append({"reason":"ASSET_LIABILITY_MISMATCH"})
        return {"matches":not errors,"cash":cash,"merchant_liability":liability,"errors":errors}

    def schedule_reconcile(self):
        id=uuid.uuid4().hex
        with self.transaction(True) as db:
            db.execute("INSERT INTO reconciliations VALUES(?,'pending',NULL,?)",(id,self.clock()))
            self.enqueue(db,"reconcile:"+id,"reconcile",{"run":id})
        return {"run":id,"status":"pending"}

    def reconcile(self,id,statement):
        differences=[]
        provider={r["id"]:r for r in statement["operations"]}
        with self.transaction(True) as db:
            for row in db.execute("SELECT * FROM operations").fetchall():
                receipt=provider.pop(row["id"],None)
                reason=None
                if receipt is None and row["status"]=="SUCCEEDED":
                    reason="MISSING_PROVIDER_TRANSACTION"
                elif receipt:
                    if tuple(row[k] for k in ("merchant","kind","parent","amount","currency"))!=tuple(receipt[k] for k in ("merchant","kind","parent","amount","currency")):
                        reason="STATEMENT_AMOUNT_OR_IDENTITY_MISMATCH"
                    elif row["status"]!=receipt["status"]:
                        reason="PROVIDER_AHEAD_OR_STATUS_MISMATCH"
                    elif row["provider_id"] and row["provider_id"]!=receipt["provider_id"]:
                        reason="PROVIDER_ID_MISMATCH"
                settled=db.execute("SELECT 1 FROM ledger WHERE operation=? AND kind='settle' AND posted=1",(row["id"],)).fetchone()
                if bool(settled)!=(row["status"]=="SUCCEEDED"):
                    reason="OPERATION_LEDGER_MISMATCH"
                if reason:
                    detail={"local_status":row["status"],"provider":receipt}
                    differences.append({"operation":row["id"],"reason":reason,"review":self.review(db,row["id"],reason,detail)})
            for op,receipt in provider.items():
                differences.append({"operation":op,"reason":"ORPHAN_PROVIDER_OPERATION","review":self.review(db,op,"ORPHAN_PROVIDER_OPERATION",receipt)})
            audit=self.audit(db)
            for error in audit["errors"]:
                differences.append({**error,"review":self.review(db,"internal",error["reason"],error)})
            result={"matches":not differences,"differences":differences,"audit":audit,"provider_snapshot":statement["snapshot"]}
            db.execute("UPDATE reconciliations SET status='done',result=? WHERE id=?",(dumps(result),id))
            return result

    def status(self):
        with self.transaction() as db:
            return {"operations":db.execute("SELECT count(*) FROM operations").fetchone()[0],
                    "pending_jobs":db.execute("SELECT count(*) FROM jobs WHERE status IN('pending','inflight')").fetchone()[0],
                    "dead_jobs":db.execute("SELECT count(*) FROM jobs WHERE status='dead'").fetchone()[0],
                    "reviews":[dict(r) for r in db.execute("SELECT * FROM reviews WHERE state='open' ORDER BY created LIMIT 100")],"audit":self.audit(db)}

    def reconciliation(self,id):
        with self.transaction() as db:
            row=db.execute("SELECT * FROM reconciliations WHERE id=?",(id,)).fetchone()
            if not row:
                raise Error(404,"reconciliation run missing")
            return {"run":id,"status":row["status"],"result":json.loads(row["result"]) if row["result"] else None}

    def ledger(self,merchant):
        with self.transaction() as db:
            rows=[]
            for row in db.execute("SELECT l.* FROM ledger l JOIN operations o ON o.id=l.operation WHERE o.merchant=? AND l.posted=1 ORDER BY l.created,l.id LIMIT 101",(merchant,)).fetchall():
                rows.append({**dict(row),"lines":[dict(r) for r in db.execute("SELECT account,debit,credit FROM lines WHERE tx=? ORDER BY line",(row["id"],))]})
            if len(rows)>100:
                raise Error(422,"ledger view exceeds reference response limit; use offline audit")
            return {"transactions":rows,"currency":"USD"}
