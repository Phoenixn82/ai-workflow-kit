#!/usr/bin/env bash
# setup.sh <base|loki|heimdall|thor> [--dry-run]
#
# Turns a Mac into a headless home server lane: the server half of the loki, heimdall and thor profiles
# (profiles/<name>/README.md). Every lane includes "base". Old names still work: media = loki, workhorse = thor.
# Run it from the kit's root folder (your clone), as your normal user (NOT with sudo; it asks for your password
# when it needs it):
#     servers/macos/setup.sh heimdall --dry-run      preview, changes nothing
#     servers/macos/setup.sh heimdall
# Needs Homebrew (https://brew.sh). Safe to re-run: it checks before changing anything, keeps a file it replaces
# beside it (<file>.bak-<time>) and prints every action ("+" = will do / did, "=" = already so).
#
#   base       never sleep (lid closed too), wake for network access, restart after power loss,
#              Remote Login keys-only, Tailscale, firewall, automatic security updates, alerts
#   loki       base + Jellyfin app + colima/Docker for the optional download stack
#   heimdall   base + colima/Docker (uptime-kuma, ntfy) + wake-watch every 2 minutes (launchd)
#   thor       base + Ollama (tailnet only). The Mac never sleeps (thor-sleep is Linux-only).
#
# Tailscale and the apps that run as your user (Jellyfin, colima, Ollama) start only after login: turn on automatic
# login (System Settings > Users & Groups; needs FileVault off; read servers/common.md about that tradeoff).
set -euo pipefail

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
LANE=""
DRY=0
for a in "$@"; do
  case "$a" in
    base|heimdall) [ -z "$LANE" ] || usage; LANE=$a ;;
    loki|media) [ -z "$LANE" ] || usage; LANE=loki ;;
    thor|workhorse) [ -z "$LANE" ] || usage; LANE=thor ;;
    --dry-run|-n) DRY=1 ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $a" >&2; usage ;;
  esac
done
[ -n "$LANE" ] || usage
[ "$(uname -s)" = Darwin ] || { echo "setup.sh: this is the macOS script (see ../linux or ../windows)" >&2; exit 1; }
[ "$(id -u)" != 0 ] || { echo "setup.sh: run as your normal user, not with sudo" >&2; exit 1; }
ME=$(id -un)   # wake-watch runs as you; the name goes into a plist and an ACL entry, so a plain account name only
case "$ME" in ''|*[!A-Za-z0-9._-]*) echo "setup.sh: account name '$ME' is not a plain one (letters, digits, . _ -)" >&2; exit 1 ;; esac

HERE=$(cd "$(dirname "$0")" && pwd)
KIT=$(cd "$HERE/.." && pwd)
STACKS=$HOME/homeserver
NEXT=()
HOST=$(scutil --get LocalHostName 2>/dev/null || hostname -s)
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale   # the Tailscale app's command line
# thor's pf guard (audit R4-1), run as root by the org.homeserver.pf LaunchDaemon at every boot and by setup.sh before
# it lets Ollama listen on every interface: load the rule (pf on), and only then write this boot's id into PF_READY.
# kern.bootsessionuuid is new at every boot (kern.boottime is not an id: macOS moves it when the clock is set), so a
# PF_READY left from an earlier boot never counts, however /var/run is cleaned. A failed load removes it. umask 022:
# the agent reads it as you, also when sudo hands down a stricter umask. macOS's own programs and root-owned paths only
# (R2-2).
PF_READY=/var/run/org.homeserver.pf.ready
PF_GUARD='umask 022; u=$(/usr/sbin/sysctl -n kern.bootsessionuuid) && [ -n "$u" ] && /sbin/pfctl -E -a com.apple/homeserver -f /etc/pf.anchors/homeserver && echo "$u" > /var/run/org.homeserver.pf.ready || { /bin/rm -f /var/run/org.homeserver.pf.ready; exit 1; }'
# What the Ollama LaunchAgent runs, as you, before Ollama: wait until PF_READY holds this boot's id, then start it. So
# after a restart nothing listens on every interface before the guard loaded the rule, nor ever when it failed.
OLLAMA_GATE='r=/var/run/org.homeserver.pf.ready
until u=$(/usr/sbin/sysctl -n kern.bootsessionuuid) && [ -n "$u" ] && [ "$(/bin/cat "$r" 2>/dev/null)" = "$u" ]; do
  [ -n "${said:-}" ] || echo "waiting for the pf rule (org.homeserver.pf) before Ollama listens on every interface" >&2
  said=1; /bin/sleep 2
