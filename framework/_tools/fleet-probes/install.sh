#!/usr/bin/env bash
# Install the fleet probes from odin: freellmapi-probe on heimdall, disk-probe on thor. Copies scripts + units,
# writes the two secrets host-to-host (router key -> heimdall, Telegram chat id -> thor; never printed) and
# daemon-reloads. It does NOT enable the timers: the enable lines are printed at the end (README step 3).
#   install.sh --dry-run    print every command, run none
#   install.sh              do it
set -euo pipefail
cd "$(dirname "$0")"
# pipefail: a failed key/chat-id fetch must stop the install, and the remote side writes .new and moves it only
# when non-empty, so a failed fetch never truncates a working secret file
run() { if [ "${DRY:-}" ]; then printf '+ %s\n' "$*"; else bash -o pipefail -c "$*"; fi; }
case "${1:-}" in --dry-run) DRY=1 ;; "") ;; *) echo "usage: install.sh [--dry-run]" >&2; exit 2 ;; esac
[ "${DRY:-}" ] || ~/.local/bin/thor-up >/dev/null  # thor suspends when idle

# heimdall: freellmapi-probe
run "ssh heimdall 'mkdir -p ~/.local/bin ~/.config/systemd/user'"
run "scp -q freellmapi-probe heimdall:.local/bin/freellmapi-probe"
run "scp -q units/freellmapi-probe.service units/freellmapi-probe.timer heimdall:.config/systemd/user/"
# ponytail: same fetch as _tools/lanes/lanes/router.py key(); the key goes thor -> odin pipe -> heimdall, never to a tty
run "ssh thor 'cd ~/freellmapi-docker && curl -sf -H \"Authorization: Bearer \$(cat .admin-session)\" http://127.0.0.1:3001/api/settings/api-key' | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"apiKey\"])' | ssh heimdall 'umask 077; mkdir -p ~/.config/freellmapi-probe && cat > ~/.config/freellmapi-probe/key.new && test -s ~/.config/freellmapi-probe/key.new && mv ~/.config/freellmapi-probe/key.new ~/.config/freellmapi-probe/key'"
run "ssh heimdall 'chmod +x ~/.local/bin/freellmapi-probe && ~/.local/bin/freellmapi-probe --self-test && systemctl --user daemon-reload'"

# thor: disk-probe
run "ssh thor 'mkdir -p ~/.local/bin ~/.config/systemd/user'"
run "scp -q disk-probe thor:.local/bin/disk-probe"
run "scp -q units/disk-probe.service units/disk-probe.timer thor:.config/systemd/user/"
run "ssh heimdall 'cat ~/services/uptime-kuma/telegram-chat-id' | ssh thor 'umask 077; mkdir -p ~/.config/disk-probe && cat > ~/.config/disk-probe/chat-id.new && test -s ~/.config/disk-probe/chat-id.new && mv ~/.config/disk-probe/chat-id.new ~/.config/disk-probe/chat-id'"
run "ssh thor 'chmod +x ~/.local/bin/disk-probe && ~/.local/bin/disk-probe --self-test && systemctl --user daemon-reload'"

echo "Installed, NOT enabled. To enable (the user/orchestrator decision):"
echo "  ssh heimdall systemctl --user enable --now freellmapi-probe.timer"
echo "  ssh thor systemctl --user enable --now disk-probe.timer"
