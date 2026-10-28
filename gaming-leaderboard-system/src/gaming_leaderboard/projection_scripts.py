"""Redis owns atomic projection snapshots; validate before the first mutation."""

SNAPSHOT = r"""
local root=KEYS[1]
for _,name in ipairs({'lineage','position','epoch'}) do
  local t=redis.call('TYPE',root..':'..name).ok
  if t~='none' and t~='string' then return redis.error_reply('TYPE corrupt') end
end
local lineage=redis.call('GET',root..':lineage')
local raw=redis.call('GET',root..':position')
local pos=tonumber(raw or '0')
if not pos or pos<0 or pos%1~=0 or pos>9007199254740991 then return redis.error_reply('POSITION corrupt') end
if lineage and not raw then return redis.error_reply('POSITION missing') end
return cjson.encode({lineage=lineage or cjson.null,position=pos,epoch=redis.call('GET',root..':epoch') or cjson.null})
"""

APPLY = r"""
local root=KEYS[1]
local batch=cjson.decode(ARGV[1])
for _,name in ipairs({'lineage','position','epoch'}) do
  local t=redis.call('TYPE',root..':'..name).ok
  if t~='none' and t~='string' then return redis.error_reply('TYPE corrupt') end
end
local mt=redis.call('TYPE',root..':months').ok
if mt~='none' and mt~='set' then return redis.error_reply('TYPE corrupt') end
local generation=redis.call('GET',root..':lineage')
if generation and generation~=batch.lineage then return redis.error_reply('LINEAGE changed') end
local epoch=redis.call('GET',root..':epoch') or cjson.null
if epoch~=batch.epoch then return redis.error_reply('EPOCH changed') end
local pos=tonumber(redis.call('GET',root..':position') or '0')
if not pos or pos<0 or pos%1~=0 then return redis.error_reply('POSITION corrupt') end
if pos~=batch.after then return cjson.encode({stale=true,position=pos,epoch=epoch}) end
local nextpos=pos
local planned={}
if #batch.events==0 and generation==batch.lineage and epoch~=cjson.null then
  return cjson.encode({position=pos,epoch=epoch})
end
for _,e in ipairs(batch.events) do
  if e.seq~=nextpos+1 then return redis.error_reply('OFFSET gap') end
  local p=e.payload
  for _,kind in ipairs({'score','order'}) do
    local t=redis.call('TYPE',root..':'..kind..':'..p.month).ok
    if t~='none' and t~='zset' then return redis.error_reply('TYPE corrupt') end
  end
  local sk=root..':score:'..p.month
  local ok=root..':order:'..p.month
  if redis.call('ZCARD',sk)~=redis.call('ZCARD',ok) then return redis.error_reply('INDEX corrupt') end
  local id=p.month..':'..p.user
  local previous=planned[id]
  if previous==nil then
    local old=redis.call('ZSCORE',sk,p.user)
    if old then
      previous=-tonumber(old)
      if not previous or previous<0 or previous>1000000000 or previous%1~=0 then return redis.error_reply('SCORE corrupt') end
      local member=string.format('%010d',1000000000-previous)..':'..p.user
      if redis.call('ZSCORE',ok,member)~='0' then return redis.error_reply('INDEX corrupt') end
      if redis.call('SISMEMBER',root..':months',p.month)~=1 then return redis.error_reply('MONTH index corrupt') end
    else
      previous=-1
    end
  end
  if previous==-1 then
    if p.score~=0 then return redis.error_reply('SCORE initial event missing') end
  elseif p.score<previous or p.score>previous+1 then
    return redis.error_reply('SCORE noncontiguous')
  end
  planned[id]=p.score
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
if epoch==cjson.null then epoch=batch.next_epoch end
redis.call('SET',root..':lineage',batch.lineage)
redis.call('SET',root..':position',nextpos)
redis.call('SET',root..':epoch',epoch)
return cjson.encode({position=nextpos,epoch=epoch})
"""

QUERY = r"""
local root=KEYS[1]
local q=cjson.decode(ARGV[1])
if redis.call('GET',root..':lineage')~=q.lineage then return redis.error_reply('NOTREADY lineage') end
local epoch=redis.call('GET',root..':epoch')
if not epoch or (q.epoch and epoch~=q.epoch) then return redis.error_reply('NOTREADY epoch') end
local pos=tonumber(redis.call('GET',root..':position') or '0')
if not pos or pos<q.minimum then return redis.error_reply('NOTREADY position') end
local sk=root..':score:'..q.month
local ok=root..':order:'..q.month
if redis.call('ZCARD',sk)~=redis.call('ZCARD',ok) then return redis.error_reply('INDEX corrupt; rebuild required') end
local function rows(members)
  local result={}
  for _,m in ipairs(members) do
    local user=string.sub(m,12)
    local score=1000000000-tonumber(string.sub(m,1,10))
    if string.sub(m,11,11)~=':' or not score or score<0 or score>1000000000 or redis.call('ZSCORE',sk,user)~=tostring(-score) then
      -- Redis may render negative zero as "0"; compare numerically below.
      local actual=tonumber(redis.call('ZSCORE',sk,user))
      if string.sub(m,11,11)~=':' or not score or score<0 or score>1000000000 or not actual or actual~=-score then return redis.error_reply('INDEX corrupt') end
    end
    table.insert(result,{user=user,score=score,lex=m})
  end
  return result
end
local result={position=pos,shard=q.shard,lineage=q.lineage,epoch=epoch}
if q.mode=='top' then
  result.rows=rows(redis.call('ZRANGE',ok,0,q.limit-1))
elseif q.mode=='target' then
  local score=redis.call('ZSCORE',sk,q.user)
  if score then
    local member=string.format('%010d',1000000000+tonumber(score))..':'..q.user
    local found=rows({member})
    if redis.call('ZSCORE',ok,member)~='0' then return redis.error_reply('INDEX corrupt') end
    result.target=found[1]
  end
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
local t=redis.call('TYPE',root..':months').ok
if t~='none' and t~='set' then return redis.error_reply('MONTH index corrupt') end
if redis.call('SCARD',root..':months')>12000 then return redis.error_reply('MONTH budget exceeded') end
local months=redis.call('SMEMBERS',root..':months')
for _,month in ipairs(months) do
  if not string.match(month,'^%d%d%d%d%-%d%d$') then return redis.error_reply('MONTH index corrupt') end
end
for _,month in ipairs(months) do redis.call('DEL',root..':score:'..month,root..':order:'..month) end
redis.call('DEL',root..':months',root..':lineage',root..':position')
redis.call('SET',root..':epoch',ARGV[1])
return ARGV[1]
"""