done
exec "$@"'

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
run()  { printf '  + %s\n' "$*"; if [ "$DRY" = 0 ]; then "$@"; fi; }
next() { NEXT+=("$*"); }
# backup FILE [sudo] : the copy about to be replaced stays beside it as FILE.bak-<time>, without exec bits. In the
# folders a daemon reads whole (LaunchDaemons, LaunchAgents, sshd_config.d: launchd and macOS's sshd Include take
# every file name but dotfiles) it is .FILE.bak-<time>, so the old copy is never loaded next to the new one.
backup() {
  local f=$1 su=${2:-} ts b
  ts=$(date +%Y%m%d-%H%M%S); b="$f.bak-$ts"
  case "$f" in */LaunchDaemons/*|*/LaunchAgents/*|*/sshd_config.d/*) b="${f%/*}/.${f##*/}.bak-$ts" ;; esac
  printf '  + back up %s -> %s\n' "$f" "$b"
  [ "$DRY" = 0 ] || return 0
  if [ -n "$su" ]; then sudo cp -p "$f" "$b" && sudo chmod a-x "$b"; else cp -p "$f" "$b" && chmod a-x "$b"; fi
}
# put DEST MODE [sudo] < content : write only if different (a changed file is backed up first)
CHANGED=0
put() {
  local dest=$1 mode=$2 su=${3:-} tmp
  tmp=$(mktemp)
  cat > "$tmp"
  if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
    printf '  = %s (unchanged)\n' "$dest"; CHANGED=0
  else
    [ ! -f "$dest" ] || backup "$dest" "$su"
    printf '  + write %s\n' "$dest"; CHANGED=1
    if [ "$DRY" = 0 ]; then
      if [ -n "$su" ]; then sudo mkdir -p "$(dirname "$dest")"; sudo install -m "$mode" "$tmp" "$dest"
      else mkdir -p "$(dirname "$dest")"; install -m "$mode" "$tmp" "$dest"; fi
    fi
  fi
  rm -f "$tmp"
}
seed() { if [ -e "$1" ]; then printf '  = %s (exists, kept)\n' "$1"; cat > /dev/null; else put "$@"; fi; }
# xml TEXT: TEXT as the content of a plist <string>
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
# tailnet_addr ADDRESS: it is a Tailscale address (100.64.0.0/10 or fd7a:115c:a1e0::/48)
tailnet_addr() {
  local o
  case "$1" in
    [Ff][Dd]7[Aa]:115[Cc]:[Aa]1[Ee]0:*) return 0 ;;
    100.*) o=${1#100.}; o=${o%%.*}
           case "$o" in ''|*[!0-9]*) ;; *) if [ "$o" -ge 64 ] && [ "$o" -le 127 ]; then return 0; fi ;; esac ;;
  esac
  return 1
}
# on_tailnet: this script runs in an SSH session that came in over Tailscale
on_tailnet() {
  local a
  set -- ${SSH_CONNECTION:-}   # client-address client-port server-address server-port
  for a in "${1:-}" "${3:-}"; do
    if tailnet_addr "$a"; then return 0; fi
  done
  return 1
}
# unsure_why: prints why SSH_CONNECTION may not show how you reach this Mac now, or returns 1 when it does. A shell
# inside tmux or screen (or zellij, herdr, dtach, abduco) keeps the value from when it started, whatever you attach
# over later (audit R3-3), and so does one under anything else that outlives its connection (an editor's remote
# server, mosh, nohup): the value is this connection's only with its sshd above this shell, and says how you reach
# this Mac only when it comes from another computer (an ssh to this Mac from itself, a forward or a tunnel rides inside
# another connection). Without one it is a terminal at the Mac's screen only when the screen is yours and no sshd is
# above this shell. Whatever it is, an ssh or Screen Sharing connection open from a Tailscale address may be yours.
unsure_why() {
  local pid=$$ n=0 out ppid comm sshd=0 console proto rq sq la fa st
  if [ -n "${TMUX:-}" ]; then echo "this shell runs inside tmux (TMUX is set)"; return 0; fi
  if [ -n "${STY:-}" ]; then echo "this shell runs inside screen (STY is set)"; return 0; fi
  if [ -n "${ZELLIJ:-}" ]; then echo "this shell runs inside zellij (ZELLIJ is set)"; return 0; fi
  if [ -n "${HERDR_ENV:-}" ]; then echo "this shell runs inside herdr (HERDR_ENV is set)"; return 0; fi
  case "${TERM:-}" in
    screen*|tmux*) echo "TERM is $TERM, as inside tmux or screen (here, or on the computer you connect from)"; return 0 ;;
  esac
  while [ "$pid" -gt 1 ] && [ "$n" -lt 64 ]; do   # up the process tree; ps prints the parent and the program
    out=$(ps -o ppid= -o comm= -p "$pid" 2>/dev/null || true)
    out=${out#"${out%%[! ]*}"}; ppid=${out%% *}; comm=${out#* }
    comm=${comm%% *}; comm=${comm##*/}; comm=${comm#-}; comm=${comm%:}   # /opt/homebrew/bin/tmux, -zsh, "tmux: server"
    case "$ppid" in ''|*[!0-9]*) echo "its process tree cannot be read (ps -p $pid)"; return 0 ;; esac
    case "$comm" in
      tmux|tmux[!A-Za-z]*|screen|screen[!A-Za-z]*|SCREEN|SCREEN[!A-Za-z]*|zellij|herdr|dtach|abduco)
        echo "this shell runs inside $comm (process $pid)"; return 0 ;;
      sshd|sshd[!A-Za-z]*) sshd=1 ;;
    esac
    pid=$ppid; n=$((n + 1))
  done
  if [ -n "${SSH_CONNECTION:-}" ]; then
    if [ "$sshd" = 0 ]; then echo "SSH_CONNECTION is set but no sshd is above this shell: it came from an older session"; return 0; fi
    set -- $SSH_CONNECTION
    case "${1:-}" in 127.*|::1|"${3:-}")
      echo "this ssh session comes from this Mac itself (${1:-}), inside another connection"; return 0 ;;
    esac
  else
    if [ "$sshd" = 1 ]; then echo "SSH_CONNECTION is not set, yet this shell runs under sshd"; return 0; fi
    console=$(stat -f %Su /dev/console 2>/dev/null || true)
    if [ "$console" != "$ME" ]; then
      echo "SSH_CONNECTION is not set and the Mac's screen is ${console:-nobody}'s, not yours"; return 0
    fi
  fi
  # macOS netstat: local and foreign address end in .PORT; -W keeps long (IPv6) addresses whole
  while read -r proto rq sq la fa st; do
    case "$st" in ESTABLISHED*) ;; *) continue ;; esac
    case "${la##*.}" in 22|5900) ;; *) continue ;; esac
    if tailnet_addr "${fa%.*}"; then
      echo "an ssh or Screen Sharing connection (port ${la##*.}) from ${fa%.*} comes in over Tailscale now (end it before the re-run)"
      return 0
    fi
  done < <(netstat -anW -p tcp 2>/dev/null || netstat -an -p tcp 2>/dev/null || true)
  return 1
}

