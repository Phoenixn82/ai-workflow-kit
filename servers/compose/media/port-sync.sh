#!/usr/bin/env bash
# port-sync.sh: run every 2 minutes (media-port-sync.timer).
#  1. If gluetun was recreated, transmission/prowlarr/flaresolverr keep running with NO network
#     (they are stuck in the old namespace). Detect that and force-recreate them.
#  2. Keep Transmission's peer port equal to the VPN's forwarded port (it can change on reconnect).
# Usage: port-sync.sh [COMPOSE_DIR]   (default: the directory this script is in)
set -uo pipefail
cd "${1:-$(dirname "$(readlink -f "$0")")}" || exit 1
[ "$(docker inspect -f '{{.State.Health.Status}}' gluetun 2>/dev/null)" = healthy ] || exit 0
if ! docker exec transmission grep -q 'tun0:' /proc/net/dev 2>/dev/null; then
  docker compose --profile arr up -d --force-recreate transmission prowlarr flaresolverr >/dev/null 2>&1
  logger -t port-sync "re-attached transmission/prowlarr/flaresolverr to gluetun"
  sleep 15
fi
port=$(docker exec gluetun cat /tmp/gluetun/forwarded_port 2>/dev/null) || exit 0
[ -n "$port" ] || exit 0
set -a; . ./.env; set +a
auth="$TRANSMISSION_USER:$TRANSMISSION_PASS"
cur=$(docker exec transmission transmission-remote 127.0.0.1:9091 -n "$auth" -si 2>/dev/null | awk '/Listen port/{print $3}')
if [ -n "$cur" ] && [ "$cur" != "$port" ]; then
  docker exec transmission transmission-remote 127.0.0.1:9091 -n "$auth" -p "$port" >/dev/null &&
    logger -t port-sync "transmission peer port $cur -> $port"
fi
exit 0
