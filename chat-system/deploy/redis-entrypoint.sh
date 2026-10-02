#!/bin/sh
set -eu
# Preserve CONFIG REWRITE / Sentinel topology changes across container restarts.
config=/data/redis.conf
if [ ! -f "$config" ]; then
  if [ "${REDIS_ROLE:-server}" = sentinel ]; then
    cat > "$config" <<EOF
port 26379
bind 0.0.0.0
protected-mode no
dir /data
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel announce-ip ${REDIS_HOST}
sentinel monitor chat-primary redis-1 6379 2
sentinel down-after-milliseconds chat-primary 3000
sentinel failover-timeout chat-primary 15000
sentinel parallel-syncs chat-primary 1
EOF
  else
    cat > "$config" <<EOF
port 6379
bind 0.0.0.0
protected-mode no
dir /data
appendonly yes
appendfsync always
maxmemory-policy noeviction
repl-diskless-sync-delay 0
min-replicas-to-write 1
min-replicas-max-lag 5
replica-announce-ip ${REDIS_HOST}
EOF
    if [ "${REDIS_ROLE:-server}" = replica ]; then
      echo 'replicaof redis-1 6379' >> "$config"
    fi
  fi
fi
if [ "${REDIS_ROLE:-server}" = sentinel ]; then
  exec redis-server "$config" --sentinel
fi
exec redis-server "$config"
