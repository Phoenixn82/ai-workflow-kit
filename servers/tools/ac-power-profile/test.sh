#!/usr/bin/env bash
# Offline check of ac-power-profile's decision: fake power supplies and host-heat state, --dry-run only.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd); t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
mk() { mkdir -p "$t/ps/$1"; echo "$2" > "$t/ps/$1/type"; echo "$3" > "$t/ps/$1/online"; }
run() { AC_POWER_SYS=$t/ps HOST_HEAT_STATE=$t/heat.json sh "$here/ac-power-profile" --dry-run; }
mk BAT0 Battery 0; mk ADP1 Mains 0
[ "$(run)" = "ac-power-profile: would set power-saver" ] || { echo "FAIL battery: $(run)"; exit 1; }
echo 1 > "$t/ps/ADP1/online"
[ "$(run)" = "ac-power-profile: would set performance" ] || { echo "FAIL plugged: $(run)"; exit 1; }
echo '{"level": 2, "mit": {"active": true, "orig": {}}}' > "$t/heat.json"
run | grep -q "host-heat is cooling" || { echo "FAIL heat: $(run)"; exit 1; }
echo '{"level": 0, "mit": {"active": false, "orig": {}}}' > "$t/heat.json"
[ "$(run)" = "ac-power-profile: would set performance" ] || { echo "FAIL cooled: $(run)"; exit 1; }
echo "ac-power-profile test: ok"
