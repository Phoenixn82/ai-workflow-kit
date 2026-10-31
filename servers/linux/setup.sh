#!/usr/bin/env bash
# setup.sh <base|loki|heimdall|thor> [--dry-run] [--only=STEP[,STEP]]    |    setup.sh --confirm-firewall
#
# Turns a Debian/Ubuntu machine into a headless home server lane: the server half of the loki, heimdall and
# thor profiles (profiles/<name>/README.md). Every lane includes "base". Old names still work: media = loki,
# workhorse = thor. Run it from the kit's root folder (your clone), as your normal user, with sudo:
#     sudo servers/linux/setup.sh heimdall
#     servers/linux/setup.sh heimdall --dry-run      preview: changes nothing, no root needed
#
# Safe to re-run: every step checks first and only changes what differs; a file it replaces is kept beside it
# as <file>.bak-<time>. Every action is printed ("+" = will do / did, "=" = already so). It never partitions,
# formats or mounts disks.
#
#   base      lid closed: keep running; sleep/suspend masked; SSH keys-only (+ port 2222 for machine
#             jobs), Tailscale, firewall, auto-updates, journald cap, Wi-Fi power-save off, Wake-on-LAN,
#             heat watch, alerts
#   loki      base + Docker + Jellyfin (+ optional download stack), ClamAV, one-way firewall, a read-only
#             account thor pulls backups through, nightly database snapshots
#   heimdall  base + Docker + uptime-kuma + ntfy (+ optional Pi-hole), wake-watch, backups to thor, Cockpit
#   thor      base, but sleep stays allowed: thor-sleep (installed off) suspends it when idle; Ollama
#             (tailnet only), backup receiving folder, pulls loki's backups
#
#   --only=STEP[,STEP]  run just these steps of the lane: power, firewall, backup (repairs and tests)
#
# New firewall rules start on trial: only the running firewall changes. Open a NEW ssh session to the box within
# 5 minutes of the run's end and run there (that it gets in is the test):
#     sudo servers/linux/setup.sh --confirm-firewall
# which makes them the rules file loaded at boot. Without that, or after a restart, the firewall from before is back.
set -euo pipefail

usage() { sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
LANE=""
DRY=0
ONLY=""
CONFIRM=0
for a in "$@"; do
  case "$a" in
    --confirm-firewall) CONFIRM=1 ;;
    base|heimdall) [ -z "$LANE" ] || usage; LANE=$a ;;
    loki|media) [ -z "$LANE" ] || usage; LANE=loki ;;
    thor|workhorse) [ -z "$LANE" ] || usage; LANE=thor ;;
    --dry-run|-n) DRY=1 ;;
    --only=*) ONLY=${a#--only=}; [ -n "$ONLY" ] || usage; for s in ${ONLY//,/ }; do
                case "$s" in power|firewall|backup) ;; *) echo "unknown step: $s (power, firewall, backup)" >&2; usage ;; esac
              done ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $a" >&2; usage ;;
  esac
done
if [ "$CONFIRM" = 1 ]; then [ -z "$LANE$ONLY" ] && [ "$DRY" = 0 ] || usage; else [ -n "$LANE" ] || usage; fi

HERE=$(cd "$(dirname "$0")" && pwd)
KIT=$(cd "$HERE/.." && pwd)        # servers/
REPO=$(cd "$KIT/.." && pwd)        # the kit's root (framework/ holds thor-sleep)
TOOLS=$KIT/tools
UNITS=$TOOLS/systemd
ETC=/etc/homeserver
OPT=/opt/homeserver
POWER_MASKS=$ETC/power-masks   # the sleep targets this script masked: the thor lane lifts exactly these
SLEEP_TARGETS="sleep.target suspend.target hibernate.target hybrid-sleep.target"
NEXT=()           # "next steps" printed at the end
RELOAD=0          # systemd daemon-reload needed
FW_SAVE=$ETC/fw-rollback                      # the firewall as it was, while new rules are on trial
FW_LOCKDIR=/run/homeserver-fw                 # root's, mode 700: no other account can open the lock inside (fw_lock)
FW_LOCK=$FW_LOCKDIR/lock                      # held by trial, re-arm, confirm and rollback (fw_lock); outside FW_SAVE
FW_OLD_LOCK=/run/homeserver-fw.lock           # the lock before audit R3-1, open to every account: retired (fw_old_lock)
FW_CONFIRM_SECS=${FW_CONFIRM_SECS:-300}       # how long after the run a trial waits for --confirm-firewall
[[ $FW_CONFIRM_SECS =~ ^[1-9][0-9]{0,5}$ ]] || { echo "setup.sh: FW_CONFIRM_SECS must be a number of seconds" >&2; exit 2; }

# Downloads that end up running as root: exact versions, checked against these sha256 sums before use (fetch).
# A changed upstream file stops the run; nothing is installed from it.
TAILSCALE_VERSION=1.102.3                                                               # same as install/tools.json
TAILSCALE_GPG_SHA256=3e03dacf222698c60b8e2f990b809ca1b3e104de127767864284e6c228f1fb39   # apt key 2596 A99E AAB3 3821 893C 0A79 458C A832 957F 5868
DOCKER_GPG_SHA256=1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570      # apt key 9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88
OLLAMA_VERSION=0.33.3                                                                   # github.com/ollama/ollama/releases; same as install/tools.json
OLLAMA_SHA256_amd64=c13cea8f3389db4145f8a6cb88d1747242a48639d7c13e3bda7c1ebdc6eebb2f
OLLAMA_SHA256_arm64=4425a112af999ae6572c1ce211fbabeaca7bab23ed5860972acdfc0cc2358420

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  {
  printf 'setup.sh: %s\n' "$*" >&2
  if [ "$DRY" = 0 ] && grep -qsF "$FW_LOCK" "$FW_SAVE/rollback" && [ "$CONFIRM" = 0 ]; then   # a trial of this version
    printf 'setup.sh: new firewall rules are on trial: from a NEW ssh session run  sudo %s --confirm-firewall  or the firewall from before comes back by itself\n' "$0" >&2
  fi
  exit 1
}
run()  { printf '  + %s\n' "$*"; if [ "$DRY" = 0 ]; then "$@"; fi; }
next() { NEXT+=("$*"); }

