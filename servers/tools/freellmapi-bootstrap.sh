#!/usr/bin/env bash
# freellmapi-bootstrap.sh [INSTALL_DIR]
# Install FreeLLMAPI (an OpenAI-compatible router over free-tier model providers) as a loopback-only
# `systemd --user` service on port 3001. Run as the user who will own it (NOT root), on thor.
#
#   - clones upstream into INSTALL_DIR (default ./freellmapi; refuses an existing path), checks out the pinned,
#     reviewed commit below and installs exactly its lockfile (npm ci)
#   - generates a fresh local encryption key into INSTALL_DIR/.env (mode 600)
#   - an existing ~/.config/systemd/user/freellmapi.service that differs is moved to
#     freellmapi.service.bak-<timestamp> (never over an earlier backup) before the new one is written
#   - enables linger, so the user service starts at boot without anyone logged in
#   - checks it answers on 127.0.0.1:3001 (/api/ping: /v1 needs an API key) and that nothing listens beyond loopback
#
# Needs: git, node + npm, a C/C++ toolchain (build-essential) and python3 for the native sqlite module.
# Provider API keys are NOT part of this script: add them in the FreeLLMAPI dashboard afterwards.
# To reach it from other tailnet machines without opening it to the LAN:
#   tailscale serve --bg --http=3001 http://127.0.0.1:3001
set -euo pipefail

upstream_url='https://github.com/tashfeenahmed/freellmapi.git'
# Bump: pick a release at least two weeks old, read its package.json lifecycle scripts and package-lock.json
# (every package has an integrity hash), then change both lines and install/README.md's pin table.
pinned_commit='5d7b9b6f015b87923ec24e489ab233f4493e047e'   # v0.13.3 (2026-09-30), reviewed 2026-10-13
install_dir="${1:-$PWD/freellmapi}"

if [ "$(id -u)" = 0 ]; then
  echo 'Run this as your normal user, not root (it installs a user service).' >&2
  exit 1
fi
if [ -e "$install_dir" ]; then
  printf 'Refusing to replace existing path: %s\n' "$install_dir" >&2
  exit 1
fi
for required_command in git node npm systemctl loginctl curl ss; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$required_command" >&2
    exit 1
  fi
done

node_bin_dir="$(dirname "$(command -v node)")"
current_user="$(id -un)"
unit_dir="$HOME/.config/systemd/user"
unit_file="$unit_dir/freellmapi.service"

echo "+ git clone $upstream_url $install_dir && git checkout $pinned_commit"
git clone --no-checkout "$upstream_url" "$install_dir"
cd "$install_dir"
git -c advice.detachedHead=false checkout --detach "$pinned_commit"
if [ "$(git rev-parse HEAD)" != "$pinned_commit" ]; then
  echo "Checkout is not the pinned commit $pinned_commit; refusing to build it." >&2
  exit 1
fi
echo '+ npm ci && npm run build'
# Exactly the lockfile (integrity-checked), every package fetched from the configured registry.
npm ci --replace-registry-host=always
# Copied trees can lose executable bits on npm shims; restore them.
find "$install_dir" -path '*/node_modules/.bin/*' -type f -exec chmod u+x {} +
# Rebuild the native sqlite module for this exact Node version (newer Node may lack a prebuilt binary).
npm rebuild better-sqlite3 --foreground-scripts
npm run build

umask 077
key="$(node -e "process.stdout.write(require('crypto').randomBytes(32).toString('hex'))")"
printf 'ENCRYPTION_KEY=%s\nPORT=3001\nHOST=127.0.0.1\n' "$key" > "$install_dir/.env"
chmod 600 "$install_dir/.env"
unset key

install -d -m 700 "$unit_dir"
new_unit="$(mktemp "$unit_dir/.freellmapi.service.XXXXXX")"
trap 'rm -f "$new_unit"' EXIT
cat > "$new_unit" <<UNIT
[Unit]
Description=FreeLLMAPI local free-model router (loopback only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$install_dir
EnvironmentFile=$install_dir/.env
Environment=PATH=$node_bin_dir:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/env node server/dist/index.js
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT
chmod 644 "$new_unit"
if [ -e "$unit_file" ] || [ -L "$unit_file" ]; then
  if [ -f "$unit_file" ] && [ ! -L "$unit_file" ] && cmp -s "$new_unit" "$unit_file"; then
    echo "= $unit_file is already this unit"
  else
    base="$unit_file.bak-$(date +%Y%m%d-%H%M%S)"
    backup=$base n=1
    while [ -e "$backup" ] || [ -L "$backup" ]; do backup="$base-$n"; n=$((n + 1)); done
    mv -n -- "$unit_file" "$backup" || true
    if [ -e "$unit_file" ] || [ -L "$unit_file" ]; then
      echo "Could not move $unit_file aside to $backup; nothing replaced." >&2
      exit 1
    fi
    echo "+ your previous freellmapi.service kept as $backup (move it back to undo)"
  fi
fi
if [ -d "$unit_file.d" ]; then
  echo "! drop-ins in $unit_file.d/ still apply to the new unit: check them"
fi
mv -f -- "$new_unit" "$unit_file"
trap - EXIT

if [ "$(loginctl show-user "$current_user" -p Linger --value 2>/dev/null)" != 'yes' ]; then
  echo "+ loginctl enable-linger $current_user"
  loginctl enable-linger "$current_user"
fi

systemctl --user daemon-reload
systemctl --user enable freellmapi.service
systemctl --user restart freellmapi.service   # restart, not start: a replaced unit must run the new checkout
for _ in $(seq 1 15); do
  curl -sf --max-time 3 http://127.0.0.1:3001/api/ping >/dev/null && break
  sleep 2
done
curl -sS --fail --max-time 15 http://127.0.0.1:3001/api/ping >/dev/null

listeners="$(ss -H -ltn 'sport = :3001')"
case "$listeners" in
  *'127.0.0.1:3001'*) ;;
  *) echo 'FreeLLMAPI did not bind to 127.0.0.1:3001.' >&2; exit 1 ;;
esac
case "$listeners" in
  *'0.0.0.0:3001'*|*'[::]:3001'*|*'*:3001'*)
    echo 'FreeLLMAPI is listening beyond loopback; refusing to report success.' >&2
    exit 1 ;;
esac

echo 'FreeLLMAPI is active on 127.0.0.1:3001. Add provider keys in its dashboard (http://127.0.0.1:3001/).'
echo 'From another machine: ssh -L 3001:127.0.0.1:3001 thor   (or use tailscale serve, see header)'
