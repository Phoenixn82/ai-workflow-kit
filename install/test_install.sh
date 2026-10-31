#!/usr/bin/env bash
# test_install.sh [--system | --containers]       PASS/FAIL per check; exit 1 if any fail.
#   (no flag)     dry-run plans for arch, debian and macos; the hardware gate; a --harness dry run twice on a
#                 pre-populated ~/.claude; real writes twice in a temp HOME (HOME-only tools, stub claude, codex,
#                 systemctl, loginctl): backups, idempotency, no clobbering. Safe on any machine: nothing outside the
#                 temp HOME is written.
#   --system      also writes outside HOME (a compose project under /opt, a sudo copy to /usr/local/bin, units) with
#                 a stub docker and curl. Only as root inside a throwaway container; it refuses anywhere else.
#   --containers  runs the suite with --system in debian:stable and archlinux:latest (docker or podman), after
#                 install.sh bootstraps Python and installs git for real. Needs the network.
set -uo pipefail
KIT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INSTALL=$KIT/install/install.sh
export PYTHONDONTWRITEBYTECODE=1 KIT_HEALTH_WAIT=2
fails=0 n=0
pass() { n=$((n + 1)); echo "PASS $*"; }
fail() { n=$((n + 1)); fails=$((fails + 1)); echo "FAIL $*"; }
check() { local what=$1; shift; if "$@" >/dev/null 2>&1; then pass "$what"; else fail "$what"; fi; }
TMP=$(mktemp -d "${TMPDIR:-/tmp}/kit-test.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

if [ "${1:-}" = --containers ]; then
  engine=$(command -v docker || command -v podman) || { echo "FAIL --containers: no docker or podman"; exit 1; }
  rc=0
  for img in debian:stable archlinux:latest; do
    echo "== $img"
    "$engine" run --rm -v "$KIT:/kit:ro" "$img" bash -c '
      set -e
      . /etc/os-release
      [ "$ID" = arch ] && pacman -Sy --noconfirm >/dev/null   # a real Arch box has a package database; the image does not
      echo "-- bootstrap: no Python yet, install.sh installs it, then git for real"
      ! command -v python3 >/dev/null
      /kit/install/install.sh --profile raven --only git --yes
      git --version
      echo "-- second run: everything present"
      /kit/install/install.sh --profile raven --only git --yes | grep -q "present .*git"
      bash /kit/install/test_install.sh --system' || rc=1
  done
  [ $rc = 0 ] && echo "PASS containers (debian:stable, archlinux:latest)" || echo "FAIL containers"
  exit $rc
fi
SYSTEM=""
if [ "${1:-}" = --system ]; then
  [ "$(id -u)" = 0 ] && { [ -f /.dockerenv ] || [ -f /run/.containerenv ]; } ||
    { echo "FAIL --system runs only as root inside a throwaway container"; exit 1; }
  SYSTEM=1
fi

OS=$(python3 "$KIT/install/lib.py" detect-os)
STUB=$TMP/stub LOG=$TMP/stub.log
mkdir -p "$STUB"
export STUB_LOG=$LOG
mk() { printf '#!/bin/sh\n%s\n' "$2" > "$STUB/$1"; chmod +x "$STUB/$1"; }
mk claude 'case "$1" in --version) echo "2.1.0 (Claude Code)";; mcp) exit 1;; esac; exit 0'
mk codex 'echo "codex-cli 0.50.0"'
mk systemctl 'echo "systemctl $*" >> "$STUB_LOG"; case " $* " in *" is-enabled "*) exit 1;; esac; exit 0'
mk loginctl 'echo "loginctl $*" >> "$STUB_LOG"; case "$1" in show-user) echo no;; esac; exit 0'
mk sg 'echo "sg $*" >> "$STUB_LOG"'
BASEPATH=$STUB:/usr/bin:/bin:/usr/sbin:/sbin
HW='{"ram_gb": 32, "disk_gb": 1000}'

tree_sum() {  # contents, modes and link targets of every file under the given roots (tool caches aside: a verify
             # such as `mise --version` writes ~/.cache, which is the tool's doing, not the installer's)
  python3 - "$@" <<'PY'
import hashlib, os, sys
h = hashlib.sha256()
for root in sys.argv[1:]:
    for d, dirs, files in sorted(os.walk(root)):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(d, f)
            if "__pycache__" in p or p.endswith("stub.log") or "/.cache/" in p or "/.npm/" in p:
                continue
            st = os.lstat(p)
            h.update(p.encode() + oct(st.st_mode).encode())
            h.update(os.readlink(p).encode() if os.path.islink(p) else open(p, "rb").read())
print(h.hexdigest())
PY
}
same_file() { python3 -c 'import filecmp, sys; sys.exit(not filecmp.cmp(sys.argv[1], sys.argv[2], shallow=False))' "$1" "$2" 2>/dev/null; }   # no cmp on a minimal Arch
# What the installer itself writes under HOME. A verify command may leave its own config elsewhere (gh, mise and
# friends on a CI runner do); that is the tool's doing, so the checks below look only at these.
KIT_ROOTS=".claude .codex .config/ai-workflow-kit .config/systemd AI_Projects .local/bin .local/telegram services"
roots() { local r out=""; for r in $KIT_ROOTS; do [ -e "$1/$r" ] && out+=" $1/$r"; done; echo "${out# }"; }
count_bak() { find "$@" -name '*.bak-*' 2>/dev/null | wc -l | tr -d ' '; }

# ------------------------------------------------------------------------------------------- A. plans per OS
ids=$(python3 -c "import json; print(' '.join(json.load(open('$KIT/profiles/odin/profile.json'))['tools']))")
for os_ in arch debian macos; do
  H=$TMP/plan-$os_; mkdir -p "$H"
  out=$(HOME=$H PATH=$BASEPATH KIT_FAKE_HW=$HW "$INSTALL" --profile odin --dry-run --os "$os_" 2>&1); rc=$?
  [ $rc = 0 ] && pass "odin dry run on $os_ exits 0" || fail "odin dry run on $os_ exits 0 (rc $rc)"
  missing=""
  for id in $ids; do
    line=$(grep -E "^  [a-z]+ +$id( |$)" <<<"$out" | head -1)
    grep -Eq "(arch|debian|macos|mise|uv|npm|docker|claude-plugin|bundled): |install manually: |skip: not for $os_" <<<"$line" ||
      missing+=" $id"
  done
  [ -z "$missing" ] && pass "odin on $os_: every tool has a method, a manual line or a skip reason" ||
    fail "odin on $os_: no method for:$missing"
  grep -q "Overwritten without a backup: 0" <<<"$out" && pass "odin on $os_: dry run changes nothing" ||
    fail "odin on $os_: dry-run footer missing"
  [ -z "$(roots "$H")" ] && pass "odin on $os_: the dry run wrote nothing to HOME" ||
    fail "odin on $os_: dry run wrote to HOME: $(roots "$H")"
done
for p in loki heimdall thor raven telegram; do
  for os_ in debian macos; do
    out=$(HOME=$TMP/plan-debian PATH=$BASEPATH KIT_FAKE_HW='{"ram_gb":64,"disk_gb":4000,"nvidia_vram_gb":24}' \
      "$INSTALL" --profile "$p" --dry-run --os "$os_" 2>&1); rc=$?
    [ $rc = 0 ] && pass "$p dry run on $os_" || fail "$p dry run on $os_ (rc $rc): $(tail -3 <<<"$out")"
    if [ "$p" = heimdall ] && [ "$os_" = debian ] && [ ! -d /opt/homeserver/heimdall ] && [ ! -d "$HOME/homeserver/heimdall" ]; then
      grep -q 'Not counted above: server-lane-heimdall runs `sudo servers/linux/setup.sh heimdall`.*`servers/linux/setup.sh heimdall --dry-run`' <<<"$out" &&
        pass "heimdall dry run: the server setup's own writes are pointed at, not counted as none" ||
        fail "heimdall dry run: no pointer to setup.sh's own --dry-run"
    fi
  done
done
out=$("$INSTALL" --profile nope --dry-run 2>&1); [ $? = 2 ] && pass "unknown profile exits 2" || fail "unknown profile exits 2"
other=debian; [ "$OS" = debian ] && other=arch
out=$(HOME=$TMP/plan-debian PATH=$BASEPATH KIT_FAKE_HW=$HW "$INSTALL" --profile raven --os $other --yes 2>&1)
[ $? = 2 ] && pass "a real run with a foreign --os exits 2" || fail "a real run with a foreign --os exits 2"
out=$(HOME=$TMP/plan-debian PATH=$BASEPATH KIT_FAKE_HW=$HW "$INSTALL" --profile raven </dev/null 2>&1)
[ $? = 2 ] && grep -q "no terminal" <<<"$out" && pass "no terminal and no --yes: nothing installed, exit 2" ||
  fail "no terminal and no --yes must stop"

# ------------------------------------------------------------------------------------------- B. hardware gate
out=$(HOME=$TMP/plan-debian PATH=$BASEPATH KIT_FAKE_HW='{"ram_gb":32,"nvidia_vram_gb":4}' \
  "$INSTALL" --profile thor --dry-run --os debian 2>&1); rc=$?
[ $rc = 2 ] && grep -q "prerequisite: thor: needs an NVIDIA GPU with 8 GB" <<<"$out" &&
  pass "thor on a 4 GB GPU stops at the prerequisite (exit 2)" || fail "thor on a 4 GB GPU: rc $rc: $out"
mkdir -p "$TMP/nogpu"   # every command of /usr/bin and /bin except nvidia-smi
for d in /bin /usr/bin; do for f in "$d"/*; do b=${f##*/}
  [ "$b" = nvidia-smi ] || [ -e "$TMP/nogpu/$b" ] || [ -L "$TMP/nogpu/$b" ] || ln -s "$f" "$TMP/nogpu/$b"; done; done
