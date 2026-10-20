import base64
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import sqlite3
from urllib.error import HTTPError,URLError
from urllib.parse import parse_qs,urlencode,urlsplit

from .common import Error, dumps, http_json, shard_for, text
from .mail import send
from .objects import Objects


def server(role,config,store,host,port,state=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def auth(self,role="user"):
            supplied=self.headers.get("Authorization","")
            if role!="user":
                if hmac.compare_digest(supplied,"Bearer "+config[role+"_token"]):
                    return None
            else:
                for user,token in config["users"].items():
                    if hmac.compare_digest(supplied,"Bearer "+token):
                        return user
            raise Error(401,"valid role credential required")

        def body(self):
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=16*1024*1024:
                raise Error(413,"JSON body must contain 1..16777216 bytes")
            return json.loads(self.rfile.read(size))

        def dispatch(self):
            target=urlsplit(self.path)
            path,method=target.path,self.command
            params=parse_qs(target.query,keep_blank_values=True)
            if any(len(v)!=1 for v in params.values()):
                raise ValueError("repeated parameter")
            params={k:v[0] for k,v in params.items()}
            if path=="/health" and method=="GET":
                return {"role":role,"local_only":True}
            if role=="objects":
                self.auth("internal")
                if path=="/objects" and method=="POST":
                    body=self.body()
                    if not isinstance(body,dict) or set(body)!={"data"}:
                        raise ValueError("object requires base64 data")
                    return store.put(base64.b64decode(body["data"],validate=True))
                if path.startswith("/objects/") and method=="GET":
                    data=store.get(path[len("/objects/"):])
                    return {"data":base64.b64encode(data).decode(),"size":len(data)}
                raise Error(404,"unknown object route")
            if role=="smtp":
                self.auth("admin")
                if path=="/admin/status" and method=="GET":
                    return store.status()
                if path=="/admin/control" and method=="POST":
                    return store.control(self.body())
                raise Error(404,"unknown spool route")
            if path.startswith("/internal/") and role in ("primary","replica"):
                self.auth("internal")
                if path=="/internal/status" and method=="GET":
                    return {**store.status(),"replication":state or {}}
                if path=="/internal/changes" and method=="GET" and role=="primary":
                    return store.changes(int(params.get("after",0)))
                if role=="primary" and method=="POST":
                    body=self.body()
                    if path=="/internal/receive":
                        return store.receive(body["user"],body["delivery"],body["descriptor"],body["body"])
                    if path=="/internal/claim":
                        return {"job":store.claim()}
                    if path=="/internal/settle":
                        return {"settled":store.settle(body["job"],body.get("error"))}
                raise Error(404,"unknown internal route")
            if not path.startswith("/v1/"):
                raise Error(404,"unknown route")
            user=self.auth()
            body=self.body() if method in ("POST","PUT") else None
            if role=="api":
                shard=shard_for(user,len(config["primary_urls"]))
                prefer_replica=params.pop("replica","false")
                if prefer_replica not in ("true","false"):
                    raise ValueError("replica must be true/false")
                query=("?"+urlencode(params)) if params else ""
                token=self.headers["Authorization"][7:]
                if method!="GET" and prefer_replica=="true":
                    raise Error(409,"replicas cannot accept writes")
                urls=[("replica",config["replica_urls"][shard])] if prefer_replica=="true" else [("primary",config["primary_urls"][shard])]
                if method=="GET" and prefer_replica!="true":
                    urls.append(("replica",config["replica_urls"][shard]))
                last=None
                for source,url in urls:
                    try:
                        value=http_json(url+path+query,token,body,method)
                        value["source"]=source
                        return value
                    except HTTPError as exc:
                        if exc.code!=503:
                            raise
                        last=exc
                    except (URLError,OSError,TimeoutError) as exc:
                        last=exc
                raise Error(503,"required mailbox node unavailable") from last
            store.owner(user)
            if role=="replica":
                if method!="GET":
                    raise Error(409,"replicas are read-only")
                if not store.status()["lineage"]:
                    raise Error(503,"replica not initialized")
            if path=="/v1/uploads" and method=="POST":
                filename=text(body["filename"],"filename",200)
                if not filename or "/" in filename or "\\" in filename or "\r" in filename or "\n" in filename:
                    raise ValueError("attachment filename must be a plain basename")
                data=base64.b64decode(body["data"],validate=True)
                if len(data)>2*1024*1024:
                    raise Error(413,"attachment exceeds 2 MiB")
                return store.upload(user,{**Objects(config).put(data),"filename":filename})
            if path=="/v1/messages":
                if method=="POST":
                    return send(store,config,user,body)
                if method=="GET":
                    if set(params)-{"folder","read","before","q","field"}:
                        raise ValueError("unknown mailbox parameter")
                    if "before" in params:
                        params["before"]=int(params["before"])
                    return store.listing(user,**params)
            parts=path.strip("/").split("/")
            if len(parts)>=3 and parts[:2]==["v1","messages"]:
                mail=parts[2]
                if len(parts)==3:
                    if method=="GET":
                        value=store.get(user,mail)
                        value["body"]=Objects(config).get(value["descriptor"]["body_key"]).decode("utf-8")
                        return value
                    if method=="PUT":
                        if not isinstance(body,dict) or set(body)!={"version","read"}:
                            raise ValueError("read update requires version and read")
                        return store.update(user,mail,body["version"],body["read"])
                    if method=="DELETE":
                        if set(params)!={"version"}:
                            raise ValueError("delete requires version query parameter")
                        return store.update(user,mail,int(params["version"]),delete=True)
                if len(parts)==5 and parts[3]=="attachments" and method=="GET":
                    value=store.get(user,mail)
                    if value["descriptor"]["folder"]=="quarantine":
                        raise Error(403,"quarantined attachment download blocked")
                    index=int(parts[4])
                    attachments=value["descriptor"]["attachments"]
                    if not 0<=index<len(attachments):
                        raise Error(404,"attachment missing")
                    attachment=attachments[index]
                    raw=Objects(config).get(attachment["key"])
                    return {**attachment,"data":base64.b64encode(raw).decode()}
            raise Error(404,"unknown route or method")

        def handle_request(self):
            self.connection.settimeout(15)
            try:
                value,status=self.dispatch(),200
            except Error as exc:
                value,status={"error":str(exc)},exc.status
            except HTTPError as exc:
                try:
                    value,status=json.loads(exc.read(1024*1024)),exc.code
                except Exception:
                    value,status={"error":"upstream failure"},503
            except (ValueError,TypeError,KeyError) as exc:
                value,status={"error":str(exc)},400
            except (URLError,OSError,TimeoutError,sqlite3.OperationalError):
                value,status={"error":"temporary local service failure"},503
            except Exception:
                value,status={"error":"internal failure"},500
            raw=dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(raw)))
            self.send_header("Cache-Control","no-store")
            self.end_headers()
            self.wfile.write(raw)

        do_GET=handle_request
        do_POST=handle_request
        do_PUT=handle_request
        do_DELETE=handle_request

    return ThreadingHTTPServer((host,port),Handler)
