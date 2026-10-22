"""Owns real Redis/HTTP children; requires Redis, never substitutes a mock."""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from gaming_leaderboard.common import http_json
from gaming_leaderboard.redis import Redis


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


def wait_for(probe,timeout=20):
    deadline,last=time.monotonic()+timeout,None
    while time.monotonic()<deadline:
        try:
            result=probe()
            if result:
                return result
        except Exception as exc:
            last=exc
        time.sleep(.05)
    raise AssertionError(f"condition not reached: {last}")


def info(client,section="server"):
    return dict(line.split(":",1) for line in client.command("INFO",section).splitlines() if line and not line.startswith("#") and ":" in line)


class Lab:
    def __init__(self):
        self.root=ROOT/".runtime"/("lab-"+uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.binary=os.environ.get("LB_REDIS_BINARY") or shutil.which("redis-server")
        self.wsl_binary=os.environ.get("LB_WSL_REDIS_BINARY")
        if not self.binary and not self.wsl_binary:
            raise RuntimeError("real Redis required: set LB_REDIS_BINARY or LB_WSL_REDIS_BINARY; no mock fallback")
        self.distro=os.environ.get("LB_WSL_DISTRO","Ubuntu-26.04")
        self.ports={}
        for name in ["authority","projection-0","projection-1"]+[f"redis-{i}" for i in range(4)]:
            value=port()
            while value in self.ports.values():
                value=port()
            self.ports[name]=value
        self.urls={n:f"http://127.0.0.1:{p}" for n,p in self.ports.items()}
        self.config={kind+"_token":secrets.token_hex(24) for kind in ("admin","read","game","internal")}
        self.config.update(redis_password=secrets.token_hex(24),namespace="lab_"+uuid.uuid4().hex,
                           authority_url=self.urls["authority"],projection_urls=[self.urls[f"projection-{p}"] for p in range(2)],
                           redis_nodes=[[{"host":"127.0.0.1","port":self.ports[f"redis-{2*p+i}"]} for i in range(2)] for p in range(2)],late_grace=172800)
        self.config_path=self.root/"secrets.json"
        self.config_path.write_text(json.dumps(self.config))
        self.processes,self.logs,self.run_ids={},[],{}
        self.clients=[Redis("127.0.0.1",self.ports[f"redis-{i}"],self.config["redis_password"]) for i in range(4)]

    def path(self,path):
        if self.binary:
            return str(path.resolve())
        value=path.resolve().as_posix()
        if len(value)<3 or value[1:3]!=":/":
            raise ValueError("WSL lab expects an absolute Windows drive path")
        return "/mnt/"+value[0].lower()+value[2:]

    def popen(self,name,args,env=None):
        output=(self.root/(name+".log")).open("ab")
        self.logs.append(output)
        process=subprocess.Popen(args,env=env,stdout=output,stderr=output,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.processes[name]=process
        return process

    def spawn_redis(self,index,follow=None):
        folder=self.root/f"redis-{index}"
        folder.mkdir(exist_ok=True)
        parent=follow if follow is not None else (index-1 if index%2 else None)
        if parent==-1:
            parent=None
        lines=[f"port {self.ports[f'redis-{index}']}","bind 127.0.0.1","protected-mode yes",
               f"requirepass {self.config['redis_password']}",f"masterauth {self.config['redis_password']}",
               f'dir "{self.path(folder)}"',"appendonly yes","appendfsync always",'save ""',"repl-diskless-sync-delay 0",
               "repl-diskless-load swapdb","aof-use-rdb-preamble no"]
        if parent is not None:
            lines.append(f"replicaof 127.0.0.1 {self.ports[f'redis-{parent}']}")
        conf=folder/"redis.conf"
        conf.write_text("\n".join(lines)+"\n")
        if self.binary:
            args=[self.binary,str(conf)]
        else:
            extra=[]
            if os.environ.get("LB_WSL_REDIS_LIB"):
                extra=["env","LD_LIBRARY_PATH="+os.environ["LB_WSL_REDIS_LIB"]]
            args=["wsl","-d",self.distro,"--",*extra,self.wsl_binary,self.path(conf)]
        self.popen(f"redis-{index}",args)
        wait_for(lambda:self.clients[index].command("PING")=="PONG")
        self.run_ids[index]=info(self.clients[index])["run_id"]
        if parent is not None:
            wait_for(lambda:info(self.clients[index],"replication").get("master_link_status")=="up")

    def stop_redis(self,index,crash=False):
        name=f"redis-{index}"
        process=self.processes.get(name)
        if not process:
            return
        details=info(self.clients[index])
        if details["run_id"]!=self.run_ids[index]:
            raise RuntimeError("refusing to stop a Redis instance not owned by this lab")
        if crash:
            if self.binary:
                process.kill()
            else:
                subprocess.run(["wsl","-d",self.distro,"--","kill","-KILL",details["process_id"]],check=True,timeout=10,
                               creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        else:
            try:
                self.clients[index].command("SHUTDOWN","NOSAVE")
            except Exception:
                pass
        process.wait(timeout=15)
        self.processes.pop(name)

    def spawn(self,name):
        role=name.split("-")[0]
        args=[sys.executable,"-m","gaming_leaderboard.cli",role,"--config",str(self.config_path),"--root",str(self.root),"--port",str(self.ports[name])]
        if role=="projection":
            args += ["--shard",name.split("-")[1]]
        self.popen(name,args,dict(os.environ,PYTHONPATH=str(ROOT/"src")))
        wait_for(lambda:self.call(name,"/health"))

    def stop(self,name):
        process=self.processes.pop(name,None)
        if process:
            process.kill()
            process.wait(timeout=10)

    def call(self,name,path,body=None,role="read",method=None):
        return http_json(self.urls[name]+path,self.config[role+"_token"],body,method)

    def api(self,path,body=None,role="read",method=None):
        return self.call("authority",path,body,role,method)

    def register(self,user):
        return self.api("/admin/players/"+user,{"name":"Player "+user},"admin","PUT")

    def match(self,match,winner,other="loser",ended_at=None):
        return self.api("/v1/scores",{"match_id":str(match),"winner":winner,"players":[winner,other],
                                      "ended_at":int(time.time()) if ended_at is None else ended_at},"game")

    def caught_up(self):
        source=self.api("/internal/status",role="internal")
        for p in range(2):
            state=self.call(f"projection-{p}","/internal/status",role="internal")
            if state["position"]<source["heads"][str(p)] or state["lineage"]!=source["lineage"]:
                return False
        return True

    def __enter__(self):
        try:
            for i in range(4):
                self.spawn_redis(i)
            for name in ("authority","projection-0","projection-1"):
                self.spawn(name)
            wait_for(self.caught_up)
            return self
        except BaseException:
            self.__exit__(None,None,None)
            raise

    def __exit__(self,*args):
        for name in list(self.processes):
            if not name.startswith("redis-"):
                self.stop(name)
        for i in range(4):
            if f"redis-{i}" in self.processes:
                self.stop_redis(i)
        for output in self.logs:
            output.close()
