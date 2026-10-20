import json
import math
import re

import redis

from .core import Error, Ring, dumps, meters


class Backend:
    def __init__(self,store,cache_url,namespace="nearby"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",namespace):
            raise ValueError("invalid namespace")
        self.store,self.namespace = store,namespace
        self.cache = redis.Redis.from_url(cache_url,decode_responses=True,socket_connect_timeout=0.5,socket_timeout=0.5)
        self.brokers = {}

    def key(self,user):
        return self.namespace+":location:"+user

    def channel(self,user):
        return self.namespace+":user:"+user

    def publish(self,user):
        # Hints have no coordinates. Data is always reread with current ACLs.
        config = self.store.ring()
        if not config["nodes"]:
            return False
        ring = Ring(config["nodes"])
        node = ring.owner(user)
        url = ring.nodes[node]
        try:
            if url not in self.brokers:
                self.brokers[url] = redis.Redis.from_url(url,socket_connect_timeout=0.5,socket_timeout=0.5)
            self.brokers[url].publish(self.channel(user),dumps({"user":user}))
            return True
        except redis.RedisError:
            return False

    def warm(self,report):
        remaining = math.ceil((report["received_at"]+self.store.ttl-self.store.clock())*1000)
        if remaining <= 0:
            return
        try:
            self.cache.eval("""
            local old=redis.call('GET',KEYS[1])
            if old then
              local ok,value=pcall(cjson.decode,old)
              if ok and value.revision and tonumber(value.revision)>tonumber(ARGV[1]) then return 0 end
            end
            redis.call('SET',KEYS[1],ARGV[2],'PX',ARGV[3]); return 1
            """,1,self.key(report["id"]),report["revision"],dumps(report),remaining)
        except redis.RedisError:
            pass  # SQL current-state recovery remains available.

    def update(self,actor,body):
        report = self.store.update(actor,body)
        self.warm(report)
        published = self.publish(actor)
        return {"revision":report["revision"],"hint_published":published}

    def snapshot(self,actor,radius=8046.72):
        if type(radius) not in (int,float) or not math.isfinite(radius) or not 0 < radius <= 100000:
            raise Error(400,"radius must be within (0,100000] meters")
        own,reports = self.store.reports(actor)
        if own is None:
            return []
        try:
            cached = self.cache.mget([self.key(r["id"]) for r in reports]) if reports else []
        except redis.RedisError:
            cached = [None]*len(reports)
        result = []
        for authoritative,value in zip(reports,cached):
            report = authoritative
            if value:
                try:
                    candidate = json.loads(value)
                    # Equality guards out-of-order publication and stale privacy state.
                    if candidate == authoritative:
                        report = candidate
                except (ValueError,TypeError):
                    pass
            if report is authoritative:
                self.warm(authoritative)
            dist = meters(own,report)
            if dist <= radius+1e-6:
                # Device identity and server internals are not disclosed to friends.
                result.append({k:report[k] for k in ("id","latitude","longitude","timestamp","revision")} | {"distance_m":dist})
        return sorted(result,key=lambda row:(row["distance_m"],row["id"]))

    def close(self):
        self.cache.close()
        for broker in self.brokers.values():
            broker.close()
