#!/usr/bin/env bash
# tailscale-serve.sh NAME [off]: publish a project on the tailnet only, with HTTPS, at
#   https://<heimdall>.<your-tailnet>.ts.net:<APP_PORT>      (turn on HTTPS once: admin console > DNS > HTTPS)
# The project keeps listening on 127.0.0.1 only; nothing is opened to the LAN or the internet. This is the default
# way to reach a project; use Caddy (deploy/caddy) only for one that must be on the public internet.
# Runs tailscale as you (setup.sh made you its operator), else with sudo (or once: sudo tailscale set --operator=$USER).
set -euo pipefail
name=${1:?usage: tailscale-serve.sh NAME [off]}
env=${APPS_DIR:-$HOME/apps}/$name/.env
port=$(sed -n 's/^APP_PORT=//p' "$env" | tail -n 1)
[[ $port =~ ^[0-9]+$ ]] || { echo "tailscale-serve.sh: no APP_PORT in $env" >&2; exit 1; }
# tailscaled refuses a change from a user who is not its operator (its socket is open to all): then retry with sudo
ts() { tailscale "$@" 2>/dev/null || sudo tailscale "$@"; }
if [ "${2:-}" = off ]; then ts serve --https="$port" off
else ts serve --bg --https="$port" "http://127.0.0.1:$port"; fi
tailscale serve status