out=$(HOME=$TMP/plan-debian PATH=$TMP/nogpu "$INSTALL" --profile thor --dry-run --os debian 2>&1); rc=$?
[ $rc = 2 ] && grep -q "no NVIDIA GPU" <<<"$out" && pass "thor with nvidia-smi absent stops (exit 2)" ||
  fail "thor with nvidia-smi absent: rc $rc: $(tail -2 <<<"$out")"
mk nvidia-smi 'echo 24576'
out=$(HOME=$TMP/plan-debian PATH=$BASEPATH KIT_FAKE_HW= "$INSTALL" --profile thor --dry-run --os debian 2>&1); rc=$?
ram=$(python3 -c "import sys; sys.path.insert(0,'$KIT/install'); import lib; print(int(lib.hw_facts()['ram_gb']))")
if [ "$ram" -ge 15 ]; then
  [ $rc = 0 ] && pass "thor with a 24 GB NVIDIA GPU (real probe) passes" || fail "thor with a 24 GB GPU: rc $rc: $(head -3 <<<"$out")"
fi
rm -f "$STUB/nvidia-smi"
out=$(HOME=$TMP/plan-debian PATH=$BASEPATH KIT_FAKE_HW='{"ram_gb":16,"apple_silicon":true,"unified_memory_gb":16}' \
  "$INSTALL" --profile thor --dry-run --os macos 2>&1)
[ $? = 0 ] && pass "thor on a 16 GB Apple-silicon Mac passes" || fail "thor on a 16 GB Apple-silicon Mac"
out=$(HOME=$TMP/plan-debian PATH=$BASEPATH KIT_FAKE_HW='{"ram_gb":8,"apple_silicon":true,"unified_memory_gb":8}' \
  "$INSTALL" --profile thor --dry-run --os macos 2>&1)
