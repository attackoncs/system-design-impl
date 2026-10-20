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
from message_queue.client import Client
from message_queue.common import rpc


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


class Cluster:
    def __init__(self,lease=10):
        self.root=ROOT/".runtime"/("lab-"+uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.lease=lease
        self.processes={}
        self.logs=[]
        self.backend_ports=[free_port() for _ in range(3)]
        self.proxy_ports=[free_port() for _ in range(3)]
        self.coordinator_port=free_port()
        self.config={"cluster_token":secrets.token_hex(24),"client_token":secrets.token_hex(24),"brokers":{f"b{i}":f"http://127.0.0.1:{p}" for i,p in enumerate(self.proxy_ports)}}
        self.config_path=self.root/"secrets.json"
        self.config_path.write_text(json.dumps(self.config))
        self.url=f"http://127.0.0.1:{self.coordinator_port}"
        self.client=Client(self.url,self.config["client_token"])

    def spawn(self,name,args):
        output=(self.root/(name+".log")).open("ab")
        self.logs.append(output)
        env=dict(os.environ,PYTHONPATH=str(ROOT/"src"))
        process=subprocess.Popen([sys.executable,*args],env=env,stdout=output,stderr=output,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.processes[name]=process
        return process

    def wait(self,predicate,timeout=12):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                result=predicate()
                if result:
                    return result
            except (OSError,ValueError):
                pass
            time.sleep(0.05)
        raise RuntimeError("lab wait timed out; inspect isolated process logs")

    def start_broker(self,index):
        self.spawn(f"b{index}",["-m","message_queue.cli","broker","--root",str(self.root/f"b{index}"),"--config",str(self.config_path),"--port",str(self.backend_ports[index])])
        self.wait(lambda:rpc(f"http://127.0.0.1:{self.backend_ports[index]}","/health",{},self.config["cluster_token"]))

    def start_coordinator(self):
        self.spawn("coordinator",["-m","message_queue.cli","coordinator","--root",str(self.root/"coordinator"),"--config",str(self.config_path),"--port",str(self.coordinator_port),"--lease-seconds",str(self.lease)])
        self.wait(lambda:self.client.call("/health"))

    def start(self):
        for i in range(3):
            self.start_broker(i)
            self.partition(i,False)
            self.spawn(f"proxy{i}",[str(ROOT/"tools/proxy.py"),"--port",str(self.proxy_ports[i]),"--target",str(self.backend_ports[i]),"--control",str(self.root/f"proxy{i}.json")])
            self.wait(lambda i=i:rpc(self.config["brokers"][f"b{i}"],"/health",{},self.config["cluster_token"]))
        self.start_coordinator()
        return self

    def partition(self,index,blocked=True):
        (self.root/f"proxy{index}.json").write_text(json.dumps({"blocked":blocked}))

    def stop(self,name):
        process=self.processes.pop(name,None)
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def close(self):
        for name in list(self.processes):
            self.stop(name)
        for log in self.logs:
            log.close()

    def __enter__(self):
        try:
            return self.start()
        except BaseException:
            self.close()
            raise

    def __exit__(self,*args):
        self.close()
