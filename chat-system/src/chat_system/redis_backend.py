"""Shared asynchronous Redis storage, device fencing, and node leases.

All keys use one Redis hash tag. Redis persistence is a deployment requirement.
"""
import hashlib
import json
import secrets
import uuid
from dataclasses import asdict

from .models import AuthenticationError, Channel, ChatError, Message


def identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise ChatError("invalid identifier")
    return value


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def message_from_json(raw):
    value = json.loads(raw)
    value["message_id"] = int(value["message_id"])
    return Message(**value)


_APPEND = """
if ARGV[8] ~= '' then
    local time=redis.call('TIME'); local now=tonumber(time[1])+tonumber(time[2])/1000000
    local record=redis.call('HGET',KEYS[8],ARGV[8])
    if not record then return {'FENCED'} end
    local s=cjson.decode(record)
    local node=redis.call('HGET',KEYS[9],s.node)
    if s.session~=ARGV[9] or s.instance~=ARGV[10] or s.expires<=now or
        not node or cjson.decode(node).instance~=s.instance or
        tonumber(redis.call('ZSCORE',KEYS[10],s.node) or '0')<=now or
        redis.call('HGET',KEYS[11],ARGV[11])~=ARGV[3] then return {'FENCED'} end
end
local old = redis.call('HGET', KEYS[3], ARGV[6])
if old then
    local data = redis.call('HGET', KEYS[2], old)
    local m = cjson.decode(data)
    if m.channel_id ~= ARGV[2] or m.content ~= ARGV[4] then
        return {'CONFLICT'}
    end
    return {'EXISTING', data}
end
if redis.call('HGET', KEYS[4], ARGV[2]) ~= ARGV[1] then return {'RETRY'} end
local channel = cjson.decode(ARGV[1])
local allowed = false
for _, user in ipairs(channel.members) do if user == ARGV[3] then allowed = true end end
if not allowed then return {'DENIED'} end
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= 9007199254740990 then return {'LIMIT'} end
redis.call('INCR', KEYS[1])
local id = redis.call('GET', KEYS[1])
local m = cjson.decode(ARGV[7])
m.message_id = id
local data = cjson.encode(m)
redis.call('HSET', KEYS[2], id, data)
redis.call('HSET', KEYS[3], ARGV[6], id)
redis.call('ZADD', KEYS[7], id, id)
for i, user in ipairs(channel.members) do
    redis.call('ZADD', KEYS[11+i], id, id)
    if user ~= ARGV[3] then
        redis.call('XADD', KEYS[6], '*', 'user', user, 'message_id', id)
    end
end
redis.call('XADD', KEYS[5], '*', 'data', data, 'members', cjson.encode(channel.members))
return {'NEW', data}
"""

_ADD_MEMBER = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return {'MISSING'} end
local c = cjson.decode(raw)
if c.kind ~= 'group' or c.owner_id ~= ARGV[2] then return {'DENIED'} end
for _, user in ipairs(c.members) do if user == ARGV[3] then return {'OK', raw} end end
if #c.members >= tonumber(ARGV[4]) then return {'FULL'} end
table.insert(c.members, ARGV[3]); table.sort(c.members)
local result = cjson.encode(c)
redis.call('HSET', KEYS[1], ARGV[1], result)
return {'OK', result}
"""

_LEASE = """
local time = redis.call('TIME'); local now = tonumber(time[1])+tonumber(time[2])/1000000
local old = redis.call('HGET', KEYS[1], ARGV[1])
local expires = tonumber(redis.call('ZSCORE', KEYS[2], ARGV[1]) or '0')
if old and expires > now and cjson.decode(old).instance ~= ARGV[2] then return 0 end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
redis.call('ZADD', KEYS[2], now+tonumber(ARGV[4]), ARGV[1])
return 1
"""

_ADMIT = """
local time = redis.call('TIME'); local now = tonumber(time[1])+tonumber(time[2])/1000000
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw or cjson.decode(raw).instance ~= ARGV[2] then return 0 end
if tonumber(redis.call('ZSCORE', KEYS[2], ARGV[1]) or '0') <= now then return 0 end
redis.call('ZREMRANGEBYSCORE', KEYS[5], '-inf', now)
local old = redis.call('HGET', KEYS[3], ARGV[3])
local count=0
for _, device in ipairs(redis.call('ZRANGE',KEYS[5],0,-1)) do
    local sraw=redis.call('HGET',KEYS[3],device)
    if sraw then
        local s=cjson.decode(sraw)
        if s.node==ARGV[1] and s.instance==ARGV[2] and s.expires>now and device~=ARGV[3] then
            count=count+1
        end
    end
