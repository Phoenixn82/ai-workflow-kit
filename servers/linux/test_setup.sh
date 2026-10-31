#!/usr/bin/env bash
# test_setup.sh: container tests for the role-specific pieces of servers/linux/setup.sh. Needs Docker, and lets two
# throwaway containers have NET_ADMIN (they load the generated firewall into their own network namespace only) and,
# for section 8, one --privileged container that boots systemd (skipped with a note where that cannot boot).
# Builds two small Debian 13 images once (kit-setup-test:3, kit-setup-systemd:1), then 4-5 minutes. Run it from anywhere:
#     bash servers/linux/test_setup.sh
# Nothing outside the containers is touched; they and their two networks are removed at the end.
#   0. every lane's --dry-run plan runs to the end; heimdall's masks sleep, thor's does not and installs thor-sleep
#   1. power: a lane masks the sleep targets and notes them; thor lifts exactly those (a mask someone else made
#      stays, with a warning, also on a re-run), installs thor-sleep, fleet-hold and fleet-wake-shutdown@ with the
#      user filled in and the timer off; on a box from before the notes (firewall.nft, no notes) all four count as
#      the kit's; a file it replaces is backed up and never executable; a second run writes nothing
#   2. hold: thor's system-sleep hook reaches heimdall's fleet-wake over the one-command key (thor: hold); a held
#      thor that goes quiet is left alone, an unheld one is woken after three misses; heimdall-alert finds tg in
#      ~/.local/bin from a user unit's PATH
#   3. suspend path: the installed thor-sleep, after six idle checks, holds on heimdall first and then calls
#      systemctl suspend; with /etc/thor-sleep.disable it does neither
#   4. one-way firewall (loki's generated rules loaded): loki cannot open a connection to thor over the tailnet or
#      to a LAN host; thor reaches loki's 2222 over the tailnet, the LAN host reaches 22 but not 2222; with the
#      Samba share installed the rules add 445 from the LAN; with Caddy set up heimdall's add 80/443
#   5. loki backups, through that firewall: thor pulls via the read-only account (db snapshot in, cache out); the
#      key cannot write, run a command or read outside the folder; the next night hard-links unchanged files; a
#      second run the same day gets its own folder; a deleted file comes back through a restore streamed via a
#      third machine
#   6. the loki Samba share template passes testparm
#   7. heimdall's Pi-hole: an empty PIHOLE_PASSWORD leaves the password variable out (Pi-hole then makes a random
#      one; passed in empty it would turn the password off), a set one is passed through
#   8. firewall trial under real systemd, run through sudo (which drops SSH_CONNECTION) by an admin connected from
#      outside the LAN to a custom sshd port: the trial is armed anyway and changes only the running firewall; a
#      new connection from that admin gets in, a stranger outside does not, the LAN reaches the custom port; a
#      confirm from the old session is refused, also cut off from sshd as in tmux or screen; unconfirmed, the
#      firewall from before is back (loaded table; rules
#      file and boot setting untouched) and is what a reboot loads, also when there was none; a reboot during the
#      trial brings it back too; confirmed from a new session, the new rules become the rules file, after reboot too;
#      one lock serialises trial, re-arm, confirm and rollback (a test wrapper around nft pauses one call): a rollback
#      that holds it makes a confirm wait, which then reports the rollback; a rollback that starts while a confirm
#      holds it waits, then leaves nftables alone; a re-run while the trial expires ends as one consistent trial,
#      whichever of the two goes first. The lock (audit R3-1) is in a folder of root's, mode 700, also under umask 000;
#      another account cannot open it during any of those trials, nor take the old path; a symlinked or open lock
#      folder is refused. The old lock of an older setup.sh: its trial still waiting stops a new run and rolls back by
#      itself; a run waits while an older one holds it, then retires it (a folder); held 30 s by another account, or
#      open in an older run (another account's open copy does not count), the new run stops. A confirm (audit R3-4) reloads the firewall: paused just before
#      the load, the table on trial is still there, a stranger is still dropped and the rollback still armed; a load
#      that fails (injected), whether the service ran or not, leaves the rules on trial loaded, the rules file and boot
#      setting as before and the rollback armed; a confirm after that succeeds, or the rollback comes. Ollama (thor,
#      audit R3-2; a stand-in that listens where OLLAMA_HOST says) is open to the tailnet and closed to the LAN (also
#      when a LAN host routes the box's tailnet address over the LAN) and outside while on trial, after the rollback
#      to no firewall, after a run cut off mid-trial, after a restart before the confirm and once confirmed (also
#      after a restart); it does not run without its guard table; a re-run of its step changes nothing; on a box an
#      older setup.sh left open (0.0.0.0, no guard) the step closes it
#   9. downloads: no curl | sh left; a download whose sha256 is not the pinned one is refused before anything of it
#      is unpacked or run (Ollama path with a substituted tarball), and the right one is accepted
#  10. host-heat, both install orders (framework installer then setup.sh's heat step, and the reverse): the installed
#      command with the env left behind writes the CSV in the user's home only as that user; the symlinked dir,
#      ancestor and month file and the FIFO fixtures of test_host_heat.py --installed stay untouched; a real --check
#      with a timing --set (hot_min=0) is refused
set -euo pipefail
KIT=$(cd "$(dirname "$0")/../.." && pwd)
IMG=kit-setup-test:3
id=kt$$
lan=$id-lan tail=$id-tail
command -v docker >/dev/null || { echo "SKIP: no docker"; exit 0; }
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "  ok: $*"; }
cleanup() {
  docker rm -f "$id-loki" "$id-thor" "$id-heimdall" "$id-lanhost" "$id-sd" "$id-wan1" "$id-wan2" "$id-home1" \
    "$id-hhframework" "$id-hhserver" "$id-tpeer" >/dev/null 2>&1 || true
  docker network rm "$lan" "$tail" "$id-home" "$id-wan" "$id-tail8" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "== building $IMG (once)"
  docker build -q -t "$IMG" - >/dev/null <<'DOCKERFILE'
FROM debian:13
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      openssh-server openssh-client rsync acl nftables iproute2 python3 netcat-openbsd procps ca-certificates \
      samba-common-bin curl zstd pciutils >/dev/null && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd && ssh-keygen -A && useradd -m -s /bin/bash -u 1000 tester
# stand-in for systemctl (no systemd in a container): mask/unmask/is-enabled act on /etc/systemd/system the way
# the real one does; everything else is only logged
RUN printf '%s\n' '#!/bin/sh' 'echo "$*" >> /var/log/fake-systemctl.log' 'd=/etc/systemd/system' \
  'case "$1" in' \
  '  mask) shift; mkdir -p "$d"; for u; do ln -sf /dev/null "${d:?}/${u:?}"; done ;;' \
  '  unmask) shift; for u; do [ "$(readlink "${d:?}/${u:?}")" = /dev/null ] && rm -f "${d:?}/${u:?}"; done ;;' \
  '  is-enabled) q=; [ "$2" = --quiet ] && { q=1; shift; }; if [ "$(readlink "${d:?}/$2" 2>/dev/null)" = /dev/null ]; then [ -n "$q" ] || echo masked; exit 1; fi; [ -n "$q" ] || echo disabled; exit 1 ;;' \
  'esac' 'exit 0' > /usr/local/sbin/systemctl && chmod 755 /usr/local/sbin/systemctl \
 && printf '%s\n' '#!/bin/sh' 'echo "$*" >> /var/log/fake-systemd-run.log' > /usr/local/bin/systemd-run && chmod 755 /usr/local/bin/systemd-run
DOCKERFILE
fi

docker network create "$lan" >/dev/null
docker network create "$tail" >/dev/null
start() {  # start NAME NETWORK [extra docker args...]
  local n=$1 net=$2; shift 2
  docker run -d --name "$id-$n" --hostname "$n" --network "$net" -v "$KIT:/kit:ro" "$@" "$IMG" sleep infinity >/dev/null
}
start loki "$lan" --cap-add NET_ADMIN
docker network connect "$tail" "$id-loki"
start thor "$tail"
start heimdall "$tail" --cap-add NET_ADMIN
start lanhost "$lan"
ip_of() { docker inspect -f "{{(index .NetworkSettings.Networks \"$2\").IPAddress}}" "$id-$1"; }
LOKI_LAN=$(ip_of loki "$lan") LOKI_TAIL=$(ip_of loki "$tail") THOR=$(ip_of thor "$tail")
HEIMDALL=$(ip_of heimdall "$tail") LANHOST=$(ip_of lanhost "$lan")
GW=$(docker network inspect -f '{{(index .IPAM.Config 0).Gateway}}' "$lan")
x() { local n=$1; shift; docker exec "$id-$n" "$@"; }
xs() { local n=$1; shift; docker exec "$id-$n" sh -c "$*"; }
setup() { local n=$1; shift; docker exec -e SUDO_USER=tester "$id-$n" /kit/servers/linux/setup.sh "$@"; }
for n in loki thor heimdall lanhost; do xs "$n" '/usr/sbin/sshd -o Port=22 -o Port=2222'; done
# loki's tailnet side is the interface named tailscale0, as on a real box
dev=$(xs loki "ip -o -4 addr show | awk -v a=$LOKI_TAIL '\$4 ~ \"^\" a \"/\" {print \$2}'")
[ -n "$dev" ] || fail "cannot find loki's tailnet interface"
xs loki "ip link set $dev down && ip link set $dev name tailscale0 && ip link set tailscale0 up && ip route replace default via $GW"

echo "== 0. dry-run plans"
for lane in base loki heimdall thor media workhorse; do
  out=$(setup lanhost "$lane" --dry-run 2>&1) || fail "dry run of $lane: $out"
  echo "$out" | grep -q "Done (" || fail "dry run of $lane did not finish"
done
out=$(setup lanhost heimdall --dry-run 2>&1)
echo "$out" | grep -q 'systemctl mask sleep.target' || fail "heimdall plan does not mask sleep"
out=$(setup lanhost thor --dry-run 2>&1)
! echo "$out" | grep -q 'systemctl mask' || fail "thor plan masks a sleep target"
echo "$out" | grep -q '/usr/local/bin/thor-sleep' || fail "thor plan does not install thor-sleep"
out=$(setup lanhost workhorse --dry-run 2>&1)
echo "$out" | grep -q 'Done (thor)' || fail "the old name workhorse is not thor"
ok "every lane plans to the end; heimdall masks sleep, thor installs thor-sleep instead"

echo "== 1. power"
setup heimdall heimdall --only=power >/dev/null
for t in sleep suspend hibernate hybrid-sleep; do
  [ "$(x heimdall readlink /etc/systemd/system/$t.target)" = /dev/null ] || fail "heimdall: $t.target not masked"
done
[ "$(xs heimdall 'wc -l < /etc/homeserver/power-masks')" = 4 ] || fail "heimdall: masks not noted"
xs thor 'mkdir -p /etc/systemd/system && ln -s /dev/null /etc/systemd/system/hibernate.target'   # someone else's mask
setup thor base --only=power >/dev/null
xs thor 'grep -qx hibernate.target /etc/homeserver/power-masks' && fail "a mask the kit did not make was noted"
out=$(setup thor thor --only=power 2>&1)
for t in sleep suspend hybrid-sleep; do
  xs thor "[ ! -e /etc/systemd/system/$t.target ]" || fail "thor: $t.target still masked"
done
[ "$(x thor readlink /etc/systemd/system/hibernate.target)" = /dev/null ] || fail "thor lifted a mask it did not make"
echo "$out" | grep -q 'hibernate.target is masked, not by this kit' || fail "no warning about the foreign mask"
x thor grep -qF '"runuser", "-u", "tester", "--"' /usr/local/bin/thor-sleep || fail "thor-sleep: user not filled in"
x thor grep -qF 'runuser -u tester -- ssh' /lib/systemd/system-sleep/fleet-hold || fail "fleet-hold: user not filled in"
x thor grep -qx 'User=tester' /etc/systemd/system/fleet-wake-shutdown@.service || fail "fleet-wake-shutdown@: user not filled in"
xs thor '[ -x /lib/systemd/system-sleep/fleet-hold ] && [ -f /etc/systemd/system/thor-sleep.timer ] && [ -f /home/tester/.ssh/fleet-hold.pub ]' \
  || fail "thor-sleep pieces or the hold key missing"
xs thor 'grep -qE "^enable .*thor-sleep" /var/log/fake-systemctl.log' && fail "thor-sleep.timer was enabled; it must stay off"
x thor /usr/local/bin/thor-sleep --self-test >/dev/null || fail "installed thor-sleep fails its self-test"
out=$(setup thor thor --only=power 2>&1)
! echo "$out" | grep -q '+ write' || fail "second thor run wrote files: $(echo "$out" | grep '+ write')"
xs lanhost 'mkdir -p /etc/homeserver /etc/systemd/system && : > /etc/homeserver/firewall.nft && for t in sleep suspend hibernate hybrid-sleep; do ln -s /dev/null /etc/systemd/system/$t.target; done'
# an older hold hook stands where the kit's goes: it is backed up, and the backup can never run (systemd-sleep
# runs every executable in that folder)
xs lanhost 'mkdir -p /lib/systemd/system-sleep && printf "#!/bin/sh\nexit 0\n" > /lib/systemd/system-sleep/fleet-hold && chmod 755 /lib/systemd/system-sleep/fleet-hold'
setup lanhost thor --only=power >/dev/null
xs lanhost 'ls /etc/systemd/system/*.target 2>/dev/null' && fail "a box from before the notes kept the kit's masks"
bak=$(xs lanhost 'ls /lib/systemd/system-sleep/fleet-hold.bak-* 2>/dev/null') || fail "the replaced fleet-hold was not backed up"
xs lanhost "[ ! -x $bak ] && grep -q 'exit 0' $bak" || fail "the fleet-hold backup is executable or not the old file"
# the box is now one this version set up (firewall.nft exists): a re-run keeps a mask that is not the kit's
xs lanhost 'ln -s /dev/null /etc/systemd/system/hibernate.target'
out=$(setup lanhost thor --only=power 2>&1)
[ "$(x lanhost readlink /etc/systemd/system/hibernate.target)" = /dev/null ] || fail "a re-run took a foreign mask for the kit's"
echo "$out" | grep -q 'hibernate.target is masked, not by this kit' || fail "the re-run did not warn about the foreign mask"
ok "masks noted and lifted exactly; foreign mask kept with a warning, on re-runs too; thor-sleep installed for tester, timer off; a replaced file is backed up, never executable; rerun writes nothing"

echo "== 2. intentional-sleep hold"
x heimdall install -m 755 /kit/framework/_tools/fleet-wake/fleet-wake /usr/local/bin/fleet-wake
mac=$(printf '%02x:%02x:%02x:%02x:%02x:%02x' 2 0 0 0 0 1)   # a locally administered test address
xs heimdall "su tester -c 'mkdir -p ~/.config/ai-workflow-kit && printf \"THOR_HOST=$THOR\nTHOR_MAC=$mac\n\" > ~/.config/ai-workflow-kit/hosts.env'"
pub=$(x thor cat /home/tester/.ssh/fleet-hold.pub)
xs heimdall "su tester -c 'mkdir -p -m 700 ~/.ssh && echo \"command=\\\"/usr/local/bin/fleet-wake hold thor\\\",restrict $pub\" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'"
xs thor "su tester -c 'printf \"Host heimdall\n  HostName $HEIMDALL\n  Port 2222\n  IdentityFile ~/.ssh/fleet-hold\n  IdentitiesOnly yes\n\" > ~/.ssh/config && ssh-keyscan -p 2222 $HEIMDALL > ~/.ssh/known_hosts 2>/dev/null'"
fw() { xs heimdall "su tester -c 'fleet-wake $*'"; }
fw check >/dev/null; fw status | grep -qx 'thor: up' || fail "fleet-wake does not see thor up"
x thor /lib/systemd/system-sleep/fleet-hold pre
fw status | grep -qx 'thor: hold' || fail "the hold did not reach heimdall: $(fw status)"
xs thor 'pkill -x sshd'                                         # thor goes to sleep
out=$(fw check; fw check; fw check)
if echo "$out" | grep -q 'Wake-on-LAN' || ! fw status | grep -qx 'thor: hold'; then fail "a held thor was woken: $out"; fi
xs heimdall "su tester -c 'echo up > ~/.local/state/fleet-wake/thor'"   # the same outage, without a hold
out=$(fw check; fw check; fw check)
echo "$out" | grep -q 'thor: sent Wake-on-LAN packet' || fail "an unexpected outage was not woken: $out"
# fleet-wake alerts through heimdall-alert from a user unit, whose PATH lacks ~/.local/bin, where tg is linked
xs heimdall "su tester -c 'mkdir -p ~/.local/bin && printf \"#!/bin/sh\necho tg: \\\$*\n\" > ~/.local/bin/tg && chmod +x ~/.local/bin/tg && touch ~/telegram-bot.token'"
out=$(x heimdall runuser -u tester -- env -i HOME=/home/tester PATH=/usr/local/bin:/usr/bin:/bin sh /kit/servers/tools/heimdall-alert "thor down")
[ "$out" = "tg: fleet thor down" ] || fail "heimdall-alert did not use tg from ~/.local/bin: $out"
ok "hold reaches heimdall over the one-command key; held = left alone, unheld = one Wake-on-LAN; alerts reach tg fleet"

echo "== 3. thor-sleep suspend path"
xs thor '/usr/sbin/sshd -o Port=22 -o Port=2222'
xs heimdall "su tester -c 'echo up > ~/.local/state/fleet-wake/thor'"
out=$(docker exec -i "$id-thor" python3 - 2>&1 <<'PY'
import importlib.machinery, importlib.util, subprocess, tempfile, urllib.error
from pathlib import Path
loader = importlib.machinery.SourceFileLoader("thor_sleep", "/usr/local/bin/thor-sleep")
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("thor_sleep", loader)); loader.exec_module(m)
d = Path(tempfile.mkdtemp())
m.STATE, m.COUNT, m.GPU_LOW, m.DISABLE = d, d / "idle-count", d / "gpu-low-since", d / "disable"
real_run, calls, execs = subprocess.run, [], []
def run(cmd, **kw):
    if cmd[0] == "runuser":                       # the real hold, over ssh to the heimdall container
        calls.append(cmd); return real_run(cmd, **kw)
    out = {"who": "", "ps": "systemd\nbash\n", "systemctl": "inactive\n"}.get(cmd[0], "")
    if cmd[0] == "nvidia-smi":
        out = "0\n" if "--query-gpu=utilization.gpu" in cmd else ""
    return subprocess.CompletedProcess(cmd, 0, out, "")
def urlopen(url, timeout=None):
    raise urllib.error.URLError("refused")       # Ollama stopped: a reading, not a failure
def execvp(prog, args):
    execs.append(args); raise SystemExit(0)
subprocess.run, m.urllib.request.urlopen, m.os.getloadavg, m.os.execvp = run, urlopen, lambda: (0.0, 0.0, 0.0), execvp
m.time.sleep = lambda s: None
m.COUNT.write_text("5")
try:
    m.main([])
except SystemExit:
    pass
assert calls and calls[0][:4] == ["runuser", "-u", "tester", "--"] and calls[0][-2:] == ["heimdall", "true"], calls
assert execs == [["systemctl", "suspend"]], execs
m.DISABLE.touch(); m.COUNT.write_text("5"); calls.clear(); execs.clear()
m.main([])
assert not calls and not execs, (calls, execs)
print("suspend path ok")
PY
) || fail "suspend path: $out"
echo "$out" | grep -q '^suspend path ok$' || fail "suspend path did not run: $out"
fw status | grep -qx 'thor: hold' || fail "thor-sleep did not set the hold before suspending: $(fw status)"
ok "six idle checks: hold on heimdall first, then systemctl suspend; the disable file stops both"

