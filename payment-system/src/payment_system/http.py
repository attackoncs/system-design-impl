import html
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import sqlite3
import time
from urllib.parse import urlsplit

from .common import Error,dumps,signature


def server(role,config,store,host,port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def auth(self,kind):
            supplied=self.headers.get("Authorization","")
            if kind=="merchant":
                for merchant,token in config["merchants"].items():
                    if hmac.compare_digest(supplied,"Bearer "+token):
                        return merchant
            elif hmac.compare_digest(supplied,"Bearer "+config[kind+"_token"]):
                return None
            raise Error(401,"valid role credential required")

        def body(self,raw=False):
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=1024*1024:
                raise Error(413,"JSON body requires 1..1048576 bytes")
            data=self.rfile.read(size)
            if len(data)!=size:
                raise ValueError("truncated request")
            return data if raw else json.loads(data)

        def dispatch(self):
            path,method=urlsplit(self.path).path,self.command
            if path=="/health" and method=="GET":
                return {"role":role,"simulated_payments_only":True}
            if role=="receiver":
                if path=="/events" and method=="POST":
                    self.auth("event")
                    return store.receive(self.body())
                self.auth("admin")
                if path=="/admin/status" and method=="GET":
                    return store.status()
                if path=="/admin/control" and method=="POST":
                    return store.control(self.body())
                raise Error(404,"unknown local receiver route")
            if role=="provider":
                if path.startswith("/checkout/"):
                    token=path.split("/")[-1]
                    if method=="POST":
                        body=self.body()
                        if not isinstance(body,dict) or set(body)!={"choice"}:
                            raise ValueError("simulated checkout only accepts choice; never card details")
                        return store.checkout(token,body["choice"])
                    if method=="GET":
                        receipt=store.checkout(token)
                        self.content_type="text/html; charset=utf-8"
                        return ("<!doctype html><html lang='en'><meta charset='utf-8'><title>Simulated checkout</title>"
                                "<body><h1>Simulated checkout</h1><p>No real funds or card details.</p><p>Amount: "+html.escape(str(receipt["amount"]))+
                                " USD cents</p><button data-choice='test-success'>Approve simulation</button>"
                                "<button data-choice='test-decline'>Decline simulation</button><pre id='result'></pre>"
                                "<script>document.querySelectorAll('button').forEach(b=>b.onclick=async()=>{let r=await fetch(location.href,{method:'POST',"
                                "headers:{'Content-Type':'application/json'},body:JSON.stringify({choice:b.dataset.choice})});"
                                "document.getElementById('result').textContent=await r.text()})</script></body></html>").encode()
                if path.startswith("/admin/"):
                    self.auth("admin")
                    if path=="/admin/control" and method=="POST":
                        return store.control(self.body())
                    if path=="/admin/status" and method=="GET":
                        return store.status()
                self.auth("provider")
                if path=="/operations" and method=="POST":
                    return store.create(self.body())
                if path.startswith("/operations/") and method=="GET":
                    return store.get(path.split("/")[-1])
                if path=="/statements" and method=="GET":
                    return store.statements()
                raise Error(404,"unknown provider route")
            if path=="/webhooks/mock" and method=="POST":
                raw=self.body(True)
                stamp=int(self.headers.get("X-Timestamp","0"))
                if abs(time.time()-stamp)>300 or not hmac.compare_digest(self.headers.get("X-Signature",""),signature(config["webhook_secret"],stamp,raw)):
                    raise Error(401,"webhook signature or timestamp invalid")
                body=json.loads(raw)
                if not isinstance(body,dict) or set(body)!={"event_id","receipt"}:
                    raise ValueError("invalid callback envelope")
                return store.apply(body["receipt"],body["event_id"])
            if path.startswith("/internal/"):
                self.auth("internal")
                if path.startswith("/internal/operation/") and method=="GET":
                    return store.operation(path.split("/")[-1])
                if method=="POST":
                    body=self.body()
                    if path=="/internal/claim":
                        return {"job":store.claim()}
                    if path=="/internal/finish":
                        return {"finished":store.finish_job(body["job"],body.get("pending",False),body.get("error"))}
                    if path=="/internal/apply":
                        return store.apply(body["receipt"])
                    if path=="/internal/reconcile":
                        return store.reconcile(body["run"],body["statement"])
                raise Error(404,"unknown internal route")
            if path.startswith("/admin/"):
                self.auth("admin")
                if path=="/admin/status" and method=="GET":
                    return store.status()
                if path=="/admin/reconcile" and method=="POST":
                    return store.schedule_reconcile()
                if path=="/admin/retry-job" and method=="POST":
                    body=self.body()
                    if not isinstance(body,dict) or set(body)!={"job_id"} or not isinstance(body["job_id"],str):
                        raise ValueError("job_id required")
                    return store.retry_job(body["job_id"])
                if path.startswith("/admin/reconciliations/") and method=="GET":
                    return store.reconciliation(path.split("/")[-1])
                if path.startswith("/admin/retry/") and method=="POST":
                    return store.retry(path.split("/")[-1])
                raise Error(404,"unknown administrator route")
            merchant=self.auth("merchant")
            if path=="/v1/wallet" and method=="GET":
                return store.wallet(merchant)
            if path=="/v1/ledger" and method=="GET":
                return store.ledger(merchant)
            if path.startswith("/v1/operations/") and method=="GET":
                return store.operation(path.split("/")[-1],merchant)
            if method=="POST" and path in ("/v1/payments","/v1/payouts","/v1/refunds"):
                kind={"/v1/payments":"payin","/v1/payouts":"payout","/v1/refunds":"refund"}[path]
                return store.create(merchant,kind,self.body())
            raise Error(404,"unknown merchant route")

        def handle_request(self):
            self.connection.settimeout(10)
            self.content_type="application/json"
            try:
                value,status=self.dispatch(),200
            except Error as exc:
                value,status={"error":str(exc)},exc.status
            except (ValueError,TypeError,KeyError) as exc:
                value,status={"error":str(exc)},400
            except (OSError,TimeoutError,sqlite3.OperationalError):
                value,status={"error":"service unavailable; retry using original request key"},503
            except Exception:
                value,status={"error":"internal failure"},500
            raw=value if isinstance(value,bytes) else dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type",self.content_type)
            self.send_header("Content-Length",str(len(raw)))
            self.send_header("Cache-Control","no-store")
            self.end_headers()
            self.wfile.write(raw)

        do_GET=handle_request
        do_POST=handle_request

    return ThreadingHTTPServer((host,port),Handler)