# backup FILE : the copy about to be replaced stays beside it as FILE.bak-<time>, never run and never loaded: no
# exec bits (systemd-sleep runs every executable in its folder), and under /etc/apt a trailing .bak too (APT reads
# every other file name in its .d folders; it skips *.bak)
backup() {
  local b; b="$1.bak-$(date +%Y%m%d-%H%M%S)"
  case "$1" in /etc/apt/*) b="$b.bak" ;; esac
  printf '  + back up %s -> %s\n' "$1" "$b"
  [ "$DRY" = 1 ] || { cp -p -- "$1" "$b" && chmod a-x -- "$b"; }
}
# put DEST MODE < content : write only if different (a changed file is backed up first). Sets CHANGED=1 when it
# (would) write.
CHANGED=0
put() {
  local dest=$1 mode=$2 tmp
  tmp=$(mktemp)
  cat > "$tmp"
  if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
    printf '  = %s (unchanged)\n' "$dest"; CHANGED=0
  else
    [ ! -f "$dest" ] || backup "$dest"
    printf '  + write %s (mode %s)\n' "$dest" "$mode"; CHANGED=1
    [ "$DRY" = 1 ] || install -D -m "$mode" "$tmp" "$dest"
  fi
  rm -f "$tmp"
}
putfile() { put "$2" "$3" < "$1"; }
# seed DEST MODE < content : write only if the file does not exist yet (configs you edit later)
seed() {
  local dest=$1 mode=$2
  if [ -e "$dest" ]; then printf '  = %s (exists, kept)\n' "$dest"; cat > /dev/null; CHANGED=0; return 0; fi
  put "$dest" "$mode"
}
unit() {  # install a unit file from tools/systemd
  putfile "$UNITS/$1" "/etc/systemd/system/$1" 644
  [ "$CHANGED" = 0 ] || RELOAD=1
}
reload_units() { if [ "$RELOAD" = 1 ]; then run systemctl daemon-reload; RELOAD=0; fi; }
enable() {  # enable --now, only if not already enabled+active
  if systemctl is-enabled --quiet "$1" 2>/dev/null && systemctl is-active --quiet "$1" 2>/dev/null; then
    printf '  = %s enabled and running\n' "$1"
  else
    run systemctl enable --now "$1"
  fi
}

APT_UPDATED=0
apt_install() {
  local p missing=()
  for p in "$@"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing+=("$p")
  done
  if [ "${#missing[@]}" = 0 ]; then printf '  = installed: %s\n' "$*"; return 0; fi
  if [ "$APT_UPDATED" = 0 ]; then run apt-get update -q; APT_UPDATED=1; fi
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}"
}
apt_has() { local c; c=$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/{print $2}'); [ -n "$c" ] && [ "$c" != "(none)" ]; }
# apt_repo : REPO (debian or ubuntu) and CODE (release codename), for a vendor's apt repository
apt_repo() {
  REPO=debian CODE=${VERSION_CODENAME:-}
  case " ${ID:-} ${ID_LIKE:-} " in *" ubuntu "*) REPO=ubuntu; CODE=${UBUNTU_CODENAME:-$CODE} ;; esac
  [ -n "$CODE" ] || die "cannot tell the release codename for the vendor's apt repository"
}

# fetch URL SHA256 DEST : download over https to DEST (mode 644) only if the file's sha256 is SHA256; otherwise
# stop, leaving nothing behind. Every download this script installs or runs goes through here.
fetch() {
  local url=$1 want=$2 dest=$3 tmp got
  printf '  + download %s (must have sha256 %s) -> %s\n' "$url" "$want" "$dest"
  [ "$DRY" = 0 ] || return 0
  tmp=$(mktemp "$dest.XXXXXX")
  if ! curl -fsSL --proto '=https' --retry 3 -o "$tmp" "$url"; then rm -f "$tmp"; die "download failed: $url"; fi
  got=$(sha256sum "$tmp" | cut -d' ' -f1)
  if [ "$got" != "$want" ]; then
    rm -f "$tmp"
    die "$url has sha256 $got, not the $want pinned in setup.sh: not installed. If upstream changed it on purpose, update the pin after checking the new file."
  fi
  chmod 644 "$tmp" && mv -f "$tmp" "$dest"
}

# ---------------------------------------------------------------- firewall trial helpers
# ssh_ports : the TCP ports sshd listens on, one per line (sshd -T; its config files when that cannot run)
ssh_ports() {
  local t
  if t=$(/usr/sbin/sshd -T 2>/dev/null) && [ -n "$t" ]; then
    printf '%s\n' "$t" | awk '$1 == "port" {print $2} $1 == "listenaddress" {n = split($2, a, ":"); print a[n]}'
  else
    cat /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null | awk 'tolower($1) == "port" {print $2}'
    echo 22
  fi | grep -xE '[0-9]{1,5}' | sort -nu
}
# norm_ip ADDR : ADDR as nft writes it (no [], %zone or ::ffff: prefix); prints nothing unless it is a plain
# IPv4 or IPv6 address, so nothing else ever reaches the rules file
norm_ip() {
  local a=${1#[} o
  a=${a%]}; a=${a%%%*}
  case "$a" in ::ffff:*.*) a=${a#::ffff:} ;; esac
  if [[ $a =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
    for o in "${BASH_REMATCH[@]:1}"; do [ "$((10#$o))" -le 255 ] || return 0; done
    printf '%s\n' "$a"
  elif [[ $a =~ ^[0-9a-fA-F:]{2,39}$ && $a == *:* ]]; then
    printf '%s\n' "$a"
  fi
  return 0
}
# ssh_conns PORT... : established TCP connections to those local ports, one per line:
#   PEER-ADDRESS PEER-PORT LOCAL-PORT PIDS   (PIDS: comma-separated holders of the socket, - when unknown)
ssh_conns() {
  local want=" $* " l p rest ip pids
  ss -Htnp state established 2>/dev/null | while read -r _ _ l p rest; do
    case "$want" in *" ${l##*:} "*) ;; *) continue ;; esac
    ip=$(norm_ip "${p%:*}"); [ -n "$ip" ] || continue
    pids=$(grep -o 'pid=[0-9]*' <<< "$rest" | cut -d= -f2 | paste -sd, - || true)
    printf '%s %s %s %s\n' "$ip" "${p##*:}" "${l##*:}" "${pids:--}"
  done
}
# my_conns PORT... : the SSH connection(s) this run came in over. sudo usually drops SSH_CONNECTION, so the main
# evidence is the process tree: the sshd process this run descends from holds the connection's socket.
my_conns() {
  local up=" " p=$$ ip pp lp pids x
  while [ -n "$p" ] && [ "$p" -gt 1 ] 2>/dev/null; do
    up="$up$p "
    p=$(awk '$1 == "PPid:" {print $2}' "/proc/$p/status" 2>/dev/null || true)
  done
  ssh_conns "$@" | while read -r ip pp lp pids; do
    for x in ${pids//,/ }; do case "$up" in *" $x "*) echo "$ip $pp $lp"; break ;; esac; done
  done
  if [ -n "${SSH_CONNECTION:-}" ]; then
    read -r ip pp _ lp _ <<< "$SSH_CONNECTION"
    ip=$(norm_ip "$ip")
    if [ -n "$ip" ] && [[ $pp =~ ^[0-9]+$ && $lp =~ ^[0-9]+$ ]]; then echo "$ip $pp $lp"; fi
  fi
}
# user_conns PORT... : the SSH connections held by $SUDO_USER's processes, as "ADDRESS PEER-PORT LOCAL-PORT" lines
user_conns() {
  local uid ip pp lp pids x
  uid=$(id -u "${SUDO_USER:-root}" 2>/dev/null || echo 0)
  [ "$uid" != 0 ] || return 0
  ssh_conns "$@" | while read -r ip pp lp pids; do
    for x in ${pids//,/ }; do
      if [ "$(awk '$1 == "Uid:" {print $2}' "/proc/$x/status" 2>/dev/null)" = "$uid" ]; then echo "$ip $pp $lp"; break; fi
    done
  done
}
# mgmt_sources PORT... : where the person running this is connected from, as "ADDRESS LOCAL-PORT" lines: this
# run's own connection, else (inside tmux or screen, where the process tree ends) the sshd connections of
# $SUDO_USER and the address `who -m` shows
mgmt_sources() {
  local mine ip lp x
  mine=$(my_conns "$@")
  if [ -n "$mine" ]; then awk '{print $1, $3}' <<< "$mine"; return 0; fi
  user_conns "$@" | awk '{print $1, $3}'
  ip=$(who -m 2>/dev/null | sed -n 's/.*(\(.*\))$/\1/p' | head -n 1 || true)
  ip=$(norm_ip "$ip")
  if [ -n "$ip" ]; then
    lp=$(ssh_conns "$@" | awk -v a="$ip" '$1 == a {print $3}' | sort -u)
    for x in ${lp:-$*}; do echo "$ip $x"; done
  fi
}
# fw_lock / fw_unlock : the trial (fw_trial), the end-of-run re-arm, confirm_firewall and the rollback script all
# hold $FW_LOCK while they look at or change the trial, and each re-checks it after taking the lock, so none acts on
# a state another has just changed (audit R2-1: a rollback past its check deleted the table a confirm had just loaded).
# The lock sits in $FW_LOCKDIR, a folder of root's with mode 700, made with umask 077 and checked before every use (the
# rollback script does the same): no other account can open it, so none can hold it and stall a rollback (audit R3-1;
# flock needs no write access, so a lock file in /run that anyone could read was enough).
fw_lock() {
  local um; um=$(umask)
  umask 077; mkdir -p -- "$FW_LOCKDIR" 2>/dev/null || true
  [ ! -L "$FW_LOCKDIR" ] && [ -d "$FW_LOCKDIR" ] && [ "$(stat -c '%u %a' -- "$FW_LOCKDIR")" = "0 700" ] \
    || die "$FW_LOCKDIR is not a folder of root's with mode 700, so another account might hold the firewall lock in it: sudo rm -rf $FW_LOCKDIR, then run this again"
  exec 9> "$FW_LOCK" && umask "$um" && flock 9 || die "cannot take the firewall lock $FW_LOCK"
  [ -d "$FW_OLD_LOCK" ] && [ ! -L "$FW_OLD_LOCK" ] || fw_old_lock
}
fw_unlock() { exec 9>&- 8>&-; }
# fw_old_lock : with $FW_LOCK held, retire $FW_OLD_LOCK, the lock of setup.sh before audit R3-1, so the two never run side
# by side. It is taken here too (until fw_unlock): an older setup.sh holding it finishes first. This run stops instead
# while the older version is still at work: the lock held longer than 30 s (any account could take it), a trial of
# that version waiting (its rollback knows only the old lock), or an older setup.sh waiting for the lock. Otherwise a
# folder takes the old path, so an older setup.sh or rollback started later cannot take that lock at all (until a
# restart).
fw_old_lock() {
  local f p
  # no trial waiting: a rollback started anyway has nothing to undo and can only be waiting for one of the two locks
  fw_pending || systemctl stop homeserver-fw-rollback.service 2>/dev/null || true
  if [ -e "$FW_OLD_LOCK" ] || [ -L "$FW_OLD_LOCK" ]; then
    exec 8< "$FW_OLD_LOCK" || die "cannot open $FW_OLD_LOCK"
    if ! flock -n 8; then
      note "waiting up to 30 s for $FW_OLD_LOCK (the lock of an older setup.sh)"
      flock -w 30 8 || die "$FW_OLD_LOCK, the lock of an older setup.sh that any account can hold, is still held after 30 s: let that run finish, or restart the machine (the lock lives in /run), then run this again"
    fi
  fi
  if fw_pending && ! grep -qsF "$FW_LOCK" "$FW_SAVE/rollback"; then
    die "new firewall rules from an older setup.sh are on trial, and their rollback uses $FW_OLD_LOCK: confirm them with that version or let them roll back by themselves (a restart brings the firewall from before back too), then run this again"
  fi
  # an older setup.sh that has it open now waits for it (or is about to): a root process other than this one; another
  # account's open copy (real uid, so not one passed on to a set-uid program) does not count
  if [ -e "$FW_OLD_LOCK" ]; then
    for f in /proc/[0-9]*/fd/*; do
      p=${f#/proc/}; p=${p%%/*}
      if [ "$p" != $$ ] && [ "$f" -ef "$FW_OLD_LOCK" ] && [ "$(awk '$1 == "Uid:" {print $2}' "/proc/$p/status" 2>/dev/null)" = 0 ]; then
        die "an older setup.sh is waiting for $FW_OLD_LOCK: let it finish, then run this again"
      fi
    done
  fi
  rm -f -- "$FW_OLD_LOCK" && mkdir -m 700 -- "$FW_OLD_LOCK" \
    || die "cannot put a folder in place of $FW_OLD_LOCK (an older setup.sh may have just taken it): run this again"
}
# fw_pending : new firewall rules are on trial (saved state + a running rollback timer)
fw_pending() { [ -d "$FW_SAVE" ] && systemctl is-active --quiet homeserver-fw-rollback.timer 2>/dev/null; }
# fw_arm SECONDS : (re)start the rollback clock. Called with $FW_LOCK held, so a rollback the old timer already
# started can only be waiting for that lock: stopping it there undoes nothing.
fw_arm() {
  printf '  + firewall rollback in %s s unless confirmed (homeserver-fw-rollback.timer)\n' "$1"
  [ "$DRY" = 0 ] || return 0
  systemctl stop homeserver-fw-rollback.timer 2>/dev/null || true
  systemctl stop homeserver-fw-rollback.service 2>/dev/null || true
  systemctl reset-failed homeserver-fw-rollback.timer homeserver-fw-rollback.service 2>/dev/null || true
  systemd-run --quiet --unit=homeserver-fw-rollback --on-active="$1" --timer-property=AccuracySec=1s \
    /bin/sh "$FW_SAVE/rollback"
}
# fw_trial RULES PORT... : load RULES on trial. Only the running firewall changes: the rules file and the boot
# setting stay as they are until confirm_firewall, so a restart brings the old firewall back too. Saved first: the
# loaded table as it is now (kept from an earlier trial still waiting: that is the last confirmed one) and the SSH
# connections open now (none of them can confirm). The rollback clock runs before anything loads.
fw_trial() {
  local rules=$1; shift
  printf '  + load the new rules on trial: the running firewall only (rules file and boot setting change when you confirm)\n'
  printf '    confirm within %s s of the end of this run (setup.sh --confirm-firewall, from a NEW ssh session), or the old one is back\n' "$FW_CONFIRM_SECS"
  [ "$DRY" = 0 ] || return 0
  fw_lock
  if ! fw_pending; then
    rm -rf "$FW_SAVE" "$FW_SAVE.new"
    install -d -m 700 "$FW_SAVE.new"
    { echo 'table inet homeserver'; echo 'delete table inet homeserver'; nft list table inet homeserver 2>/dev/null || true; } \
      > "$FW_SAVE.new/active.nft"
    : > "$FW_SAVE.new/connections"
    cat > "$FW_SAVE.new/rollback" <<'EOF'
#!/bin/sh
# Written by setup.sh; run by homeserver-fw-rollback.timer when new firewall rules were not confirmed in time
# (setup.sh --confirm-firewall cancels it). The trial changed only the running firewall: the table loaded before
# it comes back. The rules file and the boot setting were never changed.
# setup.sh's lock first: a confirm or re-run holding it finishes, then the check below sees the trial as it is now. It
# sits in a folder of root's with mode 700, checked here as setup.sh does, so no other account can hold it.
# Nothing saved any more (confirmed, or rolled back already) = leave nftables alone.
umask 077
mkdir -p /run/homeserver-fw 2>/dev/null
[ ! -L /run/homeserver-fw ] && [ -d /run/homeserver-fw ] && [ "$(stat -c '%u %a' /run/homeserver-fw)" = "0 700" ] || {
  echo "homeserver firewall: /run/homeserver-fw is not a folder of root's with mode 700, so the rollback did not run: remove it (rm -rf /run/homeserver-fw), then: systemctl start homeserver-fw-rollback.service" | wall 2>/dev/null; exit 1; }
exec 9> /run/homeserver-fw/lock
flock 9 || exit 1
d=/etc/homeserver/fw-rollback
[ -f "$d/active.nft" ] || exit 0
/usr/sbin/nft -f "$d/active.nft" || /usr/sbin/nft delete table inet homeserver
date '+%F %T' > /etc/homeserver/firewall.rolled-back
rm -rf "$d"
exec 9>&-
echo "homeserver firewall: the new rules were not confirmed in time, so the firewall from before is back. Re-run setup.sh to try again." | wall 2>/dev/null
EOF
    mv "$FW_SAVE.new" "$FW_SAVE"
  fi
  { cat "$FW_SAVE/connections"; ssh_conns "$@" | awk '{print $1, $2, $3}'; } | sort -u > "$FW_SAVE/connections.new"
  mv "$FW_SAVE/connections.new" "$FW_SAVE/connections"
  install -m 600 "$rules" "$FW_SAVE/firewall.nft"
  fw_arm 3600   # covers the rest of this run; the end of the run sets the real deadline
  run nft -f "$FW_SAVE/firewall.nft"
  rm -f "$ETC/firewall.rolled-back"
  fw_unlock
}
# confirm_firewall : keep the rules on trial: they become the rules file, loaded at boot. Only from a session opened
# after they loaded (that it got in through them is the test), or from the console.
confirm_firewall() {
  [ "$(id -u)" = 0 ] || die "needs root: sudo $0 --confirm-firewall"
  fw_lock   # a rollback already running finishes first; one that starts now waits, then finds nothing to undo
  if ! fw_pending; then
    rm -rf "$FW_SAVE"
    [ ! -f "$ETC/firewall.rolled-back" ] || die "too late: the rules on trial were not confirmed in time and were rolled back at $(cat "$ETC/firewall.rolled-back"); the firewall from before is loaded. Try again: sudo $0 <lane> --only=firewall"
    echo "setup.sh: no firewall change is waiting for confirmation"; return 0
  fi
  [ -f "$FW_SAVE/firewall.nft" ] || die "$FW_SAVE/firewall.nft is missing: re-run setup.sh <lane> --only=firewall"
  local mine ports again
  ports=$(ssh_ports | paste -sd' ' - || true)
  again="Open a NEW ssh session to this box and run it there (when ssh shares connections, ControlMaster, open it with: ssh -o ControlPath=none ...)."
  mine=$(my_conns $ports)
  if [ -n "$mine" ]; then
    if grep -qxF -f <(printf '%s\n' "$mine") "$FW_SAVE/connections"; then
      die "this SSH session was already open when the new rules loaded, so it does not show that a new one gets in. $again"
    fi
  else
    # Inside tmux or screen the process tree does not lead to the connection. Then one of $SUDO_USER's SSH connections
    # must be new; with none open (the console, Tailscale SSH) it confirms.
    mine=$(user_conns $ports)
    if [ -n "$mine" ] && [ -z "$(grep -vxF -f "$FW_SAVE/connections" <<< "$mine" || true)" ]; then
      die "cannot tell which SSH connection this is (tmux or screen?), and every SSH connection of ${SUDO_USER:-} was already open when the new rules loaded, so none of them shows that a new one gets in. $again"
    fi
  fi
  # The rules file and boot setting first, then the service loads that file in place of the table on trial in one nft
  # transaction: reload when it runs, start when it does not. Never restart: its stop deletes the table, and the box
  # stays open until the start, for good if the start fails (audit R3-4). Only once both worked is the rollback
  # cancelled; otherwise the rules file and boot setting go back to what they were, the rules on trial stay loaded and
  # the rollback stays armed.
  local prev=$FW_SAVE/confirm.prev was_on=0
  rm -f "$prev"
  [ ! -e "$ETC/firewall.nft" ] || cp -p "$ETC/firewall.nft" "$prev" || die "cannot keep a copy of $ETC/firewall.nft"
  if systemctl is-enabled --quiet homeserver-firewall.service 2>/dev/null; then was_on=1; fi
  if ! { put "$ETC/firewall.nft" 600 < "$FW_SAVE/firewall.nft"; cmp -s "$FW_SAVE/firewall.nft" "$ETC/firewall.nft"; } \
     || ! run systemctl enable homeserver-firewall.service || ! fw_load; then
    if [ -e "$prev" ]; then cp -p "$prev" "$ETC/firewall.nft" || warn "could not put $ETC/firewall.nft back: a copy is $prev"
    else rm -f "$ETC/firewall.nft"; fi
    [ "$was_on" = 1 ] || systemctl disable --quiet homeserver-firewall.service 2>/dev/null || true
    die "the rules on trial could not be made the rules loaded at boot (see above): the rules file and boot setting are as before, the rules on trial stay loaded, and unless a confirm succeeds the firewall from before comes back by itself. Fix the cause, then run --confirm-firewall again from a new session."
  fi
  rm -rf "$FW_SAVE"   # first: a rollback that still runs finds nothing to undo
  systemctl stop homeserver-fw-rollback.timer 2>/dev/null || true
  systemctl stop homeserver-fw-rollback.service 2>/dev/null || true   # a rollback waiting for the lock
  systemctl reset-failed homeserver-fw-rollback.timer homeserver-fw-rollback.service 2>/dev/null || true
  echo "setup.sh: firewall confirmed: these rules stay and load at boot"
}
# fw_load : homeserver-firewall.service loads the rules file without a moment of no table: ExecReload when it runs,
# ExecStart when it does not; either is one nft -f, whose file deletes and re-adds the table in one transaction
fw_load() {
  if systemctl is-active --quiet homeserver-firewall.service; then run systemctl reload homeserver-firewall.service
  else run systemctl start homeserver-firewall.service; fi
}

# ---------------------------------------------------------------- preflight
[ -r /etc/os-release ] || die "no /etc/os-release: this script is for Debian/Ubuntu"
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "*|*" ubuntu "*) ;;
  *) die "this script supports Debian and Ubuntu (found: ${ID:-unknown})" ;;
esac
if [ "$CONFIRM" = 1 ]; then confirm_firewall; exit 0; fi
if [ "$DRY" = 1 ]; then
  ADMIN=${SUDO_USER:-$(id -un)}
  echo "DRY RUN: nothing will be changed. Lane: $LANE"
else
  [ "$(id -u)" = 0 ] || die "needs root: sudo $0 $LANE   (or preview: $0 $LANE --dry-run)"
  ADMIN=${SUDO_USER:-}
  [ -n "$ADMIN" ] && [ "$ADMIN" != root ] || die "run it with sudo from your normal user account (not as root directly)"
fi
ADMIN_HOME=$(getent passwd "$ADMIN" | cut -d: -f6)
HOST=$(hostname -s 2>/dev/null || cat /etc/hostname)

# LAN facts, used only for firewall rules
LAN_DEV=$(ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}' || true)
# every default router, per address family, on any interface ("a, b"; empty if none)
gateways() { ip "$1" route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "via") print $(i + 1)}' | sort -u | paste -sd, - | sed 's/,/, /g'; }
GW=$(gateways -4 || true)
GW6=$(gateways -6 || true)
LAN=""
if [ -n "$LAN_DEV" ]; then
  cidr=$(ip -4 -o addr show dev "$LAN_DEV" 2>/dev/null | awk '{print $4; exit}' || true)
  if [[ "$cidr" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)/([0-9]+)$ ]]; then
    pfx=${BASH_REMATCH[5]}
    n=$(( (BASH_REMATCH[1] << 24 | BASH_REMATCH[2] << 16 | BASH_REMATCH[3] << 8 | BASH_REMATCH[4]) & ((0xFFFFFFFF << (32 - pfx)) & 0xFFFFFFFF) ))
    LAN="$((n >> 24 & 255)).$((n >> 16 & 255)).$((n >> 8 & 255)).$((n & 255))/$pfx"
  fi
fi
note "host $HOST, admin user $ADMIN, LAN ${LAN:-not detected} via ${LAN_DEV:-?}, router ${GW:-?}"

# ---------------------------------------------------------------- base
base_packages() {
  say "Packages"
  local pk=(openssh-server avahi-daemon libnss-mdns unattended-upgrades nftables curl ca-certificates
            acpid iw ethtool python3 rsync)
  if grep -q GenuineIntel /proc/cpuinfo && apt_has intel-microcode; then pk+=(intel-microcode); fi
  if grep -q AuthenticAMD /proc/cpuinfo && apt_has amd64-microcode; then pk+=(amd64-microcode); fi
  apt_install "${pk[@]}"
}

base_power() {
  # logind never sleeps on its own, on any lane: on thor the idle decision is thor-sleep's (it checks real work,
  # logind only sees logins), the others never sleep at all.
  say "Lid closed and idle: keep running"
  put /etc/systemd/logind.conf.d/10-server.conf 644 <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF
  [ "$CHANGED" = 0 ] || run systemctl kill -s HUP systemd-logind
  if [ "$LANE" = thor ]; then thor_power; else mask_sleep; fi

  say "Lid closed: backlight off (acpid)"
  putfile "$TOOLS/lid-backlight" /usr/local/sbin/lid-backlight 755
  put /etc/acpi/events/lid-backlight 644 <<'EOF'
event=button/lid.*
action=/usr/local/sbin/lid-backlight
EOF
  local acpi_changed=$CHANGED
  put /etc/systemd/system/acpid.service.d/lid-backlight.conf 644 <<'EOF'
[Service]
# also apply once at start, in case it booted with the lid already closed
ExecStartPost=-/usr/local/sbin/lid-backlight
EOF
  if [ "$CHANGED" = 1 ] || [ "$acpi_changed" = 1 ]; then RELOAD=1; reload_units; run systemctl restart acpid; fi
  enable acpid.service
  if [ "$DRY" = 0 ] && ! systemctl is-active --quiet acpid; then
    warn "acpid is not running: journalctl -u acpid -b   (the lid backlight trick needs it; nothing else does)"
  fi
}

masked() { [ "$(systemctl is-enabled "$1" 2>/dev/null || true)" = masked ]; }

mask_sleep() {
  say "Never sleep: sleep, suspend and hibernate masked"
  local t
  for t in $SLEEP_TARGETS; do
    if masked "$t"; then printf '  = %s masked\n' "$t"
    else
      run systemctl mask "$t"
      printf '  + note %s in %s\n' "$t" "$POWER_MASKS"
      if [ "$DRY" = 0 ]; then install -d -m 755 "$ETC"; grep -qsxF "$t" "$POWER_MASKS" || echo "$t" >> "$POWER_MASKS"; fi
    fi
  done
}

# thor sleeps when idle, so the masks base puts everywhere else must not be there. Only masks this script made
# are lifted: the ones noted in $POWER_MASKS, or, on a box set up before that file existed (it has the kit's
# $ETC/firewall.nft but no $POWER_MASKS), all four, because every earlier lane masked all four.
thor_power() {
  say "Sleep allowed: thor suspends itself when idle (thor-sleep); masks this kit made are lifted"
  local t legacy=0
  if [ ! -e "$POWER_MASKS" ] && [ -e "$ETC/firewall.nft" ]; then legacy=1; fi
  for t in $SLEEP_TARGETS; do
    if ! masked "$t"; then printf '  = %s not masked\n' "$t"
    elif [ "$legacy" = 1 ] || grep -qsxF "$t" "$POWER_MASKS"; then
      run systemctl unmask "$t"
      if [ "$DRY" = 0 ] && [ -f "$POWER_MASKS" ]; then
        grep -vxF "$t" "$POWER_MASKS" > "$POWER_MASKS.new" || true; mv "$POWER_MASKS.new" "$POWER_MASKS"
      fi
    else
      warn "$t is masked, not by this kit: left alone. thor-sleep cannot suspend until: sudo systemctl unmask $t"
      next "Power: $t was masked by someone else, so thor cannot suspend. If that is not on purpose: sudo systemctl unmask $t"
    fi
  done
  # the notes file, even empty, marks this box as set up by this version: a re-run (firewall.nft exists by then)
  # must not take a mask the first run left alone for one of the kit's
  if [ "$DRY" = 0 ] && [ ! -e "$POWER_MASKS" ]; then install -d -m 755 "$ETC"; : > "$POWER_MASKS"; fi
  install_thor_sleep
}

# thor-sleep and its hold, from framework/_tools/fleet-wake/thor. The exported files name the scrubbed user "you";
# the user here is the account whose ~/.ssh holds the hold key (you, the admin), filled in at install time.
install_thor_sleep() {
  say "thor-sleep: suspend after 60 idle minutes, telling heimdall first (installed, left off)"
  local src=$REPO/framework/_tools/fleet-wake f dest mode want tmp key=$ADMIN_HOME/.ssh/fleet-hold
  [ -f "$src/thor/thor-sleep" ] || die "missing $src/thor/thor-sleep: run this from a full clone of the kit"
  apt_install python3 openssh-client util-linux
  for f in thor/thor-sleep:/usr/local/bin/thor-sleep:755 thor/fleet-hold:/lib/systemd/system-sleep/fleet-hold:755 \
           thor/thor-sleep.service:/etc/systemd/system/thor-sleep.service:644 \
           thor/thor-sleep.timer:/etc/systemd/system/thor-sleep.timer:644 \
           fleet-wake-shutdown@.service:/etc/systemd/system/fleet-wake-shutdown@.service:644; do
    IFS=: read -r f dest mode <<< "$f"
    tmp=$(mktemp)
    sed -e "s/\"runuser\", \"-u\", \"you\"/\"runuser\", \"-u\", \"$ADMIN\"/" -e "s/runuser -u you /runuser -u $ADMIN /" \
        -e "s/^User=you\$/User=$ADMIN/" "$src/$f" > "$tmp"
    case "$f" in
      thor/thor-sleep) want="\"runuser\", \"-u\", \"$ADMIN\"" ;;
      thor/fleet-hold) want="runuser -u $ADMIN " ;;
      fleet-wake-shutdown@.service) want="User=$ADMIN" ;;
      *) want="" ;;
    esac
    if [ -n "$want" ] && ! grep -qF -- "$want" "$tmp"; then
      rm -f "$tmp"; die "could not fill in the user name in $src/$f (did its runuser/User line change?)"
    fi
    put "$dest" "$mode" < "$tmp"
    rm -f "$tmp"
    case "$dest" in *.service|*.timer) [ "$CHANGED" = 0 ] || RELOAD=1 ;; esac
  done
  reload_units
  if [ -f "$key" ]; then printf '  = hold key %s exists\n' "$key"
  else
    run runuser -u "$ADMIN" -- sh -c "umask 077; mkdir -p '$ADMIN_HOME/.ssh' && ssh-keygen -q -t ed25519 -N '' -C 'fleet-hold@$HOST' -f '$key'"
  fi
  local pub="(run without --dry-run to create the key)"
  [ -f "$key.pub" ] && pub=$(cat "$key.pub")
  if systemctl is-enabled --quiet thor-sleep.timer 2>/dev/null; then printf '  = thor-sleep.timer enabled\n'
  else
    next "thor-sleep (installed, off). 1) On heimdall, add ONE line to your ~/.ssh/authorized_keys:"
    next "   command=\"/usr/local/bin/fleet-wake hold thor\",restrict $pub"
    next "2) Here, in ~/.ssh/config:  Host heimdall / HostName <heimdall's tailnet name or address> / Port 2222 / IdentityFile ~/.ssh/fleet-hold / IdentitiesOnly yes   then once: ssh heimdall true"
    next "3) Test the hold: sudo /lib/systemd/system-sleep/fleet-hold pre   then on heimdall: fleet-wake status  -> thor: hold"
    next "4) Turn it on: sudo systemctl enable --now thor-sleep.timer fleet-wake-shutdown@thor.service   (kill switch: sudo touch /etc/thor-sleep.disable)"
  fi
}

base_ssh() {
  say "SSH: keys only, root login off; port 2222 for machine-to-machine jobs"
  local pw="PasswordAuthentication no
KbdInteractiveAuthentication no"
  if [ ! -s "$ADMIN_HOME/.ssh/authorized_keys" ]; then
    pw="# no key in ~$ADMIN/.ssh/authorized_keys yet: password logins left as they were"
    warn "NO SSH KEY for $ADMIN: password logins stay ON, or a headless box could lock you out."
    warn "From your laptop: ssh-copy-id $ADMIN@$HOST.local   then re-run: sudo $HERE/setup.sh $LANE"
    next "SSH: password logins are still ON. Add your key (ssh-copy-id $ADMIN@$HOST.local), then: sudo $HERE/setup.sh $LANE"
  fi
  put /etc/ssh/sshd_config.d/10-homeserver.conf 644 <<EOF
# Port 22: you (from the LAN; on the tailnet, Tailscale SSH answers port 22 instead).
# Port 2222: machine jobs with single-command keys (backups, wake-watch hold). The firewall allows it
# only from the tailnet.
Port 22
Port 2222
$pw
PermitRootLogin no
EOF
  if [ "$CHANGED" = 1 ] && [ "$DRY" = 0 ]; then
    sshd -t || die "sshd config test failed; fix /etc/ssh/sshd_config.d/10-homeserver.conf"
    if systemctl list-unit-files ssh.socket >/dev/null 2>&1 && systemctl is-active --quiet ssh.socket; then
      run systemctl daemon-reload; run systemctl restart ssh.socket   # Ubuntu socket activation
    else
      run systemctl reload-or-restart ssh   # existing sessions survive either way
    fi
  fi
  # make sure sshd runs at boot (Ubuntu may use ssh.socket instead, which is fine)
  if ! systemctl is-enabled --quiet ssh.socket 2>/dev/null; then enable ssh.service; fi
}

base_logs_updates() {
  say "Logs capped at 500 MB; security updates install themselves"
  put /etc/systemd/journald.conf.d/10-size.conf 644 <<'EOF'
[Journal]
SystemMaxUse=500M
EOF
  [ "$CHANGED" = 0 ] || run systemctl restart systemd-journald
  put /etc/apt/apt.conf.d/20auto-upgrades 644 <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
}

base_network() {
  say "Wi-Fi power saving off (it makes a laptop server slow and flaky)"
  unit wifi-powersave-off@.service
  reload_units
  local w found=0
  for w in $(iw dev 2>/dev/null | awk '$1 == "Interface" {print $2}'); do
    found=1; enable "wifi-powersave-off@$w.service"
  done
  [ "$found" = 1 ] || note "no Wi-Fi interface"

  say "Wake-on-LAN on wired ports"
  unit wol@.service
  reload_units
  local d n found=0
  for d in /sys/class/net/*; do
    n=${d##*/}
    [ -e "$d/device" ] && [ ! -e "$d/wireless" ] && [ ! -e "$d/phy80211" ] || continue
    if ethtool "$n" 2>/dev/null | grep -q 'Supports Wake-on:.*g'; then
      found=1; enable "wol@$n.service"
      next "Wake-on-LAN: the MAC of $n is $(cat "$d/address") (for heimdall's /etc/wake-watch.conf). Also enable WoL in the BIOS."
    fi
  done
  [ "$found" = 1 ] || note "no wired port with Wake-on-LAN support (a USB Ethernet adapter usually cannot wake the machine)"
}