echo "== 4. one-way firewall"
nc_ok() { x "$1" nc -z -w 3 "$2" "$3" >/dev/null 2>&1; }
# controls: without the rules every one of these connections works, so a refusal below is the firewall's doing
nc_ok loki "$THOR" 22 && nc_ok loki "$LANHOST" 22 && nc_ok lanhost "$LOKI_LAN" 2222 || fail "control: the test network is broken"
out=$(setup loki loki --only=firewall 2>&1) || fail "loki firewall step: $out"
# docker exec has no SSH_CONNECTION, like sudo: the trial is armed all the same, the old state saved first
x loki grep -q -- '--unit=homeserver-fw-rollback --on-active=300 ' /var/log/fake-systemd-run.log || fail "no rollback armed without SSH_CONNECTION"
xs loki '[ -f /etc/homeserver/fw-rollback/active.nft ] && [ -f /etc/homeserver/fw-rollback/rollback ]' || fail "the firewall before the change was not saved"
echo "$out" | grep -q 'setup.sh --confirm-firewall' || fail "the run does not say how to confirm"
x loki test ! -e /etc/homeserver/firewall.nft || fail "the trial wrote the rules file before it was confirmed"
setup loki --confirm-firewall >/dev/null   # docker exec = the console: no SSH session to be an old one
x loki test ! -e /etc/homeserver/fw-rollback || fail "the confirm left the trial behind"
x loki nft -f /etc/homeserver/firewall.nft
nc_ok loki "$THOR" 22 && fail "loki opened a connection to thor over the tailnet"
nc_ok loki "$LANHOST" 22 && fail "loki opened a connection to a LAN host"
nc_ok thor "$LOKI_TAIL" 2222 || fail "thor cannot reach loki's 2222 over the tailnet"
nc_ok lanhost "$LOKI_LAN" 22 || fail "the LAN cannot reach loki's ssh"
nc_ok lanhost "$LOKI_LAN" 2222 && fail "the LAN reached loki's 2222 (machine jobs are tailnet-only)"
x loki grep -q 'dport 445' /etc/homeserver/firewall.nft && fail "445 open without the Samba share"
xs loki 'mkdir -p /etc/samba && cp /kit/profiles/loki/samba/media-share.conf /etc/samba/media-share.conf'
setup loki loki --only=firewall >/dev/null && setup loki --confirm-firewall >/dev/null
x loki grep -q 'tcp dport 445 accept' /etc/homeserver/firewall.nft || fail "445 not opened with the Samba share installed"
xs loki 'ls /etc/homeserver/firewall.nft.bak-* >/dev/null 2>&1' || fail "the replaced firewall rules were not backed up"
x loki nft -f /etc/homeserver/firewall.nft
nc_ok loki "$THOR" 22 && fail "after the reload loki opened a connection to thor"
xs heimdall "su tester -c 'mkdir -p ~/apps/caddy && cp /kit/profiles/heimdall/deploy/caddy/Caddyfile ~/apps/caddy/'"
xs heimdall 'printf "Port 22\nPort 2222\nPort 2200\n" > /etc/ssh/sshd_config.d/custom.conf'   # sshd -T: 22, 2200, 2222
setup heimdall heimdall --only=firewall >/dev/null && setup heimdall --confirm-firewall >/dev/null
x heimdall grep -q 'tcp dport { 80, 443 } accept' /etc/homeserver/firewall.nft || fail "heimdall did not open 80/443 for Caddy"
x heimdall grep -q 'tcp dport { 22, 2200 } accept comment "SSH from the home network"' /etc/homeserver/firewall.nft \
  || fail "the LAN SSH rule does not follow sshd's ports (22, 2200; 2222 stays tailnet-only)"
