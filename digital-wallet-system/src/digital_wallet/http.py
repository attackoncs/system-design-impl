import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import sqlite3
import threading
from urllib.parse import parse_qs,urlsplit

from .common import Error,encode,integer


def server(role,store,config,host,port):
    gate=threading.RLock()
    if role=="shard":
        store.initialize("CREATE TABLE IF NOT EXISTS fault(id INTEGER PRIMARY KEY CHECK(id=1),lose_reply INTEGER,reject_prepare INTEGER); INSERT OR IGNORE INTO fault VALUES(1,0,0);")
    if role=="replica":
        store.initialize("CREATE TABLE IF NOT EXISTS fault(id INTEGER PRIMARY KEY CHECK(id=1),reject_append INTEGER); INSERT OR IGNORE INTO fault VALUES(1,0);")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def auth(self,kind):
            value=self.headers.get("Authorization","")
            if kind=="owner":
                for owner,token in config["owners"].items():
                    if hmac.compare_digest(value,"Bearer "+token):
                        return owner
            elif hmac.compare_digest(value,"Bearer "+config[kind+"_token"]):
                return
            raise Error(401,"role credential required")

        def body(self):
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=1024*1024:
                raise Error(413,"body must contain 1..1048576 bytes")
            raw=self.rfile.read(size)
            if len(raw)!=size:
                raise ValueError("truncated body")
            value=json.loads(raw)
            if not isinstance(value,dict):
                raise ValueError("object body required")
            return value

        def dispatch(self):
            parsed=urlsplit(self.path)
            path,method=parsed.path,self.command
            if path=="/health" and method=="GET":
                return {"role":role,"synthetic_funds_only":True}
            if path.startswith("/admin/"):
                self.auth("admin")
                if role in ("shard","replica") and path=="/admin/fence" and method=="POST":
                    body=self.body()
                    return store.fence(body["epoch"],body["leader"])
                if role=="replica" and path=="/admin/control" and method=="POST":
                    body=self.body()
                    if set(body)!={"reject_append"} or type(body["reject_append"]) is not bool:
                        raise ValueError("boolean reject_append required")
                    with store.transaction() as db:
                        db.execute("UPDATE fault SET reject_append=?",(body["reject_append"],))
                    return {"updated":True}
                if role=="shard":
                    if path=="/admin/snapshot" and method=="POST":
                        return store.snapshot()
                    if path=="/admin/control" and method=="POST":
                        body=self.body()
                        if set(body)!={"lose_reply","reject_prepare"} or any(type(value) is not bool for value in body.values()):
                            raise ValueError("boolean fault flags required")
                        with store.transaction() as db:
                            db.execute("UPDATE fault SET lose_reply=?,reject_prepare=?",(body["lose_reply"],body["reject_prepare"]))
                        return {"updated":True}
                if role=="coordinator":
                    if path=="/admin/status" and method=="GET":
                        return store.status()
                    if path=="/admin/audit" and method=="GET":
                        return store.audit()
                    if path=="/admin/sync" and method=="POST":
                        return store.journal.sync()
                    if path.startswith("/admin/retry/") and method=="POST":
                        return store.retry(path.split("/")[-1])
                    if path.startswith("/admin/cancel/") and method=="POST":
                        return store.cancel(path.split("/")[-1])
                raise Error(404,"unknown admin route")
            if path.startswith("/internal/"):
                self.auth("internal")
                if role=="replica":
                    if path=="/internal/status" and method=="POST":
                        body=self.body()
                        return store.status(body["epoch"],body["leader"])
                    if path=="/internal/append" and method=="POST":
                        body=self.body()
                        with store.connection() as db:
                            if db.execute("SELECT reject_append FROM fault").fetchone()[0]:
                                raise Error(503,"simulated replica append outage")
                        return store.append(body["entry"],body["epoch"],body["leader"])
                if role=="shard":
                    if path=="/internal/view" and method=="GET":
                        query=parse_qs(parsed.query,strict_parsing=True)
                        if set(query)-{"at"}:
                            raise ValueError("unknown query")
                        return store.view(integer(int(query["at"][0]),1) if "at" in query else None)
                    if path=="/internal/audit" and method=="GET":
                        return store.audit()
                    if path=="/internal/action" and method=="POST":
                        body=self.body()
                        with store.connection() as db:
                            fault=dict(db.execute("SELECT * FROM fault").fetchone())
                        if fault["reject_prepare"] and body["kind"]=="prepare":
                            raise Error(422,"simulated definitive prepare rejection")
                        result=store.action(body["intent"],body["kind"],body["role"],body["epoch"],body["leader"])
                        if fault["lose_reply"]:
                            raise Error(503,"simulated reply lost after durable participant commit")
                        return result
                if role=="coordinator" and path=="/internal/step" and method=="POST":
                    return store.step()
                raise Error(404,"unknown internal route")
            if role=="coordinator":
                owner=self.auth("owner")
                if path=="/v1/wallet/balance_transfer" and method=="POST":
                    return store.create(self.body(),owner)
                if path.startswith("/v1/transfers/") and method=="GET":
                    return store.owned(path.split("/")[-1],owner)
                if path.startswith("/v1/accounts/") and method=="GET":
                    query=parse_qs(parsed.query,strict_parsing=True)
                    if set(query)-{"at"}:
                        raise ValueError("unknown query")
                    return store.balance(path.split("/")[-1],owner,integer(int(query["at"][0]),1) if "at" in query else None)
            raise Error(404,"unknown route")

        def handle_request(self):
            self.connection.settimeout(10)
            try:
                with gate:
                    value=self.dispatch()
                status=200
            except Error as exc:
                value,status={"error":str(exc)},exc.status
            except (ValueError,KeyError,TypeError) as exc:
                value,status={"error":str(exc)},400
            except (OSError,TimeoutError,sqlite3.OperationalError):
                value,status={"error":"temporarily unavailable; retry original transaction_id"},503
            except Exception:
                value,status={"error":"internal failure"},500
            raw=encode(value).encode()
            self.send_response(status)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(raw)))
            self.send_header("Cache-Control","no-store")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):
                pass

        do_GET=handle_request
        do_POST=handle_request

    return ThreadingHTTPServer((host,port),Handler)