base_tailscale() {
  say "Tailscale"
  if command -v tailscale >/dev/null 2>&1; then
    printf '  = tailscale installed\n'
  else
    # Tailscale's own apt repository (what its install.sh would set up, without running that script): its signing
    # key is checked against the sha256 pinned above, then apt verifies the pinned version it installs from there.
    apt_repo
    apt_install ca-certificates curl
    run install -d -m 755 /usr/share/keyrings
    fetch "https://pkgs.tailscale.com/stable/$REPO/$CODE.noarmor.gpg" "$TAILSCALE_GPG_SHA256" /usr/share/keyrings/tailscale-archive-keyring.gpg
    put /etc/apt/sources.list.d/tailscale.list 644 <<EOF
deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/$REPO $CODE main
EOF
    APT_UPDATED=0
    apt_install "tailscale=$TAILSCALE_VERSION" tailscale-archive-keyring
  fi
  local flags=(--ssh "--operator=$ADMIN" "--hostname=$HOST")
  # The quarantined media box (loki) never starts connections to the tailnet, so it does not use MagicDNS
  # (its own DNS stays the router's). Everyone else gets MagicDNS names.
  [ "$LANE" != loki ] || flags+=(--accept-dns=false)
  if [ "$DRY" = 0 ] && tailscale status >/dev/null 2>&1; then
    printf '  = already logged in to the tailnet\n'
    run tailscale set "${flags[@]}"
  else
    # Run the login as a transient unit, not in this shell: if your SSH session drops before you approve,
    # the login keeps waiting instead of dying with it.
    if systemctl is-active --quiet ts-login.service 2>/dev/null; then run systemctl stop ts-login.service; fi
    run systemd-run --unit=ts-login --collect /usr/bin/tailscale up "${flags[@]}"
    next "Tailscale: open the login link shown by:  journalctl -u ts-login -n 20   then approve this machine."
    next "Tailscale admin console > Machines > $HOST: Disable key expiry (or tag it; tags also stop expiry). See servers/tailscale.md."
  fi
}

