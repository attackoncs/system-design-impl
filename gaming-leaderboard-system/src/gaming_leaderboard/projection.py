import json
import threading
import time

from .common import Error, digest, dumps, http_json, integer, month_bounds, shard_for
from .redis import Redis, RedisError

APPLY = r"""
local root=KEYS[1]
local batch=cjson.decode(ARGV[1])
local generation=redis.call('GET',root..':lineage')
if generation and generation~=batch.lineage then return redis.error_reply('LINEAGE changed') end
local pos=tonumber(redis.call('GET',root..':position') or '0')
if pos~=batch.after then return cjson.encode({stale=true,position=pos}) end
local nextpos=pos
for _,e in ipairs(batch.events) do
  if e.seq~=nextpos+1 then return redis.error_reply('OFFSET gap') end
  for _,kind in ipairs({'score','order'}) do
    local t=redis.call('TYPE',root..':'..kind..':'..e.payload.month).ok
    if t~='none' and t~='zset' then return redis.error_reply('TYPE corrupt') end
  end
  nextpos=e.seq
end
for _,e in ipairs(batch.events) do
  local p=e.payload
  local sk=root..':score:'..p.month
  local ok=root..':order:'..p.month
  local old=redis.call('ZSCORE',sk,p.user)
  if old then redis.call('ZREM',ok,string.format('%010d',1000000000+tonumber(old))..':'..p.user) end
  redis.call('ZADD',sk,-p.score,p.user)
  redis.call('ZADD',ok,0,string.format('%010d',1000000000-p.score)..':'..p.user)
  redis.call('SADD',root..':months',p.month)
end
redis.call('SET',root..':lineage',batch.lineage)
redis.call('SET',root..':position',nextpos)
return cjson.encode({position=nextpos})
"""

QUERY = r"""
local root=KEYS[1]
local q=cjson.decode(ARGV[1])
if redis.call('GET',root..':lineage')~=q.lineage then return redis.error_reply('NOTREADY lineage') end
local pos=tonumber(redis.call('GET',root..':position') or '0')
if pos<q.minimum then return redis.error_reply('NOTREADY position') end
local sk=root..':score:'..q.month
local ok=root..':order:'..q.month
if redis.call('ZCARD',sk)~=redis.call('ZCARD',ok) then return redis.error_reply('INDEX corrupt; rebuild required') end
local function rows(members)
  local result={}
  for _,m in ipairs(members) do
    table.insert(result,{user=string.sub(m,12),score=1000000000-tonumber(string.sub(m,1,10)),lex=m})
  end
  return result
end
local result={position=pos,shard=q.shard}
if q.mode=='top' then
  result.rows=rows(redis.call('ZRANGE',ok,0,q.limit-1))
elseif q.mode=='target' then
  local score=redis.call('ZSCORE',sk,q.user)
  if score then result.target={user=q.user,score=-tonumber(score),lex=string.format('%010d',1000000000+tonumber(score))..':'..q.user} end
elseif q.mode=='around' then
  result.higher=redis.call('ZCOUNT',sk,'-inf','('..tostring(-q.score))
  result.before=redis.call('ZLEXCOUNT',ok,'-','('..q.pivot)
  result.above=rows(redis.call('ZREVRANGEBYLEX',ok,'('..q.pivot,'-','LIMIT',0,q.radius))
  result.below=rows(redis.call('ZRANGEBYLEX',ok,'('..q.pivot,'+','LIMIT',0,q.radius))
elseif q.mode=='counts' then
  result.counts={}
  for _,score in ipairs(q.scores) do result.counts[tostring(score)]=redis.call('ZCOUNT',sk,'-inf','('..tostring(-score)) end
elseif q.mode=='dump' then
  if redis.call('ZCARD',sk)>10000 then return redis.error_reply('BUDGET exceeded') end
  result.rows=rows(redis.call('ZRANGE',ok,0,-1))
else return redis.error_reply('MODE unknown') end
return cjson.encode(result)
"""

RESET = r"""
local root=KEYS[1]
for _,month in ipairs(redis.call('SMEMBERS',root..':months')) do
  redis.call('DEL',root..':score:'..month,root..':order:'..month)
end
redis.call('DEL',root..':months',root..':lineage',root..':position')
return 1
"""


class Projection:
    def __init__(self, config, shard):
        self.config, self.shard = config, shard
        self.root = config["namespace"] + ":shard:" + str(shard)
        self.clients = [
            Redis(n["host"], n["port"], config["redis_password"])
            for n in config["redis_nodes"][shard]
        ]
        self.lock = threading.RLock()
        self.state = {"error": "starting", "last_poll": 0}

    def master(self):
        masters = []
        for client in self.clients:
            try:
                if client.command("ROLE", timeout=0.2)[0] == "master":
                    masters.append(client)
            except (OSError, RedisError):
                pass
        if len(masters) != 1:
            raise Error(
                503,
                "exactly one reachable master required; fence/promote operationally",
            )
        return masters[0]

    def position(self, client=None):
        return int(
            (client or self.master()).command("GET", self.root + ":position") or 0
        )

    def apply(self, batch):
        if batch["shard"] != self.shard:
            raise Error(409, "wrong event shard")
        expected = batch["after"]
        for entry in batch["events"]:
            p = entry["payload"]
            month_bounds(p["month"])
            integer(p["score"], "score", 0, 1_000_000_000)
            if (
                shard_for(p["user"], len(self.config["projection_urls"])) != self.shard
                or entry["seq"] != expected + 1
                or digest(p) != entry["hash"]
            ):
                raise Error(409, "event checksum, offset or ownership mismatch")
            expected = entry["seq"]
        return json.loads(
            self.master().command("EVAL", APPLY, 1, self.root, dumps(batch))
        )

    def poll_once(self):
        with self.lock:
            after = self.position()
            batch = http_json(
                self.config["authority_url"]
                + f"/internal/feed?shard={self.shard}&after={after}",
                self.config["internal_token"],
            )
            client = self.master()
            generation = client.command("GET", self.root + ":lineage")
            if (
                not batch["events"]
                and generation == batch["lineage"]
                and self.position(client) == after
            ):
                result = {"position": after}
            else:
                result = self.apply(batch)
            self.state.update(
                error=None,
                last_poll=time.time(),
                source_head=batch["head"],
                position=result["position"],
            )
            return batch["head"] > result["position"]

    def run(self, stop):
        while not stop.is_set():
            try:
                busy = self.poll_once()
                stop.wait(0.005 if busy else 0.03)
            except Exception as exc:
                self.state["error"] = str(exc)
                stop.wait(0.1)

    def query(self, body):
        month_bounds(body["month"])
        integer(body["minimum"], "minimum")
        if body["mode"] == "top":
            integer(body["limit"], "limit", 1, 10)
        if body["mode"] == "around":
            integer(body["radius"], "radius", 0, 4)
        with self.lock:
            result = json.loads(
                self.master().command(
                    "EVAL", QUERY, 1, self.root, dumps({**body, "shard": self.shard})
                )
            )
            # Redis Lua cjson encodes empty tables as {}; normalize array fields.
            for key in ("rows", "above", "below"):
                if key in result and result[key] == {}:
                    result[key] = []
            return result

    def reset(self):
        with self.lock:
            self.master().command("EVAL", RESET, 1, self.root)
            self.state["error"] = "rebuilding"
        return {"rebuilding": True, "shard": self.shard}
