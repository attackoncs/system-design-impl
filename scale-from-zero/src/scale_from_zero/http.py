import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import sqlite3
import threading
from urllib.parse import parse_qs,urlsplit

from .app import ASSETS
from .common import Error,encode,identifier,integer,request
from .store import partition


def server(role,store,config,index,host,port):
    gate=threading.RLock()
    stats={"requests":0,"failovers":0,"next":0}
    cache_gate=threading.RLock()
    if role=="edge":
        from .cache import Cache
        edge_cache=Cache(ttl=30)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def auth(self,kind):
            if not hmac.compare_digest(self.headers.get("Authorization",""),"Bearer "+config[kind+"_token"]):
                raise Error(401,"role credential required")

        def body(self):
            if hasattr(self,"parsed_body"):
                return self.parsed_body
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=65536:
                raise Error(413,"body requires 1..65536 bytes")
            raw=self.rfile.read(size)
            value=json.loads(raw)
            if len(raw)!=size or not isinstance(value,dict):
                raise ValueError("complete JSON object required")
            self.parsed_body=value
            return value

        def dispatch(self):
            parsed=urlsplit(self.path)
            path,method=parsed.path,self.command
            query=parse_qs(parsed.query,strict_parsing=True)
            with gate:
                stats["requests"]+=1
            if path=="/health" and method=="GET":
                return {"role":role,"index":index,"single_host_lab":True}
            if path=="/admin/status" and method=="GET":
                self.auth("admin")
                if role in ("primary","replica","worker"):
                    return store.status()
                if role=="cache":
                    with cache_gate:
                        return store.status()
                if role in ("web","single"):
                    return {"web_node":store.node,"cache_errors":store.cache_errors,**(store.store.status() if role=="single" else {})}
                if role=="edge":
                    with cache_gate:
                        return edge_cache.status()
                with gate:
                    return {key:stats[key] for key in ("requests","failovers")}
            if role=="worker" and path=="/admin/control" and method=="POST":
                self.auth("admin")
                body=self.body()
                if set(body)!={"lose_ack"}:
                    raise ValueError("lose_ack required")
                return store.control(body["lose_ack"])
            if path.startswith("/internal/"):
                self.auth("internal")
                if role in ("primary","replica"):
                    if path=="/internal/changes" and method=="GET":
                        return store.changes(integer(int(query.get("after",["0"])[0])))
                    if method=="POST":
                        body=self.body()
                        if path in ("/internal/get","/internal/update","/internal/session_create","/internal/session_check"):
                            user=identifier(body["user"])
                            if partition(user,len(config["primaries"]))!=index:
                                raise Error(409,"wrong database shard")
                            if path=="/internal/get":
                                return store.get(user)
                            if role!="primary":
                                raise Error(403,"replica is read-only")
                            if path=="/internal/update":
                                return store.update(user,body["profile"])
                            if path=="/internal/session_create":
                                return store.session_create(user)
                            return store.session_check(user,body["token"])
                        if role=="primary" and path=="/internal/claim":
                            return {"job":store.claim()}
                        if role=="primary" and path=="/internal/ack":
                            return store.ack(body["job"])
                if role=="cache" and method=="POST":
                    body=self.body()
                    key=identifier(body["key"])
                    with cache_gate:
                        if path=="/internal/get":
                            return {"value":store.get(key,integer(body.get("min_version",0)))}
                        if path=="/internal/put":
                            store.put(key,body["value"])
                            return {"stored":True}
                        if path=="/internal/delete":
                            store.delete(key)
                            return {"deleted":True}
                raise Error(404,"internal route missing")
            if role=="lb":
                if path not in ("/v1/login","/v1/profile") and not path.startswith("/assets/"):
                    raise Error(404,"load balancer route missing")
                body=self.body() if method=="POST" else None
                safe=method=="GET" or body is not None and isinstance(body.get("request_id"),str)
                with gate:
                    start=stats["next"]
                    stats["next"]=(start+1)%len(config["webs"])
                token=self.headers.get("Authorization","")
                last=None
                for offset in range(len(config["webs"]) if safe else 1):
                    url=config["webs"][(start+offset)%len(config["webs"])]
                    try:
                        return request(url+self.path,token[7:] if token.startswith("Bearer ") else "",body,timeout=1.5)
                    except Error as exc:
                        if exc.status<500:
                            raise
                        last=exc
                    except (OSError,TimeoutError) as exc:
                        last=exc
                    with gate:
                        stats["failovers"]+=1
                raise Error(503,"no eligible web node available: "+type(last).__name__)
            if role=="edge" and path.startswith("/assets/") and method=="GET":
                key=path.rsplit("/",1)[1]
                if key not in ASSETS:
                    raise Error(404,"unknown versioned asset")
                with cache_gate:
                    value=edge_cache.get(key)
                if value is None:
                    value=request(config["entry"]+path,"",timeout=1.5)
                    with cache_gate:
                        edge_cache.put(key,value)
                return {**value,"edge_model":"local cache; not a real CDN"}
            if role in ("web","single"):
                if path.startswith("/assets/") and method=="GET":
                    key=path.rsplit("/",1)[1]
                    if key not in ASSETS:
                        raise Error(404,"asset missing")
                    return {"asset":key,"content":ASSETS[key]}
                if path=="/v1/login" and method=="POST":
                    return store.login(self.body())
                if path=="/v1/profile":
                    user=store.authorize(self.headers.get("Authorization",""))
                    if method=="POST":
                        return store.update(user,self.body())
                    if method=="GET":
                        consistency=query.get("consistency",["eventual"])[0]
                        if consistency not in ("strong","eventual"):
                            raise ValueError("consistency must be strong or eventual")
                        return store.get(user,consistency=="strong",integer(int(query.get("min_version",["0"])[0])))
            raise Error(404,"route missing")

        def handle_request(self):
            self.connection.settimeout(10)
            try:
                value,status=self.dispatch(),200
            except Error as exc:
                value,status={"error":str(exc)},exc.status
            except (ValueError,KeyError,TypeError) as exc:
                value,status={"error":str(exc)},400
            except (OSError,TimeoutError,sqlite3.OperationalError):
                value,status={"error":"upstream unavailable; retry original request_id for writes"},503
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