# media_egress CHAIN HOOK VERDICT : no new connections from loki, the media box (or its containers), to private,
# link-local or on-link addresses, IPv4 and IPv6. The router, DNS and the internet stay reachable.
media_egress() {
  # Not tied to one interface: whatever leaves by any other route than a default router (any LAN, Wi-Fi or
  # wired, its global IPv6 prefix included) is refused. No router for a family = nothing of it goes out.
  local gw4="" v4="    meta nfproto ipv4 counter $3 comment \"no IPv4 router seen: no IPv4 out\""
  local gw6="" v6="    meta nfproto ipv6 counter $3 comment \"no IPv6 router seen: no IPv6 out\""
  if [ -n "$GW" ]; then
    gw4="    ip daddr { $GW } accept comment \"router: DHCP, DNS, internet\""
    v4="    rt ip nexthop != { $GW } counter $3 comment \"anything else directly on a LAN\""
  fi
  if [ -n "$GW6" ]; then
    gw6="    ip6 daddr { $GW6 } accept comment \"IPv6 router\""
    v6="    rt ip6 nexthop != { $GW6 } counter $3 comment \"hosts on a LAN's own (global) IPv6 prefix\""
  fi
  cat <<EOF
  chain $1 {
    type filter hook $2; policy accept;
    ct state established,related accept
    oifname "lo" accept
    oifname "tailscale0" counter $3 comment "loki: no new connections to tailnet hosts"
    oifname { "docker0", "br-*" } accept comment "the box and its own containers"
    ip daddr { 224.0.0.0/4, 255.255.255.255 } accept comment "multicast/broadcast: DHCP, mDNS"
    ip6 daddr ff00::/8 accept comment "multicast: neighbour discovery, DHCPv6, mDNS"
    icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept comment "IPv6 neighbour discovery"
$gw4
$gw6
    meta l4proto { tcp, udp } th dport 53 accept comment "DNS (router or a house DNS server)"
    udp dport 41641 accept comment "Tailscale direct paths"
    ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16 } counter $3 comment "loki: no new connections to private IPv4"
    ip6 daddr { fc00::/7, fe80::/10 } counter $3 comment "loki: no new connections to IPv6 LAN (ULA, link-local)"
$v4
$v6
  }