# the address setup.sh is run from over SSH stays allowed, but never on 2222 and never a tailnet address
mg() { docker exec -e SUDO_USER=tester -e SSH_CONNECTION="$1" "$id-heimdall" /kit/servers/linux/setup.sh heimdall --only=firewall >/dev/null \
         && x heimdall cat /etc/homeserver/fw-rollback/firewall.nft; }
r=$(mg "203.0.113.9 5555 10.9.9.9 2200") && grep -q 'ip saddr 203.0.113.9 tcp dport 2200 accept' <<<"$r" || fail "the address setup.sh ran from was not kept"
r=$(mg "198.51.100.7 5555 10.9.9.9 2222") && ! grep -q '198.51.100.7' <<<"$r" || fail "a management rule opened 2222, which stays tailnet-only"
r=$(mg "100.100.100.100 5555 10.9.9.9 22") && ! grep -qF -e 100.100.100.100 -e 203.0.113.9 <<<"$r" || fail "a tailnet address got its own rule"
setup heimdall --confirm-firewall >/dev/null
ok "loki reaches nothing; thor and the LAN reach it as allowed; Samba and Caddy ports only once set up; trial armed without SSH_CONNECTION, rules file written only on confirm; SSH rule follows sshd's ports; the address setup.sh ran from is kept, never on 2222 or for the tailnet"

echo "== 5. loki backups pulled by thor"
xs loki "mkdir -p /opt/homeserver/media/jellyfin/config/data /opt/homeserver/media/jellyfin/cache && cd /opt/homeserver/media \
  && echo 'services: {}' > compose.yaml && echo settings-v1 > config.xml && (umask 077; echo VPN_KEY=test-only > .env) \
  && head -c 100000 /dev/zero > jellyfin/cache/big.bin \
  && python3 -c 'import sqlite3; c=sqlite3.connect(\"jellyfin/config/data/jellyfin.db\"); c.execute(\"create table t(x)\"); c.execute(\"insert into t values (7)\"); c.commit()' \
  && chown -R tester:tester /opt/homeserver"
setup loki loki --only=backup >/dev/null
x loki runuser -u tester -- /usr/local/bin/db-snapshot /opt/homeserver/media/jellyfin/config /opt/homeserver/media/db-snapshots >/dev/null
setup thor thor --only=backup >/dev/null
pub=$(x thor cat /etc/homeserver/backup-pull/id_ed25519.pub)
xs loki "echo 'command=\"rrsync -ro /opt/homeserver/media\",restrict $pub' >> /var/lib/backup-pull/.ssh/authorized_keys"
xs thor "echo 'SOURCE_loki=backup-pull@$LOKI_TAIL' >> /etc/homeserver/backup-pull.conf && ssh-keyscan -p 2222 $LOKI_TAIL >> /etc/homeserver/backup-pull/known_hosts 2>/dev/null"
pull() { x thor runuser -u tester -- env BACKUP_PULL_DAY="$1" /usr/local/bin/backup-pull; }
out=$(pull 2026-10-11 2>&1) || fail "first pull failed: $out"
b=/srv/backups/loki/2026-10-11
xs thor "[ -f $b/config.xml ] && [ -f $b/.env ] && ls $b/db-snapshots/data_jellyfin-*.db >/dev/null" || fail "the pull missed files"
xs thor "[ ! -e $b/jellyfin/cache/big.bin ]" || fail "the cache was pulled"
xs thor "python3 -c 'import sqlite3,glob; assert sqlite3.connect(glob.glob(\"$b/db-snapshots/*.db\")[0]).execute(\"select x from t\").fetchone() == (7,)'" \
  || fail "the database snapshot is not readable"
key="ssh -p 2222 -i /etc/homeserver/backup-pull/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=/etc/homeserver/backup-pull/known_hosts"
asthor() { x thor runuser -u tester -- sh -c "$*"; }
asthor "rsync -e '$key' /etc/hostname backup-pull@$LOKI_TAIL:./planted" >/dev/null 2>&1 && fail "the pull key could write"
xs loki '[ ! -e /opt/homeserver/media/planted ]' || fail "a file was planted on loki"
out=$(asthor "$key backup-pull@$LOKI_TAIL id" 2>&1 || true)
echo "$out" | grep -q 'uid=' && fail "the pull key ran a command: $out"
asthor "rsync -e '$key' backup-pull@$LOKI_TAIL:/etc/passwd /tmp/p1; rsync -e '$key' backup-pull@$LOKI_TAIL:../../../etc/passwd /tmp/p2" >/dev/null 2>&1 || true
xs thor '[ ! -e /tmp/p1 ] && [ ! -e /tmp/p2 ]' || fail "the pull key read outside /opt/homeserver/media"
xs loki 'echo settings-v2 > /opt/homeserver/media/config.xml'
pull 2026-10-12 >/dev/null 2>&1 || fail "second pull failed"
[ "$(x thor stat -c %i $b/compose.yaml)" = "$(x thor stat -c %i /srv/backups/loki/2026-10-12/compose.yaml)" ] || fail "an unchanged file was not hard-linked"
x thor grep -qx settings-v2 /srv/backups/loki/2026-10-12/config.xml || fail "the changed file was not pulled"
x thor grep -qx settings-v1 $b/config.xml || fail "the older copy changed"
pull 2026-10-12 >/dev/null 2>&1 || fail "third pull failed"
xs thor 'ls -d /srv/backups/loki/2026-10-12T* >/dev/null 2>&1' || fail "a second run the same day did not get its own folder"
x thor test -f $b/config.xml || fail "an old copy was deleted"
xs loki 'rm /opt/homeserver/media/config.xml'
x thor tar -C /srv/backups/loki/2026-10-12 -cf - . | docker exec -i -u tester "$id-loki" tar -C /opt/homeserver/media -xpf -
x loki grep -qx settings-v2 /opt/homeserver/media/config.xml || fail "the restore did not bring config.xml back"
ok "pull through the firewall; read-only key; hard-linked nights; nothing deleted; restore via a third machine"