BREW=$(command -v brew || true)
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  if [ -z "$BREW" ] && [ -x "$b" ]; then BREW=$b; fi
done
if [ -z "$BREW" ]; then
  echo 'Homebrew is missing. Install it first (it asks questions, so this script does not do it for you):'
  echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  exit 1
fi
PREFIX=$("$BREW" --prefix)
brew_formula() { local f; for f in "$@"; do if "$BREW" list --formula "$f" >/dev/null 2>&1; then printf '  = brew %s\n' "$f"; else run "$BREW" install "$f"; fi; done; }
brew_cask()    { local c; for c in "$@"; do if "$BREW" list --cask "$c" >/dev/null 2>&1; then printf '  = brew cask %s\n' "$c"; else run "$BREW" install --cask "$c"; fi; done; }

# launchd job: daemon LABEL PLIST_CONTENT (starts at boot without login; as root unless the plist has a UserName).
# A job that runs as root runs only macOS's own programs and root-owned files: never anything in Homebrew's folder
# or elsewhere your account can change, or whoever gets into your account gets root at the job's next run.
daemon() {
  local label=$1 path=/Library/LaunchDaemons/$1.plist
  put "$path" 644 sudo
  if [ "$CHANGED" = 1 ]; then
    run sudo launchctl bootout "system/$label" 2>/dev/null || true
    run sudo launchctl bootstrap system "$path"
  elif ! sudo -n launchctl print "system/$label" >/dev/null 2>&1 && [ "$DRY" = 0 ]; then
    run sudo launchctl bootstrap system "$path"
  fi
}