EOF
}

firewall_rules() {
  local lan_ssh="" extra="" egress=""
  if [ -n "$LAN" ]; then
    lan_ssh="    ip saddr $LAN tcp dport $SSH_SET accept comment \"SSH from the home network\""
  fi
  case "$LANE" in
    heimdall)
      [ -z "$LAN" ] || extra="    ip saddr $LAN meta l4proto { tcp, udp } th dport 53 accept comment \"Pi-hole DNS for the house\""
      # the public front door for deployed projects (profiles/heimdall/deploy/caddy), once you set it up
      if [ -e "$ADMIN_HOME/apps/caddy/Caddyfile" ]; then
        extra="$extra
    tcp dport { 80, 443 } accept comment \"Caddy: public HTTPS for deployed projects\""
      fi ;;
    loki)
      [ -z "$LAN" ] || extra="    ip saddr $LAN tcp dport 8096 accept comment \"native Jellyfin for TVs (Docker ports bypass this table)\"
    ip saddr $LAN udp dport 7359 accept comment \"Jellyfin client discovery\""
      # the Samba share (profiles/loki/samba/media-share.conf), once you installed it: TCP 445 only
      if [ -n "$LAN" ] && [ -e /etc/samba/media-share.conf ]; then
        extra="$extra
    ip saddr $LAN tcp dport 445 accept comment \"Samba: read-only media share for the house\""
      fi
      egress="
  # One-way box: others may reach it, it may not start connections to them. Replies still flow.
$(media_egress output "output priority 0" reject)
$(media_egress forward "forward priority -5" drop)" ;;
  esac
  cat <<EOF
#!/usr/sbin/nft -f
# Written by setup.sh ($LANE lane). Loaded by homeserver-firewall.service.
# Only this table is replaced on reload; Docker's and Tailscale's own tables are left alone.
# Tailscale traffic arrives on tailscale0 and is accepted here; who may reach what on the tailnet
# is decided in the Tailscale access policy (tailscale.md), not by this file.
# Docker-published ports do NOT pass through this input chain: bind them to 127.0.0.1.
table inet homeserver
delete table inet homeserver
table inet homeserver {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    ct state invalid drop
    iif lo accept
    meta l4proto { icmp, ipv6-icmp } accept
    iifname "tailscale0" accept
    udp dport 41641 accept comment "Tailscale direct connections"
    udp dport 5353 accept comment "mDNS: $HOST.local"
    udp sport 67 udp dport 68 accept comment "DHCP client"
    udp sport 547 udp dport 546 accept comment "DHCPv6 client"
$lan_ssh
    ip6 saddr fe80::/10 tcp dport $SSH_SET accept comment "SSH via $HOST.local (often IPv6 link-local)"
$MGMT_RULES$extra
  }$egress
}
EOF
}