echo "== 6. Samba template"
xs loki 'printf "[global]\n   include = /kit/profiles/loki/samba/media-share.conf\n" > /tmp/smb.conf'
out=$(x loki testparm -s /tmp/smb.conf 2>/dev/null) || fail "testparm rejects the share"
echo "$out" | grep -q '^\[media\]' && echo "$out" | grep -q 'valid users = @sambashare' || fail "testparm output: $out"
ok "the media share parses: [media], read-only, @sambashare"
echo "== 7. Pi-hole password"
pd=$(mktemp -d); hc=$KIT/servers/compose/heimdall/compose.yaml
pih() { env -u PIHOLE_PASSWORD "$@" docker compose -f "$hc" --project-directory "$pd" --profile pihole config pihole 2>&1; }
out=$(pih) || fail "compose config: $out"
! grep -q webserver_api_password <<<"$out" && grep -q PIHOLE_PASSWORD_EMPTY <<<"$out" || fail "unset: $out"
out=$(pih PIHOLE_PASSWORD=) && ! grep -q webserver_api_password <<<"$out" || fail "empty: $out"
marker=kit-test-value
out=$(pih PIHOLE_PASSWORD="$marker") && grep -q "FTLCONF_webserver_api_password: $marker" <<<"$out" || fail "set: $out"
rmdir "$pd"
ok "an empty or unset PIHOLE_PASSWORD is left out (random password), a set one is passed"
echo "== 8. firewall trial under real systemd"
SD=kit-setup-systemd:1
if ! docker image inspect "$SD" >/dev/null 2>&1; then
  echo "== building $SD (once)"
  docker build -q -t "$SD" - >/dev/null <<'DOCKERFILE'
