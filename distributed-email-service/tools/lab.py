import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from distributed_email.common import http_json,shard_for


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


def wait_for(probe,timeout=20):
    end,last=time.monotonic()+timeout,None
    while time.monotonic()<end:
        try:
            result=probe()
            if result:
                return result
        except Exception as exc:
            last=exc
        time.sleep(.05)
    raise AssertionError(f"condition not reached: {last}")


class Lab:
    def __init__(self,scanner="test-markers"):
        self.root=ROOT/".runtime"/("lab-"+uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.ports={}
        for name in ("api","objects","smtp","smtp-wire","primary-0","primary-1","replica-0","replica-1"):
            value=port()
            while value in self.ports.values():
                value=port()
            self.ports[name]=value
        self.urls={name:f"http://127.0.0.1:{p}" for name,p in self.ports.items()}
        self.recipient=next(f"recipient{i}" for i in range(100) if shard_for(f"recipient{i}",2)==1)
        self.config={r+"_token":secrets.token_hex(24) for r in ("admin","internal","smtp")}
        self.config.update(users={u:secrets.token_hex(24) for u in ("alice",self.recipient,"outsider")},
                           primary_urls=[self.urls[f"primary-{p}"] for p in range(2)],
                           replica_urls=[self.urls[f"replica-{p}"] for p in range(2)],
                           object_url=self.urls["objects"],smtp_port=self.ports["smtp-wire"],scanner=scanner)
        self.config_path=self.root/"secrets.json"
        self.config_path.write_text(json.dumps(self.config))
        self.processes,self.logs={},[]

    def spawn(self,name):
        role=name.split("-")[0]
        args=[sys.executable,"-m","distributed_email.cli",role,"--config",str(self.config_path),"--root",str(self.root)]
        if role in ("primary","replica","worker"):
            args += ["--shard",name.split("-")[1]]
        if role!="worker":
            args += ["--port",str(self.ports[name])]
        handle=(self.root/(name+".log")).open("ab")
        self.logs.append(handle)
        self.processes[name]=subprocess.Popen(args,env=dict(os.environ,PYTHONPATH=str(ROOT/"src")),stdout=handle,stderr=handle,
                                             creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if role!="worker":
            wait_for(lambda:self.call(name,"/health"))

    def stop(self,name):
        process=self.processes.pop(name,None)
        if process:
            process.kill()
            process.wait(timeout=10)

    def call(self,name,path,body=None,user="alice",method=None):
        token=self.config["users"][user] if user in self.config["users"] else self.config[user+"_token"]
        return http_json(self.urls[name]+path,token,body,method)

    def api(self,path,body=None,user="alice",method=None):
        return self.call("api",path,body,user,method)

    def send(self,key="req",subject="Network test",body="searchable hello",attachments=None):
        return self.api("/v1/messages",{"request_key":key,"to":[self.recipient+"@local.test"],"subject":subject,
                                         "body":body,"attachment_ids":attachments or []})

    def caught_up(self):
        for p in range(2):
            source=self.call(f"primary-{p}","/internal/status",user="internal")
            replica=self.call(f"replica-{p}","/internal/status",user="internal")
            if replica["position"]<source["position"] or replica["lineage"]!=source["lineage"]:
                return False
        return True

    def settled(self):
        return (all(self.call(f"primary-{p}","/internal/status",user="internal")["pending"]==0 for p in range(2))
                and self.call("smtp","/admin/status",user="admin")["pending"]==0)

    def __enter__(self):
        try:
            for name in ("objects","primary-0","primary-1","replica-0","replica-1","smtp","api","worker-0","worker-1"):
                self.spawn(name)
            wait_for(self.caught_up)
            return self
        except BaseException:
            self.__exit__(None,None,None)
            raise

    def __exit__(self,*args):
        for name in list(self.processes):
            self.stop(name)
        for handle in self.logs:
            handle.close()