# fw_ssh_facts : SSH_PORTS (every port sshd listens on), SSH_SET (those the home network may use: all but 2222,
# the machine-job port, which stays tailnet-only) and MGMT_RULES (one accept line per address this run came in from
# that the other rules would not let back in: another subnet, the internet, a custom port; never 2222, and never a
# tailnet address, which tailscale0 lets in already)
fw_ssh_facts() {
  local p ip lp fam lan=()
  SSH_PORTS=$(ssh_ports | paste -sd' ' - || true)
  [ -n "$SSH_PORTS" ] || SSH_PORTS=22
  for p in $SSH_PORTS; do [ "$p" = 2222 ] || lan+=("$p"); done
  [ "${#lan[@]}" -gt 0 ] || lan=(22)
  if [ "${#lan[@]}" = 1 ]; then SSH_SET=${lan[0]}; else SSH_SET="{ $(printf '%s, ' "${lan[@]}" | sed 's/, $//') }"; fi
  MGMT_RULES=""
  while read -r ip lp; do
    [ -n "$ip" ] && [ "$lp" != 2222 ] || continue
    case " ${lan[*]} " in *" $lp "*) if in_lan "$ip"; then continue; fi ;; esac
    case "$ip" in 127.*|::1|[fF][dD]7[aA]:115[cC]:[aA]1[eE]0:*) continue ;; *:*) fam=ip6 ;; *) fam=ip ;; esac
    if [[ $ip =~ ^100\.([0-9]+)\. ]] && [ "${BASH_REMATCH[1]}" -ge 64 ] && [ "${BASH_REMATCH[1]}" -le 127 ]; then continue; fi
    MGMT_RULES="$MGMT_RULES    $fam saddr $ip tcp dport $lp accept comment \"where setup.sh was last run from over SSH\"
"
  done < <(mgmt_sources $SSH_PORTS | sort -u)
  note "sshd listens on: $SSH_PORTS (the home network may use: ${lan[*]})"
  if [ -n "$MGMT_RULES" ]; then note "this run came in over SSH from outside those rules: that address stays allowed"; fi
}
# in_lan ADDR : ADDR is in the detected IPv4 LAN, or IPv6 link-local
in_lan() {
  case "$1" in [fF][eE][89abAB]?:*) return 0 ;; *:*) return 1 ;; esac
  [ -n "$LAN" ] || return 1
  local a b c d pfx n m
  IFS=./ read -r a b c d pfx <<< "$LAN"; n=$(( a << 24 | b << 16 | c << 8 | d ))
  m=$(( (0xFFFFFFFF << (32 - pfx)) & 0xFFFFFFFF ))
  IFS=. read -r a b c d <<< "$1"
  [ $(( (a << 24 | b << 16 | c << 8 | d) & m )) = "$n" ]
}

base_firewall() {
  say "Firewall (nftables, default deny inbound)"
  [ -n "$LAN" ] || warn "LAN not detected: SSH will only be allowed over the tailnet"
  if [ -f "$ETC/firewall.rolled-back" ]; then
    warn "new firewall rules of $(cat "$ETC/firewall.rolled-back") were not confirmed in time and were undone; this run tries again"
  fi
  unit homeserver-firewall.service
  reload_units
  fw_ssh_facts
  local tmp; tmp=$(mktemp)
  firewall_rules > "$tmp"
  if [ "$DRY" = 0 ]; then nft -c -f "$tmp" || { rm -f "$tmp"; die "generated firewall did not pass nft -c"; }; fi
  # Always a trial, local or remote (sudo drops SSH_CONNECTION, so "remote" cannot be told reliably).
  if ! { [ -f "$ETC/firewall.nft" ] && cmp -s "$tmp" "$ETC/firewall.nft"; } \
     || ! systemctl is-active --quiet homeserver-firewall.service 2>/dev/null || fw_pending; then
    fw_trial "$tmp" $SSH_PORTS
  else
    printf '  = firewall loaded\n'
  fi
  rm -f "$tmp"
}

base_heat_alerts() {
  say "Alerts (ntfy / Telegram, optional)"
  putfile "$TOOLS/alert" /usr/local/bin/alert 755
  seed "$ETC/alert.conf" 600 <<'EOF'
# alert settings (used by /usr/local/bin/alert). Fill in one, then test: sudo alert "hello"
# Easiest: install the ntfy app on your phone, subscribe to a long random topic name, and put it here.
#NTFY_URL=https://ntfy.sh/pick-a-long-random-topic-name
# Self-hosted on heimdall (tailnet only, after tailscale serve on heimdall):  NTFY_URL=http://heimdall:8090/pick-a-topic
#TELEGRAM_TOKEN=
#TELEGRAM_CHAT=
EOF
  # readable by your group too: jobs that run as you (fleet-wake, the probes) alert through it
  local grp; grp=$(id -gn "$ADMIN")
  if [ "$(stat -c '%a %U %G' "$ETC/alert.conf" 2>/dev/null)" = "640 root $grp" ]; then printf '  = %s mode 640, group %s\n' "$ETC/alert.conf" "$grp"
  else run chown "root:$grp" "$ETC/alert.conf"; run chmod 640 "$ETC/alert.conf"; fi
  unit alert@.service

  say "Heat watch (host-heat, every minute)"
  putfile "$TOOLS/host-heat" /usr/local/bin/host-heat 755
  seed /etc/host-heat/env 644 <<'EOF'
# host-heat settings. CPU WARN/HOT = this many degrees below the CPU's own limit (usually 100 C).
# A machine that normally runs hot under load (big gaming laptop): 5,1 and 15.
HOST_HEAT_CPU_OFFSETS=10,5
HOST_HEAT_HOT_MIN=30
EOF
  if lsblk -dno TRAN 2>/dev/null | grep -qx sata && ! grep -qs drivetemp /sys/class/hwmon/hwmon*/name; then
    run modprobe drivetemp   # SATA SSD temperatures
    put /etc/modules-load.d/host-heat.conf 644 <<< "drivetemp"
  fi
  unit host-heat.service
  unit host-heat.timer
  reload_units
  enable host-heat.timer
  next "Alerts: edit $ETC/alert.conf (ntfy topic), then test: sudo alert \"test from $HOST\""
}

do_base() {
  base_packages
  base_power
  base_ssh
  base_logs_updates
  base_network
  base_heat_alerts
  base_tailscale
  base_firewall
  next "BIOS (once, at the keyboard): power on after AC loss = On; Wake-on-LAN = On; ErP/deep sleep = Off; battery charge limit 60-80% if offered, else 'fan always on'. See servers/common.md."
  next "Battery: kept at 100% around the clock it can swell. Look at it monthly; a bulging case or lifting touchpad = unplug and remove it."
}