# ---------------------------------------------------------------- base
do_base() {
  [ "$DRY" = 1 ] || sudo -v   # ask for the password once, up front

  say "Power: never sleep (also with the lid closed), wake for network access, restart after power loss"
  local kv k v cur
  for kv in sleep=0 disksleep=0 displaysleep=10 womp=1 autorestart=1 powernap=0 tcpkeepalive=1; do
    k=${kv%=*}; v=${kv#*=}
    cur=$(pmset -g 2>/dev/null | awk -v k="$k" '$1 == k {print $2}')
    if [ -z "$cur" ]; then note "$k: not supported on this Mac"
    elif [ "$cur" = "$v" ]; then printf '  = %s %s\n' "$k" "$v"
    else run sudo pmset -a "$k" "$v"; fi
  done
  if [ "$(pmset -g 2>/dev/null | awk '/SleepDisabled/ {print $2}')" = 1 ]; then printf '  = disablesleep 1\n'
  else run sudo pmset -a disablesleep 1; fi   # lid closed without external display: keep running

  say "Remote Login (SSH), keys only"
  if sudo -n systemsetup -getremotelogin 2>/dev/null | grep -q ': On'; then printf '  = Remote Login on\n'
  else
    run sudo systemsetup -setremotelogin on || {
      note "systemsetup was refused (newer macOS needs Full Disk Access for Terminal)."
      note "Turn it on by hand: System Settings > General > Sharing > Remote Login."; }
  fi
  local pw="PasswordAuthentication no
KbdInteractiveAuthentication no"
  if [ ! -s "$HOME/.ssh/authorized_keys" ]; then
    pw="# no key in ~/.ssh/authorized_keys yet: password logins left as they were"
    printf '  ! NO SSH KEY for %s: password logins stay ON, or a headless Mac could lock you out.\n' "$USER" >&2
    printf '  ! From your laptop: ssh-copy-id %s@%s.local   then re-run: %s/setup.sh %s\n' "$USER" "$HOST" "$HERE" "$LANE" >&2
    next "SSH: password logins are still ON. Add your key (ssh-copy-id $USER@$HOST.local), then: $HERE/setup.sh $LANE"
  fi
  put /etc/ssh/sshd_config.d/100-homeserver.conf 644 sudo <<EOF
$pw
PermitRootLogin no
EOF

  say "Firewall: on, stealth mode (no reply to probes)"
  local fw=/usr/libexec/ApplicationFirewall/socketfilterfw
  if "$fw" --getglobalstate 2>/dev/null | grep -qi enabled; then printf '  = firewall on\n'; else run sudo "$fw" --setglobalstate on; fi
  if "$fw" --getstealthmode 2>/dev/null | grep -qi 'enabled\|is on'; then printf '  = stealth mode on\n'; else run sudo "$fw" --setstealthmode on; fi

  say "Automatic security updates"
  local key
  for key in AutomaticCheckEnabled AutomaticDownload CriticalUpdateInstall ConfigDataInstall; do
    if [ "$(defaults read /Library/Preferences/com.apple.SoftwareUpdate "$key" 2>/dev/null)" = 1 ]; then printf '  = %s\n' "$key"
    else run sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate "$key" -bool true; fi
  done

  say "Tailscale (Tailscale's signed standalone app: starts at login, like the apps below)"
  # Not Homebrew's tailscaled as a root LaunchDaemon: root would run it from Homebrew's folder, which your account
  # can change. The app's only root part is its system extension, which macOS checks against Tailscale's signature.
  local old=/Library/LaunchDaemons/homebrew.mxcl.tailscale.plist keep_old=0 why=
  if [ -f "$old" ]; then   # retired while you reach this Mac over it, it cuts you, and the run, off half way
    if on_tailnet; then why="this session comes in over Tailscale"; else why=$(unsure_why) || why=; fi
  fi
  if [ -n "$why" ]; then
    keep_old=1
    printf '  ! Homebrew'"'"'s tailscaled still runs as root from Homebrew'"'"'s folder: whoever gets into your account can get root through it.\n' >&2
    printf '  ! It is left running: %s, so stopping it could cut you off.\n' "$why" >&2
    printf '  ! Re-run from a NEW ssh session from another computer on the home network (ssh %s@%s.local) or a terminal at the Mac, outside tmux and screen.\n' "$USER" "$HOST" >&2
    next "Tailscale: Homebrew's tailscaled still runs as root ($why). From a new ssh session from another computer on the home network (ssh $USER@$HOST.local) or a terminal at the Mac's screen, outside tmux and screen, re-run: $HERE/setup.sh $LANE"
  elif [ -f "$old" ]; then   # left by an earlier run of this script (sudo brew services start tailscale)
    run sudo launchctl bootout system/homebrew.mxcl.tailscale 2>/dev/null || true
    backup "$old" sudo
    run sudo rm -f "$old"
    next "Tailscale: Homebrew's tailscaled no longer runs as root. Remove it (brew uninstall tailscale) and restart the Mac before you first open the Tailscale app: the two must not run at once."
  fi
  if [ -d /Applications/Tailscale.app ]; then printf '  = Tailscale app\n'; else brew_cask tailscale-app; fi
  if [ "$keep_old" = 1 ]; then :   # opening the app comes after the re-run that retires Homebrew's tailscaled
  elif [ "$DRY" = 0 ] && "$TS" status >/dev/null 2>&1; then printf '  = logged in to the tailnet\n'
  else
    next "Tailscale: at the Mac's screen (or Screen Sharing), open Tailscale from Applications, allow its system extension and VPN configuration when macOS asks, log in and approve the Mac. Check that Tailscale is in System Settings > General > Login Items."
    next "Tailscale admin console: disable key expiry for $HOST (or tag it). servers/tailscale.md."
  fi

  say "Alerts (ntfy / Telegram, optional)"
  put /usr/local/bin/alert 755 sudo < "$KIT/tools/alert"
  seed /etc/homeserver/alert.conf 600 sudo <<'EOF'
# alert settings (used by /usr/local/bin/alert). Fill in one, then test: alert "hello"
#NTFY_URL=https://ntfy.sh/pick-a-long-random-topic-name
#TELEGRAM_TOKEN=
#TELEGRAM_CHAT=
EOF
  # Readable by you as well: wake-watch runs as you and alerts through it. An ACL entry for you alone, not the group
  # (staff, which every account on a Mac is in); changing it stays root's.
  if [ "$DRY" = 0 ] && ls -le /etc/homeserver/alert.conf 2>/dev/null | grep -q " user:$ME allow read$"; then
    printf '  = /etc/homeserver/alert.conf readable by %s\n' "$ME"
  else run sudo chmod +a "user:$ME allow read" /etc/homeserver/alert.conf; fi
  next "Alerts: sudo nano /etc/homeserver/alert.conf (ntfy topic), then: alert 'test from $HOST'"
  next "Heat: macOS throttles by itself; check now and then with: pmset -g therm   (and keep the vents clear)"
  next "Battery: if the Mac stays plugged in 24/7, limit charging (System Settings > Battery > Charging on Apple silicon, or the AlDente app) and look for swelling monthly."
}

# colima = a small Linux VM that runs Docker on the Mac
do_docker() {
  say "Docker via colima"
  brew_formula colima docker docker-compose
  local cfg="$HOME/.docker/config.json"
  if [ -f "$cfg" ]; then printf '  = %s (exists, kept)\n' "$cfg"
  else put "$cfg" 644 <<EOF
{ "cliPluginsExtraDirs": ["$PREFIX/lib/docker/cli-plugins"] }
EOF
  fi
  if [ "$DRY" = 0 ] && ! docker compose version >/dev/null 2>&1; then
    echo "setup.sh: 'docker compose version' failed. Check that $PREFIX/bin is on your PATH and that  \"cliPluginsExtraDirs\": [\"$PREFIX/lib/docker/cli-plugins\"]  is in $cfg, then re-run." >&2
    exit 1
  fi
  if "$BREW" services list 2>/dev/null | awk '$1 == "colima" {print $2}' | grep -q started; then printf '  = colima running\n'
  else run "$BREW" services start colima; fi
}

# stack LANE : copy compose files to ~/homeserver/LANE; heimdall gets ports instead of host networking
copy_stack() {
  local lane=$1 f
  say "Compose files -> $STACKS/$lane"
  for f in "$KIT/compose/$lane"/* "$KIT/compose/$lane"/env.example; do
    [ -f "$f" ] || continue
    if [ "$lane" = heimdall ] && [ "${f##*/}" = compose.yaml ]; then
      sed -e '/network_mode: host/d' -e 's/^    # ports: /    ports: /' "$f" | put "$STACKS/$lane/compose.yaml" 644
    else
      put "$STACKS/$lane/${f##*/}" 644 < "$f"
    fi
  done
  local tz; tz=$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||'); tz=${tz:-Etc/UTC}
  sed -e "s|^TZ=.*|TZ=$tz|" -e "s|^PUID=.*|PUID=$(id -u)|" -e "s|^PGID=.*|PGID=$(id -g)|" \
      -e "s|^NTFY_BASE_URL=.*|NTFY_BASE_URL=http://$HOST:8090|" -e "s|^DATA_DIR=.*|DATA_DIR=$HOME/Media|" \
      "$KIT/compose/$lane/env.example" | seed "$STACKS/$lane/.env" 600
}