[ $? = 2 ] && pass "thor on an 8 GB Mac stops (exit 2)" || fail "thor on an 8 GB Mac must stop"
out=$(HOME=$TMP/plan-debian PATH=$BASEPATH KIT_FAKE_HW='{"ram_gb":8,"disk_gb":100}' \
  "$INSTALL" --profile loki --dry-run --os debian 2>&1)
[ $? = 2 ] && grep -q "250 GB disk" <<<"$out" && pass "loki on a 100 GB disk stops (exit 2)" || fail "loki disk gate"
out=$(HOME=$TMP/plan-debian PATH=$BASEPATH KIT_FAKE_HW='{"ram_gb":8,"disk_gb":100}' \
  "$INSTALL" --profile loki --dry-run --os debian --ignore-hardware 2>&1)
[ $? = 0 ] && pass "--ignore-hardware goes on with a warning" || fail "--ignore-hardware"

# ------------------------------------------------------------------------------------------- C. --harness dry run, twice
H=$TMP/harness-dry; mkdir -p "$H/.claude"
cat > "$H/.claude/settings.json" <<'JSON'
{"model": "mine", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
JSON
printf '# Mine\n\n## My rules\n- keep me\n' > "$H/.claude/CLAUDE.md"
before=$(tree_sum $(roots "$H"))
for i in 1 2; do
  out=$(HOME=$H PATH=$BASEPATH KIT_FAKE_HW=$HW "$INSTALL" --profile odin --dry-run --harness 2>&1); rc=$?
  [ $rc = 0 ] && grep -q "would back up ~/.claude/settings.json -> ~/.claude/settings.json.bak-" <<<"$out" &&
    grep -q "would merge into ~/.claude/CLAUDE.md: ## How to work" <<<"$out" &&
    pass "--harness dry run $i: reports the backup and the merge" || fail "--harness dry run $i (rc $rc)"
  [ "$(roots "$H")" = "$H/.claude" ] && [ "$(tree_sum $(roots "$H"))" = "$before" ] && [ "$(count_bak "$H")" = 0 ] &&
    pass "--harness dry run $i: no file changed" || fail "--harness dry run $i changed files: $(roots "$H")"
done

# ------------------------------------------------------------------------------------------- D. real writes, twice
H=$TMP/home; mkdir -p "$H/.claude/hooks" "$H/.config/ai-workflow-kit" "$H/AI_Projects/.claude/hooks"
cp "$TMP/harness-dry/.claude/settings.json" "$TMP/harness-dry/.claude/CLAUDE.md" "$H/.claude/"
printf 'THOR_HOST=mine\n' > "$H/.config/ai-workflow-kit/hosts.env"; chmod 600 "$H/.config/ai-workflow-kit/hosts.env"
printf '# my own boot hook\n' > "$H/AI_Projects/.claude/hooks/inject-vault-boot.sh"
printf 'echo my own guard\n' > "$H/.claude/hooks/webcred-guard.sh"
# a dotfiles manager's link: the merge goes into the file it points at and the link stays
mkdir -p "$H/AI_Projects/dotfiles" "$H/.codex"; printf '# Codex, mine\n\n## Mine\n- keep me\n' > "$H/AI_Projects/dotfiles/AGENTS.md"
ln -s ../AI_Projects/dotfiles/AGENTS.md "$H/.codex/AGENTS.md"
mkdir -p "$TMP/orig"; cp -a "$H/." "$TMP/orig/"
only=workspace,hosts-env,vault,route,vault-lint,harness-lint,handoff-v2,claude-harness,codex-harness,kit-skills,secret-firewall-corpus
[ "$OS" != macos ] && only+=,retention
run() { HOME=$H PATH=$BASEPATH KIT_FAKE_HW=$HW "$INSTALL" --profile odin --only "$only" --yes --optional all --harness 2>&1; }
: > "$LOG"
out1=$(run); rc=$?
[ $rc = 0 ] && pass "real run 1 exits 0" || { fail "real run 1 exits 0 (rc $rc)"; tail -25 <<<"$out1"; }
J() { python3 -c "import json,sys; d=json.load(open('$H/.claude/settings.json')); sys.exit(0 if ($1) else 1)"; }
check "settings.json: your model kept" J 'd["model"] == "mine"'
check "settings.json: your Stop hook kept" J '"echo mine" in json.dumps(d["hooks"]["Stop"])'
check "settings.json: the dispatcher hook added with your home spelled out" \
  J "any('$H/.claude/hooks/dispatch/dispatch.sh' in __import__('shlex').split(h['command']) for g in d['hooks']['PreToolUse'] for h in g['hooks'])"
check "settings.json: no hook for a tool that is not installed (vault-rag)" J '"vault-rag" not in json.dumps(d["hooks"])'
check "settings.json: OLLAMA_HOST left out without THOR_OLLAMA_URL" J '"OLLAMA_HOST" not in d["env"]'
check "settings.json: env paths rendered" J "d['env']['AI_PROJECTS_ROOT'] == '$H/AI_Projects'"
check "settings.json: no plugin enabled that is not installed" J '"enabledPlugins" not in d'
b=$(ls "$H"/.claude/settings.json.bak-* 2>/dev/null | head -1)
[ -n "$b" ] && same_file "$b" "$TMP/orig/.claude/settings.json" && pass "settings.json: original backed up beside it" ||
  fail "settings.json backup"
grep -q '^## My rules' "$H/.claude/CLAUDE.md" && grep -q '^## How to work' "$H/.claude/CLAUDE.md" &&
  pass "CLAUDE.md: your section kept, the kit's appended" || fail "CLAUDE.md merge"
b=$(ls "$H"/.claude/CLAUDE.md.bak-* 2>/dev/null | head -1)
[ -n "$b" ] && same_file "$b" "$TMP/orig/.claude/CLAUDE.md" && pass "CLAUDE.md: original backed up" || fail "CLAUDE.md backup"
same_file "$H/.config/ai-workflow-kit/hosts.env" "$TMP/orig/.config/ai-workflow-kit/hosts.env" &&
  pass "hosts.env: yours untouched" || fail "hosts.env was changed"
same_file "$H/AI_Projects/.claude/hooks/inject-vault-boot.sh" "$TMP/orig/AI_Projects/.claude/hooks/inject-vault-boot.sh" &&
  grep -q "kept: ~/AI_Projects/.claude/hooks/inject-vault-boot.sh" <<<"$out1" &&
  pass "a non-harness link onto your own file keeps yours and says so" || fail "inject-vault-boot.sh clobbered or not reported"
b=$(ls "$H"/.claude/hooks/webcred-guard.sh.bak-* 2>/dev/null | head -1)
same_file "$H/.claude/hooks/webcred-guard.sh" "$KIT/framework/harness/hooks/webcred-guard.sh" && [ -n "$b" ] &&
  grep -q "my own guard" "$b" && pass "a --harness copy backs up your file, then replaces it" || fail "harness copy backup"
[ "$(readlink "$H/.local/bin/route")" = "$KIT/framework/harness/bin/route" ] && pass "route linked onto PATH" ||
  fail "route link"
[ -L "$H/.claude/skills/codex-review" ] && pass "kit skills linked (harness)" || fail "kit skills"
python3 -c "import json; d=json.load(open('$H/.codex/hooks.json')); assert 'vault-rag' not in json.dumps(d) and 'vault-sync pull' in json.dumps(d)" &&
  pass "codex hooks.json made, without hooks for missing scripts" || fail "codex hooks.json"
b=$(ls "$H"/AI_Projects/dotfiles/AGENTS.md.bak-* 2>/dev/null | head -1)
[ "$(readlink "$H/.codex/AGENTS.md")" = ../AI_Projects/dotfiles/AGENTS.md ] && grep -q '^## Mine' "$H/AI_Projects/dotfiles/AGENTS.md" &&
  [ "$(grep -c '^## ' "$H/AI_Projects/dotfiles/AGENTS.md")" -gt 1 ] && [ -n "$b" ] && [ ! -L "$b" ] &&
  same_file "$b" "$TMP/orig/AI_Projects/dotfiles/AGENTS.md" &&
  pass "a linked AGENTS.md: merged into the file behind the link, the link kept, the original backed up" ||
  fail "linked AGENTS.md: link $(readlink "$H/.codex/AGENTS.md"), backup '$b'"
if [ "$OS" != macos ]; then
  [ -f "$H/.config/systemd/user/retain.timer" ] && pass "retention units installed" || fail "retention units"
  [ "$(grep -c 'retain' "$H/AI_Projects/_system/automations/REGISTRY.md" 2>/dev/null)" = 1 ] &&
    grep -q 'systemctl --user disable --now retain.timer' "$H/AI_Projects/_system/automations/REGISTRY.md" &&
    pass "registry row with the kill switch" || fail "registry row"
  grep -q 'systemctl --user daemon-reload' "$LOG" && ! grep -q 'enable --now' "$LOG" && ! grep -q enable-linger "$LOG" &&
    pass "units reloaded, nothing enabled (enable: false), no lingering" || fail "systemctl calls: $(cat "$LOG")"
fi
baks=$(count_bak "$H"); sum=$(tree_sum $(roots "$H"))
out2=$(run); rc=$?
[ $rc = 0 ] && pass "real run 2 exits 0" || { fail "real run 2 exits 0 (rc $rc)"; tail -20 <<<"$out2"; }
[ "$(count_bak "$H")" = "$baks" ] && pass "run 2 made no new backups" || fail "run 2 made new backups"
[ "$(tree_sum $(roots "$H"))" = "$sum" ] && pass "run 2 changed nothing (idempotent)" || fail "run 2 changed files"
summary=$(sed -n '/^== Summary/,/^[A-Z]/p' <<<"$out2")
grep -q '^  present ' <<<"$summary" && ! grep -Eq '^  (installed|failed|blocked|needs|manual)' <<<"$summary" &&
  grep -q "nothing to add" <<<"$out2" && ! grep -q "would back up\|backup:" <<<"$out2" &&
  pass "run 2: every tool present, every rule already merged" || fail "run 2 summary: $summary"

# ------------------------------------------------------------------------------------------- E. units, linger, docker group
H=$TMP/linger; mkdir -p "$H"; : > "$LOG"
HOME=$H PATH=$BASEPATH python3 - "$KIT" >/dev/null <<'PY' && pass "an enabled user unit turns lingering on" || fail "linger"
import argparse, json, sys
sys.path.insert(0, sys.argv[1] + "/install")
import lib
a = argparse.Namespace(dry_run=False, harness=False, os="debian", yes=True, optional="all", only=[], profiles=["odin"],
                       ignore_hardware=False)
i = lib.Installer(a)
i.rows = {"vault-rag": {"skip": None, "optional": False, "tool": {}}}
i.status = {"vault-rag": "present"}
odin = lib.profile("odin")
# enable forced on a copy: whether a profile enables a unit is the profile's call, lingering is the installer's
i.automations([{"name": "odin", "automations": [dict(x, enable=True) for x in odin["automations"]
                                                if x["name"] == "vault-rag-reindex"]}])
log = open(lib.os.environ.get("STUB_LOG")).read()
assert "systemctl --user enable --now vault-rag-reindex.timer" in log, log
assert "loginctl enable-linger" in log, log
assert not i.failed, i.failed
PY
: > "$LOG"
KIT_DOCKER_SG=1 BASH_ENV=$KIT/install/shell-env.sh PATH=$BASEPATH bash -c 'docker ps -a --format "{{.Names}}"'
grep -q 'sg docker -c docker ps -a --format' "$LOG" && pass "right after a Docker install, docker runs through sg" ||
  fail "docker through sg: $(cat "$LOG")"

if [ "$(id -u)" != 0 ]; then
  out=$(HOME=$TMP/linger PATH=$BASEPATH python3 - "$KIT" <<'PY'
import argparse, sys, types
sys.path.insert(0, sys.argv[1] + "/install")
import lib
lib.grp.getgrnam = lambda name: types.SimpleNamespace(gr_mem=[], gr_gid=999)   # not a member yet
a = argparse.Namespace(dry_run=True, harness=False, os="debian", yes=True, optional="", only=[], profiles=["heimdall"],
                       ignore_hardware=False)
lib.Installer(a).docker_group()
PY
)
  grep -q "+ sudo usermod -aG docker " <<<"$out" && pass "not in the docker group yet: the installer adds you (says why)" ||
    fail "docker group step: $out"
fi
H=$TMP/registry; mkdir -p "$H/AI_Projects/_system/automations"
printf '# Automation registry\n\n| Automation | Schedule | Lane | Scope | How to kill |\n|---|---|---|---|---|\n| log trim (retained 30 days) | daily | local | logs | rm the cron line |\n' \
  > "$H/AI_Projects/_system/automations/REGISTRY.md"
HOME=$H PATH=$BASEPATH python3 - "$KIT" >/dev/null <<'PY' && pass "registry: a mention of \"retained\" is no row for retain.timer; a second run adds none" || fail "registry match"
import argparse, sys
sys.path.insert(0, sys.argv[1] + "/install")
import lib
a = argparse.Namespace(dry_run=False, harness=False, os="debian", yes=True, optional="", only=[], profiles=["odin"],
                       ignore_hardware=False)
i = lib.Installer(a)
ret = next(x for x in lib.profile("odin")["automations"] if x["unit"].endswith("/retain.timer"))
reg = lib.home() / "AI_Projects/_system/automations/REGISTRY.md"
i.register("odin", ret)
i.register("odin", ret)
text = reg.read_text()
assert "retained 30 days" in text and len([ln for ln in text.splitlines() if "retain.timer" in ln]) == 1, text
assert len(list(reg.parent.glob("REGISTRY.md.bak-*"))) == 1
PY
mk sudo 'while [ $# -gt 0 ] && [ "${1#-}" != "$1" ]; do shift; done; exec "$@"'   # runs the command as you
H=$TMP/sudo; mkdir -p "$H/sys"; printf 'mine\n' > "$H/sys/fleet-wake"
HOME=$H PATH=$BASEPATH python3 - "$KIT" "$H/sys/fleet-wake" >/dev/null <<'PY' && pass "a sudo copy that replaces a file backs it up first" || fail "sudo copy backup"
import argparse, sys
sys.path.insert(0, sys.argv[1] + "/install")
import lib
a = argparse.Namespace(dry_run=False, harness=True, os="debian", yes=True, optional="", only=[], profiles=["heimdall"],
                       ignore_hardware=False)
i = lib.Installer(a)
dest = lib.Path(sys.argv[2])
step = {"do": "copy", "sudo": True, "harness": True, "src": "framework/_tools/fleet-wake/fleet-wake", "dest": str(dest),
        "mode": "755"}
assert i.place(step) == "ok"
baks = list(dest.parent.glob("fleet-wake.bak-*"))
assert len(baks) == 1 and baks[0].read_text() == "mine\n", baks
assert dest.read_bytes() == (lib.KIT / step["src"]).read_bytes() and i.backups
assert i.place(step) == "ok" and len(list(dest.parent.glob("fleet-wake.bak-*"))) == 1   # the same file: no new backup
PY
rm -f "$STUB/sudo"
python3 - "$KIT" <<'PY' && pass "every POST_UP entry names a compose service of a profile" || fail "POST_UP drift"
import sys
sys.path.insert(0, sys.argv[1] + "/install")
import lib
have = {(s["compose"], s["service"]) for n in lib.PROFILE_NAMES for s in lib.profile(n)["services"] if s["kind"] == "compose"}
sys.exit(0 if set(lib.POST_UP) <= have else 1)
PY

# ------------------------------------------------------------------------------------------- G. security fixes
# S15: an atomic write never exposes a restrictive file and never writes through a planted name or link.
H=$TMP/atomic; mkdir -p "$H"
HOME=$H PATH=$BASEPATH python3 - "$KIT" >/dev/null <<'PY' && pass "write_atomic: temp file 0600 from creation to rename, a planted link is never followed, nothing left on failure" || fail "write_atomic permissions"
import os, stat, sys
sys.path.insert(0, sys.argv[1] + "/install")
import lib
d = lib.home() / ".claude"; d.mkdir()
dest, victim = d / "settings.json", lib.home() / "victim"
dest.write_text('{"token": "old"}'); os.chmod(dest, 0o600); victim.write_text("keep me")
os.symlink(victim, d / ".settings.json.kit-tmp")          # the old predictable temp name, planted as a link
os.umask(0o022)
seen, real_mkstemp, real_replace = [], lib.tempfile.mkstemp, lib.os.replace
def mkstemp(**kw):
    fd, tmp = real_mkstemp(**kw)
    seen.append(stat.S_IMODE(os.stat(tmp).st_mode))        # right after creation
    return fd, tmp
def replace(src, dst):
    if dst == dest:
        seen.append(stat.S_IMODE(os.stat(src).st_mode))    # all content written, just before the rename
        assert open(src).read() == '{"token": "new"}'
    real_replace(src, dst)
lib.tempfile.mkstemp, lib.os.replace = mkstemp, replace
lib.write_atomic(dest, '{"token": "new"}')
assert seen == [0o600, 0o600], seen
assert stat.S_IMODE(dest.stat().st_mode) == 0o600 and dest.read_text() == '{"token": "new"}'
assert victim.read_text() == "keep me" and os.path.islink(d / ".settings.json.kit-tmp")
new = d / "new.json"; lib.write_atomic(new, "x")           # a new file gets what the umask gives
assert stat.S_IMODE(new.stat().st_mode) == 0o644
def boom(src, dst):
    raise OSError("disk full")
lib.os.replace = boom
try:
    lib.write_atomic(dest, "never")
except OSError:
    pass
assert dest.read_text() == '{"token": "new"}'
assert sorted(x.name for x in d.iterdir()) == [".settings.json.kit-tmp", "new.json", "settings.json"], list(d.iterdir())
PY

# S12: a home with a space, an apostrophe, $ and a backslash. Every spelling of the home in a hook command renders
# to one word; the rendered dispatcher command still runs (and blocks a secret read); a launch guard is kept.
H="$TMP/te st'o\$x\\y"; mkdir -p "$H/.claude"; cp -R "$KIT/framework/harness/hooks" "$H/.claude/hooks"
out=$(HOME=$H PATH=$BASEPATH python3 - "$KIT" "$H" 2>&1 <<'PY'
import argparse, json, os, shlex, subprocess, sys
sys.path.insert(0, sys.argv[1] + "/install")
import lib
H, kit = sys.argv[2], sys.argv[1]
disp = H + "/.claude/hooks/dispatch/dispatch.sh"
fails = []
guard = ("[ -r \"$HOME/.claude/hooks/dispatch/dispatch.sh\" ] || { echo 'dispatcher missing' >&2; exit 2; }; "
         "bash \"$HOME/.claude/hooks/dispatch/dispatch.sh\" PreToolUse")
forms = ['bash "$HOME/.claude/hooks/dispatch/dispatch.sh" PreToolUse', "bash $HOME/.claude/hooks/dispatch/dispatch.sh PreToolUse",
         "bash ${HOME}/.claude/hooks/dispatch/dispatch.sh PreToolUse", "bash ~/.claude/hooks/dispatch/dispatch.sh PreToolUse",
         "bash '$HOME/.claude/hooks/dispatch/dispatch.sh' PreToolUse", guard]
env = {"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"}     # the rendered command must not need $HOME at all
for f in forms:
    r = lib.render_command(f, H)
    if f == guard and "|| { echo 'dispatcher missing' >&2; exit 2; };" not in r:
        fails.append(f"guard changed: {r}")
    # run with the dispatcher swapped for a probe that prints its path and argument
    os.rename(disp, disp + ".real"); open(disp, "w").write('printf "%s|%s" "$0" "$1"\n')
    for shell in ("bash", "sh"):
        p = subprocess.run([shell, "-c", r], capture_output=True, text=True, env=env)
        if p.stdout != f"{disp}|PreToolUse":
            fails.append(f"{shell}: {f!r} -> {r!r} ran {p.stdout!r} {p.stderr[-200:]!r}")
    os.replace(disp + ".real", disp)
    if f != guard and shlex.split(r) != ["bash", disp, "PreToolUse"]:
        fails.append(f"not three words: {shlex.split(r)}")
# the launch guard still blocks when the dispatcher cannot start
os.rename(disp, disp + ".real")
p = subprocess.run(["bash", "-c", lib.render_command(guard, H)], capture_output=True, text=True, env=env)
os.replace(disp + ".real", disp)
if p.returncode != 2:
    fails.append(f"guard without a dispatcher: rc {p.returncode}")
# the real reference settings: the PreToolUse dispatcher denies an SSH key read under this home
a = argparse.Namespace(dry_run=True, harness=True, os="debian", yes=True, optional="", only=[], profiles=["odin"],
                       ignore_hardware=False)
i = lib.Installer(a)
d = i.render_settings(json.load(open(kit + "/framework/harness/settings.reference.json")))
cmd = d["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
inp = json.dumps({"session_id": "t", "hook_event_name": "PreToolUse", "tool_name": "Read", "cwd": H,
                  "tool_input": {"file_path": H + "/.ssh/id_ed25519"}})
p = subprocess.run(["bash", "-c", cmd], input=inp, capture_output=True, text=True, env={"HOME": H, "PATH": "/usr/bin:/bin"})
if '"block"' not in p.stdout and '"deny"' not in p.stdout:
    fails.append(f"secret read not denied: rc {p.returncode} {p.stdout[:200]!r} {p.stderr[-200:]!r}")
if d["env"]["AI_PROJECTS_ROOT"] != H + "/AI_Projects":
    fails.append(f"env value not spelled out plainly: {d['env']['AI_PROJECTS_ROOT']!r}")
if i.hook_missing(cmd) is not None:
    fails.append(f"hook_missing misread an installed path: {i.hook_missing(cmd)}")
miss = lib.render_command('bash "$HOME/.claude/hooks/nope dir/x.sh" Stop', H)
if i.hook_missing(miss) != H + "/.claude/hooks/nope dir/x.sh":
    fails.append(f"hook_missing did not name the whole missing path: {i.hook_missing(miss)!r}")
print("\n".join(fails) or "OK")
PY
)
[ "$out" = OK ] && pass "a home with a space, ' \$ and \\: hook commands render to one word, run under bash and sh, keep a launch guard, and the dispatcher still denies a secret read" ||
  fail "home with special characters: $out"

# S12: unattended mode (bypassPermissions, no prompt) is merged only when the safety dispatcher hook is wired too.
out=$(HOME=$TMP/unattended PATH=$BASEPATH python3 - "$KIT" 2>&1 <<'PY'
import copy, json, sys
sys.path.insert(0, sys.argv[1] + "/install")
import lib
kit = json.load(open(sys.argv[1] + "/framework/harness/settings.reference.json"))
assert kit["permissions"]["defaultMode"] == "bypassPermissions" and kit["skipDangerousModePermissionPrompt"] is True
def merge(mine, hook_ok):
    merged, added, kept, skipped = copy.deepcopy(mine), [], [], []
    lib.merge_values(merged, kit, "", added, kept, skipped, hook_ok)
    lib.hold_unrestricted(mine, merged, added, skipped)
    return merged, added, skipped
no_disp = lambda c: "/x/dispatch.sh" if "dispatch/dispatch.sh" in c else None
m, added, skipped = merge({}, no_disp)                     # dispatcher not installed: unattended mode waits
assert "defaultMode" not in m["permissions"] and "skipDangerousModePermissionPrompt" not in m, m.get("permissions")
assert any("bypassPermissions waits" in x for x in skipped), skipped
m, added, skipped = merge({"permissions": {"allow": []}}, no_disp)
assert "defaultMode" not in m["permissions"] and "permissions.defaultMode" not in added, added
m, _, _ = merge({}, lambda c: None)                         # dispatcher wired: both keys arrive
assert m["permissions"]["defaultMode"] == "bypassPermissions" and m["skipDangerousModePermissionPrompt"] is True
mine = {"permissions": {"defaultMode": "bypassPermissions"}, "skipDangerousModePermissionPrompt": True}
m, _, _ = merge(mine, no_disp)                              # your own choice stays yours
assert m["permissions"]["defaultMode"] == "bypassPermissions" and m["skipDangerousModePermissionPrompt"] is True
print("OK")
PY
)
[ "$out" = OK ] && pass "bypassPermissions and the skipped prompt are merged only together with the safety dispatcher hook" ||
  fail "unattended mode without the dispatcher: $out"

# S10: a pinned piped script runs only after its sha256 matched; a changed upstream file is refused before it runs.
H=$TMP/pinned; mkdir -p "$H"
out=$(HOME=$H PATH=$BASEPATH python3 - "$KIT" "$H" 2>&1 <<'PY'
import argparse, hashlib, sys
sys.path.insert(0, sys.argv[1] + "/install")
import lib
H = sys.argv[2]
url = "https://example.invalid/tool/v1.2.3/install.sh"
good = f'printf "%s %s" "$FOO" "$1" > {H}/ran\n'.encode()
bad = f'touch {H}/ran\n'.encode()
calls = []
def fake(body):
    def fetch(u, dest):
        calls.append(u)
        open(dest, "xb").write(body)
        return hashlib.sha256(body).hexdigest()
    return fetch
tool = {"id": "x", "methods": {"debian": f"curl -fsSL {url} | FOO=1 sh -s -- --flag", "manual": "see https://example.invalid"},
        "script_sha256": {url: hashlib.sha256(good).hexdigest()}}
a = argparse.Namespace(dry_run=False, harness=False, os="debian", yes=True, optional="", only=[], profiles=["odin"],
                       ignore_hardware=False)
i = lib.Installer(a)
row = lambda t, k="debian": {"id": "x", "tool": t, "method": k, "cmd": t["methods"][k]}
lib.fetch = fake(bad)
assert i.run_method(row(tool)) == "fail"
assert not lib.os.path.exists(H + "/ran"), "a mismatched script ran"
lib.fetch = fake(good)
assert i.run_method(row(tool)) == "ok"
assert open(H + "/ran").read() == "1 --flag", open(H + "/ran").read()
lib.os.remove(H + "/ran")
n = len(calls)
assert i.run_method(row(dict(tool, script_sha256={}))) == "fail"                       # no recorded hash
t2 = dict(tool, methods=dict(tool["methods"], debian=f"curl -fsSL {url} | sudo sh"))   # not the pinned shape
assert i.run_method(row(t2)) == "fail"
assert len(calls) == n and not lib.os.path.exists(H + "/ran"), "refused scripts were fetched or ran"
def cut(u, dest):                                                                      # a download cut off midway
    raise lib.http.client.IncompleteRead(b"x", 10)
lib.fetch = cut
assert i.run_method(row(tool)) == "fail" and not lib.os.path.exists(H + "/ran")
print("OK")
PY
)
grep -q "upstream changed: https://example.invalid/tool/v1.2.3/install.sh has sha256" <<<"$out" && [ "$(tail -1 <<<"$out")" = OK ] &&
  pass "a piped installer runs only after its sha256 matches; a changed, unhashed or unpinned script is refused before it runs" ||
  fail "pinned scripts: $(tail -5 <<<"$out")"

# S4: uptime-kuma, ntfy and Pi-hole never start without the server lane (its firewall): --only pulls it in, a failed
# setup leaves them stopped even when their image is already there, and the template listens on 127.0.0.1.
out=$(HOME=$TMP/plan-debian PATH=$BASEPATH KIT_FAKE_HW='{"ram_gb":8}' "$INSTALL" --profile heimdall --only uptime-kuma \
  --dry-run --os debian 2>&1)
grep -Eq '^  [a-z]+ +server-lane-heimdall ' <<<"$out" && pass "heimdall --only uptime-kuma: the plan includes the server lane (firewall)" ||
  fail "--only uptime-kuma left out server-lane-heimdall"
out=$(HOME=$TMP/plan-debian PATH=$BASEPATH python3 - "$KIT" 2>&1 <<'PY'
import argparse, sys
sys.path.insert(0, sys.argv[1] + "/install")
import lib
for svc in ("uptime-kuma", "ntfy", "pihole"):
    ids = [r["id"] for r in lib.plan(["heimdall"], "debian", only=[svc], probe_state=False)]
    assert "server-lane-heimdall" in ids and ids.index("server-lane-heimdall") < ids.index(svc), (svc, ids)
a = argparse.Namespace(dry_run=True, harness=False, os="debian", yes=True, optional="all", only=["uptime-kuma", "ntfy"],
                       profiles=["heimdall"], ignore_hardware=False)
i = lib.Installer(a)
i.all_optional = True
i.rows = {r["id"]: r for r in lib.plan(["heimdall"], "debian", only=["uptime-kuma", "ntfy"], probe_state=False)}
i.status = {"git": "present", "docker": "present", "server-lane-heimdall": "failed",
            "uptime-kuma": "present", "ntfy": "present"}
i.services([lib.profile("heimdall")])
PY
)
grep -q "uptime-kuma: not started, its tool server-lane-heimdall is failed" <<<"$out" &&
  grep -q "ntfy: not started, its tool server-lane-heimdall is failed" <<<"$out" && ! grep -q "docker compose" <<<"$out" &&
  pass "a failed server setup: uptime-kuma and ntfy stay stopped although their images are present" ||
  fail "failed setup must keep the listeners stopped: $out"
C=$KIT/servers/compose/heimdall/compose.yaml
grep -Eq '^      UPTIME_KUMA_HOST: 127\.0\.0\.1 ' "$C" && grep -Eq '^      NTFY_LISTEN_HTTP: "127\.0\.0\.1:8090"' "$C" &&
  sed -e '/network_mode: host/d' -e 's/^    # ports: /    ports: /' "$C" | grep -c '"127.0.0.1:' | grep -qx 5 &&
  ! sed -e 's/^    # ports: /    ports: /' "$C" | grep -E '^    ports: ' | grep -oE '"[^"]+"' |
    grep -vE '^"(127\.0\.0\.1:|53:53/(tcp|udp)")' | grep -q . &&
  ! sed -e '/network_mode: host/d' "$C" | grep -Eq 'UPTIME_KUMA_HOST|NTFY_LISTEN_HTTP' &&
  pass "heimdall template: uptime-kuma and ntfy listen on 127.0.0.1; the macOS ports: variant publishes on 127.0.0.1 only" ||
  fail "heimdall template bind addresses"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  cfg=$(cd "$KIT/servers/compose/heimdall" && docker compose -f compose.yaml config 2>&1)
  grep -q 'UPTIME_KUMA_HOST: 127.0.0.1' <<<"$cfg" && grep -q 'NTFY_LISTEN_HTTP: 127.0.0.1:8090' <<<"$cfg" &&
    pass "docker compose config: the listeners resolve to 127.0.0.1" || fail "docker compose config: $cfg"
fi

# ------------------------------------------------------------------------------------------- F. outside HOME (containers)
if [ -n "$SYSTEM" ] && [ "$OS" = debian ]; then
  H=$TMP/server; mkdir -p "$H/.config/ai-workflow-kit"; : > "$LOG"
  printf 'HEIMDALL_HOST=127.0.0.1\n' > "$H/.config/ai-workflow-kit/hosts.env"
  mkdir -p /opt/homeserver/heimdall   # the server lane done (its verify: this folder + the firewall unit, stubbed active)
  mk docker 'echo "$PWD: docker $*" >> "$STUB_LOG"; exit 0'
  mk curl 'exit 0'
  srun() { HOME=$H PATH=$BASEPATH KIT_FAKE_HW='{"ram_gb":8}' "$INSTALL" --profile heimdall \
    --only hosts-env,docker,uptime-kuma,ntfy,fleet-wake --yes --optional all 2>&1; }
  out=$(srun); rc=$?
  [ $rc = 0 ] && pass "server run 1 exits 0" || { fail "server run 1 (rc $rc)"; tail -25 <<<"$out"; }
  P=/opt/homeserver/heimdall
  same_file "$P/compose.yaml" "$KIT/servers/compose/heimdall/compose.yaml" && [ "$(stat -c %a "$P/.env")" = 600 ] &&
    pass "compose project made under /opt once, .env from env.example (0600)" || fail "compose project copy"
  grep -q "fill in $P/.env" <<<"$out" && ! grep -q "$P: docker compose" "$LOG" &&
    pass "a .env made this run waits for you (no service of that project started)" || fail "fresh .env must wait"
  grep -q "$H/services/ollama-embed: docker compose --env-file $H/.config/ai-workflow-kit/hosts.env up -d ollama" "$LOG" &&
    grep -q "ollama pull nomic-embed-text:v1.5" "$LOG" && grep -q "ollama-embed is healthy" <<<"$out" &&
    pass "ollama-embed: compose up in its own project dir, then the model, then the health check" ||
    fail "ollama-embed: $(cat "$LOG")"
  same_file /usr/local/bin/fleet-wake "$KIT/framework/_tools/fleet-wake/fleet-wake" && [ "$(stat -c %a /usr/local/bin/fleet-wake)" = 755 ] &&
    pass "sudo copy to /usr/local/bin (mode 755)" || fail "sudo copy"
  [ -f "$H/.config/systemd/user/fleet-wake.timer" ] && grep -q 'fleet-wake' "$H/AI_Projects/_system/automations/REGISTRY.md" &&
    pass "fleet-wake units and registry row" || fail "fleet-wake units"
  printf '# mine\n' >> "$P/compose.yaml"; printf 'TZ=Europe/Berlin\n' >> "$P/.env"
  cp "$P/compose.yaml" "$TMP/compose.mine"; cp "$P/.env" "$TMP/env.mine"; : > "$LOG"
  out=$(srun); rc=$?
  [ $rc = 0 ] && grep -q "$P: docker compose up -d uptime-kuma" "$LOG" && grep -q "$P: docker compose up -d ntfy" "$LOG" &&
    pass "server run 2 starts the stack in /opt/homeserver/heimdall" || { fail "server run 2 (rc $rc)"; cat "$LOG"; }
  same_file "$P/compose.yaml" "$TMP/compose.mine" && same_file "$P/.env" "$TMP/env.mine" &&
    pass "your edited compose.yaml and .env are never touched" || fail "compose project edited"
  sum=$(tree_sum $(roots "$H") /opt/homeserver /usr/local/bin)
  out=$(srun); rc=$?
  [ $rc = 0 ] && [ "$(tree_sum $(roots "$H") /opt/homeserver /usr/local/bin)" = "$sum" ] && pass "server run 3 changed nothing" ||
    fail "server run 3 (rc $rc)"
  rm -f "$STUB/docker" "$STUB/curl"
elif [ -n "$SYSTEM" ]; then
  echo "SKIP outside-HOME checks: the server profiles (loki, heimdall, thor) are Debian/macOS only, this is $OS"
fi

echo "$((n - fails))/$n passed"
[ $fails = 0 ] && echo PASS || echo FAIL
exit $((fails > 0))