end
if count >= tonumber(ARGV[6]) then return 0 end
local record = cjson.decode(ARGV[4]); record.expires = now+tonumber(ARGV[5])
redis.call('HSET', KEYS[3], ARGV[3], cjson.encode(record))
redis.call('ZADD', KEYS[4], record.expires, ARGV[3])
redis.call('ZADD', KEYS[5], record.expires, ARGV[3])
return 1
"""

_SESSION = """
local time = redis.call('TIME'); local now = tonumber(time[1])+tonumber(time[2])/1000000
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return 0 end
local s = cjson.decode(raw)
if s.session ~= ARGV[2] then return 0 end
if ARGV[3] == 'close' then
    redis.call('HDEL', KEYS[1], ARGV[1]); redis.call('ZREM', KEYS[2], ARGV[1])
    redis.call('ZREM', KEYS[3], ARGV[1]); return 1
end
if s.expires <= now then return 0 end
local node = redis.call('HGET', KEYS[4], s.node)
if not node or cjson.decode(node).instance ~= s.instance then return 0 end
if tonumber(redis.call('ZSCORE', KEYS[5], s.node) or '0') <= now then return 0 end
if ARGV[3] == 'heartbeat' then
    s.expires = now+tonumber(ARGV[4])
    redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(s))
    redis.call('ZADD', KEYS[2], s.expires, ARGV[1])
    redis.call('ZADD', KEYS[3], s.expires, ARGV[1])
elseif ARGV[3] == 'close' then
    redis.call('HDEL', KEYS[1], ARGV[1]); redis.call('ZREM', KEYS[2], ARGV[1])
    redis.call('ZREM', KEYS[3], ARGV[1])
end
return 1
"""

_ONLINE = """
local time = redis.call('TIME'); local now = tonumber(time[1])+tonumber(time[2])/1000000
local devices = redis.call('ZRANGEBYSCORE', KEYS[1], '('..now, '+inf')
for _, device in ipairs(devices) do
    local raw = redis.call('HGET', KEYS[2], device)
    if raw then
        local s = cjson.decode(raw)
        local node = redis.call('HGET', KEYS[3], s.node)
        if node and s.expires > now and cjson.decode(node).instance == s.instance and
            tonumber(redis.call('ZSCORE', KEYS[4], s.node) or '0') > now then return 1 end
    end