# ---------------------------------------------------------------- lanes
do_media() {
  say "Jellyfin (native app; uses the Mac's hardware video encoder)"
  brew_cask jellyfin
  next "Open Jellyfin once (Applications), finish the setup page at http://localhost:8096, and add it to Login Items."
  next "Jellyfin: Dashboard > Playback > Transcoding > Apple VideoToolbox."
  do_docker
  copy_stack media
  next "Download stack (optional, read profiles/loki/README.md first): edit $STACKS/media/.env, then: cd $STACKS/media && docker compose --profile arr up -d"
  next "Tag this Mac tag:quarantine in the Tailscale admin console if it runs the download stack (servers/tailscale.md)."
}

do_heimdall() {
  do_docker
  copy_stack heimdall
  run sh -c "cd '$STACKS/heimdall' && docker compose up -d"
  next "Share the web UIs with the tailnet: $TS serve --bg --http=3001 http://127.0.0.1:3001  (uptime-kuma), and the same with 8090 for ntfy."

  say "wake-watch every 2 minutes (launchd, no login needed; runs as $ME, not root)"
  # As you, like on Linux: a 'wake-watch hold NAME' arriving over SSH updates the same state, and nothing root runs
  # comes from Homebrew's folder. macOS's own python3; -I: no imports from the script's folder or PYTHON* variables.
  if ! xcode-select -p >/dev/null 2>&1; then
    echo "setup.sh: wake-watch runs with /usr/bin/python3, which needs Apple's Command Line Tools: xcode-select --install, then re-run" >&2
    [ "$DRY" = 1 ] || exit 1
  fi
  put /usr/local/bin/wake-watch 755 sudo < "$KIT/tools/wake-watch"
  seed /etc/wake-watch.conf 644 sudo <<'EOF'
# wake-watch hosts: name  address-to-probe  wired-MAC
#thor  thor  XX:XX:XX:XX:XX:XX
#BROADCAST=255.255.255.255
EOF
  local d   # its state (wake-watch's default folder) and its log: yours, in folders only root can change
  for d in /var/lib/wake-watch /var/log/wake-watch; do
    if [ "$(stat -f %Su "$d" 2>/dev/null)" = "$ME" ]; then printf '  = %s (yours)\n' "$d"
    else run sudo install -d -o "$ME" -m 755 "$d"; fi
  done
  if [ -d /usr/local/var/wake-watch ]; then   # where an earlier run kept the state: a pause there stays a pause
    [ ! -e /usr/local/var/wake-watch/HALT ] || [ -e /var/lib/wake-watch/HALT ] || run touch /var/lib/wake-watch/HALT
    next "wake-watch keeps its state in /var/lib/wake-watch now; remove the old folder: sudo rm -r /usr/local/var/wake-watch"
  fi
  daemon org.homeserver.wake-watch <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>org.homeserver.wake-watch</string>
  <key>UserName</key><string>$ME</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>-I</string><string>/usr/local/bin/wake-watch</string><string>check</string></array>
  <key>StartInterval</key><integer>120</integer>
  <key>StandardOutPath</key><string>/var/log/wake-watch/wake-watch.log</string>
  <key>StandardErrorPath</key><string>/var/log/wake-watch/wake-watch.log</string>
</dict>
</plist>
EOF
  next "wake-watch: add boxes to /etc/wake-watch.conf (sudo nano), then: /usr/bin/python3 /usr/local/bin/wake-watch status   (pause: touch /var/lib/wake-watch/HALT)"
}

