import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading
from urllib.error import HTTPError,URLError
from urllib.parse import parse_qs,urlsplit
from urllib.request import Request,urlopen

from .navigation import route
from .render import render_tile,routing_tile
from .store import Error,dumps

MAX_CATALOG=64*1024*1024


def fetch(url,method="GET",body=None,token=None):
    headers={"Content-Type":"application/json"}
    if token:
        headers["Authorization"]="Bearer "+token
    data=dumps(body).encode() if body is not None else None
    with urlopen(Request(url,data=data,headers=headers,method=method),timeout=3) as response:
        payload=response.read(MAX_CATALOG+1)
        if len(payload)>MAX_CATALOG:
            raise ValueError("catalog/response exceeds 64 MiB reference limit")
        return payload,response.headers.get("Content-Type","application/json"),response.headers.get("Cache-Control","no-store")


class Replicator:
    def __init__(self,store,upstream,token,interval=1):
        self.store,self.upstream,self.token,self.interval=store,upstream.rstrip("/"),token,interval
        self.stop=threading.Event()
        self.error=None
        self.thread=threading.Thread(target=self.run,daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                payload,_,_=fetch(self.upstream+"/internal/catalog",token=self.token)
                self.store.install_snapshot(json.loads(payload))
                self.error=None
            except Exception:
                self.error="catalog synchronization failed"
            self.stop.wait(self.interval)


class Gateway:
    def __init__(self,regions):
        if not isinstance(regions,dict) or not regions:
            raise ValueError("region map required")
        for peers in regions.values():
            if not isinstance(peers,list) or not peers:
                raise ValueError("nonempty peer lists required")
            for peer in peers:
                parsed=urlsplit(peer)
                if parsed.scheme not in ("http","https") or not parsed.netloc or parsed.query or parsed.fragment:
                    raise ValueError("invalid regional peer URL")
        self.regions,self.counter,self.lock=regions,0,threading.Lock()

    def read(self,region,path,method,body):
        if region not in self.regions:
            raise Error(400,"known X-Region required")
        peers=self.regions[region]
        with self.lock:
            start=self.counter%len(peers)
            self.counter+=1
        for index in range(len(peers)):
            try:
                return fetch(peers[(start+index)%len(peers)].rstrip("/")+path,method,body)
            except HTTPError as exc:
                if exc.code<500:
                    raise Error(exc.code,"regional peer rejected request")
            except (OSError,URLError,ValueError):
                pass
        raise Error(503,"no peer available in requested region")


def server(host,port,store=None,replicator=None,gateway=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version="HTTP/1.1"

        def log_message(self,*args):
            pass

        def auth(self,admin=False):
            header=self.headers.get("Authorization","")
            if not header.startswith("Bearer "):
                raise Error(401,"Bearer required")
            return store.authenticate(header[7:],admin)

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise Error(400,"Transfer-Encoding unsupported")
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=2*1024*1024:
                raise Error(413,"request body must be 1 byte..2 MiB")
            result=json.loads(self.rfile.read(size))
            if not isinstance(result,dict):
                raise ValueError("JSON object required")
            return result

        def dispatch(self):
            parsed=urlsplit(self.path)
            path=parsed.path
            params=parse_qs(parsed.query,keep_blank_values=True)
            if any(len(v)!=1 for v in params.values()):
                raise ValueError("duplicate parameter")
            params={k:v[0] for k,v in params.items()}
            body=self.body() if self.command=="POST" else None
            if path=="/health" and self.command=="GET":
                return {"role":"gateway","regions":list(gateway.regions)} if gateway else {**store.health(),"replication_error":replicator.error if replicator else None}
            public_get=path.startswith("/v1/tiles/") or path.startswith("/v1/routing-tiles/") or path in ("/v1/geocode","/v1/reverse")
            if gateway:
                if self.command=="GET" and public_get or self.command=="POST" and path=="/v1/route":
                    return gateway.read(self.headers.get("X-Region"),self.path,self.command,body)
                raise Error(403,"gateway only forwards public map reads")
            if path=="/internal/catalog" and self.command=="GET":
                self.auth(admin=True)
                return store.export()
            if path=="/v1/route" and self.command=="POST":
                return route(store,**body)
            if path=="/v1/geocode" and self.command=="GET":
                return store.geocode(params["address"])
            if path=="/v1/reverse" and self.command=="GET":
                return store.geocode({"latitude":float(params["latitude"]),"longitude":float(params["longitude"])},reverse=True)
            if self.command=="GET" and path.startswith("/v1/tiles/"):
                graph,z,x,name=path[len("/v1/tiles/"):].split("/")
                y,format=name.split(".")
                payload,content_type=render_tile(store,graph,int(z),int(x),int(y),format)
                return payload,content_type,"public, max-age=31536000, immutable"
            if self.command=="GET" and path.startswith("/v1/routing-tiles/"):
                graph,x,y=path[len("/v1/routing-tiles/"):].split("/")
                return dumps(routing_tile(store,graph,int(x),int(y))).encode(),"application/json","public, max-age=31536000, immutable"
            if path=="/v1/locations" and self.command=="POST":
                return store.ingest(self.auth(),body["reports"])
            if path=="/v1/locations" and self.command=="GET":
                return store.history(self.auth())
            if path=="/internal/traffic" and self.command=="POST":
                self.auth(admin=True)
                return store.traffic(**body)
            if path=="/internal/aggregate" and self.command=="POST":
                self.auth(admin=True)
                return store.aggregate(**body)
            if path=="/internal/graph" and self.command=="POST":
                self.auth(admin=True)
                return store.import_graph(body)
            raise Error(404,"route not found")

        def handle_request(self):
            self.connection.settimeout(10)
            status=200
            try:
                result=self.dispatch()
                payload,content_type,cache=result if isinstance(result,tuple) else (dumps(result).encode(),"application/json","no-store")
            except Error as exc:
                status,payload,content_type,cache=exc.status,dumps({"error":exc.message}).encode(),"application/json","no-store"
            except (ValueError,TypeError,KeyError,OverflowError):
                status,payload,content_type,cache=400,b'{"error":"invalid request"}',"application/json","no-store"
            except Exception:
                status,payload,content_type,cache=500,b'{"error":"internal error"}',"application/json","no-store"
            etag='"'+hashlib.sha256(payload).hexdigest()+'"'
            if status==200 and self.command=="GET" and cache!="no-store" and self.headers.get("If-None-Match")==etag:
                status,payload=304,b""
            self.send_response(status)
            self.send_header("Content-Type",content_type)
            self.send_header("Content-Length",str(len(payload)))
            self.send_header("Cache-Control",cache)
            self.send_header("ETag",etag)
            self.send_header("Connection","close")
            self.end_headers()
            self.close_connection=True
            try:
                self.wfile.write(payload)
            except OSError:
                pass

        do_GET=do_POST=handle_request
    httpd=ThreadingHTTPServer((host,port),Handler)
    httpd.daemon_threads=True
    return httpd
