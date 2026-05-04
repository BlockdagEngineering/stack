#!/bin/sh
# Fix ownership of persisted paths on every container start (named/bind volumes
# are often populated as root → bdagStack cannot open bdageth/chaindata/ancient/*).
set -eu
if [ "$(id -u)" = 0 ]; then
  mkdir -p /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack
  chown -R bdagStack:bdagStack /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack || true
  exec runuser -u bdagStack -g bdagStack -- "$@"
fi
exec "$@"
