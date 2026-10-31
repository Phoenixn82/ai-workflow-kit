#!/usr/bin/env bash
# healthcheck.sh URL [TRIES] [DELAY]: exit 0 as soon as URL answers 2xx or 3xx, else 1 after TRIES attempts
# (default 30) DELAY seconds apart (default 2). deploy.sh and restore.sh run it after a start; Uptime Kuma
# watches the same URL afterwards (an HTTP monitor), so a project that breaks later still raises an alert.
set -euo pipefail
url=${1:?usage: healthcheck.sh URL [TRIES] [DELAY]}
tries=${2:-30} delay=${3:-2}
[[ $tries =~ ^[1-9][0-9]*$ && $delay =~ ^[0-9]+$ ]] || { echo "healthcheck.sh: TRIES and DELAY are numbers" >&2; exit 2; }
for ((i = 1; i <= tries; i++)); do
  code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" || true)
  if [[ $code =~ ^[23] ]]; then echo "healthy: $url ($code, try $i)"; exit 0; fi
  [ "$i" = "$tries" ] || sleep "$delay"
done
echo "UNHEALTHY: $url (last answer: ${code:-none}) after $tries tries" >&2
exit 1