end
return 0
"""


class RedisBackend:
    def __init__(self, client, namespace="chat"):
        if not namespace or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in namespace):
            raise ValueError("invalid Redis namespace")
        self.redis = client
        self.prefix = "{" + namespace + "}:"

    @classmethod
    def from_url(cls, url="redis://127.0.0.1:6379/0", namespace="chat"):
        from redis.asyncio import Redis
        return cls(Redis.from_url(url, decode_responses=True, socket_connect_timeout=3,
                                 socket_timeout=5, health_check_interval=15), namespace)

    def key(self, name):
        return self.prefix + name

    def user_key(self, name, user):
        return self.key(name + ":" + digest(identifier(user)))

    @staticmethod
    def device_key(user, device):
        return digest(json.dumps([identifier(user), identifier(device)], ensure_ascii=False))

    async def now(self):
        seconds, micros = await self.redis.time()
        return seconds + micros / 1_000_000

    async def close(self):
        await self.redis.aclose()

    async def issue(self, user_id):
        identifier(user_id)
        token = secrets.token_urlsafe(32)
        await self.redis.hset(self.key("credentials"), digest(token), user_id)
        return token

    async def authenticate(self, token):
        if not isinstance(token, str) or not token.isascii() or not 1 <= len(token) <= 256:
            raise AuthenticationError("invalid token")
        user = await self.redis.hget(self.key("credentials"), digest(token))
        if user is None:
            raise AuthenticationError("invalid token")
        return user

    async def revoke(self, token):
        await self.redis.hdel(self.key("credentials"), digest(token))

    async def _channel_raw(self, channel_id):
        raw = await self.redis.hget(self.key("channels"), identifier(channel_id))
        if raw is None:
            raise ChatError("channel not found")
        return raw

    @staticmethod
    def _channel(raw):
        value = json.loads(raw)
        value["members"] = tuple(value["members"])
        return Channel(**value)

    async def get_channel(self, channel_id):
        return self._channel(await self._channel_raw(channel_id))

    async def create_channel(self, owner_id, members, kind, limit):
        users = tuple(sorted({identifier(u) for u in members} | {identifier(owner_id)}))
        if kind not in ("direct", "group") or not 2 <= len(users) <= limit:
            raise ChatError("invalid channel")
        if kind == "direct" and len(users) != 2:
            raise ChatError("direct chat requires two users")
        channel_id = ("direct-" + digest(json.dumps(users, ensure_ascii=False))
                      if kind == "direct" else "group-" + uuid.uuid4().hex)
        channel = Channel(channel_id, owner_id, kind, users)
        await self.redis.hsetnx(self.key("channels"), channel_id, json.dumps(asdict(channel)))
        return await self.get_channel(channel_id)

    async def add_member(self, channel_id, actor, user_id, limit):
        result = await self.redis.eval(_ADD_MEMBER, 1, self.key("channels"),
                                       identifier(channel_id), identifier(actor), identifier(user_id), limit)
        if result[0] != "OK":
            raise ChatError("member addition denied")
        return self._channel(result[1])

    async def append(self, channel_id, sender_id, content, client_message_id, session=None, node=None, instance=None):
        for _ in range(10):
            raw = await self._channel_raw(channel_id)
            channel = self._channel(raw)
            if sender_id not in channel.members:
                raise ChatError("not a channel member")
            retry_key = digest(json.dumps([sender_id, client_message_id], ensure_ascii=False))
            template = json.dumps(asdict(Message(0, channel_id, sender_id, content,
                                                await self.now(), client_message_id)), ensure_ascii=False)
            keys = [self.key(n) for n in ("sequence", "messages", "retry_keys", "channels", "events", "push")]
            keys += [self.key("history:" + channel_id)]
            keys += [self.key(n) for n in ("sessions", "nodes", "node_expiry", "credentials")]
            keys += [self.user_key("inbox", u) for u in channel.members]
            result = await self.redis.eval(_APPEND, len(keys), *keys, raw, channel_id,
                                           sender_id, content, client_message_id, retry_key, template,
                                           self.device_key(sender_id, session.device_id) if session else "",
                                           session.session_id if session else "", instance or "",
                                           digest(session.token) if session else "")
            if result[0] in ("NEW", "EXISTING"):
                return message_from_json(result[1]), result[0] == "NEW", channel.members
            if result[0] != "RETRY":
                raise ChatError("message write denied or conflicting retry key")
        raise ChatError("channel membership changed repeatedly; retry")

    @staticmethod
    def _page(after, limit):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ChatError("invalid cursor or page size")

    async def get_message(self, message_id):
        raw = await self.redis.hget(self.key("messages"), str(message_id))
        if raw is None:
            raise ChatError("message missing")
        return message_from_json(raw)

    async def sync(self, user_id, after=0, limit=100):
        self._page(after, limit)
        ids = await self.redis.zrangebyscore(self.user_key("inbox", user_id), f"({after}", "+inf",
                                            start=0, num=limit)
        if not ids:
            return []
        rows = await self.redis.hmget(self.key("messages"), ids)
        return [message_from_json(row) for row in rows]

    async def history(self, channel_id, user_id, after=0, limit=100):
        self._page(after, limit)
        if user_id not in (await self.get_channel(channel_id)).members:
            raise ChatError("not a channel member")
        found = []
        cursor = after
        while len(found) < limit:
            ids = await self.redis.zrangebyscore(self.key("history:" + channel_id), f"({cursor}", "+inf",
                                                start=0, num=limit)
            if not ids:
                break
            for message_id in ids:
                if await self.redis.zscore(self.user_key("inbox", user_id), message_id) is not None:
                    found.append(await self.get_message(message_id))
                    if len(found) == limit:
                        break
            cursor = int(ids[-1])
        return found

    async def renew_node(self, node_id, instance, url, region, capacity, lease):
        record = json.dumps({"server_id": node_id, "instance": instance, "url": url,
                             "region": region, "capacity": capacity})
        return bool(await self.redis.eval(_LEASE, 2, self.key("nodes"), self.key("node_expiry"),
                                          node_id, instance, record, lease))

    async def remove_node(self, node_id, instance):
        await self.redis.eval("""
            local raw=redis.call('HGET',KEYS[1],ARGV[1])
            if raw and cjson.decode(raw).instance==ARGV[2] then
                redis.call('HDEL',KEYS[1],ARGV[1]); redis.call('ZREM',KEYS[2],ARGV[1])
            end
        """, 2, self.key("nodes"), self.key("node_expiry"), node_id, instance)

    async def checkpoint(self, node_id, instance, cursor):
        return bool(await self.redis.eval("""
            local time=redis.call('TIME'); local now=tonumber(time[1])+tonumber(time[2])/1000000
            local raw=redis.call('HGET',KEYS[1],ARGV[1])
            if not raw or cjson.decode(raw).instance~=ARGV[2] or
                tonumber(redis.call('ZSCORE',KEYS[2],ARGV[1]) or '0')<=now then return 0 end
            redis.call('HSET',KEYS[3],ARGV[1],ARGV[3]); return 1
        """, 3, self.key("nodes"), self.key("node_expiry"), self.key("node_cursors"),
            node_id, instance, cursor))

    async def admit(self, user, device, session_id, node, instance, ttl, capacity):
        device_key = self.device_key(user, device)
        record = json.dumps({"user": user, "device": device, "session": session_id,
                             "node": node, "instance": instance})
        return bool(await self.redis.eval(_ADMIT, 5, self.key("nodes"), self.key("node_expiry"),
                    self.key("sessions"), self.user_key("devices", user), self.key("node_devices:" + node),
                    node, instance, device_key, record, ttl, capacity))

    async def session_operation(self, user, device, session_id, node, operation="check", ttl=30):
        return bool(await self.redis.eval(_SESSION, 5, self.key("sessions"), self.user_key("devices", user),
                    self.key("node_devices:" + node), self.key("nodes"), self.key("node_expiry"),
                    self.device_key(user, device), session_id, operation, ttl))

    async def is_online(self, user):
        return bool(await self.redis.eval(_ONLINE, 4, self.user_key("devices", user), self.key("sessions"),
                                          self.key("nodes"), self.key("node_expiry")))

    async def select(self, region=None):
        from .discovery import Server
        now = await self.now()
        live = await self.redis.zrangebyscore(self.key("node_expiry"), f"({now}", "+inf")
        servers = []
        for node in live:
            raw = await self.redis.hget(self.key("nodes"), node)
            if not raw:
                continue
            value = json.loads(raw)
            # Filter fenced devices left in a former node's capacity index.
            devices = await self.redis.zrangebyscore(self.key("node_devices:" + node), f"({now}", "+inf")
            connections = 0
            for device in devices:
                record = await self.redis.hget(self.key("sessions"), device)
                if record:
                    s = json.loads(record)
                    if s["node"] == node and s["instance"] == value["instance"]:
                        connections += 1
            if connections < value["capacity"]:
                servers.append(Server(node, value["url"], value["region"], value["capacity"], connections))
        if not servers:
            raise ChatError("no chat server available")
        return min(servers, key=lambda s: (region is not None and s.region != region,
                                           s.connections / s.capacity, s.server_id))