# ---------------------------------------------------------------- docker (loki, heimdall)
install_docker() {
  say "Docker (official repository)"
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    printf '  = docker + compose installed\n'
  else
    apt_repo
    apt_install ca-certificates curl
    run install -m 0755 -d /etc/apt/keyrings
    fetch "https://download.docker.com/linux/$REPO/gpg" "$DOCKER_GPG_SHA256" /etc/apt/keyrings/docker.asc
    put /etc/apt/sources.list.d/docker.list 644 <<EOF
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$REPO $CODE stable
EOF
    APT_UPDATED=0
    apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  seed /etc/docker/daemon.json 644 <<'EOF'
{
  "log-driver": "local",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
  [ "$CHANGED" = 0 ] || run systemctl restart docker
  if id -nG "$ADMIN" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    printf '  = %s in the docker group\n' "$ADMIN"
  else
    run usermod -aG docker "$ADMIN"
    next "Log out and back in once so '$ADMIN' can run docker without sudo."
  fi
}

# copy_stack LANE : compose files to /opt/homeserver/LANE, owned by the admin user
copy_stack() {
  local lane=$1 f
  say "Compose files -> $OPT/$lane"
  [ -d "$OPT/$lane" ] || run install -d -m 755 -o "$ADMIN" -g "$(id -gn "$ADMIN")" "$OPT/$lane"
  for f in "$KIT/compose/$lane"/* "$KIT/compose/$lane"/env.example; do
    [ -f "$f" ] || continue
    case "$f" in *.sh) putfile "$f" "$OPT/$lane/${f##*/}" 755 ;; *) putfile "$f" "$OPT/$lane/${f##*/}" 644 ;; esac
  done
}
own() { run chown -R "$ADMIN:$(id -gn "$ADMIN")" "$@"; }
host_tz() { timedatectl show -p Timezone --value 2>/dev/null || echo Etc/UTC; }

# ---------------------------------------------------------------- loki (media)
do_loki() {
  install_docker
  copy_stack media
  local data=/srv/media env=$OPT/media/.env
  say "Media folders under $data (one filesystem, so hardlinks work)"
  local d
  for d in "$data/downloads/complete" "$data/downloads/incomplete" "$data/media/movies" "$data/media/tv" "$data/.quarantine"; do
    [ -d "$d" ] && printf '  = %s\n' "$d" || run install -d -m 775 -o "$ADMIN" -g "$(id -gn "$ADMIN")" "$d"
  done
  if findmnt -no OPTIONS --target "$data" 2>/dev/null | grep -qw noexec; then
    printf '  = %s is mounted noexec\n' "$data"
  else
    next "Media storage: put $data on its own partition or disk mounted with noexec,nosuid,nodev (nothing downloaded can run). Example fstab: UUID=... $data ext4 defaults,noexec,nosuid,nodev 0 2   (this script never touches disks)"
  fi

  say "Settings file $env"
  local tmp; tmp=$(mktemp)
  sed -e "s/^PUID=.*/PUID=$(id -u "$ADMIN")/" -e "s/^PGID=.*/PGID=$(id -g "$ADMIN")/" \
      -e "s|^TZ=.*|TZ=$(host_tz)|" -e "s|^DATA_DIR=.*|DATA_DIR=$data|" "$KIT/compose/media/env.example" > "$tmp"
  if [ -e /dev/dri/renderD128 ]; then
    local rg; rg=$(stat -c %g /dev/dri/renderD128)   # the group that owns the device ("render" on most systems)
    sed -i -e 's/^#COMPOSE_FILE=/COMPOSE_FILE=/' -e "s/^#RENDER_GID=.*/RENDER_GID=$rg/" "$tmp"
    note "iGPU found: hardware transcoding enabled (compose.hwaccel.yaml)"
  fi
  seed "$env" 600 < "$tmp"
  rm -f "$tmp"
  for d in jellyfin/config jellyfin/cache gluetun/wireguard transmission prowlarr radarr sonarr seerr; do
    [ -d "$OPT/media/$d" ] || run install -d -m 755 "$OPT/media/$d"
  done
  own "$OPT/media"

  say "Virus scanning of everything downloaded (ClamAV)"
  apt_install clamav clamav-daemon clamav-freshclam inotify-tools
  putfile "$TOOLS/media-scan-watch" /usr/local/bin/media-scan-watch 755
  unit media-scan-watch.service
  unit media-scan-sweep.service
  unit media-scan-sweep.timer
  unit media-port-sync.service
  unit media-port-sync.timer
  reload_units
  enable clamav-freshclam.service
  enable clamav-daemon.service
  enable media-scan-watch.service
  enable media-scan-sweep.timer
  next "ClamAV: the first signature download takes a few minutes. Then: sudo systemctl restart clamav-daemon media-scan-watch ; systemctl is-active clamav-daemon"
  enable media-port-sync.timer

  say "Start Jellyfin"
  run sh -c "cd '$OPT/media' && docker compose --profile core up -d"
  next "Jellyfin on the tailnet: sudo tailscale serve --bg --http=8096 http://127.0.0.1:8096   then open http://$HOST:8096 and create the admin account."
  next "Download stack (optional, read profiles/loki/README.md first): fill VPN + Transmission settings in $env, then: cd $OPT/media && docker compose --profile core --profile arr up -d"
  next "Tailscale admin console: tag this machine tag:quarantine (one-way: it can be reached, it cannot start connections). See servers/tailscale.md."
  loki_backup
}

# thor pulls loki's service data; loki never opens a connection (its firewall forbids it). The pull logs in as
# backup-pull, an account that can only read, and only /opt/homeserver/media: its one key is forced to
# `rrsync -ro` there, and root owns that key file, so the account cannot add keys to itself.
loki_backup() {
  local acct=backup-pull home=/var/lib/backup-pull d
  say "Read-only backup account '$acct' (thor pulls from it; this box connects to nothing)"
  apt_install acl rsync
  if id "$acct" >/dev/null 2>&1; then printf '  = user %s\n' "$acct"
  else
    run useradd --system --home-dir "$home" --create-home --shell /bin/sh --comment "thor pulls backups, read-only" "$acct"
    run usermod -p '*' "$acct"   # no password, but not locked: key logins work
  fi
  [ -d "$home/.ssh" ] && printf '  = %s/.ssh\n' "$home" || run install -d -m 755 -o root -g root "$home/.ssh"
  seed "$home/.ssh/authorized_keys" 644 <<'EOF'
# thor's backup-pull key, ONE line (on thor: sudo cat /etc/homeserver/backup-pull/id_ed25519.pub):
#command="rrsync -ro /opt/homeserver/media",restrict ssh-ed25519 AAAA... backup-pull@thor
EOF
  # read access without changing anyone's file modes: ACLs, inherited by new files and folders
  if getfacl -p "$OPT/media" 2>/dev/null | grep -qx "default:user:$acct:r-x"; then printf '  = %s can read %s (ACL)\n' "$acct" "$OPT/media"
  else
    run setfacl -R -m "u:$acct:rX" "$OPT/media"
    run find "$OPT/media" -type d -exec setfacl -m "d:u:$acct:rX" {} +
  fi

  say "Database snapshots for the backup (a copy of a live SQLite file can be torn)"
  putfile "$TOOLS/db-snapshot" /usr/local/bin/db-snapshot 755
  unit db-snapshot.service
  unit db-snapshot.timer
  put /etc/systemd/system/db-snapshot.service.d/10-user.conf 644 <<EOF
[Service]
# runs as you: everything under $OPT/media stays yours, so a restore can write it back without root
User=$ADMIN
EOF
  [ "$CHANGED" = 0 ] || RELOAD=1
  reload_units
  enable db-snapshot.timer
  if ! grep -qs '^command=' "$home/.ssh/authorized_keys"; then
    next "Backups: thor pulls them. On thor run setup.sh thor, then: sudo cat /etc/homeserver/backup-pull/id_ed25519.pub"
    next "   and add it here as ONE line of $home/.ssh/authorized_keys:  command=\"rrsync -ro $OPT/media\",restrict ssh-ed25519 AAAA..."
    next "   Tailscale policy: allow tag:server -> tag:quarantine tcp:2222 (servers/tailscale.md). Restore: profiles/loki/README.md."
  fi
}

# ---------------------------------------------------------------- heimdall
do_heimdall() {
  install_docker
  copy_stack heimdall
  local env=$OPT/heimdall/.env tmp
  tmp=$(mktemp)
  sed -e "s|^TZ=.*|TZ=$(host_tz)|" -e "s|^NTFY_BASE_URL=.*|NTFY_BASE_URL=http://$HOST:8090|" \
      "$KIT/compose/heimdall/env.example" > "$tmp"
  seed "$env" 600 < "$tmp"
  rm -f "$tmp"
  local d
  for d in uptime-kuma ntfy/cache pihole/etc-pihole; do
    [ -d "$OPT/heimdall/$d" ] || run install -d -m 755 "$OPT/heimdall/$d"
  done
  own "$OPT/heimdall"

  say "wake-watch (Wake-on-LAN recovery for your other boxes)"
  putfile "$TOOLS/wake-watch" /usr/local/bin/wake-watch 755
  seed /etc/wake-watch.conf 644 <<'EOF'
# wake-watch hosts: name  address-to-probe  wired-MAC   (MAC: on that box, ip -br link)
# The box must have Wake-on-LAN on in its BIOS, and be wired to the same LAN as heimdall.
# Never list a box here that fleet-wake also watches (thor, odin): only fleet-wake honours thor-sleep's holds.
#mybox  mybox  XX:XX:XX:XX:XX:XX
# Extra broadcast address(es), e.g. your LAN's .255 address:
#BROADCAST=255.255.255.255
EOF
  unit wake-watch.service
  unit wake-watch.timer
  put /etc/systemd/system/wake-watch.service.d/10-user.conf 644 <<EOF
[Service]
# runs as you, so a 'wake-watch hold NAME' arriving over SSH can update the same state
User=$ADMIN
EOF
  [ "$CHANGED" = 0 ] || RELOAD=1
  reload_units
  enable wake-watch.timer
  next "wake-watch: add your boxes to /etc/wake-watch.conf (not thor if fleet-wake watches it), then: wake-watch status   (profiles/heimdall/README.md)"
  say "heimdall-alert (where fleet-wake and freellmapi-probe send alerts)"
  seed /usr/local/bin/heimdall-alert 755 < "$TOOLS/heimdall-alert"
  heimdall_backup

  say "Cockpit (web admin, tailnet only: https://$HOST:9090)"
  if dpkg-query -W -f='${Status}' cockpit-ws 2>/dev/null | grep -q 'install ok installed'; then
    printf '  = cockpit installed\n'
  else
    if [ "$APT_UPDATED" = 0 ]; then run apt-get update -q; APT_UPDATED=1; fi
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends cockpit-ws cockpit-system
  fi

  say "Start uptime-kuma + ntfy"
  run sh -c "cd '$OPT/heimdall' && docker compose up -d"
  if ss -H -lun 'sport = :53' 2>/dev/null | grep -q '127.0.0.5[34]'; then
    next "Pi-hole needs port 53, which systemd-resolved holds. If you want Pi-hole: set DNSStubListener=no in /etc/systemd/resolved.conf, restart systemd-resolved (profiles/heimdall/README.md)."
  fi
  next "uptime-kuma and ntfy listen on 127.0.0.1 only. Share them with your tailnet: sudo tailscale serve --bg --http=3001 http://127.0.0.1:3001 && sudo tailscale serve --bg --http=8090 http://127.0.0.1:8090 ; then open http://$HOST:3001 (uptime-kuma: create the admin account, add monitors) from a tailnet device."
  next "Pi-hole (optional): the 'Pi-hole' section of profiles/heimdall/README.md, then: cd $OPT/heimdall && docker compose --profile pihole up -d"
}

# Nightly add-only copy of /opt/homeserver to thor (rrsync on thor refuses deletes and overwrites).
heimdall_backup() {
  say "Nightly backups to thor (backup-push)"
  putfile "$TOOLS/backup-push" /usr/local/bin/backup-push 755
  unit backup-push.service
  unit backup-push.timer
  reload_units
  if [ -f "$ETC/backup/id_ed25519" ]; then
    printf '  = backup key exists\n'
  else
    run install -d -m 700 "$ETC/backup"
    run ssh-keygen -q -t ed25519 -N '' -C "backup-push@$HOST" -f "$ETC/backup/id_ed25519"
  fi
  seed "$ETC/backup.conf" 600 <<'EOF'
# backup-push settings. When DEST is set and the key is accepted on the other side:
#   sudo systemctl enable --now backup-push.timer ; sudo systemctl start backup-push ; journalctl -u backup-push
#DEST=you@thor
PORT=2222
SOURCES="/opt/homeserver"
# thor sleeps when idle: with its wired MAC here, backup-push wakes it (Wake-on-LAN) and waits for it first
#WAKE_MAC=XX:XX:XX:XX:XX:XX
#WAKE_BROADCAST=255.255.255.255
EOF
  if grep -q '^DEST=' "$ETC/backup.conf" 2>/dev/null; then enable backup-push.timer
  else
    local pub="(run without --dry-run to create the key)"
    [ -f "$ETC/backup/id_ed25519.pub" ] && pub=$(cat "$ETC/backup/id_ed25519.pub")
    next "Backups: on thor, add this ONE line to your ~/.ssh/authorized_keys:"
    next "   command=\"rrsync -wo -no-del -no-overwrite /srv/backups/$HOST\",restrict $pub"
    next "   then here: set DEST (and WAKE_MAC, thor sleeps) in $ETC/backup.conf; sudo sh -c 'ssh-keyscan -p 2222 thor > $ETC/backup/known_hosts'; sudo systemctl enable --now backup-push.timer"
  fi
}

# ---------------------------------------------------------------- thor (workhorse)
do_thor() {
  thor_ollama
  if lspci 2>/dev/null | grep -qi 'vga.*nvidia\|3d.*nvidia'; then
    command -v nvidia-smi >/dev/null 2>&1 || next "NVIDIA GPU found but no driver: install it (Debian: apt install nvidia-driver from non-free; Ubuntu: ubuntu-drivers install), reboot, then: sudo systemctl restart ollama"
  else
    next "No NVIDIA GPU: thor-sleep reads the GPU with nvidia-smi, so here every check counts as busy and thor never sleeps (safe, but no power saving)."
  fi
  next "Pull a model: ollama pull qwen3:8b   (size it to your RAM/VRAM: profiles/thor/README.md). Other boxes: OLLAMA_HOST=http://$HOST:11434"
  thor_backup
  next "Optional: free-tier model router: run servers/tools/freellmapi-bootstrap.sh as $ADMIN (not root). profiles/thor/README.md."
  next "BIOS: 'Restore on AC power loss' = Power On, and Wake-on-LAN on: thor sleeps, and heimdall or odin wake it."
}

thor_ollama() {
  say "Ollama (local models; reachable from the tailnet, not the LAN)"
  if command -v ollama >/dev/null 2>&1; then
    printf '  = ollama installed\n'
  else
    install_ollama
  fi
  # 0.0.0.0 instead of the tailnet IP: a service bound to the tailnet IP that starts before Tailscale has its address
  # never listens, and thor-sleep and other local tools ask 127.0.0.1. What keeps it off the home network is a table of
  # its own (homeserver-ollama-guard.service: port 11434 from loopback and tailscale0 only), never the main firewall,
  # which goes on trial and rolls back to whatever was there before (nothing, on a new box) and is not loaded at all
  # after a restart during a trial (audit R3-2). Ollama does not run without that table (BindsTo), which loads first.
  # Two loaded models: one big model plus a small embedding model; Ollama still evicts when both do not fit.
  unit homeserver-ollama-guard.service
  reload_units
  enable homeserver-ollama-guard.service
  put /etc/systemd/system/ollama.service.d/10-homeserver.conf 644 <<'EOF'
[Unit]
# Ollama listens on every interface only while homeserver-ollama-guard.service keeps port 11434 to loopback and the tailnet
BindsTo=homeserver-ollama-guard.service
After=homeserver-ollama-guard.service

[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=5m"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
EOF
  if [ "$CHANGED" = 1 ]; then RELOAD=1; reload_units; run systemctl restart ollama; fi
}

# Ollama $OLLAMA_VERSION laid out the way its own install.sh does it (that script is not run): the release tarball,
# checked against its pinned sha256 before anything of it is unpacked, goes to /usr/local (bin/ollama,
# lib/ollama), and ollama.service runs it as the ollama system user (models in /usr/share/ollama/.ollama).
install_ollama() {
  local arch sum dir g
  arch=$(dpkg --print-architecture)
  case "$arch" in
    amd64) sum=$OLLAMA_SHA256_amd64 ;;
    arm64) sum=$OLLAMA_SHA256_arm64 ;;
    *) die "Ollama has no Linux build for $arch" ;;
  esac
  apt_install zstd pciutils   # .tar.zst archive; lspci finds the GPU below
  dir=$(mktemp -d /var/tmp/ollama.XXXXXX)   # 1.5 GB: not /tmp, which may be RAM
  fetch "https://github.com/ollama/ollama/releases/download/v$OLLAMA_VERSION/ollama-linux-$arch.tar.zst" "$sum" "$dir/ollama.tar.zst"
  if [ -e /usr/local/lib/ollama ]; then run mv /usr/local/lib/ollama "/usr/local/lib/ollama.bak-$(date +%Y%m%d-%H%M%S)"; fi
  run tar -C /usr/local --no-same-owner --zstd -xf "$dir/ollama.tar.zst"
  rm -rf "$dir"
  if id ollama >/dev/null 2>&1; then printf '  = user ollama\n'
  else run useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama; fi
  for g in render video; do
    if getent group "$g" >/dev/null && ! id -nG ollama 2>/dev/null | tr ' ' '\n' | grep -qx "$g"; then run usermod -aG "$g" ollama; fi
  done
  if ! id -nG "$ADMIN" 2>/dev/null | tr ' ' '\n' | grep -qx ollama; then run usermod -aG ollama "$ADMIN"; fi
  put /etc/systemd/system/ollama.service 644 <<'EOF'