do_workhorse() {
  say "Ollama (native: uses the Apple GPU)"
  brew_formula ollama
  local uid agent="$HOME/Library/LaunchAgents/org.homeserver.ollama.plist"
  uid=$(id -u)
  # Ollama listens on every interface (not 127.0.0.1 + tailscale serve: loopback-bound Ollama answers 403 to Host
  # "thor", profiles/thor/README.md), so the pf rule that keeps the home network out comes first (audit R4-1): loaded
  # now, before the agent that starts Ollama is written or started, and at every boot before that agent lets it listen
  # (OLLAMA_GATE). An agent an earlier run left starts Ollama without waiting: stopped and set aside first, so a failure
  # or an interruption below leaves nothing listening on every interface, now or after a restart.
  if [ -f "$agent" ] && ! grep -qF "$PF_READY" "$agent"; then
    printf '  ! %s starts Ollama on every interface without waiting for the pf rule: stopped and set aside\n' "$agent" >&2
    run launchctl bootout "gui/$uid/org.homeserver.ollama" 2>/dev/null || true
    backup "$agent"
    run rm -f "$agent"
  fi

  say "Ollama's port: this Mac and the tailnet only, on every interface (Wi-Fi, Ethernet, ...), before Ollama starts"
  put /etc/pf.anchors/homeserver 644 sudo <<'EOF'
# Written by setup.sh: only loopback and tailnet addresses may connect to Ollama.
pass in quick on lo0 proto tcp from any to any port 11434
pass in quick proto tcp from { 100.64.0.0/10, fd7a:115c:a1e0::/48 } to any port 11434
block in quick proto tcp from any to any port 11434
EOF
  # Loaded here, every run, before Ollama: what the boot job runs, but waited for. Before the boot job is (re)loaded,
  # whose own run starts at once: two loads of one anchor at the same time can fail one, which removes the record.
  printf '  + load the rule now and mark it loaded for this boot (sudo /bin/sh -c: what org.homeserver.pf runs at boot)\n'
  if [ "$DRY" = 0 ] && ! sudo /bin/sh -c "$PF_GUARD"; then
    run launchctl bootout "gui/$uid/org.homeserver.ollama" 2>/dev/null || true
    echo "setup.sh: the pf rule did not load (sudo pfctl -E -a com.apple/homeserver -f /etc/pf.anchors/homeserver shows why), so Ollama is not started on every interface: its agent is not written, or it is stopped and after a restart waits for the rule. Fix that, then re-run: $HERE/setup.sh $LANE" >&2
    exit 1
  fi
  daemon org.homeserver.pf <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>org.homeserver.pf</string>
  <key>ProgramArguments</key>
  <array><string>/bin/sh</string><string>-c</string><string>$(xml "$PF_GUARD")</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF

  say "Ollama on every interface, once the pf rule is loaded for this boot"
  put "$agent" 644 <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>org.homeserver.ollama</string>
  <key>ProgramArguments</key>
  <array><string>/bin/sh</string><string>-c</string><string>$(xml "$OLLAMA_GATE")</string><string>org.homeserver.ollama</string><string>$PREFIX/bin/ollama</string><string>serve</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OLLAMA_HOST</key><string>0.0.0.0:11434</string>
    <key>OLLAMA_KEEP_ALIVE</key><string>5m</string>
    <key>OLLAMA_NUM_PARALLEL</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/ollama.log</string>
</dict>
</plist>
EOF
  if [ "$CHANGED" = 1 ]; then
    run launchctl bootout "gui/$uid/org.homeserver.ollama" 2>/dev/null || true
    run launchctl bootstrap "gui/$uid" "$agent"
  elif [ "$DRY" = 0 ] && launchctl print "gui/$uid" >/dev/null 2>&1 &&
       ! launchctl print "gui/$uid/org.homeserver.ollama" >/dev/null 2>&1; then
    # stopped by a run whose pf rule failed. Without a login session (an ssh with nobody logged in at the screen) there
    # is no gui domain to start it in, and launchd loads it at the next login anyway.
    run launchctl bootstrap "gui/$uid" "$agent"
  fi
  next "Pull a model: ollama pull qwen3:8b. Other machines: OLLAMA_HOST=http://$HOST:11434 (tailnet only)."
  next "After a restart Ollama starts once org.homeserver.pf has loaded the pf rule; ~/Library/Logs/ollama.log says when it waits. If it keeps waiting, see why: sudo pfctl -E -a com.apple/homeserver -f /etc/pf.anchors/homeserver; then retry the boot job: sudo launchctl kickstart -k system/org.homeserver.pf"
}

do_base
case "$LANE" in
  loki) do_media ;;
  heimdall) do_heimdall ;;
  thor) do_workhorse ;;
esac
say "Done ($LANE). Next steps:"
[ "$DRY" = 0 ] || note "(dry run: nothing was changed)"
next "Turn on automatic login (System Settings > Users & Groups) so user apps start after a power cut."
# ${NEXT[@]+...}: macOS ships bash 3.2, where an empty array trips set -u
for n in ${NEXT[@]+"${NEXT[@]}"}; do note "- $n"; done