FROM debian:13
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      systemd systemd-sysv dbus openssh-server nftables iproute2 sudo procps python3 >/dev/null \
 && rm -rf /var/lib/apt/lists/* && useradd -m -s /bin/bash -u 1000 tester \
 && echo 'tester ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/tester && printf 'Port 2200\n' > /etc/ssh/sshd_config.d/custom.conf
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
DOCKERFILE
fi
docker network create "$id-home" >/dev/null; docker network create "$id-wan" >/dev/null
docker run -d --name "$id-sd" --hostname sd --privileged --tmpfs /run --tmpfs /run/lock --network "$id-home" \
  -v "$KIT:/kit:ro" "$SD" >/dev/null
docker network connect "$id-wan" "$id-sd"
for n in wan1 wan2; do start "$n" "$id-wan"; done
start home1 "$id-home"
docker network create "$id-tail8" >/dev/null; docker network connect "$id-tail8" "$id-sd"; start tpeer "$id-tail8"   # the tailnet
sd_up() { for _ in $(seq 60); do x sd systemctl is-active --quiet ssh 2>/dev/null && return 0; sleep 1; done; return 1; }
if ! sd_up; then
  echo "  skip: systemd does not boot in a container here ($(x sd systemctl is-system-running 2>&1 | head -n 1))"
else
  HOME_GW=$(docker network inspect -f '{{(index .IPAM.Config 0).Gateway}}' "$id-home")
  SD_WAN=$(ip_of sd "$id-wan") SD_HOME=$(ip_of sd "$id-home") WAN1=$(ip_of wan1 "$id-wan")
  HOME_NET=$(docker network inspect -f '{{(index .IPAM.Config 0).Subnet}}' "$id-home")
  # the LAN is the default route's network; sd's side of the tailnet network is named tailscale0, as on a real box
  sdroute() {
    SD_TAIL=$(ip_of sd "$id-tail8")
    xs sd "ip route replace default via $HOME_GW && d=\$(ip -o -4 addr show | awk -v a=$SD_TAIL '\$4 ~ \"^\" a \"/\" {print \$2}') \
      && { [ \"\$d\" = tailscale0 ] || { ip link set \$d down && ip link set \$d name tailscale0 && ip link set tailscale0 up; }; }"
  }
  sdroute
  x sd useradd -M -s /bin/sh mallory   # an unprivileged account on the box
  x wan1 sh -c 'mkdir -p -m 700 /root/.ssh && ssh-keygen -q -t ed25519 -N "" -f /root/.ssh/id_ed25519'
  key=$(x wan1 cat /root/.ssh/id_ed25519.pub)
  xs sd "install -d -m 700 -o tester -g tester /home/tester/.ssh && echo '$key' > /home/tester/.ssh/authorized_keys && chown tester: /home/tester/.ssh/authorized_keys"
  so="-p 2200 -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5"
  master() { x wan1 sh -c "rm -f /tmp/cm; ssh -fNM -S /tmp/cm $so tester@$SD_WAN </dev/null >/dev/null 2>&1 && ssh -S /tmp/cm -O check x 2>/dev/null"; }   # the admin's session
  old() { x wan1 ssh -S /tmp/cm $so tester@"$SD_WAN" "$@"; }                     # ... commands over it
  new() { x wan1 ssh -o ControlPath=none $so tester@"$SD_WAN" "$@"; }            # a NEW connection
  table() { x sd nft list table inet homeserver 2>/dev/null; }
  has() { local t; t=$(table) && [[ $t == *"$1"* ]]; }   # not table | grep -q: pipefail + SIGPIPE would fail it
  trial() { old "umask 022; cd /kit && sudo FW_CONFIRM_SECS=$1 servers/linux/setup.sh base --only=firewall" 2>&1; }
  gone() { for _ in $(seq 60); do x sd test -d /etc/homeserver/fw-rollback || return 0; sleep 1; done; return 1; }
  reboot() {
    docker restart -t 5 "$id-sd" >/dev/null && sd_up && sdroute || return 1
    SD_WAN=$(ip_of sd "$id-wan") SD_HOME=$(ip_of sd "$id-home"); master
  }
  # the firewall before: a table that lets everything in, loaded at boot by homeserver-firewall.service
  xs sd 'mkdir -p /etc/homeserver && printf "table inet homeserver {\n  chain input { type filter hook input priority 0; policy accept; comment \"kit-test-previous\"; }\n}\n" > /etc/homeserver/firewall.nft \
    && cp /kit/servers/tools/systemd/homeserver-firewall.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now homeserver-firewall.service' >/dev/null 2>&1
  prev=$(x sd cat /etc/homeserver/firewall.nft)
  master || fail "cannot reach sd's custom port 2200 from outside the LAN before the change"
  [ -n "$(old 'echo $SSH_CONNECTION')" ] && [ -z "$(old 'sudo sh -c "echo \$SSH_CONNECTION"')" ] || fail "sudo kept SSH_CONNECTION: the test would not test that"

  # A: unconfirmed -> the old firewall comes back; the rules file and boot setting were never changed
  out=$(trial 30) || fail "setup over sudo failed: $out"
  echo "$out" | grep -q 'setup.sh --confirm-firewall' || fail "no confirm instruction: $out"
  # R3-1: the lock is in a folder of root's, mode 700 (umask 022 here); the old path is a folder of root's too
  [ "$(x sd stat -c '%U %a' /run/homeserver-fw /run/homeserver-fw/lock /run/homeserver-fw.lock | paste -sd' ' -)" = "root 700 root 600 root 700" ] \
    || fail "the lock folder, lock or old path is not root's alone: $(x sd stat -c '%n %U %a' /run/homeserver-fw /run/homeserver-fw/lock /run/homeserver-fw.lock 2>&1)"
  x sd runuser -u mallory -- sh -c 'exec 9< /run/homeserver-fw/lock' 2>/dev/null && fail "another account can open the firewall lock"
  x sd runuser -u mallory -- flock -n /run/homeserver-fw/lock true 2>/dev/null && fail "another account can take the firewall lock"
  x sd runuser -u mallory -- flock -n /run/homeserver-fw.lock true 2>/dev/null && fail "another account can take the old lock path"
  has "ip saddr $WAN1 tcp dport 2200 accept" || fail "the admin's address outside the LAN was not kept: $(table)"
  has "ip saddr $HOME_NET tcp dport 2200 accept" || fail "the custom sshd port is not open to the LAN"
  [ "$(x sd cat /etc/homeserver/firewall.nft)" = "$prev" ] || fail "the trial changed the rules file before it was confirmed"
  new true || fail "a new connection from the admin's address was refused under the new rules"
  nc_ok wan2 "$SD_WAN" 2200 && fail "a stranger outside the LAN got in: the new rules do not filter"
  nc_ok home1 "$SD_HOME" 2200 || fail "the LAN cannot reach the custom sshd port"
  out=$(old 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) && fail "the old session confirmed: $out"
  echo "$out" | grep -q 'already open' || fail "unexpected refusal: $out"
  # the same from inside tmux or screen, where the process tree no longer leads to sshd (setsid cuts it off the same way)
  old "setsid -f sh -c 'sudo /kit/servers/linux/setup.sh --confirm-firewall > /tmp/detached.out 2>&1; echo rc=\$? >> /tmp/detached.out'"
  for _ in $(seq 20); do x sd grep -q '^rc=' /tmp/detached.out 2>/dev/null && break; sleep 1; done
  x sd grep -qx 'rc=1' /tmp/detached.out && x sd grep -q 'tmux or screen' /tmp/detached.out \
    || fail "the old session confirmed from outside its process tree (as in tmux): $(x sd cat /tmp/detached.out 2>&1)"
  gone || fail "the rollback did not run"
  has kit-test-previous || fail "the loaded table is not the one from before: $(table)"
  [ "$(x sd cat /etc/homeserver/firewall.nft)" = "$prev" ] || fail "the rules file is not the one from before"
  x sd systemctl is-enabled --quiet homeserver-firewall.service || fail "the boot setting was not restored (enabled)"
  x sd test -f /etc/homeserver/firewall.rolled-back || fail "no rolled-back marker"
  nc_ok wan2 "$SD_WAN" 2200 || fail "after the rollback the permissive firewall from before is not in effect"
  reboot || fail "sd did not come back after a restart"
  has kit-test-previous || fail "after a reboot the firewall is not the one from before: $(table)"

  # B: unconfirmed with no firewall before -> none again, also after a reboot
  xs sd 'systemctl disable --now homeserver-firewall.service && rm -f /etc/homeserver/firewall.nft' >/dev/null 2>&1
  out=$(trial 15) || fail "second trial failed: $out"
  has 'policy drop' || fail "the trial did not load the new rules"
  x sd systemctl is-enabled --quiet homeserver-firewall.service && fail "the trial enabled the firewall at boot before it was confirmed"
  gone || fail "the second rollback did not run"
  table && fail "a table is loaded although there was none before"
  x sd test ! -e /etc/homeserver/firewall.nft || fail "the rules file came back although there was none before"
  x sd systemctl is-enabled --quiet homeserver-firewall.service && fail "the firewall is enabled at boot although it was not before"
  reboot || fail "sd did not come back after the second restart"
  table && fail "after a reboot a table is loaded although there was none before"

  # D: a restart during the trial -> the firewall from before (none here) is what boots; nothing is left waiting
  out=$(trial 300) || fail "trial before the restart failed: $out"
  has 'policy drop' || fail "the trial before the restart did not load"
  reboot || fail "sd did not come back after a restart during the trial"
  table && fail "a restart during the trial booted the unconfirmed rules"
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) && echo "$out" | grep -q 'no firewall change is waiting' \
    || fail "after the restart a trial still waits: $out"

  # C: confirmed from a NEW session -> the new rules become the rules file, kept after the deadline and a reboot
  out=$(trial 15) || fail "third trial failed: $out"
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) || fail "a new session could not confirm: $out"
  x sd systemctl is-active --quiet homeserver-fw-rollback.timer && fail "the rollback timer still runs after the confirm"
  x sd test ! -e /etc/homeserver/fw-rollback || fail "the saved firewall is still there after the confirm"
  x sd grep -q "ip saddr $WAN1 tcp dport 2200 accept" /etc/homeserver/firewall.nft || fail "the confirm did not write the rules file"
  x sd systemctl is-enabled --quiet homeserver-firewall.service || fail "the confirm did not enable the firewall at boot"
  sleep 20
  has 'where setup.sh was last run from' || fail "the confirmed rules did not stay"
  reboot || fail "sd did not come back after the third restart"
  has "ip saddr $WAN1 tcp dport 2200 accept" || fail "after a reboot the confirmed rules are not loaded: $(table)"
  new true || fail "after a reboot the admin cannot open a new connection"
  nc_ok wan2 "$SD_WAN" 2200 && fail "after a reboot a stranger outside the LAN got in"
  ok "trial armed through sudo, running firewall only; admin outside the LAN on port 2200 kept, stranger dropped, LAN on 2200; old-session confirm refused (also cut off from sshd as in tmux); unconfirmed = old table back, rules file and boot setting untouched (also from nothing), and what a reboot loads; restart during the trial = old firewall; confirmed from a new session = rules file + boot, kept through the deadline and a reboot"

  # E-G: one lock serialises trial, re-arm, confirm and rollback (audit R2-1). A wrapper around nft logs every call and
  # holds the one named in /run/nft-pause until /run/nft-go exists. Each starts from the permissive table from before.
  x sd mv /usr/sbin/nft /usr/sbin/nft.real
  docker exec -i "$id-sd" sh -c 'cat > /usr/sbin/nft && chmod 755 /usr/sbin/nft' <<'WRAP'
#!/bin/sh
echo "$*" >> /run/nft-calls
if [ -f /run/nft-fail ] && [ "$*" = "$(cat /run/nft-fail)" ]; then echo "nft: injected failure" >&2; exit 1; fi
if [ -f /run/nft-pause ] && [ "$*" = "$(cat /run/nft-pause)" ]; then
  rm -f /run/nft-pause; : > /run/nft-paused
  while [ ! -e /run/nft-go ]; do sleep 0.2; done
  rm -f /run/nft-go /run/nft-paused
fi
exec /usr/sbin/nft.real "$@"
WRAP
  prevfw() {
    printf '%s\n' "$prev" | docker exec -i "$id-sd" sh -c 'systemctl stop homeserver-firewall.service; nft delete table inet homeserver 2>/dev/null
      cat > /etc/homeserver/firewall.nft && systemctl enable homeserver-firewall.service 2>/dev/null && systemctl start homeserver-firewall.service'
    has kit-test-previous || fail "cannot load the firewall from before: $(table)"
  }
  waitfor() { for _ in $(seq 60); do x sd test -e "$1" && return 0; sleep 0.5; done; return 1; }
  co=$(mktemp)
  # R3-1: an unprivileged account tries to open and take the lock every 0.2 s through E-H (rollback first, confirm
  # first, re-run at expiry, failed confirms); it must never get it
  x sd rm -f /tmp/mallory.log /tmp/mallory.stop
  docker exec -d "$id-sd" runuser -u mallory -- sh -c 'while [ ! -e /tmp/mallory.stop ]; do
    (exec 3< /run/homeserver-fw/lock) 2>/dev/null && echo "opened the lock" >> /tmp/mallory.log
    flock -n /run/homeserver-fw/lock true 2>/dev/null && echo "took the lock" >> /tmp/mallory.log
    flock -n /run/homeserver-fw.lock true 2>/dev/null && echo "took the old lock path" >> /tmp/mallory.log
    sleep 0.2; done'

  # E: the rollback wins. It holds the lock, paused inside nft just after its check; a confirm from a new session
  # waits for it, then reports the rollback. Loaded table, rules file and report all say: the firewall from before.
  prevfw
  xs sd 'echo "-f /etc/homeserver/fw-rollback/active.nft" > /run/nft-pause'
  out=$(trial 3) || fail "trial E failed: $out"
  waitfor /run/nft-paused || fail "E: the rollback did not start"
  new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' > "$co" 2>&1 & bg=$!
  sleep 3
  kill -0 "$bg" 2>/dev/null || fail "E: the confirm did not wait for the rollback that holds the lock: $(cat "$co")"
  x sd touch /run/nft-go
  if wait "$bg"; then fail "E: the confirm reported success after the rollback: $(cat "$co")"; fi
  grep -q 'too late' "$co" || fail "E: the confirm does not report the rollback: $(cat "$co")"
  has kit-test-previous && ! has 'policy drop' || fail "E: the loaded table is not the one from before: $(table)"
  [ "$(x sd cat /etc/homeserver/firewall.nft)" = "$prev" ] || fail "E: the rules file is not the one from before"
  x sd test ! -e /etc/homeserver/fw-rollback || fail "E: the trial was left behind"

  # F: the confirm wins. It holds the lock, paused while it reloads the firewall, when a rollback starts (the trial's
  # own script, as the timer runs it); the rollback waits, then finds the trial confirmed and leaves nftables alone
  # (without the lock it loaded the old table here, or deleted the table once the confirm had removed the snapshot)
  prevfw
  out=$(trial 120) || fail "trial F failed: $out"
  xs sd 'echo "-f /etc/homeserver/firewall.nft" > /run/nft-pause; rm -f /run/rollback.rc'
  new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' > "$co" 2>&1 & bg=$!
  waitfor /run/nft-paused || fail "F: the confirm did not reach the firewall reload: $(cat "$co")"
  # R3-4: just before the confirmed rules load, the table on trial is still there (a restart had deleted it by now),
  # a stranger is still dropped, and the rollback is still armed
  has 'policy drop' || fail "F: no table while the confirm loads the rules (stop, then start): $(table)"
  nc_ok wan2 "$SD_WAN" 2200 && fail "F: a stranger got in while the confirm loaded the rules"
  x sd systemctl is-active --quiet homeserver-fw-rollback.timer || fail "F: the rollback was cancelled before the confirmed rules loaded"
  xs sd ': > /run/nft-calls'
  docker exec -d "$id-sd" sh -c '/bin/sh /etc/homeserver/fw-rollback/rollback; echo $? > /run/rollback.rc'
  sleep 3
  x sd test ! -e /run/rollback.rc || fail "F: the rollback did not wait for the confirm that holds the lock"
  x sd touch /run/nft-go
  wait "$bg" || fail "F: the confirm failed: $(cat "$co")"
  grep -q 'firewall confirmed' "$co" || fail "F: the confirm did not report success: $(cat "$co")"
  waitfor /run/rollback.rc && [ "$(x sd cat /run/rollback.rc)" = 0 ] || fail "F: the rollback did not finish cleanly"
  calls=$(x sd cat /run/nft-calls)
  ! grep -qe 'fw-rollback/active.nft' -e 'delete table' <<<"$calls" || fail "F: the rollback touched nftables after the confirm: $calls"
  has "ip saddr $WAN1 tcp dport 2200 accept" || fail "F: the confirmed rules are not loaded: $(table)"
  x sd grep -q "ip saddr $WAN1 tcp dport 2200 accept" /etc/homeserver/firewall.nft || fail "F: the rules file is not the confirmed one"
  x sd test ! -e /etc/homeserver/firewall.rolled-back || fail "F: a rollback is reported although the rules were confirmed"
  x sd systemctl is-active --quiet homeserver-fw-rollback.timer && fail "F: a rollback is still armed after the confirm"

  # G: a re-run while the trial expires, in both orders. The lock is held here while the rollback comes due and the
  # re-run starts, so both wait; then one is held back (SIGSTOP) and the other goes first. Either way the end is one
  # trial: the re-run's rules loaded, the firewall from before saved and still the rules file, a rollback armed, the
  # run saying so; unconfirmed, the one from before comes back.
  for first in rollback re-run; do
    prevfw
    out=$(trial 6) || fail "trial G ($first first) failed: $out"
    docker exec -d "$id-sd" flock /run/homeserver-fw/lock sh -c 'while [ ! -e /run/lock-go ]; do sleep 0.2; done; rm -f /run/lock-go'
    for _ in $(seq 20); do x sd flock -n /run/homeserver-fw/lock true 2>/dev/null || break; sleep 0.2; done
    for _ in $(seq 30); do [ "$(x sd systemctl is-active homeserver-fw-rollback.service 2>/dev/null)" = active ] && break; sleep 0.5; done
    [ "$(x sd systemctl is-active homeserver-fw-rollback.service 2>/dev/null)" = active ] && has 'policy drop' \
      || fail "G: the rollback did not wait for the lock: $(table)"
    trial 8 > "$co" 2>&1 & bg=$!
    rerun=""
    for _ in $(seq 30); do   # the re-run's flock: the one whose parent runs setup.sh
      rerun=$(xs sd 'for p in $(pgrep -x flock); do grep -qs setup.sh /proc/$(ps -o ppid= -p $p | tr -d " ")/cmdline && echo $p; done' || true)
      [ -z "$rerun" ] || break; sleep 0.5
    done
    [ -n "$rerun" ] || fail "G: the re-run did not wait for the lock: $(cat "$co")"
    xs sd ': > /run/nft-calls'
    if [ "$first" = rollback ]; then x sd kill -STOP $rerun; else x sd systemctl kill --signal=SIGSTOP homeserver-fw-rollback.service; fi
    x sd touch /run/lock-go
    if [ "$first" = rollback ]; then gone || fail "G: the rollback did not go first"; x sd kill -CONT $rerun; fi
    wait "$bg" || fail "G ($first first): the re-run failed: $(cat "$co")"
    calls=$(x sd cat /run/nft-calls)
    if [ "$first" = rollback ]; then grep -q 'fw-rollback/active.nft' <<<"$calls" || fail "G: the rollback did not run first: $calls"
    else ! grep -q 'fw-rollback/active.nft' <<<"$calls" || fail "G: the rollback held back behind the re-run still ran: $calls"; fi
    has 'policy drop' || fail "G ($first first): the re-run's rules are not loaded: $(table)"
    x sd grep -q kit-test-previous /etc/homeserver/fw-rollback/active.nft || fail "G ($first first): the saved firewall is not the one from before"
    x sd systemctl is-active --quiet homeserver-fw-rollback.timer || fail "G ($first first): no rollback armed"
    [ "$(x sd cat /etc/homeserver/firewall.nft)" = "$prev" ] || fail "G ($first first): the rules file changed without a confirm"
    grep -q 'FIREWALL (on trial)' "$co" || fail "G ($first first): the re-run does not report the trial: $(cat "$co")"
    gone || fail "G ($first first): the rollback did not run"
    has kit-test-previous && ! has 'policy drop' || fail "G ($first first): the firewall from before did not come back: $(table)"
  done
  rm -f "$co"
  ok "one lock: a rollback holding it makes a confirm wait, which then reports the rollback (old table, old file); a rollback starting during a confirm waits and leaves nftables alone (confirmed table, file, no rollback reported); a re-run while the trial expires ends as one trial with the old firewall saved, then rolled back, whether the rollback or the re-run goes first"

  # H (audit R3-4): a confirm whose load fails (injected into the nft call that loads the rules file) fails, and leaves
  # the rules on trial loaded, the rules file and boot setting as before and the rollback armed
  co=$(mktemp)
  prevfw   # the service runs: the confirm reloads it
  out=$(trial 60) || fail "trial H failed: $out"
  xs sd 'echo "-f /etc/homeserver/firewall.nft" > /run/nft-fail'
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) && fail "H: the confirm reported success although the load failed: $out"
  echo "$out" | grep -q 'could not be made the rules loaded at boot' || fail "H: unexpected: $out"
  has 'policy drop' || fail "H: the rules on trial are gone after the failed confirm: $(table)"
  nc_ok wan2 "$SD_WAN" 2200 && fail "H: a stranger got in after the failed confirm"
  [ "$(x sd cat /etc/homeserver/firewall.nft)" = "$prev" ] || fail "H: the rules file was not put back"
  x sd systemctl is-enabled --quiet homeserver-firewall.service || fail "H: the failed confirm changed the boot setting"
  x sd systemctl is-active --quiet homeserver-fw-rollback.timer && x sd test -f /etc/homeserver/fw-rollback/active.nft \
    || fail "H: a confirm that failed cancelled the rollback"
  x sd rm -f /run/nft-fail
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) || fail "H: the confirm after the failure failed: $out"
  has "ip saddr $WAN1 tcp dport 2200 accept" && x sd grep -q "ip saddr $WAN1 tcp dport 2200 accept" /etc/homeserver/firewall.nft \
    || fail "H: the second confirm did not keep the rules: $(table)"
  x sd systemctl is-active --quiet homeserver-fw-rollback.timer && fail "H: a rollback is still armed after the second confirm"
  # the same with no firewall before (the service does not run: the confirm starts it); then the rollback comes
  xs sd 'systemctl disable --now homeserver-firewall.service; systemctl reset-failed homeserver-firewall.service
    rm -f /etc/homeserver/firewall.nft; nft delete table inet homeserver' >/dev/null 2>&1 || true
  out=$(trial 20) || fail "trial H2 failed: $out"
  xs sd 'echo "-f /etc/homeserver/firewall.nft" > /run/nft-fail'
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) && fail "H2: the confirm reported success although the start failed: $out"
  echo "$out" | grep -q 'could not be made the rules loaded at boot' || fail "H2: unexpected: $out"
  has 'policy drop' || fail "H2: the rules on trial are gone after the failed confirm: $(table)"
  nc_ok wan2 "$SD_WAN" 2200 && fail "H2: a stranger got in after the failed confirm"
  x sd test ! -e /etc/homeserver/firewall.nft || fail "H2: a rules file was left although there was none before"
  x sd systemctl is-enabled --quiet homeserver-firewall.service && fail "H2: the failed confirm left the firewall enabled at boot"
  x sd systemctl is-active --quiet homeserver-fw-rollback.timer || fail "H2: a confirm that failed cancelled the rollback"
  x sd rm -f /run/nft-fail
  gone || fail "H2: the rollback did not come after the failed confirm"
  table && fail "H2: after the rollback a table is loaded although there was none before"
  x sd touch /tmp/mallory.stop; sleep 1
  [ -z "$(x sd cat /tmp/mallory.log 2>/dev/null)" ] || fail "another account got at the firewall lock during E-H: $(x sd cat /tmp/mallory.log)"
  ok "R3-4: a confirm paused just before the load leaves the table on trial, a stranger dropped and the rollback armed; a failed load (service running or not) keeps the rules on trial loaded, the rules file and boot setting as before and the rollback armed; R3-1: another account never opened or took the lock through E-H"

  # L (audit R3-1): the lock folder is checked before use: a symlink (here to another account's folder) or a folder
  # others can enter is refused; made anew under umask 000 it is still root's, mode 700, and the lock mode 600
  x sd rm -f /etc/homeserver/firewall.rolled-back
  xs sd 'mv /run/homeserver-fw /run/homeserver-fw.real && install -d -o mallory -m 700 /tmp/mal && ln -s /tmp/mal /run/homeserver-fw'
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) && fail "L: a symlinked lock folder was used: $out"
  echo "$out" | grep -q 'not a folder of root' || fail "L: unexpected: $out"
  x sd test ! -e /tmp/mal/lock || fail "L: setup.sh made its lock in another account's folder"
  xs sd 'rm /run/homeserver-fw && mkdir -m 755 /run/homeserver-fw'
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) && fail "L: a lock folder of mode 755 was used: $out"
  echo "$out" | grep -q 'not a folder of root' || fail "L: unexpected (755): $out"
  xs sd 'rmdir /run/homeserver-fw && rm -rf /run/homeserver-fw.real'
  out=$(old "umask 000; sudo sh -c 'umask 000; exec /kit/servers/linux/setup.sh --confirm-firewall'" 2>&1) || fail "L: a confirm under umask 000 failed: $out"
  [ "$(x sd stat -c '%U %a' /run/homeserver-fw /run/homeserver-fw/lock | paste -sd' ' -)" = "root 700 root 600" ] \
    || fail "L: made under umask 000, the lock folder or lock is open to others: $(x sd stat -c '%n %U %a' /run/homeserver-fw /run/homeserver-fw/lock)"

  # M (audit R3-1): the lock of an older setup.sh, /run/homeserver-fw.lock, which any account could hold.
  # M1: a trial of the older version still waiting (its rollback knows only that lock) stops a confirm and a re-run;
  # the old lock stays a lock, and that trial rolls back by itself
  oldlock() { xs sd 'rm -rf /run/homeserver-fw.lock && (umask 022 && : > /run/homeserver-fw.lock)'; }
  prevfw
  out=$(trial 25) || fail "trial M1 failed: $out"
  xs sd 'sed -i "s|^exec 9> /run/homeserver-fw/lock\$|exec 9> /run/homeserver-fw.lock|" /etc/homeserver/fw-rollback/rollback'
  oldlock
  x sd grep -qx 'exec 9> /run/homeserver-fw.lock' /etc/homeserver/fw-rollback/rollback || fail "M1: cannot make the older trial"
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) && fail "M1: a confirm took over a trial of an older setup.sh: $out"
  echo "$out" | grep -q 'from an older setup.sh' || fail "M1: unexpected confirm: $out"
  out=$(trial 25) && fail "M1: a re-run took over a trial of an older setup.sh: $out"
  echo "$out" | grep -q 'from an older setup.sh' || fail "M1: unexpected re-run: $out"
  x sd test -f /run/homeserver-fw.lock || fail "M1: the old lock was retired while the older trial waited"
  has 'policy drop' || fail "M1: the older trial's rules are not loaded: $(table)"
  gone || fail "M1: the older trial did not roll back by itself"
  has kit-test-previous && ! has 'policy drop' || fail "M1: the firewall from before did not come back: $(table)"
  # M2: an older setup.sh holding its lock: a new run waits for it, then retires it; nothing can take it after that
  oldlock
  docker exec -d "$id-sd" flock /run/homeserver-fw.lock sh -c 'while [ ! -e /run/old-go ]; do sleep 0.2; done; rm -f /run/old-go'
  for _ in $(seq 20); do x sd flock -n /run/homeserver-fw.lock true 2>/dev/null || break; sleep 0.2; done
  xs sd ': > /run/nft-calls'
  trial 75 > "$co" 2>&1 & bg=$!
  sleep 4
  kill -0 "$bg" 2>/dev/null || fail "M2: the new run did not wait for the older run's lock: $(cat "$co")"
  grep -q 'waiting up to 30 s' "$co" || fail "M2: the new run does not say what it waits for: $(cat "$co")"
  ! x sd grep -q 'fw-rollback/firewall.nft' /run/nft-calls || fail "M2: the new run loaded its rules beside the older run"
  x sd touch /run/old-go
  wait "$bg" || fail "M2: the new run failed after the older one finished: $(cat "$co")"
  has 'policy drop' || fail "M2: the new run's rules are not loaded: $(table)"
  [ "$(x sd stat -c '%F %U %a' /run/homeserver-fw.lock)" = "directory root 700" ] || fail "M2: the old lock was not retired"
  x sd sh -c 'exec 9> /run/homeserver-fw.lock' 2>/dev/null && fail "M2: an older setup.sh could still take its lock"
  x sd runuser -u mallory -- flock -n /run/homeserver-fw.lock true 2>/dev/null && fail "M2: another account can take the old lock path"
  # M3: held by another account (anyone could open that file): a new run stops after 30 s and changes nothing
  oldlock
  docker exec -d "$id-sd" runuser -u mallory -- flock /run/homeserver-fw.lock sh -c 'while [ ! -e /tmp/mal-go ]; do sleep 0.2; done'
  for _ in $(seq 20); do x sd flock -n /run/homeserver-fw.lock true 2>/dev/null || break; sleep 0.2; done
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) && fail "M3: a confirm went on while another account held the old lock: $out"
  echo "$out" | grep -q 'still held after 30 s' || fail "M3: unexpected: $out"
  has 'policy drop' && x sd test -f /etc/homeserver/fw-rollback/active.nft || fail "M3: the refused confirm changed the trial"
  x sd touch /tmp/mal-go
  gone || fail "M3: M2's trial did not roll back"
  # M4: an older setup.sh that has its lock open (waiting for it, or about to) stops a new run, which leaves the lock to
  # it; another account's open copy does not
  oldlock
  x sd rm -f /etc/homeserver/firewall.rolled-back
  docker exec -d "$id-sd" runuser -u mallory -- sh -c 'exec 3< /run/homeserver-fw.lock; while [ ! -e /tmp/mal-go2 ]; do sleep 0.2; done'
  docker exec -d "$id-sd" sh -c 'exec 3< /run/homeserver-fw.lock; while [ ! -e /run/old-go ]; do sleep 0.2; done; rm -f /run/old-go'
  sleep 1
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) && fail "M4: a new run went on while an older one had the old lock open: $out"
  echo "$out" | grep -q 'is waiting for' || fail "M4: unexpected: $out"
  x sd test -f /run/homeserver-fw.lock || fail "M4: the old lock was retired under an older run"
  x sd touch /run/old-go; sleep 1
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) || fail "M4: a run failed with only another account holding the old lock open: $out"
  [ "$(x sd stat -c '%F' /run/homeserver-fw.lock)" = directory ] || fail "M4: another account's open copy kept the old lock from being retired"
  x sd touch /tmp/mal-go2
  ok "R3-1: lock folder root's, mode 700 (also under umask 000), lock 600; a symlinked or mode-755 folder refused; an older trial waiting stops a confirm and a re-run and rolls back by itself; a run waits for an older one holding the old lock, then retires it so nothing takes it again; held 30 s by another account, or open in an older run (not another account's), the new run stops"

  # O (audit R3-2): thor's Ollama listens on 0.0.0.0 behind a table of its own (homeserver-ollama-guard.service), not
  # behind the firewall on trial. A stand-in listens where OLLAMA_HOST says; its unit is the one setup.sh writes.
  xs sd 'systemctl disable --now homeserver-firewall.service; systemctl reset-failed homeserver-firewall.service
    rm -f /etc/homeserver/firewall.nft /etc/homeserver/firewall.rolled-back; nft delete table inet homeserver
    useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama' >/dev/null 2>&1 || true
  docker exec -i "$id-sd" sh -c 'cat > /usr/local/bin/ollama && chmod 755 /usr/local/bin/ollama' <<'FAKE'
#!/bin/sh
# stand-in for "ollama serve": listens where OLLAMA_HOST says (Ollama's default: 127.0.0.1:11434)
[ "$1" = serve ] || exit 1
h=${OLLAMA_HOST:-127.0.0.1:11434}
exec python3 -m http.server -b "${h%:*}" -d /tmp "${h##*:}"
FAKE
  sed -n "/^  put \/etc\/systemd\/system\/ollama.service 644 <<'EOF'\$/,/^EOF\$/p" "$KIT/servers/linux/setup.sh" | sed '1d;$d' \
    | docker exec -i "$id-sd" sh -c 'cat > /etc/systemd/system/ollama.service && systemctl daemon-reload && systemctl enable --now ollama' >/dev/null 2>&1
  listens() { for _ in $(seq 30); do [[ "$(x sd ss -Hltn 'sport = :11434')" == *"$1:11434"* ]] && return 0; sleep 0.5; done; return 1; }
  listens 127.0.0.1 || fail "O: the stand-in Ollama does not listen on its default, 127.0.0.1"
  ofns=$(sed -n '/^say()  /p; /^note() /p; /^warn() /p; /^die()  {/,/^}/p; /^run()  /p; /^next() /p; /^backup() {/,/^}/p; /^put() {/,/^}/p; /^putfile() /p; /^unit() {/,/^}/p; /^reload_units() /p; /^enable() {/,/^}/p; /^thor_ollama() {/,/^}/p' "$KIT/servers/linux/setup.sh")
  ollama_step() {   # setup.sh's Ollama step, as the thor lane runs it (Ollama is there: nothing is downloaded)
    docker exec -i "$id-sd" bash -s 2>&1 <<EOF
set -euo pipefail
DRY=0 CONFIRM=1 FW_SAVE=/nonexistent FW_LOCK=/nonexistent UNITS=/kit/servers/tools/systemd RELOAD=0 CHANGED=0 NEXT=()
$ofns
thor_ollama
EOF
  }
  closed() {   # 11434: open on loopback and to the tailnet, closed to the LAN and outside
    listens 0.0.0.0 || fail "O ($1): Ollama does not listen on 0.0.0.0"
    x sd bash -c 'exec 3<> /dev/tcp/127.0.0.1/11434' || fail "O ($1): Ollama is not reachable on loopback"
    nc_ok tpeer "$SD_TAIL" 11434 || fail "O ($1): the tailnet cannot reach Ollama"
    nc_ok home1 "$SD_HOME" 11434 && fail "O ($1): the LAN reached Ollama"
    # the LAN host routes the box's tailnet address over the LAN: the box accepts it there (any local address on any
    # interface), so only a rule on the interface stops it; binding Ollama to the tailnet address would not
    docker exec --privileged "$id-home1" ip route replace "$SD_TAIL/32" via "$SD_HOME" || fail "O: cannot route home1 to the tailnet address"
    nc_ok home1 "$SD_TAIL" 11434 && fail "O ($1): the LAN reached Ollama at the box's tailnet address"
    nc_ok wan2 "$SD_WAN" 11434 && fail "O ($1): a stranger outside reached Ollama"
    return 0
  }
  # a successful run on a box with no firewall before: the firewall on trial, then Ollama's step
  out=$(trial 20) || fail "trial O failed: $out"
  out=$(ollama_step) || fail "O: the Ollama step failed: $out"
  x sd systemctl is-active --quiet homeserver-ollama-guard.service || fail "O: the guard table is not loaded: $out"
  x sd grep -qx 'BindsTo=homeserver-ollama-guard.service' /etc/systemd/system/ollama.service.d/10-homeserver.conf || fail "O: Ollama is not bound to its guard"
  closed "on trial"
  # the trial expires: the firewall from before (none) is back, so sshd is open to the LAN again; 11434 stays closed
  gone || fail "O: the rollback did not run"
  table && fail "O: a table is loaded although there was none before"
  nc_ok home1 "$SD_HOME" 2200 || fail "O: with no firewall loaded the LAN does not reach sshd: the checks below would prove nothing"
  docker exec --privileged "$id-home1" ip route replace "$SD_TAIL/32" via "$SD_HOME"
  nc_ok home1 "$SD_TAIL" 2200 || fail "O: with no firewall loaded the LAN does not reach sshd at the tailnet address either: that check would prove nothing"
  closed "after the rollback to no firewall"
  # a run cut off mid-trial (killed while its trial loads): only the trial's hour-long rollback is armed; it comes
  xs sd 'echo "-f /etc/homeserver/fw-rollback/firewall.nft" > /run/nft-pause'
  trial 300 > "$co" 2>&1 & bg=$!
  waitfor /run/nft-paused || fail "O: the run to cut off did not reach its trial: $(cat "$co")"
  x sd pkill -KILL -f 'setup.sh base --only=firewall' || true
  x sd pkill -KILL -f 'fw-rollback/firewall.nft' || true
  wait "$bg" || true
  x sd rm -f /run/nft-paused
  x sd systemctl is-active --quiet homeserver-fw-rollback.timer || fail "O: the cut-off run left no rollback armed"
  closed "run cut off mid-trial"
  x sd systemctl start homeserver-fw-rollback.service   # what its timer does, an hour early
  gone || fail "O: the cut-off run's rollback did not run"
  x sd systemctl stop homeserver-fw-rollback.timer
  closed "after the cut-off run's rollback"
  # a restart before the confirm: the rules on trial do not boot, the guard and Ollama do
  out=$(trial 300) || fail "trial O (restart) failed: $out"
  reboot || fail "sd did not come back after a restart during the Ollama trial"
  table && fail "O: a restart during the trial booted the unconfirmed rules"
  x sd systemctl is-active --quiet homeserver-ollama-guard.service || fail "O: the guard table did not load at boot"
  closed "restart before the confirm"
  # confirmed: the tailnet still reaches Ollama, the LAN and outside do not, also after a restart
  out=$(trial 60) || fail "trial O (confirm) failed: $out"
  out=$(new 'sudo /kit/servers/linux/setup.sh --confirm-firewall' 2>&1) || fail "O: the confirm failed: $out"
  closed "confirmed"
  reboot || fail "sd did not come back after a restart with the confirmed rules"
  has 'policy drop' || fail "O: the confirmed rules did not boot: $(table)"
  closed "confirmed, after a restart"
  # Ollama does not run without its table: a guard that cannot load (injected) takes Ollama down and keeps it down
  xs sd 'sed -n "s/^ExecStart=\/usr\/sbin\/nft \"\(.*\)\"\$/\1/p" /etc/systemd/system/homeserver-ollama-guard.service > /run/nft-fail'
  x sd test -s /run/nft-fail || fail "O: cannot read the guard's nft command"
  x sd systemctl restart homeserver-ollama-guard.service 2>/dev/null && fail "O: the guard loaded despite the injected failure"
  x sd systemctl is-active --quiet ollama && fail "O: Ollama runs without its guard table"
  x sd systemctl start ollama 2>/dev/null && fail "O: Ollama started without its guard table"
  [[ "$(x sd ss -Hltn 'sport = :11434')" == *11434* ]] && fail "O: something listens on 11434 without the guard"
  x sd rm -f /run/nft-fail
  x sd systemctl start ollama || fail "O: Ollama did not start with its guard"
  x sd systemctl is-active --quiet homeserver-ollama-guard.service || fail "O: starting Ollama did not load its guard"
  closed "guard back"
  # a re-run of the step changes nothing (no Ollama restart, which would unload its models)
  out=$(ollama_step) || fail "O: a re-run of the Ollama step failed: $out"
  grep -q '= homeserver-ollama-guard.service enabled and running' <<<"$out" && grep -q '10-homeserver.conf (unchanged)' <<<"$out" \
    && ! grep -q 'restart ollama' <<<"$out" || fail "O: a re-run of the Ollama step changed something: $out"
  # a box an older setup.sh set up (Ollama on 0.0.0.0, no guard) with no firewall: open to the LAN until the step runs
  xs sd 'systemctl disable --now homeserver-ollama-guard.service; rm -f /etc/systemd/system/homeserver-ollama-guard.service
    printf "[Service]\nEnvironment=\"OLLAMA_HOST=0.0.0.0:11434\"\n" > /etc/systemd/system/ollama.service.d/10-homeserver.conf
    systemctl daemon-reload; systemctl restart ollama; nft delete table inet homeserver' >/dev/null 2>&1 || true
  listens 0.0.0.0 && nc_ok home1 "$SD_HOME" 11434 || fail "O: the older setup is not open to the LAN: the next check would prove nothing"
  out=$(ollama_step) || fail "O: the Ollama step on a box an older setup.sh set up failed: $out"
  closed "set up by an older setup.sh, then this step"
  rm -f "$co"
  ok "R3-2: Ollama on 0.0.0.0 behind its own table: open to the tailnet and loopback, closed to the LAN (also at the box's tailnet address) and outside while on trial, after the rollback to no firewall, after a run cut off mid-trial and its rollback, after a restart before the confirm, and confirmed (also after a restart); without its table it does not run; a re-run changes nothing; a box an older setup.sh left open is closed by the step"
fi

echo "== 9. downloads are pinned and checked"
grep -nE 'curl[^#]*\|[[:space:]]*(sudo[[:space:]]+)?(ba)?sh' "$KIT/servers/linux/setup.sh" && fail "setup.sh still pipes a download into a shell"
[ "$(grep -c 'curl -' "$KIT/servers/linux/setup.sh")" = "$(sed -n '/^fetch() {/,/^}/p' "$KIT/servers/linux/setup.sh" | grep -c 'curl -')" ] \
  || fail "setup.sh calls curl outside fetch()"
# the Ollama install path with a substituted tarball (a fake curl serves it): refused, nothing unpacked
fns=$(sed -n '/^die()  {/,/^}/p; /^run()  /p; /^next() /p; /^apt_install() {/,/^}/p; /^fetch() {/,/^}/p; /^install_ollama() {/,/^}/p' "$KIT/servers/linux/setup.sh")
xs lanhost 'mkdir -p /fake && printf "%s\n" "#!/bin/sh" "while [ \$# -gt 0 ]; do [ \"\$1\" = -o ] && { echo not-the-release > \"\$2\"; exit 0; }; shift; done; exit 1" > /fake/curl && chmod 755 /fake/curl'
out=$(docker exec -i "$id-lanhost" env PATH=/fake:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin bash -s 2>&1 <<EOF
set -euo pipefail
DRY=0 CONFIRM=1 FW_SAVE=/nonexistent ADMIN=tester APT_UPDATED=1 NEXT=()
OLLAMA_VERSION=0.0.0 OLLAMA_SHA256_amd64=$(printf '0%.0s' $(seq 64)) OLLAMA_SHA256_arm64=$(printf '0%.0s' $(seq 64))
$fns
install_ollama
EOF
) && fail "a tarball with the wrong sha256 was accepted: $out"
echo "$out" | grep -q 'not the 0000' || fail "unexpected failure: $out"
xs lanhost '[ ! -e /usr/local/bin/ollama ] && [ ! -e /usr/local/lib/ollama ] && ! ls /var/tmp/ollama.*/ollama.tar.zst* >/dev/null 2>&1' || fail "something of the refused download was unpacked or left behind"
good=$(printf 'not-the-release\n' | sha256sum | cut -d' ' -f1)
out=$(docker exec -i "$id-lanhost" env PATH=/fake:/usr/bin:/bin bash -s 2>&1 <<EOF
set -euo pipefail
DRY=0 CONFIRM=1 FW_SAVE=/nonexistent
$fns
fetch https://example.invalid/f $good /tmp/fetched && stat -c %a /tmp/fetched
EOF
) || fail "the right sha256 was refused: $out"
echo "$out" | grep -qx 644 || fail "the accepted file is not mode 644: $out"
ok "no curl | sh; a substituted Ollama tarball is refused before anything is unpacked; the pinned sum is accepted"

