"""Atomic Redis programs; arguments and return codes are owned by RedisBackend."""

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


_ADMIT = """
local time = redis.call('TIME'); local now = tonumber(time[1])+tonumber(time[2])/1000000
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw or cjson.decode(raw).instance ~= ARGV[2] then return 0 end
if tonumber(redis.call('ZSCORE', KEYS[2], ARGV[1]) or '0') <= now then return 0 end
local old = redis.call('HGET', KEYS[3], ARGV[3])
if (old or '') ~= ARGV[7] then return -1 end
local count=redis.call('ZCOUNT',KEYS[5],'('..now,'+inf')
if tonumber(redis.call('ZSCORE',KEYS[5],ARGV[3]) or '0')>now then count=count-1 end
if count >= tonumber(ARGV[6]) then return 0 end
if old then redis.call('ZREM',KEYS[6],ARGV[3]) end
local record = cjson.decode(ARGV[4]); record.expires = now+tonumber(ARGV[5])
redis.call('HSET', KEYS[3], ARGV[3], cjson.encode(record))
redis.call('ZADD', KEYS[4], record.expires, ARGV[3])
redis.call('ZADD', KEYS[5], record.expires, ARGV[3])
redis.call('ZADD', KEYS[7], record.expires, ARGV[3])
redis.call('EXPIRE',KEYS[4],math.ceil(tonumber(ARGV[5])*2))
redis.call('EXPIRE',KEYS[5],math.ceil(tonumber(ARGV[5])*2))
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
    redis.call('ZREM', KEYS[3], ARGV[1]); redis.call('ZREM',KEYS[6],ARGV[1]); return 1
end
if ARGV[5]~='' and redis.call('HGET',KEYS[7],ARGV[5])~=s.user then return 0 end
if s.expires <= now then return 0 end
local node = redis.call('HGET', KEYS[4], s.node)
if not node or cjson.decode(node).instance ~= s.instance then return 0 end
if tonumber(redis.call('ZSCORE', KEYS[5], s.node) or '0') <= now then return 0 end
if ARGV[3] == 'heartbeat' then
    s.expires = now+tonumber(ARGV[4])
    redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(s))
    redis.call('ZADD', KEYS[2], s.expires, ARGV[1])
    redis.call('ZADD', KEYS[3], s.expires, ARGV[1])
    redis.call('ZADD', KEYS[6], s.expires, ARGV[1])
    redis.call('EXPIRE',KEYS[2],math.ceil(tonumber(ARGV[4])*2))
    redis.call('EXPIRE',KEYS[3],math.ceil(tonumber(ARGV[4])*2))
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