[Unit]
Description=Ollama Service
After=network-online.target

[Service]
ExecStart=/usr/local/bin/ollama serve
User=ollama
Group=ollama
Restart=always
RestartSec=3
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

[Install]
WantedBy=default.target
EOF
  [ "$CHANGED" = 0 ] || RELOAD=1
  reload_units
  enable ollama.service
  if lspci 2>/dev/null | grep -qiE '(vga|3d|display).*(amd|ati)'; then
    next "AMD GPU: Ollama's ROCm add-on is not installed here; see https://docs.ollama.com/linux (AMD GPU install), version $OLLAMA_VERSION."
  fi
}

# thor holds the backups: heimdall pushes into /srv/backups/<host> (add-only key), and thor pulls loki's,
# because loki may not open connections.
thor_backup() {
  say "Backup receiving folder (heimdall pushes here)"
  if [ -d /srv/backups ]; then printf '  = /srv/backups\n'
  else run install -d -m 750 -o "$ADMIN" -g "$(id -gn "$ADMIN")" /srv/backups; fi
  if ! /usr/bin/rrsync -help 2>/dev/null | grep -q -- -no-overwrite; then
    warn "this rrsync cannot refuse overwrites (-no-overwrite needs rsync 3.5+: Debian 13+). Backups to this box need it;"
    warn "senders pinned with -no-overwrite fail (and alert) until it is there. profiles/thor/README.md 'Receiving backups'."
  fi

  say "Pull loki's backups (backup-pull: read-only key, loki connects to nothing)"
  local kd=$ETC/backup-pull conf=$ETC/backup-pull.conf
  putfile "$TOOLS/backup-pull" /usr/local/bin/backup-pull 755
  unit backup-pull.service
  unit backup-pull.timer
  put /etc/systemd/system/backup-pull.service.d/10-user.conf 644 <<EOF
[Service]
# runs as you: /srv/backups and the pull key are yours
User=$ADMIN
EOF
  [ "$CHANGED" = 0 ] || RELOAD=1
  reload_units
  if [ -f "$kd/id_ed25519" ]; then printf '  = pull key exists\n'
  else
    run install -d -m 700 -o "$ADMIN" -g "$(id -gn "$ADMIN")" "$kd"
    run runuser -u "$ADMIN" -- ssh-keygen -q -t ed25519 -N '' -C "backup-pull@$HOST" -f "$kd/id_ed25519"
  fi
  seed "$conf" 644 <<'EOF'
# backup-pull settings (thor). One SOURCE_<name>=<account>@<host> line per box to pull; that box decides
# what can be read (its forced command). Then: sudo systemctl enable --now backup-pull.timer
#SOURCE_loki=backup-pull@loki
PORT=2222
EXCLUDES="jellyfin/cache"
# Every run is a new dated folder in /srv/backups/<name>/ (unchanged files hard-linked, so cheap);
# nothing is ever deleted for you: prune old folders yourself.
EOF
  if grep -q '^SOURCE_' "$conf" 2>/dev/null; then enable backup-pull.timer
  else
    local pub="(run without --dry-run to create the key)"
    [ -f "$kd/id_ed25519.pub" ] && pub=$(cat "$kd/id_ed25519.pub")
    next "loki backups: on loki, add ONE line to /var/lib/backup-pull/.ssh/authorized_keys (setup.sh loki made the account):"
    next "   command=\"rrsync -ro /opt/homeserver/media\",restrict $pub"
    next "   then here: SOURCE_loki=backup-pull@loki in $conf; sudo sh -c 'ssh-keyscan -p 2222 loki >> $kd/known_hosts'; sudo systemctl enable --now backup-pull.timer"
  fi
}

# ---------------------------------------------------------------- main
if [ -n "$ONLY" ]; then
  for step in ${ONLY//,/ }; do
    case "$step:$LANE" in
      power:*) base_power ;;
      firewall:*) base_firewall ;;
      backup:loki) loki_backup ;;
      backup:heimdall) heimdall_backup ;;
      backup:thor) thor_backup ;;
      backup:*) note "lane $LANE has no backup step" ;;
    esac
  done
else
  do_base
  case "$LANE" in
    loki) do_loki ;;
    heimdall) do_heimdall ;;
    thor) do_thor ;;
  esac
fi

# a firewall on trial: its deadline counts from now, when you can act on it, and it heads the list. Under the lock, so
# a rollback that comes due meanwhile waits, then is stopped by the re-arm; what the summary says is what holds.
[ "$DRY" = 1 ] || fw_lock
if [ "$DRY" = 0 ] && [ -f "$ETC/firewall.rolled-back" ]; then
  NEXT=("FIREWALL: new rules were rolled back at $(cat "$ETC/firewall.rolled-back") (not confirmed in time). Try again: sudo $HERE/setup.sh $LANE --only=firewall" "${NEXT[@]}")
fi
if [ "$DRY" = 0 ] && fw_pending; then
  say "Firewall on trial"
  fw_arm "$FW_CONFIRM_SECS"
  NEXT=("FIREWALL (on trial): by $(date -d "+$FW_CONFIRM_SECS sec" +%H:%M:%S), open a NEW ssh session to this box and run:  sudo $HERE/setup.sh --confirm-firewall   (that the session gets in is the test; it makes the rules permanent). Not confirmed in time, or after a restart, the firewall from before is back by itself." "${NEXT[@]}")
fi
[ "$DRY" = 1 ] || fw_unlock
say "Done ($LANE). Next steps:"
[ "$DRY" = 0 ] || note "(dry run: nothing was changed)"
for n in "${NEXT[@]}"; do note "- $n"; done