echo "== 10. host-heat: one safe CSV writer, whichever installer ran last"
# The framework installer (as install.sh runs it: tester's private staging dir, then root) and setup.sh's heat step
# share /usr/local/bin/host-heat and /etc/host-heat/env. Audit S1 round 2: setup.sh put back a writer that ignored the
# framework's HOST_HEAT_USER and wrote in tester's home as root. test_host_heat.py --installed runs its fixtures against
# exactly the pair each order leaves.
hh=/kit/framework/_tools/host-heat
heat_fns=$(sed -n '/^say()  /p; /^note() /p; /^warn() /p; /^die()  {/,/^}/p; /^run()  /p; /^next() /p; /^backup() {/,/^}/p;
  /^put() {/,/^}/p; /^putfile() /p; /^seed() {/,/^}/p; /^unit() {/,/^}/p; /^reload_units() /p; /^enable() {/,/^}/p;
  /^base_heat_alerts() {/,/^}/p' "$KIT/servers/linux/setup.sh")
for order in "framework server" "server framework"; do
  c=hh${order%% *} last=${order##* }
  start "$c" "$lan"
  xs "$c" 'mkdir -p /stub && printf "#!/bin/sh\nexit 0\n" > /stub/modprobe && chmod 755 /stub/modprobe'
  for step in $order; do
    if [ "$step" = framework ]; then
      xs "$c" "s=\$(runuser -u tester -- mktemp -d) && runuser -u tester -- cp $hh/host-heat \$s/ && PATH=/stub:\$PATH bash $hh/remote-install.sh tester - - \$s >/dev/null && rm -rf \$s" \
        || fail "$order: the framework installer failed"
    else
      docker exec -i "$id-$c" bash -s >/dev/null <<EOF || fail "$order: setup.sh's heat step failed"
set -euo pipefail
PATH=/stub:\$PATH DRY=0 CONFIRM=0 FW_SAVE=/nonexistent ADMIN=tester HOST=$c ETC=/etc/homeserver
TOOLS=/kit/servers/tools UNITS=/kit/servers/tools/systemd RELOAD=0 CHANGED=0 NEXT=()
$heat_fns
base_heat_alerts
EOF
    fi
  done
  want=/kit/servers/tools/host-heat; [ "$last" = framework ] && want=$hh/host-heat
  x "$c" cmp -s /usr/local/bin/host-heat "$want" || fail "$order: the installed host-heat is not the $last copy"
  x "$c" grep -qx HOST_HEAT_USER=tester /etc/host-heat/env || fail "$order: the env has no HOST_HEAT_USER: $(x "$c" cat /etc/host-heat/env)"
  out=$(x "$c" python3 "$hh/test_host_heat.py" --installed 2>&1) || fail "$order: the installed pair is not safe:
$out"
  # a timing --set with a real action (hot_min=0: power off on the first HOT check) is refused, as --fake is
  out=$(x "$c" runuser -u tester -- /usr/local/bin/host-heat --check --set hot_min=0 2>&1) \
    && fail "$order: host-heat took a real --check with --set hot_min=0: $out"
  grep -q -- --dry-run <<<"$out" || fail "$order: host-heat did not refuse a real --check with --set hot_min=0: $out"
  ok "$order: the $last copy with the env left behind writes the CSV as tester; symlinked dir, ancestor, month file and FIFO fixtures untouched; a real --check with --set refused"
done

echo "PASS: dry runs, power policy, sleep hold, suspend path, one-way firewall, loki backups and restore, Samba, Pi-hole password, firewall trial under systemd with its lock, pinned downloads, host-heat in both install orders"
