"""Owned localhost Redis and WebSocket process fixtures; no existing service changes."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

import redis
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from nearby_friends.core import Store


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


class Cluster:
    def __init__(self,ttl=600):
        self.root = ROOT/".runtime"/("lab-"+uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.namespace = "lab_"+uuid.uuid4().hex
        self.ttl = ttl
        self.processes = []
        self.redis = []
        self.urls = []
        self.logs = []
        self.tokens = {user:user+"-demo-secret" for user in ["alice","bob","eve"]}
        self.store = Store(self.root/"state.sqlite",ttl=ttl)
        for user,token in self.tokens.items():
            self.store.provision(user,token,admin=user == "alice")

    def spawn(self,args,env=None,name="process"):
        log = (self.root/(name+".log")).open("wb")
        self.logs.append(log)
        process = subprocess.Popen(args,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.processes.append(process)
        return process

    def start_redis(self):
        binary = os.environ.get("NEARBY_REDIS_BINARY") or shutil.which("redis-server")
        wsl_binary = os.environ.get("NEARBY_WSL_REDIS_BINARY")
        if not binary and not wsl_binary:
            raise RuntimeError("set NEARBY_REDIS_BINARY or NEARBY_WSL_REDIS_BINARY; no Redis mock is used")
        for index in range(2):
            number = port()
            folder = self.root/("redis-"+str(index))
            folder.mkdir()
            common = ["--port",str(number),"--bind","127.0.0.1","--protected-mode","yes","--save","","--appendonly","no"]
            if binary:
                args = [binary,*common,"--dir",str(folder)]
            else:
                env_args = []
                if os.environ.get("NEARBY_WSL_REDIS_LIB"):
                    env_args = ["env","LD_LIBRARY_PATH="+os.environ["NEARBY_WSL_REDIS_LIB"]]
                args = ["wsl","-d",os.environ.get("NEARBY_WSL_DISTRO","Ubuntu-26.04"),"--",*env_args,wsl_binary,*common]
            process = self.spawn(args,name="redis-"+str(index))
            url = f"redis://127.0.0.1:{number}/0"
            client = redis.Redis.from_url(url,socket_connect_timeout=0.3,socket_timeout=0.3)
            deadline = time.monotonic()+15
            while True:
                try:
                    client.ping()
                    run_id = client.info("server")["run_id"]
                    self.redis.append((client,run_id,process))
                    self.urls.append(url)
                    break
                except redis.RedisError:
                    if process.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError("Redis startup failed; see isolated lab log")
                    time.sleep(0.1)
        self.store.set_ring("alice",{"broker-a":self.urls[0],"broker-b":self.urls[1]},0)

    async def start_node(self,name,reconcile=1):
        number = port()
        env = dict(os.environ,PYTHONPATH=str(ROOT/"src"))
        process = self.spawn([sys.executable,"-m","nearby_friends.cli","serve","--db",str(self.root/"state.sqlite"),"--port",str(number),"--cache-url",self.urls[0],"--namespace",self.namespace,"--reconcile",str(reconcile)],env,name)
        url = f"ws://127.0.0.1:{number}"
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            try:
                ws = await connect(url,additional_headers={"Authorization":"Bearer "+self.tokens["alice"]})
                await ws.recv()
                await ws.close()
                return url
            except (OSError,TimeoutError):
                if process.poll() is not None:
                    raise RuntimeError("WebSocket node exited; inspect lab log")
                await asyncio.sleep(0.05)
        raise RuntimeError("WebSocket node startup timed out")

    def stop_redis(self,index):
        client,run_id,process = self.redis[index]
        try:
            if client.info("server")["run_id"] == run_id:
                client.shutdown(nosave=True)
        except redis.RedisError:
            pass

    def close(self):
        for index in range(len(self.redis)):
            self.stop_redis(index)
            self.redis[index][0].close()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in self.logs:
            log.close()


class Client:
    def __init__(self,ws):
        self.ws = ws
        self.counter = 0
        self.events = []

    @classmethod
    async def open(cls,url,token):
        ws = await connect(url,additional_headers={"Authorization":"Bearer "+token})
        client = cls(ws)
        client.initial = json.loads(await ws.recv())
        return client

    async def command(self,op,**body):
        self.counter += 1
        request_id = str(self.counter)
        await self.ws.send(json.dumps({"op":op,"request_id":request_id,**body}))
        while True:
            message = json.loads(await asyncio.wait_for(self.ws.recv(),5))
            if message.get("request_id") == request_id:
                return message
            self.events.append(message)

    async def until(self,predicate,timeout=5):
        for message in self.events:
            if predicate(message):
                self.events.remove(message)
                return message
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            message = json.loads(await asyncio.wait_for(self.ws.recv(),max(0.01,deadline-time.monotonic())))
            if predicate(message):
                return message
            self.events.append(message)
        raise TimeoutError("event not received")

    async def close(self):
        await self.ws.close()
